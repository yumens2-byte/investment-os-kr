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

v1.4.0 (2026-09-04, R-12): referenced_tweets.id 확장으로 원글(부모 트윗) 컨텍스트 확보.
  expansions 확장은 같은 응답에 포함되므로 추가 읽기 콜 0.
  2026-08-18 「LLM 생성 컨텍스트 규약」 이행 — 댓글 단문만 보고 생성하면
  환각·주객전도가 필연이며, 실제 사고 2건(P-1 축하 미러링, R-9 베트남어 오독)의
  공통 근본 원인이었다.

v1.3.0 (2026-08-30, R-3): fetch_mentions 반환에 saturated / oldest_id 추가.
  수집 상한 포화(= 미수집 멘션 존재 가능)가 기존에는 로그·리포트 어디에도
  흔적을 남기지 않아, 커서 전진으로 인한 영구 유실을 사후 판정할 수 없었다.
  기존 반환 키는 전부 보존하므로 호출부 호환 유지.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import tweepy

from reply_engine.config import MENTIONS_MAX_RESULTS

VERSION = "1.4.0"

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


def _parent_text(tweet: Any, referenced_texts: dict[str, str]) -> str:
    """
    이 트윗이 '답글로 단' 대상(부모 트윗)의 본문을 반환한다 (R-12).

    referenced_tweets에는 replied_to / quoted / retweeted가 섞여 오므로
    replied_to만 취한다. 참조가 없거나 includes에 본문이 없으면 "" (그 경우
    generator는 원글 없이 생성하는 기존 동작으로 폴백한다).
    어떤 예외도 수집 전체를 실패시키지 않는다.
    """
    try:
        refs = getattr(tweet, "referenced_tweets", None) or []
        for ref in refs:
            ref_type = getattr(ref, "type", None) or (
                ref.get("type") if isinstance(ref, dict) else None
            )
            if ref_type != "replied_to":
                continue
            ref_id = getattr(ref, "id", None) or (
                ref.get("id") if isinstance(ref, dict) else None
            )
            if ref_id:
                return referenced_texts.get(str(ref_id), "")
    except Exception as exc:  # noqa: BLE001 - 관측 실패가 수집을 막지 않는다
        logger.warning(f"[XClient] 원글 컨텍스트 해석 실패 (무시): {exc}")
    return ""


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
                     in_reply_to_user_id, created_at, parent_text}, ... ],
          · parent_text — 이 댓글이 답글로 단 원글 본문 (R-12, 없으면 "")
        "users": { author_id: {username, created_at, followers}, ... },
        "newest_id": str | None,
        "oldest_id": str | None,     # R-3: 유실 구간 사후 추적용
        "saturated": bool,           # R-3: 수집 상한 포화 = 미수집분 존재 가능
        "error": str | None,
      }
    """
    params: dict[str, Any] = {
        "max_results": MENTIONS_MAX_RESULTS,
        "tweet_fields": [
            "author_id", "conversation_id", "in_reply_to_user_id",
            "created_at", "referenced_tweets",
        ],
        # R-12: referenced_tweets.id는 부모 트윗을 같은 응답의 includes에 실어주므로
        #       추가 읽기 콜이 발생하지 않는다.
        "expansions": ["author_id", "referenced_tweets.id"],
        "user_fields": ["username", "created_at", "public_metrics"],
    }
    if since_id:
        params["since_id"] = since_id

    try:
        resp = client.get_users_mentions(my_user_id, user_auth=True, **params)
    except Exception as exc:
        logger.error(f"[XClient] get_users_mentions 실패: {exc}")
        return {"success": False, "tweets": [], "users": {}, "newest_id": None,
                "oldest_id": None, "saturated": False, "error": str(exc)}

    includes = getattr(resp, "includes", None) or {}

    # R-12: includes.tweets = 참조된(부모) 트윗 본문. id -> text 매핑을 먼저 만든다.
    referenced_texts: dict[str, str] = {}
    for rt in includes.get("tweets", []) or []:
        referenced_texts[str(rt.id)] = getattr(rt, "text", "") or ""

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
                    "parent_text": _parent_text(t, referenced_texts),   # R-12
                }
            )

    users: dict[str, dict] = {}
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
    oldest_id = meta.get("oldest_id")
    oldest_id = str(oldest_id) if oldest_id else None

    # R-3: 수집 상한 포화 감지. 커서는 newest_id로 전진하므로
    # 이번에 못 가져온 구간은 이후 어떤 실행에서도 재조회되지 않는다.
    saturated = len(tweets) >= MENTIONS_MAX_RESULTS

    logger.info(f"[XClient] 멘션 수집 {len(tweets)}건 (newest_id={newest_id})")
    if saturated:
        logger.warning(
            f"[XClient] 수집 상한 포화 ({len(tweets)}/{MENTIONS_MAX_RESULTS}) — "
            f"미수집 멘션 존재 가능. oldest_id={oldest_id} 이전 구간은 "
            "커서 전진 후 재조회 불가 (R-3)"
        )

    return {"success": True, "tweets": tweets, "users": users, "newest_id": newest_id,
            "oldest_id": oldest_id, "saturated": saturated, "error": None}


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
