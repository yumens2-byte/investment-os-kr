"""2026-10-28 자연스러운 호응 고도화 회귀.

근거: docs/reply_upgrade_naturalness_20261028.md. 댓글 원문은 운영 DB 실측 사례다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import core.gemini_gateway as gw
import run_reply
from reply_engine import classifier, gate, generator, policy, x_client
from reply_engine.config import REPLY_SIMILARITY_THRESHOLD
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet

VOTE_PARENT = (
    "🔥 극단적 선택\n\nA) 서울에 30평대 아파트, 대출 10억 남았고\n"
    "B) 현금 10억 보유. 월 300만원씩 배당주 투자 중."
)
LONG_OPINION = (
    "시장이 중립인데 파생과 신용 온도가 일치하는 점이 흥미롭네요. "
    "변동성 지표까지 같이 보면 해석이 더 선명해질 것 같아요"
)


# ── 의도 판정 (실측 댓글) ──────────────────────────────────────

@pytest.mark.parametrize(
    "comment,label,parent,expected",
    [
        ("@tiger18272 공포탐욕이 하루만에 8점이나 뛰다니 공포 지림요 ㅠ",
         "SUPPORTIVE_NEUTRAL", "", "WORRY"),
        ("@tiger18272 비상비상  버텨라", "SUPPORTIVE_NEUTRAL", "", "WORRY"),
        ("@tiger18272 좋은 하루 보내세요 ㅎㅎ", "POSITIVE", "", "GREETING"),
        ("@tiger18272 감사해요 행복한 밤 보내세요 🍒", "POSITIVE", "", "GREETING"),
        ("@tiger18272 저는 B요 ㅎ 금리 8%면 이자가 월 660만원대로 뛰는 셈이라",
         "SUPPORTIVE_NEUTRAL", VOTE_PARENT, "CHOICE"),
        ("@tiger18272 저는 B할래요 ㅋㅋㅋㅋ", "SUPPORTIVE_NEUTRAL", "", "CHOICE"),
        (f"@tiger18272 {LONG_OPINION}", "SUPPORTIVE_NEUTRAL", "", "OPINION"),
        # 기존 의도는 그대로 유지돼야 한다
        ("@tiger18272 에피소드 100화 축하드립니다 화이팅입니다!!",
         "POSITIVE", "", "CONGRATULATIONS"),
        ("@tiger18272 가즈아!!!!!", "SUPPORTIVE_NEUTRAL", "", "MARKET_HYPE"),
        ("@tiger18272 👍👍👍", "POSITIVE", "", "REACTION"),
        ("@tiger18272 ㅋㅋㅋ", "POSITIVE", "", "LAUGH"),
        ("@tiger18272 잘보고 있습니다^^", "POSITIVE", "", "PRAISE"),
        ("@tiger18272 감사합니다", "POSITIVE", "", "THANKS"),
        ("@tiger18272 리더는 무겁지..", "SUPPORTIVE_NEUTRAL", "", "ACK"),
    ],
)
def test_intent_for_real_comments(comment, label, parent, expected):
    assert policy.intent_for(comment, label, parent) == expected


@pytest.mark.parametrize("comment,expected", [
    ("감사합니다ㅠㅠ", "THANKS"),                       # 감동의 ㅠ
    ("너무 좋아요ㅠㅠ 최고", "ACK"),
    ("어렵지 않게 설명해주셔서 좋네요", "ACK"),           # 부정된 약한 신호
    ("공포탐욕지수 흐름 잘 보고 있어요", "PRAISE"),        # 지수 이름
    ("걱정 마세요 다 잘될 거예요", "ACK"),
    ("투자 너무 어렵네요 ㅠ", "WORRY"),
    ("시장은 매일 전쟁터입니다 ㅠㅠ", "WORRY"),
])
def test_worry_precision(comment, expected):
    label = "POSITIVE" if expected == "PRAISE" else "SUPPORTIVE_NEUTRAL"
    assert policy.intent_for(comment, label) == expected


def test_choice_needs_vote_context_or_choice_words():
    # 단독 알파벳만으로는 선택으로 보지 않는다
    assert not policy.is_choice_comment("오늘 A 섹터 흐름 좋네요")
    assert policy.is_choice_comment("오늘 A 섹터 흐름 좋네요", VOTE_PARENT)
    assert policy.is_choice_comment("B 선택할게요")


# ── 신규 안전 풀 ───────────────────────────────────────────────

_INTENT_SAMPLES = {
    "WORRY": "공포탐욕이 하루만에 8점이나 뛰다니 공포 지림요 ㅠ",
    "GREETING": "좋은 하루 보내세요 ㅎㅎ",
    "CHOICE": "저는 B할래요 ㅋㅋㅋㅋ",
    "OPINION": LONG_OPINION,
}


@pytest.mark.parametrize("intent", sorted(_INTENT_SAMPLES))
def test_new_pools_pass_gate_and_are_diverse(intent):
    comment = _INTENT_SAMPLES[intent]
    assert policy.intent_for(comment, "SUPPORTIVE_NEUTRAL") == intent
    pool = policy.SAFE_POOLS[intent]
    assert len(pool) >= 4
    for text in pool:
        assert gate.check_reply(text, [], comment_text=comment) == (True, None), text
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            assert gate.jaccard_similarity(a, b) < REPLY_SIMILARITY_THRESHOLD, (a, b)


def test_contextual_fallbacks_follow_new_intent():
    item = {"id": "2114246585119092983", "text": _INTENT_SAMPLES["WORRY"],
            "label": "SUPPORTIVE_NEUTRAL"}
    assert set(generator.contextual_fallbacks(item)) == set(policy.SAFE_POOLS["WORRY"])
    vote = {"id": "1", "text": "저는 B를 고를래요", "label": "SUPPORTIVE_NEUTRAL",
            "parent_text": VOTE_PARENT}
    assert set(generator.contextual_fallbacks(vote)) == set(policy.SAFE_POOLS["CHOICE"])


# ── 게이트 ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "reply", ["ㅎㅎ 반가워요", "기대되는 흐름이네요", "오늘도 화이팅이에요 🚀"]
)
def test_tone_gate_blocks_cheer_on_worry(reply):
    assert gate.check_reply(reply, [], comment_text=_INTENT_SAMPLES["WORRY"]) == (
        False, "GATE_TONE")


@pytest.mark.parametrize("reply", ["좋은 선택이에요 😊", "B가 낫죠", "현명하게 고르셨네요"])
def test_choice_gate_blocks_endorsement(reply):
    ok, reason = gate.check_reply(reply, [], comment_text="저는 B할래요 ㅋㅋㅋㅋ")
    assert not ok and reason == "GATE_INTENT"


@pytest.mark.parametrize("comment", [
    "@tiger18272 류님 저녁 맛있게 드세용 😄😄",
    "@tiger18272 점심 맛있게 드세요",
    "@tiger18272 맛저하세요~",
    "@tiger18272 오늘은 푹 쉬세요",
])
def test_meal_and_rest_wishes_are_greetings(comment):
    assert policy.intent_for(comment, "POSITIVE") == "GREETING"


def test_live_incident_wish_misread_is_replaced():
    """10-28 live 실측: '저녁 맛있게 드세용' → '맛있는 저녁 되셨다니 다행이에요!' 발행."""
    item = {"id": "2115373192760140047", "text": "@tiger18272 류님 저녁 맛있게 드세용 😄😄",
            "label": "POSITIVE"}
    bad = "맛있는 저녁 되셨다니 다행이에요! 😊"
    assert gate.check_reply(bad, [], item["text"]) == (False, "GATE_ROLE_REVERSAL")
    text, source, reason = gate.choose_reply(
        [bad], generator.contextual_fallbacks(item), [], item["text"])
    assert reason == "GATE_ROLE_REVERSAL" and source == "TEMPLATE_FALLBACK"
    assert text in policy.SAFE_POOLS["GREETING"]


def test_congratulations_requires_acknowledgement():
    comment = "@tiger18272 에피소드 100화 축하드립니다 화이팅입니다!!"
    assert gate.check_reply("ㅎㅎ 반가워요", [], comment) == (False, "GATE_INTENT")
    for text in policy.SAFE_POOLS["CONGRATULATIONS"]:
        assert gate.check_reply(text, [], comment) == (True, None), text


def test_length_tier_depends_on_comment_length():
    reply = "변동성 지표까지 함께 보신다니 파생과 신용 온도 얘기가 더 입체적으로 읽히네요"
    assert 40 < len(reply) <= 60
    assert gate.check_reply(reply, [], comment_text=LONG_OPINION) == (True, None)
    assert gate.check_reply(reply, [], comment_text="좋네요") == (False, "GATE_LENGTH")
    assert gate.check_reply(reply, []) == (False, "GATE_LENGTH")  # 댓글 미지정 = 기존 40자


def test_choose_reply_order():
    recent = ["관심 가는 흐름이네요 🚀"]
    comment = "원유 시장의 변동성이 커지고 있다"
    text, source, reason = gate.choose_reply(
        ["관심 가는 흐름이네요 🙂", "원유 움직임이 확실히 커졌네요"], ("댓글 잘 봤어요 🙂",),
        recent, comment)
    assert (text, source, reason) == ("원유 움직임이 확실히 커졌네요", "AI_ALT", "GATE_SIMILARITY")

    text, source, reason = gate.choose_reply(
        ["관심 가는 흐름이네요 🙂"], ("댓글 잘 봤어요 🙂",), recent, comment)
    assert (text, source, reason) == ("댓글 잘 봤어요 🙂", "TEMPLATE_FALLBACK", "GATE_SIMILARITY")

    assert gate.choose_reply(["지금 매수하세요"], (), [], comment) == (
        None, "NONE", "GATE_BANNED_WORD")
    assert gate.choose_reply([], (), [], comment) == (None, "NONE", "GATE_EMPTY")


def test_incident_worry_reply_is_replaced_by_empathy():
    """실측: '공포 지림요 ㅠ'에 'ㅎㅎ 반가워요' 발행 → 이제는 공감 풀로 대체된다."""
    item = {"id": "x1", "text": "@tiger18272 " + _INTENT_SAMPLES["WORRY"],
            "label": "SUPPORTIVE_NEUTRAL"}
    text, source, reason = gate.choose_reply(
        ["ㅎㅎ 반가워요"], generator.contextual_fallbacks(item), [], item["text"])
    assert reason == "GATE_TONE" and source == "TEMPLATE_FALLBACK"
    assert text in policy.SAFE_POOLS["WORRY"]


# ── 생성기 ─────────────────────────────────────────────────────

def _capture_generator(monkeypatch, data):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return {"success": True, "data": data}

    monkeypatch.setattr(generator, "gemini_call", fake)
    return captured


def test_generator_returns_candidates_and_accepts_legacy_schema(monkeypatch):
    captured = _capture_generator(monkeypatch, [
        {"id": "a", "replies": [" 첫 후보 ", "첫 후보", "'둘째 후보'", 3, "셋째", "넷째"]},
        {"id": "b", "reply": "구 스키마 답글"},
    ])
    out = generator.generate_batch([
        {"id": "a", "text": "좋네요", "label": "POSITIVE"},
        {"id": "b", "text": LONG_OPINION, "label": "SUPPORTIVE_NEUTRAL"},
    ])
    assert out["a"] == "첫 후보"
    assert out.candidates["a"] == ["첫 후보", "둘째 후보"]  # 중복·비문자 제거, 상위 3개 범위
    assert out["b"] == "구 스키마 답글" and out.candidates["b"] == ["구 스키마 답글"]
    assert out.sources == {"a": "AI", "b": "AI"}
    rows = [json.loads(line) for line in captured["prompt"].splitlines() if line.startswith("{")]
    assert {r["id"]: r["max_len"] for r in rows} == {"a": 40, "b": 60}
    assert {r["id"]: r["intent"] for r in rows}["b"] == "OPINION"
    assert '"replies"' in captured["prompt"]
    assert captured["max_tokens"] == 512 + 200 * 2


def test_generator_prompt_drops_copied_example_sentences(monkeypatch):
    captured = _capture_generator(monkeypatch, [])
    generator.generate_batch([{"id": "a", "text": "가즈아", "label": "POSITIVE"}])
    for copied in ("관심 가는 흐름이네요", "도움이 됐다니 다행이에요", "ㅋㅋ → ㅎㅎ 😄"):
        assert copied not in captured["prompt"], copied


# ── 분류기: QUESTION 오분류 축소 ───────────────────────────────

def test_vote_answer_is_supportive_without_ai(monkeypatch):
    calls = []
    monkeypatch.setattr(classifier, "gemini_call", lambda **k: calls.append(k) or {
        "success": True, "data": []})
    text = ("@tiger18272 저는 B를 고르고 싶은데, 10년 동안 계좌를 안 열어보고 버틸 자신이 "
            "없어서 자꾸 A로 마음이 기웁니다 😅")
    labels = classifier.classify_batch([{"id": "1", "text": text, "parent_text": VOTE_PARENT}])
    assert labels["1"] == "SUPPORTIVE_NEUTRAL" and calls == []


def test_vote_question_still_goes_to_ai(monkeypatch):
    calls = []
    monkeypatch.setattr(classifier, "gemini_call", lambda **k: calls.append(k) or {
        "success": True, "data": [{"id": "1", "label": "QUESTION"}]})
    labels = classifier.classify_batch(
        [{"id": "1", "text": "B 하면 세금이 더 나오는지 궁금", "parent_text": VOTE_PARENT}])
    assert labels["1"] == "QUESTION"
    assert "운영자의 답변을 실제로 요구하는 질문만" in calls[0]["prompt"]


# ── P0-1 X 연결 / P0-2 Gemini 404 ──────────────────────────────

def test_x_client_uses_timeout_session_and_resets(monkeypatch):
    for name in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.setenv(name, "v")
    client = x_client.get_x_client()
    assert isinstance(client.session, x_client.TimeoutSession)
    old = client.session
    closed = []
    monkeypatch.setattr(old, "close", lambda: closed.append(True))
    x_client.reset_connection(client)
    assert closed == [True]
    assert client.session is not old and isinstance(client.session, x_client.TimeoutSession)
    x_client.reset_connection(object())  # session 없는 대역은 무시


def test_timeout_session_applies_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        x_client.requests.Session, "request",
        lambda self, method, url, **kw: seen.update(kw) or "ok",
    )
    session = x_client.TimeoutSession()
    session.request("POST", "https://example.invalid")
    assert seen["timeout"] == x_client.REQUEST_TIMEOUT
    session.request("POST", "https://example.invalid", timeout=5)
    assert seen["timeout"] == 5


def test_gateway_skips_key_after_404(monkeypatch):
    calls = []
    monkeypatch.setattr(gw, "_build_keys", lambda: [("main", "k-main", False),
                                                     ("sub", "k-sub", False)])

    def client(key):
        def generate(**_):
            calls.append(key)
            if key == "k-main":
                raise RuntimeError("404 NOT_FOUND model")
            return SimpleNamespace(text="[]", usage_metadata=None)
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate))

    monkeypatch.setattr(gw, "_get_client", client)
    assert gw.call("a")["key_used"] == "sub"
    assert gw.call("b")["key_used"] == "sub"
    assert calls == ["k-main", "k-sub", "k-sub"]
    assert gw.call("c", model="flash")["success"]  # 다른 모델은 다시 시도
    assert calls[-2:] == ["k-main", "k-sub"]


# ── 파이프라인: 후보 선택 + 근거 저장 + 연결 재생성 ─────────────

def test_live_pipeline_uses_alternate_candidate_and_persists_evidence(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)
    from reply_engine import store

    monkeypatch.setattr(
        store, "get_recent_response_texts", lambda _n=30: ["저야말로 감사합니다 😊"]
    )
    monkeypatch.setattr(generator, "gemini_call", lambda **_k: {"success": True, "data": [
        {"id": "100", "replies": ["저야말로 감사합니다 😊", "오늘도 들러주셔서 힘이 나요"]}]})
    resets = []
    monkeypatch.setattr(x_client, "reset_connection", lambda c: resets.append(c))

    result = run_reply.main()

    assert published == [("100", "오늘도 들러주셔서 힘이 나요")]
    assert len(resets) == 1
    meta = json.loads(mem.history["100"]["error_message"])
    assert meta["response_source"] == "AI_ALT"
    assert meta["draft_gate_reason"] == "GATE_SIMILARITY"
    assert meta["intent"] == "THANKS"
    assert result["source_counts"] == {"AI_ALT": 1}
    entry = next(r for r in result["review"] if r["reply_tweet_id"] == "100")
    assert entry["source"] == "AI_ALT" and entry["draft_gate_reason"] == "GATE_SIMILARITY"


def test_encode_metadata_keeps_previous_evidence_when_absent():
    first = json.loads(policy.encode_metadata(
        {"text": "t", "_response_source": "AI", "_draft_gate_reason": None, "_intent": "ACK"}))
    again = json.loads(policy.encode_metadata({"text": "t", "_metadata": first}))
    assert (again["response_source"], again["intent"]) == ("AI", "ACK")
    assert again["reply_policy"] == policy.POLICY_VERSION == "2.2.0"
