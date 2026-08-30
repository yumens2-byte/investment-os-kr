"""reply_engine — R-9 외국어 댓글 정형 문구 경로 검증 (2026-08-30 라이브 사고 후속).

사고: 베트남어 댓글에 "현실적인 판단이라니, 동의합니다 ㅎㅎ 👍" 발행.
정책(마스터 확정 C안): 외국어도 무응답이 아니라 한국어로 응답하되,
  AI 생성이 아닌 의도 단정 없는 정형 문구만 사용한다.
"""

from __future__ import annotations

from itertools import combinations

import run_reply
from reply_engine import gate, generator, lang
from reply_engine.config import REPLY_MAX_LENGTH
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet
from tests.test_reply_r_patch import _override_mentions, _tweet

# 실사고 원문 (2093991421682364649)
_VN = (
    "@tiger18272 30 chuyến/ngày thì đúng là biến nơi đây thành "
    "bệ phóng không gian bận rộn nhất hành tinh"
)


# ---------------------------------------------------------------------------
# 언어 판정
# ---------------------------------------------------------------------------

def test_mentions_are_stripped_before_language_check():
    """멘션은 항상 라틴 문자 — 제거하지 않으면 한국어 댓글이 외국어로 오판된다."""
    assert lang.is_non_korean("@tiger18272 감사합니다") is False
    assert lang.latin_char_count("@tiger18272 감사합니다") == 0


def test_incident_comment_is_detected_as_non_korean():
    """실사고 베트남어 댓글이 외국어로 판정되어야 한다."""
    assert lang.is_non_korean(_VN) is True


def test_korean_comments_are_not_flagged():
    assert lang.is_non_korean("크 멋져요.!!!") is False
    assert lang.is_non_korean("정리감사합니다.") is False
    assert lang.is_non_korean("ㅋㅋㅋ") is False          # 자모 전용
    assert lang.is_non_korean("SCHD 좋네요") is False     # 한영 혼용 → 한글 있으면 통과


def test_emoji_and_symbol_only_comments_preserve_existing_behavior():
    """이모지·숫자·기호 전용 댓글은 라틴 0자 — 기존 AI 경로를 유지해야 한다."""
    assert lang.is_non_korean("👍") is False
    assert lang.is_non_korean("!!!") is False
    assert lang.is_non_korean("100") is False
    assert lang.is_non_korean("") is False
    assert lang.is_non_korean("@edt") is False   # 멘션만 남는 경우


def test_short_latin_threshold_boundary():
    """임계 3자 경계 — 2자는 한국어 취급, 3자부터 외국어."""
    assert lang.is_non_korean("ok") is False     # 2자
    assert lang.is_non_korean("wow") is True     # 3자
    assert lang.is_non_korean("Nice") is True


# ---------------------------------------------------------------------------
# 정형 문구 풀 품질
# ---------------------------------------------------------------------------

def test_non_kr_pool_passes_all_gates():
    """전 문구가 게이트를 통과하고 길이 규격을 지켜야 한다."""
    for text in generator._POOL_NON_KR:
        assert len(text) <= REPLY_MAX_LENGTH, text
        ok, reason = gate.check_reply(text, [], comment_text=_VN)
        assert ok, f"{reason}: {text}"


def test_non_kr_pool_internal_similarity_under_threshold():
    """풀 내부 유사도가 임계 미만이어야 연쇄 발행에서 탈락하지 않는다."""
    for a, b in combinations(generator._POOL_NON_KR, 2):
        assert gate.jaccard_similarity(a, b) < 0.6, f"{a} vs {b}"


def test_non_kr_pool_survives_sequential_publication():
    """풀 전량을 순차 발행해도 GATE_SIMILARITY로 탈락하지 않는다."""
    recent: list[str] = []
    for text in generator._POOL_NON_KR:
        ok, reason = gate.check_reply(text, recent, comment_text=_VN)
        assert ok, f"{reason}: {text}"
        recent.append(text)


def test_pick_non_kr_is_deterministic():
    """동일 댓글 재처리 시 동일 문구 (멱등)."""
    first = generator.pick_non_kr("2093991421682364649")
    assert first == generator.pick_non_kr("2093991421682364649")
    assert first in generator._POOL_NON_KR


# ---------------------------------------------------------------------------
# generate_batch 분기
# ---------------------------------------------------------------------------

def test_generate_batch_skips_gemini_when_all_non_korean(monkeypatch):
    """전건 외국어면 Gemini 호출 자체가 없어야 한다 (비용·오염 차단)."""
    called = []
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: called.append(1) or {"success": True, "data": []},
    )

    replies = generator.generate_batch([
        {"id": "n1", "text": _VN, "label": "SUPPORTIVE_NEUTRAL"},
    ])

    assert called == []
    assert replies["n1"] in generator._POOL_NON_KR


def test_generate_batch_excludes_non_korean_from_ai_prompt(monkeypatch):
    """혼재 배치에서 외국어 원문이 프롬프트에 섞이면 안 된다."""
    seen: dict = {}

    def _capture(**kwargs):
        seen["prompt"] = kwargs.get("prompt", "")
        return {"success": True, "data": [{"id": "k1", "reply": "감사합니다 ㅎㅎ"}]}

    monkeypatch.setattr(generator, "gemini_call", _capture)

    replies = generator.generate_batch([
        {"id": "k1", "text": "크 멋져요", "label": "POSITIVE"},
        {"id": "n1", "text": _VN, "label": "SUPPORTIVE_NEUTRAL"},
    ])

    assert "chuyến" not in seen["prompt"]
    assert "크 멋져요" in seen["prompt"]
    assert replies["k1"] == "감사합니다 ㅎㅎ"
    assert replies["n1"] in generator._POOL_NON_KR


# ---------------------------------------------------------------------------
# 파이프라인 통합
# ---------------------------------------------------------------------------

def test_pipeline_publishes_template_for_non_korean(monkeypatch):
    """실사고 재현: 외국어 댓글에 정형 문구가 발행되고 source가 표기된다."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [
        _tweet("2093991421682364649", "A1", "c1", _VN),
    ])
    # 외국어는 룰 미확정 → AI 분류 위임. 실사고와 동일 라벨로 목킹.
    monkeypatch.setattr(
        run_reply.classifier, "gemini_call",
        lambda **_k: {
            "success": True,
            "data": [{"id": "2093991421682364649", "label": "SUPPORTIVE_NEUTRAL"}],
        },
    )

    result = run_reply.main()

    entry = result["review"][0]
    assert entry["result"] == "PUBLISHED"
    assert entry["source"] == "TEMPLATE_NON_KR"
    assert entry["reply_text"] in generator._POOL_NON_KR
    assert result["non_kr_replies"] == 1


def test_pipeline_marks_korean_replies_as_ai_source(monkeypatch):
    """한국어 댓글은 기존 AI 경로를 유지한다 (회귀 방어)."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [_tweet("k1", "A1", "c1", "정리 감사합니다")])

    result = run_reply.main()

    assert result["review"][0]["source"] == "AI"
    assert result["non_kr_replies"] == 0
