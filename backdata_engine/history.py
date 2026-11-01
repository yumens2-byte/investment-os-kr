"""
backdata_engine/history.py — 지수 일봉 전체 이력 수집 (DN2026_0090)

yfinance period="max". 읽기 전용이라 utils.retry.with_retry 사용을 허용한다
(쓰기 무재시도 규약 STD-PUB-03은 발행 경로에만 적용).
반환: [(date, close), ...] 날짜 오름차순, 결측 종가 제거. 실패 시 None.
"""

from __future__ import annotations

import logging
from datetime import date

from utils.retry import with_retry

VERSION = "0.1.0"

logger = logging.getLogger(__name__)


def _download(symbol: str) -> list[tuple[date, float]]:
    import pandas as pd  # noqa: PLC0415
    import yfinance as yf  # noqa: PLC0415

    df = yf.download(symbol, period="max", interval="1d", progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"{symbol} 데이터 없음")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"].dropna()
    rows: list[tuple[date, float]] = []
    for idx, value in close.items():
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if f != f or f <= 0:  # NaN·비정상 값 제거
            continue
        rows.append((idx.date(), f))
    if not rows:
        raise ValueError(f"{symbol} 종가 없음")
    rows.sort(key=lambda r: r[0])
    return rows


def fetch_closes(symbol: str) -> list[tuple[date, float]] | None:
    result = with_retry(_download, symbol, label=f"Backdata {symbol}")
    if result is None:
        logger.error(f"[BHistory] {symbol} 수집 최종 실패")
        return None
    logger.info(f"[BHistory] {symbol} {len(result)}봉 ({result[0][0]} ~ {result[-1][0]})")
    return result
