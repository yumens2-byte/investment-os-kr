"""
following_engine/executor.py
==============================
Execution Mode 라우팅 + LIVE Safety Guard (문서 18·20장).

dry_run — 로그만 (DB 무기록)
shadow  — kr_following_action 적재 (would_execute), X 쓰기 절대 없음
live    — Guard 통과한 QUOTE만 발행 (무재시도), 발행-기록 짝
"""

from __future__ import annotations

import logging
from typing import Any

import tweepy

from following_engine import store
from following_engine.config import (
    AUTHOR_COOLDOWN_HOURS,
    LIVE_ALLOWLIST,
    MAX_ACTIONS_PER_DAY,
)

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def live_safety_guard(
    candidate: dict,
    executed_today: int,
    executed_this_run: int,
    per_run_limit: int,
) -> tuple[bool, str | None]:
    """실 발행 직전 최종 가드 (문서 20장). 하나라도 실패 → SKIPPED_POLICY."""
    if candidate["action_type"] not in LIVE_ALLOWLIST:
        return False, "GUARD_ACTION_NOT_ALLOWED"
    if not (candidate.get("generated_text") or "").strip():
        return False, "GUARD_TEXT_BLANK"
    if executed_today + executed_this_run >= MAX_ACTIONS_PER_DAY:
        return False, "GUARD_DAILY_LIMIT"
    if executed_this_run >= per_run_limit:
        return False, "GUARD_RUN_LIMIT"
    if store.action_exists(candidate["post_id"]):
        return False, "GUARD_DUPLICATE"
    if store.author_in_cooldown(candidate["author_id"], AUTHOR_COOLDOWN_HOURS, "live"):
        return False, "GUARD_AUTHOR_COOLDOWN"
    return True, None


def publish_quote(client: tweepy.Client, text: str, quote_post_id: str) -> str | None:
    """QUOTE 발행 — 무재시도 (X 쓰기 규약). 성공 시 tweet_id."""
    try:
        resp = client.create_tweet(
            text=text, quote_tweet_id=quote_post_id, user_auth=True
        )
        tweet_id = str(resp.data["id"])
        logger.info(f"[FExecutor] QUOTE 발행 완료: {tweet_id} ← {quote_post_id}")
        return tweet_id
    except Exception as exc:
        logger.error(f"[FExecutor] QUOTE 발행 실패 (재시도 없음): {exc}")
        return None


def build_record(candidate: dict, mode: str, would_execute: bool,
                 status: str, skip_reason: str | None) -> dict[str, Any]:
    return {
        "post_id": candidate["post_id"],
        "source_type": "FOLLOWING_ENGAGEMENT",
        "author_id": candidate["author_id"],
        "author_username": candidate.get("author_username", ""),
        "post_text": candidate.get("post_text", "")[:1000],
        "action_type": candidate["action_type"],
        "action_status": status,
        "relevance_score": candidate.get("relevance_score"),
        "importance_score": candidate.get("importance_score"),
        "engagement_value": candidate.get("engagement_value"),
        "content_value": candidate.get("content_value"),
        "summary": candidate.get("summary", ""),
        "reason": candidate.get("reason", ""),
        "generated_text": candidate.get("generated_text", ""),
        "execution_mode": mode,
        "would_execute": would_execute,
        "skip_reason": skip_reason,
        "actual_x_post_id": None,
    }
