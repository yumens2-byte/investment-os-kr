"""Independent review regressions for sentiment, fallback roles and emoji sequences."""

from __future__ import annotations

import pytest

import run_reply
from reply_engine import classifier, gate, generator
from reply_engine.policy import intent_for
from tests.test_reply_v2 import setup_pipeline, tweet

CRITICISMS = (
    "도움이 안 됐어요",
    "유익하지 않네요",
    "좋아요라고 한 적 없어요",
    "홍보하지 마세요. 감사합니다",
    "감사할 필요 없어요",
    "도움이 안됐어요",
    "도움 안돼요",
    "안좋아요",
)


@pytest.mark.parametrize("body", CRITICISMS)
def test_negated_positive_requires_context_instead_of_positive_rule(body):
    assert classifier.classify_by_rule(body) is None


@pytest.mark.parametrize("body", ["감사합니다", "자료 유익해요", "잘 봤습니다", "👍🏽"])
def test_clear_positive_does_not_require_model(body, monkeypatch):
    monkeypatch.setattr(classifier, "gemini_call", lambda **_: pytest.fail("unexpected model"))
    result = classifier.classify_batch([{"id": "100", "text": body}])
    assert result == {"100": "POSITIVE"}
    assert result.api_calls == 0


@pytest.mark.parametrize("body", ["유익한가요?", "언제 발표하나요?", "좋은 자료인가요?"])
def test_genuine_questions_stay_excluded(body):
    assert classifier.classify_by_rule(body) == "QUESTION"


@pytest.mark.parametrize("body", CRITICISMS)
def test_criticism_does_not_publish_or_consume_positive_slot(body, monkeypatch):
    mem, sent = setup_pipeline(
        monkeypatch,
        [tweet("99", body, parent_text="시장 자료입니다"), tweet("100", "감사합니다")],
    )
    prompts = []

    def classify(**kwargs):
        prompts.append(kwargs["prompt"])
        return {"success": True, "data": [{"id": "99", "label": "NEGATIVE"}]}

    monkeypatch.setattr(classifier, "gemini_call", classify)
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 1)
    result = run_reply.main()
    assert len(prompts) == 1
    assert body in prompts[0] and "시장 자료입니다" in prompts[0]
    assert [tid for tid, _ in sent] == ["100"]
    assert mem.history["99"]["skip_reason"] == "CLASS_NEGATIVE"
    assert result["actual_published"] == 1


def test_uncertain_negation_is_deferred_without_fallback_publication(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [tweet("99", "유익하지 않네요")])
    monkeypatch.setattr(generator, "generate_batch", lambda _: pytest.fail("generated criticism"))
    result = run_reply.main()
    assert sent == []
    assert mem.history["99"]["skip_reason"] == "CLASSIFIER_UNAVAILABLE"
    assert result["deferred"] == 1


@pytest.mark.parametrize("body", ["엔비디아 좋네요", "시장 흐름 멋지네요", "좋네요"])
def test_market_positive_cannot_use_account_praise_fallback(body, monkeypatch):
    item = {"id": "100", "text": body, "label": "POSITIVE"}
    assert intent_for(body, "POSITIVE") == "ACK"
    candidates = generator.contextual_fallbacks(item)
    assert all("좋게 봐주셔서" not in candidate for candidate in candidates)
    monkeypatch.setattr(generator, "gemini_call", lambda **_: {"success": False})
    assert generator.generate_batch([item])["100"] in candidates


@pytest.mark.parametrize("body", ["자료 좋아요", "정리 멋지네요", "잘 봤습니다", "유익해요"])
def test_explicit_content_praise_retains_praise_pool(body):
    assert intent_for(body, "POSITIVE") == "PRAISE"


@pytest.mark.parametrize("emoji", ["👍🏽", "👏🏻", "👨‍👩‍👧", "👩🏽‍💻", "🇰🇷", "❤️", "😊"])
def test_single_composed_emoji_preserves_generated_reply(emoji):
    assert gate.check_reply(f"감사해요 {emoji}", []) == (True, None)


@pytest.mark.parametrize("emoji", ["👍🏽😊", "👨‍👩‍👧👍", "🇰🇷🇺🇸", "👏🏻👏🏽", "❤️😊"])
def test_two_separate_emoji_remain_blocked(emoji):
    assert gate.check_reply(f"감사해요 {emoji}", []) == (False, "GATE_EMOJI")
