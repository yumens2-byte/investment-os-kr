"""Per-run append-only decision journal; no credentials or comment bodies."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


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
        path = Path("logs/reply_events.jsonl")
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logging.getLogger(__name__).warning("Reply decision journal write failed")
