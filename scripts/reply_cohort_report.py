"""Offline deduplicated observation report from retained reply_events JSONL files.

Run: python -m scripts.reply_cohort_report --account-id 123 logs/reply_events.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reply_engine.telemetry import observed_metrics


def rollup(events: list[dict], account_id: str, mode: str = "live") -> dict:
    selected = [r for r in events if r.get("platform") == "x"
                and r.get("account_user_id") == account_id and r.get("mode") == mode]
    decisions = [
        {**r, "reply_tweet_id": r.get("tweet_id"),
         "result": r.get("result") or r.get("decision")}
        for r in selected if r.get("decision") in {
            "DECISION", "ELIGIBLE", "PUBLISHED", "PUBLISHED_DB_UNCONFIRMED", "PUBLISH_RESULT"
        }
    ]
    requests = {r["attempt_id"] for r in selected
                if r.get("decision") == "PUBLISH_REQUEST" and r.get("attempt_id")}
    confirmed = {r["attempt_id"] for r in selected
                 if r.get("decision") in {"PUBLISHED", "PUBLISHED_DB_UNCONFIRMED"}
                 and r.get("attempt_id") in requests}
    return {
        "account_user_id": account_id,
        "mode": mode,
        "metrics": observed_metrics(decisions, mode),
        "scope_metrics": {
            scope: observed_metrics([r for r in decisions if r.get("scope") == scope], mode)
            for scope in ("OWN_ROOT", "FOREIGN_DIRECT", "FOREIGN_OTHER", "UNVERIFIED")
        },
        "requests": len(requests),
        "confirmed_requests": len(confirmed),
        "unconfirmed_requests": len(requests - confirmed),
        "confirmation_rate": round(len(confirmed) / len(requests), 4) if requests else None,
        "limitations": [
            "Retained observations only; missing runs and legacy events are not reconstructed.",
            "Unconfirmed includes incomplete journals, rejections and unknown outcomes.",
            "Quality approval and full-cohort coverage require separate review evidence.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--mode", choices=("live", "shadow", "dry_run"), default="live")
    parser.add_argument("journals", type=Path, nargs="+")
    args = parser.parse_args()
    events = []
    for path in args.journals:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    parser.error("journal entries must be objects")
                events.append(row)
    print(json.dumps(rollup(events, args.account_id, args.mode), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
