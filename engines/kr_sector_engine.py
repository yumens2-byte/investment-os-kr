"""
KR Market OS — 섹터 흐름 엔진
섹터 대표 종목 등락률 기반으로 섹터 흐름 비율(합계 100%)을 계산.

계산 방식:
  1. 각 섹터 = 대표 종목 등락률 평균
  2. 섹터 강도 = 등락률 절대값
  3. 비율 = 각 섹터 강도 / 전체 강도 합계 × 100
  4. 데이터 없는 섹터는 제외 후 나머지로 100% 재계산
"""

from __future__ import annotations

import logging

from config.settings import SECTOR_TICKERS

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

# 기수집 티커(YAHOO_TICKERS)에서 섹터 계산용 등락률 키 매핑
_YAHOO_CHG_MAP: dict[str, str] = {
    "005930.KS": "samsung_chg_pct",
    "000660.KS": "skhynix_chg_pct",
}


def run_sector_engine(
    market_data: dict,
    sector_raw: dict[str, float | None],
) -> list[dict]:
    """
    섹터 흐름 비율 계산.

    Parameters
    ----------
    market_data : 기본 수집 데이터 (samsung_chg_pct, skhynix_chg_pct 포함)
    sector_raw  : {티커: 등락률(%)} — collect_sector_data() 반환값

    Returns
    -------
    list[dict] 정렬된 섹터 목록 (강도 내림차순):
        [{"name": "반도체", "chg_pct": -1.5, "ratio": 30.2, "direction": "down"}, ...]
    """
    # 전체 등락률 소스: 기수집 + 신규 수집 통합
    all_chg: dict[str, float | None] = dict(sector_raw)

    # 기수집 티커는 market_data에서 등락률 가져옴
    for ticker, key in _YAHOO_CHG_MAP.items():
        if ticker not in all_chg or all_chg[ticker] is None:
            all_chg[ticker] = market_data.get(key)

    # 섹터별 평균 등락률 계산
    sector_results: list[dict] = []

    for sector_name, tickers in SECTOR_TICKERS.items():
        chg_values = [all_chg.get(t) for t in tickers if all_chg.get(t) is not None]

        if not chg_values:
            logger.debug(f"[Sector] {sector_name}: 데이터 없음 → 제외")
            continue

        avg_chg = sum(chg_values) / len(chg_values)
        sector_results.append({
            "name": sector_name,
            "chg_pct": round(avg_chg, 2),
            "strength": abs(avg_chg),  # 비율 계산용 절대값
            "direction": "up" if avg_chg > 0 else ("down" if avg_chg < 0 else "flat"),
        })

    if not sector_results:
        logger.warning("[Sector] 계산 가능한 섹터 없음")
        return []

    # 비율 계산 (전체 강도 합산 → 100%)
    total_strength = sum(s["strength"] for s in sector_results)

    for s in sector_results:
        if total_strength > 0:
            s["ratio"] = round(s["strength"] / total_strength * 100, 1)
        else:
            s["ratio"] = round(100.0 / len(sector_results), 1)

    # 강도 내림차순 정렬
    sector_results.sort(key=lambda x: x["strength"], reverse=True)

    # 부동소수점 합산 오차 보정 (마지막 항목에서 조정)
    total_ratio = sum(s["ratio"] for s in sector_results)
    diff = round(100.0 - total_ratio, 1)
    if sector_results and diff != 0:
        sector_results[-1]["ratio"] = round(sector_results[-1]["ratio"] + diff, 1)

    logger.info(
        f"[Sector] 계산 완료: {len(sector_results)}개 섹터 | "
        + " / ".join(f"{s['name']} {s['ratio']}%" for s in sector_results)
    )
    return sector_results
