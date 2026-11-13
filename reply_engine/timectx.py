"""
reply_engine/timectx.py
=========================
답글 시간 맥락 (DN2026_0103, X 전용).

실측(kr_reply_history, 2026-10-30~11-01):
  - 10-30(금) 16:19 KST  "드디어 불타늘 금모닝!!" → "기분 좋은 아침 시작이네요! 😊"
  - 10-31(토) 01:05 KST  "불금 달려보시쥬ㅎ"      → "금요일도 활기차게 보내시길 바라요!"
  댓글 작성 → 수집 지연 p50 269분 / p90 460분 (DN2026_0099 이후 24건)이라 댓글 속 시점이
  답글 게시 시점에는 이미 지나 있다. 생성 프롬프트에는 현재 시각이 없었다.

이 모듈은 순수 함수만 둔다 (DB·네트워크 없음).
  - now_kst(): 현재 KST
  - label(): 프롬프트용 "YYYY-MM-DD(요일) HH:MM"
  - age_minutes(): 댓글 작성 후 경과 분
  - mismatch(): 답글 속 시점 표현이 게시 시각(KST)과 맞지 않으면 사유 문자열
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

VERSION = "1.0.0"  # DN2026_0103

KST = timezone(timedelta(hours=9))
WEEKDAYS_KO = "월화수목금토일"

# 시간대 표현 → 허용 KST 시(hour) 구간 [start, end). end < start면 자정을 넘는 구간.
_DAYPART_RULES: tuple[tuple[str, re.Pattern[str], int, int], ...] = (
    ("아침", re.compile(r"아침|모닝"), 5, 11),
    ("점심", re.compile(r"점심|맛점"), 11, 15),
    ("저녁", re.compile(r"저녁|맛저"), 16, 22),
    ("밤", re.compile(r"굿\s*밤|굿\s*나잇|(?:좋은|편안한|포근한)\s*밤|밤\s*(?:되|보내)"), 20, 4),
)
# 출퇴근: 평일(월~금)·해당 시간대만. 파일럿 실측(토 01:07 "퇴근길 조심히 들어가세요" 통과) 보완.
_COMMUTE_RULES: tuple[tuple[str, re.Pattern[str], int, int], ...] = (
    ("출근", re.compile(r"출근"), 6, 11),
    ("퇴근", re.compile(r"퇴근"), 16, 22),
)
_WEEKDAY_PATTERN = re.compile(r"([월화수목금토일])요일")
_BULGEUM_PATTERN = re.compile(r"불금")
_WEEKEND_PATTERN = re.compile(r"주말")


def _utcnow() -> datetime:
    """시계 주입점 (테스트는 tests/conftest.py에서 고정 시각으로 교체한다)."""
    return datetime.now(UTC)


def now_kst(now: datetime | None = None) -> datetime:
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(KST)


def label(now: datetime) -> str:
    """프롬프트용 KST 표기. 예: '2026-11-13(금) 23:11'."""
    k = now_kst(now)
    return f"{k:%Y-%m-%d}({WEEKDAYS_KO[k.weekday()]}) {k:%H:%M}"


def _parse(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def age_minutes(created_at, now: datetime) -> int | None:
    """댓글 작성 후 경과 분. 작성 시각 불명 또는 미래 시각이면 None."""
    created = _parse(created_at)
    if created is None:
        return None
    minutes = (now_kst(now) - created).total_seconds() / 60.0
    return int(minutes) if minutes >= 0 else None


def _in_hours(hour: int, start: int, end: int) -> bool:
    return start <= hour < end if start < end else (hour >= start or hour < end)


def mismatch(text: str, now: datetime) -> str | None:
    """답글 속 시점 표현이 게시 시각(KST)과 맞지 않으면 사유, 맞으면 None.

    판정 대상은 명시적 시점어만이다 (시간대·출퇴근·요일·불금·주말). '오늘', '하루'처럼
    어느 시각에나 쓰일 수 있는 표현은 판정하지 않는다 (과차단 방지).
    """
    k = now_kst(now)
    hour, weekday = k.hour, k.weekday()  # weekday: 월=0 … 일=6
    body = text or ""
    for name, pattern, start, end in _DAYPART_RULES:
        if pattern.search(body) and not _in_hours(hour, start, end):
            return f"{name}@{hour:02d}시"
    for name, pattern, start, end in _COMMUTE_RULES:
        if pattern.search(body) and (weekday >= 5 or not _in_hours(hour, start, end)):
            return f"{name}@{WEEKDAYS_KO[weekday]}{hour:02d}시"
    for m in _WEEKDAY_PATTERN.finditer(body):
        if WEEKDAYS_KO.index(m.group(1)) != weekday:
            return f"{m.group(1)}요일@{WEEKDAYS_KO[weekday]}"
    # 불금: 금 12:00 ~ 토 04:59
    if _BULGEUM_PATTERN.search(body) and not (
        (weekday == 4 and hour >= 12) or (weekday == 5 and hour < 5)
    ):
        return f"불금@{WEEKDAYS_KO[weekday]}{hour:02d}시"
    # 주말: 금 12:00 ~ 일 23:59
    if _WEEKEND_PATTERN.search(body) and not (
        (weekday == 4 and hour >= 12) or weekday in (5, 6)
    ):
        return f"주말@{WEEKDAYS_KO[weekday]}{hour:02d}시"
    return None
