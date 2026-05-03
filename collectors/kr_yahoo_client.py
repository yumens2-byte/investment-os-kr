"""
KR Market OS — Yahoo Finance 수집기
KOSPI, KOSDAQ, 삼성전자, SK하이닉스, KRW/USD 환율 + 섹터 대표 종목 수집
주의: ^KS11, ^KQ11은 KST 기준 → ET 기준 실행 시 전일 종가 수집됨 (정상 동작)
fast_info.last_price 방식은 period=1y download 의존으로 불안정 → history(period='5d') 사용
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import yfinance as yf

from config.settings import SECTOR_ALL_TICKERS, YAHOO_TICKERS
from utils.retry import with_retry

VERSION = "1.3.0"

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


def collect_sector_data() -> dict[str, float | None]:
    """
    섹터 대표 종목 등락률 수집.
    SECTOR_ALL_TICKERS(기수집 제외) 전체를 수집하여
    {티커: 등락률(%)} dict 반환.
    수집 실패 종목은 None 처리.
    """
    logger.info(f"[Yahoo-Sector] 섹터 종목 수집 시작: {len(SECTOR_ALL_TICKERS)}개")
    sector_raw: dict[str, float | None] = {}

    for symbol in SECTOR_ALL_TICKERS:
        data = _fetch_ticker(symbol)
        sector_raw[symbol] = data.get("chg_pct") if data else None

    ok = sum(1 for v in sector_raw.values() if v is not None)
    logger.info(f"[Yahoo-Sector] 수집 완료: {ok}/{len(SECTOR_ALL_TICKERS)}개")
    return sector_raw


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _fetch_ticker(symbol: str) -> dict | None:
    """
    단일 티커 수집 (3회 재시도 / 2초 간격).
    history(period='5d') 사용 — fast_info.last_price보다 안정적.
    반환: {"price": float, "chg_pct": float} 또는 None
    """

    def _do_fetch() -> dict:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist is None or hist.empty:
            raise ValueError(f"{symbol} 데이터 없음")

        price = _safe_float(hist["Close"].iloc[-1])
        if price is None:
            raise ValueError(f"{symbol} 종가 파싱 실패")

        prev_close = _safe_float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
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
