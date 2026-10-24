"""Offline review of exported unresolved history and X tweet evidence.

Run from repository root: python -m scripts.reply_evidence_review --help
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reply_engine.reconciliation import review_publication
from reply_engine.telemetry import text_hash


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True, help="JSON array of history rows")
    parser.add_argument("--evidence", type=Path, required=True, help="JSON array of X API tweets")
    parser.add_argument("--account-id", required=True)
    args = parser.parse_args()
    history_raw = args.history.read_text(encoding="utf-8")
    evidence_raw = args.evidence.read_text(encoding="utf-8")
    history, evidence = json.loads(history_raw), json.loads(evidence_raw)
    if not isinstance(history, list) or not isinstance(evidence, list):
        parser.error("history and evidence must be JSON arrays")
    if not all(isinstance(r, dict) for r in history + evidence):
        parser.error("every array entry must be an object")
    print(json.dumps({
        "read_only": True,
        "history_sha256": text_hash(history_raw),
        "evidence_sha256": text_hash(evidence_raw),
        "source_authenticity": "operator_must_verify_export",
        "results": [review_publication(r, evidence, account_id=args.account_id) for r in history],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
