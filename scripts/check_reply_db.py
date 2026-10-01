"""GitHub Actions에서 실행하는 Reply Engine DB read-only preflight."""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from reply_engine.db_audit import audit_reply_db


def connection_context() -> dict:
    """Expose only an endpoint match and allowlisted credential classifications.

    JWT claims are decoded locally without signature verification. A declared role
    is diagnostic metadata, not proof of the role authenticated by PostgREST.
    Neither the endpoint, project reference, key nor other JWT claims are returned.
    """
    expected = os.getenv("REPLY_EXPECTED_DB_REF", "").strip()
    endpoint = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    matches = None
    if expected:
        try:
            hostname = urlparse(endpoint).hostname or ""
            matches = hostname.split(".", 1)[0] == expected
        except ValueError:
            matches = False

    kind, role = "unknown", "unknown"
    if key.startswith("sb_publishable_"):
        kind = "publishable"
    elif key.startswith("sb_secret_"):
        kind = "secret"
    elif len(key) <= 16384 and len(key.split(".")) == 3:
        try:
            payload = key.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if isinstance(claims, dict):
                kind = "jwt"
                declared = claims.get("role")
                role = declared if declared in ("anon", "service_role") else "other"
        except (ValueError, TypeError, UnicodeError):
            pass
    return {"expected_project_matches": matches, "key_kind": kind, "declared_role": role}


def main() -> int:
    require_likes = os.getenv("REPLY_LIKE_ENABLED", "").strip().lower() == "true"
    report = audit_reply_db(require_likes=require_likes)
    report["connection_context"] = connection_context()
    report["feature_requirements"] = {"likes": require_likes}
    report["checked_at"] = datetime.now(UTC).isoformat()
    output = Path("logs/reply_db_audit.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    annotation = "notice" if report["healthy"] else "error"
    issue_summary = (
        ", ".join(f"{name}={count}" for name, count in report.get("issues", {}).items() if count)
        or "none"
    )
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
