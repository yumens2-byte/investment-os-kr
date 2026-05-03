"""
KR Market OS — Yahoo Finance 수집기
KOSPI, KOSDAQ, 삼성전자, SK하이닉스, KRW/USD 환율 수집
주의: ^KS11, ^KQ11은 KST 기준 → ET 기준 실행 시 전일 종가 수집됨 (정상 동작)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import yfinance as yf

from config.settings import YAHOO_TICKERS
from utils.retry import with_retry

VERSION = "1.1.0"

logger = logging.getLogger(__name__)


def collect_yahoo_data() -> dict:
    """
    Yahoo Finance 전체 수집 진입점.
    반환: {
        kospi, kospi_chg_pct,
        kosdaq, kosdaq_chg_pct,
        samsung, samsung_chg_pct,
        skhynix, skhynix_chg_pct,
        krw_fx,
        collected_at
    }
    개별 티커 수집 실패 시 None 처리 (파이프라인 중단 없음).
    """
    logger.info("[Yahoo] 수집 시작")

    results: dict = {}
    for key, symbol in YAHOO_TICKERS.items():
        data = _fetch_ticker(symbol)
        if key == "krw_fx":
            results["krw_fx"] = data.get("price") if data else None
        else:
            results[key] = data.get("price") if data else None
            results[f"{key}_chg_pct"] = data.get("chg_pct") if data else None

    results["collected_at"] = datetime.now(UTC).isoformat()

    collected = sum(1 for k, v in results.items() if v is not None and k != "collected_at")
    logger.info(f"[Yahoo] 수집 완료: {collected}개 항목")
    return results


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _fetch_ticker(symbol: str) -> dict | None:
    """
    단일 티커 수집 (3회 재시도 / 2초 간격).
    반환: {"price": float, "chg_pct": float} 또는 None
    """

    def _do_fetch() -> dict:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = _safe_float(getattr(info, "last_price", None))
        if price is None:
            raise ValueError(f"{symbol} 가격 없음")
        prev_close = _safe_float(getattr(info, "previous_close", None))
        chg_pct = None
        if prev_close and prev_close != 0:
            chg_pct = round((price - prev_close) / prev_close * 100, 4)
        return {"price": price, "chg_pct": chg_pct}

    result = with_retry(_do_fetch, label=f"Yahoo {symbol}")
    if result is None:
        logger.warning(f"[Yahoo] {symbol} 수집 최종 실패 → None 처리")
    return result


def _safe_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None  # NaN 체크
    except (TypeError, ValueError):
        return None
