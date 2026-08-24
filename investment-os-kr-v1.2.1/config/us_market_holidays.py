"""
config/us_market_holidays.py
================================
미국 주식 시장 휴무일 체크 (KR Market OS)

2026/2027년 NYSE/NASDAQ 휴무일 목록 관리.
run_market.py 시작 시 호출하여 미장 휴무일이면 발행 스킵.

발행 기준:
  - 한국 평일 + 전날(ET) 미국 영업일이었을 때만 발행
  - 미국 휴장 다음 날 한국 아침 → 미장 데이터 부재 → 스킵

환경변수:
  FORCE_RUN=true  — 휴무일이어도 강제 실행 (수동 테스트용)

출처: investment-os 본 OS의 동일명 모듈을 그대로 이식 (2026-05-04).
       검증된 코드, 추측 없이 복사.
"""
from __future__ import annotations

import logging
import os
from datetime import date

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

# 2026년 미국 주식 시장 휴무일 (NYSE/NASDAQ)
US_MARKET_HOLIDAYS_2026: dict[str, str] = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Presidents' Day",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (Observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
}

# 2027년 (연말 자동 운영 대비)
US_MARKET_HOLIDAYS_2027: dict[str, str] = {
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Presidents' Day",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth (Observed)",
    "2027-07-05": "Independence Day (Observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (Observed)",
}

# 전체 휴무일 합산
ALL_HOLIDAYS: dict[str, str] = {**US_MARKET_HOLIDAYS_2026, **US_MARKET_HOLIDAYS_2027}


def is_us_market_holiday(check_date: date | None = None) -> tuple[bool, str]:
    """
    미국 시장 휴무일 여부 확인.

    Args:
        check_date: 확인할 날짜 (기본: 오늘)

    Returns:
        (is_holiday: bool, holiday_name: str)
        예: (True, "Good Friday") 또는 (False, "")
    """
    if check_date is None:
        check_date = date.today()

    date_str = check_date.isoformat()
    holiday_name = ALL_HOLIDAYS.get(date_str, "")

    if holiday_name:
        return True, holiday_name
    return False, ""


def should_skip_market_session(check_date: date | None = None) -> tuple[bool, str]:
    """
    시장 세션 스킵 여부 판단 (휴무일 + 주말 + FORCE_RUN 체크).

    Returns:
        (should_skip: bool, reason: str)
    """
    # FORCE_RUN 환경변수 — 강제 실행
    force_run = os.getenv("FORCE_RUN", "").lower() in ("true", "1", "yes")
    if force_run:
        logger.info("[Holiday] FORCE_RUN=true — 휴무일/주말 무시, 강제 실행")
        return False, ""

    if check_date is None:
        check_date = date.today()

    # 주말 체크 (토=5, 일=6)
    weekday = check_date.weekday()
    if weekday >= 5:
        weekday_label = ["월", "화", "수", "목", "금", "토", "일"][weekday]
        reason = f"주말 ({weekday_label}요일)"
        logger.info(f"[Holiday] {reason} — 세션 스킵")
        return True, reason

    # 미장 휴무일 체크
    is_holiday, holiday_name = is_us_market_holiday(check_date)
    if is_holiday:
        reason = f"미장 휴무일: {holiday_name} ({check_date})"
        logger.info(f"[Holiday] {reason} — 세션 스킵")
        return True, reason

    return False, ""
