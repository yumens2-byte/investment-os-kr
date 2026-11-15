"""DN2026_0104 회귀 — 사람 문체 변형층(humanize)과 호칭 되돌려주기 차단(GATE_ADDRESS_ECHO).

외부 호출(X·Gemini·DB)은 전부 대역이다 (STD-TEST-04). 시계는 tests/conftest.py가 고정한다.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import run_reply
from reply_engine import classifier, gate, generator, humanize, policy, x_client
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet

ROOT = Path(__file__).resolve().parents[1]


# ── ① humanize.apply 단위 ──────────────────────────────────────

def test_deterministic_per_key():
    text = "좋게 봐주시니 감사해요."
    assert humanize.apply(text, "1985").text == humanize.apply(text, "1985").text
    outputs = {humanize.apply(text, str(k)).text for k in range(200)}
    assert len(outputs) > 3  # 키에 따라 변형이 갈린다


def test_protected_tokens_untouched():
    texts = ["코스피 2900 회복 반가워요.", "SPY 흐름 같이 봐요.", "류님 덕분이에요.",
             "3.5% 숫자 반갑네요.", "$NVDA 소식 고마워요."]
    for text in texts:
        for k in range(300):
            out = humanize.apply(text, str(k)).text
            for token in re.findall(r"\d+(?:\.\d+)?%?|[A-Z]{2,}|\$\w+|\S*님", text):
                assert token.rstrip(".") in out, (text, out)


def test_merge_space_never_joins_protected_or_long_tokens():
    for k in range(2000):
        result = humanize.apply("오늘 장 정말 좋네요 2900 류님 고마워요", str(k))
        if "T5" in result.ops:
            assert "2900" in result.text and "류님" in result.text
            assert "오늘장" in result.text or "장정말" in result.text or "좋네요" in result.text


def test_laugh_intent_tail_uses_kk():
    tails = {humanize.apply("재밌게 보셨다니 다행이에요", str(k), intent="LAUGH").text
             for k in range(400)}
    assert any(t.endswith("ㅋㅋ") for t in tails)
    assert not any(t.endswith("ㅎㅎ") for t in tails)


def test_no_tilde_after_exclamation():
    for k in range(600):
        out = humanize.apply("주말 잘 보내세요!", str(k)).text
        assert not out.endswith("!~")


def test_rates_match_approved_draft():
    """1,000개 키 기준 변형 후 비율이 승인 초안(사람 댓글 수준)에 근접해야 한다."""
    n = 1000
    period = [humanize.apply("좋게 봐주시니 감사해요.", str(k)).text for k in range(n)]
    smile = [humanize.apply("읽어주셔서 고맙습니다 😊", str(k)).text for k in range(n)]
    comma = [humanize.apply("고마워요, 즐거운 주말 되세요", str(k)).text for k in range(n)]
    end_period = sum(t.endswith(".") for t in period) / n
    has_smile = sum("😊" in t for t in smile) / n
    has_comma = sum("," in t for t in comma) / n
    assert 0.12 <= end_period <= 0.28   # 49% → 목표 약 20% (원문 전부 마침표 기준)
    assert 0.22 <= has_smile <= 0.38    # 원문 전부 이모지 기준 → 약 30%
    assert 0.22 <= has_comma <= 0.38
    plain = [humanize.apply("좋게 봐주셔서 고마워요", str(k)) for k in range(n)]
    t5 = sum("T5" in r.ops for r in plain) / n
    t6 = sum("T6" in r.ops for r in plain) / n
    assert t5 <= 0.09 and 0.02 <= t6 <= 0.10


def test_colloquial_dictionary_only():
    seen = set()
    for k in range(3000):
        r = humanize.apply("네, 감사합니다", str(k))
        if "T6" in r.ops:
            seen.add(r.text)
    assert seen and all(("감사합니당" in s) or s.startswith("넵") for s in seen)


@pytest.mark.parametrize("raw,expected", [
    ("", "shadow"), ("off", "off"), ("LIVE", "live"), ("shadow", "shadow"), ("true", "shadow"),
])
def test_mode_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("REPLY_HUMANIZE_MODE", raw)
    assert humanize.mode() == expected


# ── ② GATE_ADDRESS_ECHO (실측 11-15 15:27) ──────────────────────

def test_address_echo_incident_blocked():
    comment = "@tiger18272 류님 오늘도 좋은 하루 보내세요🌸🌸🌸"
    assert gate.check_reply("류님도 편안한 시간 보내세요!", [], comment,
                            strict=True)[1] == "GATE_ADDRESS_ECHO"
    assert gate.check_reply("편안한 시간 보내세요!", [], comment, strict=True) == (True, None)
    # FB·기존 비엄격 경로는 불변
    assert gate.check_reply("류님도 편안한 시간 보내세요!", [], comment,
                            strict=False)[1] != "GATE_ADDRESS_ECHO"
    # 댓글에 호칭이 없으면 판정하지 않는다
    assert gate.check_reply("편안한 시간 보내세요!", [], "좋은 하루 보내세요",
                            strict=True) == (True, None)


# ── ③ 파이프라인 연결 ─────────────────────────────────────────

def _run(monkeypatch, *, mode="live", hz_mode="shadow", comment="오늘 정리 잘 봤어요 유익하네요",
         drafts=("좋게 봐주시니 감사해요.",)):
    _base_env(monkeypatch, mode)
    _quiet(monkeypatch)
    if hz_mode is not None:
        monkeypatch.setenv("REPLY_HUMANIZE_MODE", hz_mode)
    mem = _MemStore()
    mem.install(monkeypatch)
    published = []
    _install_x(monkeypatch, published)
    monkeypatch.setattr(x_client, "fetch_mentions", lambda *_a, **_k: {
        "success": True, "tweets": [{
            "id": "100", "text": comment, "author_id": "222", "conversation_id": "300",
            "in_reply_to_user_id": "111", "created_at": datetime.now(UTC)}],
        "users": {}, "newest_id": "100", "error": None})
    monkeypatch.setattr(classifier, "gemini_call", lambda **_k: {
        "success": True, "data": [{"id": "100", "label": "POSITIVE"}]})
    monkeypatch.setattr(generator, "gemini_call", lambda **_k: {
        "success": True, "data": [{"id": "100", "replies": list(drafts)}]})
    result = run_reply.main()
    entry = next(r for r in result["review"] if r["reply_tweet_id"] == "100")
    row = mem.rows.get("100") if hasattr(mem, "rows") else None
    return result, entry, published, row


def _fixed(monkeypatch, text, ops=("T1",)):
    monkeypatch.setattr(humanize, "apply",
                        lambda original, key, intent="": humanize.Result(text, list(ops)))


def test_shadow_publishes_original_and_records_preview(monkeypatch):
    _fixed(monkeypatch, "좋게 봐주시니 감사해요")
    result, entry, published, _ = _run(monkeypatch, hz_mode="shadow")
    assert entry["reply_text"] == "좋게 봐주시니 감사해요."
    assert published and published[0][1] == "좋게 봐주시니 감사해요."
    assert result["humanize_counts"] == {"preview": 1}


def test_live_publishes_humanized_text(monkeypatch):
    _fixed(monkeypatch, "좋게 봐주시니 감사해요 ㅎㅎ", ("T1", "T3"))
    result, entry, published, _ = _run(monkeypatch, hz_mode="live")
    assert entry["reply_text"] == "좋게 봐주시니 감사해요 ㅎㅎ"
    assert published[0][1] == "좋게 봐주시니 감사해요 ㅎㅎ"
    assert result["humanize_counts"] == {"applied": 1}


def test_live_reverts_when_variant_fails_gate(monkeypatch):
    _fixed(monkeypatch, "좋게 봐주시니 감사해요 #투자", ("T3",))
    result, entry, published, _ = _run(monkeypatch, hz_mode="live")
    assert entry["reply_text"] == "좋게 봐주시니 감사해요."
    assert result["humanize_counts"] == {"reverted": 1}


def test_off_skips_humanize(monkeypatch):
    calls = []
    monkeypatch.setattr(humanize, "apply", lambda *a, **k: calls.append(a))
    result, entry, _, _ = _run(monkeypatch, hz_mode="off")
    assert not calls and "humanize_counts" not in result
    assert entry["reply_text"] == "좋게 봐주시니 감사해요."


def test_metadata_records_humanize(monkeypatch):
    _fixed(monkeypatch, "좋게 봐주시니 감사해요 ㅎㅎ", ("T1", "T3"))
    captured = []
    real = policy.encode_metadata

    def spy(tweet, **kw):
        out = real(tweet, **kw)
        captured.append(json.loads(out))
        return out

    monkeypatch.setattr(run_reply, "encode_metadata", spy)
    _run(monkeypatch, hz_mode="live")
    meta = next(m for m in captured if "humanize" in m)["humanize"]
    assert meta["applied"] is True and meta["ops"] == ["T1", "T3"]
    assert meta["original"] == "좋게 봐주시니 감사해요." and meta["version"] == humanize.VERSION


def test_pipeline_address_echo_falls_to_alt(monkeypatch):
    _, entry, _, _ = _run(
        monkeypatch, hz_mode="off", comment="류님 오늘도 좋은 하루 보내세요🌸🌸🌸",
        drafts=("류님도 편안한 시간 보내세요!", "따뜻한 인사 고마워요 🙂"))
    assert entry["reply_text"] == "따뜻한 인사 고마워요 🙂"
    assert entry["draft_gate_reason"] == "GATE_ADDRESS_ECHO"


def test_workflow_passes_humanize_mode():
    text = (ROOT / ".github/workflows/reply_engine.yml").read_text(encoding="utf-8")
    assert "REPLY_HUMANIZE_MODE:    ${{ vars.REPLY_HUMANIZE_MODE }}" in text


def test_versions():
    assert humanize.VERSION == "1.0.0"
    assert gate.VERSION == "2.5.0"
    assert policy.POLICY_VERSION == "2.5.0"
    assert run_reply.VERSION == "2.6.0"
