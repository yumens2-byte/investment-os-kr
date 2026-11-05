"""
reply_engine/schedule.py
==========================
X Reply Engine 랜덤 간격 실행 판정 (DN2026_0099).

reply_engine.yml은 10분 틱(cron "3-59/10 * * * *")으로 깨어나고, 이 모듈의 순수 함수가
"이번 틱에 실제로 실행할지"를 결정한다. 시각·난수·상태는 모두 인자로 주입한다(테스트 가능).

판정 규칙 (DN2026_0099 B3):
  1) workflow_dispatch                     → RUN_DUE (야간·DB 상태 무관)
  2) KST 03:00~03:59                       → SKIP_NIGHT
  3) 상태 조회 실패                         → SKIP_GATE_ERROR (fail-closed)
  4) 상태 행 없음                           → RUN_DUE (자가 복구)
  5) now >= next_run_at                     → RUN_DUE (3시간 이상 오래된 기록은 stale 표시)
  6) 그 외                                  → SKIP_NOT_DUE

다음 실행 시각 (B3):
  next = tick_at + U(40, 70)분. 결과가 KST 03시대면 KST 04:00 + U(0, 10)분.
  10분 틱 해상도 때문에 실제 실행 간격은 40~80분이 된다.

선점 (DN2026_0099 보완 A, 메인 확정):
  간격은 '시작→시작' 기준이므로 next_run_at은 decide(RUN_DUE) 시점에 선점 기록한다
  (build_claim_row). 실행이 길어져도 다음 틱은 선점된 next_run_at 때문에 SKIP_NOT_DUE가 된다.

v1.0.0 (2026-11-06, DN2026_0099): 신설.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

VERSION = "1.0.0"

KST = timezone(timedelta(hours=9))

# STD-SCHED-09: 결정값은 의미가 드러나는 문자열 (true/false 금지)
RUN_DUE = "RUN_DUE"
SKIP_NOT_DUE = "SKIP_NOT_DUE"
SKIP_NIGHT = "SKIP_NIGHT"
SKIP_GATE_ERROR = "SKIP_GATE_ERROR"
DECISIONS = (RUN_DUE, SKIP_NOT_DUE, SKIP_NIGHT, SKIP_GATE_ERROR)

GAP_MIN_MINUTES = 40.0
GAP_MAX_MINUTES = 70.0          # + 10분 틱 해상도 = 실제 간격 상한 80분
NIGHT_HOUR_KST = 3              # KST 03:00~03:59 실행 제외
NIGHT_RESUME_HOUR_KST = 4
NIGHT_RESUME_JITTER_MAX_MINUTES = 10.0
STALE_HOURS = 3


@dataclass(frozen=True)
class Decision:
    decision: str
    tick_at: datetime
    reason: str
    next_run_at: datetime | None = None
    last_tick_at: datetime | None = None
    stale: bool = False

    @property
    def elapsed_min(self) -> float | None:
        if self.last_tick_at is None:
            return None
        return round((self.tick_at - self.last_tick_at).total_seconds() / 60.0, 1)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def parse_ts(value) -> datetime | None:
    """Supabase timestamptz 문자열/ datetime → aware datetime. 해석 불가면 None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def is_night(dt: datetime) -> bool:
    """KST 03:00:00 ~ 03:59:59 이면 True."""
    return _aware(dt).astimezone(KST).hour == NIGHT_HOUR_KST


def decide(
    now: datetime,
    state: dict | None,
    *,
    is_dispatch: bool = False,
    load_failed: bool = False,
) -> Decision:
    """이번 틱 실행 여부. state는 kr_reply_schedule 행(dict) 또는 None."""
    now = _aware(now).astimezone(UTC)
    if is_dispatch:
        return Decision(RUN_DUE, now, "workflow_dispatch")
    if is_night(now):
        return Decision(SKIP_NIGHT, now, "KST 03:00~03:59 실행 제외")
    if load_failed:
        return Decision(SKIP_GATE_ERROR, now, "schedule 상태 조회 실패 (fail-closed)")
    if not state:
        return Decision(RUN_DUE, now, "schedule 행 없음 (자가 복구)")

    next_run_at = parse_ts(state.get("next_run_at"))
    last_tick_at = parse_ts(state.get("last_tick_at"))
    if next_run_at is None:
        return Decision(RUN_DUE, now, "next_run_at 해석 불가 (자가 복구)", None, last_tick_at)

    ref = last_tick_at or next_run_at
    stale = (now - ref) >= timedelta(hours=STALE_HOURS)
    if now >= next_run_at:
        reason = "next_run_at 도래" + (" (stale: 기록 3시간 이상 경과)" if stale else "")
        return Decision(RUN_DUE, now, reason, next_run_at, last_tick_at, stale)
    return Decision(SKIP_NOT_DUE, now, "next_run_at 미도래", next_run_at, last_tick_at, stale)


def compute_next_run(tick_at: datetime, rng: random.Random | None = None) -> tuple[datetime, float]:
    """(next_run_at, 계획 간격 분). KST 03시대에 걸리면 KST 04:00 + U(0,10)분으로 이월."""
    rng = rng or random.Random()
    tick_at = _aware(tick_at).astimezone(UTC)
    gap = rng.uniform(GAP_MIN_MINUTES, GAP_MAX_MINUTES)
    nxt = tick_at + timedelta(minutes=gap)
    if is_night(nxt):
        k = nxt.astimezone(KST)
        resume = k.replace(hour=NIGHT_RESUME_HOUR_KST, minute=0, second=0, microsecond=0)
        resume += timedelta(minutes=rng.uniform(0.0, NIGHT_RESUME_JITTER_MAX_MINUTES))
        nxt = resume.astimezone(UTC)
    planned = round((nxt - tick_at).total_seconds() / 60.0, 1)
    return nxt, planned


def build_claim_row(
    account: str,
    tick_at: datetime,
    prev_state: dict | None,
    *,
    now: datetime,
    rng: random.Random | None = None,
) -> dict:
    """kr_reply_schedule 선점 행 (decide RUN_DUE 시점).

    last_gap_min = 이번 tick − 직전 last_tick_at (분).
    """
    tick_at = _aware(tick_at).astimezone(UTC)
    nxt, planned = compute_next_run(tick_at, rng)
    prev_tick = parse_ts((prev_state or {}).get("last_tick_at"))
    gap = None
    if prev_tick is not None:
        gap = round((tick_at - prev_tick).total_seconds() / 60.0, 1)
    return {
        "account": account,
        "next_run_at": nxt.isoformat(),
        "last_tick_at": tick_at.isoformat(),
        "last_gap_min": gap,
        "last_decision": RUN_DUE,
        "updated_at": _aware(now).astimezone(UTC).isoformat(),
        "_planned_gap_min": planned,
    }
