"""
scripts/reply_schedule_gate.py
================================
X Reply Engine 랜덤 간격 실행 게이트 CLI (DN2026_0099).

  python -m scripts.reply_schedule_gate decide
      → GITHUB_OUTPUT: decision ∈ {RUN_DUE, SKIP_NOT_DUE, SKIP_NIGHT, SKIP_GATE_ERROR}, tick_at
      → RUN_DUE(스케줄 틱)이면 출력 전에 next_run_at을 선점 기록한다 (조건부 갱신 CAS):
          행 없음 → insert / 행 있음 → update ... where account = ? and next_run_at = <읽은 값>
        선점 실패(예외·결과 없음) → SKIP_GATE_ERROR (선점 없이 실행 금지, fail-closed)
      → workflow_dispatch는 항상 RUN_DUE, next_run_at을 무조건 upsert (실패해도 실행, 경고만)
      → SKIP 판정은 DB에 쓰지 않는다. SKIP이면 GITHUB_STEP_SUMMARY에 사유·next_run_at 기록.
  python -m scripts.reply_schedule_gate report --tick-at <UTC ISO>
      → 텔레메트리 전용(읽기만). logs/reply_schedule_*.json 1건. next_run_at을 쓰지 않는다.

판정·계산은 reply_engine/schedule.py 순수 함수가 한다. 이 파일은 I/O만 담당한다.
DB 클라이언트는 reply_engine.store.get_client(모듈 전역 조회 → 테스트 patch 가능)를 사용한다.

v1.1.0 (2026-11-13, DN2026_0103): decide 로그에 late_min(= tick_at − next_run_at, RUN_DUE만) 추가.
  cron 재배치 효과를 Actions 로그로 관측한다 (DB 스키마·판정 불변).
v1.0.0 (2026-11-06, DN2026_0099): 신설 (선점 방식 — decide 시점 기록, 이중 기록 없음).
  SKIP_GATE_ERROR 알림은 UTC 분 0~9 틱만(시간당 최대 1회), 예외는 클래스명만 기록(STD-SEC-01).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from core.alert import send_admin_alert
from reply_engine import schedule, store

VERSION = "1.1.0"  # DN2026_0103

logger = logging.getLogger(__name__)

TABLE = "kr_reply_schedule"
# run_reply.py `_ACCOUNT = "kr_main"` (kr_reply_cursor.account 키)와 동일 규칙.
# 스케줄은 모드와 무관한 워크플로 단위이므로 shadow 접미사(":shadow")를 붙이지 않는다.
ACCOUNT = "kr_main"
LOGS_DIR = Path("logs")


def _fmt(dt: datetime | None) -> str:
    return dt.isoformat() if dt is not None else "-"


def _log_line(stage: str, decision: str, tick_at, next_run_at, gap_min, extra: str = "") -> str:
    gap = "-" if gap_min is None else f"{gap_min}"
    line = (
        f"[ReplySchedule] v{VERSION} {stage} | {decision} | {_fmt(tick_at)} | "
        f"{_fmt(next_run_at)} | {gap}"
    )
    return f"{line} | {extra}" if extra else line


def _append_file(env_name: str, text: str) -> None:
    path = os.environ.get(env_name, "").strip()
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def _exc_name(exc: BaseException) -> str:
    """STD-SEC-01: 공개 Actions 로그에 예외 문자열(URL 등)을 남기지 않고 클래스명만 쓴다."""
    return type(exc).__name__


ALERT_MINUTE_WINDOW = 10  # UTC 분 0~9 틱에서만 알림 (10분 틱 → 시간당 최대 1회)


def _alert_gate_error(now: datetime, text: str) -> bool:
    """SKIP_GATE_ERROR 알림 억제. 로그·STEP_SUMMARY는 호출자가 매번 남긴다."""
    if now.astimezone(UTC).minute >= ALERT_MINUTE_WINDOW:
        logger.info("[ReplySchedule] 알림 억제 (UTC 분 %02d)", now.astimezone(UTC).minute)
        return False
    send_admin_alert(text)
    return True


def load_state() -> dict | None:
    """kr_reply_schedule 행. 예외는 호출자에게 전파한다(fail-closed 판정용)."""
    result = store.get_client().table(TABLE).select("*").eq("account", ACCOUNT).limit(1).execute()
    return result.data[0] if result.data else None


def claim(row: dict, prev_state: dict | None) -> bool:
    """조건부 선점. 행 없음 → insert, 행 있음 → 읽은 next_run_at과 같을 때만 update.

    반환 False = 다른 gate가 먼저 갱신했거나 결과 없음. 예외는 호출자에게 전파.
    """
    table = store.get_client().table(TABLE)
    if not prev_state:
        return bool(table.insert(row).execute().data)
    values = {k: v for k, v in row.items() if k != "account"}
    result = (
        table.update(values)
        .eq("account", ACCOUNT)
        .eq("next_run_at", prev_state.get("next_run_at"))
        .execute()
    )
    return bool(result.data)


def late_minutes(d: schedule.Decision) -> float | None:
    """RUN_DUE 틱이 예정 시각(next_run_at)보다 늦은 분 (DN2026_0103). 해당 없음이면 None."""
    if d.decision != schedule.RUN_DUE or d.next_run_at is None:
        return None
    return round((d.tick_at - d.next_run_at).total_seconds() / 60.0, 1)


def run_decide(now: datetime | None = None, *, rng=None) -> schedule.Decision:
    now = now or datetime.now(UTC)
    is_dispatch = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    state = None
    load_failed = False
    load_error = ""
    if not schedule.is_night(now) or is_dispatch:
        try:
            state = load_state()
        except Exception as exc:
            load_failed = True
            load_error = _exc_name(exc)
            logger.error("[ReplySchedule] 상태 조회 실패: %s", load_error)

    d = schedule.decide(now, state, is_dispatch=is_dispatch, load_failed=load_failed)
    if d.decision == schedule.SKIP_GATE_ERROR:
        _alert_gate_error(now, f"[ReplySchedule] SKIP_GATE_ERROR — {TABLE} 조회 실패: {load_error}")

    claimed_next = None
    claim_note = ""
    if d.decision == schedule.RUN_DUE:
        row = schedule.build_claim_row(ACCOUNT, d.tick_at, state, now=now, rng=rng)
        planned = row.pop("_planned_gap_min")
        claimed_next = schedule.parse_ts(row["next_run_at"])
        if is_dispatch:
            # 수동 실행은 항상 진행한다. 직후 예약 실행이 붙지 않도록 next_run_at을 갱신.
            try:
                ok = bool(store.get_client().table(TABLE).upsert(row).execute().data)
            except Exception as exc:
                ok = False
                name = _exc_name(exc)
                print(f"::warning::[ReplySchedule] dispatch next_run_at 갱신 실패: {name}")
            claim_note = f"claim=upsert ok={str(ok).lower()} planned_gap_min={planned}"
        else:
            try:
                ok = claim(row, state)
                err = "" if ok else "선점 결과 없음 (다른 gate 선점 또는 미반영)"
            except Exception as exc:
                ok, err = False, f"선점 실패: {_exc_name(exc)}"
            if ok:
                claim_note = f"claim=ok planned_gap_min={planned}"
            else:
                _alert_gate_error(now, f"[ReplySchedule] SKIP_GATE_ERROR — {err}")
                d = dataclasses.replace(
                    d, decision=schedule.SKIP_GATE_ERROR, reason=f"{err} (fail-closed)"
                )
                claimed_next = None

    extra = f"reason={d.reason}" + (" stale=true" if d.stale else "")
    late = late_minutes(d)
    if late is not None:
        extra += f" late_min={late}"
    if claim_note:
        extra += f" {claim_note}"
    shown_next = claimed_next if claimed_next is not None else d.next_run_at
    print(_log_line("decide", d.decision, d.tick_at, shown_next, d.elapsed_min, extra))

    _append_file("GITHUB_OUTPUT", f"decision={d.decision}\ntick_at={d.tick_at.isoformat()}\n")
    if d.decision != schedule.RUN_DUE:
        _append_file(
            "GITHUB_STEP_SUMMARY",
            "### Reply schedule gate — skipped\n"
            f"- decision: `{d.decision}`\n"
            f"- reason: {d.reason}\n"
            f"- tick_at: `{d.tick_at.isoformat()}`\n"
            f"- next_run_at: `{_fmt(d.next_run_at)}`\n",
        )
    return dataclasses.replace(d, next_run_at=shown_next)


def run_report(
    tick_at_raw: str,
    *,
    now: datetime | None = None,
    logs_dir: Path | None = None,
) -> dict:
    """텔레메트리 전용: DB를 읽기만 하고 JSON 1건을 남긴다 (next_run_at 덮어쓰기 금지)."""
    now = now or datetime.now(UTC)
    logs_dir = logs_dir or LOGS_DIR
    tick_at = schedule.parse_ts(tick_at_raw)
    warnings: list[str] = []
    if tick_at is None:
        warnings.append(f"tick_at 해석 불가({tick_at_raw!r})")
    state = None
    try:
        state = load_state()
    except Exception as exc:
        warnings.append(f"상태 조회 실패: {_exc_name(exc)}")
    for w in warnings:
        print(f"::warning::[ReplySchedule] {w}")

    next_run_at = schedule.parse_ts((state or {}).get("next_run_at"))
    duration = None
    if tick_at is not None:
        duration = round((now - tick_at).total_seconds() / 60.0, 1)
    gap = (state or {}).get("last_gap_min")
    print(
        _log_line(
            "report",
            schedule.RUN_DUE,
            tick_at,
            next_run_at,
            gap,
            f"run_duration_min={duration}",
        )
    )
    report = {
        "version": VERSION,
        "dn": "DN2026_0099",
        "stage": "report",
        "account": ACCOUNT,
        "tick_at": tick_at.isoformat() if tick_at else None,
        "finished_at": now.astimezone(UTC).isoformat(),
        "run_duration_min": duration,
        "state": state,
        "warnings": warnings,
    }
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(UTC).strftime("%Y%m%d_%H%M%S")
        (logs_dir / f"reply_schedule_{stamp}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    except Exception as exc:
        print(f"::warning::[ReplySchedule] 로그 파일 기록 실패: {_exc_name(exc)}")
    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="reply_schedule_gate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("decide")
    r = sub.add_parser("report")
    r.add_argument("--tick-at", default="")
    args = parser.parse_args(argv)

    if args.cmd == "decide":
        try:
            run_decide()
        except Exception as exc:  # 어떤 예외도 실행으로 이어지지 않게 한다 (fail-closed)
            now = datetime.now(UTC)
            print(f"::warning::[ReplySchedule] decide 예외: {_exc_name(exc)}")
            err = f"error={_exc_name(exc)}"
            print(_log_line("decide", schedule.SKIP_GATE_ERROR, now, None, None, err))
            _append_file(
                "GITHUB_OUTPUT", f"decision={schedule.SKIP_GATE_ERROR}\ntick_at={now.isoformat()}\n"
            )
            _append_file(
                "GITHUB_STEP_SUMMARY",
                f"### Reply schedule gate — skipped\n- decision: `{schedule.SKIP_GATE_ERROR}`\n"
                f"- reason: decide 예외 {_exc_name(exc)}\n",
            )
        return 0
    try:
        run_report(args.tick_at)
    except Exception as exc:
        print(f"::warning::[ReplySchedule] report 예외 (job 실패시키지 않음): {_exc_name(exc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
