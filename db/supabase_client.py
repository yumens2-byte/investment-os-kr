"""
KR Market OS — Supabase 클라이언트
테이블: public.kr_daily_snapshots
(kr 스키마는 PostgREST 미노출 → public 스키마 사용)
"""

from __future__ import annotations

import logging
import os

from supabase import Client, create_client


VERSION = "1.1.0"

logger = logging.getLogger(__name__)

_client: Client | None = None

_TABLE = "kr_daily_snapshots"  # public 스키마 기본


def get_client() -> Client:
    """Supabase 클라이언트 싱글턴 반환."""
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_KEY 환경변수 미설정")
        _client = create_client(url, key)
        logger.info("[Supabase] 클라이언트 초기화 완료")
    return _client


def upsert_snapshot(data: dict) -> bool:
    """
    public.kr_daily_snapshots 테이블에 upsert.
    snapshot_date 기준 ON CONFLICT 처리.
    반환: 성공 True / 실패 False
    """
    try:
        client = get_client()
        result = client.table(_TABLE).upsert(data).execute()
        logger.info(f"[Supabase] upsert 완료: {data.get('snapshot_date')}")
        return bool(result.data)
    except Exception as exc:
        logger.error(f"[Supabase] upsert 실패: {exc}")
        return False
