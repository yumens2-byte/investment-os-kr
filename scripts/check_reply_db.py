"""GitHub Actions에서 실행하는 Reply Engine DB read-only preflight."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from reply_engine.db_audit import audit_reply_db


def main() -> int:
    report = audit_reply_db()
    report["checked_at"] = datetime.now(UTC).isoformat()
    output = Path("logs/reply_db_audit.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
