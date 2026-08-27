"""reply_engine — C-4 (v1.2.1) 분류 스킵 건 review 기록 테스트 (2026-08-27).

커버 시나리오:
  R-01 분류 NEGATIVE 스킵 건이 review 배열에 기존 스키마로 기록된다
  R-02 PASS 건과 스킵 건이 혼재해도 각각 올바른 result로 기록된다 (shadow)
  R-03 comment_preview는 100자로 절단된다
  R-04 스킵 건의 reply_text는 None이다 (생성 미도달 명시)
  R-05 skip_reasons 집계와 review 기록 건수가 정합한다
"""

from __future__ import annotations

from datetime import UTC, datetime

import run_reply
from reply_engine import classifier, generator, x_client
from tests.test_reply_pipeline import _base_env, _MemStore, _quiet


def _mk_tweet(tid: str, text: str, author: str, conv: str) -> dict:
    return {
        "id": tid, "text": text, "author_id": author,
        "conversation_id": conv, "in_reply_to_user_id": "111",
        "created_at": datetime.now(UTC),
    }


def _run_two_labels(monkeypatch, mode: str, neg_text: str = "@edt 이런 건 별로네요"):
    """POSITIVE 1건 + NEGATIVE 1건 수집 시나리오 공통 구성."""
    _base_env(monkeypatch, mode)
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)

    tweets = [
        _mk_tweet("900", "@edt 오늘도 감사합니다!", "222", "c1"),
        _mk_tweet("901", neg_text, "333", "c2"),
    ]
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_conversation_roots", lambda _c, ids: {i: "111" for i in ids}
    )
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": tweets, "users": {},
                            "newest_id": "901", "error": None},
    )
    monkeypatch.setattr(
        classifier, "classify_batch",
        lambda items: {"900": "POSITIVE", "901": "NEGATIVE"},
    )
    monkeypatch.setattr(
        generator, "generate_batch",
        lambda items: {"900": "감사합니다, 큰 힘이 돼요"},
    )
    return run_reply.main()


# ---------------------------------------------------------------------------
# R-01 / R-04: 스킵 건 기록 + reply_text None
# ---------------------------------------------------------------------------

def test_r01_class_skip_recorded_in_review(monkeypatch):
    result = _run_two_labels(monkeypatch, "shadow")

    skipped = [e for e in result["review"] if e["result"] == "CLASS_NEGATIVE"]
    assert len(skipped) == 1
    entry = skipped[0]
    assert entry["reply_tweet_id"] == "901"
    assert entry["label"] == "NEGATIVE"
    assert entry["reply_text"] is None            # R-04: 생성 미도달 명시
    assert "별로" in entry["comment_preview"]     # 원문 검수 가능


# ---------------------------------------------------------------------------
# R-02 / R-05: PASS 건 혼재 정합
# ---------------------------------------------------------------------------

def test_r02_pass_and_skip_coexist(monkeypatch):
    result = _run_two_labels(monkeypatch, "shadow")

    results = sorted(e["result"] for e in result["review"])
    assert results == ["CLASS_NEGATIVE", "SIMULATED"]
    assert result["published"] == 1               # shadow 시뮬레이션 1건
    # R-05: 집계 정합 — 스킵 사유 1건 = review 스킵 기록 1건
    assert result["skip_reasons"].get("CLASS_NEGATIVE") == 1


# ---------------------------------------------------------------------------
# R-03: comment_preview 100자 절단
# ---------------------------------------------------------------------------

def test_r03_preview_truncated_to_100(monkeypatch):
    long_text = "@edt " + ("부정적인 얘기 " * 30)   # 100자 초과
    result = _run_two_labels(monkeypatch, "shadow", neg_text=long_text)

    entry = [e for e in result["review"] if e["result"] == "CLASS_NEGATIVE"][0]
    assert len(entry["comment_preview"]) == 100
    assert entry["comment_preview"] == long_text[:100]
