"""reply_engine — filter / classifier / generator 단위 테스트."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reply_engine import classifier, generator
from reply_engine import filter as filter_mod

_MY_ID = "111"


def _tweet(**overrides):
    base = {
        "id": "t1",
        "text": "@edt 오늘 브리핑 잘 봤습니다",
        "author_id": "222",
        "conversation_id": "c1",
        "in_reply_to_user_id": _MY_ID,
        "created_at": datetime.now(UTC) - timedelta(hours=1),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# filter — 정적 검사
# ---------------------------------------------------------------------------

def test_filter_scope_self_blacklist():
    ok, _ = filter_mod.check_tweet(_tweet(), None, _MY_ID, set())
    assert ok

    assert filter_mod.check_tweet(
        _tweet(in_reply_to_user_id="999"), None, _MY_ID, set()
    )[1] == "OUT_OF_SCOPE"
    assert filter_mod.check_tweet(
        _tweet(author_id=_MY_ID), None, _MY_ID, set()
    )[1] == "SELF"
    assert filter_mod.check_tweet(
        _tweet(), None, _MY_ID, {"222"}
    )[1] == "BLACKLIST"


def test_filter_expired_and_text_heuristics():
    old = datetime.now(UTC) - timedelta(hours=25)
    assert filter_mod.check_tweet(_tweet(created_at=old), None, _MY_ID, set())[1] == "EXPIRED"

    assert filter_mod.check_tweet(
        _tweet(text="@edt ㅇ"), None, _MY_ID, set()
    )[1] == "TOO_SHORT"
    assert filter_mod.check_tweet(
        _tweet(text="좋아요 https://spam.io"), None, _MY_ID, set()
    )[1] == "SPAM_LINK"
    assert filter_mod.check_tweet(
        _tweet(text="리딩방 초대합니다"), None, _MY_ID, set()
    )[1] == "SPAM_KEYWORD"


def test_filter_spam_account_heuristic():
    fresh_no_followers = {
        "username": "bot123",
        "followers": 0,
        "created_at": datetime.now(UTC) - timedelta(days=3),
    }
    assert filter_mod.check_tweet(
        _tweet(), fresh_no_followers, _MY_ID, set()
    )[1] == "SPAM_ACCOUNT"

    # 오래된 계정은 팔로워 0이어도 통과
    old_account = {
        "username": "old",
        "followers": 0,
        "created_at": datetime.now(UTC) - timedelta(days=400),
    }
    ok, _ = filter_mod.check_tweet(_tweet(), old_account, _MY_ID, set())
    assert ok


def test_filter_caps_and_dup(monkeypatch):
    monkeypatch.setattr(filter_mod.store, "history_exists", lambda _id: True)
    assert filter_mod.check_caps_and_dup(_tweet())[1] == "DUP"

    monkeypatch.setattr(filter_mod.store, "history_exists", lambda _id: False)
    monkeypatch.setattr(filter_mod.store, "count_author_responded_today", lambda _a: 1)
    assert filter_mod.check_caps_and_dup(_tweet())[1] == "AUTHOR_CAP"

    monkeypatch.setattr(filter_mod.store, "count_author_responded_today", lambda _a: 0)
    monkeypatch.setattr(filter_mod.store, "count_conversation_responded_today", lambda _c: 3)
    assert filter_mod.check_caps_and_dup(_tweet())[1] == "CONV_CAP"

    monkeypatch.setattr(filter_mod.store, "count_conversation_responded_today", lambda _c: 0)
    ok, reason = filter_mod.check_caps_and_dup(_tweet())
    assert ok and reason is None


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------

def test_classify_by_rule():
    assert classifier.classify_by_rule("정말 감사합니다!") == "POSITIVE"
    assert classifier.classify_by_rule("완전 틀렸네요") == "NEGATIVE"
    assert classifier.classify_by_rule("환율 어떻게 보시나요?") == "QUESTION"
    assert classifier.classify_by_rule("음 그렇군요") is None  # 모호 → AI 위임


def test_classify_batch_rule_only_skips_ai(monkeypatch):
    def _no_call(**_kwargs):
        raise AssertionError("룰 확정 건만 있으면 AI 호출이 없어야 한다")

    monkeypatch.setattr(classifier, "gemini_call", _no_call)
    labels = classifier.classify_batch([{"id": "a", "text": "감사합니다"}])
    assert labels == {"a": "POSITIVE"}


def test_classify_batch_ai_and_failure(monkeypatch):
    monkeypatch.setattr(
        classifier,
        "gemini_call",
        lambda **_k: {"success": True, "data": [{"id": "b", "label": "SUPPORTIVE_NEUTRAL"}]},
    )
    labels = classifier.classify_batch([{"id": "b", "text": "음 그렇군요"}])
    assert labels == {"b": "SUPPORTIVE_NEUTRAL"}

    # AI 실패 → AMBIGUOUS (default-deny)
    monkeypatch.setattr(
        classifier, "gemini_call", lambda **_k: {"success": False, "data": None, "error": "429"}
    )
    labels = classifier.classify_batch([{"id": "c", "text": "음 그렇군요"}])
    assert labels == {"c": "AMBIGUOUS"}

    # 비정상 라벨 → AMBIGUOUS
    monkeypatch.setattr(
        classifier,
        "gemini_call",
        lambda **_k: {"success": True, "data": [{"id": "d", "label": "WEIRD"}]},
    )
    labels = classifier.classify_batch([{"id": "d", "text": "음 그렇군요"}])
    assert labels == {"d": "AMBIGUOUS"}


# ---------------------------------------------------------------------------
# generator
# ---------------------------------------------------------------------------

def test_generate_batch_ai_success(monkeypatch):
    monkeypatch.setattr(
        generator,
        "gemini_call",
        lambda **_k: {"success": True, "data": [{"id": "a", "reply": "감사합니다, 힘이 되네요"}]},
    )
    replies = generator.generate_batch([{"id": "a", "text": "잘 봤어요", "label": "POSITIVE"}])
    assert replies == {"a": "감사합니다, 힘이 되네요"}


def test_generate_batch_pool_fallback_deterministic(monkeypatch):
    monkeypatch.setattr(
        generator, "gemini_call", lambda **_k: {"success": False, "data": None, "error": "down"}
    )
    items = [{"id": "same-id", "text": "잘 봤어요", "label": "POSITIVE"}]
    first = generator.generate_batch(items)["same-id"]
    second = generator.generate_batch(items)["same-id"]
    assert first == second  # 동일 댓글 → 동일 문구 (멱등)
    assert first  # 비어 있지 않음


def test_generate_batch_empty():
    assert generator.generate_batch([]) == {}
