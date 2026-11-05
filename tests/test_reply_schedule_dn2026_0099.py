"""DN2026_0099 — X Reply Engine 40~80분 랜덤 간격 실행 게이트 테스트 (STD-CI-10).

① 시각 행렬 ② next 계산 ③ 7일 운영 시뮬레이션 ④ yml 정합. Supabase는 mock.
"""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reply_engine import schedule as sch
from scripts import reply_schedule_gate as gate

KST = sch.KST
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/reply_engine.yml"


def kst(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=KST).astimezone(UTC)


def state(next_run_at, last_tick_at=None):
    return {
        "account": "kr_main",
        "next_run_at": next_run_at.isoformat(),
        "last_tick_at": last_tick_at.isoformat() if last_tick_at else None,
    }


# ---------------------------------------------------------------------------
# ① 시각 행렬
# ---------------------------------------------------------------------------

PAST = kst(2026, 11, 6, 0, 0)


@pytest.mark.parametrize(
    "now,expected",
    [
        (kst(2026, 11, 6, 2, 59, 59), sch.RUN_DUE),
        (kst(2026, 11, 6, 3, 0, 0), sch.SKIP_NIGHT),
        (kst(2026, 11, 6, 3, 59, 59), sch.SKIP_NIGHT),
        (kst(2026, 11, 6, 4, 0, 0), sch.RUN_DUE),
    ],
)
def test_night_boundaries(now, expected):
    assert sch.decide(now, state(PAST)).decision == expected


def test_night_wins_over_missing_row_and_db_error():
    now = kst(2026, 11, 6, 3, 30)
    assert sch.decide(now, None).decision == sch.SKIP_NIGHT
    assert sch.decide(now, None, load_failed=True).decision == sch.SKIP_NIGHT


def test_missing_row_self_heals():
    d = sch.decide(kst(2026, 11, 6, 12, 0), None)
    assert d.decision == sch.RUN_DUE and "행 없음" in d.reason


def test_unparseable_next_self_heals():
    d = sch.decide(kst(2026, 11, 6, 12, 0), {"next_run_at": "garbage"})
    assert d.decision == sch.RUN_DUE


def test_next_just_before_and_after():
    nxt = kst(2026, 11, 6, 12, 0)
    last = nxt - timedelta(minutes=50)
    before = sch.decide(nxt - timedelta(seconds=1), state(nxt, last))
    at = sch.decide(nxt, state(nxt, last))
    after = sch.decide(nxt + timedelta(seconds=1), state(nxt, last))
    assert before.decision == sch.SKIP_NOT_DUE
    assert at.decision == sch.RUN_DUE
    assert after.decision == sch.RUN_DUE
    assert not after.stale


def test_dispatch_always_runs():
    for now in (kst(2026, 11, 6, 3, 30), kst(2026, 11, 6, 12, 0)):
        fut = now + timedelta(hours=1)
        assert sch.decide(now, state(fut), is_dispatch=True).decision == sch.RUN_DUE
        assert sch.decide(now, None, is_dispatch=True, load_failed=True).decision == sch.RUN_DUE


def test_db_error_fail_closed():
    d = sch.decide(kst(2026, 11, 6, 12, 0), None, load_failed=True)
    assert d.decision == sch.SKIP_GATE_ERROR


def test_stale_flag():
    now = kst(2026, 11, 6, 12, 0)
    d = sch.decide(now, state(now - timedelta(hours=2), now - timedelta(hours=3)))
    assert d.decision == sch.RUN_DUE and d.stale and "stale" in d.reason


def test_decisions_are_descriptive_strings():
    assert set(sch.DECISIONS) == {"RUN_DUE", "SKIP_NOT_DUE", "SKIP_NIGHT", "SKIP_GATE_ERROR"}


# ---------------------------------------------------------------------------
# ② next 계산
# ---------------------------------------------------------------------------


