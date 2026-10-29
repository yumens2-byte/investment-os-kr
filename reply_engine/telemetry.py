"""Per-run append-only decision journal and run report; no credentials or comment bodies.

v1.1.0 (2026-10-07, FB-1): 플랫폼 공통화.
  - event(): summary["journal_path"]가 있으면 해당 파일에 기록한다 (기본 X 경로 유지).
  - write_run_report(): run_reply._write_report 본문을 공통 함수로 이동했다.
    X는 기존 함수 이름(run_reply._write_report)으로 그대로 호출하며 출력은 동일하다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

VERSION = "1.1.0"

DEFAULT_JOURNAL_PATH = "logs/reply_events.jsonl"

logger = logging.getLogger(__name__)


def text_hash(text: str) -> str:
    """Hash exact UTF-8 text, without lossy normalization."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_attempt_id() -> str:
    return uuid4().hex


def scope_for(tweet: dict, account_id: str) -> str:
    root = str(tweet.get("root_author_id") or "")
    if not root:
        return "UNVERIFIED"
    if root == account_id:
        return "OWN_ROOT"
    if str(tweet.get("parent_author_id") or "") == account_id:
        return "FOREIGN_DIRECT"
    return "FOREIGN_OTHER"


def observed_metrics(review: list[dict], mode: str) -> dict:
    """Count unique IDs in supplied observations, not a complete cohort.

    Eligible means static/scope checks AND classification passed. Cap/gate
    failures stay in the denominator. Duplicates cannot inflate the rate.
    """
    grouped: dict[str, list[dict]] = {}
    for row in review:
        tid = str(row.get("reply_tweet_id") or "")
        if tid:
            grouped.setdefault(tid, []).append(row)
    eligible = {
        tid for tid, rows in grouped.items() if any(r.get("eligible") is True for r in rows)
    }
    confirmed = {
        tid for tid, rows in grouped.items()
        if mode == "live" and any(r.get("result") in {
            "PUBLISHED", "PUBLISHED_DB_UNCONFIRMED"
        } for r in rows)
    }
    return {
        "coverage": "supplied_observations_only",
        "unique_observed": len(grouped),
        "unique_eligible": len(eligible),
        "unique_confirmed_eligible": len(confirmed & eligible),
        "eligible_publish_rate": (
            round(len(confirmed & eligible) / len(eligible), 4) if eligible else None
        ),
    }


def quality_metrics(review: list[dict], mode: str) -> dict:
    """Selection metrics are observations, never a semantic accuracy score.

    Only rows actually gated enter the denominator. Duplicate report rows do
    not inflate rates; latest evaluated observation per comment wins.
    """
    evaluated = {
        str(row["reply_tweet_id"]): row for row in review
        if row.get("reply_tweet_id") and row.get("quality_evaluated") is True
    }
    rows = list(evaluated.values())
    selected = [r for r in rows if r.get("result") in {
        "PUBLISHED", "PUBLISHED_DB_UNCONFIRMED", "SIMULATED"
    }]
    actual = [r for r in selected if mode == "live" and r.get("result") != "SIMULATED"]
    simulated = [r for r in selected if r.get("result") == "SIMULATED"]
    def counts(items, field):
        result = {}
        for row in items:
            key = row.get(field)
            if key:
                result[key] = result.get(key, 0) + 1
        return result
    return {
        "coverage": "unique_evaluated_observations_only",
        "evaluated": len(rows), "selected": len(selected),
        "rejected_first_drafts": sum(bool(r.get("draft_gate_reason")) for r in rows),
        "draft_gate_reasons": counts(rows, "draft_gate_reason"),
        "actual_sources": counts(actual, "source"),
        "simulated_sources": counts(simulated, "source"),
        "template_selection_rate": (
            round(sum(r.get("source") == "TEMPLATE_FALLBACK" for r in selected)
                  / len(selected), 4) if selected else None
        ),
        "human_review_required": True,
        "semantic_quality_verified": False,
    }


def new_run() -> dict:
    return {
        "run_id": os.getenv("GITHUB_RUN_ID") or uuid4().hex,
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", "1"),
        "commit_sha": os.getenv("REPLY_RUNTIME_SHA", os.getenv("GITHUB_SHA", "unknown")),
    }


def event(summary: dict, decision: str, tweet_id: str = "", **details) -> None:
    row = {
        **{k: summary.get(k) for k in (
            "run_id", "run_attempt", "commit_sha", "mode", "platform", "account_user_id"
        )},
        "observed_at": datetime.now(UTC).isoformat(),
        "decision": decision,
        "tweet_id": tweet_id,
        **details,
    }
    try:
        path = Path(summary.get("journal_path") or DEFAULT_JOURNAL_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
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
    # No comment bodies in the append-only journal. Retained artifact snapshots
    # can be deduplicated offline; they do not assert complete cohort coverage.
    for row in review:
        event(summary, "DECISION", str(row.get("reply_tweet_id") or ""),
              origin=row.get("origin"), scope=row.get("scope", "UNVERIFIED"),
              eligible=row.get("eligible"), result=row.get("result"),
              source=row.get("source"), draft_gate_reason=row.get("draft_gate_reason"))
    mode = summary.get("mode", "unknown")
    summary["quality_metrics"] = quality_metrics(review, mode)
    summary["observed_metrics"] = observed_metrics(review, mode)
    summary["scope_metrics"] = {
        scope: observed_metrics([r for r in review if r.get("scope") == scope], mode)
        for scope in ("OWN_ROOT", "FOREIGN_DIRECT", "FOREIGN_OTHER", "UNVERIFIED")
    }
    attempts = int(summary.get("publish_attempts") or 0)
    summary["transport_metrics"] = {
        "requests": attempts,
        "confirmed": published,
        "unknown": sum(r.get("result") == "PUBLISH_UNKNOWN" for r in review),
        "confirmation_rate": round(published / attempts, 4) if attempts else None,
    }
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
