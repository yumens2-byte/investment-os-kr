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

VERSION = "1.0.0"

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


def mark_failed(article_hash: str, reason: str) -> None:
    try:
        (
            get_client().table(TABLE)
            .update({"skip_reason": reason})
            .eq("article_hash", article_hash).execute()
        )
    except Exception as exc:
        logger.error(f"[SStore] mark_failed 실패: {exc}")
