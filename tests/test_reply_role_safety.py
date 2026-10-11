"""Replay the October role reversal with mocked external services."""

import pytest

import run_reply
from reply_engine import classifier, gate, generator, policy
from tests.test_reply_v2 import setup_pipeline, tweet

COMMENT = "@tiger18272 이런 지표 정리 덕분에 시장 온도를 한눈에 파악할 수 있네요"


@pytest.mark.parametrize("label", ["POSITIVE", "SUPPORTIVE_NEUTRAL"])
def test_content_benefit_is_praise_even_with_neutral_label(label):
    assert policy.intent_for(COMMENT, label) == "PRAISE"


@pytest.mark.parametrize("reply", [
    "지표 정리 감사합니다 😊", "자료 공유 고맙습니다", "분석 감사합니다",
    "지표 정리해 주셔서 감사합니다", "자료를 정리해주셔서 고마워요",
    "분석해 주신 점 감사합니다", "정보 제공해주셔서 감사합니다",
])
def test_content_credit_cannot_be_reversed(reply):
    assert gate.check_reply(reply, [], COMMENT) == (False, "GATE_ROLE_REVERSAL")


@pytest.mark.parametrize("reply", [
    "좋게 봐주셔서 감사해요 😊", "도움이 됐다니 다행이에요", "읽어주셔서 고맙습니다",
])
def test_reader_thanks_remain_allowed(reply):
    assert gate.check_reply(reply, [], COMMENT) == (True, None)


def test_actual_reader_contribution_is_not_blanket_blocked():
    assert gate.check_reply("자료 공유 감사합니다", [], "제가 정리한 자료 공유합니다")[0]


@pytest.mark.parametrize("comment", [
    "이런 지표 정리\n덕분에 한눈에 파악할 수 있네요",
    "지표 정리\r\n덕분에 한눈에 파악할 수 있네요",
    "지표 정리\t덕분에 한눈에 파악할 수 있네요",
])
def test_multiline_content_praise_keeps_role_guard(comment):
    assert policy.intent_for(comment, "SUPPORTIVE_NEUTRAL") == "PRAISE"
    assert gate.check_reply("지표 정리 감사합니다", [], comment)[1] == "GATE_ROLE_REVERSAL"


@pytest.mark.parametrize("comment", [
    "자료가 유익해서 저도 정리해 공유합니다",
    "지표 정리 덕분에 이해됐어요. 제가 만든 자료도 공유해요",
    "제가 정리한 자료를 첨부했습니다",
])
def test_explicit_reader_sharing_preserves_credit(comment):
    assert policy.intent_for(comment, "SUPPORTIVE_NEUTRAL") == "ACK"
    assert not policy.praises_our_content(comment)
    assert gate.check_reply("자료 공유 감사합니다", [], comment)[0]


@pytest.mark.parametrize("comment", [
    "자료가 유익해서 저도 정리해 공유하지 않습니다",
    "지표 정리 덕분에 제가 잘 이해했어요",
])
def test_first_person_without_sharing_does_not_disable_guard(comment):
    assert not policy.reader_contributes_content(comment)
    assert gate.check_reply("자료 공유 감사합니다", [], comment)[1] == "GATE_ROLE_REVERSAL"


def test_negated_benefit_is_not_praise():
    assert policy.intent_for("자료가 도움이 안 돼요", "SUPPORTIVE_NEUTRAL") == "ACK"


def test_bad_ai_draft_is_replaced_before_publication(monkeypatch):
    _, sent = setup_pipeline(monkeypatch, [tweet(text=COMMENT)])
    monkeypatch.setattr(classifier, "classify_batch", lambda _: {"100": "SUPPORTIVE_NEUTRAL"})
    monkeypatch.setattr(generator, "generate_batch", lambda _: {"100": "지표 정리 감사합니다 😊"})
    report = run_reply.main()
    assert report["actual_published"] == 1
    review = report["review"][0]
    assert review["draft_gate_reason"] == "GATE_ROLE_REVERSAL"
    assert review["source"] == "TEMPLATE_FALLBACK"
    assert sent[0][1] in policy.SAFE_POOLS["PRAISE"]


def test_role_guard_exhaustion_never_posts(monkeypatch):
    _, sent = setup_pipeline(monkeypatch, [tweet(text=COMMENT)])
    monkeypatch.setattr(classifier, "classify_batch", lambda _: {"100": "SUPPORTIVE_NEUTRAL"})
    monkeypatch.setattr(generator, "generate_batch", lambda _: {"100": "지표 정리 감사합니다 😊"})
    monkeypatch.setattr(generator, "contextual_fallbacks", lambda _: ())
    report = run_reply.main()
    assert report["skip_reasons"] == {"GATE_ROLE_REVERSAL": 1}
    assert report["actual_published"] == 0 and sent == []


@pytest.mark.parametrize("platform", ["x", "facebook"])
def test_prompt_explicitly_keeps_content_credit(monkeypatch, platform):
    prompts = []

    def fake(**kwargs):
        prompts.append(kwargs["prompt"])
        return {"success": False, "error": "offline"}

    monkeypatch.setattr(generator, "gemini_call", fake)
    generator.generate_batch([{"id": "1", "text": COMMENT, "label": "SUPPORTIVE_NEUTRAL"}],
                             platform=platform)
    assert "내가 만든 자료를 상대가 만든 것처럼 감사하지 않는다" in prompts[0]
    assert '"intent": "PRAISE"' in prompts[0]
