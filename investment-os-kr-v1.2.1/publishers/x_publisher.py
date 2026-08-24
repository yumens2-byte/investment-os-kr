"""
KR Market OS — X (Twitter) 발행기
tweepy v4 OAuth 1.0a 기반 스레드 발행
"""

from __future__ import annotations

import logging
import os
import time

import tweepy

from utils.retry import with_retry

VERSION = "1.1.0"

logger = logging.getLogger(__name__)

_THREAD_DELAY_SEC: float = 1.5  # 트윗 간 딜레이


def publish_thread(tweets: list[str], dry_run: bool = False) -> list[str]:
    """
    트윗 스레드 발행 (단건 발행 시 3회 재시도 / 2초 간격).
    tweets[0] → 첫 트윗
    tweets[1:] → 각각 reply_to 스레드 연결
    반환: tweet_id 리스트 (발행 성공 건)
    dry_run=True 이면 발행 없이 로그만 출력.
    """
    if not tweets:
        logger.warning("[X] 발행할 트윗 없음")
        return []

    if dry_run:
        for i, t in enumerate(tweets):
            logger.info(f"[X DRY_RUN] tweet[{i}]:\n{t}\n---")
        return ["dry_run_id"] * len(tweets)

    client = _get_client()
    if client is None:
        return []

    tweet_ids: list[str] = []
    reply_to: str | None = None

    for i, text in enumerate(tweets):

        def _do_tweet(
            _text: str = text,
            _reply_to: str | None = reply_to,
        ) -> str:
            if _reply_to:
                resp = client.create_tweet(text=_text, in_reply_to_tweet_id=_reply_to)
            else:
                resp = client.create_tweet(text=_text)
            return str(resp.data["id"])

        tweet_id = with_retry(_do_tweet, label=f"X tweet[{i}]")

        if tweet_id is None:
            logger.error(f"[X] tweet[{i}] 최종 실패 — 스레드 연결 중단")
            break

        tweet_ids.append(tweet_id)
        reply_to = tweet_id
        logger.info(f"[X] tweet[{i}] 발행 완료: {tweet_id}")

        if i < len(tweets) - 1:
            time.sleep(_THREAD_DELAY_SEC)

    return tweet_ids


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _get_client() -> tweepy.Client | None:
    """tweepy.Client 생성. 환경변수 미설정 시 None 반환."""
    api_key = os.environ.get("X_API_KEY", "")
    api_secret = os.environ.get("X_API_SECRET", "")
    access_token = os.environ.get("X_ACCESS_TOKEN", "")
    access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

    if not all([api_key, api_secret, access_token, access_secret]):
        logger.error("[X] API 자격증명 환경변수 미설정")
        return None

    return tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )
