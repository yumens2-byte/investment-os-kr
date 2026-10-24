"""Upgrade integration: real pipeline, PostgREST/Tweepy SDKs, offline transports."""

from __future__ import annotations

import json
import socket
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

import run_reply
from reply_engine import policy, telemetry
from reply_engine.reconciliation import review_publication
from scripts.reply_cohort_report import rollup

pytest_plugins = ["tests.test_reply_sdk_integration"]


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Integration tests must not open network sockets")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def journal():
    return [json.loads(line) for line in Path("logs/reply_events.jsonl").read_text().splitlines()]


def test_live_sdk_publication_links_claim_event_report_and_rollup(services):
    services.add_mention("500")
    result = run_reply.main()
    row = services.tables["kr_reply_history"][0]
    meta = policy.decode_metadata(row["error_message"])
    events = journal()
    request = next(e for e in events if e["decision"] == "PUBLISH_REQUEST")
    confirmed = next(e for e in events if e["decision"] == "PUBLISHED")
    assert meta["attempt_id"] == request["attempt_id"] == confirmed["attempt_id"]
    assert request["response_text_sha256"] == telemetry.text_hash(row["response_text"])
    assert confirmed["response_id"] == row["response_tweet_id"] == "901"
    assert result["observed_metrics"]["eligible_publish_rate"] == 1
    aggregated = rollup(events + events, "111")
    assert aggregated["requests"] == aggregated["confirmed_requests"] == 1
    assert aggregated["metrics"] == result["observed_metrics"]
    report = json.loads(next(Path("logs").glob("reply_report_*.json")).read_text())
    assert report["review"][0]["attempt_id"] == request["attempt_id"]


def test_timeout_sdk_to_evidence_review_and_second_run_never_reposts(services):
    services.add_mention("500")
    services.timeout = True
    result = run_reply.main()
    row = services.tables["kr_reply_history"][0]
    assert row["skip_reason"] == "PUBLISH_UNKNOWN"
    meta = policy.decode_metadata(row["error_message"])
    proof = {
        "id": "999", "author_id": "111", "text": row["response_text"],
        "created_at": (datetime.fromisoformat(meta["requested_at"]) + timedelta(seconds=1)
                       ).isoformat(),
        "referenced_tweets": [{"type": "replied_to", "id": "500"}],
    }
    before = deepcopy(row)
    assert review_publication(row, [proof], account_id="111")["status"] == (
        "PUBLISHED_EVIDENCE_REVIEW_REQUIRED"
    )
    assert not review_publication(row, [], account_id="111")["may_retry"]
    assert row == before
    services.timeout = False
    run_reply.main()
    assert services.post_count == 1
    assert result["transport_metrics"]["unknown"] == 1
    assert rollup(journal(), "111")["confirmed_requests"] == 0


@pytest.mark.parametrize("mode", ["shadow", "dry_run"])
def test_nonlive_sdk_modes_produce_no_publication_evidence(services, monkeypatch, mode):
    monkeypatch.setenv("REPLY_MODE", mode)
    services.add_mention("500")
    result = run_reply.main()
    assert services.post_count == result["actual_published"] == 0
    assert not any(e["decision"] == "PUBLISH_REQUEST" for e in journal())
    assert rollup(journal(), "111", mode)["confirmed_requests"] == 0
    assert rollup(journal(), "111")["metrics"]["unique_eligible"] == 0
    if mode == "dry_run":
        assert not any(r.method in {"POST", "PATCH", "DELETE"} for r in services.db_requests)


def test_sdk_db_confirmation_failure_preserves_confirmed_transport(services, monkeypatch):
    services.add_mention("500")
    original = services.database_http

    def fail_confirmation(request):
        if request.method == "PATCH" and json.loads(request.content).get("responded") is True:
            return httpx.Response(503, json={"message": "injected DB outage"})
        return original(request)

    # Inject a transport error while keeping the real store update/retry path.
    from db import supabase_client
    api = supabase_client._client.table.__self__
    monkeypatch.setattr(api.session._transport, "handler", fail_confirmation)
    result = run_reply.main()
    assert result["review"][0]["result"] == "PUBLISHED_DB_UNCONFIRMED"
    assert result["transport_metrics"]["confirmation_rate"] == 1
    assert rollup(journal(), "111")["confirmed_requests"] == 1
    assert services.tables["kr_reply_history"][0]["skip_reason"] == "PUBLISHING"
    assert services.post_count == 1


def test_congratulations_model_to_fallback_through_real_sdks(services, monkeypatch):
    services.add_mention("500", text="100화 축하드립니다!")
    original = services.generate

    def reversed_draft(*, contents, **kwargs):
        result = original(contents=contents, **kwargs)
        if any(line.startswith('{"id":') for line in contents.splitlines()):
            result.text = json.dumps([{"id": "500", "reply": "축하합니다 😊"}])
        return result

    monkeypatch.setattr(services, "generate", reversed_draft)
    result = run_reply.main()
    row = services.tables["kr_reply_history"][0]
    assert row["response_text"] in policy.SAFE_POOLS["CONGRATULATIONS"]
    assert result["review"][0]["draft_gate_reason"] == "GATE_ROLE_REVERSAL"
    assert result["actual_published"] == 1


def test_interrupted_final_report_still_rolls_up_sdk_events(services, monkeypatch):
    services.add_mention("500")
    monkeypatch.setattr(run_reply, "_write_report", lambda *_args: None)
    run_reply.main()
    assert not list(Path("logs").glob("reply_report_*.json"))
    result = rollup(journal(), "111")
    assert result["metrics"]["unique_eligible"] == 1
    assert result["confirmed_requests"] == 1
