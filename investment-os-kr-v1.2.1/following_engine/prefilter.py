"""
following_engine/prefilter.py
===============================
AI 호출 전 저비용 룰 필터 (문서 9·10장). 첫 탈락 사유만 기록.

skip_reason: SELF / SKIP_RETWEET / TOO_SHORT / TOPIC_EXCLUDE / TOPIC_MISS / BLACKLIST
             / DUP / AUTHOR_COOLDOWN

K-1 (2026-08-20 실사고): collector가 exclude=["retweets"]를 전달함에도 응답에
RT형 게시물이 유입되어 첫 QUOTE 후보가 RT를 대상으로 삼음 (원작자 어트리뷰션 왜곡).
→ 원인 불문 결정적 이중 방어: "RT @" 시작 텍스트는 여기서 차단한다.
"""

from __future__ import annotations

import logging

from following_engine import store
from following_engine.config import (
    AUTHOR_COOLDOWN_HOURS,
    MIN_TEXT_LENGTH,
    TOPICS_EXCLUDE,
    TOPICS_INCLUDE,
)

VERSION = "1.0.1"

logger = logging.getLogger(__name__)


def check_static(tweet: dict, my_user_id: str, blacklist: set[str]) -> tuple[bool, str | None]:
    """DB 조회가 필요 없는 정적 검사."""
    text = (tweet.get("text") or "").strip()
    lowered = text.lower()

    if tweet.get("author_id") == my_user_id:
        return False, "SELF"
    if text.startswith("RT @"):
        return False, "SKIP_RETWEET"   # K-1: RT 인용 금지 (원작자 어트리뷰션 왜곡)
    if tweet.get("author_id") in blacklist:
        return False, "BLACKLIST"
    if len(text) < MIN_TEXT_LENGTH:
        return False, "TOO_SHORT"
    for kw in TOPICS_EXCLUDE:
        if kw.lower() in lowered:
            return False, "TOPIC_EXCLUDE"
    if not any(topic.lower() in lowered for topic in TOPICS_INCLUDE):
        return False, "TOPIC_MISS"
    return True, None


def check_db(tweet: dict, mode: str) -> tuple[bool, str | None]:
    """DB 가드 — 정적 통과 건만 호출 (조회 최소화)."""
    if store.action_exists(tweet["id"]):
        return False, "DUP"
    if store.author_in_cooldown(tweet["author_id"], AUTHOR_COOLDOWN_HOURS, mode):
        return False, "AUTHOR_COOLDOWN"
    return True, None
