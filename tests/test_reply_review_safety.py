"""Independent safety review regressions; all service interactions stay local."""

import json
from datetime import UTC, datetime, timedelta

import pytest

import run_reply
from reply_engine import classifier, store, x_client
from reply_engine.policy import BatchResult, decode_metadata, encode_metadata
from tests.test_reply_v2 import install_storage, setup_pipeline, tweet


def test_unknown_publication_reserves_daily_capacity(monkeypatch):
    _, _sent = setup_pipeline(monkeypatch, [tweet("a"), tweet("b", author="333")])
    monkeypatch.setattr(run_reply, "REPLY_DAILY_CAP", 1)
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 2)
    calls = []
    monkeypatch.setattr(
        x_client, "post_reply", lambda *_args: calls.append(1) or (None, "timeout")
    )
    result = run_reply.main()
    assert len(calls) == 1
    assert result["skip_reasons"] == {"PUBLISH_UNKNOWN": 1, "DAILY_CAP": 1}


def test_unknown_publication_reserves_foreign_thread_capacity(monkeypatch):
    _, _sent = setup_pipeline(
        monkeypatch,
        [tweet("a", parent_author_id="111"),
         tweet("b", author="333", parent_author_id="111")],
    )
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_ENABLED", True)
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_RUN_CAP", 1)
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 2)
    monkeypatch.setattr(x_client, "fetch_conversation_roots", lambda *_args: {"c1": "other"})
    calls = []
    monkeypatch.setattr(
        x_client, "post_reply", lambda *_args: calls.append(1) or (None, "timeout")
    )
    result = run_reply.main()
    assert len(calls) == 1
    assert result["skip_reasons"]["FOREIGN_THREAD_CAP"] == 1


def test_confirmed_rejection_returns_daily_capacity(monkeypatch):
    _, _sent = setup_pipeline(monkeypatch, [tweet("a"), tweet("b", author="333")])
    monkeypatch.setattr(run_reply, "REPLY_DAILY_CAP", 1)
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 2)
    calls = []

    def post(*_args):
        calls.append(1)
        return (None, "429 Too Many Requests") if len(calls) == 1 else ("posted-b", None)

    monkeypatch.setattr(x_client, "post_reply", post)
    result = run_reply.main()
    assert len(calls) == 2
    assert result["actual_published"] == 1


def test_recovered_claim_counts_on_attempt_day_and_preserves_original_time(monkeypatch):
    original = datetime.now(UTC) - timedelta(days=1)
    metadata = encode_metadata(tweet(created_at=original))
    rows = {
        "x": {
            "reply_tweet_id": "x", "mode": "live", "responded": False,
            "response_tweet_id": None, "skip_reason": None,
            "created_at": original.isoformat(), "error_message": metadata,
        }
    }
    install_storage(monkeypatch, rows)
    claimed = store.claim_publication("x", metadata)
    assert claimed
    assert rows["x"]["created_at"] >= store.kst_day_start_utc_iso()
    assert decode_metadata(claimed)["original_created_at"] == original.isoformat()
    assert store.claim_publication("x", metadata) is None


@pytest.mark.parametrize(
    "reason", ["PUBLISH_FAIL", "PUBLISH_REJECTED", "SPEND_CAP", "PUBLISH_EXHAUSTED"]
)
def test_terminal_publication_failure_cannot_be_replayed(monkeypatch, reason):
    rows = {
        "x": {
            "reply_tweet_id": "x", "mode": "live", "responded": False,
            "response_tweet_id": None, "skip_reason": reason,
            "created_at": datetime.now(UTC).isoformat(),
        }
    }
    install_storage(monkeypatch, rows)
    assert store.history_exists("x")
    assert "x" in store.history_exists_bulk(["x"])
    assert not store.insert_history({**rows["x"], "skip_reason": None})
    assert store.claim_publication("x", "{}") is None


@pytest.mark.parametrize("metadata_source", ["retry_queue", "history_lookup"])
def test_fresh_mentions_preserve_classifier_retry_counter(monkeypatch, metadata_source):
    source = tweet("x", text="음 그렇군요")
    mem, sent = setup_pipeline(monkeypatch, [source])
    metadata = decode_metadata(encode_metadata({**source, "_account_user_id": "111"}))
    metadata["classification_attempts"] = 2
    row = {
        "reply_tweet_id": "x", "mode": "live", "responded": False,
        "response_tweet_id": None, "skip_reason": "CLASSIFIER_UNAVAILABLE",
        "error_message": json.dumps(metadata),
    }
    if metadata_source == "retry_queue":
        monkeypatch.setattr(store, "get_retryable_history", lambda _limit: [row])
    else:
        lookup = store.HistoryLookup()
        lookup.rows["x"] = row
        monkeypatch.setattr(store, "history_exists_bulk", lambda _ids: lookup)
    unavailable = BatchResult()
    unavailable["x"] = "AMBIGUOUS"
    unavailable.unavailable_ids.add("x")
    monkeypatch.setattr(classifier, "classify_batch", lambda _items: unavailable)
    result = run_reply.main()
    assert not sent
    assert result["skip_reasons"]["CLASSIFIER_EXHAUSTED"] == 1
    assert decode_metadata(mem.history["x"]["error_message"])["classification_attempts"] == 3


def test_failed_publication_status_save_keeps_claim_blocked_and_cursor(monkeypatch):
    mem, _sent = setup_pipeline(monkeypatch, [tweet("x")])
    monkeypatch.setattr(x_client, "post_reply", lambda *_args: (None, "429 Too Many Requests"))
    monkeypatch.setattr(store, "update_skip_reason", lambda *_args: False)
    result = run_reply.main()
    assert result["cursor_advanced"] is False
    assert mem.history["x"]["skip_reason"] == "PUBLISHING"
    assert store._blocked_row(mem.history["x"])
