"""DN2026_0100 — 읽기 리소스 단위 계상 · 타인 스레드 일일 상한.

X API는 반환 리소스 단위로 과금한다(docs.x.com pricing). 리소스 단가가 설정되고
호출부가 resources를 넘길 때만 리소스 단위로 계상하며, 넘기지 않는 호출부(following·shock)는
기존 호출 단가 계상이 그대로 유지되어야 한다.
"""

from __future__ import annotations

import run_reply
from reply_engine import config
from reply_engine.budget import BudgetGuard
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet
from tests.test_reply_r10_foreign import _install_roots
from tests.test_reply_r_patch import _override_mentions, _tweet


def _row(est: float = 0.0) -> dict:
    return {"read_calls": 0, "write_calls": 0, "gemini_calls": 0, "est_cost_krw": est}


def _cost_env(monkeypatch, resource: str | None = "7") -> None:
    monkeypatch.setenv("X_READ_COST_KRW", "50")
    monkeypatch.setenv("X_WRITE_COST_KRW", "21")
    monkeypatch.setenv("DAILY_BUDGET_KRW", "3000")
    if resource is None:
        monkeypatch.delenv("X_READ_RESOURCE_COST_KRW", raising=False)
    else:
        monkeypatch.setenv("X_READ_RESOURCE_COST_KRW", resource)


def test_resource_mode_charges_per_resource(monkeypatch):
    _cost_env(monkeypatch)
    guard = BudgetGuard(_row())
    assert guard.resource_mode is True
    guard.record_read(resources=6)
    guard.record_read(resources=0)  # 빈 폴링 — 콜 수만 증가
    assert guard.row["read_calls"] == 2
    assert guard.row["est_cost_krw"] == 42.0
    assert guard.snapshot()["read_resources_run"] == 6


def test_legacy_call_sites_keep_per_call_cost(monkeypatch):
    """resources를 넘기지 않는 호출부(following·shock)는 기존 50원/콜."""
    _cost_env(monkeypatch)
    guard = BudgetGuard(_row())
    guard.record_read()
    assert guard.row["est_cost_krw"] == 50.0


def test_no_resource_price_falls_back_to_per_call(monkeypatch):
    _cost_env(monkeypatch, resource=None)
    guard = BudgetGuard(_row())
    assert guard.resource_mode is False
    guard.record_read(resources=10)
    assert guard.row["est_cost_krw"] == 50.0


def test_invalid_resource_price_is_ignored(monkeypatch):
    _cost_env(monkeypatch, resource="-3")
    assert config.get_read_resource_cost() is None
    _cost_env(monkeypatch, resource="abc")
    assert config.get_read_resource_cost() is None


def test_explicit_costs_never_use_resource_price(monkeypatch):
    """FB 등 costs=를 넘기는 호출부는 리소스 단가를 쓰지 않는다."""
    _cost_env(monkeypatch)
    guard = BudgetGuard(_row(), costs=(None, None))
    assert guard.resource_mode is False


def test_settle_read_replaces_reservation(monkeypatch):
    _cost_env(monkeypatch)
    guard = BudgetGuard(_row(100.0))
    guard.record_read()  # 호출 전 예약 50원
    assert guard.row["est_cost_krw"] == 150.0
    guard.settle_read(3)  # 실제 3리소스 × 7원
    assert guard.row["est_cost_krw"] == 121.0
    guard.record_read()
    guard.settle_read(0)  # 빈 응답 — 예약분 환원, 시작값 아래로는 내려가지 않음
    assert guard.row["est_cost_krw"] == 121.0


def test_settle_read_noop_without_resource_mode(monkeypatch):
    _cost_env(monkeypatch, resource=None)
    guard = BudgetGuard(_row())
    guard.record_read()
    guard.settle_read(0)
    assert guard.row["est_cost_krw"] == 50.0


def test_foreign_thread_daily_cap_blocks_when_reached(monkeypatch):
    """회당 상한이 남아도 당일 타인 스레드 누계가 일일 상한에 닿으면 FOREIGN_THREAD_CAP."""
    _base_env(monkeypatch, "live")
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_ENABLED", True)
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_DAILY_CAP", 4)
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.foreign_responded_count = 4
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [_tweet("f1", "A1", "cf1", "감사합니다")])
    _install_roots(monkeypatch, {"cf1": "OTHER"})

    result = run_reply.main()

    assert result["foreign_thread_replies"] == 0
    assert result["skip_reasons"].get("FOREIGN_THREAD_CAP") == 1