def test_next_gap_range_daytime():
    rng = random.Random(7)
    tick = kst(2026, 11, 6, 12, 3)
    gaps = [sch.compute_next_run(tick, rng)[1] for _ in range(2000)]
    assert min(gaps) >= 40.0 and max(gaps) <= 70.0


class FixedRng:
    def __init__(self, *values):
        self.values = list(values)

    def uniform(self, a, b):
        v = self.values.pop(0)
        assert a <= v <= b
        return v


def test_next_avoids_night_hour():
    # 02:30 + 40 = 03:10 → KST 04:00 + 5분
    nxt, planned = sch.compute_next_run(kst(2026, 11, 6, 2, 30), FixedRng(40.0, 5.0))
    assert nxt == kst(2026, 11, 6, 4, 5)
    assert planned == 95.0


def test_next_boundaries_around_night():
    # 02:19 + 40 = 02:59 → 그대로
    nxt, _ = sch.compute_next_run(kst(2026, 11, 6, 2, 19), FixedRng(40.0))
    assert nxt == kst(2026, 11, 6, 2, 59)
    # 02:20 + 40 = 03:00 → 04:00 + 0
    nxt, _ = sch.compute_next_run(kst(2026, 11, 6, 2, 20), FixedRng(40.0, 0.0))
    assert nxt == kst(2026, 11, 6, 4, 0)
    # 02:50 + 70 = 04:00 → 그대로 (03시대 아님)
    nxt, _ = sch.compute_next_run(kst(2026, 11, 6, 2, 50), FixedRng(70.0))
    assert nxt == kst(2026, 11, 6, 4, 0)
    # KST 자정 직전(23:50) 실행 → 익일 00:30~01:00
    nxt, _ = sch.compute_next_run(kst(2026, 11, 6, 23, 50), FixedRng(40.0))
    assert nxt == kst(2026, 11, 7, 0, 30)


def test_build_claim_row_gap():
    tick = kst(2026, 11, 6, 12, 3)
    prev = state(tick, tick - timedelta(minutes=52.5))
    row = sch.build_claim_row("kr_main", tick, prev, now=tick, rng=FixedRng(45.0))
    assert row["last_gap_min"] == 52.5
    assert row["last_decision"] == "RUN_DUE"
    assert sch.parse_ts(row["next_run_at"]) == tick + timedelta(minutes=45)
    row2 = sch.build_claim_row("kr_main", tick, None, now=tick, rng=FixedRng(45.0))
    assert row2["last_gap_min"] is None


# ---------------------------------------------------------------------------
# ③ 7일 운영 시뮬레이션 (decide 시점 선점, 실행 소요 0~25분 무작위)
# ---------------------------------------------------------------------------


def simulate(drop_prob: float, seed: int, days: int = 7, max_duration: float = 25.0) -> dict:
    rng = random.Random(seed)
    drop_rng = random.Random(seed + 1000)
    dur_rng = random.Random(seed + 2000)
    start = datetime(2026, 11, 9, 0, 3, tzinfo=UTC)  # cron 3-59/10 정렬
    st: dict | None = None
    runs: list[datetime] = []
    busy_until = start
    overlaps = 0
    long_run_skips = 0
    t = start
    end = start + timedelta(days=days)
    while t < end:
        if drop_rng.random() >= drop_prob:
            d = sch.decide(t, st)
            if d.decision == sch.RUN_DUE:
                if t < busy_until:
                    overlaps += 1
                runs.append(t)
                row = sch.build_claim_row("kr_main", d.tick_at, st, now=t, rng=rng)
                row.pop("_planned_gap_min")
                st = row  # decide 시점 선점 — 실행 종료를 기다리지 않음
                busy_until = t + timedelta(minutes=dur_rng.uniform(0.0, max_duration))
            elif t < busy_until and d.decision == sch.SKIP_NOT_DUE:
                long_run_skips += 1
        t += timedelta(minutes=10)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(runs, runs[1:])]
    day_gaps = [
        g for (a, b), g in zip(zip(runs, runs[1:]), gaps)
        if not any(sch.is_night(a + timedelta(minutes=m)) for m in range(0, int(g) + 1))
    ]
    per_day: dict = {}
    for r in runs:
        key = r.astimezone(KST).date()
        per_day[key] = per_day.get(key, 0) + 1
    full_days = [v for k, v in per_day.items()
                 if k != runs[0].astimezone(KST).date() and k != runs[-1].astimezone(KST).date()]
    return {
        "runs": runs,
        "gaps": gaps,
        "day_gaps": day_gaps,
        "night_runs": sum(1 for r in runs if sch.is_night(r)),
        "per_day_full": full_days,
        "overlaps": overlaps,
        "long_run_skips": long_run_skips,
    }


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_seven_day_simulation_no_drop(seed):
    r = simulate(0.0, seed)
    assert r["night_runs"] == 0 and r["overlaps"] == 0
    assert r["long_run_skips"] > 0  # 실행 중 도래한 틱이 선점 덕에 SKIP_NOT_DUE
    assert all(40.0 <= g <= 80.0 for g in r["day_gaps"]), r["day_gaps"]
    # 80분 초과 간격은 KST 03시대를 건너는 야간 이월 간격뿐이다 (하루 1회 이하)
    night_gaps = [g for g in r["gaps"] if g > 80.0]
    assert len(night_gaps) <= 7 + 1 and all(g <= 160.0 for g in night_gaps)
    assert min(r["per_day_full"]) >= 17 and max(r["per_day_full"]) <= 30
    print(
        f"\n[SIM drop=0 seed={seed}] runs={len(r['runs'])} "
        f"gap(min/max/avg, 야간 제외)={min(r['day_gaps']):.0f}/{max(r['day_gaps']):.0f}/"
        f"{sum(r['day_gaps']) / len(r['day_gaps']):.1f} 전체max={max(r['gaps']):.0f} "
        f"일실행 min/max={min(r['per_day_full'])}/{max(r['per_day_full'])} "
        f"night={r['night_runs']} overlap={r['overlaps']} 실행중SKIP={r['long_run_skips']}"
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_seven_day_simulation_drop_30(seed):
    r = simulate(0.3, seed)
    assert r["night_runs"] == 0 and r["overlaps"] == 0
    assert min(r["gaps"]) >= 40.0
    assert min(r["per_day_full"]) >= 10
    print(
        f"\n[SIM drop=0.3 seed={seed}] runs={len(r['runs'])} "
        f"gap(min/max/avg)={min(r['gaps']):.0f}/{max(r['gaps']):.0f}/"
        f"{sum(r['gaps']) / len(r['gaps']):.1f} "
        f"일실행 min/max={min(r['per_day_full'])}/{max(r['per_day_full'])} "
        f"night={r['night_runs']} overlap={r['overlaps']} 실행중SKIP={r['long_run_skips']}"
    )


def test_long_run_next_tick_is_not_due():
    """실행이 25분 걸려도 선점된 next_run_at 때문에 다음 틱들은 SKIP_NOT_DUE."""
    t0 = kst(2026, 11, 6, 12, 3)
    row = sch.build_claim_row("kr_main", t0, None, now=t0, rng=FixedRng(40.0))
    for m in (10, 20, 30):
        assert sch.decide(t0 + timedelta(minutes=m), row).decision == sch.SKIP_NOT_DUE
    assert sch.decide(t0 + timedelta(minutes=40), row).decision == sch.RUN_DUE


# ---------------------------------------------------------------------------
# gate CLI (Supabase mock)
# ---------------------------------------------------------------------------


class FakeTable:
    def __init__(self, db, fail_select=False, fail_write=False, cas_miss=False):
        self.db, self.fail_select, self.fail_write, self.cas_miss = (
            db, fail_select, fail_write, cas_miss)
        self._op, self._filters = None, {}

    def select(self, *_):
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def limit(self, *_):
        return self

    def upsert(self, row):
        self._op, self._row = "upsert", row
        return self

    def insert(self, row):
        self._op, self._row = "insert", row
        return self

    def update(self, row):
        self._op, self._row = "update", row
        return self

    def execute(self):
        self.db.setdefault("ops", []).append((self._op, dict(self._filters)))
        if self._op == "select":
            if self.fail_select:
                raise RuntimeError("db down")
            return SimpleNamespace(data=[self.db["row"]] if self.db.get("row") else [])
        if self.fail_write:
            raise RuntimeError("write denied")
        if self._op == "insert":
            if self.db.get("row"):
                raise RuntimeError("duplicate key")
            self.db["row"] = dict(self._row)
            return SimpleNamespace(data=[self._row])
        if self._op == "update":
            cur = self.db.get("row")
            if self.cas_miss or not cur or cur["next_run_at"] != self._filters.get("next_run_at"):
                return SimpleNamespace(data=[])
            cur.update(self._row)
            return SimpleNamespace(data=[cur])
        self.db["row"] = dict(self._row)
        return SimpleNamespace(data=[self._row])


def install_db(monkeypatch, db, **kw):
    tables = []

    def table(name):
        tables.append(name)
        return FakeTable(db, **kw)

    monkeypatch.setattr(gate.store, "get_client", lambda: SimpleNamespace(table=table))
    alerts: list[str] = []
    monkeypatch.setattr(gate, "send_admin_alert", lambda text: alerts.append(text) or False)
    return tables, alerts


def gh_files(monkeypatch, tmp_path, event="schedule"):
    out, summ = tmp_path / "out", tmp_path / "summary"
    out.write_text(""), summ.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    return out, summ


def test_gate_decide_skip_writes_summary_and_no_db_write(monkeypatch, tmp_path, capsys):
    out, summ = gh_files(monkeypatch, tmp_path)
    now = kst(2026, 11, 6, 12, 3)
    db = {"row": state(now + timedelta(minutes=20), now - timedelta(minutes=30))}
    tables, _ = install_db(monkeypatch, db)
    d = gate.run_decide(now)
    assert d.decision == "SKIP_NOT_DUE" and set(tables) == {"kr_reply_schedule"}
    assert [op for op, _ in db["ops"]] == ["select"]  # SKIP은 읽기만
    assert "decision=SKIP_NOT_DUE" in out.read_text()
    assert f"tick_at={now.isoformat()}" in out.read_text()
    assert "next_run_at" in summ.read_text()
    log = capsys.readouterr().out
    assert re.search(r"\[ReplySchedule\] v1\.0\.0 decide \| SKIP_NOT_DUE \| .+ \| .+ \| 30\.0", log)


def test_gate_decide_run_due_claims_with_cas(monkeypatch, tmp_path):
    out, _ = gh_files(monkeypatch, tmp_path)
    now = kst(2026, 11, 6, 12, 3)
    old_next = (now - timedelta(minutes=2)).isoformat()
    db = {"row": {"account": "kr_main", "next_run_at": old_next,
                  "last_tick_at": (now - timedelta(minutes=52)).isoformat()}}
    install_db(monkeypatch, db)
    d = gate.run_decide(now, rng=FixedRng(55.0))
    assert d.decision == "RUN_DUE" and "decision=RUN_DUE" in out.read_text()
    op, filters = db["ops"][-1]
    assert op == "update" and filters == {"account": "kr_main", "next_run_at": old_next}
    assert sch.parse_ts(db["row"]["next_run_at"]) == now + timedelta(minutes=55)
    assert db["row"]["last_gap_min"] == 52.0 and db["row"]["last_tick_at"] == now.isoformat()
    # 같은 상태로 다음 틱 → 선점된 next 때문에 SKIP_NOT_DUE
    later = gate.run_decide(now + timedelta(minutes=10))
    assert later.decision == "SKIP_NOT_DUE"


def test_gate_decide_missing_row_inserts(monkeypatch, tmp_path):
    gh_files(monkeypatch, tmp_path)
    db: dict = {}
    install_db(monkeypatch, db)
    assert gate.run_decide(kst(2026, 11, 6, 12, 3), rng=FixedRng(40.0)).decision == "RUN_DUE"
    assert db["ops"][-1][0] == "insert" and db["row"]["account"] == "kr_main"


@pytest.mark.parametrize("kw", [{"fail_write": True}, {"cas_miss": True}])
def test_gate_decide_claim_failure_is_gate_error(monkeypatch, tmp_path, kw):
    out, summ = gh_files(monkeypatch, tmp_path)
    now = kst(2026, 11, 6, 12, 3)
    db = {"row": state(now - timedelta(minutes=1))}
    _, alerts = install_db(monkeypatch, db, **kw)
    d = gate.run_decide(now)
    assert d.decision == "SKIP_GATE_ERROR" and alerts
    assert "decision=SKIP_GATE_ERROR" in out.read_text()
    assert "fail-closed" in summ.read_text()


def test_gate_decide_db_error_alerts_and_skips(monkeypatch, tmp_path):
    out, _ = gh_files(monkeypatch, tmp_path)
    _, alerts = install_db(monkeypatch, {}, fail_select=True)
    d = gate.run_decide(kst(2026, 11, 6, 12, 3))
    assert d.decision == "SKIP_GATE_ERROR" and alerts
    assert "decision=SKIP_GATE_ERROR" in out.read_text()


def test_gate_decide_night_skips_db(monkeypatch, tmp_path):
    gh_files(monkeypatch, tmp_path)
    tables, _ = install_db(monkeypatch, {}, fail_select=True)
    assert gate.run_decide(kst(2026, 11, 6, 3, 13)).decision == "SKIP_NIGHT"
    assert tables == []


def test_gate_dispatch_updates_next_even_when_not_due(monkeypatch, tmp_path):
    gh_files(monkeypatch, tmp_path, event="workflow_dispatch")
    now = kst(2026, 11, 6, 12, 3)
    db = {"row": state(now + timedelta(minutes=5), now - timedelta(minutes=35))}
    install_db(monkeypatch, db)
    d = gate.run_decide(now, rng=FixedRng(60.0))
    assert d.decision == "RUN_DUE" and db["ops"][-1][0] == "upsert"
    assert sch.parse_ts(db["row"]["next_run_at"]) == now + timedelta(minutes=60)


def test_gate_dispatch_runs_even_if_write_fails(monkeypatch, tmp_path, capsys):
    out, _ = gh_files(monkeypatch, tmp_path, event="workflow_dispatch")
    install_db(monkeypatch, {}, fail_select=True, fail_write=True)
    assert gate.run_decide(kst(2026, 11, 6, 3, 13)).decision == "RUN_DUE"
    assert "decision=RUN_DUE" in out.read_text()
    assert "::warning::" in capsys.readouterr().out


def test_gate_main_decide_never_raises(monkeypatch, tmp_path):
    out, _ = gh_files(monkeypatch, tmp_path)
    monkeypatch.setattr(gate, "run_decide", lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert gate.main(["decide"]) == 0
    assert "decision=SKIP_GATE_ERROR" in out.read_text()


def test_gate_report_is_read_only(monkeypatch, tmp_path, capsys):
    tick = kst(2026, 11, 6, 12, 3)
    db = {"row": state(tick + timedelta(minutes=50), tick)}
    install_db(monkeypatch, db)
    rep = gate.run_report(tick.isoformat(), now=tick + timedelta(minutes=12), logs_dir=tmp_path)
    assert [op for op, _ in db["ops"]] == ["select"]
    assert rep["run_duration_min"] == 12.0
    files = list(tmp_path.glob("reply_schedule_*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["state"]["next_run_at"] == db["row"]["next_run_at"]
    assert saved["dn"] == "DN2026_0099"
    assert "[ReplySchedule] v1.0.0 report | RUN_DUE |" in capsys.readouterr().out


def test_gate_report_failure_is_soft(monkeypatch, tmp_path):
    install_db(monkeypatch, {}, fail_select=True)
    rep = gate.run_report("not-a-time", now=kst(2026, 11, 6, 12, 3), logs_dir=tmp_path)
    assert len(rep["warnings"]) == 2
    monkeypatch.setattr(gate, "run_report", lambda *_a, **_k: (_ for _ in ()).throw(OSError("x")))
    assert gate.main(["report", "--tick-at", "x"]) == 0


# ---------------------------------------------------------------------------
# publish_summary gate skip (DN2026_0099 보완 B, urllib mock)
# ---------------------------------------------------------------------------


def _summary_env(monkeypatch, tmp_path):
    from scripts import publish_summary as ps
    monkeypatch.setenv("RUN_ID", "77")
    monkeypatch.setenv("GH_TOKEN", "TEST_ONLY")
    monkeypatch.setenv("RUN_CREATED", "2026-11-06T03:03:00Z")
    monkeypatch.setenv("RUN_CONCLUSION", "success")
    monkeypatch.setattr(ps, "OUT_DIR", tmp_path / "docs/reply_summary")
    git_calls: list = []
    monkeypatch.setattr(ps, "sh", lambda *c, check=True: git_calls.append(c) or "")
    monkeypatch.setattr(
        ps.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1)
    )
    return ps, git_calls


def _urlopen_factory(jobs_payload):
    def fake(req, timeout=30):
        import io
        url = req.full_url
        if url.endswith("/artifacts"):
            return io.BytesIO(json.dumps({"artifacts": []}).encode())
        if "/jobs" in url:
            if isinstance(jobs_payload, Exception):
                raise jobs_payload
            return io.BytesIO(json.dumps(jobs_payload).encode())
        raise AssertionError(url)
    return fake


@pytest.mark.parametrize("jobs", [
    {"jobs": [{"name": "Schedule Gate", "conclusion": "success"},
              {"name": "Run Tests", "conclusion": "skipped"},
              {"name": "Reply Pipeline", "conclusion": "skipped"}]},
    {"jobs": [{"name": "Schedule Gate", "conclusion": "success"}]},
])
def test_publish_summary_gate_skip_does_not_commit(monkeypatch, tmp_path, capsys, jobs):
    ps, git_calls = _summary_env(monkeypatch, tmp_path)
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_factory(jobs))
    ps.main()
    assert git_calls == []
    assert not (tmp_path / "docs/reply_summary").exists()
    assert "GATE_SKIP" in capsys.readouterr().out


@pytest.mark.parametrize("jobs", [
    {"jobs": [{"name": "Reply Pipeline", "conclusion": "failure"}]},
    RuntimeError("api down"),
])
def test_publish_summary_reply_ran_or_jobs_error_keeps_old_behaviour(monkeypatch, tmp_path, jobs):
    ps, git_calls = _summary_env(monkeypatch, tmp_path)
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_factory(jobs))
    ps.main()
    md = list((tmp_path / "docs/reply_summary").rglob("run_77.md"))
    assert len(md) == 1 and "요약 생성 부분 실패" in md[0].read_text()
    assert any(c[:2] == ("git", "commit") for c in git_calls)
    assert any(c[:2] == ("git", "push") for c in git_calls)

# ---------------------------------------------------------------------------
# ④ yml 정합
# ---------------------------------------------------------------------------


def _load_yaml():
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return None
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_cron_and_comments():
    text = WORKFLOW.read_text()
    assert '- cron: "3-59/10 * * * *"' in text
    assert "17 1,4,9,13" not in text
    assert "v2.1.0 (2026-11-06, DN2026_0099)" in text
    backdata = (ROOT / ".github/workflows/backdata_card.yml").read_text().splitlines()[5]
    assert "3-59/10" in backdata and "1,4,9,13" not in backdata


def test_workflow_jobs_wiring():
    text = WORKFLOW.read_text()
    cfg = _load_yaml()
    if cfg is not None:
        jobs = cfg["jobs"]
        assert "concurrency" not in cfg
        assert jobs["gate"]["timeout-minutes"] == 5 and "needs" not in jobs["gate"]
        assert "concurrency" not in jobs["gate"]
        assert jobs["test"]["needs"] == "gate"
        assert jobs["reply"]["needs"] == ["gate", "test"]
        for j in ("test", "reply"):
            assert jobs[j]["if"] == "needs.gate.outputs.decision == 'RUN_DUE'"
        expected = {"group": "reply-engine", "cancel-in-progress": False}
        assert jobs["reply"]["concurrency"] == expected
        return
    # PyYAML 미설치 — 문자열 검사
    head = text.split("\njobs:", 1)[0]
    assert "\nconcurrency:" not in head
    gate_block = text.split("\n  gate:", 1)[1].split("\n  test:", 1)[0]
    assert "timeout-minutes: 5" in gate_block and "needs:" not in gate_block
    assert "concurrency:" not in gate_block
    assert "python -m scripts.reply_schedule_gate decide" in gate_block
    assert "decision: ${{ steps.decide.outputs.decision ||" in gate_block
    assert "tick_at: ${{ steps.decide.outputs.tick_at ||" in gate_block
    test_block = text.split("\n  test:", 1)[1].split("\n  reply:", 1)[0]
    assert "needs: gate" in test_block
    assert "if: needs.gate.outputs.decision == 'RUN_DUE'" in test_block
    reply_block = text.split("\n  reply:", 1)[1]
    assert "needs: [gate, test]" in reply_block
    assert "if: needs.gate.outputs.decision == 'RUN_DUE'" in reply_block
    pattern = r"\n    concurrency:\n      group: reply-engine\n      cancel-in-progress: false"
    assert re.search(pattern, reply_block)


def test_workflow_telemetry_step_only():
    text = WORKFLOW.read_text()
    assert "Commit schedule" not in text and "reply_schedule_gate commit" not in text
    step = text.split("- name: Record schedule telemetry", 1)[1].split(
        "- name: Upload run report", 1)[0]
    assert "if: always() && needs.gate.outputs.decision == 'RUN_DUE'" in step
    assert "TICK_AT:" in step and "${{ needs.gate.outputs.tick_at }}" in step
    assert 'report --tick-at "$TICK_AT"' in step  # STD-SCHED-08: run에 expression 직접 삽입 금지
    assert "continue-on-error: true" in step
    upload = text.split("- name: Upload run report", 1)[1]
    assert "logs/reply_schedule_*.json" in upload


def test_publish_summary_failed_run_with_reply_skipped_still_records(monkeypatch, tmp_path):
    """test·gate 실패 런(conclusion != success)은 reply가 skipped여도 기존대로 md 기록."""
    ps, git_calls = _summary_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUN_CONCLUSION", "failure")
    jobs = {"jobs": [{"name": "Run Tests", "conclusion": "failure"},
                     {"name": "Reply Pipeline", "conclusion": "skipped"}]}
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_factory(jobs))
    ps.main()
    md = list((tmp_path / "docs/reply_summary").rglob("run_77.md"))
    assert len(md) == 1 and "결론: failure" in md[0].read_text()
    assert any(c[:2] == ("git", "commit") for c in git_calls)


