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

import json
import logging
from datetime import UTC, datetime, timedelta

from db.supabase_client import get_client
from reply_engine.config import REPLY_MAX_AGE_HOURS, REPLY_RETRY_WINDOW_HOURS
from reply_engine.policy import BLOCKED_STATES, DEFER_REASONS, decode_metadata

VERSION = "2.0.0"

logger = logging.getLogger(__name__)

_T_HISTORY = "kr_reply_history"
_T_CURSOR = "kr_reply_cursor"
_T_BUDGET = "kr_reply_budget"
_T_BLACKLIST = "kr_reply_blacklist"
_T_LIKES = "kr_reply_likes"

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
    kst_midnight_as_utc = (
        datetime(kst_now.year, kst_now.month, kst_now.day, tzinfo=UTC) - _KST_OFFSET
    )
    return kst_midnight_as_utc.isoformat()


# ---------------------------------------------------------------------------
# history — L1 멱등성 + 상한 카운트
# ---------------------------------------------------------------------------


def _retry_cutoff_iso() -> str:
    """재시도 창의 하한 시각 (R-11). 이보다 오래된 미발행 이력은 재시도하지 않는다."""
    return (datetime.now(UTC) - timedelta(hours=REPLY_RETRY_WINDOW_HOURS)).isoformat()


def _blocked_row(row: dict) -> bool:
    if row.get("response_tweet_id") or row.get("responded"):
        return True
    if row.get("skip_reason") in BLOCKED_STATES:
        return True
    meta = decode_metadata(row.get("error_message"))
    if (
        int(meta.get("publish_attempts", 0)) >= 3
        or int(meta.get("classification_attempts", 0)) >= 3
    ):
        return True
    due = meta.get("next_attempt_at")
    try:
        if due and datetime.fromisoformat(due) > datetime.now(UTC):
            return True
    except (TypeError, ValueError):
        return True
    created = row.get("created_at")
    return bool(created and str(created) < _retry_cutoff_iso())


def history_exists(reply_tweet_id: str) -> bool:
    try:
        rows = (
            get_client()
            .table(_T_HISTORY)
            .select("*")
            .eq("reply_tweet_id", reply_tweet_id)
            .limit(1)
            .execute()
        ).data or []
        return any(_blocked_row(row) for row in rows)
    except Exception as exc:
        logger.error("[Store] history_exists failed: %s", exc)
        return True


def insert_history(record: dict) -> bool:
    """Insert new decisions or CAS-update unpublished rows; never overwrite publication."""
    try:
        client = get_client()
        rows = (
            client.table(_T_HISTORY)
            .select("*")
            .eq("reply_tweet_id", record["reply_tweet_id"])
            .limit(1)
            .execute()
        ).data or []
        if not rows:
            return bool(client.table(_T_HISTORY).insert(record).execute().data)
        previous = rows[0]
        if _blocked_row(previous):
            return False
        if record.get("mode") == "shadow" and previous.get("mode") == "live":
            return False
        old_meta = decode_metadata(previous.get("error_message"))
        new_meta = decode_metadata(record.get("error_message"))
        if old_meta and new_meta:
            for counter in ("publish_attempts", "classification_attempts"):
                new_meta[counter] = max(
                    int(old_meta.get(counter, 0)), int(new_meta.get(counter, 0))
                )
            new_meta["original_created_at"] = old_meta.get("original_created_at")
            record = {**record, "error_message": json.dumps(new_meta, ensure_ascii=False)}
        query = (
            client.table(_T_HISTORY)
            .update(record)
            .eq("reply_tweet_id", record["reply_tweet_id"])
            .eq("responded", False)
            .is_("response_tweet_id", "null")
        )
        if previous.get("skip_reason") is None:
            query = query.is_("skip_reason", "null")
        else:
            query = query.eq("skip_reason", previous["skip_reason"])
        return bool(query.execute().data)
    except Exception as exc:
        logger.error(
            "[Store] history decision save failed (%s): %s", record.get("reply_tweet_id"), exc
        )
        return False


