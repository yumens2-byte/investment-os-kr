"""
following_engine/prefilter.py
===============================
AI 호출 전 저비용 룰 필터 (문서 9·10장). 첫 탈락 사유만 기록.

skip_reason: SELF / TOO_SHORT / TOPIC_EXCLUDE / TOPIC_MISS / BLACKLIST
             / DUP / AUTHOR_COOLDOWN
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

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def check_static(tweet: dict, my_user_id: str, blacklist: set[str]) -> tuple[bool, str | None]:
    """DB 조회가 필요 없는 정적 검사."""
    text = (tweet.get("text") or "").strip()
    lowered = text.lower()

    if tweet.get("author_id") == my_user_id:
        return False, "SELF"
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
