"""Per-run append-only decision journal and run report; no credentials or comment bodies.

v1.1.0 (2026-10-07, FB-1): 플랫폼 공통화.
  - event(): summary["journal_path"]가 있으면 해당 파일에 기록한다 (기본 X 경로 유지).
  - write_run_report(): run_reply._write_report 본문을 공통 함수로 이동했다.
    X는 기존 함수 이름(run_reply._write_report)으로 그대로 호출하며 출력은 동일하다.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

VERSION = "1.1.0"

DEFAULT_JOURNAL_PATH = "logs/reply_events.jsonl"

logger = logging.getLogger(__name__)


def new_run() -> dict:
    return {
        "run_id": os.getenv("GITHUB_RUN_ID") or uuid4().hex,
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", "1"),
        "commit_sha": os.getenv("REPLY_RUNTIME_SHA", os.getenv("GITHUB_SHA", "unknown")),
    }


def event(summary: dict, decision: str, tweet_id: str = "", **details) -> None:
    row = {
        **{k: summary.get(k) for k in ("run_id", "run_attempt", "commit_sha", "mode")},
        "observed_at": datetime.now(UTC).isoformat(),
        "decision": decision,
        "tweet_id": tweet_id,
        **details,
    }
    try:
        path = Path(summary.get("journal_path") or DEFAULT_JOURNAL_PATH)
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logging.getLogger(__name__).warning("Reply decision journal write failed")


def write_run_report(summary: dict, guard=None, *, prefix: str = "reply_report") -> None:
    """실행 요약 JSON 리포트 (artifact 업로드 대상) — 실패해도 파이프라인 무영향.
    guard 전달 시 예산 스냅샷 포함 (B-3).
    """
    collected = int(summary.get("collected") or 0)
    processed = int(summary.get("processed", collected) or 0)
    candidates = int(summary.get("candidates") or 0)
    classified_pass = int(summary.get("classified_pass") or 0)
    published = int(summary.get("actual_published", summary.get("published")) or 0)
    if summary.get("mode") in {"dry_run", "shadow"}:
        published = 0
    summary["funnel"] = {
        "candidate_rate": round(candidates / processed, 4) if processed else 0.0,
        "classification_pass_rate": (round(classified_pass / candidates, 4) if candidates else 0.0),
        "publish_rate_of_processed": round(published / processed, 4) if processed else 0.0,
        "publish_rate_of_collected": (
            round(published / collected, 4)
            if collected and not summary.get("recovered_failures")
            else None
        ),
        "publish_rate_of_pass": round(published / classified_pass, 4) if classified_pass else 0.0,
    }
    review = summary.get("review", [])
    summary["cohorts"] = {
        origin: {
            "reviewed": sum(r.get("origin") == origin for r in review),
            "published": sum(r.get("origin") == origin and r.get("result") in {
                "PUBLISHED", "PUBLISHED_DB_UNCONFIRMED"
            } for r in review),
            "simulated": sum(r.get("origin") == origin and r.get("result") == "SIMULATED"
                             for r in review),
        } for origin in ("new", "recovered")
    }
    summary["finished_at"] = datetime.now(UTC).isoformat()
    if guard is not None:
        summary["budget"] = guard.snapshot()
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"{prefix}_{stamp}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        logger.info(f"[Report] 리포트 저장: {path}")
    except Exception as exc:
        logger.warning(f"[Report] 리포트 저장 실패 (무시): {exc}")