def claim_publication(reply_tweet_id: str, metadata: str) -> str | None:
    """Atomic READY -> PUBLISHING transition; return persisted attempt metadata."""
    try:
        client = get_client()
        rows = (
            client.table(_T_HISTORY)
            .select("error_message")
            .eq("reply_tweet_id", reply_tweet_id)
            .limit(1)
            .execute()
        ).data or []
        if not rows:
            return None
        previous = decode_metadata(rows[0].get("error_message"))
        current = decode_metadata(metadata)
        attempts = int(previous.get("publish_attempts", 0))
        if attempts >= 3:
            return None
        current["publish_attempts"] = attempts + 1
        current["original_created_at"] = previous.get(
            "original_created_at", current.get("original_created_at")
        )
        metadata = json.dumps(current, ensure_ascii=False)
        result = (
            client.table(_T_HISTORY)
            .update({"skip_reason": "PUBLISHING", "error_message": metadata})
            .eq("reply_tweet_id", reply_tweet_id)
            .eq("mode", "live")
            .eq("responded", False)
            .is_("response_tweet_id", "null")
            .is_("skip_reason", "null")
            .execute()
        )
        return metadata if result.data else None
    except Exception as exc:
        logger.error("[Store] publication claim failed (%s): %s", reply_tweet_id, exc)
        return None


