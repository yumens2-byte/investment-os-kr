"""
FRED 수집기 테스트 (API 호출 없음 — 내부 헬퍼 단위 테스트)
"""

from __future__ import annotations

from collectors.kr_fred_client import (
    _diff,
    _empty_result,
    _parse_value,
    _pct_chg,
)


class TestParseValue:
    def test_normal_float(self):
        assert _parse_value("4.51") == 4.51

    def test_dot_returns_none(self):
        assert _parse_value(".") is None

    def test_none_returns_none(self):
        assert _parse_value(None) is None

    def test_invalid_string(self):
        assert _parse_value("N/A") is None

    def test_integer_string(self):
        assert _parse_value("1382") == 1382.0


class TestDiff:
    def test_positive(self):
        assert _diff(1382.0, 1377.0) == 5.0

    def test_negative(self):
        assert _diff(1377.0, 1382.0) == -5.0

    def test_none_cur(self):
        assert _diff(None, 1377.0) is None

    def test_none_prev(self):
        assert _diff(1382.0, None) is None


class TestPctChg:
    def test_positive(self):
        result = _pct_chg(104.5, 104.0)
        assert result is not None
        assert abs(result - 0.4808) < 0.001

    def test_zero_prev(self):
        assert _pct_chg(100.0, 0.0) is None

    def test_none(self):
        assert _pct_chg(None, 104.0) is None


class TestEmptyResult:
    def test_all_none(self):
        result = _empty_result()
        for k, v in result.items():
            if k != "collected_at":
                assert v is None

    def test_has_collected_at(self):
        result = _empty_result()
        assert "collected_at" in result
        assert result["collected_at"] is not None
