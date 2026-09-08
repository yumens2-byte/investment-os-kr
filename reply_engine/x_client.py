"""
reply_engine/x_client.py
==========================
X API v2 클라이언트 래퍼 (OAuth 1.0a User Context).

- 클라이언트 빌더는 publishers/x_publisher.py의 _get_client() 패턴 복제
  (레거시 무접촉 원칙 — private 함수 import 회피)
- 수집: GET /2/users/:id/mentions (tweepy Client.get_users_mentions, user_auth=True)
- 발행: POST /2/tweets (in_reply_to_tweet_id) — 재시도 없음 (중복 답글 방지 우선, 승인 E)

tweepy 4.17.0 시그니처 확인 완료:
  get_users_mentions(self, id, *, user_auth=False, **params)
  get_me(self, *, user_auth=True, **params)
  create_tweet(..., in_reply_to_tweet_id=None, user_auth=True)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import tweepy

from reply_engine.config import MENTIONS_MAX_RESULTS

VERSION = "1.2.0"

logger = logging.getLogger(__name__)


def get_x_client() -> tweepy.Client | None:
    """tweepy.Client 생성. 환경변수 미설정 시 None (x_publisher 패턴 동일)."""
    api_key = os.environ.get("X_API_KEY", "")
    api_secret = os.environ.get("X_API_SECRET", "")
    access_token = os.environ.get("X_ACCESS_TOKEN", "")
    access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

    if not all([api_key, api_secret, access_token, access_secret]):
        logger.error("[XClient] API 자격증명 환경변수 미설정")
        return None

    return tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )


def fetch_my_user_id(client: tweepy.Client) -> str | None:
    """내 user_id 조회 (get_me, OAuth 1.0a). 실패 시 None."""
    try:
        resp = client.get_me(user_auth=True)
        if resp and resp.data:
            user_id = str(resp.data.id)
            logger.info(f"[XClient] get_me 성공: user_id={user_id}")
            return user_id
        logger.error("[XClient] get_me 응답 데이터 없음")
        return None
    except Exception as exc:
        logger.error(f"[XClient] get_me 실패: {exc}")
        return None


def fetch_mentions(
    client: tweepy.Client,
    my_user_id: str,
    since_id: str | None,
) -> dict[str, Any]:
    """
    멘션 타임라인 1콜 수집 (페이지네이션 없음 — 초과분은 다음 실행 커서 처리).

    반환:
      {
        "success": bool,
        "tweets": [ {id, text, author_id, conversation_id,
                     in_reply_to_user_id, created_at}, ... ],
        "users": { author_id: {username, created_at, followers}, ... },
        "newest_id": str | None,
        "error": str | None,
      }
    """
    params: dict[str, Any] = {
        "max_results": MENTIONS_MAX_RESULTS,
        "tweet_fields": ["author_id", "conversation_id", "in_reply_to_user_id", "created_at"],
        "expansions": ["author_id"],
        "user_fields": ["username", "created_at", "public_metrics"],
    }
    if since_id:
        params["since_id"] = since_id

    try:
        resp = client.get_users_mentions(my_user_id, user_auth=True, **params)
    except Exception as exc:
        logger.error(f"[XClient] get_users_mentions 실패: {exc}")
        return {"success": False, "tweets": [], "users": {}, "newest_id": None, "error": str(exc)}

    tweets: list[dict] = []
    if resp and resp.data:
        for t in resp.data:
            tweets.append(
                {
                    "id": str(t.id),
                    "text": t.text or "",
                    "author_id": str(t.author_id) if t.author_id else "",
                    "conversation_id": str(t.conversation_id) if t.conversation_id else "",
                    "in_reply_to_user_id": (
                        str(t.in_reply_to_user_id) if t.in_reply_to_user_id else ""
                    ),
                    "created_at": t.created_at,  # datetime | None
                }
            )

    users: dict[str, dict] = {}
    includes = getattr(resp, "includes", None) or {}
    for u in includes.get("users", []) or []:
        metrics = getattr(u, "public_metrics", None) or {}
        users[str(u.id)] = {
            "username": getattr(u, "username", "") or "",
            "created_at": getattr(u, "created_at", None),
            "followers": int(metrics.get("followers_count", 0)),
        }

    meta = getattr(resp, "meta", None) or {}
    newest_id = meta.get("newest_id")
    newest_id = str(newest_id) if newest_id else None

    logger.info(f"[XClient] 멘션 수집 {len(tweets)}건 (newest_id={newest_id})")
    return {"success": True, "tweets": tweets, "users": users, "newest_id": newest_id,
            "error": None}


def post_reply(client: tweepy.Client, text: str, in_reply_to_tweet_id: str) -> str | None:
    """
    답글 1건 발행. 재시도 없음 (승인 E — 타임아웃 후 재시도 시 이중 답글 리스크).
    성공 시 tweet_id, 실패 시 None.
    """
    try:
        resp = client.create_tweet(
            text=text,
            in_reply_to_tweet_id=in_reply_to_tweet_id,
            user_auth=True,
        )
        tweet_id = str(resp.data["id"])
        logger.info(f"[XClient] 답글 발행 완료: {tweet_id} → reply_to={in_reply_to_tweet_id}")
        return tweet_id
    except Exception as exc:
        logger.error(f"[XClient] 답글 발행 실패 (재시도 없음): {exc}")
        return None


def fetch_conversation_roots(
    client: tweepy.Client,
    conversation_ids: list[str],
) -> dict[str, str] | None:
    """
    대화 루트 트윗들의 author_id 배치 조회 (P-1 스코프 검증, 1콜).

    conversation_id == 루트 트윗 ID 이므로 GET /2/tweets?ids=... 로 소유자 확인 가능.
    tweepy 4.17.0 확인: get_tweets(self, ids, *, user_auth=False, **params)

    반환:
      {conversation_id: root_author_id} — 조회된 것만 포함 (삭제/보호계정은 누락됨)
      None — API 호출 자체 실패 (호출부에서 전량 보수적 스킵 처리)
    """
    ids = [str(i) for i in dict.fromkeys(conversation_ids) if i]
    if not ids:
        return {}

    try:
        resp = client.get_tweets(ids=ids, user_auth=True, tweet_fields=["author_id"])
    except Exception as exc:
        logger.error(f"[XClient] 대화 루트 조회 실패: {exc}")
        return None

    roots: dict[str, str] = {}
    if resp and resp.data:
        for t in resp.data:
            roots[str(t.id)] = str(t.author_id) if t.author_id else ""

    logger.info(f"[XClient] 대화 루트 조회 {len(ids)}건 요청 → {len(roots)}건 확인")
    return roots


# ── N-1 (2026-08-25): X 플랫폼 회복 불가 오류 판별 ──────────────
# 2026-08-25 장애: 월간 spend cap 도달로 3개 엔진 X 호출이 동시 403.
# 일반 실패와 구분해 종료 코드를 분리한다 (진단 지연 방지 — 재시도로 풀리지 않음).
_SPEND_CAP_MARKERS = ("spend cap", "monthly spend", "usage cap")


def is_spend_cap_error(error_text: str | None) -> bool:
    """X API 응답 문자열이 월간 지출 상한(회복 불가) 사유인지 판별."""
    if not error_text:
        return False
    lowered = str(error_text).lower()
    return any(marker in lowered for marker in _SPEND_CAP_MARKERS)
