"""DN2026_0098 X 답글 복창·해석 제거 회귀.

사례는 14일 실발행 관찰을 의역한 합성 문장이다 (실제 사용자 댓글 원문·계정명 미사용).
Gemini 호출은 전부 mock (STD-TEST-04).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

import run_reply
from reply_engine import gate, generator, policy, x_client
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet

# ── ① 복창형·해석형 답글 탈락 ────────────────────────────────

@pytest.mark.parametrize("comment,reply,reason", [
    ("아래쪽에 한 표 던져요", "아래쪽을 고르셨군요!", "GATE_RESTATE"),
    ("D가 제일 끌리네요", "D를 선택하셨군요!", "GATE_RESTATE"),
    ("저는 2번으로 갈래요", "2번을 고르셨네요 😊", "GATE_RESTATE"),
    ("우선은 관망하는 쪽이에요", "네, 우선 관망하시는 흐름이시군요.", "GATE_RESTATE"),
    ("지표보다 체감 긴장감이 더 세네요", "그 긴장감이 느껴지시는군요.", "GATE_RESTATE"),
    ("금리 발표가 시장을 크게 흔들었군요", "금리 발표가 시장을 흔들었나 보네요.", "GATE_RESTATE"),
    ("환율 1400 내려온 건 반가운데 아직 모르겠네", "환율 흐름을 짚어주셨네요.", "GATE_RESTATE"),
    ("이번엔 좀 더 쉽게 넘어가겠죠", "이번엔 더 좋은 결과가 있겠죠.", "GATE_RESTATE"),
    ("매수 금액이 바로 보여서 좋네요", "금액이 보이기 편하셨다니 다행이에요.", "GATE_INTERPRET"),
    ("요약돼서 쏙 들어와요", "요약이 쏙 들어온다니 뿌듯하네요.", "GATE_INTERPRET"),
    ("아껴 쓰는 게 답이네요", "네, 현명한 판단이었죠.", "GATE_INTERPRET"),
])
def test_restate_and_interpretation_rejected(comment, reply, reason):
    assert gate.check_reply(reply, [], comment, strict=True) == (False, reason)


def test_keyword_ratio_restate_without_ending():
    comment = "중간 발표 결과가 시장 심리에 영향을 크게 줬어요"
    assert gate.check_reply("발표 결과가 시장 심리에 영향", [], comment, strict=True) == (
        False, "GATE_RESTATE")


def test_existing_echo_gate_still_applies():
    comment = "축하드립니다 대박나세요"
    assert gate.check_reply("축하드립니다 대박나세요", [], comment, strict=True)[0] is False


# ── ② 정상 답글 통과 ─────────────────────────────────────────

@pytest.mark.parametrize("comment,reply", [
    ("좋은 주말 보내세요", "감사합니다, 주말 잘 보내세요"),
    ("반도체 수급 쪽이 눈에 띄네요", "저도 그 부분 눈여겨보고 있어요"),
    ("오늘 정리 잘 봤어요", "읽어주셔서 고맙습니다"),
    ("지표 정리 덕분에 도움이 됐어요", "도움이 됐다니 다행이에요"),
    ("제가 모은 자료 공유합니다", "자료 공유 감사합니다"),
    ("C로 골라볼게요", "참여해 주셔서 고마워요 ㅎㅎ"),
])
def test_natural_replies_pass(comment, reply):
    assert gate.check_reply(reply, [], comment, strict=True) == (True, None)


def test_restate_failure_falls_through_to_next_candidate_then_pool():
    comment = "위쪽에 한 표요"
    drafts = ["위쪽을 선택하셨군요!", "참여 고마워요 🙂"]
    assert gate.choose_reply(drafts, (), [], comment, strict=True) == (
        "참여 고마워요 🙂", "AI_ALT", "GATE_RESTATE")
    pool = generator.contextual_fallbacks({"id": "9", "text": comment, "label": "POSITIVE"},
                                          extended=True)
    chosen, source, reason = gate.choose_reply(["위쪽을 선택하셨군요!"], pool, [], comment,
                                               strict=True)
    assert source == "TEMPLATE_FALLBACK" and reason == "GATE_RESTATE"
    assert chosen in policy.SAFE_POOLS["CHOICE"]


def test_opinion_ai_draft_requires_comment_keyword():
    comment = ("배당 흐름이 안정적이라 저는 현금 비중을 조금 더 두는 편이 낫다고 봐요. "
               "금리 부담도 서서히 줄고 있어서요")
    assert policy.intent_for(comment, "SUPPORTIVE_NEUTRAL") == "OPINION"
    pool = policy.SAFE_POOLS["OPINION"]
    chosen, source, reason = gate.choose_reply(["말씀 잘 들었어요 😊"], pool, [], comment,
                                               strict=True)
    assert reason == "GATE_GENERIC" and source == "TEMPLATE_FALLBACK"
    assert gate.choose_reply(["배당 이야기 공감돼요"], pool, [], comment, strict=True)[1] == "AI"
    # strict=False(FB 등)는 기준 커밋처럼 범용 AI 문구를 그대로 통과시킨다.
    assert gate.choose_reply(["말씀 잘 들었어요 😊"], pool, [], comment) == (
        "말씀 잘 들었어요 😊", "AI", None)


# ── ③ 선택 인식 ──────────────────────────────────────────────

@pytest.mark.parametrize("comment,markers", [
    ("C로 할래요", {"C"}),
    ("위쪽 한 표요", {"위", "한표"}),
    ("3번 선택", {"3번"}),
    ("B 고를래요 저축이 걱정돼서요", {"B"}),
])
def test_choice_recognized_before_worry(comment, markers):
    assert policy.intent_for(comment, "SUPPORTIVE_NEUTRAL", extended=True) == "CHOICE"
    assert policy.choice_markers(comment) == markers


@pytest.mark.parametrize("comment", [
    "2번이나 떨어졌네요 무섭다", "위로가 되네요 걱정이었는데", "위험해 보여서 불안해요",
])
def test_non_choice_worry_unchanged(comment):
    assert policy.intent_for(comment, "SUPPORTIVE_NEUTRAL", extended=True) == "WORRY"


@pytest.mark.parametrize("reply", ["C 쪽도 괜찮죠", "결과가 궁금하네요", "그 선택 응원해요"])
def test_choice_reply_may_not_repeat_marker_or_evaluate(reply):
    assert gate.check_reply(reply, [], "C로 할래요", strict=True)[0] is False


def test_choice_pool_never_names_options():
    for text in policy.SAFE_POOLS["CHOICE"]:
        assert not policy.choice_markers(text) - {"한표"}, text
        for comment in ("C로 할래요", "위쪽 한 표요", "3번 선택"):
            assert gate.check_reply(text, [], comment, strict=True) == (True, None), (
                comment, text)


# ── ④ 재시도 재생성 분기 ─────────────────────────────────────

@pytest.mark.parametrize("meta,skip,keep", [
    ({}, "BUDGET_WRITE", False),
    ({"publish_attempts": 0}, "RUN_CAP", False),
    ({"publish_attempts": 1}, "RUN_CAP", True),
    ({"publish_attempts": 0}, "PUBLISH_RETRYABLE", True),
    ({"publish_attempts": 0}, "PUBLISH_UNKNOWN", True),
    ({"publish_attempts": "x"}, "RUN_CAP", True),
])
def test_retry_keeps_stored_text(meta, skip, keep):
    assert policy.retry_keeps_stored_text(meta, skip) is keep


def _retry_row(attempts: int, skip: str) -> dict:
    meta = {"created_at": datetime.now(UTC), "in_reply_to_user_id": "111",
            "_account_user_id": "111", "text": "좋은 글 감사합니다"}
    if attempts:
        meta["_metadata"] = {"reply_policy": "x", "publish_attempts": attempts}
    return {
        "reply_tweet_id": "failed-1", "conversation_id": "conv-1",
        "author_id": "author-1", "comment_text": "좋은 글 감사합니다",
        "classification": "POSITIVE", "response_text": "예전 초안이에요",
        "skip_reason": skip, "error_message": run_reply.encode_metadata(meta),
    }


@pytest.mark.parametrize("attempts,skip,expected,source", [
    (0, "RUN_CAP", "저야말로 고마워요 🙂", "AI"),
    (1, "RUN_CAP", "예전 초안이에요", "DB_RETRY"),
    (0, "PUBLISH_RETRYABLE", "예전 초안이에요", "DB_RETRY"),
])
def test_retry_regeneration_branch(monkeypatch, attempts, skip, expected, source):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda *_a: {"success": True, "tweets": [], "users": {}, "newest_id": None,
                     "oldest_id": None, "saturated": False, "error": None},
    )
    monkeypatch.setattr(run_reply.store, "get_retryable_history",
                        lambda _limit: [_retry_row(attempts, skip)])
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True,
                      "data": [{"id": "failed-1", "replies": ["저야말로 고마워요 🙂"]}]},
    )

    result = run_reply.main()

    assert published == [("failed-1", expected)]
    assert result["review"][-1]["source"] == source


def test_metadata_records_generator_version_and_keeps_previous():
    raw = policy.encode_metadata({"text": "t", "_generator_version": generator.VERSION})
    meta = policy.decode_metadata(raw)
    assert meta["generator_version"] == "2.4.0"
    again = policy.decode_metadata(policy.encode_metadata({"text": "t", "_metadata": meta}))
    assert again["generator_version"] == "2.4.0"
    assert "generator_version" not in policy.decode_metadata(policy.encode_metadata({"text": "t"}))


# ── ⑤ FB 프롬프트 바이트 불변 / X 블록 교체 ─────────────────

# 기준 커밋 5c5246d의 generator로 산출한 FB 프롬프트 sha256 (소재 블록 유무 각각).
_FB_SHA = "17cf35e9d2361f0980d6062ec60cc72b70e1d68587bcd03ae83048061b03c833"
_FB_SHA_SUBJECT = "7a3f4029f6de0c6d329710679cb52952f7e43d027652ff1fc3c61c16be05e711"
_ITEMS = [
    {"id": "500", "text": "자료 정리 감사합니다", "label": "POSITIVE",
     "parent_text": "시장 정보 원문", "parent_author_id": "111", "root_author_id": "111"},
    {"id": "501", "text": "ㅋㅋㅋ 오늘 장 미쳤네요", "label": "SUPPORTIVE_NEUTRAL"},
]


def _prompt(monkeypatch, items, platform):
    prompts: list[str] = []
    monkeypatch.setattr(generator, "gemini_call",
                        lambda **k: prompts.append(k["prompt"]) or {"success": False})
    generator.generate_batch(items, platform=platform)
    return prompts[0]


@pytest.mark.parametrize("subject,expected", [(False, _FB_SHA), (True, _FB_SHA_SUBJECT)])
def test_facebook_prompt_bytes_unchanged(monkeypatch, subject, expected):
    items = [{**i, "_preserve_subject": True} if subject else i for i in _ITEMS]
    prompt = _prompt(monkeypatch, items, "facebook")
    assert hashlib.sha256(prompt.encode()).hexdigest() == expected


def test_x_subject_block_drops_restate_example(monkeypatch):
    prompt = _prompt(monkeypatch, [{**i, "_preserve_subject": True} for i in _ITEMS], "x")
    assert "짚어주셨네요" not in prompt and "소재 하나를 유지" not in prompt
    assert "상대가 건넨 말에 대한 내 반응" in prompt
    assert "필요할 때만 한 번" in prompt


# ── ⑥ strict=False / extended=False = 기준 커밋 5c5246d 동작 (FB 격리) ──
# 기대값은 기준 커밋 policy.py·gate.py를 직접 로드해 산출한 결과다.

@pytest.mark.parametrize("comment,base_intent,x_intent", [
    ("C로 골라볼게요", "ACK", "CHOICE"),
    ("B 고를래요 저축 걱정이 커서요", "WORRY", "CHOICE"),
    ("위쪽 한 표요", "ACK", "CHOICE"),
    ("3번 선택", "ACK", "CHOICE"),
    ("좋은 하루 보내세요 B요", "GREETING", "CHOICE"),
    ("저는 B할래요 ㅋㅋㅋㅋ", "CHOICE", "CHOICE"),
    ("투자 너무 어렵네요 ㅠ", "WORRY", "WORRY"),
])
def test_intent_default_matches_base_commit(comment, base_intent, x_intent):
    assert policy.intent_for(comment, "SUPPORTIVE_NEUTRAL") == base_intent
    assert policy.intent_for(comment, "SUPPORTIVE_NEUTRAL", extended=True) == x_intent


@pytest.mark.parametrize("comment,reply,base,x", [
    ("위쪽 한 표요", "위쪽을 선택하셨군요!", (True, None), (False, "GATE_RESTATE")),
    ("B가 최고", "B를 선택하셨군요!", (True, None), (False, "GATE_RESTATE")),
    ("C로 골라볼게요", "C번을 선택하셨군요! 결과가 궁금하네요.", (True, None),
     (False, "GATE_INTENT")),
    ("일단 기다리는 쪽이네요", "네, 일단 지켜보는 흐름이시군요.", (True, None),
     (False, "GATE_RESTATE")),
    ("얼마씩 담는지 한눈에 보여 편하네요", "담는 양이 보이기 편하셨다니 다행이에요.",
     (True, None), (False, "GATE_INTERPRET")),
    ("C로 할래요", "그 선택 응원해요", (True, None), (False, "GATE_INTENT")),
    ("저는 B할래요", "B 선택 응원합니다", (True, None), (False, "GATE_INTENT")),
    ("B 고를래요 저축 걱정이 커서요", "ㅎㅎ 반가워요", (False, "GATE_TONE"),
     (False, "GATE_INTENT")),
    ("저녁 맛있게 먹었어요", "맛있게 드셨군요", (True, None), (False, "GATE_RESTATE")),
])
def test_gate_default_matches_base_commit(comment, reply, base, x):
    assert gate.check_reply(reply, [], comment) == base
    assert gate.check_reply(reply, [], comment, strict=True) == x


def test_generator_extended_intent_only_on_x(monkeypatch):
    items = [{"id": "c1", "text": "C로 골라볼게요", "label": "SUPPORTIVE_NEUTRAL"}]
    x_prompt = _prompt(monkeypatch, items, "x")
    fb_prompt = _prompt(monkeypatch, items, "facebook")
    assert '"intent": "CHOICE"' in x_prompt
    assert '"intent": "ACK"' in fb_prompt
    assert set(generator.contextual_fallbacks(items[0])) == set(policy.SAFE_POOLS["ACK"])
    assert set(generator.contextual_fallbacks(items[0], extended=True)) == set(
        policy.SAFE_POOLS["CHOICE"])
    assert set(generator.contextual_fallbacks({**items[0], "_platform": "x"})) == set(
        policy.SAFE_POOLS["CHOICE"])


def test_choice_reply_interpreting_feelings_blocked_without_keyword_overlap():
    """G4 실측 재생 보완: CHOICE 답글의 해석 어미는 핵심어 겹침 없이도 차단 (X strict)."""
    from reply_engine import gate as _gate

    parent = (
        "A) 외모 좋은데 저축이 버거움\nB) 자산은 많은데 짝을 못 찾음\n"
        "어느 쪽? 댓글로! A 또는 B"
    )
    comment = "전 B 할래요. 돈 걱정 없이 짝 만나고 싶어요"
    ok, reason = _gate.check_reply(
        "짝을 원하는 마음이 느껴지네요.", [], comment, context={"parent_text": parent}, strict=True
    )
    assert (ok, reason) == (False, "GATE_RESTATE")
    ok_base, _ = _gate.check_reply(
        "짝을 원하는 마음이 느껴지네요.", [], comment, context={"parent_text": parent}
    )
    assert ok_base is True  # FB/기본 경로는 기준 커밋과 동일
    for pooled in _gate_choice_pool():
        assert _gate.check_reply(pooled, [], comment, context={"parent_text": parent},
                                 strict=True)[0] is True


def _gate_choice_pool():
    from reply_engine.generator import SAFE_POOLS
    return SAFE_POOLS["CHOICE"]


def test_choice_thanks_for_cheer_is_intent_blocked():
    """'응원' 어휘는 CHOICE strict에서 선택 평가로 본다 (의도된 규칙 고정)."""
    assert gate.check_reply("응원 감사해요!", [], "저는 B요", strict=True) == (
        False, "GATE_INTENT")


def test_interpretation_draft_replaced_by_safe_pool_not_silenced():
    """해석형 AI 초안은 strict에서 GATE_INTERPRET 탈락, 안전 풀로 대체돼 무응답이 아니다."""
    comment = "재밌게 잘 봤어요"
    draft = "재밌게 보셨다니 기쁘네요"
    assert gate.check_reply(draft, [], comment, strict=True) == (False, "GATE_INTERPRET")
    pool = generator.contextual_fallbacks({"id": "77", "text": comment, "label": "POSITIVE"},
                                          extended=True)
    chosen, source, reason = gate.choose_reply([draft], pool, [], comment, strict=True)
    assert chosen is not None and chosen in pool
    assert source == "TEMPLATE_FALLBACK" and reason == "GATE_INTERPRET"
