"""
KR Market OS — Yahoo Finance 수집기
KOSPI, KOSDAQ, 삼성전자, SK하이닉스, KRW/USD 환율 + 섹터 대표 종목 수집
주의: ^KS11, ^KQ11은 KST 기준 → ET 기준 실행 시 전일 종가 수집됨 (정상 동작)
User-Agent 설정 Session 사용 — 빈 JSON 응답(봇 차단) 방어
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import requests
import yfinance as yf

from config.settings import SECTOR_ALL_TICKERS, YAHOO_TICKERS
from utils.retry import with_retry

VERSION = "1.5.0"

logger = logging.getLogger(__name__)

# Yahoo Finance 봇 차단 방어용 커스텀 세션 (모듈 수준 1회 생성)
_YF_SESSION = requests.Session()
_YF_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
})

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
    yf.download() 사용 — history()보다 안정적 (빈 JSON 응답 방어).
    period='1mo' → 최근 거래일 기준 종가/전일종가 추출.
    반환: {"price": float, "chg_pct": float} 또는 None
    """

    def _do_fetch() -> dict:
        df = yf.download(
            symbol,
            period="1mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
            session=_YF_SESSION,
        )
        if df is None or df.empty:
            raise ValueError(f"{symbol} 데이터 없음")

        # yfinance 0.2.x: 단일 티커도 MultiIndex 컬럼 반환할 수 있음 → 평탄화
        if isinstance(df.columns, __import__("pandas").MultiIndex):
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
        return f if f == f else None  # NaN 체크
    except (TypeError, ValueError):
        return None
