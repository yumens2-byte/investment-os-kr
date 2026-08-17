"""reply_engine — 배포 전 시나리오 매트릭스 보강 테스트 (2026-08-17 전수 검토).

커버 시나리오:
  S-01 REPLY_MODE 빈 문자열(vars 미설정 yml 유입) → dry_run 강등
  S-02 답글 40자 경계 (40 통과 / 41 탈락)
  S-03 EXPIRED 경계 (24h 직전 통과)
  S-04 수집 0건(resp.data None) → 정상 종료
  S-05 일일 상한 부분 도달 (7 기존 + 2 후보 → 1건만 발행)
  S-06 배치 내 동일 생성문 → 두 번째 GATE_SIMILARITY
  S-07 [R-1] 최초 실행 get_me 소모 후 읽기 예산 재확인 차단
  S-08 [R-2] PUBLISH_FAIL 시 DB skip_reason 사후 기록
  S-09 [R-2] BUDGET_WRITE 시 insert 시점 skip_reason 기록 + 미발행
  S-10 [R-2] DAILY_CAP 건도 이력 기록 (감사추적)
  S-11 store.update_skip_reason CRUD
  S-12 GET_ME_FAIL 경로에서도 소모 예산 저장
  S-13 shadow에서 게이트 탈락 건 skip_reason 포함 기록
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import run_reply
from reply_engine import config, gate, store, x_client
from reply_engine import filter as filter_mod
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet

# ---------------------------------------------------------------------------
# S-01 ~ S-03: 경계값
# ---------------------------------------------------------------------------

def test_s01_empty_mode_string_degrades(monkeypatch):
    monkeypatch.setenv("REPLY_MODE", "")
    assert config.get_mode() == "dry_run"
    monkeypatch.setenv("REPLY_MODE", "  ")
    assert config.get_mode() == "dry_run"


def test_s02_length_boundary():
    exactly_40 = "가" * 40
    assert gate.check_reply(exactly_40, []) == (True, None)
    assert gate.check_reply("가" * 41, [])[1] == "GATE_LENGTH"


def test_s03_expired_boundary_just_inside():
    almost_24h = datetime.now(UTC) - timedelta(hours=23, minutes=50)
    tweet = {
        "id": "t", "text": "@edt 감사합니다 잘 봤어요", "author_id": "222",
        "conversation_id": "c", "in_reply_to_user_id": "111", "created_at": almost_24h,
    }
    ok, _ = filter_mod.check_tweet(tweet, None, "111", set())
    assert ok


# ---------------------------------------------------------------------------
# S-04: 수집 0건
# ---------------------------------------------------------------------------

def test_s04_no_data_response_is_success():
    resp = SimpleNamespace(data=None, includes=None, meta={})

    class _Client:
        def get_users_mentions(self, _id, **_kwargs):
            return resp

    result = x_client.fetch_mentions(_Client(), "111", since_id="9")
    assert result["success"] is True
    assert result["tweets"] == []
    assert result["newest_id"] is None


def test_s04_pipeline_no_mentions_exit_ok(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": [], "users": {},
                            "newest_id": None, "error": None},
    )
    result = run_reply.main()
    assert result["success"] is True
    assert result["exit_reason"] == "EXIT_NO_MENTIONS"
    assert len(mem.budget_saved) == 1  # 읽기 소모분 저장


# ---------------------------------------------------------------------------
# S-05: 일일 상한 부분 도달
# ---------------------------------------------------------------------------

def test_s05_partial_daily_cap(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.responded_count = 7  # 상한 8 중 7 소진
    mem.install(monkeypatch)
    published: list = []
    now = datetime.now(UTC)
    two_pass = [
        {"id": "200", "text": "@edt 감사합니다!", "author_id": "222",
         "conversation_id": "c1", "in_reply_to_user_id": "111", "created_at": now},
        {"id": "201", "text": "@edt 오늘도 감사해요", "author_id": "333",
         "conversation_id": "c2", "in_reply_to_user_id": "111", "created_at": now},
    ]
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": two_pass, "users": {},
                            "newest_id": "201", "error": None},
    )
    monkeypatch.setattr(
        x_client, "post_reply",
        lambda _c, text, tid: published.append((tid, text)) or f"resp-{tid}",
    )
    from reply_engine import generator

    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True, "data": [
            {"id": "200", "reply": "감사합니다, 큰 힘이 돼요"},
            {"id": "201", "reply": "따뜻한 말씀 감사드립니다"},
        ]},
    )

    result = run_reply.main()
    assert result["published"] == 1                 # 1건만 발행 후 상한
    assert result["skip_reasons"]["DAILY_CAP"] == 1
    assert mem.history["201"]["skip_reason"] == "DAILY_CAP"   # S-10 감사추적


# ---------------------------------------------------------------------------
# S-06: 배치 내 동일 생성문
# ---------------------------------------------------------------------------

def test_s06_within_batch_similarity(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    now = datetime.now(UTC)
    two_pass = [
        {"id": "300", "text": "@edt 감사합니다", "author_id": "222",
         "conversation_id": "c1", "in_reply_to_user_id": "111", "created_at": now},
        {"id": "301", "text": "@edt 정말 감사합니다", "author_id": "333",
         "conversation_id": "c2", "in_reply_to_user_id": "111", "created_at": now},
    ]
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": two_pass, "users": {},
                            "newest_id": "301", "error": None},
    )
    monkeypatch.setattr(
        x_client, "post_reply",
        lambda _c, text, tid: published.append((tid, text)) or f"resp-{tid}",
    )
    from reply_engine import generator

    same_reply = "좋게 봐주셔서 감사합니다"
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True, "data": [
            {"id": "300", "reply": same_reply},
            {"id": "301", "reply": same_reply},
        ]},
    )

    result = run_reply.main()
    assert result["published"] == 1
    assert result["skip_reasons"]["GATE_SIMILARITY"] == 1
    assert mem.history["301"]["skip_reason"] == "GATE_SIMILARITY"  # S-13 계열


# ---------------------------------------------------------------------------
# S-07 / S-12: [R-1] 예산 재확인 + GET_ME_FAIL 예산 저장
# ---------------------------------------------------------------------------

def test_s07_budget_recheck_after_get_me(monkeypatch):
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.cursor = None  # 최초 실행 → get_me 필요
    mem.install(monkeypatch)
    monkeypatch.setattr(
        store, "get_budget",
        lambda d: {"budget_date": d, "read_calls": 7, "write_calls": 0,
                   "gemini_calls": 0, "est_cost_krw": 0.0},  # count 모드 8 중 7 소진
    )
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(x_client, "fetch_my_user_id", lambda _c: "111")

    def _must_not_fetch(*_a, **_k):
        raise AssertionError("get_me로 read 예산 소진 → fetch 호출되면 안 됨 (R-1)")

    monkeypatch.setattr(x_client, "fetch_mentions", _must_not_fetch)

    result = run_reply.main()
    assert result["exit_reason"] == "EXIT_BUDGET"
    assert len(mem.budget_saved) == 1
    assert mem.budget_saved[0]["read_calls"] == 8  # get_me 소모분 기록


def test_s12_get_me_fail_saves_budget(monkeypatch):
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.cursor = None
    mem.install(monkeypatch)
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(x_client, "fetch_my_user_id", lambda _c: None)

    result = run_reply.main()
    assert result["exit_reason"] == "EXIT_GET_ME_FAIL"
    assert len(mem.budget_saved) == 1
    assert mem.budget_saved[0]["read_calls"] == 1


# ---------------------------------------------------------------------------
# S-08 / S-09: [R-2] 발행 실패/예산 차단 시 skip_reason DB 기록
# ---------------------------------------------------------------------------

def test_s08_publish_fail_records_reason(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)

    updated: list = []
    monkeypatch.setattr(
        store, "update_skip_reason",
        lambda tid, reason: updated.append((tid, reason)) or True,
    )
    _install_x(monkeypatch, [])
    monkeypatch.setattr(x_client, "post_reply", lambda _c, _t, _tid: None)  # 발행 실패

    result = run_reply.main()
    assert result["published"] == 0
    assert result["skip_reasons"]["PUBLISH_FAIL"] == 1
    assert updated == [("100", "PUBLISH_FAIL")]
    assert mem.history["100"]["responded"] is False


def test_s09_budget_write_blocked_records_reason(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    monkeypatch.setattr(
        store, "get_budget",
        lambda d: {"budget_date": d, "read_calls": 0, "write_calls": 5,
                   "gemini_calls": 0, "est_cost_krw": 0.0},  # count 모드 write 상한 도달
    )
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()
    assert result["published"] == 0
    assert published == []
    assert result["skip_reasons"]["BUDGET_WRITE"] == 1
    assert mem.history["100"]["skip_reason"] == "BUDGET_WRITE"  # insert 시점 기록


# ---------------------------------------------------------------------------
# S-11: store.update_skip_reason CRUD
# ---------------------------------------------------------------------------

def test_s11_update_skip_reason(monkeypatch):
    from tests.test_reply_store import _patch_client

    _patch_client(monkeypatch, data=[{"reply_tweet_id": "t1"}])
    assert store.update_skip_reason("t1", "PUBLISH_FAIL") is True

    _patch_client(monkeypatch, fail=True)
    assert store.update_skip_reason("t1", "PUBLISH_FAIL") is False


# ---------------------------------------------------------------------------
# S-13: shadow 게이트 탈락 기록
# ---------------------------------------------------------------------------

def test_s13_shadow_gate_fail_recorded(monkeypatch):
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    from reply_engine import generator

    banned = [{"id": "100", "reply": "지금 매수하세요"}]  # 금지어 → 게이트 탈락
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True, "data": banned},
    )

    result = run_reply.main()
    assert result["published"] == 0
    assert result["skip_reasons"]["GATE_BANNED_WORD"] == 1
    assert mem.history["100"]["skip_reason"] == "GATE_BANNED_WORD"
    assert mem.history["100"]["mode"] == "shadow"


def test_pipeline_versions_bumped():
    """R-1/R-2 + B 시리즈 보완 반영 버전 확인 (지침 5)."""
    assert run_reply.VERSION == "1.0.4"
    assert store.VERSION == "1.0.1"
