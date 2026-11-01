"""
backdata_engine/x_publisher.py — X 본글 발행·성과 조회 (DN2026_0090)

발행은 단일 시도(재시도 절대 금지, STD-PUB-03). 기존 publishers/x_publisher.publish_thread는
쓰기에 with_retry를 써서 재사용하지 않는다. 링크 없는 본글만 발행한다(URL 게시 단가 $0.200).
"""

from __future__ import annotations

import logging
from typing import Any

VERSION = "0.1.0"

logger = logging.getLogger(__name__)

METRIC_FIELDS = ["public_metrics", "non_public_metrics", "organic_metrics", "created_at"]


def is_unknown_exception(exc: Exception) -> bool:
    """요청이 서버에 닿았는지 알 수 없는 예외 — 타임아웃·연결 끊김·5xx (R6-02)."""
    import requests  # noqa: PLC0415
    import tweepy  # noqa: PLC0415

    if isinstance(exc, (requests.Timeout, requests.ConnectionError,
                        requests.exceptions.ChunkedEncodingError)):
        return True
    return isinstance(exc, tweepy.TwitterServerError)


def post_text(client: Any, text: str) -> tuple[str | None, str | None]:
    """
    (tweet_id | None, 오류 | None). 결과 불명(타임아웃·연결·5xx·id 없는 성공)은
    오류 문자열 앞에 'UNKNOWN:'을 붙인다 — 호출부가 failed가 아니라 unknown으로 기록한다.
    """
    try:
        resp = client.create_tweet(text=text, user_auth=True)
    except Exception as exc:
        prefix = "UNKNOWN: " if is_unknown_exception(exc) else ""
        logger.error(f"[BXPub] 발행 실패 (재시도 안 함): {prefix}{type(exc).__name__}")
        # spend cap 판정이 잘린 문자열 때문에 빗나가지 않도록 전체 문자열을 반환한다 (R6-02)
        return None, f"{prefix}{type(exc).__name__}: {exc}"
    try:
        tweet_id = str(resp.data["id"])
    except Exception:
        return None, "UNKNOWN: success response without id"
    logger.info(f"[BXPub] 발행 성공: {tweet_id}")
    return tweet_id, None


def _as_dict(obj: Any) -> dict:
    return dict(obj) if isinstance(obj, dict) else {}


def fetch_metrics(client: Any, tweet_ids: list[str]) -> tuple[dict[str, dict], str | None]:
    """본인 게시물 지표 (사용자 OAuth, 30일 이내). {id: {impressions, engagements, raw}}."""
    if not tweet_ids:
        return {}, None
    try:
        resp = client.get_tweets(ids=tweet_ids, tweet_fields=METRIC_FIELDS, user_auth=True)
    except Exception as exc:
        logger.error(f"[BXPub] 지표 조회 실패: {exc}")
        return {}, str(exc)
    out: dict[str, dict] = {}
    for tw in getattr(resp, "data", None) or []:
        pub = _as_dict(getattr(tw, "public_metrics", None))
        non_pub = _as_dict(getattr(tw, "non_public_metrics", None))
        org = _as_dict(getattr(tw, "organic_metrics", None))
        impressions = org.get("impression_count",
                              non_pub.get("impression_count", pub.get("impression_count")))
        keys = ("like_count", "reply_count", "retweet_count", "quote_count", "bookmark_count")
        parts = [pub.get(k) for k in keys if isinstance(pub.get(k), int)]
        out[str(tw.id)] = {
            "impressions": impressions if isinstance(impressions, int) else None,
            "engagements": sum(parts) if parts else None,
            "raw": {"public": pub, "non_public": non_pub, "organic": org},
        }
    return out, None