# ---------------------------------------------------------------------------
# G4 반영: 알림 억제 · 예외 문자열 비노출 · 스크립트 부재 preflight · DN2026_0100 env
# ---------------------------------------------------------------------------

SECRET_TEXT = "https://leak.example/rest/v1?apikey=TEST_ONLY"


@pytest.mark.parametrize("minute,alerted", [(3, True), (9, True), (13, False), (53, False)])
def test_gate_error_alert_throttled_to_first_tick_of_hour(monkeypatch, tmp_path, minute, alerted):
    out, summ = gh_files(monkeypatch, tmp_path)
    _, alerts = install_db(monkeypatch, {}, fail_select=True)
    now = datetime(2026, 11, 6, 5, minute, tzinfo=UTC)
    assert gate.run_decide(now).decision == "SKIP_GATE_ERROR"
    assert bool(alerts) is alerted
    assert "decision=SKIP_GATE_ERROR" in out.read_text()  # 로그·요약은 매번
    assert "SKIP_GATE_ERROR" in summ.read_text()


def test_claim_failure_alert_also_throttled(monkeypatch, tmp_path):
    gh_files(monkeypatch, tmp_path)
    now = datetime(2026, 11, 6, 5, 23, tzinfo=UTC)
    _, alerts = install_db(monkeypatch, {"row": state(now - timedelta(minutes=1))}, cas_miss=True)
    assert gate.run_decide(now).decision == "SKIP_GATE_ERROR" and alerts == []


