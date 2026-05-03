"""
KR Market Engine 테스트
"""

from __future__ import annotations

from engines.kr_market_engine import (
    _calc_signal,
    _score_foreign_pressure,
    _score_krw_regime,
    _score_rate_burden,
    _score_yield_spread,
    run_kr_engine,
)

# ---------------------------------------------------------------------------
# _score_krw_regime
# ---------------------------------------------------------------------------

class TestKrwRegime:
    def test_strong(self):
        assert _score_krw_regime(1300.0) == "STRONG"

    def test_strong_boundary(self):
        assert _score_krw_regime(1319.9) == "STRONG"

    def test_neutral_lower(self):
        assert _score_krw_regime(1320.0) == "NEUTRAL"

    def test_neutral_upper(self):
        assert _score_krw_regime(1379.9) == "NEUTRAL"

    def test_weak(self):
        assert _score_krw_regime(1380.0) == "WEAK"

    def test_weak_high(self):
        assert _score_krw_regime(1450.0) == "WEAK"

    def test_none(self):
        assert _score_krw_regime(None) == "NEUTRAL"


# ---------------------------------------------------------------------------
# _score_foreign_pressure
# ---------------------------------------------------------------------------

class TestForeignPressure:
    def test_high(self):
        # DXY +0.5%, KRW +10원 동시 → HIGH
        assert _score_foreign_pressure(0.5, 10.0) == "HIGH"

    def test_high_boundary(self):
        # DXY +0.3%, KRW +5원 정확히 경계 → HIGH
        assert _score_foreign_pressure(0.3, 5.0) == "HIGH"

    def test_low(self):
        assert _score_foreign_pressure(-0.5, -10.0) == "LOW"

    def test_medium_single_dxy(self):
        # DXY 강세만, KRW 변화 없음 → MEDIUM
        assert _score_foreign_pressure(0.5, 0.0) == "MEDIUM"

    def test_medium_single_krw(self):
        # KRW 약세만, DXY 변화 없음 → MEDIUM
        assert _score_foreign_pressure(0.0, 10.0) == "MEDIUM"

    def test_none_both(self):
        assert _score_foreign_pressure(None, None) == "MEDIUM"

    def test_none_one(self):
        assert _score_foreign_pressure(0.5, None) == "MEDIUM"


# ---------------------------------------------------------------------------
# _score_rate_burden
# ---------------------------------------------------------------------------

class TestRateBurden:
    def test_high(self):
        # 4.5% 이상 + 상승 → HIGH
        assert _score_rate_burden(4.5, 0.05) == "HIGH"

    def test_high_exact_boundary(self):
        assert _score_rate_burden(4.5, 0.01) == "HIGH"

    def test_not_high_if_falling(self):
        # 4.5% 이상이지만 하락 중 → MEDIUM
        assert _score_rate_burden(4.6, -0.02) == "MEDIUM"

    def test_low(self):
        assert _score_rate_burden(3.9, 0.0) == "LOW"

    def test_low_boundary(self):
        assert _score_rate_burden(3.999, None) == "LOW"

    def test_medium(self):
        assert _score_rate_burden(4.2, 0.01) == "MEDIUM"

    def test_none(self):
        assert _score_rate_burden(None, None) == "MEDIUM"


# ---------------------------------------------------------------------------
# _score_yield_spread
# ---------------------------------------------------------------------------

class TestYieldSpread:
    def test_inverted(self):
        assert _score_yield_spread(-0.5) == "INVERTED"

    def test_inverted_boundary(self):
        assert _score_yield_spread(-0.31) == "INVERTED"

    def test_flat_lower(self):
        assert _score_yield_spread(-0.3) == "FLAT"

    def test_flat_upper(self):
        assert _score_yield_spread(0.29) == "FLAT"

    def test_normal(self):
        assert _score_yield_spread(0.3) == "NORMAL"

    def test_normal_high(self):
        assert _score_yield_spread(1.5) == "NORMAL"

    def test_none(self):
        assert _score_yield_spread(None) == "FLAT"


# ---------------------------------------------------------------------------
# _calc_signal
# ---------------------------------------------------------------------------

class TestCalcSignal:
    def test_danger(self):
        assert _calc_signal(2) == "위험"

    def test_danger_high(self):
        assert _calc_signal(3) == "위험"

    def test_caution(self):
        assert _calc_signal(1) == "주의"

    def test_neutral(self):
        assert _calc_signal(0) == "중립"

    def test_good(self):
        assert _calc_signal(-1) == "우호"

    def test_good_high(self):
        assert _calc_signal(-3) == "우호"


# ---------------------------------------------------------------------------
# run_kr_engine 통합
# ---------------------------------------------------------------------------

class TestRunKrEngine:
    def test_danger_scenario(self):
        """외인 HIGH + 금리 HIGH + 환율 WEAK → 위험"""
        data = {
            "krw_usd": 1400.0,
            "dxy_chg_pct": 0.5,
            "krw_usd_chg": 15.0,
            "us10y": 4.8,
            "us10y_chg": 0.05,
            "t10y2y": -0.5,
        }
        result = run_kr_engine(data)
        assert result["market_signal"] == "위험"

    def test_good_scenario(self):
        """외인 LOW + 금리 LOW + 환율 STRONG → 우호"""
        data = {
            "krw_usd": 1300.0,
            "dxy_chg_pct": -0.5,
            "krw_usd_chg": -10.0,
            "us10y": 3.8,
            "us10y_chg": -0.02,
            "t10y2y": 0.5,
        }
        result = run_kr_engine(data)
        assert result["market_signal"] == "우호"

    def test_all_none_returns_neutral(self):
        """모든 값 None → 중립 반환"""
        data = {k: None for k in [
            "krw_usd", "dxy_chg_pct", "krw_usd_chg",
            "us10y", "us10y_chg", "t10y2y",
        ]}
        result = run_kr_engine(data)
        assert result["market_signal"] == "중립"
        assert isinstance(result["signal_score"], int)

    def test_result_keys(self):
        """반환 dict 키 검증"""
        result = run_kr_engine({})
        required_keys = {
            "krw_regime", "foreign_pressure", "rate_burden",
            "yield_spread", "market_signal", "signal_score",
        }
        assert required_keys.issubset(result.keys())
