"""reply_engine — F-1(에코 오탈락 개선)/F-2(유사도 fallback) 검증 (2026-08-20 실측 기반).

실측 사례:
  오탈락(F-1 대상): 댓글 "감사합니다^^" → 답글 "저야말로 감사합니다 🙂" GATE_ECHO 3회 차단
  참양성(유지 필수): "축하해주셔서 감사합니다♡♡" 미러링 / "반갑습니다!" 되풀이
  커버리지 손실(F-2 대상): "의견 감사합니다" 3연속 생성 → GATE_SIMILARITY 2건 탈락
"""

from __future__ import annotations

from datetime import UTC, datetime

import run_reply
from reply_engine import gate, generator, x_client
from tests.test_reply_pipeline import _base_env, _MemStore, _quiet

# ---------------------------------------------------------------------------
# F-1: 에코 게이트 — 오탈락 해소 + 참양성 유지
# ---------------------------------------------------------------------------

def test_f1_false_positive_resolved():
    """실측 오탈락: 순수 감사 댓글에 대한 모범 답글은 이제 통과해야 한다."""
    ok, reason = gate.check_reply(
        "저야말로 감사합니다 🙂", [], comment_text="@tiger18272 감사합니다^^"
    )
    assert ok, reason


def test_f1_incident_still_blocked():
    """참양성 유지: 축하 미러링(2026-08-18 사고)은 여전히 차단."""
    ok, reason = gate.check_reply(
        "축하해주셔서 진심으로 감사합니다.♡", [],
        comment_text="@tiger18272 축하해주셔서 감사합니다♡♡",
    )
    assert not ok and reason == "GATE_ECHO"


def test_f1_greeting_echo_still_blocked():
    """참양성 유지: 인사말 되풀이(2026-08-20 실측)도 여전히 차단."""
    ok, reason = gate.check_reply(
        "반갑습니다! 감사해요.", [], comment_text="@tiger18272 반갑습니다!"
    )
    assert not ok and reason == "GATE_ECHO"


def test_f1_generic_reply_to_situational_comment_passes():
    """상황어 댓글에 상투어만으로 답하면(잔여 공집합) 미러링이 아니므로 통과."""
    ok, reason = gate.check_reply(
        "감사합니다", [], comment_text="@tiger18272 축하해주셔서 감사합니다♡♡"
    )
    assert ok, reason


# ---------------------------------------------------------------------------
# F-2: 유사도 탈락 시 풀 fallback
# ---------------------------------------------------------------------------

def test_f2_pick_fallback_deterministic_and_from_pool():
    first = generator.pick_fallback("POSITIVE", "seed-x")
    second = generator.pick_fallback("POSITIVE", "seed-x")
    assert first == second
    assert first in generator._POOL_POSITIVE
    assert generator.pick_fallback("SUPPORTIVE_NEUTRAL", "seed-x") in generator._POOL_SUPPORTIVE


def test_f2_pipeline_recovers_coverage(monkeypatch):
    """실측 재현: 모델이 동일 문구를 2연속 생성 → 2건 모두 발행되어야 한다 (기존: 1건)."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    now = datetime.now(UTC)
    two_pass = [
        {"id": "600", "text": "@tiger18272 잘 봤습니다 감사합니다", "author_id": "222",
         "conversation_id": "c1", "in_reply_to_user_id": "111", "created_at": now},
        {"id": "601", "text": "@tiger18272 오늘도 감사합니다", "author_id": "333",
         "conversation_id": "c2", "in_reply_to_user_id": "111", "created_at": now},
    ]
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_conversation_roots", lambda _c, ids: {i: "111" for i in ids}
    )
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": two_pass, "users": {},
                            "newest_id": "601", "error": None},
    )
    monkeypatch.setattr(
        x_client, "post_reply",
        lambda _c, text, tid: published.append((tid, text)) or f"resp-{tid}",
    )
    same = "의견 감사합니다"  # 실측 연쇄 생성 문구
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True, "data": [
            {"id": "600", "reply": same},
            {"id": "601", "reply": same},
        ]},
    )

    result = run_reply.main()
    assert result["published"] == 2                       # 기존이면 1건 + GATE_SIMILARITY 1건
    assert "GATE_SIMILARITY" not in result["skip_reasons"]
    # 두 번째 건은 풀 문구로 대체 발행되었어야 함
    assert published[0][1] == same
    assert published[1][1] in generator._POOL_POSITIVE + generator._POOL_SUPPORTIVE
    # review에는 대체된 최종 문구가 기록됨
    assert result["review"][1]["reply_text"] == published[1][1]


def test_f2_fallback_failure_still_skips(monkeypatch):
    """fallback 문구조차 유사하면 기존대로 GATE_SIMILARITY 스킵 (재시도는 1회뿐)."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    now = datetime.now(UTC)
    two_pass = [
        {"id": "700", "text": "@tiger18272 감사합니다 잘 봤어요", "author_id": "222",
         "conversation_id": "c1", "in_reply_to_user_id": "111", "created_at": now},
        {"id": "701", "text": "@tiger18272 늘 감사합니다", "author_id": "333",
         "conversation_id": "c2", "in_reply_to_user_id": "111", "created_at": now},
    ]
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_conversation_roots", lambda _c, ids: {i: "111" for i in ids}
    )
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": two_pass, "users": {},
                            "newest_id": "701", "error": None},
    )
    monkeypatch.setattr(
        x_client, "post_reply",
        lambda _c, text, tid: published.append((tid, text)) or f"resp-{tid}",
    )
    pool_text = generator.pick_fallback("POSITIVE", "701")
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True, "data": [
            {"id": "700", "reply": pool_text},   # 첫 건이 하필 풀 문구와 동일
            {"id": "701", "reply": pool_text},   # 둘째 건 동일 → fallback도 동일 → 스킵
        ]},
    )

    result = run_reply.main()
    assert result["published"] == 1
    assert result["skip_reasons"]["GATE_SIMILARITY"] == 1


def test_versions_bumped_f_series():
    """F-1/F-2 반영 버전 확인 (지침 5)."""
    assert run_reply.VERSION == "1.4.0"
    assert gate.VERSION == "1.1.1"
    assert generator.VERSION == "1.3.0"
