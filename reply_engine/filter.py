"""
reply_engine/filter.py
========================
수집된 멘션을 답글 후보로 거르는 필터 체인.

각 필터는 (통과 여부, skip_reason)을 반환하며, 첫 탈락 사유만 기록한다.
skip_reason 코드:
  OUT_OF_SCOPE   — 내 게시글에 대한 직접 답글이 아님 (외부 멘션 등)
  SELF           — 내 계정이 작성
  DUP            — 이미 처리 이력 존재 (L1)
  BLACKLIST      — 블랙리스트 사용자
  EXPIRED        — 작성 후 REPLY_MAX_AGE_HOURS 경과 (승인 D)
  TOO_SHORT      — 텍스트 2자 미만
  SPAM_LINK      — URL 포함
  SPAM_KEYWORD   — 스팸 키워드 포함
  SPAM_ACCOUNT   — 신생/무팔로워 계정 휴리스틱
  AUTHOR_CAP     — 사용자별 일일 상한 도달 (L4)
  CONV_CAP       — 대화별 일일 상한 도달 (L5)
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from reply_engine import store
from reply_engine.config import (
    REPLY_AUTHOR_DAILY_CAP,
    REPLY_CONV_DAILY_CAP,
    REPLY_MAX_AGE_HOURS,
    SPAM_ACCOUNT_MIN_AGE_DAYS,
    SPAM_ACCOUNT_MIN_FOLLOWERS,
    SPAM_KEYWORDS,
)

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://|t\.co/", re.IGNORECASE)


def _to_aware_utc(value) -> datetime | None:
    """tweepy datetime 또는 ISO 문자열을 aware UTC datetime으로 정규화."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def check_tweet(
    tweet: dict,
    user: dict | None,
    my_user_id: str,
    blacklist: set[str],
) -> tuple[bool, str | None]:
    """
    단건 필터 (DB 카운트 불필요한 정적 검사).
    반환: (통과 여부, skip_reason | None)
    """
    author_id = tweet.get("author_id", "")
    text = (tweet.get("text") or "").strip()

    # 범위: 내 게시글(내 트윗)에 대한 직접 답글만
    if tweet.get("in_reply_to_user_id") != my_user_id:
        return False, "OUT_OF_SCOPE"

    # 자기 자신 (스레드 셀프 답글)
    if author_id == my_user_id:
        return False, "SELF"

    # 블랙리스트
    if author_id in blacklist:
        return False, "BLACKLIST"

    # 만료 (승인 D — 24h 경과 폐기)
    created = _to_aware_utc(tweet.get("created_at"))
    if created is not None:
        age = datetime.now(UTC) - created
        if age > timedelta(hours=REPLY_MAX_AGE_HOURS):
            return False, "EXPIRED"

    # 텍스트 휴리스틱 (멘션 핸들 제거 후 실질 텍스트 기준)
    body = re.sub(r"@\w+", "", text).strip()
    if len(body) < 2:
        return False, "TOO_SHORT"
    if _URL_PATTERN.search(text):
        return False, "SPAM_LINK"
    for kw in SPAM_KEYWORDS:
        if kw in text:
            return False, "SPAM_KEYWORD"

    # 계정 휴리스틱 (user 정보가 있을 때만 — 없으면 통과)
    if user is not None:
        followers = int(user.get("followers", 0))
        acct_created = _to_aware_utc(user.get("created_at"))
        if followers < SPAM_ACCOUNT_MIN_FOLLOWERS and acct_created is not None:
            acct_age_days = (datetime.now(UTC) - acct_created).days
            if acct_age_days < SPAM_ACCOUNT_MIN_AGE_DAYS:
                return False, "SPAM_ACCOUNT"

    return True, None


def check_caps_and_dup(tweet: dict) -> tuple[bool, str | None]:
    """
    DB 조회가 필요한 가드 (L1 중복 / L4 사용자 상한 / L5 대화 상한).
    정적 필터 통과 건에만 호출해 DB 조회량을 최소화한다.
    """
    reply_tweet_id = tweet.get("id", "")
    author_id = tweet.get("author_id", "")
    conversation_id = tweet.get("conversation_id", "")

    if store.history_exists(reply_tweet_id):
        return False, "DUP"

    if store.count_author_responded_today(author_id) >= REPLY_AUTHOR_DAILY_CAP:
        return False, "AUTHOR_CAP"

    if store.count_conversation_responded_today(conversation_id) >= REPLY_CONV_DAILY_CAP:
        return False, "CONV_CAP"

    return True, None
