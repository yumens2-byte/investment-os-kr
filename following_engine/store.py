"""
following_engine/store.py
===========================
kr_following_action 영속화 (문서 16장 스키마 준용).

커서/예산은 reply_engine.store 재사용 (kr_reply_cursor account='kr_following',
kr_reply_budget 공유 — 문서 보안 6·9장 재사용 원칙).
보수적 실패 처리: 존재 확인 실패=존재 취급, 카운트 실패=상한 도달 취급.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from db.supabase_client import get_client
from reply_engine.store import kst_day_start_utc_iso

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

_T_ACTION = "kr_following_action"

CURSOR_ACCOUNT = "kr_following"


def action_exists(post_id: str) -> bool:
    try:
        result = (
            get_client().table(_T_ACTION).select("post_id")
            .eq("post_id", post_id).limit(1).execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[FStore] action_exists 실패: {exc}")
        return True  # 확인 불가 → 처리 금지


def action_exists_for_mode(post_id: str, mode: str) -> bool:
    """현재 모드에서 재처리하면 안 되는 이력인지 판단한다.

    shadow 기록은 실제 X 발행이 아니므로 이후 live 승격을 막지 않는다. 반대로 live의
    READY/FAILED 이력은 타임아웃 후 중복 발행 가능성까지 고려해 모두 재처리 금지한다.
    조회 실패 역시 기존 L1 정책대로 차단한다.
    """
    try:
        result = (
            get_client().table(_T_ACTION)
            .select("execution_mode,action_status,actual_x_post_id")
            .eq("post_id", post_id).execute()
        )
        rows = result.data or []
        return any(
            row.get("execution_mode") in {"live", mode} or row.get("actual_x_post_id")
            for row in rows
        )
    except Exception as exc:
        logger.error(f"[FStore] action_exists_for_mode 실패: {exc}")
        return True


def insert_action(record: dict) -> bool:
    try:
        # post_id가 PK인 운영 스키마에서 shadow 검수 행을 live 행으로 안전하게 승격한다.
        # live/실발행 이력은 action_exists_for_mode가 앞단에서 차단하므로 덮어쓰지 않는다.
        result = (
            get_client().table(_T_ACTION)
            .upsert(record, on_conflict="post_id")
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[FStore] insert_action 실패 ({record.get('post_id')}): {exc}")
        return False


def mark_executed(post_id: str, actual_x_post_id: str) -> bool:
    try:
        result = (
            get_client().table(_T_ACTION)
            .update({
                "action_status": "EXECUTED",
                "actual_x_post_id": actual_x_post_id,
                "executed_at": datetime.now(UTC).isoformat(),
            })
            .eq("post_id", post_id).execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[FStore] mark_executed 실패 ({post_id}): {exc}")
        return False


def mark_failed(post_id: str, error_code: str, error_message: str) -> bool:
    try:
        result = (
            get_client().table(_T_ACTION)
            .update({
                "action_status": "FAILED",
                "error_code": error_code[:40],
                "error_message": error_message[:300],
            })
            .eq("post_id", post_id).execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[FStore] mark_failed 실패 ({post_id}): {exc}")
        return False


def count_actions_today(mode: str) -> int:
    """일일 상한 카운트 — live는 EXECUTED, shadow는 would_execute 기준 (시뮬 등가성)."""
    try:
        query = (
            get_client().table(_T_ACTION)
            .select("post_id", count="exact")
            .gte("created_at", kst_day_start_utc_iso())
        )
        if mode == "live":
            query = query.eq("action_status", "EXECUTED")
        else:
            query = query.eq("would_execute", True).eq("execution_mode", mode)
        result = query.execute()
        return int(result.count or 0)
    except Exception as exc:
        logger.error(f"[FStore] count_actions_today 실패: {exc}")
        return 10**9


def author_in_cooldown(author_id: str, hours: int, mode: str) -> bool:
    """동일 작성자 쿨다운 (문서 11장). 판단 불가 시 True(차단)."""
    try:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        query = (
            get_client().table(_T_ACTION)
            .select("post_id", count="exact")
            .eq("author_id", author_id)
            .gte("created_at", cutoff)
        )
        if mode == "live":
            query = query.eq("action_status", "EXECUTED")
        else:
            query = query.eq("would_execute", True)
        result = query.execute()
        return int(result.count or 0) > 0
    except Exception as exc:
        logger.error(f"[FStore] author_in_cooldown 실패: {exc}")
        return True


def get_recent_generated_texts(limit: int = 30) -> list[str]:
    try:
        result = (
            get_client().table(_T_ACTION)
            .select("generated_text")
            .neq("generated_text", "")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [r["generated_text"] for r in (result.data or []) if r.get("generated_text")]
    except Exception as exc:
        logger.error(f"[FStore] recent_texts 실패: {exc}")
        return []
