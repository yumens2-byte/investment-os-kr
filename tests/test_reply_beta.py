"""Production beta orchestration and review follow-up resource regressions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import run_reply
from reply_engine import generator, store
from reply_engine.policy import decode_metadata, encode_metadata
from scripts import reply_operational_beta as beta
from tests.test_reply_v2 import setup_pipeline, tweet


def install_beta(monkeypatch, tmp_path, live=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 1)
    monkeypatch.setattr(run_reply, "is_like_enabled", lambda: False)
    monkeypatch.setattr(beta.db_audit, "audit_reply_db", lambda: {"healthy": True})
    dry = {
        "success": True, "exit_reason": "EXIT_OK", "cursor_advanced": False,
        "actual_published": 0,
        "budget": {"run_delta": {
            "read_calls": 2, "write_calls": 0, "gemini_calls": 1, "est_cost_krw": 20.0,
        }},
    }
    live = live or {"success": True, "actual_published": 1, "publish_attempts": 1,
                    "skip_reasons": {}}
    modes = []

    def run():
        import os
        mode = os.environ["REPLY_MODE"]
        modes.append(mode)
        return dry if mode == "dry_run" else live

    monkeypatch.setattr(run_reply, "main", run)
    row = {"read_calls": 3, "write_calls": 1, "gemini_calls": 2, "est_cost_krw": 30.0}
    monkeypatch.setattr(store, "get_budget", lambda _day: dict(row))
    saved = []
    monkeypatch.setattr(store, "upsert_budget", lambda data: saved.append(dict(data)) or True)
    return modes, saved


def test_beta_charges_dry_run_reads_before_one_live_attempt(monkeypatch, tmp_path):
    modes, saved = install_beta(monkeypatch, tmp_path)
    result = beta.main()
    assert result["success"] and result["publication_verified"]
    assert modes == ["dry_run", "live"]
    assert saved == [{"read_calls": 5, "write_calls": 1, "gemini_calls": 3, "est_cost_krw": 50.0}]
    assert (tmp_path / "logs/reply_beta_report.json").exists()


def test_beta_stops_before_live_if_usage_cannot_be_saved(monkeypatch, tmp_path):
    modes, _ = install_beta(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "upsert_budget", lambda _row: False)
    result = beta.main()
    assert not result["success"] and modes == ["dry_run"]


@pytest.mark.parametrize("failure", ["PUBLISH_UNKNOWN", "DB_CONFIRM_FAIL", "HISTORY_INSERT_FAIL"])
def test_beta_reports_failure_even_when_pipeline_exits_ok(monkeypatch, tmp_path, failure):
    install_beta(monkeypatch, tmp_path, {
        "success": True, "actual_published": 0, "publish_attempts": 1,
        "skip_reasons": {failure: 1},
    })
    assert not beta.main()["success"]


def test_beta_no_eligible_comment_does_not_claim_publication_verified(monkeypatch, tmp_path):
    install_beta(monkeypatch, tmp_path, {
        "success": True, "actual_published": 0, "publish_attempts": 0, "skip_reasons": {},
    })
    result = beta.main()
    assert result["success"] and not result["publication_verified"]


def test_budget_read_failure_prevents_any_external_call(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPLY_ENABLED", "true")
    monkeypatch.setenv("REPLY_MODE", "dry_run")
    monkeypatch.setattr(store, "get_budget", lambda _day: (_ for _ in ()).throw(RuntimeError("DB")))
    monkeypatch.setattr(run_reply.x_client, "get_x_client", lambda: pytest.fail("external call"))
    assert run_reply.main()["exit_reason"] == "EXIT_BUDGET_UNAVAILABLE"


def test_store_budget_missing_is_zero_but_read_exception_is_not(monkeypatch):
    class Query:
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def limit(self, *_args): return self
        def execute(self): raise RuntimeError("DB offline")

    monkeypatch.setattr(store, "get_client", lambda: SimpleNamespace(table=lambda _name: Query()))
    with pytest.raises(RuntimeError, match="budget could not be loaded"):
        store.get_budget(store.kst_today())


def test_capped_candidates_do_not_generate_unused_replies(monkeypatch):
    _, sent = setup_pipeline(monkeypatch, [tweet(str(i), author=f"a{i}") for i in range(15)])
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 1)
    calls = []
    original = generator.generate_batch
    monkeypatch.setattr(
        generator, "generate_batch", lambda items: calls.append(len(items)) or original(items)
    )
    result = run_reply.main()
    assert calls == [1] and len(sent) == 1 and result["skip_reasons"]["RUN_CAP"] == 14


def test_recovery_overfetch_does_not_hide_eligible_row_behind_exhausted_rows(monkeypatch):
    import httpx
    from postgrest import SyncPostgrestClient

    from tests.test_reply_sdk_integration import OfflineServices
    now = datetime.now(UTC)
    exhausted = []
    for i in range(100):
        meta = decode_metadata(encode_metadata(tweet(str(i))))
        meta["publish_attempts"] = 3
        import json
        exhausted.append({
            "reply_tweet_id": str(i), "mode": "live", "responded": False,
            "response_tweet_id": None, "skip_reason": "PUBLISH_RETRYABLE",
            "created_at": (now - timedelta(hours=1)).isoformat(),
            "error_message": json.dumps(meta),
        })
    eligible = {
        "reply_tweet_id": "eligible", "mode": "live", "responded": False,
        "response_tweet_id": None, "skip_reason": "RUN_CAP",
        "created_at": now.isoformat(), "error_message": encode_metadata(tweet("eligible")),
    }
    services = OfflineServices()
    services.tables["kr_reply_history"] = exhausted + [eligible]
    with httpx.Client(transport=httpx.MockTransport(services.database_http)) as http:
        api = SyncPostgrestClient("https://unit.test/rest/v1", http_client=http)
        monkeypatch.setattr(store, "get_client", lambda: SimpleNamespace(table=api.from_))
        assert [row["reply_tweet_id"] for row in store.get_retryable_history(100)] == ["eligible"]
    assert dict(services.db_requests[0].url.params)["limit"] == "500"


def test_replay_of_expired_published_comment_does_not_stall_new_cursor(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [
        tweet("100", created_at=datetime.now(UTC) - timedelta(hours=25)),
        tweet("101", author="other"),
    ])
    mem.history["100"] = {
        "reply_tweet_id": "100", "responded": True, "response_tweet_id": "existing",
    }
    result = run_reply.main()
    assert result["skip_reasons"]["DUP"] == 1
    assert result["cursor_advanced"] and [tid for tid, _ in sent] == ["101"]
    assert mem.history["100"]["response_tweet_id"] == "existing"
