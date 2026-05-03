"""
Yahoo Finance 수집기 테스트 (API 호출 없음 — 내부 헬퍼 단위 테스트)
"""

from __future__ import annotations

from collectors.kr_yahoo_client import _safe_float


class TestSafeFloat:
    def test_normal_float(self):
        assert _safe_float(2650.0) == 2650.0

    def test_string_float(self):
        assert _safe_float("2650.0") == 2650.0

    def test_none(self):
        assert _safe_float(None) is None

    def test_nan(self):
        result = _safe_float(float("nan"))
        assert result is None

    def test_invalid_string(self):
        assert _safe_float("N/A") is None

    def test_zero(self):
        assert _safe_float(0.0) == 0.0
