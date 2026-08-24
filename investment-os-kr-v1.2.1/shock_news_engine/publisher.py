"""shock_news_engine/publisher.py — X 발행 (v1.0.0). 무재시도 (이중 발행 방지 규약)."""

from __future__ import annotations

import logging

import tweepy

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def post_shock(client: tweepy.Client, comment: str, url: str) -> str | None:
    """코멘트 + 기사 링크 발행. 단일 시도 — 실패 시 None (재시도 절대 금지)."""
    text = f"{comment}\n{url}"
    try:
        resp = client.create_tweet(text=text, user_auth=True)
        tweet_id = str(resp.data["id"])
        logger.info(f"[SPublisher] 발행 성공: {tweet_id}")
        return tweet_id
    except Exception as exc:
        logger.error(f"[SPublisher] 발행 실패 (재시도 안 함): {exc}")
        return None