def mark_responded(reply_tweet_id: str, response_tweet_id: str) -> bool:
    """발행 성공 기록을 최대 3회 저장한다 (idempotent DB update)."""
    for attempt in range(1, 4):
        try:
            result = (
                get_client()
                .table(_T_HISTORY)
                .update(
                    {
                        "responded": True,
                        "response_tweet_id": response_tweet_id,
                        "skip_reason": None,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
                .eq("reply_tweet_id", reply_tweet_id)
                .execute()
            )
            if result.data:
                return True
        except Exception as exc:
            logger.error(f"[Store] mark_responded 실패 ({reply_tweet_id}, {attempt}/3): {exc}")
    return False


def update_skip_reason(
    reply_tweet_id: str,
    skip_reason: str,
    error_message: str | None = None,
) -> bool:
    """발행 단계 실패 사유 사후 기록 (PUBLISH_FAIL 등 — 감사추적용)."""
    try:
        result = (
            get_client()
            .table(_T_HISTORY)
            .update({"skip_reason": skip_reason, "error_message": error_message})
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
            query = query.or_(
                "responded.eq.true,skip_reason.in.(PUBLISHING,PUBLISH_UNKNOWN,DB_CONFIRM_FAIL)"
            )
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


def get_history_metrics(days: int = 7) -> dict:
    """최근 이력의 전환율과 주요 차단 사유를 한 번의 DB 조회로 집계한다."""
    since = (datetime.now(UTC) - timedelta(days=max(1, days))).isoformat()
    try:
        result = (
            get_client()
            .table(_T_HISTORY)
            .select("responded,skip_reason,response_tweet_id")
            .eq("mode", "live")
            .gte("created_at", since)
            .limit(5000)
            .execute()
        )
        rows = result.data or []
        responded = sum(bool(row.get("response_tweet_id") or row.get("responded")) for row in rows)
        skips: dict[str, int] = {}
        for row in rows:
            reason = row.get("skip_reason")
            if reason:
                skips[str(reason)] = skips.get(str(reason), 0) + 1
        total = len(rows)
        return {
            "available": True,
            "lookback_days": max(1, days),
            "history_rows": total,
            "responded": responded,
            "response_rate": round(responded / total, 4) if total else 0.0,
            "skip_reasons": dict(sorted(skips.items(), key=lambda item: (-item[1], item[0]))[:10]),
            "truncated": total >= 5000,
        }
    except Exception as exc:
        logger.warning(f"[Store] 운영 지표 조회 실패 (발행 계속): {exc}")
        return {
            "available": False,
            "lookback_days": max(1, days),
            "error": type(exc).__name__,
        }


def get_retryable_history(limit: int = 10) -> list[dict]:
    """Bounded oldest-first recovery of deferred decisions, never unknown publications."""
    try:
        result = (
            get_client()
            .table(_T_HISTORY)
            .select("*")
            .eq("mode", "live")
            .eq("responded", False)
            .is_("response_tweet_id", "null")
            .in_("skip_reason", sorted(DEFER_REASONS))
            .gte("created_at", _retry_cutoff_iso())
            .order("created_at")
            .limit(max(1, min(100, limit)))
            .execute()
        )
        rows = []
        now = datetime.now(UTC)
        for row in result.data or []:
            meta = decode_metadata(row.get("error_message"))
            # Legacy PUBLISH_FAIL has no proof of non-publication and is not eligible.
            if not meta or not row.get("reply_tweet_id"):
                continue
            try:
                original = datetime.fromisoformat(
                    str(meta.get("original_created_at")).replace("Z", "+00:00")
                )
                if original.tzinfo is None:
                    original = original.replace(tzinfo=UTC)
                if now - original > timedelta(hours=REPLY_MAX_AGE_HOURS):
                    continue
                if int(meta.get("publish_attempts", 0)) >= 3:
                    continue
                due = meta.get("next_attempt_at")
                if due and datetime.fromisoformat(due) > now:
                    continue
                if int(meta.get("classification_attempts", 0)) >= 3:
                    continue
            except (TypeError, ValueError):
                continue
            rows.append(row)
        return rows
    except Exception as exc:
        logger.warning("[Store] recovery lookup failed: %s", exc)
        return []


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
    ids = [i for i in dict.fromkeys(reply_tweet_ids) if i]
    found = set()
    try:
        for chunk in _chunks(ids):
            rows = (
                get_client().table(_T_HISTORY).select("*").in_("reply_tweet_id", chunk).execute()
            ).data or []
            found.update(row["reply_tweet_id"] for row in rows if _blocked_row(row))
    except Exception as exc:
        logger.error("[Store] duplicate lookup failed: %s", exc)
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
                .or_(
                    "responded.eq.true,skip_reason.in.(PUBLISHING,PUBLISH_UNKNOWN,DB_CONFIRM_FAIL)"
                )
                .in_(column, chunk)
                .execute()
            )
            for row in result.data or []:
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
        result = get_client().table(_T_CURSOR).select("*").eq("account", account).limit(1).execute()
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


def get_existing_like_ids(tweet_ids: list[str]) -> set[str]:
    """이미 처리한 좋아요 ID를 일괄 조회한다. 실패하면 전건 제외한다."""
    ids = [str(value) for value in dict.fromkeys(tweet_ids) if value]
    if not ids:
        return set()
    try:
        rows = (
            get_client()
            .table(_T_LIKES)
            .select("reply_tweet_id")
            .in_("reply_tweet_id", ids)
            .execute()
        )
        return {str(row["reply_tweet_id"]) for row in (rows.data or [])}
    except Exception as exc:
        logger.error(f"[Store] like 이력 조회 실패: {exc}")
        return set(ids)


def count_likes_today() -> int:
    """KST 기준 당일 기록된 실/시뮬레이션 좋아요 수."""
    try:
        result = (
            get_client()
            .table(_T_LIKES)
            .select("reply_tweet_id", count="exact")
            .gte("created_at", kst_day_start_utc_iso())
            .execute()
        )
        return int(result.count or 0)
    except Exception as exc:
        logger.error(f"[Store] 당일 like 카운트 실패: {exc}")
        return 10**9


def insert_like(record: dict) -> bool:
    """좋아요 처리 이력을 기록한다."""
    try:
        return bool(get_client().table(_T_LIKES).insert(record).execute().data)
    except Exception as exc:
        logger.error(f"[Store] like 기록 실패: {exc}")
        return False
