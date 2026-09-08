"""Reply Engine LIKE 기능 검증 (2026-08-26 승인 — 조회 전건 좋아요, 무필터).

정책: 제외는 SELF/블랙리스트/기좋아요(L1) 3종뿐. dry_run/shadow에서 X like 호출 0.
답글 파이프라인과 독립 — LIKE가 실패·상한 도달해도 답글은 정상 진행.
"""

from __future__ import annotations

from datetime import UTC, datetime

import run_reply
from reply_engine import config, store, x_client
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet


def _install_like_store(monkeypatch, existing=None, today=0):
    import os
    if "REPLY_LIKE_ENABLED" not in os.environ:          # opt-in 전환 후 테스트는 명시 활성
        monkeypatch.setenv("REPLY_LIKE_ENABLED", "true")
    calls = {"inserted": [], "existing_queries": []}

    def _existing(ids):
        calls["existing_queries"].append(list(ids))
        return set(existing or [])

    monkeypatch.setattr(store, "get_existing_like_ids", _existing)
    monkeypatch.setattr(store, "count_likes_today", lambda: today)
    monkeypatch.setattr(
        store, "insert_like", lambda rec: calls["inserted"].append(dict(rec)) or True
    )
    return calls


def _like_spy(monkeypatch, fail_error=None):
    liked: list[str] = []

    def _like(_c, tid):
        if fail_error is not None:
            return False, fail_error
        liked.append(tid)
        return True, None

    monkeypatch.setattr(x_client, "post_like", _like)
    return liked


def test_like_config_defaults(monkeypatch):
    assert config.REPLY_LIKE_PER_RUN == 20
    assert config.REPLY_LIKE_PER_DAY == 50
    monkeypatch.delenv("REPLY_LIKE_ENABLED", raising=False)
    assert config.is_like_enabled() is False             # 2026-09-08: 기본 비활성(opt-in)
    monkeypatch.setenv("REPLY_LIKE_ENABLED", "false")
    assert config.is_like_enabled() is False
    monkeypatch.setenv("REPLY_LIKE_ENABLED", "true")
    assert config.is_like_enabled() is True


def test_like_dry_run_no_calls_no_db(monkeypatch):
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    lcalls = _install_like_store(monkeypatch)
    liked = _like_spy(monkeypatch)

    result = run_reply.main()
    assert result["likes"]["targets"] >= 1
    assert liked == [] and lcalls["inserted"] == []       # X·DB 무접촉
    assert result["likes"]["liked"] >= 1                  # would 카운트만


def test_like_shadow_records_only(monkeypatch):
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    lcalls = _install_like_store(monkeypatch)
    liked = _like_spy(monkeypatch)

    result = run_reply.main()
    assert liked == []                                    # X like 호출 0 (spy)
    assert len(lcalls["inserted"]) == result["likes"]["liked"] >= 1
    rec = lcalls["inserted"][0]
    assert rec["mode"] == "shadow" and rec["would_like"] is True
    assert "liked_at" not in rec


def test_like_live_executes_and_records(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)
    lcalls = _install_like_store(monkeypatch)
    liked = _like_spy(monkeypatch)

    result = run_reply.main()
    assert len(liked) == result["likes"]["liked"] >= 1
    assert lcalls["inserted"][0]["liked_at"]              # 실행-기록 짝
    assert result["published"] == 1                        # 답글 파이프라인 무영향
    assert mem.budget_saved[-1]["write_calls"] == len(liked) + 1


