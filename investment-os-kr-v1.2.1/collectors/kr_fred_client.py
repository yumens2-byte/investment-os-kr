"""
KR Market OS — FRED 수집기
국장 관련 FRED 시리즈 수집 (KRW/USD, 미국금리, DXY, 장단기스프레드)
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import requests

from config.settings import (
    FRED_BASE_URL,
    FRED_SERIES,
    FRED_TIMEOUT_SEC,
)
from utils.retry import with_retry

VERSION = "1.1.0"

logger = logging.getLogger(__name__)


def collect_fred_data() -> dict:
    """
    FRED 전체 수집 진입점.
    반환: {
        krw_usd, krw_usd_prev, krw_usd_chg,
        us10y, us10y_prev, us10y_chg,
        us2y, dxy, dxy_prev, dxy_chg_pct,
        fedfunds, t10y2y,
        collected_at
    }
    개별 항목 수집 실패 시 None 처리 (파이프라인 중단 없음).
    """
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        logger.warning("[FRED] FRED_API_KEY 미설정 — 모든 값 None 반환")
        return _empty_result()

    logger.info("[FRED] 수집 시작")

    # 단일값 시리즈
    fedfunds = _fetch_latest(api_key, FRED_SERIES["fedfunds"])
    t10y2y = _fetch_latest(api_key, FRED_SERIES["t10y2y"])
    us2y = _fetch_latest(api_key, FRED_SERIES["us2y"])

    # 현재 + 전일 시리즈 (변화량 계산용)
    krw_cur, krw_prev = _fetch_two(api_key, FRED_SERIES["krw_usd"])
    us10y_cur, us10y_prev = _fetch_two(api_key, FRED_SERIES["us10y"])
    dxy_cur, dxy_prev = _fetch_two(api_key, FRED_SERIES["dxy"])

    # 변화량 계산
    krw_chg = _diff(krw_cur, krw_prev)
    us10y_chg = _diff(us10y_cur, us10y_prev)
    dxy_chg_pct = _pct_chg(dxy_cur, dxy_prev)

    result = {
        "krw_usd": krw_cur,
        "krw_usd_prev": krw_prev,
        "krw_usd_chg": krw_chg,
        "us10y": us10y_cur,
        "us10y_prev": us10y_prev,
        "us10y_chg": us10y_chg,
        "us2y": us2y,
        "dxy": dxy_cur,
        "dxy_prev": dxy_prev,
        "dxy_chg_pct": dxy_chg_pct,
        "fedfunds": fedfunds,
        "t10y2y": t10y2y,
        "collected_at": datetime.now(UTC).isoformat(),
    }

    collected = sum(1 for v in result.values() if v is not None and not isinstance(v, str))
    logger.info(f"[FRED] 수집 완료: {collected}개 항목")
    return result


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _fetch_latest(api_key: str, series_id: str) -> float | None:
    """최신값 1개 반환."""
    rows = _fetch_observations(api_key, series_id, limit=5)
    if rows is None:
        return None
    for row in reversed(rows):
        val = _parse_value(row.get("value"))
        if val is not None:
            return val
    return None


def _fetch_two(api_key: str, series_id: str) -> tuple[float | None, float | None]:
    """최신값 + 전일값 반환 (결측값 건너뜀)."""
    rows = _fetch_observations(api_key, series_id, limit=10)
    if rows is None:
        return None, None

    values: list[float] = []
    for row in reversed(rows):
        val = _parse_value(row.get("value"))
        if val is not None:
            values.append(val)
        if len(values) >= 2:
            break

    cur = values[0] if len(values) > 0 else None
    prev = values[1] if len(values) > 1 else None
    return cur, prev


def _fetch_observations(api_key: str, series_id: str, limit: int) -> list[dict] | None:
    """FRED API observations 엔드포인트 호출 (3회 재시도 / 2초 간격)."""
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }

    def _do_request() -> list[dict]:
        resp = requests.get(FRED_BASE_URL, params=params, timeout=FRED_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
        return data.get("observations", [])

    result = with_retry(_do_request, label=f"FRED {series_id}")
    if result is None:
        logger.warning(f"[FRED] {series_id} 수집 최종 실패 → None 처리")
    return result


def _parse_value(raw: str | None) -> float | None:
    """FRED 결측값('.' 문자) 처리."""
    if raw is None or raw == ".":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _diff(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None:
        return None
    return round(cur - prev, 4)


def _pct_chg(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / prev * 100, 4)


def _empty_result() -> dict:
    keys = [
        "krw_usd", "krw_usd_prev", "krw_usd_chg",
        "us10y", "us10y_prev", "us10y_chg",
        "us2y", "dxy", "dxy_prev", "dxy_chg_pct",
        "fedfunds", "t10y2y",
    ]
    result = {k: None for k in keys}
    result["collected_at"] = datetime.now(UTC).isoformat()
    return result
