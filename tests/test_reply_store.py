"""reply_engine/store.py 단위 테스트 — fake supabase 클라이언트 사용."""

from __future__ import annotations

import re

from reply_engine import store


class _FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _FakeQuery:
    """supabase-py 체이닝 API 최소 모사."""

    def __init__(self, result: _FakeResult, fail: bool = False):
        self._result = result
        self._fail = fail

    def __getattr__(self, name):
        # R-11: postgrest의 not_ 은 메서드가 아니라 property다 (.not_.is_(...) 체이닝)
        if name == "not_":
            return self

        def _chain(*_args, **_kwargs):
            return self
        return _chain

    def execute(self):
        if self._fail:
            raise RuntimeError("db down")
        return self._result


class _FakeClient:
    def __init__(self, result: _FakeResult, fail: bool = False):
        self._query = _FakeQuery(result, fail)

    def table(self, _name):
        return self._query


def _patch_client(monkeypatch, data=None, count=None, fail=False):
    client = _FakeClient(_FakeResult(data, count), fail=fail)
    monkeypatch.setattr(store, "get_client", lambda: client)


# ---------------------------------------------------------------------------
# 시간 헬퍼
# ---------------------------------------------------------------------------

def test_kst_helpers_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", store.kst_today())
    iso = store.kst_day_start_utc_iso()
    # KST 자정 = UTC 15:00 (전날)
    assert "T15:00:00" in iso


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

def test_history_exists_true_false(monkeypatch):
    """R-11: 중복 기준은 '실제 발행됨' 또는 '재시도 창 경과'다."""
    _patch_client(monkeypatch, data=[{"reply_tweet_id": "t1"}])
    assert store.history_exists("t1") is True

    _patch_client(monkeypatch, data=[])
    assert store.history_exists("t1") is False


def test_history_exists_db_failure_is_conservative(monkeypatch):
    """조회 실패 시 True(=이미 존재) 반환 — 확인 불가면 발행 금지."""
    _patch_client(monkeypatch, fail=True)
    assert store.history_exists("t1") is True


def test_insert_and_mark(monkeypatch):
    _patch_client(monkeypatch, data=[{"reply_tweet_id": "t1"}])
    assert store.insert_history({"reply_tweet_id": "t1"}) is True
    assert store.mark_responded("t1", "r1") is True

    _patch_client(monkeypatch, fail=True)
    assert store.insert_history({"reply_tweet_id": "t1"}) is False
    assert store.mark_responded("t1", "r1") is False


def test_counts_conservative_on_failure(monkeypatch):
    _patch_client(monkeypatch, data=[], count=2)
    assert store.count_responded_today() == 2
    assert store.count_author_responded_today("a") == 2
    assert store.count_conversation_responded_today("c") == 2

    _patch_client(monkeypatch, fail=True)
    # 실패 시 매우 큰 값 → 상한 로직이 자동 차단
    assert store.count_responded_today() >= 10**9


def test_recent_texts_and_blacklist(monkeypatch):
    _patch_client(monkeypatch, data=[{"response_text": "감사합니다"}, {"response_text": None}])
    assert store.get_recent_response_texts() == ["감사합니다"]

    _patch_client(monkeypatch, data=[{"author_id": "999"}])
    assert store.get_blacklist_ids() == {"999"}

    _patch_client(monkeypatch, fail=True)
    assert store.get_recent_response_texts() == []
    assert store.get_blacklist_ids() == set()


def test_cursor_and_budget(monkeypatch):
    _patch_client(monkeypatch, data=[{"account": "kr_main", "since_id": "5", "my_user_id": "1"}])
    cursor = store.get_cursor("kr_main")
    assert cursor["since_id"] == "5"
    assert store.upsert_cursor("kr_main", "6", "1") is True

    _patch_client(monkeypatch, data=[])
    budget = store.get_budget("2026-08-17")
    assert budget["read_calls"] == 0
    assert budget["est_cost_krw"] == 0.0

    _patch_client(monkeypatch, data=[{"budget_date": "2026-08-17"}])
    assert store.upsert_budget({"budget_date": "2026-08-17", "read_calls": 1}) is True
