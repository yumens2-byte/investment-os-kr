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

v1.2.0 (2026-09-04, R-11): 중복 판정 기준을 '이력 존재' → '실제 발행됨'으로 변경.
  DB 점검 결과 shadow 기간 49건 + PUBLISH_FAIL 2건이 발행 없이 이력에만 남아
  L1 DUP 가드로 영구 차단됐다. response_tweet_id가 채워진 건만 중복으로 본다.
  재시도 폭주를 막기 위해 실패 건은 REPLY_RETRY_WINDOW_HOURS 창 안에서만 재대상이 된다.
  재처리 시 PK(reply_tweet_id) 충돌이 발생하므로 insert → upsert로 전환한다.

v1.1.0 (2026-08-30, R-5): 배치 조회 3종 신설 (history_exists_bulk,
  count_author_responded_today_bulk, count_conversation_responded_today_bulk).
  기존 단건 함수는 하위호환·비상 경로로 유지한다.
  사유: 후보 N건 × 3쿼리 순차 실행 구조가 MENTIONS_MAX_RESULTS 100 상향 시
  최대 300쿼리로 선형 폭증. 배치 전환으로 3쿼리 고정.
  실패 정책은 단건과 동일하게 보수적(확인 불가 = 발행 금지)으로 유지한다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from db.supabase_client import get_client
from reply_engine.config import REPLY_RETRY_WINDOW_HOURS

VERSION = "1.2.0"

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

def _retry_cutoff_iso() -> str:
    """재시도 창의 하한 시각 (R-11). 이보다 오래된 미발행 이력은 재시도하지 않는다."""
    return (datetime.now(UTC) - timedelta(hours=REPLY_RETRY_WINDOW_HOURS)).isoformat()


def history_exists(reply_tweet_id: str) -> bool:
    """
    L1 가드: 이 댓글에 이미 답글이 나갔는가 (R-11).

    '이력 존재'가 아니라 '실제 발행됨(response_tweet_id 존재)'을 중복 기준으로 본다.
    미발행 이력(shadow 시뮬레이션, PUBLISH_FAIL)은 재시도 창 안에서는 중복이 아니며,
    창을 넘기면 재시도를 종결하기 위해 중복으로 취급한다.
    """
    try:
        published = (
            get_client()
            .table(_T_HISTORY)
            .select("reply_tweet_id")
            .eq("reply_tweet_id", reply_tweet_id)
            .not_.is_("response_tweet_id", "null")
            .limit(1)
            .execute()
        )
        if published.data:
            return True

        expired = (
            get_client()
            .table(_T_HISTORY)
            .select("reply_tweet_id")
            .eq("reply_tweet_id", reply_tweet_id)
            .lt("created_at", _retry_cutoff_iso())
            .limit(1)
            .execute()
        )
        return bool(expired.data)
    except Exception as exc:
        logger.error(f"[Store] history_exists 조회 실패: {exc}")
        # 조회 실패 시 True 반환 — 확인 불가면 발행하지 않는 보수적 처리
        return True


def insert_history(record: dict) -> bool:
    """
    이력 기록. 실패 시 False.

    R-11: 발행 실패·shadow 건이 재처리 대상이 되므로 같은 reply_tweet_id로
    다시 들어올 수 있다. PK 충돌을 피하기 위해 upsert를 쓴다.
    """
    try:
        result = (
            get_client()
            .table(_T_HISTORY)
            .upsert(record, on_conflict="reply_tweet_id")
            .execute()
        )
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


# ---------------------------------------------------------------------------
# 배치 조회 (R-5) — postgrest 2.31.0 `in_(column, values)` 검증 완료
# ---------------------------------------------------------------------------

# in_()는 값을 URL 쿼리스트링에 직렬화하므로 과도한 길이를 피해 분할 조회한다.
_IN_CHUNK_SIZE = 50


def _chunks(items: list[str], size: int = _IN_CHUNK_SIZE):
    """리스트를 size 단위로 분할 (URL 길이 안전장치)."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def history_exists_bulk(reply_tweet_ids: list[str]) -> set[str]:
    """
    L1 배치 가드: 재응답하면 안 되는 reply_tweet_id 집합 (R-11).

    중복 기준은 '이력 존재'가 아니라 다음 둘 중 하나다.
      (a) 실제 발행됨 — response_tweet_id 존재
      (b) 재시도 창 경과 — 미발행이지만 REPLY_RETRY_WINDOW_HOURS를 넘긴 이력

    조회 실패 시 전건 '중복'으로 반환한다 — 확인 불가면 발행하지 않는 보수적 처리
    (단건 history_exists와 동일 정책).
    """
    ids = [i for i in dict.fromkeys(reply_tweet_ids) if i]
    if not ids:
        return set()

    cutoff = _retry_cutoff_iso()
    found: set[str] = set()
    try:
        for chunk in _chunks(ids):
            published = (
                get_client()
                .table(_T_HISTORY)
                .select("reply_tweet_id")
                .in_("reply_tweet_id", chunk)
                .not_.is_("response_tweet_id", "null")
                .execute()
            )
            expired = (
                get_client()
                .table(_T_HISTORY)
                .select("reply_tweet_id")
                .in_("reply_tweet_id", chunk)
                .lt("created_at", cutoff)
                .execute()
            )
            for result in (published, expired):
                found |= {
                    row["reply_tweet_id"]
                    for row in (result.data or [])
                    if row.get("reply_tweet_id")
                }
    except Exception as exc:
        logger.error(f"[Store] history_exists_bulk 실패 → 전건 DUP 처리: {exc}")
        return set(ids)
    return found


def _count_today_bulk(column: str, values: list[str]) -> dict[str, int]:
    """
    당일(KST) responded=True 이력을 컬럼값별로 집계.
    조회 실패 시 전건 큰 값 반환 (보수적 차단 — _count_today와 동일 정책).
    """
    keys = [v for v in dict.fromkeys(values) if v]
    if not keys:
        return {}

    counts: dict[str, int] = {}
    try:
        for chunk in _chunks(keys):
            result = (
                get_client()
                .table(_T_HISTORY)
                .select(column)
                .gte("created_at", kst_day_start_utc_iso())
                .eq("responded", True)
                .in_(column, chunk)
                .execute()
            )
            for row in (result.data or []):
                key = row.get(column)
                if key:
                    counts[key] = counts.get(key, 0) + 1
    except Exception as exc:
        logger.error(f"[Store] _count_today_bulk 실패 ({column}) → 보수 차단: {exc}")
        return {k: 10**9 for k in keys}
    return counts


def count_author_responded_today_bulk(author_ids: list[str]) -> dict[str, int]:
    """L4 배치: 저자별 당일 발행 수."""
    return _count_today_bulk("author_id", author_ids)


def count_conversation_responded_today_bulk(conversation_ids: list[str]) -> dict[str, int]:
    """L5 배치: 대화별 당일 발행 수."""
    return _count_today_bulk("conversation_id", conversation_ids)


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
