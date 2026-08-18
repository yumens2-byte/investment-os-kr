"""reply_engine — 보완 B-1/B-2/B-3 검증 (2026-08-17 dry_run 1차 분석 후속).

  B-1: X_MY_USER_ID 변수 → get_me 생략 (우선순위: 변수 > 커서 > get_me)
  B-2: 단가 오설정(단가 ≥ 일일예산) CONFIG WARNING
  B-3: 리포트 summary에 예산 스냅샷 포함
"""

from __future__ import annotations

import run_reply
from reply_engine import config, x_client
from reply_engine.budget import BudgetGuard
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet


def _row(read=0, write=0, cost=0.0):
    return {"budget_date": "2026-08-17", "read_calls": read, "write_calls": write,
            "gemini_calls": 0, "est_cost_krw": cost}


# ---------------------------------------------------------------------------
# B-1
# ---------------------------------------------------------------------------

def test_b1_get_my_user_id_validation(monkeypatch):
    monkeypatch.setenv("X_MY_USER_ID", "1914285902266007552")
    assert config.get_my_user_id() == "1914285902266007552"
    monkeypatch.setenv("X_MY_USER_ID", "abc123")   # 숫자 아님 → 무시
    assert config.get_my_user_id() == ""
    monkeypatch.delenv("X_MY_USER_ID", raising=False)
    assert config.get_my_user_id() == ""


def test_b1_env_user_id_skips_get_me(monkeypatch):
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "1914285902266007552")
    mem = _MemStore()
    mem.cursor = None  # 커서 없어도 get_me 불필요해야 함
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])

    def _must_not_call(_c):
        raise AssertionError("X_MY_USER_ID 설정 시 get_me 호출되면 안 됨 (B-1)")

    monkeypatch.setattr(x_client, "fetch_my_user_id", _must_not_call)

    result = run_reply.main()
    assert result["success"] is True
    # get_me 생략 → mentions 1콜만
    # (픽스처 in_reply_to=111 ≠ env user_id → 후보 0건 → 루트검증 콜 없음)
    assert result["budget"]["read_calls"] == 1


def test_b1_env_overrides_cursor(monkeypatch):
    """변수 > 커서 우선순위 확인."""
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")  # 픽스처 my_user_id와 일치
    mem = _MemStore()
    mem.cursor = {"account": "kr_main", "since_id": "1", "my_user_id": "999999"}  # 다른 값
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()
    # env의 111이 사용되어 in_reply_to_user_id=111 건이 범위 내로 판정됨
    assert result["candidates"] == 1


# ---------------------------------------------------------------------------
# B-2
# ---------------------------------------------------------------------------

def test_b2_misconfig_warning_read_cost(monkeypatch):
    monkeypatch.setenv("DAILY_BUDGET_KRW", "1000")
    monkeypatch.setenv("X_READ_COST_KRW", "1000")   # 2026-08-17 실사고 재현
    monkeypatch.setenv("X_WRITE_COST_KRW", "5")
    guard = BudgetGuard(_row())
    assert len(guard.config_warnings) == 1
    assert "X_READ_COST_KRW" in guard.config_warnings[0]


def test_b2_no_warning_on_sane_costs(monkeypatch):
    monkeypatch.setenv("DAILY_BUDGET_KRW", "1000")
    monkeypatch.setenv("X_READ_COST_KRW", "7")
    monkeypatch.setenv("X_WRITE_COST_KRW", "7")
    guard = BudgetGuard(_row())
    assert guard.config_warnings == []


def test_b2_count_mode_no_warning(monkeypatch):
    monkeypatch.delenv("X_READ_COST_KRW", raising=False)
    monkeypatch.delenv("X_WRITE_COST_KRW", raising=False)
    guard = BudgetGuard(_row())
    assert guard.cost_mode is False
    assert guard.config_warnings == []


# ---------------------------------------------------------------------------
# B-3
# ---------------------------------------------------------------------------

def test_b3_snapshot_fields(monkeypatch):
    monkeypatch.delenv("X_READ_COST_KRW", raising=False)
    monkeypatch.delenv("X_WRITE_COST_KRW", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_KRW", "1000")
    guard = BudgetGuard(_row(read=2, write=1))
    snap = guard.snapshot()
    assert snap["mode"] == "count"
    assert snap["read_calls"] == 2
    assert snap["write_calls"] == 1
    assert snap["limit_krw"] == 1000.0
    assert snap["config_warnings"] == []


def test_b3_summary_contains_budget(monkeypatch):
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])

    result = run_reply.main()
    assert "budget" in result
    assert result["budget"]["mode"] == "count"
    assert result["budget"]["read_calls"] >= 1


def test_b3_exit_budget_includes_snapshot(monkeypatch):
    """EXIT_BUDGET 시에도 artifact만으로 원인 판독 가능해야 함."""
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_READ_COST_KRW", "1000")   # 실사고 재현
    monkeypatch.setenv("X_WRITE_COST_KRW", "1000")
    monkeypatch.setenv("DAILY_BUDGET_KRW", "1000")
    monkeypatch.delenv("X_MY_USER_ID", raising=False)
    mem = _MemStore()
    mem.cursor = None
    mem.install(monkeypatch)
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(x_client, "fetch_my_user_id", lambda _c: "111")

    result = run_reply.main()
    assert result["exit_reason"] == "EXIT_BUDGET"
    assert result["budget"]["est_cost_krw"] == 1000.0
    assert result["budget"]["config_warnings"]  # 오설정 경고 포함


def test_versions_bumped_b_series():
    """보완 반영 버전 확인 (지침 5)."""
    from reply_engine import budget as budget_mod

    assert run_reply.VERSION == "1.1.0"
    assert config.VERSION == "1.0.3"
    assert budget_mod.VERSION == "1.0.1"
