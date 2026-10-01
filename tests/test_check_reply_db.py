from __future__ import annotations

import base64
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


def test_connection_diagnostics_allowlist_jwt_role_and_never_emit_claims(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)
    claims = {"role": "anon", "ref": "privateproject", "email": "hidden@example.test"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    key = f"privateheader.{payload}.privatesignature"
    monkeypatch.setenv("SUPABASE_URL", "https://privateproject.supabase.co")
    monkeypatch.setenv("REPLY_EXPECTED_DB_REF", "privateproject")
    monkeypatch.setenv("SUPABASE_KEY", key)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(
        check_reply_db,
        "audit_reply_db",
        lambda **_kw: {
            "healthy": False,
            "rows_checked": 0,
            "truncated": False,
            "issues": {},
            "schema_errors": {"kr_reply_likes": "APIError"},
            "schema_error_codes": {"kr_reply_likes": "42501"},
        },
    )
    assert check_reply_db.main() == 1
    saved = (tmp_path / "logs/reply_db_audit.json").read_text()
    printed = capsys.readouterr().out
    assert json.loads(saved)["connection_context"] == {
        "expected_project_matches": True,
        "key_kind": "jwt",
        "declared_role": "anon",
    }
    for secret in (key, payload, "privateproject", "privatesignature", "hidden@example.test"):
        assert secret not in saved and secret not in printed


def test_connection_diagnostics_handle_opaque_keys_and_project_mismatch(monkeypatch):
    monkeypatch.setenv("REPLY_EXPECTED_DB_REF", "expectedproject")
    monkeypatch.setenv("SUPABASE_URL", "https://otherproject.supabase.co")
    for key, kind in (("sb_publishable_private", "publishable"), ("sb_secret_private", "secret")):
        monkeypatch.setenv("SUPABASE_KEY", key)
        assert check_reply_db.connection_context() == {
            "expected_project_matches": False,
            "key_kind": kind,
            "declared_role": "unknown",
        }


def test_connection_diagnostics_malformed_or_unrecognized_claims_are_safe(monkeypatch):
    monkeypatch.delenv("REPLY_EXPECTED_DB_REF", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://[invalid")
    monkeypatch.setenv("SUPABASE_KEY", "bad.%%%invalid%%%.signature")
    assert check_reply_db.connection_context() == {
        "expected_project_matches": None,
        "key_kind": "unknown",
        "declared_role": "unknown",
    }
    payload = base64.urlsafe_b64encode(json.dumps({"role": "secret-role-name"}).encode()).decode()
    monkeypatch.setenv("SUPABASE_KEY", f"header.{payload}.signature")
    context = check_reply_db.connection_context()
    assert context["key_kind"] == "jwt" and context["declared_role"] == "other"
    assert "secret-role-name" not in json.dumps(context)
