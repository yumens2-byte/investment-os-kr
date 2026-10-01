"""Approved one-run production beta: preflight, dry-run, accounted usage, live <=1."""
from __future__ import annotations

import json
import os
from pathlib import Path

import run_reply
from reply_engine import db_audit, store


def main() -> dict:
    report = {"success": False, "stage": "preflight"}
    try:
        if run_reply.REPLY_RUN_CAP != 1 or run_reply.is_like_enabled():
            raise RuntimeError("Beta requires run cap=1 and likes disabled")
        audit = db_audit.audit_reply_db()
        report["audit"] = audit
        if not audit["healthy"]:
            raise RuntimeError("Production schema/data preflight failed")

        report["stage"] = "dry_run"
        os.environ["REPLY_MODE"] = "dry_run"
        dry = run_reply.main()
        report["dry_run"] = dry
        if not dry["success"]:
            raise RuntimeError(f"Dry-run did not complete: {dry['exit_reason']}")
        if dry.get("actual_published", 0) or dry.get("cursor_advanced"):
            raise RuntimeError("Dry-run side-effect contract violated")

        # Dry-run itself keeps history/cursor/budget read-only. The authorized beta
        # runner separately charges its real API reads before another invocation.
        delta = dry["budget"]["run_delta"]
        row = store.get_budget(store.kst_today())
        for key in ("read_calls", "write_calls", "gemini_calls", "est_cost_krw"):
            row[key] += delta[key]
        if not store.upsert_budget(row):
            raise RuntimeError("Dry-run API usage could not be persisted; live stopped")

        report["stage"] = "live"
        os.environ["REPLY_MODE"] = "live"
        live = run_reply.main()
        report["live"] = live
        if not live["success"]:
            raise RuntimeError(f"Live beta did not complete: {live['exit_reason']}")
        if live.get("publish_attempts", 0) > 1 or live.get("actual_published", 0) > 1:
            raise RuntimeError("Beta publication cap exceeded")
        failures = {k: v for k, v in live["skip_reasons"].items() if v and (
            k.startswith("PUBLISH_") or k in {"DB_CONFIRM_FAIL", "HISTORY_INSERT_FAIL", "SPEND_CAP"}
        )}
        if failures:
            raise RuntimeError(f"Live beta publication/persistence failures: {failures}")
        report["post_audit"] = db_audit.audit_reply_db()
        if not report["post_audit"]["healthy"]:
            raise RuntimeError("Post-beta data audit failed")
        report["success"] = True
        report["stage"] = "completed"
        report["publication_verified"] = live.get("actual_published", 0) == 1
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        path = Path("logs/reply_beta_report.json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report


if __name__ == "__main__":
    result = main()
    print(json.dumps({key: result.get(key) for key in (
        "success", "stage", "publication_verified", "error"
    )}, ensure_ascii=False))
    raise SystemExit(0 if result["success"] else 1)
