"""Diagnose DB schema failures without serializing backend exception details."""
from __future__ import annotations

from postgrest.exceptions import APIError

from reply_engine import db_audit


def test_permission_error_is_classified_without_secret_exception_details(monkeypatch):
    def sample(table, _columns, _limit):
        if table == "kr_reply_likes":
            raise APIError({"code": "42501", "message": "private token=secret-marker",
                            "details": None, "hint": None})
        return []

    monkeypatch.setattr(db_audit, "_sample", sample)
    optional = db_audit.audit_reply_db()
    assert optional["healthy"]
    assert optional["optional_schema_error_codes"] == {"kr_reply_likes": "42501"}
    required = db_audit.audit_reply_db(require_likes=True)
    assert not required["healthy"]
    assert required["schema_error_codes"] == {"kr_reply_likes": "42501"}
    assert "secret-marker" not in str(required)


def test_untrusted_backend_code_is_not_copied_into_report(monkeypatch):
    def sample(table, _columns, _limit):
        if table == "kr_reply_likes":
            raise APIError({"code": "secret-marker", "message": "private", 
                            "details": None, "hint": None})
        return []

    monkeypatch.setattr(db_audit, "_sample", sample)
    report = db_audit.audit_reply_db()
    assert report["optional_schema_errors"] == {"kr_reply_likes": "APIError"}
    assert report["optional_schema_error_codes"] == {}
    assert "secret-marker" not in str(report)
