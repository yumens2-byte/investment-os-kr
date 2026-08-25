"""shock_news_engine/publisher.py — X 발행 (v1.0.0). 무재시도 (이중 발행 방지 규약)."""

from __future__ import annotations

import logging

import tweepy

VERSION = "1.1.0"

logger = logging.getLogger(__name__)


def post_shock(client: tweepy.Client, comment: str, url: str) -> tuple[str | None, str | None]:
    """
    코멘트 + 기사 링크 발행. 단일 시도 — 실패 시 (None, 오류문자열). 재시도 절대 금지.
    N-1: 오류 문자열을 반환해 호출부가 spend cap(회복 불가)을 구분할 수 있게 한다.
    """
    text = f"{comment}\n{url}"
    try:
        resp = client.create_tweet(text=text, user_auth=True)
        tweet_id = str(resp.data["id"])
        logger.info(f"[SPublisher] 발행 성공: {tweet_id}")
        return tweet_id, None
    except Exception as exc:
        logger.error(f"[SPublisher] 발행 실패 (재시도 안 함): {exc}")
        return None, str(exc)
