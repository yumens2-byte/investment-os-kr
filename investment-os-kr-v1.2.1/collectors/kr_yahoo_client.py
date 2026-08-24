"""
KR Market OS — Yahoo Finance 수집기
KOSPI, KOSDAQ, 삼성전자, SK하이닉스, KRW/USD 환율 + 섹터 대표 종목 수집
주의: ^KS11, ^KQ11은 KST 기준 → ET 기준 실행 시 전일 종가 수집됨 (정상 동작)
yfinance 최신 버전 curl_cffi 사용 → session 파라미터 미전달 (yfinance 자체 처리)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import yfinance as yf

from config.settings import SECTOR_ALL_TICKERS, YAHOO_TICKERS
from utils.retry import with_retry

VERSION = "1.6.0"

logger = logging.getLogger(__name__)


def collect_yahoo_data() -> dict:
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
    logger.info(f"[Yahoo-Sector] 섹터 종목 수집 시작: {len(SECTOR_ALL_TICKERS)}개")
    sector_raw: dict[str, float | None] = {}
    for symbol in SECTOR_ALL_TICKERS:
        data = _fetch_ticker(symbol)
        sector_raw[symbol] = data.get("chg_pct") if data else None
    ok = sum(1 for v in sector_raw.values() if v is not None)
    logger.info(f"[Yahoo-Sector] 수집 완료: {ok}/{len(SECTOR_ALL_TICKERS)}개")
    return sector_raw


def _fetch_ticker(symbol: str) -> dict | None:
    def _do_fetch() -> dict:
        df = yf.download(
            symbol,
            period="1mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty:
            raise ValueError(f"{symbol} 데이터 없음")
        import pandas as pd  # noqa: PLC0415
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].dropna()
        if close.empty:
            raise ValueError(f"{symbol} 종가 없음")
        price = _safe_float(close.iloc[-1])
        if price is None:
            raise ValueError(f"{symbol} 종가 파싱 실패")
        prev_close = _safe_float(close.iloc[-2]) if len(close) >= 2 else None
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
        return f if f == f else None
    except (TypeError, ValueError):
        return None
