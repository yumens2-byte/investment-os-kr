"""GitHub Actions에서 실행하는 Reply Engine DB read-only preflight."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from reply_engine.db_audit import audit_reply_db


def main() -> int:
    require_likes = os.getenv("REPLY_LIKE_ENABLED", "").strip().lower() == "true"
    report = audit_reply_db(require_likes=require_likes)
    report["feature_requirements"] = {"likes": require_likes}
    report["checked_at"] = datetime.now(UTC).isoformat()
    output = Path("logs/reply_db_audit.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    annotation = "notice" if report["healthy"] else "error"
    issue_summary = ", ".join(
        f"{name}={count}" for name, count in report.get("issues", {}).items() if count
    ) or "none"
    print(
        f"::{annotation} title=Reply DB audit::healthy={report['healthy']} "
        f"rows={report['rows_checked']} truncated={report['truncated']} issues={issue_summary}"
    )
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("## Reply DB audit\n\n")
            summary.write("| healthy | rows checked | truncated | issues |\n")
            summary.write("|---|---:|---|---|\n")
            summary.write(
                f"| {report['healthy']} | {report['rows_checked']} | "
                f"{report['truncated']} | {issue_summary} |\n"
            )
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
