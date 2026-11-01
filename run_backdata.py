"""
BACKDATA CARD — 백데이터 반전 카드 파이프라인 (DN2026_0090)
============================================================
국내 지수의 '오늘 같은 날 다음엔 어땠나' 통계 1건을 X·Facebook에 발행하고,
지난 게시물 성과를 모은다.

[단계 — --step]
  post       통계 산출 → 문안 → 게이트 → (모드별) 원장·발행
  metrics    live 게시물의 24h·72h 성과 수집 (dry_run은 대상 선정만)
  preflight  읽기 전용 점검: FB 페이지 식별·인사이트 조회, X get_me (쓰기·DB 없음)

[모드 — BACKDATA_MODE]  dry_run(기본) / shadow / live, 그 외 값은 dry_run
[긴급 정지] BACKDATA_ENABLED != 'true'
[종료코드] 1: EXIT_NO_CREDENTIALS · EXIT_PUBLISH_FAIL · EXIT_PARTIAL · EXIT_SPEND_CAP ·
           EXIT_DATA_FAIL · EXIT_RECORD_FAIL · EXIT_PREFLIGHT_FAIL, 그 밖은 0
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from backdata_engine import composer, history, metrics, notify, stats, store
from backdata_engine import fb_publisher as fbpub
from backdata_engine import x_publisher as xpub
from backdata_engine.config import (
    INDEXES,
    KST,
    METRIC_MAX_AGE_DAYS,
    get_delay_max_sec,
    get_mode,
    is_enabled,
)
from core import gemini_gateway
from reply_engine import x_client

VERSION = "0.1.1"

logger = logging.getLogger(__name__)

FAIL_EXITS = frozenset({
    "EXIT_NO_CREDENTIALS", "EXIT_PUBLISH_FAIL", "EXIT_PARTIAL", "EXIT_SPEND_CAP",
    "EXIT_DATA_FAIL", "EXIT_RECORD_FAIL", "EXIT_PREFLIGHT_FAIL",
})

_sleep = time.sleep  # 테스트 교체 지점


def _setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path("logs") / f"backdata_{today}.log"),
        ],
    )
    for name in ("httpx", "httpcore", "urllib3", "requests", "yfinance", "peewee", "google_genai"):
        logging.getLogger(name).setLevel(logging.WARNING)  # STD-SEC-01


def _write_report(summary: dict) -> None:
    try:
        Path("logs").mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = Path("logs") / f"backdata_report_{summary['step']}_{stamp}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    except Exception as exc:
        logger.warning(f"[BReport] 리포트 저장 실패 (무시): {exc}")


def _alert(summary: dict, detail: str = "") -> None:
    notify.send(
        f"[Backdata] {summary['step']} {summary['exit_reason']} mode={summary['mode']} {detail}"
        .strip()
    )


def _finish(summary: dict, reason: str, *, ok: bool, notify: bool = False,
            detail: str = "") -> dict:
    summary["exit_reason"] = reason
    summary["success"] = ok
    summary["finished_at"] = datetime.now(UTC).isoformat()
    # artifact를 못 받아도 원인을 볼 수 있게 결과 요약을 로그 1줄로 (v0.1.1, STD-OBS-03)
    brief = {k: summary[k] for k in ("calls", "checks", "results", "warnings") if summary.get(k)}
    if summary.get("chosen"):
        brief["chosen"] = {k: summary["chosen"].get(k) for k in ("index", "condition_key", "n")}
    if summary.get("compose"):
        brief["compose"] = {k: summary["compose"].get(k)
                            for k in ("ok", "reason", "hook_source", "x_weighted_len")}
    logger.info(f"[Backdata] 종료 | step={summary.get('step')} | {reason} | "
                f"{json.dumps(brief, ensure_ascii=False, default=str)}")
    if notify or reason in FAIL_EXITS:
        _alert(summary, detail)
    _write_report(summary)
    return summary


# ── post ─────────────────────────────────────────────────────────────
def run_post(mode: str, now_kst: datetime) -> dict:
    trade_date = now_kst.date().isoformat()
    summary: dict = {
        "version": VERSION, "step": "post", "mode": mode, "success": False,
        "exit_reason": None, "trade_date": trade_date, "indexes": {},
        "candidates": [], "chosen": None, "compose": None,
        "calls": {"gemini": 0, "x_write": 0, "fb_write": 0, "db_write": 0},
        "started_at": datetime.now(UTC).isoformat(),
    }
    db_write = mode in ("shadow", "live")

    # S1 수집 + 신선도
    fresh: dict[str, list] = {}
    for symbol, name in INDEXES:
        rows = history.fetch_closes(symbol)
        if rows is None:
            summary["indexes"][symbol] = {"status": "fetch_failed"}
            continue
        last = rows[-1][0].isoformat()
        summary["indexes"][symbol] = {"bars": len(rows), "first": rows[0][0].isoformat(),
                                      "last": last}
        if last == trade_date:
            fresh[symbol] = rows
    if all(v.get("status") == "fetch_failed" for v in summary["indexes"].values()):
        return _finish(summary, "EXIT_DATA_FAIL", ok=False)
    failed = [s for s, v in summary["indexes"].items() if v.get("status") == "fetch_failed"]
    if not fresh:
        logger.warning(f"[S1] 당일({trade_date}) 봉 없음 — 휴장 또는 데이터 지연, 무발행")
        return _finish(summary, "EXIT_STALE", ok=True, notify=bool(failed),
                       detail=f"fetch_failed={failed}" if failed else "")
    if failed:
        summary["warnings"] = [f"fetch_failed={failed}"]

    # S2 거래일 중복
    if db_write:
        taken = store.slot_taken(trade_date, mode)
        if taken is None:
            return _finish(summary, "EXIT_RECORD_FAIL", ok=False, detail="slot_taken 조회 실패")
        if taken:
            return _finish(summary, "EXIT_SLOT_TAKEN", ok=True)

    # S3 통계
    names = dict(INDEXES)
    cands = []
    for symbol, rows in fresh.items():
        cands.extend(stats.analyze_index(symbol, names[symbol], rows))
    summary["candidates"] = [
        {k: c[k] for k in ("index", "condition_key", "n", "n_all", "rarity", "eligible")}
        for c in cands
    ]
    chosen = stats.select(cands, [s for s, _ in INDEXES])
    if chosen is None:
        return _finish(summary, "EXIT_NO_SIGNAL", ok=True)
    summary["chosen"] = chosen

    # S4 문안
    comp = composer.compose(chosen, gemini_gateway.call)
    summary["calls"]["gemini"] = comp["llm"].get("api_calls", 0)
    summary["compose"] = comp
    if not comp["ok"]:
        return _finish(summary, "EXIT_GATE_FAIL", ok=True, notify=True,
                       detail=f"reason={comp['reason']}")

    # S5 dry_run
    if mode == "dry_run":
        logger.info(f"[DRY_RUN] {chosen['condition_key']} n={chosen['n']} "
                    f"x_len={comp['x_weighted_len']} writeExecuted=false\n{comp['text_x']}")
        warn = summary.get("warnings")
        return _finish(summary, "EXIT_OK", ok=True, notify=bool(warn),
                       detail=f"warnings={warn}" if warn else "")

    # S6 pending 선기록
    record = {
        "trade_date": trade_date, "mode": mode, "format": "reversal_card",
        "condition_key": chosen["condition_key"], "stats_json": chosen,
        "text_x": comp["text_x"], "text_fb": comp["text_fb"], "text_hash": comp["text_hash"],
        "hook_source": comp["hook_source"],
        "x_status": "pending", "fb_status": "pending", "version": VERSION,
    }
    post_id, insert_err = store.insert_pending(record)
    summary["calls"]["db_write"] += 1
    if insert_err == "conflict":
        return _finish(summary, "EXIT_DUP_FINAL", ok=True)
    if post_id is None:
        return _finish(summary, "EXIT_RECORD_FAIL", ok=False, detail="insert_pending 실패")
    summary["post_id"] = post_id
    if mode == "shadow":
        logger.info(f"[SHADOW] 원장 적재 완료 (발행 없음): id={post_id}")
        return _finish(summary, "EXIT_OK", ok=True)

    # S7 live
    page_id = os.environ.get("FACE_PAGE_ID", "").strip()
    page_token = os.environ.get("FACE_PAGE_TOKEN", "").strip()
    client = x_client.get_x_client()
    if client is None or not page_id or not page_token:
        for ch in ("x", "fb"):
            store.update_channel(post_id, ch, "skipped", error="NO_CREDENTIALS")
        return _finish(summary, "EXIT_NO_CREDENTIALS", ok=False)

    delay = random.randint(0, get_delay_max_sec())
    summary["delay_sec"] = delay
    logger.info(f"[S7] 랜덤 지연 {delay}초 (안티봇, STD-PUB-10)")
    _sleep(delay)

    results: dict[str, str] = {}
    record_ok = True
    tweet_id, x_err = xpub.post_text(client, comp["text_x"])
    summary["calls"]["x_write"] += 1
    if tweet_id:
        results["x"] = "posted"
        record_ok &= store.update_channel(post_id, "x", "posted", ext_id=tweet_id)
    else:
        status = "unknown" if (x_err or "").startswith("UNKNOWN") else "failed"
        results["x"] = "spend_cap" if x_client.is_spend_cap_error(x_err) else status
        record_ok &= store.update_channel(post_id, "x", status, error=x_err)

    fb_id, fb_err = fbpub.post_feed(page_id, page_token, comp["text_fb"])
    summary["calls"]["fb_write"] += 1
    if fb_id:
        results["fb"] = "posted"
        record_ok &= store.update_channel(post_id, "fb", "posted", ext_id=fb_id)
    else:
        status = "unknown" if (fb_err or "").startswith("UNKNOWN") else "failed"
        results["fb"] = status
        record_ok &= store.update_channel(post_id, "fb", status, error=fb_err)
    summary["calls"]["db_write"] += 2
    summary["results"] = results

    posted = [ch for ch, s in results.items() if s == "posted"]
    if not record_ok:
        return _finish(summary, "EXIT_RECORD_FAIL", ok=False, detail=f"id={post_id}")
    if len(posted) == 2:
        logger.info(f"[Backdata] 발행 완료 x={tweet_id} fb={fb_id}")
        warn = summary.get("warnings")
        return _finish(summary, "EXIT_OK", ok=True, notify=bool(warn),
                       detail=f"warnings={warn}" if warn else "")
    if results.get("x") == "spend_cap" and not posted:
        return _finish(summary, "EXIT_SPEND_CAP", ok=False)
    if posted:
        return _finish(summary, "EXIT_PARTIAL", ok=False, detail=json.dumps(results))
    return _finish(summary, "EXIT_PUBLISH_FAIL", ok=False, detail=json.dumps(results))


# ── metrics ──────────────────────────────────────────────────────────
def run_metrics(mode: str, now_utc: datetime) -> dict:
    summary: dict = {
        "version": VERSION, "step": "metrics", "mode": mode, "success": False,
        "exit_reason": None, "due": [], "collected": 0, "errors": [],
        "calls": {"x_read": 0, "fb_read": 0, "db_write": 0},
        "started_at": now_utc.isoformat(),
    }
    if mode != "live":
        # dry_run·shadow: 운영 게시물이 없으므로 DB 조회·API 호출 없이 종료
        logger.info(f"[Metrics] mode={mode} — 수집 생략 (live 전용)")
        return _finish(summary, "EXIT_OK", ok=True)
    try:
        posts = store.list_metric_targets(now_utc, METRIC_MAX_AGE_DAYS)
    except Exception as exc:
        return _finish(summary, "EXIT_DATA_FAIL", ok=False, detail=str(exc)[:200])

    due = [(p, ch, cp, h) for p in posts for ch, cp, h in metrics.due_checkpoints(p, now_utc)]
    summary["due"] = [{"post_id": p["id"], "channel": ch, "checkpoint": cp, "hours": h}
                      for p, ch, cp, h in due]
    if not due:
        return _finish(summary, "EXIT_OK", ok=True)

    x_ids = [p["x_post_id"] for p, ch, _, _ in due if ch == "x"]
    x_data: dict = {}
    if x_ids:
        client = x_client.get_x_client()
        if client is None:
            # X 키 문제로 FB 수집까지 막지 않는다 (R6 제안)
            summary["errors"].append({"channel": "x", "error": "NO_CREDENTIALS"})
        else:
            x_data, err = xpub.fetch_metrics(client, x_ids)
            summary["calls"]["x_read"] += len(x_ids)
            if err:
                summary["errors"].append({"channel": "x", "error": err[:200]})
    token = os.environ.get("FACE_PAGE_TOKEN", "").strip()

    for p, ch, cp, hours in due:
        if ch == "x":
            m = x_data.get(str(p["x_post_id"]))
        else:
            if not token:
                summary["errors"].append({"channel": "fb", "error": "NO_CREDENTIALS"})
                continue
            m, err = fbpub.fetch_insights(p["fb_post_id"], token)
            summary["calls"]["fb_read"] += 1
            if err:
                summary["errors"].append({"channel": "fb", "error": err[:200]})
        if not m or (m.get("impressions") is None and m.get("engagements") is None):
            # 지표가 비어 있으면 체크포인트를 소진하지 않고 다음 실행에서 재수집 (R6-03)
            continue
        ok = store.insert_metric({
            "post_id": p["id"], "channel": ch, "checkpoint": cp, "elapsed_hours": hours,
            "impressions": m["impressions"], "engagements": m["engagements"], "raw": m["raw"],
        })
        summary["calls"]["db_write"] += 1
        summary["collected"] += 1 if ok else 0

    if summary["errors"]:
        return _finish(summary, "EXIT_OK", ok=True, notify=True,
                       detail=f"errors={len(summary['errors'])}")
    return _finish(summary, "EXIT_OK", ok=True)


# ── preflight ────────────────────────────────────────────────────────
def run_preflight(mode: str) -> dict:
    summary: dict = {"version": VERSION, "step": "preflight", "mode": mode,
                     "success": False, "exit_reason": None, "checks": {}}
    checks = summary["checks"]
    page_id = os.environ.get("FACE_PAGE_ID", "").strip()
    token = os.environ.get("FACE_PAGE_TOKEN", "").strip()
    if page_id and token:
        identity, err = fbpub.fbclient.fetch_page_identity(page_id, token)
        checks["fb_page_identity"] = "ok" if identity and not err else (
            fbpub.fbclient.format_error(err) if err else "empty")
        feed, err, _ = fbpub.fbclient.graph_get(f"{page_id}/feed", token,
                                                {"limit": 1, "fields": "id"})
        latest = ((feed or {}).get("data") or [{}])[0].get("id")
        if latest:
            _ins, ierr = fbpub.fetch_insights(latest, token)
            checks["fb_insights_read"] = "ok" if not ierr else ierr
        else:
            checks["fb_insights_read"] = "no_post" if not err else fbpub.fbclient.format_error(err)
        checks["fb_publish_permission"] = (
            "unverified (쓰기 없이 확인 불가 — 첫 live 또는 토큰 디버거)"
        )
    else:
        checks["fb_page_identity"] = "NO_CREDENTIALS"
    client = x_client.get_x_client()
    checks["x_get_me"] = "ok" if client and x_client.fetch_my_user_id(client) else "failed"
    failed = [k for k, v in checks.items()
              if v not in ("ok", "no_post") and not str(v).startswith("unverified")]
    summary["failed"] = failed
    detail = "; ".join(f"{k}={str(checks[k])[:160]}" for k in failed)
    return _finish(summary, "EXIT_PREFLIGHT_FAIL" if failed else "EXIT_OK", ok=not failed,
                   detail=detail)


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", choices=["post", "metrics", "preflight"], default="post")
    args = parser.parse_args(argv)
    _setup_logging()
    mode = get_mode()
    now_kst = datetime.now(KST)
    logger.info(f"[Backdata] v{VERSION} 시작 | step={args.step} | mode={mode} | "
                f"kst={now_kst.isoformat()}")
    if args.step == "preflight":
        return run_preflight(mode)
    if not is_enabled():
        logger.warning("[S0] BACKDATA_ENABLED != 'true' — 비활성, 종료")
        summary = {"version": VERSION, "step": args.step, "mode": mode}
        return _finish(summary, "EXIT_DISABLED", ok=True)
    if args.step == "metrics":
        return run_metrics(mode, datetime.now(UTC))
    return run_post(mode, now_kst)


if __name__ == "__main__":
    result = main()
    sys.exit(1 if result.get("exit_reason") in FAIL_EXITS else 0)
