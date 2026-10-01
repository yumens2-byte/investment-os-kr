"""Historical replay: bounded read-only context validation and accounted API usage."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reply_engine import classifier, config, generator, store, x_client
from scripts import reply_quality_replay as replay


def setup(monkeypatch, tmp_path, *, parent=True):
    monkeypatch.setattr(replay, "REPORT_PATH", tmp_path / "report.json")
    rows = [{"reply_tweet_id": "100", "author_id": "222", "comment_text": "감사합니다",
             "response_text": "기존 답글"}]
    monkeypatch.setattr(replay, "_samples", lambda: rows)
    row = {"budget_date": "2026-10-02", "read_calls": 0, "write_calls": 0,
           "gemini_calls": 0, "est_cost_krw": 0.0}
    monkeypatch.setattr(store, "get_budget", lambda _: row)
    saved = []
    monkeypatch.setattr(store, "upsert_budget", lambda row: saved.append(dict(row)) or True)
    monkeypatch.setattr(store, "get_recent_response_texts", lambda _: [])
    monkeypatch.setattr(config, "get_my_user_id", lambda: "111")
    monkeypatch.setattr(config, "REPLY_FOREIGN_THREAD_ENABLED", False)
    for name in ("insert_history", "mark_responded", "update_skip_reason", "upsert_cursor",
                 "insert_like"):
        monkeypatch.setattr(store, name, lambda *a, **kw: pytest.fail("forbidden DB mutation"))
    for name in ("post_reply", "post_reply_with_error", "post_like"):
        monkeypatch.setattr(x_client, name, lambda *a, **kw: pytest.fail("forbidden X mutation"))
    actual = SimpleNamespace(id="100", text="감사합니다", author_id="222",
                             conversation_id="c1", in_reply_to_user_id="111",
                             referenced_tweets=[{"type": "replied_to", "id": "p1"}])
    parent_tweet = SimpleNamespace(id="p1", text="시장 자료입니다", author_id="111")

    class Client:
        calls = 0

        def get_tweets(self, **kwargs):
            self.calls += 1
            assert kwargs["ids"] == ["100"]
            assert kwargs["user_auth"] is True
            return SimpleNamespace(
                data=[actual], includes={"tweets": [parent_tweet] if parent else []}
            )

    client = Client()
    monkeypatch.setattr(x_client, "get_x_client", lambda: client)
    monkeypatch.setattr(x_client, "fetch_my_user_id", lambda _: "111")
    monkeypatch.setattr(x_client, "fetch_conversation_roots", lambda *a, **kw: {"c1": "111"})

    def model(**kwargs):
        assert kwargs["allow_paid"] is False
        return {"success": True, "data": [{"id": "100", "reply": "저야말로 감사합니다 😊"}],
                "api_calls": 2, "paid": False, "usage": {"total_token_count": 100}}

    monkeypatch.setattr(generator, "gemini_call", model)
    monkeypatch.setattr(classifier, "gemini_call", lambda **_: pytest.fail("clear positive model"))
    monkeypatch.delenv("X_READ_COST_KRW", raising=False)
    monkeypatch.delenv("X_WRITE_COST_KRW", raising=False)
    return row, saved, client, actual


def test_replay_context_drafts_and_actual_usage(monkeypatch, tmp_path):
    _, saved, client, _ = setup(monkeypatch, tmp_path)
    result = replay.main()
    assert result["success"] and result["generated"] == result["verified"] == 1
    review = result["review"][0]
    assert review["previous_reply"] == "기존 답글"
    assert review["parent_text"] == "시장 자료입니다"
    assert review["context_verified"] and review["result"] == "SIMULATED_PASS"
    assert review["draft"] == review["final_reply"] == "저야말로 감사합니다 😊"
    assert result["budget"]["run_delta"]["read_calls"] == 3
    assert result["budget"]["run_delta"]["gemini_calls"] == 2
    assert saved[-1]["gemini_calls"] == 2 and client.calls == 1
    assert not result["actual_published"] and not result["likes"]
    assert not result["history_changed"] and not result["cursor_changed"]
    assert result["simulated_pass"] == 1 and result["model_verified"]
    assert not result["quality_verified"] and result["human_review_required"]
    assert json.loads(replay.REPORT_PATH.read_text())["success"]


@pytest.mark.parametrize(
    "reason", ["missing_parent", "foreign_root", "wrong_parent", "wrong_author"]
)
def test_unverified_context_never_reaches_generation(reason, monkeypatch, tmp_path):
    _, _, _, actual = setup(monkeypatch, tmp_path, parent=reason != "missing_parent")
    if reason == "foreign_root":
        monkeypatch.setattr(x_client, "fetch_conversation_roots", lambda *a, **kw: {"c1": "999"})
    if reason == "wrong_parent":
        actual.in_reply_to_user_id = "999"
    if reason == "wrong_author":
        actual.author_id = "999"
    monkeypatch.setattr(generator, "generate_batch", lambda _: pytest.fail("unverified generation"))
    result = replay.main()
    assert not result["success"] and result["verified"] == result["generated"] == 0
    assert not result["review"][0]["context_verified"]
    assert replay.REPORT_PATH.exists()


def test_budget_unavailable_saves_failure_report_before_any_external_call(monkeypatch, tmp_path):
    _, _, client, _ = setup(monkeypatch, tmp_path)

    def fail(_):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "get_budget", fail)
    monkeypatch.setattr(x_client, "fetch_my_user_id", lambda _: pytest.fail("external call"))
    result = replay.main()
    assert not result["success"] and "database unavailable" in result["error"]
    assert client.calls == 0 and replay.REPORT_PATH.exists()


def test_budget_persistence_failure_blocks_x_attempt(monkeypatch, tmp_path):
    _, _, client, _ = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "upsert_budget", lambda _: False)
    monkeypatch.setattr(x_client, "fetch_my_user_id", lambda _: pytest.fail("unaccounted call"))
    result = replay.main()
    assert not result["success"] and "persistence failed" in result["error"]
    assert client.calls == 0
    assert result["budget"]["run_delta"]["read_calls"] == 0
    assert result["budget_reservation_unconfirmed"]


def test_daily_read_limit_blocks_replay_before_x(monkeypatch, tmp_path):
    row, _, client, _ = setup(monkeypatch, tmp_path)
    row["read_calls"] = 10**6
    monkeypatch.setattr(x_client, "fetch_my_user_id", lambda _: pytest.fail("over budget"))
    result = replay.main()
    assert not result["success"] and "budget exhausted" in result["error"]
    assert client.calls == 0 and result["budget"]["run_delta"]["read_calls"] == 0


def test_failed_x_attempt_is_accounted_and_saved(monkeypatch, tmp_path):
    _, saved, _, _ = setup(monkeypatch, tmp_path)

    def fail(_):
        raise RuntimeError("429 unavailable")

    monkeypatch.setattr(x_client, "fetch_my_user_id", fail)
    result = replay.main()
    assert not result["success"] and "429" in result["error"]
    assert saved[-1]["read_calls"] == 1
    assert result["budget"]["run_delta"]["read_calls"] == 1


def test_identity_mismatch_does_not_read_or_generate_comments(monkeypatch, tmp_path):
    _, _, client, _ = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "get_my_user_id", lambda: "999")
    result = replay.main()
    assert not result["success"] and "identity mismatch" in result["error"]
    assert client.calls == 0


def test_classifier_outage_cannot_be_reported_as_success(monkeypatch, tmp_path):
    _, _, _, actual = setup(monkeypatch, tmp_path)
    actual.text = "유익하지 않네요"
    monkeypatch.setattr(classifier, "gemini_call", lambda **_: {
        "success": False, "api_calls": 3, "error": "free quota exhausted"
    })
    result = replay.main()
    assert not result["success"] and not result["gate_verified"]
    assert result["generated"] == result["simulated_pass"] == 0
    assert result["review"][0]["result"] == "CLASSIFIER_UNAVAILABLE"
    assert result["budget"]["run_delta"]["gemini_calls"] == 3


def test_all_final_gates_fail_cannot_be_reported_as_success(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    monkeypatch.setattr(generator, "generate_batch", lambda _: {"100": "매수하세요"})
    monkeypatch.setattr(generator, "contextual_fallbacks", lambda _: ())
    result = replay.main()
    assert not result["success"] and result["simulated_pass"] == 0
    assert result["review"][0]["final_reply"] is None


def test_model_outage_with_template_pass_is_explicitly_degraded(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    monkeypatch.setattr(generator, "gemini_call", lambda **kwargs: {
        "success": False, "api_calls": 3, "error": "free quota exhausted"
    })
    result = replay.main()
    assert result["success"] and result["gate_verified"] and result["degraded"]
    assert not result["model_verified"] and not result["quality_verified"]
    assert result["review"][0]["source"] == "TEMPLATE_FALLBACK"
    assert result["budget"]["run_delta"]["gemini_calls"] == 3