def test_like_no_filter_expired_and_offscope_also_liked(monkeypatch):
    """마스터 정책: 필터 없음 — EXPIRED·스코프 밖 댓글도 좋아요 대상이어야 한다."""
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    from datetime import timedelta

    old = datetime.now(UTC) - timedelta(hours=30)
    extra = [
        {"id": "900", "text": "@edt 옛날 댓글", "author_id": "555",
         "conversation_id": "c9", "in_reply_to_user_id": "111", "created_at": old},
        {"id": "901", "text": "@edt 타인 글 대댓글", "author_id": "556",
         "conversation_id": "other_conv", "in_reply_to_user_id": "111",
         "created_at": datetime.now(UTC)},
    ]
    base = list(run_reply.x_client.fetch_mentions(object(), "111", None)["tweets"])
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": base + extra, "users": {},
                            "newest_id": "901", "error": None},
    )
    lcalls = _install_like_store(monkeypatch)
    _like_spy(monkeypatch)

    run_reply.main()
    liked_ids = {r["reply_tweet_id"] for r in lcalls["inserted"]}
    assert {"900", "901"} <= liked_ids                    # 답글은 안 나가도 하트는 감


def test_like_exclusions_self_blacklist_already(monkeypatch):
    """픽스처 3건(작성자 222/333/444) — 블랙리스트 2 + 기좋아요 1로 전건 제외."""
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.blacklist = {"222", "333"}
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    lcalls = _install_like_store(monkeypatch, existing=["102"])   # 444 작성 건 기좋아요

    _like_spy(monkeypatch)

    result = run_reply.main()
    assert result["likes"]["targets"] == 0                # 전건 제외
    assert result["likes"]["skipped"]["ALREADY"] == 1
    assert lcalls["inserted"] == []


def test_like_run_cap(monkeypatch):
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    monkeypatch.setattr(run_reply, "REPLY_LIKE_PER_RUN", 1)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    now = datetime.now(UTC)
    many = [
        {"id": str(950 + i), "text": f"@edt 댓글{i}", "author_id": f"7{i}",
         "conversation_id": "c1", "in_reply_to_user_id": "111", "created_at": now}
        for i in range(3)
    ]
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": many, "users": {},
                            "newest_id": "952", "error": None},
    )
    _install_like_store(monkeypatch)
    _like_spy(monkeypatch)

    result = run_reply.main()
    assert result["likes"]["liked"] == 1
    assert result["likes"]["skipped"]["RUN_CAP"] == 2


def test_like_day_cap(monkeypatch):
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _install_like_store(monkeypatch, today=50)            # 일일 상한 도달 상태
    _like_spy(monkeypatch)

    result = run_reply.main()
    assert result["likes"]["liked"] == 0
    assert result["likes"]["skipped"]["DAY_CAP"] >= 1
    assert result["published"] == 1                       # 답글은 무영향


def test_like_spend_cap_stops_likes_not_replies(monkeypatch):
    """N-1: like가 spend cap을 맞아도 좋아요만 중단, 파이프라인은 계속."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)
    _install_like_store(monkeypatch)
    _like_spy(monkeypatch, fail_error="403 Your monthly spend cap has been reached.")

    result = run_reply.main()
    assert result["likes"]["liked"] == 0
    assert result["likes"].get("spend_cap") is True
    assert result["success"] is True                      # 실행 자체는 정상 종료


def test_like_disabled_switch(monkeypatch):
    _base_env(monkeypatch, "shadow")
    monkeypatch.setenv("REPLY_LIKE_ENABLED", "false")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    lcalls = _install_like_store(monkeypatch)
    liked = _like_spy(monkeypatch)

    result = run_reply.main()
    assert result["likes"] == {"targets": 0, "liked": 0, "skipped": {}}
    assert liked == [] and lcalls["inserted"] == []
    assert result["published"] == 1                       # 답글 정상


def test_like_insert_fail_not_counted(monkeypatch):
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    monkeypatch.setattr(store, "get_existing_like_ids", lambda ids: set())
    monkeypatch.setattr(store, "count_likes_today", lambda: 0)
    monkeypatch.setattr(store, "insert_like", lambda rec: False)   # PK 충돌
    _like_spy(monkeypatch)

    result = run_reply.main()
    assert result["likes"]["liked"] == 0


def test_like_versions():
    assert run_reply.VERSION == "1.5.0"
    assert config.VERSION == "1.4.0"
    assert store.VERSION == "1.2.0"
    assert x_client.VERSION == "1.4.0"
