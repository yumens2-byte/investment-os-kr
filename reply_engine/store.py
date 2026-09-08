"""
reply_engine/store.py
=======================
Supabase 영속화 레이어.

테이블 (public 스키마):
  kr_reply_history   — 댓글 처리 이력 (reply_tweet_id PK — L1 멱등성)
  kr_reply_cursor    — 계정별 since_id + my_user_id 캐시 (L2)
  kr_reply_budget    — 일일 호출/비용 추적
  kr_reply_blacklist — 무응답 사용자 목록

모드별 DB 쓰기 정책 (설계 v1.2 확정):
  dry_run — DB 쓰기 전면 금지 (커서 미전진)
  shadow  — history/cursor/budget 쓰기 O, X 발행 X
  live    — 전부 O

일 경계: KST (UTC+9 고정, DST 없음).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from db.supabase_client import get_client

VERSION = "1.0.1"

logger = logging.getLogger(__name__)

_T_HISTORY = "kr_reply_history"
_T_CURSOR = "kr_reply_cursor"
_T_BUDGET = "kr_reply_budget"
_T_BLACKLIST = "kr_reply_blacklist"

_KST_OFFSET = timedelta(hours=9)


# ---------------------------------------------------------------------------
# 시간 헬퍼
# ---------------------------------------------------------------------------

def kst_today() -> str:
    """KST 기준 오늘 날짜 (YYYY-MM-DD)."""
    return (datetime.now(UTC) + _KST_OFFSET).date().isoformat()


def kst_day_start_utc_iso() -> str:
    """KST 오늘 00:00을 UTC ISO로 (created_at timestamptz 비교용)."""
    kst_now = datetime.now(UTC) + _KST_OFFSET
    kst_midnight_as_utc = datetime(
        kst_now.year, kst_now.month, kst_now.day, tzinfo=UTC
    ) - _KST_OFFSET
    return kst_midnight_as_utc.isoformat()


# ---------------------------------------------------------------------------
# history — L1 멱등성 + 상한 카운트
# ---------------------------------------------------------------------------

def history_exists(reply_tweet_id: str) -> bool:
    """L1 가드: 해당 댓글이 이미 처리 이력에 있는가."""
    try:
        result = (
            get_client()
            .table(_T_HISTORY)
            .select("reply_tweet_id")
            .eq("reply_tweet_id", reply_tweet_id)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[Store] history_exists 조회 실패: {exc}")
        # 조회 실패 시 True 반환 — 확인 불가면 발행하지 않는 보수적 처리
        return True


def insert_history(record: dict) -> bool:
    """이력 INSERT. PK 충돌 포함 실패 시 False."""
    try:
        result = get_client().table(_T_HISTORY).insert(record).execute()
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[Store] insert_history 실패 ({record.get('reply_tweet_id')}): {exc}")
        return False


def mark_responded(reply_tweet_id: str, response_tweet_id: str) -> bool:
    """발행 성공 직후 responded 갱신 (발행-기록 짝 규약)."""
    try:
        result = (
            get_client()
            .table(_T_HISTORY)
            .update(
                {
                    "responded": True,
                    "response_tweet_id": response_tweet_id,
                }
            )
            .eq("reply_tweet_id", reply_tweet_id)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[Store] mark_responded 실패 ({reply_tweet_id}): {exc}")
        return False


def update_skip_reason(reply_tweet_id: str, skip_reason: str) -> bool:
    """발행 단계 실패 사유 사후 기록 (PUBLISH_FAIL 등 — 감사추적용)."""
    try:
        result = (
            get_client()
            .table(_T_HISTORY)
            .update({"skip_reason": skip_reason})
            .eq("reply_tweet_id", reply_tweet_id)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[Store] update_skip_reason 실패 ({reply_tweet_id}): {exc}")
        return False


def _count_today(column: str, value: str, responded_only: bool) -> int:
    """당일(KST) 이력 카운트 공통. 조회 실패 시 큰 값 반환 (보수적 차단)."""
    try:
        query = (
            get_client()
            .table(_T_HISTORY)
            .select("reply_tweet_id", count="exact")
            .gte("created_at", kst_day_start_utc_iso())
        )
        if column:
            query = query.eq(column, value)
        if responded_only:
            query = query.eq("responded", True)
        result = query.execute()
        return int(result.count or 0)
    except Exception as exc:
        logger.error(f"[Store] 당일 카운트 조회 실패 ({column}={value}): {exc}")
        return 10**9


def count_author_responded_today(author_id: str) -> int:
    """L4: 해당 사용자에게 오늘 발행한 답글 수."""
    return _count_today("author_id", author_id, responded_only=True)


def count_conversation_responded_today(conversation_id: str) -> int:
    """L5: 해당 대화에 오늘 발행한 답글 수."""
    return _count_today("conversation_id", conversation_id, responded_only=True)


def count_responded_today() -> int:
    """일일 답글 상한 체크용 총 발행 수."""
    return _count_today("", "", responded_only=True)


def get_recent_response_texts(limit: int = 30) -> list[str]:
    """L6 유사도 가드용 최근 발행 답글 텍스트. 실패 시 빈 리스트."""
    try:
        result = (
            get_client()
            .table(_T_HISTORY)
            .select("response_text")
            .eq("responded", True)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [row["response_text"] for row in (result.data or []) if row.get("response_text")]
    except Exception as exc:
        logger.error(f"[Store] 최근 답글 조회 실패: {exc}")
        return []


# ---------------------------------------------------------------------------
# cursor — L2
# ---------------------------------------------------------------------------

def get_cursor(account: str) -> dict | None:
    """{since_id, my_user_id} 반환. 없으면 None."""
    try:
        result = (
            get_client()
            .table(_T_CURSOR)
            .select("*")
            .eq("account", account)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error(f"[Store] get_cursor 실패: {exc}")
        return None


def upsert_cursor(account: str, since_id: str, my_user_id: str) -> bool:
    try:
        result = (
            get_client()
            .table(_T_CURSOR)
            .upsert(
                {
                    "account": account,
                    "since_id": since_id,
                    "my_user_id": my_user_id,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[Store] upsert_cursor 실패: {exc}")
        return False


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------

def get_budget(budget_date: str) -> dict:
    """당일 예산 행 조회. 없으면 0으로 초기화된 dict (INSERT는 upsert_budget에서)."""
    try:
        result = (
            get_client()
            .table(_T_BUDGET)
            .select("*")
            .eq("budget_date", budget_date)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]
    except Exception as exc:
        logger.error(f"[Store] get_budget 실패: {exc}")
    return {
        "budget_date": budget_date,
        "read_calls": 0,
        "write_calls": 0,
        "gemini_calls": 0,
        "est_cost_krw": 0.0,
    }


def upsert_budget(row: dict) -> bool:
    try:
        row = dict(row)
        row["updated_at"] = datetime.now(UTC).isoformat()
        result = get_client().table(_T_BUDGET).upsert(row).execute()
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[Store] upsert_budget 실패: {exc}")
        return False


# ---------------------------------------------------------------------------
# blacklist
# ---------------------------------------------------------------------------

def get_blacklist_ids() -> set[str]:
    """블랙리스트 author_id 집합. 실패 시 빈 집합 (블랙리스트는 부가 방어층)."""
    try:
        result = get_client().table(_T_BLACKLIST).select("author_id").execute()
        return {row["author_id"] for row in (result.data or [])}
    except Exception as exc:
        logger.error(f"[Store] blacklist 조회 실패: {exc}")
        return set()
