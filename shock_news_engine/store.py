"""
shock_news_engine/store.py — kr_shock_news_history CRUD (v1.0.0)

중복 방지 계층:
  L1 article_hash PK          — 동일 기사
  L2 (slot_key, mode) UNIQUE  — 동일 슬롯 재실행 (INSERT 충돌 → 발행 금지)
  L3 제목 유사도               — gate.check_title_duplicate (최근 7일 발행분)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from db.supabase_client import get_client
from shock_news_engine.config import RECENT_TITLE_DAYS

VERSION = "1.1.0"

logger = logging.getLogger(__name__)

TABLE = "kr_shock_news_history"


def article_exists(article_hash: str) -> bool:
    try:
        result = (
            get_client().table(TABLE).select("article_hash")
            .eq("article_hash", article_hash).limit(1).execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[SStore] article_exists 실패 (보수적 True): {exc}")
        return True   # 조회 실패 시 중복 취급 → 발행 금지 (fail-safe)


def slot_taken(slot_key: str, mode: str) -> bool:
    try:
        result = (
            get_client().table(TABLE).select("slot_key")
            .eq("slot_key", slot_key).eq("mode", mode).limit(1).execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[SStore] slot_taken 실패 (보수적 True): {exc}")
        return True


def get_recent_titles(days: int = RECENT_TITLE_DAYS) -> list[str]:
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        result = (
            get_client().table(TABLE).select("title")
            .gte("created_at", cutoff).execute()
        )
        return [row["title"] for row in (result.data or []) if row.get("title")]
    except Exception as exc:
        logger.error(f"[SStore] recent_titles 실패 (빈 목록): {exc}")
        return []


def insert_history(record: dict) -> bool:
    """L1(PK)+L2(UNIQUE) 최종 방어 — 충돌 시 False (호출부 발행 금지)."""
    try:
        get_client().table(TABLE).insert(record).execute()
        return True
    except Exception as exc:
        logger.warning(f"[SStore] INSERT 실패/충돌 (발행 금지): {exc}")
        return False


def mark_posted(article_hash: str, tweet_id: str) -> None:
    try:
        (
            get_client().table(TABLE)
            .update({"posted_tweet_id": tweet_id, "posted_at": datetime.now(UTC).isoformat()})
            .eq("article_hash", article_hash).execute()
        )
    except Exception as exc:
        logger.error(f"[SStore] mark_posted 실패 (발행-기록 불일치 — 수동 확인 필요): {exc}")


def mark_failed(article_hash: str, reason: str, slot_key: str | None = None) -> None:
    """
    실패 기록. N-2 (2026-08-25): 발행 실패 시 slot_key를 실패 표식으로 이관해
    정규 슬롯을 비운다 — 발행되지 않은 슬롯이 소모되는 문제 해소 (2026-08-25 KR16 사고).
    감사 기록은 그대로 남고, 동일 기사 재발행은 L1(article_hash PK)이 계속 차단한다.
    """
    payload: dict = {"skip_reason": reason}
    if slot_key:
        stamp = datetime.now(UTC).strftime("%H%M%S")
        payload["slot_key"] = f"{slot_key}-failed-{stamp}"
    try:
        (
            get_client().table(TABLE)
            .update(payload)
            .eq("article_hash", article_hash).execute()
        )
    except Exception as exc:
        logger.error(f"[SStore] mark_failed 실패: {exc}")
