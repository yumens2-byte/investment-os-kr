"""
tests/test_us_market_holidays.py
================================
영업일 캘린더 단위 테스트

검증 항목:
  - 휴장일 정확 판정 (2026/2027 전체)
  - 주말 스킵
  - 평일 통과
  - FORCE_RUN 우회
"""
from __future__ import annotations

import os
from datetime import date
from unittest.mock import patch

import pytest

from config.us_market_holidays import (
    ALL_HOLIDAYS,
    US_MARKET_HOLIDAYS_2026,
    US_MARKET_HOLIDAYS_2027,
    is_us_market_holiday,
    should_skip_market_session,
)


# ---------------------------------------------------------------------------
# is_us_market_holiday
# ---------------------------------------------------------------------------

class TestIsUsMarketHoliday:
    def test_new_year_2026(self):
        is_h, name = is_us_market_holiday(date(2026, 1, 1))
        assert is_h is True
        assert name == "New Year's Day"

    def test_thanksgiving_2026(self):
        is_h, name = is_us_market_holiday(date(2026, 11, 26))
        assert is_h is True
        assert "Thanksgiving" in name

    def test_christmas_2026(self):
        is_h, name = is_us_market_holiday(date(2026, 12, 25))
        assert is_h is True
        assert "Christmas" in name

    def test_normal_weekday_2026(self):
        # 2026-05-04 (월) — 정상 영업일
        is_h, name = is_us_market_holiday(date(2026, 5, 4))
        assert is_h is False
        assert name == ""

    def test_2027_holidays_covered(self):
        is_h, name = is_us_market_holiday(date(2027, 1, 1))
        assert is_h is True
        assert "New Year" in name

    def test_default_today(self):
        # 인자 없으면 오늘 — 예외 없이 실행되어야 함
        result = is_us_market_holiday()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


# ---------------------------------------------------------------------------
# should_skip_market_session
# ---------------------------------------------------------------------------

class TestShouldSkipMarketSession:
    def test_normal_weekday_passes(self):
        # 2026-05-04 (월요일, 영업일)
        skip, reason = should_skip_market_session(date(2026, 5, 4))
        assert skip is False
        assert reason == ""

    def test_saturday_skipped(self):
        # 2026-05-02 (토)
        skip, reason = should_skip_market_session(date(2026, 5, 2))
        assert skip is True
        assert "주말" in reason

    def test_sunday_skipped(self):
        # 2026-05-03 (일)
        skip, reason = should_skip_market_session(date(2026, 5, 3))
        assert skip is True
        assert "주말" in reason

    def test_holiday_weekday_skipped(self):
        # 2026-01-19 (월, MLK Day)
        skip, reason = should_skip_market_session(date(2026, 1, 19))
        assert skip is True
        assert "휴무일" in reason

    def test_force_run_overrides_holiday(self):
        # FORCE_RUN=true 시 휴장일도 통과
        with patch.dict(os.environ, {"FORCE_RUN": "true"}):
            skip, reason = should_skip_market_session(date(2026, 12, 25))
            assert skip is False
            assert reason == ""

    def test_force_run_overrides_weekend(self):
        with patch.dict(os.environ, {"FORCE_RUN": "true"}):
            skip, reason = should_skip_market_session(date(2026, 5, 2))
            assert skip is False

    def test_force_run_case_insensitive(self):
        with patch.dict(os.environ, {"FORCE_RUN": "TRUE"}):
            skip, _ = should_skip_market_session(date(2026, 5, 2))
            assert skip is False
        with patch.dict(os.environ, {"FORCE_RUN": "1"}):
            skip, _ = should_skip_market_session(date(2026, 5, 2))
            assert skip is False

    def test_force_run_empty_does_not_override(self):
        with patch.dict(os.environ, {"FORCE_RUN": ""}):
            skip, _ = should_skip_market_session(date(2026, 5, 2))
            assert skip is True


# ---------------------------------------------------------------------------
# 무결성 검증
# ---------------------------------------------------------------------------

class TestHolidayDataIntegrity:
    def test_all_2026_dates_valid(self):
        for date_str in US_MARKET_HOLIDAYS_2026:
            year, month, day = date_str.split("-")
            d = date(int(year), int(month), int(day))
            assert d.year == 2026

    def test_all_2027_dates_valid(self):
        for date_str in US_MARKET_HOLIDAYS_2027:
            year, month, day = date_str.split("-")
            d = date(int(year), int(month), int(day))
            assert d.year == 2027

    def test_no_overlap_between_years(self):
        keys_2026 = set(US_MARKET_HOLIDAYS_2026.keys())
        keys_2027 = set(US_MARKET_HOLIDAYS_2027.keys())
        assert len(keys_2026 & keys_2027) == 0

    def test_all_holidays_merged(self):
        assert len(ALL_HOLIDAYS) == len(US_MARKET_HOLIDAYS_2026) + len(US_MARKET_HOLIDAYS_2027)
