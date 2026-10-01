from __future__ import annotations

import json

from reply_engine import db_audit
from scripts import check_reply_db


def test_live_like_timestamp_is_required_by_optional_contract(monkeypatch):
    def sample(table, columns, _limit):
        if table == "kr_reply_likes" and "liked_at" in columns.split(","):
            raise RuntimeError("liked_at missing")
        return []

    monkeypatch.setattr(db_audit, "_sample", sample)
    optional = db_audit.audit_reply_db()
    assert optional["healthy"]
    assert optional["optional_schema_errors"] == {"kr_reply_likes": "RuntimeError"}
    required = db_audit.audit_reply_db(require_likes=True)
    assert not required["healthy"]
    assert required["schema_errors"] == {"kr_reply_likes": "RuntimeError"}


def test_main_writes_report_summary_and_success_exit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPLY_LIKE_ENABLED", " TRUE ")
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    monkeypatch.setattr(
        check_reply_db,
        "audit_reply_db",
        lambda **kwargs: {
            "healthy": True,
            "rows_checked": 3,
            "truncated": False,
            "issues": {"publish_state_mismatch": 0},
            "require_likes_seen": kwargs["require_likes"],
        },
    )

    assert check_reply_db.main() == 0
    report = json.loads((tmp_path / "logs/reply_db_audit.json").read_text())
    assert report["feature_requirements"] == {"likes": True}
    assert report["require_likes_seen"] is True
    assert report["checked_at"]
    assert "Reply DB audit" in summary_path.read_text()


def test_main_returns_failure_for_unhealthy_report(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(
        check_reply_db,
        "audit_reply_db",
        lambda **_kwargs: {
            "healthy": False,
            "rows_checked": 1,
            "truncated": False,
            "issues": {"publish_state_mismatch": 1},
        },
    )

    assert check_reply_db.main() == 1
