"""DN2026_0105 회귀 — DB 외부 트리거(scheduler 출처) 대응.

SQL 함수 동작은 로컬 PostgreSQL 16 + pg_net/vault/cron 대역으로 별도 검증했다(28/28).
여기서는 레포 안의 계약(게이트 판정, 워크플로 식, SQL 파일 핵심 문구)을 고정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reply_engine import schedule
from scripts import reply_schedule_gate as gate

ROOT = Path(__file__).resolve().parents[1]
YML = (ROOT / ".github/workflows/reply_engine.yml").read_text(encoding="utf-8")
SQL = (ROOT / "docs/sql/dn2026_0105_kr_reply_dispatch.sql").read_text(encoding="utf-8")

NOW = datetime(2026, 11, 15, 6, 4, tzinfo=UTC)  # KST 15:04


def _state(next_run: datetime) -> dict:
    return {"account": "kr_main", "next_run_at": next_run.isoformat(),
            "last_tick_at": (next_run - timedelta(minutes=120)).isoformat()}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    alerts: list[str] = []
    monkeypatch.setattr(gate, "send_admin_alert", lambda text: alerts.append(text) or False)
    return alerts


def _install(monkeypatch, states: list[dict | None], claim_ok: bool = True):
    calls = {"load": 0, "claim": 0, "upsert": 0}

    def load():
        calls["load"] += 1
        return states[min(calls["load"] - 1, len(states) - 1)]

    def claim(row, prev):
        calls["claim"] += 1
        return claim_ok

    monkeypatch.setattr(gate, "load_state", load)
    monkeypatch.setattr(gate, "claim", claim)
    return calls


# ── ① scheduler 출처 = schedule과 같은 일반 판정 ──────────────────

def test_scheduler_dispatch_not_due_skips(monkeypatch, env):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("REPLY_DISPATCH_SOURCE", "scheduler")
    _install(monkeypatch, [_state(NOW + timedelta(minutes=30))])
    d = gate.run_decide(NOW)
    assert d.decision == schedule.SKIP_NOT_DUE


def test_scheduler_dispatch_due_claims_with_cas_and_logs_late(monkeypatch, env, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("REPLY_DISPATCH_SOURCE", "scheduler")
    calls = _install(monkeypatch, [_state(NOW - timedelta(minutes=3))])
    d = gate.run_decide(NOW)
    out = capsys.readouterr().out
    assert d.decision == schedule.RUN_DUE and calls["claim"] == 1
    assert "late_min=3.0" in out and "source=scheduler" in out and "claim=ok" in out


def test_scheduler_dispatch_respects_night(monkeypatch, env):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("REPLY_DISPATCH_SOURCE", "scheduler")
    _install(monkeypatch, [_state(NOW - timedelta(hours=5))])
    night = datetime(2026, 11, 15, 18, 20, tzinfo=UTC)  # KST 03:20
    assert gate.run_decide(night).decision == schedule.SKIP_NIGHT


@pytest.mark.parametrize("source", ["manual", ""])
def test_manual_dispatch_still_always_runs(monkeypatch, env, source):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("REPLY_DISPATCH_SOURCE", source)
    _install(monkeypatch, [_state(NOW + timedelta(minutes=60))])

    class _T:
        def upsert(self, row):
            return self

        def execute(self):
            return type("R", (), {"data": [1]})()

    monkeypatch.setattr(gate.store, "get_client",
                        lambda: type("C", (), {"table": lambda self, name: _T()})())
    d = gate.run_decide(NOW)
    assert d.decision == schedule.RUN_DUE and d.reason == "workflow_dispatch"


# ── ② 선점 경합: 다른 실행이 먼저 선점했으면 오경보 없이 SKIP ─────────

def test_cas_miss_claimed_by_other_is_not_due_without_alert(monkeypatch, env):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("REPLY_DISPATCH_SOURCE", raising=False)
    _install(monkeypatch, [_state(NOW - timedelta(minutes=2)),
                           _state(NOW + timedelta(minutes=110))], claim_ok=False)
    d = gate.run_decide(NOW)
    assert d.decision == schedule.SKIP_NOT_DUE and "선점 경합" in d.reason
    assert env == []


def test_cas_miss_without_other_claim_stays_fail_closed(monkeypatch, env):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    _install(monkeypatch, [_state(NOW - timedelta(minutes=2))], claim_ok=False)
    d = gate.run_decide(NOW)
    assert d.decision == schedule.SKIP_GATE_ERROR and env


def test_cas_miss_reload_error_stays_fail_closed(monkeypatch, env):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    seq = iter([_state(NOW - timedelta(minutes=2))])

    def load():
        try:
            return next(seq)
        except StopIteration:
            raise RuntimeError("db down") from None

    monkeypatch.setattr(gate, "load_state", load)
    monkeypatch.setattr(gate, "claim", lambda row, prev: False)
    assert gate.run_decide(NOW).decision == schedule.SKIP_GATE_ERROR


# ── ③ 워크플로 계약 ───────────────────────────────────────────

def test_workflow_source_input_and_mode_expression():
    assert "options: [manual, scheduler]" in YML and "default: manual" in YML
    assert ("REQUESTED_MODE: ${{ (github.event_name == 'workflow_dispatch' && "
            "inputs.source != 'scheduler') && inputs.mode || vars.REPLY_MODE "
            "|| 'dry_run' }}") in YML
    assert '"$DISPATCH_SOURCE" != "scheduler"' in YML        # 수동 live 확인은 사람 실행에만
    assert "REPLY_DISPATCH_SOURCE:  ${{ inputs.source }}" in YML
    # GitHub cron 백업 유지
    assert '- cron: "8,26,44 * * * *"' in YML and '- cron: "16,36,52 * * * *"' in YML


# ── ④ SQL 계약 ───────────────────────────────────────────────

def test_sql_contract():
    assert ("https://api.github.com/repos/yumens2-byte/investment-os-kr/actions/workflows/"
            "reply_engine.yml/dispatches") in SQL
    assert "'inputs', jsonb_build_object('source', 'scheduler'))" in SQL   # mode 미전달
    assert "values ('mode', 'dry_run')" in SQL                             # 기본 호출 없음
    assert "ds.name = 'github_reply_dispatch_pat'" in SQL
    assert "cron.schedule('kr_reply_dispatch', '4-59/10 * * * *'" in SQL
    assert "cron.schedule('kr_reply_dispatch_reconcile', '7-59/10 * * * *'" in SQL
    assert SQL.count("security definer") == 2 and SQL.count("set search_path to ''") == 2
    assert "revoke all on function private.kr_reply_dispatch(timestamptz)" in SQL
    assert "interval '15 minutes'" in SQL and "= 3 then" in SQL            # 중복·야간


def test_versions():
    assert gate.VERSION == "1.2.0"
    assert "# v2.5.0 (2026-11-15, DN2026_0105)" in YML