def test_foreign_thread_daily_cap_allows_below_limit(monkeypatch):
    _base_env(monkeypatch, "live")
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_ENABLED", True)
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_DAILY_CAP", 4)
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.foreign_responded_count = 3
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [_tweet("f1", "A1", "cf1", "감사합니다")])
    _install_roots(monkeypatch, {"cf1": "OTHER"})

    result = run_reply.main()

    assert result["foreign_thread_replies"] == 1


def test_foreign_daily_cap_config_bounds(monkeypatch):
    monkeypatch.setenv("REPLY_FOREIGN_THREAD_DAILY_CAP", "999")
    assert config.env_int_clamped("REPLY_FOREIGN_THREAD_DAILY_CAP", 4, 1, 20) == 4
    assert config.REPLY_FOREIGN_THREAD_DAILY_CAP >= 1


def test_available_reads_reserve_worst_case_resources(monkeypatch):
    """리소스 모드: 페이지당 최악 비용으로 예약해 사후 계상이 예산을 넘지 않는다 (G4 R6 경고)."""
    _cost_env(monkeypatch)
    guard = BudgetGuard(_row(500.0))
    worst_page = 100 * 3 * 7.0  # 2,100원
    assert guard.available_read_calls(4, reserve_krw=worst_page) == 1  # 잔여 2,500원
    assert guard.available_read_calls(4) == 4  # 예약값 미지정 시 기존 호출 단가(50원)


def test_reserve_ignored_without_resource_mode(monkeypatch):
    _cost_env(monkeypatch, resource=None)
    guard = BudgetGuard(_row(500.0))
    assert guard.available_read_calls(4, reserve_krw=2100.0) == 4


def test_collector_fails_closed_when_settled_budget_not_saved(monkeypatch):
    """정산값 저장 실패 시 수집 결과를 반영하지 않고 중단한다 (커서 미전진)."""
    from reply_engine import store as _store
    from reply_engine import x_client as _x
    from scripts import collect_reply

    _cost_env(monkeypatch)
    monkeypatch.setenv("REPLY_ENABLED", "true")
    monkeypatch.setenv("REPLY_COLLECTOR_ENABLED", "true")
    monkeypatch.setenv("X_MY_USER_ID", "111")
    saves = []

    def upsert(row):
        saves.append(dict(row))
        return len(saves) < 2  # 예약 저장은 성공, 정산 저장은 실패

    monkeypatch.setattr(_store, "get_budget", lambda _d: _row())
    monkeypatch.setattr(_store, "upsert_budget", upsert)
    monkeypatch.setattr(_store, "get_cursor", lambda _a: {"since_id": "1"})
    persisted, cursors = [], []
    monkeypatch.setattr(_store, "persist_collected", lambda *a, **k: persisted.append(a) or True)
    monkeypatch.setattr(_store, "upsert_cursor", lambda *a, **k: cursors.append(a) or True)
    monkeypatch.setattr(_x, "get_x_client", lambda: object())
    monkeypatch.setattr(
        _x, "fetch_mentions",
        lambda *a, **k: {"success": True, "tweets": [{"id": "9"}], "users": {}, "newest_id": "9"},
    )

    report = collect_reply.main()

    assert report["exit_reason"] == "BUDGET_SAVE_FAILED"
    assert persisted == [] and cursors == []


def test_mention_pages_not_limited_by_resource_reservation(monkeypatch):
    """회귀(2026-11-06 dry_run 실측): 일 예산 1,000원·리소스 모드에서도 멘션 페이지 상한 3 유지."""
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    _cost_env(monkeypatch)
    monkeypatch.setenv("DAILY_BUDGET_KRW", "1000")
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    seen = {}

    def fake_fetch(_client, _uid, _since, max_pages=None):
        seen["max_pages"] = max_pages
        return {"success": True, "tweets": [], "users": {}, "newest_id": None,
                "pages_fetched": 1, "saturated": False, "collection_complete": True}

    monkeypatch.setattr(run_reply.x_client, "fetch_mentions", fake_fetch)
    monkeypatch.setattr(run_reply, "_DEFAULT_FETCH_MENTIONS", fake_fetch)

    run_reply.main()

    assert seen["max_pages"] == run_reply.MENTIONS_MAX_PAGES