class LeakyTable(FakeTable):
    def execute(self):
        raise RuntimeError(SECRET_TEXT)


def test_gate_logs_exception_class_only(monkeypatch, tmp_path, capsys, caplog):
    out, summ = gh_files(monkeypatch, tmp_path)
    monkeypatch.setattr(gate.store, "get_client",
                        lambda: SimpleNamespace(table=lambda _n: LeakyTable({})))
    alerts: list[str] = []
    monkeypatch.setattr(gate, "send_admin_alert", lambda t: alerts.append(t) or False)
    gate.run_decide(datetime(2026, 11, 6, 5, 3, tzinfo=UTC))
    gate.run_report("2026-11-06T05:03:00+00:00", logs_dir=tmp_path)
    monkeypatch.setattr(gate, "run_decide", lambda: (_ for _ in ()).throw(ValueError(SECRET_TEXT)))
    gate.main(["decide"])
    dumped = (capsys.readouterr().out + caplog.text + out.read_text() + summ.read_text()
              + "".join(alerts) + "".join(p.read_text() for p in tmp_path.glob("*.json")))
    assert "leak.example" not in dumped and "TEST_ONLY" not in dumped
    assert "RuntimeError" in dumped and "ValueError" in dumped


def test_workflow_preflight_handles_missing_gate_script():
    text = WORKFLOW.read_text()
    gate_block = text.split("\n  gate:", 1)[1].split("\n  test:", 1)[0]
    assert "decision: ${{ steps.decide.outputs.decision || steps.preflight.outputs.decision }}" \
        in gate_block
    assert "tick_at: ${{ steps.decide.outputs.tick_at || steps.preflight.outputs.tick_at }}" \
        in gate_block
    pre = gate_block.split("- name: Check gate script", 1)[1].split("- name: Setup Python", 1)[0]
    assert "id: preflight" in pre and "[[ -f scripts/reply_schedule_gate.py ]]" in pre
    assert 'echo "decision=SKIP_GATE_ERROR"' in pre and 'echo "tick_at=$tick_at"' in pre
    assert "::error::REPLY_RELEASE_SHA가 DN2026_0099 이전 커밋 — 변수 갱신 필요" in pre
    assert "exit 1" not in pre
    decide = gate_block.split("- name: Decide", 1)[1]
    assert "id: decide" in decide
    assert "if: steps.preflight.outputs.gate_script == 'PRESENT'" in decide
    assert gate_block.index("- name: Check gate script") < gate_block.index("- name: Decide")


def test_workflow_dn2026_0100_env_vars():
    text = WORKFLOW.read_text()
    run = text.split("- name: Run Reply Engine", 1)[1].split("run: python run_reply.py", 1)[0]
    for name in ("X_READ_RESOURCE_COST_KRW", "REPLY_FOREIGN_THREAD_DAILY_CAP"):
        assert f"{name}: ${{{{ vars.{name} }}}}" in run
    assert "DN2026_0100" in run
