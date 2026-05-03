"""
KR Market OS — 국장 분석 엔진
환율 레짐 / 외인 압박 / 금리 부담 / 장단기 스프레드 → 종합 시그널 판정
"""

from __future__ import annotations

import logging

from config.settings import (
    DXY_CHG_THRESHOLD,
    KRW_CHG_THRESHOLD,
    KRW_STRONG_THRESHOLD,
    KRW_WEAK_THRESHOLD,
    T10Y2Y_FLAT_THRESHOLD,
    T10Y2Y_INVERTED_THRESHOLD,
    US10Y_HIGH_THRESHOLD,
    US10Y_LOW_THRESHOLD,
)

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def run_kr_engine(market_data: dict) -> dict:
    """
    국장 분석 엔진 진입점.
    입력: collect_fred_data() + collect_yahoo_data() 조립 결과
    반환: {
        krw_regime:       STRONG / NEUTRAL / WEAK
        foreign_pressure: HIGH / MEDIUM / LOW
        rate_burden:      HIGH / MEDIUM / LOW
        yield_spread:     INVERTED / FLAT / NORMAL
        market_signal:    위험 / 주의 / 중립 / 우호
        signal_score:     int (HIGH=+1, LOW=-1, MEDIUM=0 합산)
    }
    """
    krw_usd = market_data.get("krw_usd")
    dxy_chg_pct = market_data.get("dxy_chg_pct")
    krw_usd_chg = market_data.get("krw_usd_chg")
    us10y = market_data.get("us10y")
    us10y_chg = market_data.get("us10y_chg")
    t10y2y = market_data.get("t10y2y")

    krw_regime = _score_krw_regime(krw_usd)
    foreign_pressure = _score_foreign_pressure(dxy_chg_pct, krw_usd_chg)
    rate_burden = _score_rate_burden(us10y, us10y_chg)
    yield_spread = _score_yield_spread(t10y2y)

    score = _calc_score(foreign_pressure, rate_burden, krw_regime)
    market_signal = _calc_signal(score)

    result = {
        "krw_regime": krw_regime,
        "foreign_pressure": foreign_pressure,
        "rate_burden": rate_burden,
        "yield_spread": yield_spread,
        "market_signal": market_signal,
        "signal_score": score,
    }

    logger.info(
        f"[Engine] 판정: {market_signal} "
        f"(KRW:{krw_regime} FP:{foreign_pressure} RB:{rate_burden} YS:{yield_spread})"
    )
    return result


# ---------------------------------------------------------------------------
# 시그널 판정 함수
# ---------------------------------------------------------------------------

def _score_krw_regime(krw_usd: float | None) -> str:
    """
    원화 환율 레짐 판정.
    STRONG  : KRW/USD < 1320  (원화 강세 → 외인 우호)
    NEUTRAL : 1320 ≤ KRW/USD < 1380
    WEAK    : KRW/USD ≥ 1380  (원화 약세 → 외인 부담)
    None 입력 → NEUTRAL
    """
    if krw_usd is None:
        return "NEUTRAL"
    if krw_usd < KRW_STRONG_THRESHOLD:
        return "STRONG"
    if krw_usd >= KRW_WEAK_THRESHOLD:
        return "WEAK"
    return "NEUTRAL"


def _score_foreign_pressure(dxy_chg_pct: float | None, krw_usd_chg: float | None) -> str:
    """
    외인 압박 지수 판정.
    HIGH   : DXY ≥ +0.3% AND KRW/USD ≥ +5원 동시 (달러 강세 + 원화 약세)
    LOW    : DXY ≤ -0.3% AND KRW/USD ≤ -5원 동시 (달러 약세 + 원화 강세)
    MEDIUM : 나머지
    단일 조건 충족만으로는 HIGH/LOW 미판정 → 복합 조건 필수
    """
    if dxy_chg_pct is None or krw_usd_chg is None:
        return "MEDIUM"

    if dxy_chg_pct >= DXY_CHG_THRESHOLD and krw_usd_chg >= KRW_CHG_THRESHOLD:
        return "HIGH"
    if dxy_chg_pct <= -DXY_CHG_THRESHOLD and krw_usd_chg <= -KRW_CHG_THRESHOLD:
        return "LOW"
    return "MEDIUM"


def _score_rate_burden(us10y: float | None, us10y_chg: float | None) -> str:
    """
    금리 부담 지수 판정.
    HIGH   : 미국 10년물 ≥ 4.5% AND 상승 중 (EM 자금이탈 압력)
    LOW    : 미국 10년물 < 4.0%
    MEDIUM : 나머지
    """
    if us10y is None:
        return "MEDIUM"
    if us10y >= US10Y_HIGH_THRESHOLD and (us10y_chg is not None and us10y_chg > 0):
        return "HIGH"
    if us10y < US10Y_LOW_THRESHOLD:
        return "LOW"
    return "MEDIUM"


def _score_yield_spread(t10y2y: float | None) -> str:
    """
    장단기 스프레드 판정.
    INVERTED : T10Y2Y < -0.3  (경기침체 선행 신호)
    FLAT     : -0.3 ≤ T10Y2Y < 0.3
    NORMAL   : T10Y2Y ≥ 0.3
    """
    if t10y2y is None:
        return "FLAT"
    if t10y2y < T10Y2Y_INVERTED_THRESHOLD:
        return "INVERTED"
    if t10y2y < T10Y2Y_FLAT_THRESHOLD:
        return "FLAT"
    return "NORMAL"


# ---------------------------------------------------------------------------
# 종합 판정
# ---------------------------------------------------------------------------

def _calc_score(foreign_pressure: str, rate_burden: str, krw_regime: str) -> int:
    """
    3개 시그널 점수 합산.
    HIGH=+1, LOW=-1, MEDIUM/NEUTRAL=0
    STRONG(환율)=-1(우호), WEAK(환율)=+1(부담)
    """
    score = 0
    score += _signal_to_score(foreign_pressure)
    score += _signal_to_score(rate_burden)
    # 환율 레짐: WEAK=부담(+1), STRONG=우호(-1)
    if krw_regime == "WEAK":
        score += 1
    elif krw_regime == "STRONG":
        score -= 1
    return score


def _signal_to_score(val: str) -> int:
    return {"HIGH": 1, "MEDIUM": 0, "LOW": -1}.get(val, 0)


def _calc_signal(score: int) -> str:
    """
    score ≥ 2 : 위험
    score == 1 : 주의
    score == 0 : 중립
    score ≤ -1 : 우호
    """
    if score >= 2:
        return "위험"
    if score == 1:
        return "주의"
    if score == 0:
        return "중립"
    return "우호"
