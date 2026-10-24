"""Evidence, denominators and October congratulations regression; all offline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import run_reply
from reply_engine import classifier, gate, generator, policy, store, telemetry, x_client
from reply_engine.reconciliation import review_publication
from scripts.reply_cohort_report import rollup
from tests.test_reply_v2 import setup_pipeline, tweet


def request_row():
    return {
        "reply_tweet_id": "100", "mode": "live", "responded": False,
        "skip_reason": "PUBLISH_UNKNOWN", "response_text": "저야말로 감사합니다",
        "error_message": json.dumps({
            "reply_policy": "2.1.0", "account_user_id": "111", "target_tweet_id": "100",
            "attempt_id": "attempt1", "requested_at": "2026-10-24T00:00:00+00:00",
            "response_text_sha256": telemetry.text_hash("저야말로 감사합니다"),
        }),
    }


def evidence():
    return {
        "id": "200", "author_id": "111", "text": "저야말로 감사합니다",
        "created_at": "2026-10-24T00:00:02+00:00",
        "referenced_tweets": [{"type": "replied_to", "id": "100"}],
    }


def test_exact_evidence_requires_manual_review_and_never_allows_retry():
    row, proof = request_row(), evidence()
    original = json.dumps([row, proof], sort_keys=True)
    result = review_publication(row, [proof, proof], account_id="111")
    assert result["status"] == "PUBLISHED_EVIDENCE_REVIEW_REQUIRED"
    assert result["response_tweet_id"] == "200"
    assert result["may_retry"] is False
    assert json.dumps([row, proof], sort_keys=True) == original


@pytest.mark.parametrize("change", [
    {"author_id": "222"}, {"text": "違う"}, {"text": "저야말로 감사합니다 "},
    {"referenced_tweets": [{"type": "quoted", "id": "100"}]},
    {"referenced_tweets": [{"type": "replied_to", "id": "999"}]},
    {"created_at": "2026-10-23T23:59:59Z"},
    {"created_at": "2026-10-24T02:00:00Z"}, {"created_at": "2026-10-24T00:00:02"},
])
def test_incomplete_or_mismatched_evidence_never_confirms(change):
    result = review_publication(request_row(), [{**evidence(), **change}], account_id="111")
    assert result["status"] == "UNRESOLVED" and not result["may_retry"]


def test_absence_is_not_proof_and_multiple_replies_are_ambiguous():
    assert review_publication(request_row(), [], account_id="111")["reason"] == (
        "NO_MATCH_IS_NOT_PROOF_OF_ABSENCE"
    )
    result = review_publication(request_row(), [evidence(), {**evidence(), "id": "201"}],
                                account_id="111")
    assert result["reason"] == "AMBIGUOUS_EVIDENCE" and not result["may_retry"]


@pytest.mark.parametrize("change,reason", [
    ({"error_message": "legacy failure"}, "MISSING_REQUEST_EVIDENCE"),
    ({"response_text": "tampered"}, "REQUEST_HASH_MISMATCH"),
    ({"mode": "shadow"}, "NOT_UNRESOLVED_LIVE"),
    ({"responded": True}, "ALREADY_CONFIRMED"),
])
def test_legacy_and_confirmed_rows_cannot_be_released(change, reason):
    result = review_publication({**request_row(), **change}, [evidence()], account_id="111")
    assert result["reason"] == reason and not result["may_retry"]


@pytest.mark.parametrize("comment", ["100화 축하드립니다!", "생일 축하해요", "100회 축하합니다"])
def test_congratulations_direction_is_not_lexical_echo_only(comment):
    assert policy.intent_for(comment, "POSITIVE") == "CONGRATULATIONS"
    assert gate.check_reply("축하합니다 😊", [], comment)[1] == "GATE_ROLE_REVERSAL"
    assert gate.check_reply("축하해 주셔서 감사합니다 😊", [], comment)[0]
    assert set(generator.contextual_fallbacks({"id": "100", "text": comment,
                                             "label": "POSITIVE"})) == set(
        policy.SAFE_POOLS["CONGRATULATIONS"]
    )


def test_thanks_for_congratulations_is_not_congratulating_the_operator():
    assert policy.intent_for("축하해주셔서 감사합니다", "POSITIVE") == "THANKS"


def test_foreign_context_requires_our_direct_parent():
    context = {"foreign_thread": True, "parent_author_id": "222", "_account_user_id": "111"}
    assert gate.check_reply("말씀 고맙습니다", [], "감사합니다", context=context)[1] == (
        "GATE_CONTEXT_UNVERIFIED"
    )
    assert gate.check_reply("말씀 고맙습니다", [], "감사합니다",
                            context={**context, "parent_author_id": "111"})[0]


def test_congratulations_pipeline_replaces_reversed_draft(monkeypatch):
    _, sent = setup_pipeline(monkeypatch, [tweet(text="100화 축하드립니다!")])
    monkeypatch.setattr(classifier, "classify_batch", lambda _: {"100": "POSITIVE"})
    monkeypatch.setattr(generator, "generate_batch", lambda _: {"100": "축하합니다 😊"})
    report = run_reply.main()
    assert report["actual_published"] == 1
    assert report["review"][0]["draft_gate_reason"] == "GATE_ROLE_REVERSAL"
    assert sent[0][1] in policy.SAFE_POOLS["CONGRATULATIONS"]


def test_unknown_publication_keeps_hash_account_and_attempt(monkeypatch):
    mem, _ = setup_pipeline(monkeypatch, [tweet()])
    monkeypatch.setattr(x_client, "post_reply", lambda *_: (None, "RemoteDisconnected"))
    report = run_reply.main()
    row = mem.history["100"]
    meta = policy.decode_metadata(row["error_message"])
    assert row["skip_reason"] == "PUBLISH_UNKNOWN" and not row["responded"]
    assert meta["response_text_sha256"] == telemetry.text_hash(row["response_text"])
    assert meta["attempt_id"] == report["review"][0]["attempt_id"]
    assert meta["claimed_at"] and meta["requested_at"]
    assert meta["account_user_id"] == "111" and meta["target_tweet_id"] == "100"
    assert store._blocked_row(row)


def test_observed_denominator_keeps_caps_and_excludes_self_duplicates():
    rows = [
        {"reply_tweet_id": "1", "eligible": True, "result": "RUN_CAP"},
        {"reply_tweet_id": "1", "eligible": True, "result": "PUBLISHED"},
        {"reply_tweet_id": "2", "eligible": True, "result": "GATE_SIMILARITY"},
        {"reply_tweet_id": "3", "eligible": False, "result": "SELF"},
    ]
    result = telemetry.observed_metrics(rows + rows, "live")
    assert result["unique_observed"] == 3
    assert result["unique_eligible"] == 2 and result["eligible_publish_rate"] == 0.5
    assert telemetry.observed_metrics(rows, "shadow")["eligible_publish_rate"] == 0
    assert telemetry.observed_metrics([], "live")["eligible_publish_rate"] is None


def test_rollup_isolates_accounts_modes_and_deduplicates_artifacts():
    base = {"account_user_id": "111", "mode": "live", "platform": "x", "tweet_id": "100"}
    rows = [
        {**base, "decision": "DECISION", "eligible": True, "result": "PUBLISHED",
         "scope": "OWN_ROOT"},
        {**base, "decision": "PUBLISH_REQUEST", "attempt_id": "a"},
        {**base, "decision": "PUBLISHED", "attempt_id": "a"},
        {**base, "decision": "PUBLISH_REQUEST", "attempt_id": "b"},
    ]
    mixed = rows + rows + [{**r, "account_user_id": "222"} for r in rows]
    mixed += [{**r, "mode": "shadow"} for r in rows]
    mixed += [{**r, "platform": "facebook"} for r in rows]
    result = rollup(mixed, "111")
    assert result["metrics"]["unique_eligible"] == 1
    assert result["requests"] == 2 and result["confirmed_requests"] == 1
    assert result["confirmation_rate"] == 0.5
    assert result["scope_metrics"]["FOREIGN_DIRECT"]["unique_eligible"] == 0


def test_generated_prompt_receives_recent_texts_and_roles(monkeypatch):
    prompts = []
    monkeypatch.setattr(generator, "gemini_call", lambda **kw: (
        prompts.append(kw["prompt"]) or {"success": False}
    ))
    generator.generate_batch([{
        "id": "100", "text": "100화 축하합니다", "label": "POSITIVE",
        "_recent_texts": ["말씀 고맙습니다"], "_account_user_id": "111", "author_id": "222",
    }])
    assert '"recent_response_texts": ["말씀 고맙습니다"]' in prompts[0]
    assert '"account_user_id": "111"' in prompts[0]
    assert '"comment_author_id": "222"' in prompts[0]


def test_pipeline_passes_recent_history_to_generation(monkeypatch):
    setup_pipeline(monkeypatch, [tweet()])
    monkeypatch.setattr(store, "get_recent_response_texts", lambda _: ["댓글 잘 봤어요"])
    received = []
    monkeypatch.setattr(generator, "generate_batch", lambda items: (
        received.extend(items) or {"100": "저야말로 감사합니다"}
    ))
    run_reply.main()
    assert received[0]["_recent_texts"] == ["댓글 잘 봤어요"]
    assert received[0]["_account_user_id"] == "111"


def test_interrupted_run_can_be_counted_without_final_report():
    base = {"account_user_id": "111", "platform": "x", "mode": "live", "tweet_id": "100",
            "eligible": True, "scope": "OWN_ROOT", "attempt_id": "a"}
    events = [{**base, "decision": decision} for decision in (
        "ELIGIBLE", "PUBLISH_REQUEST", "PUBLISHED_DB_UNCONFIRMED"
    )]
    result = rollup(events, "111")
    assert result["metrics"]["eligible_publish_rate"] == 1
    assert result["confirmed_requests"] == 1


def test_journal_does_not_include_comment_or_reply_body(tmp_path):
    path = tmp_path / "nested" / "events.jsonl"
    summary = {"journal_path": str(path), "platform": "x", "account_user_id": "111"}
    telemetry.event(summary, "PUBLISH_REQUEST", "100", response_text_sha256="digest")
    record = json.loads(path.read_text())
    assert record["account_user_id"] == "111" and record["platform"] == "x"
    assert "text" not in record and "reply_text" not in record


def test_offline_cli_reviews_evidence_and_reports_observations(tmp_path):
    root = Path(__file__).resolve().parents[1]
    history_path, evidence_path = tmp_path / "history.json", tmp_path / "evidence.json"
    history_path.write_text(json.dumps([request_row()]))
    evidence_path.write_text(json.dumps([evidence()]))
    result = subprocess.run([
        sys.executable, "-m", "scripts.reply_evidence_review", "--history", str(history_path),
        "--evidence", str(evidence_path), "--account-id", "111",
    ], cwd=root, capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["read_only"] and report["results"][0]["response_tweet_id"] == "200"
    journal = tmp_path / "events.jsonl"
    journal.write_text(json.dumps({
        "decision": "ELIGIBLE", "tweet_id": "100", "eligible": True,
        "platform": "x", "account_user_id": "111", "mode": "live", "scope": "OWN_ROOT",
    }) + "\n")
    result = subprocess.run([
        sys.executable, "-m", "scripts.reply_cohort_report", "--account-id", "111", str(journal),
    ], cwd=root, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["metrics"]["unique_eligible"] == 1
