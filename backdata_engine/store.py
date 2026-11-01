"""
backdata_engine/store.py — kr_backdata_post / kr_backdata_metrics CRUD (DN2026_0090)

중복 방지:
  UNIQUE(trade_date, mode)  — 거래일당 1건 (D-2 전용 상한)
  UNIQUE(text_hash, mode)   — 같은 문구 재발행 금지 (STD-PUB-06)
조회·쓰기 실패는 발행을 막되(fail-safe), 중복 충돌과 DB 오류를 구분해 반환한다(R6-04):
DB 오류는 호출부가 EXIT_RECORD_FAIL(종료코드 1·운영자 알림)로 보고한다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from db.supabase_client import get_client

VERSION = "0.1.0"

logger = logging.getLogger(__name__)

POST_TABLE = "kr_backdata_post"
METRIC_TABLE = "kr_backdata_metrics"


def is_conflict(exc: Exception) -> bool:
    """UNIQUE 위반(PostgreSQL 23505) 여부."""
    code = getattr(exc, "code", None)
    text = str(exc)
    return code == "23505" or "23505" in text or "duplicate key" in text.lower()


def slot_taken(trade_date: str, mode: str) -> bool | None:
    """True=처리됨 / False=미처리 / None=조회 실패(호출부 발행 금지 + 오류 보고)."""
    try:
        result = (
            get_client().table(POST_TABLE).select("id")
            .eq("trade_date", trade_date).eq("mode", mode).limit(1).execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[BStore] slot_taken 실패: {type(exc).__name__}: {str(exc)[:200]}")
        return None


def insert_pending(record: dict) -> tuple[int | None, str | None]:
    """pending 선기록 (STD-PUB-01). (id, None) / (None, 'conflict') / (None, 'error')."""
    try:
        result = get_client().table(POST_TABLE).insert(record).execute()
    except Exception as exc:
        if is_conflict(exc):
            logger.warning("[BStore] INSERT 충돌 (중복 — 발행 금지)")
            return None, "conflict"
        logger.error(f"[BStore] INSERT 실패 (발행 금지): {type(exc).__name__}: {str(exc)[:200]}")
        return None, "error"
    rows = result.data or []
    if rows and rows[0].get("id") is not None:
        return int(rows[0]["id"]), None
    logger.error("[BStore] INSERT 응답에 id 없음 (발행 금지)")
    return None, "error"


def update_channel(post_id: int, channel: str, status: str,
                   ext_id: str | None = None, error: str | None = None) -> bool:
    """발행 결과 기록 (발행-기록 짝, STD-PUB-02). 실패는 False — 운영자 알림 대상."""
    payload: dict = {f"{channel}_status": status}
    if ext_id:
        payload[f"{channel}_post_id"] = ext_id
        payload[f"{channel}_posted_at"] = datetime.now(UTC).isoformat()
    if error:
        payload[f"{channel}_error"] = error[:500]
    try:
        get_client().table(POST_TABLE).update(payload).eq("id", post_id).execute()
        return True
    except Exception as exc:
        logger.error(f"[BStore] update_channel 실패 (발행-기록 불일치 — 수동 확인 필요): {exc}")
        return False


def list_metric_targets(now: datetime, max_age_days: int) -> list[dict]:
    """live 게시물 중 최근 max_age_days 이내 발행분 + 기수집 체크포인트."""
    cutoff = (now - timedelta(days=max_age_days)).isoformat()
    client = get_client()
    posts = (
        client.table(POST_TABLE)
        .select("id,x_post_id,x_posted_at,fb_post_id,fb_posted_at")
        .eq("mode", "live").gte("created_at", cutoff).execute()
    ).data or []
    if not posts:
        return []
    ids = [p["id"] for p in posts]
    done = (
        client.table(METRIC_TABLE).select("post_id,channel,checkpoint")
        .in_("post_id", ids).execute()
    ).data or []
    collected: dict[int, set[tuple[str, str]]] = {}
    for row in done:
        collected.setdefault(row["post_id"], set()).add((row["channel"], row["checkpoint"]))
    for p in posts:
        p["collected"] = collected.get(p["id"], set())
    return posts


def insert_metric(row: dict) -> bool:
    try:
        get_client().table(METRIC_TABLE).insert(row).execute()
        return True
    except Exception as exc:
        logger.warning(f"[BStore] metric INSERT 실패/충돌: {exc}")
        return False
