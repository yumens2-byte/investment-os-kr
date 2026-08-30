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
  AUTHOR_CAP     — 사용자별 일일 상한 도달 (L4, DB 실적 기준)
  CONV_CAP       — 대화별 일일 상한 도달 (L5, DB 실적 기준)
  AUTHOR_CAP_RUN — 이번 실행 내 선행 승인으로 사용자 상한 도달 (L4, R-2)
  CONV_CAP_RUN   — 이번 실행 내 선행 승인으로 대화 상한 도달 (L5, R-2)

v1.1.0 (2026-08-30, R-2/R-5):
  R-2 캡 이중 계수 — check_caps_and_dup은 Step3에서 전건 일괄 실행되므로
    발행 전 시점의 DB 카운트가 항상 0이었고, 동일 배치 내 동일 저자 다건이
    전량 통과했다. 실사고(08-30 artifact): author_id=2057775414777671680에게
    한 실행에서 2건 발행 — REPLY_AUTHOR_DAILY_CAP=1 위반.
    Notion 공통지침 '캡 이중 계수 규약'(2026-08-18)에 따라
    DB 스냅샷 + in-run 카운터 이중 계수로 재구성.
  R-5 배치 조회 — 후보 N건 × 3쿼리를 CapContext 1회 구성(3쿼리)으로 대체.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
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

VERSION = "1.1.0"

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


@dataclass
class CapContext:
    """
    캡 판정 컨텍스트 (R-2/R-5).

    DB 스냅샷(당일 발행 실적) + in-run 카운터(이번 실행 내 승인 건수)를
    이중 계수한다. DB만 참조하면 발행 전 시점 카운트가 0이라
    동일 배치 내 다건이 전부 통과한다 (08-30 실사고).
    """

    existing_ids: set[str] = field(default_factory=set)
    author_today: dict[str, int] = field(default_factory=dict)
    conv_today: dict[str, int] = field(default_factory=dict)
    author_run: dict[str, int] = field(default_factory=dict)
    conv_run: dict[str, int] = field(default_factory=dict)
    bulk_ready: bool = False


def build_cap_context(tweets: list[dict]) -> CapContext:
    """
    후보 트윗 목록으로 배치 스냅샷을 1회 구성한다 (DB 3쿼리 고정, R-5).
    정적 필터 통과 건에만 호출해 조회 대상을 최소화한다.
    """
    ids = [t.get("id", "") for t in tweets]
    authors = [t.get("author_id", "") for t in tweets]
    convs = [t.get("conversation_id", "") for t in tweets]
    return CapContext(
        existing_ids=store.history_exists_bulk(ids),
        author_today=store.count_author_responded_today_bulk(authors),
        conv_today=store.count_conversation_responded_today_bulk(convs),
        bulk_ready=True,
    )


def check_and_admit(tweet: dict, ctx: CapContext | None = None) -> tuple[bool, str | None]:
    """
    L1 중복 / L4 사용자 상한 / L5 대화 상한 판정.

    ⚠️ 부수효과: 통과 시 ctx의 in-run 카운터를 증가시킨다 (승인 = 슬롯 점유).
       ctx=None이면 레거시 단건 쿼리 경로로 동작하며 in-run 계수는 없다.

    반환: (통과 여부, skip_reason | None)
    """
    reply_tweet_id = tweet.get("id", "")
    author_id = tweet.get("author_id", "")
    conversation_id = tweet.get("conversation_id", "")

    if ctx is not None and ctx.bulk_ready:
        if reply_tweet_id in ctx.existing_ids:
            return False, "DUP"
        author_base = ctx.author_today.get(author_id, 0)
        conv_base = ctx.conv_today.get(conversation_id, 0)
    else:
        if store.history_exists(reply_tweet_id):
            return False, "DUP"
        author_base = store.count_author_responded_today(author_id)
        conv_base = store.count_conversation_responded_today(conversation_id)

    author_run = ctx.author_run.get(author_id, 0) if ctx is not None else 0
    conv_run = ctx.conv_run.get(conversation_id, 0) if ctx is not None else 0

    if author_base + author_run >= REPLY_AUTHOR_DAILY_CAP:
        return False, "AUTHOR_CAP_RUN" if author_run else "AUTHOR_CAP"

    if conv_base + conv_run >= REPLY_CONV_DAILY_CAP:
        return False, "CONV_CAP_RUN" if conv_run else "CONV_CAP"

    if ctx is not None:
        ctx.author_run[author_id] = author_run + 1
        ctx.conv_run[conversation_id] = conv_run + 1

    return True, None


def check_caps_and_dup(tweet: dict) -> tuple[bool, str | None]:
    """하위호환 래퍼 (기존 호출부·테스트 보존). in-run 계수 없음."""
    return check_and_admit(tweet, None)
