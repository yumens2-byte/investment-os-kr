"""reply_engine — x_client 단위 + run_reply 파일럿 (E2E, 외부 의존 전량 목킹)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import run_reply
from reply_engine import x_client

# ---------------------------------------------------------------------------
# x_client 단위
# ---------------------------------------------------------------------------

def test_get_x_client_missing_env(monkeypatch):
    for key in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(key, raising=False)
    assert x_client.get_x_client() is None


def test_fetch_mentions_parses_response():
    tweet = SimpleNamespace(
        id=100,
        text="@edt 잘 봤습니다",
        author_id=222,
        conversation_id=300,
        in_reply_to_user_id=111,
        created_at=datetime.now(UTC),
    )
    user = SimpleNamespace(
        id=222,
        username="fan",
        created_at=datetime.now(UTC) - timedelta(days=500),
        public_metrics={"followers_count": 42},
    )
    resp = SimpleNamespace(
        data=[tweet],
        includes={"users": [user]},
        meta={"newest_id": "100"},
    )

    class _Client:
        def get_users_mentions(self, _id, **_kwargs):
            return resp

    result = x_client.fetch_mentions(_Client(), "111", since_id=None)
    assert result["success"] is True
    assert result["newest_id"] == "100"
    assert result["tweets"][0] == {
        "id": "100",
        "text": "@edt 잘 봤습니다",
        "author_id": "222",
        "conversation_id": "300",
        "in_reply_to_user_id": "111",
        "created_at": tweet.created_at,
    }
    assert result["users"]["222"]["followers"] == 42


def test_fetch_mentions_api_failure():
    class _Client:
        def get_users_mentions(self, _id, **_kwargs):
            raise RuntimeError("429 Too Many Requests")

    result = x_client.fetch_mentions(_Client(), "111", since_id="1")
    assert result["success"] is False
    assert result["tweets"] == []


def test_post_reply_no_retry():
    calls = {"n": 0}

    class _Client:
        def create_tweet(self, **_kwargs):
            calls["n"] += 1
            raise RuntimeError("timeout")

    assert x_client.post_reply(_Client(), "감사합니다", "100") is None
    assert calls["n"] == 1  # 재시도 없음 (승인 E)


# ---------------------------------------------------------------------------
# run_reply 파일럿 — 공용 목킹 헬퍼
# ---------------------------------------------------------------------------

class _MemStore:
    """store 모듈 대체 인메모리 구현 + 호출 추적."""

    def __init__(self):
        self.history: dict[str, dict] = {}
        self.cursor: dict | None = {"account": "kr_main", "since_id": "1", "my_user_id": "111"}
        self.budget_saved: list[dict] = []
        self.cursor_saved: list[tuple] = []
        self.blacklist: set[str] = set()
        self.responded_count = 0

    def install(self, monkeypatch):
        import reply_engine.filter as filter_mod
        from reply_engine import store

        for mod in (store, filter_mod.store):
            monkeypatch.setattr(mod, "history_exists", lambda tid: tid in self.history)
            monkeypatch.setattr(mod, "count_author_responded_today", lambda _a: 0)
            monkeypatch.setattr(mod, "count_conversation_responded_today", lambda _c: 0)
        monkeypatch.setattr(store, "insert_history", self._insert)
        monkeypatch.setattr(store, "mark_responded", self._mark)
        monkeypatch.setattr(store, "count_responded_today", lambda: self.responded_count)
        monkeypatch.setattr(store, "get_recent_response_texts", lambda _n=30: [])
        monkeypatch.setattr(store, "get_cursor", lambda _a: self.cursor)
        monkeypatch.setattr(
            store, "upsert_cursor", lambda a, s, u: self.cursor_saved.append((a, s, u)) or True
        )
        monkeypatch.setattr(store, "get_blacklist_ids", lambda: self.blacklist)
        monkeypatch.setattr(
            store,
            "get_budget",
            lambda d: {
                "budget_date": d, "read_calls": 0, "write_calls": 0,
                "gemini_calls": 0, "est_cost_krw": 0.0,
            },
        )
        monkeypatch.setattr(store, "upsert_budget", lambda r: self.budget_saved.append(r) or True)

    def _insert(self, record):
        tid = record["reply_tweet_id"]
        if tid in self.history:
            return False
        self.history[tid] = dict(record)
        return True

    def _mark(self, tid, rid):
        self.history[tid]["responded"] = True
        self.history[tid]["response_tweet_id"] = rid
        return True


def _mk_tweets():
    now = datetime.now(UTC)
    return [
        # 통과 대상 (긍정)
        {"id": "100", "text": "@edt 오늘도 감사합니다!", "author_id": "222",
         "conversation_id": "c1", "in_reply_to_user_id": "111", "created_at": now},
        # 범위 밖 (외부 멘션)
        {"id": "101", "text": "@edt 아무말", "author_id": "333",
         "conversation_id": "c2", "in_reply_to_user_id": "999", "created_at": now},
        # 스팸 링크
        {"id": "102", "text": "좋아요 https://spam.io", "author_id": "444",
         "conversation_id": "c3", "in_reply_to_user_id": "111", "created_at": now},
    ]


def _install_x(monkeypatch, published: list, fetch_success=True):
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(x_client, "fetch_my_user_id", lambda _c: "111")
    monkeypatch.setattr(
        x_client,
        "fetch_mentions",
        lambda _c, _uid, _sid: {
            "success": fetch_success,
            "tweets": _mk_tweets() if fetch_success else [],
            "users": {"222": {"username": "fan", "followers": 50,
                              "created_at": datetime.now(UTC) - timedelta(days=400)}},
            "newest_id": "102" if fetch_success else None,
            "error": None if fetch_success else "fail",
        },
    )
    monkeypatch.setattr(
        x_client,
        "post_reply",
        lambda _c, text, tid: published.append((tid, text)) or f"resp-{tid}",
    )


def _quiet(monkeypatch):
    monkeypatch.setattr(run_reply.time, "sleep", lambda _s: None)
    monkeypatch.setattr(run_reply.random, "randint", lambda _a, _b: 0)
    # Gemini 결정적 목킹 — 파일럿에서 실 네트워크 호출 원천 차단
    from reply_engine import classifier, generator

    monkeypatch.setattr(
        classifier, "gemini_call",
        lambda **_k: {"success": False, "data": None, "error": "mocked"},
    )
    fixed_reply = [{"id": "100", "reply": "감사합니다, 큰 힘이 돼요"}]
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True, "data": fixed_reply},
    )


def _base_env(monkeypatch, mode):
    monkeypatch.setenv("REPLY_ENABLED", "true")
    monkeypatch.setenv("REPLY_MODE", mode)
    monkeypatch.delenv("X_READ_COST_KRW", raising=False)
    monkeypatch.delenv("X_WRITE_COST_KRW", raising=False)


# ---------------------------------------------------------------------------
# 파일럿 시나리오
# ---------------------------------------------------------------------------

def test_pilot_disabled(monkeypatch):
    monkeypatch.setenv("REPLY_ENABLED", "false")
    result = run_reply.main()
    assert result["exit_reason"] == "EXIT_DISABLED"
    assert result["success"] is False


def test_pilot_live_full_path(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()

    assert result["success"] is True
    assert result["collected"] == 3
    assert result["candidates"] == 1              # 100만 통과
    assert result["published"] == 1
    assert published == [("100", mem.history["100"]["response_text"])]
    assert mem.history["100"]["responded"] is True
    assert mem.history["100"]["response_tweet_id"] == "resp-100"
    assert result["skip_reasons"]["OUT_OF_SCOPE"] == 1
    assert result["skip_reasons"]["SPAM_LINK"] == 1
    assert mem.cursor_saved == [("kr_main", "102", "111")]   # 커서 전진
    assert len(mem.budget_saved) == 1                        # 예산 저장


def test_pilot_dry_run_no_db_no_publish(monkeypatch):
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()

    assert result["success"] is True
    assert result["published"] == 1               # 시뮬레이션 카운트
    assert published == []                        # X 발행 없음
    assert mem.history == {}                      # DB 쓰기 없음
    assert mem.cursor_saved == []                 # 커서 미전진
    assert mem.budget_saved == []                 # 예산 미저장


def test_pilot_shadow_records_without_publish(monkeypatch):
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()

    assert result["published"] == 1
    assert published == []                        # X 발행 없음
    assert mem.history["100"]["mode"] == "shadow"
    assert mem.history["100"]["responded"] is False
    assert mem.cursor_saved != []                 # 커서 전진 O


def test_pilot_duplicate_blocked(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.history["100"] = {"reply_tweet_id": "100", "responded": True}   # 기존 이력
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()

    assert result["published"] == 0
    assert published == []
    assert result["skip_reasons"]["DUP"] == 1


def test_pilot_daily_cap(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.responded_count = 8                       # 이미 상한 도달
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()

    assert result["published"] == 0
    assert result["skip_reasons"]["DAILY_CAP"] == 1


def test_pilot_fetch_failure_exits(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [], fetch_success=False)

    result = run_reply.main()
    assert result["exit_reason"] == "EXIT_FETCH_FAIL"
    assert result["success"] is False
