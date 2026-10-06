"""GitHub Actions에서 실행하는 Facebook Reply Engine DB read-only preflight (FB-1).

X용 scripts/check_reply_db.py와 같은 감사 로직(reply_engine.db_audit)을 FB 테이블 계약으로
실행한다. 컬럼 계약은 X 계약을 그대로 미러링한다 (D8: 컬럼명 동일, 테이블명만 fb_reply_*).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from reply_engine.db_audit import REQUIRED_TABLE_CONTRACTS, audit_reply_db
from scripts.check_reply_db import connection_context

VERSION = "1.0.0"

FB_REQUIRED_TABLE_CONTRACTS = {
    table.replace("kr_reply_", "fb_reply_", 1): columns
    for table, columns in REQUIRED_TABLE_CONTRACTS.items()
}
FB_HISTORY_TABLE = "fb_reply_history"


def main() -> int:
    report = audit_reply_db(
        required_contracts=FB_REQUIRED_TABLE_CONTRACTS,
        optional_contracts={},
        history_table=FB_HISTORY_TABLE,
    )
    report["platform"] = "facebook"
    report["connection_context"] = connection_context()
    report["checked_at"] = datetime.now(UTC).isoformat()
    output = Path("logs/fb_reply_db_audit.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    issue_summary = (
        ", ".join(f"{name}={count}" for name, count in report.get("issues", {}).items() if count)
        or "none"
    )
    annotation = "notice" if report["healthy"] else "error"
    print(
        f"::{annotation} title=FB Reply DB audit::healthy={report['healthy']} "
        f"rows={report['rows_checked']} truncated={report['truncated']} issues={issue_summary}"
    )
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("## FB Reply DB audit\n\n")
            summary.write("| healthy | rows checked | truncated | issues |\n")
            summary.write("|---|---:|---|---|\n")
            summary.write(
                f"| {report['healthy']} | {report['rows_checked']} | "
                f"{report['truncated']} | {issue_summary} |\n"
            )
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
