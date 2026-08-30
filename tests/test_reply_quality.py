"""reply_engine — 개선 C-1/C-2/C-3 검증 (2026-08-17 dry_run 2차 품질 이슈 후속).

  C-1: 생성 프롬프트에 행동 안내 금지 명시
  C-2: 게이트 지시형 감지 (GATE_IMPERATIVE) — 실사고 문구 재현 회귀
  C-3: 리포트 review 배열 (댓글/라벨/답글/결과)
"""

from __future__ import annotations

import run_reply
from reply_engine import gate, generator
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet

# ---------------------------------------------------------------------------
# C-2: GATE_IMPERATIVE
# ---------------------------------------------------------------------------

def test_c2_incident_phrase_blocked():
    """dry_run 2차 실사고 문구 그대로 재현 — 반드시 탈락해야 한다."""
    assert gate.check_reply("네, 바로 확인해 보세요!", [])[1] == "GATE_IMPERATIVE"


def test_c2_imperative_variants_blocked():
    for text in (
        "자료를 확인하세요",
        "링크를 확인해 주세요",
        "꼭 참고하십시오",
        "확인 바랍니다",
        "직접 해보세요",
        "매일 체크해야 합니다",
    ):
        ok, reason = gate.check_reply(text, [])
        assert not ok and reason == "GATE_IMPERATIVE", text


def test_c2_greetings_still_pass():
    """인사 관용구는 통과해야 한다 (되세요/보내세요 미매칭)."""
    for text in (
        "감사합니다, 좋은 하루 되세요",
        "즐거운 주말 보내세요 🙂",
        "감사합니다, 오늘도 좋은 하루 되세요",
    ):
        ok, reason = gate.check_reply(text, [])
        assert ok, (text, reason)


def test_c2_fallback_pool_all_pass():
    """generator 내장 풀 전 문구가 게이트(지시형 포함)를 통과하는지 전수 검증."""
    for text in generator._POOL_POSITIVE + generator._POOL_SUPPORTIVE:
        ok, reason = gate.check_reply(text, [])
        assert ok, (text, reason)


# ---------------------------------------------------------------------------
# C-1: 프롬프트 강화
# ---------------------------------------------------------------------------

def test_c1_prompt_contains_prohibitions(monkeypatch):
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {"success": False, "data": None, "error": "capture"}

    monkeypatch.setattr(generator, "gemini_call", _capture)
    generator.generate_batch([{"id": "a", "text": "환율 어떻게 보세요?", "label": "POSITIVE"}])

    prompt = captured["prompt"]
    assert "절대 금지" in prompt
    assert "행동 안내" in prompt
    assert "질문이어도 답하지 말고" in prompt


# ---------------------------------------------------------------------------
# C-3: review 배열
# ---------------------------------------------------------------------------

def test_c3_review_entries_in_report(monkeypatch):
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])

    result = run_reply.main()
    assert len(result["review"]) == 1
    entry = result["review"][0]
    assert entry["reply_tweet_id"] == "100"
    assert entry["label"] == "POSITIVE"
    assert entry["comment_preview"].startswith("@edt")
    assert entry["reply_text"]
    assert entry["result"] == "SIMULATED"


def test_c3_incident_regression_full_path(monkeypatch):
    """생성기가 실사고 문구를 반환해도 게이트가 막고 review에 사유가 남아야 한다."""
    _base_env(monkeypatch, "dry_run")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])

    incident = [{"id": "100", "reply": "네, 바로 확인해 보세요!"}]
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True, "data": incident},
    )

    result = run_reply.main()
    assert result["published"] == 0
    assert result["skip_reasons"]["GATE_IMPERATIVE"] == 1
    assert result["review"][0]["result"] == "GATE_IMPERATIVE"
    assert result["review"][0]["reply_text"] == "네, 바로 확인해 보세요!"


def test_c3_live_published_result(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()
    assert result["published"] == 1
    assert result["review"][0]["result"] == "PUBLISHED"


def test_versions_bumped_c_series():
    """C 시리즈 반영 버전 확인 (지침 5)."""
    assert run_reply.VERSION == "1.4.0"
    assert gate.VERSION == "1.1.1"
    assert generator.VERSION == "1.3.0"
