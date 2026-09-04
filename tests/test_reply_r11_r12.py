"""reply_engine — R-11(중복 기준) / R-12(원글 컨텍스트) 검증.

R-11: DB 점검(2026-09-04)에서 shadow 49건 + PUBLISH_FAIL 2건이 발행 없이
      이력에만 남아 L1 DUP으로 영구 차단된 것이 확인됐다.
      중복 기준을 '이력 존재' → '실제 발행됨'으로 바꾸고, 재시도 폭주는
      REPLY_RETRY_WINDOW_HOURS 창으로 막는다.
R-12: 댓글 단문만으로 생성하면 환각·주객전도가 필연이다(P-1 축하 미러링,
      R-9 베트남어 오독의 공통 근본 원인). 원글을 프롬프트에 주입한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reply_engine import generator, store, x_client

# ---------------------------------------------------------------------------
# R-11 — 중복 판정 기준
# ---------------------------------------------------------------------------

class _FakeQuery:
    """postgrest 체이닝 스텁. 적용된 필터를 기록한다."""

    def __init__(self, rows: list[dict], log: list):
        self._rows = rows
        self._log = log
        self._filters: list = []
        self._negate = False

    # 체이닝 API
    def table(self, _n): return self
    def select(self, *_a, **_k): return self
    def limit(self, _n): return self
    def eq(self, c, v):
        self._filters.append(("eq", c, v))
        return self

    def in_(self, c, v):
        self._filters.append(("in", c, list(v)))
        return self

    def lt(self, c, v):
        self._filters.append(("lt", c, v))
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def is_(self, c, v):
        self._filters.append(("not_is" if self._negate else "is", c, v))
        self._negate = False
        return self

    def execute(self):
        self._log.append(list(self._filters))
        kinds = [f[0] for f in self._filters]
        # 발행 완료 조회 / 만료 조회를 필터 조합으로 구분
        if "not_is" in kinds:
            data = [r for r in self._rows if r.get("response_tweet_id")]
        elif "lt" in kinds:
            cutoff = next(f[2] for f in self._filters if f[0] == "lt")
            data = [r for r in self._rows if r.get("created_at", "") < cutoff]
        else:
            data = list(self._rows)
        self._filters = []
        return type("R", (), {"data": [{"reply_tweet_id": r["reply_tweet_id"]} for r in data]})()


def _install_fake(monkeypatch, rows: list[dict]) -> list:
    log: list = []
    monkeypatch.setattr(store, "get_client", lambda: _FakeQuery(rows, log))
    return log


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def test_published_row_is_duplicate(monkeypatch):
    """실제 발행된 건은 중복이다."""
    _install_fake(monkeypatch, [
        {"reply_tweet_id": "t1", "response_tweet_id": "r1", "created_at": _iso(1)},
    ])
    assert store.history_exists_bulk(["t1"]) == {"t1"}
    assert store.history_exists("t1") is True


def test_shadow_row_is_not_duplicate_within_window(monkeypatch):
    """shadow 시뮬레이션(미발행)은 재시도 창 안에서 중복이 아니다."""
    _install_fake(monkeypatch, [
        {"reply_tweet_id": "s1", "response_tweet_id": None, "created_at": _iso(2)},
    ])
    assert store.history_exists_bulk(["s1"]) == set()
    assert store.history_exists("s1") is False


def test_publish_fail_row_is_retryable(monkeypatch):
    """PUBLISH_FAIL 건은 창 안에서 재시도 대상이다."""
    _install_fake(monkeypatch, [
        {"reply_tweet_id": "f1", "response_tweet_id": None, "created_at": _iso(6)},
    ])
    assert store.history_exists_bulk(["f1"]) == set()


def test_expired_unpublished_row_stops_retrying(monkeypatch):
    """창을 넘긴 미발행 이력은 중복 처리되어 재시도가 종결된다 (폭주 방지)."""
    _install_fake(monkeypatch, [
        {"reply_tweet_id": "old1", "response_tweet_id": None, "created_at": _iso(48)},
    ])
    assert store.history_exists_bulk(["old1"]) == {"old1"}
    assert store.history_exists("old1") is True


def test_bulk_failure_is_conservative(monkeypatch):
    """조회 실패 시 전건 중복 — 확인 불가면 발행하지 않는다."""
    class _Boom:
        def table(self, _n): raise RuntimeError("down")
    monkeypatch.setattr(store, "get_client", lambda: _Boom())
    assert store.history_exists_bulk(["a", "b"]) == {"a", "b"}
    assert store.history_exists("a") is True


def test_insert_history_uses_upsert(monkeypatch):
    """재처리 시 PK 충돌을 피하려면 upsert여야 한다."""
    seen: dict = {}

    class _C:
        def table(self, _n): return self
        def upsert(self, record, on_conflict=None):
            seen["record"] = record
            seen["on_conflict"] = on_conflict
            return self
        def execute(self):
            return type("R", (), {"data": [{"reply_tweet_id": "x"}]})()

    monkeypatch.setattr(store, "get_client", lambda: _C())
    assert store.insert_history({"reply_tweet_id": "x"}) is True
    assert seen["on_conflict"] == "reply_tweet_id"


# ---------------------------------------------------------------------------
# R-12 — 원글 컨텍스트
# ---------------------------------------------------------------------------

class _Ref:
    def __init__(self, type_, id_):
        self.type = type_
        self.id = id_


class _T:
    def __init__(self, tid, refs=None):
        self.id = tid
        self.text = "댓글"
        self.author_id = 1
        self.conversation_id = 1
        self.in_reply_to_user_id = 111
        self.created_at = None
        self.referenced_tweets = refs


def test_parent_text_resolves_replied_to():
    """replied_to 참조의 본문을 원글로 해석한다."""
    t = _T(1, [_Ref("replied_to", 900)])
    assert x_client._parent_text(t, {"900": "원글 본문"}) == "원글 본문"


def test_parent_text_ignores_quoted_and_retweeted():
    """quoted/retweeted는 원글이 아니다."""
    t = _T(1, [_Ref("quoted", 900), _Ref("retweeted", 901)])
    assert x_client._parent_text(t, {"900": "인용", "901": "리트윗"}) == ""


def test_parent_text_missing_reference_is_safe():
    """참조 없음·본문 없음·예외 상황에서 빈 문자열을 반환한다."""
    assert x_client._parent_text(_T(1, None), {}) == ""
    assert x_client._parent_text(_T(1, [_Ref("replied_to", 900)]), {}) == ""
    assert x_client._parent_text(object(), {}) == ""


def test_parent_text_accepts_dict_refs():
    """tweepy 버전에 따라 dict로 올 수도 있다."""
    t = _T(1, [{"type": "replied_to", "id": 900}])
    assert x_client._parent_text(t, {"900": "원글"}) == "원글"


def test_expansions_request_referenced_tweets(monkeypatch):
    """추가 콜 없이 부모 트윗을 받도록 expansions가 요청되어야 한다."""
    captured: dict = {}

    class _Client:
        def get_users_mentions(self, _uid, **params):
            captured.update(params)
            return type("R", (), {"data": [], "includes": {}, "meta": {}})()

    x_client.fetch_mentions(_Client(), "111", since_id=None)

    assert "referenced_tweets.id" in captured["expansions"]
    assert "referenced_tweets" in captured["tweet_fields"]


def test_prompt_includes_parent_and_marks_unknown():
    """원글은 프롬프트에 포함되고, 없으면 '(확인 불가)'로 명시된다."""
    with_parent = generator._format_item(
        {"id": "a", "text": "크 멋져요", "parent_text": "오늘 SCHD 분석입니다"}
    )
    assert "오늘 SCHD 분석입니다" in with_parent
    assert "크 멋져요" in with_parent

    without = generator._format_item({"id": "b", "text": "ㅇㅈ"})
    assert "(확인 불가)" in without


def test_prompt_strips_newlines():
    """원글 줄바꿈이 프롬프트 항목 구조를 깨뜨리지 않아야 한다."""
    line = generator._format_item(
        {"id": "c", "text": "좋아요", "parent_text": "첫줄\n둘째줄\n셋째줄"}
    )
    assert "\n" not in line


def test_generate_batch_passes_parent_to_prompt(monkeypatch):
    """generate_batch가 원글을 실제 프롬프트에 실어 보낸다."""
    seen: dict = {}

    def _capture(**kwargs):
        seen["prompt"] = kwargs.get("prompt", "")
        return {"success": True, "data": [{"id": "k1", "reply": "감사합니다 ㅎㅎ"}]}

    monkeypatch.setattr(generator, "gemini_call", _capture)

    generator.generate_batch([
        {"id": "k1", "text": "크 멋져요", "label": "POSITIVE",
         "parent_text": "미국 ETF 주간 리포트"},
    ])

    assert "미국 ETF 주간 리포트" in seen["prompt"]
    assert "원글" in seen["prompt"]
