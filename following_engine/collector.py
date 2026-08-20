"""
following_engine/collector.py
===============================
X Home Timeline 수집 (문서 7장).

tweepy 4.17.0 확인: get_home_timeline(self, *, user_auth=True, **params)
— OAuth 1.0a User Context 기본 지원. 1콜 고정 (MAX_FETCH=100, 페이지네이션 없음).
"""

from __future__ import annotations

import logging
from typing import Any

import tweepy

from following_engine.config import MAX_FETCH

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def fetch_home_timeline(client: tweepy.Client, since_id: str | None) -> dict[str, Any]:
    """
    팔로잉 타임라인 1콜 수집 (RT/Reply 제외).

    반환: {success, tweets: [{id,text,author_id,conversation_id,created_at,metrics}],
           users: {author_id: {username, followers}}, newest_id, error}
    """
    params: dict[str, Any] = {
        "max_results": MAX_FETCH,
        "exclude": ["retweets", "replies"],
        "tweet_fields": ["created_at", "author_id", "conversation_id", "public_metrics"],
        "expansions": ["author_id"],
        "user_fields": ["username", "public_metrics"],
    }
    if since_id:
        params["since_id"] = since_id

    try:
        resp = client.get_home_timeline(user_auth=True, **params)
    except Exception as exc:
        # 요금제상 엔드포인트 미허용(403 등)도 이 경로 — 명확 로그 후 안전 종료
        logger.error(f"[FollowingCollector] home timeline 조회 실패: {exc}")
        return {"success": False, "tweets": [], "users": {}, "newest_id": None,
                "error": str(exc)}

    tweets: list[dict] = []
    if resp and resp.data:
        for t in resp.data:
            metrics = getattr(t, "public_metrics", None) or {}
            tweets.append(
                {
                    "id": str(t.id),
                    "text": t.text or "",
                    "author_id": str(t.author_id) if t.author_id else "",
                    "conversation_id": str(t.conversation_id) if t.conversation_id else "",
                    "created_at": t.created_at,
                    "metrics": {
                        "likes": int(metrics.get("like_count", 0)),
                        "replies": int(metrics.get("reply_count", 0)),
                        "reposts": int(metrics.get("retweet_count", 0)),
                        "quotes": int(metrics.get("quote_count", 0)),
                        "impressions": int(metrics.get("impression_count", 0)),
                    },
                }
            )

    users: dict[str, dict] = {}
    includes = getattr(resp, "includes", None) or {}
    for u in includes.get("users", []) or []:
        u_metrics = getattr(u, "public_metrics", None) or {}
        users[str(u.id)] = {
            "username": getattr(u, "username", "") or "",
            "followers": int(u_metrics.get("followers_count", 0)),
        }

    meta = getattr(resp, "meta", None) or {}
    newest_id = meta.get("newest_id")
    newest_id = str(newest_id) if newest_id else None

    logger.info(f"[FollowingCollector] 수집 {len(tweets)}건 (newest_id={newest_id})")
    return {"success": True, "tweets": tweets, "users": users, "newest_id": newest_id,
            "error": None}
