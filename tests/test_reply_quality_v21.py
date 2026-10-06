"""October incident regressions. No production DB, model, or X calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from postgrest import SyncPostgrestClient

import run_reply
from core import gemini_gateway as gw
from reply_engine import gate, generator, policy, store, x_client
from scripts import collect_reply, reply_health
from tests.test_reply_v2 import setup_pipeline, tweet


@pytest.mark.parametrize("body", ["👍", "👍👍👍", "👏", "❤️", "🙏", "@owner 👍👍"])
def test_reaction_is_not_an_opinion(body):
    assert policy.intent_for(body, "POSITIVE") == "REACTION"
    assert gate.check_reply("좋은 의견 감사합니다 😊", [], body) == (False, "GATE_INTENT")


@pytest.mark.parametrize(
    "body",
    [
        "잘보고 있어요",
        "잘 보고 있습니다",
        "재미 있게 보고 있습니다^^",
        "즐감하고 있습니다",
        "크 잘보고  있어요 👍👍👍👍",
    ],
)
def test_present_tense_praise(body):
    assert policy.intent_for(body, "POSITIVE") == "PRAISE"


@pytest.mark.parametrize("text", ["돈복사 준비, 기대되네요 😊", "기대되네요", "같은 마음입니다 🙂"])
def test_hype_cannot_be_amplified(text):
    assert gate.check_reply(text, [], "돈복사 준비") == (False, "GATE_MARKET_HYPE")


def test_market_observation_not_blanket_blocked():
    assert policy.intent_for("슈드 잘 가네요 ㅋ", "SUPPORTIVE_NEUTRAL") == "ACK"
    assert gate.check_reply("함께 지켜보시죠 🙂", [], "슈드 잘 가네요 ㅋ")[0]


@pytest.mark.parametrize("save_ok", [True, False])
def test_delay_crossing_ttl_never_publishes(monkeypatch, save_ok):
    now = datetime.now(UTC)
    item = tweet(
        created_at=now
        - timedelta(hours=run_reply.filter_mod.REPLY_MAX_AGE_HOURS)
        + timedelta(seconds=30)
    )
    _, sent = setup_pipeline(monkeypatch, [item])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.current

    Clock.current = now
    monkeypatch.setattr(run_reply, "datetime", Clock)
    monkeypatch.setattr(
        run_reply.time, "sleep", lambda _: setattr(Clock, "current", now + timedelta(seconds=60))
    )
    if not save_ok:
        monkeypatch.setattr(store, "update_skip_reason", lambda *_: False)
    result = run_reply.main()
    assert sent == []
    assert result["review"][0]["result"] == "EXPIRED_BEFORE_SEND"
    if not save_ok:
        assert result["cursor_advanced"] is False
        assert result["review"][0]["persistence_error"] == "EXPIRY_SAVE_FAILED"


@pytest.mark.parametrize("enabled,expected", [("false", 2), ("true", 4)])
def test_urgent_cap_is_opt_in(monkeypatch, enabled, expected):
    item = tweet(
        created_at=datetime.now(UTC) - timedelta(hours=run_reply.filter_mod.REPLY_MAX_AGE_HOURS - 1)
    )
    setup_pipeline(monkeypatch, [item])
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 2)
    monkeypatch.setenv("REPLY_URGENT_DRAIN_ENABLED", enabled)
    monkeypatch.setenv("REPLY_URGENT_RUN_CAP", "4")
    assert run_reply.main()["effective_run_cap"] == expected


@pytest.mark.parametrize("body", ["돈복사 준비", "👍👍👍", "재미 있게 보고 있습니다"])
def test_intent_fallbacks_are_relevant_and_gate_safe(body):
    item = tweet(text=body, label="POSITIVE")
    for text in generator.contextual_fallbacks(item):
        assert gate.check_reply(text, [], body)[0], text


def test_bad_live_draft_replaced_without_losing_publication(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [tweet(text="돈복사 준비")])
    monkeypatch.setattr(run_reply.classifier, "classify_batch", lambda _: {"100": "POSITIVE"})
    monkeypatch.setattr(
        generator, "generate_batch", lambda _: {"100": "돈복사 준비, 기대되네요 😊"}
    )
    result = run_reply.main()
    assert len(sent) == 1 and "기대" not in sent[0][1] and "돈복사" not in sent[0][1]
    assert result["review"][0]["draft_gate_reason"] == "GATE_MARKET_HYPE"
    assert mem.history["100"]["responded"]


def test_fallback_exhaustion_does_not_take_next_slot(monkeypatch):
    _, sent = setup_pipeline(
        monkeypatch, [tweet("bad", "돈복사 준비"), tweet("100", author="other")]
    )
    monkeypatch.setattr(
        run_reply.classifier, "classify_batch", lambda _: {"bad": "POSITIVE", "100": "POSITIVE"}
    )
    monkeypatch.setattr(
        generator, "generate_batch", lambda _: {"bad": "기대되네요", "100": "저야말로 감사합니다"}
    )
    monkeypatch.setattr(generator, "contextual_fallbacks", lambda _: ())
    result = run_reply.main()
    assert [tid for tid, _ in sent] == ["100"]
    assert result["skip_reasons"]["GATE_MARKET_HYPE"] == 1


def _gateway(monkeypatch, errors):
    calls, sleeps = [], []
    monkeypatch.setattr(
        gw,
        "_build_keys",
        lambda: [
            ("main", "secret-main", False),
            ("sub", "secret-sub", False),
            ("pay", "secret-pay", True),
        ],
    )

    def client(key):
        def generate(**_):
            calls.append(key)
            if key in errors:
                raise errors[key]
            return SimpleNamespace(text="[]", usage_metadata=None)

        return SimpleNamespace(models=SimpleNamespace(generate_content=generate))

    monkeypatch.setattr(gw, "_get_client", client)
    monkeypatch.setattr(gw.time, "sleep", sleeps.append)
    return calls, sleeps


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_key_errors_switch_immediately(monkeypatch, status):
    calls, sleeps = _gateway(monkeypatch, {"secret-main": RuntimeError(f"{status} unavailable")})
    result = gw.call("test", allow_paid=False)
    assert result["success"] and result["key_used"] == "sub"
    assert calls == ["secret-main", "secret-sub"] and sleeps == []
    assert result["api_calls"] == 2 and result["attempts"][0]["status"] == status


@pytest.mark.parametrize("status", [400, 422])
def test_bad_payload_does_not_repeat_on_other_keys(monkeypatch, status):
    calls, sleeps = _gateway(monkeypatch, {"secret-main": RuntimeError(f"{status} invalid")})
    result = gw.call("test")
    assert not result["success"] and len(calls) == 1 and not sleeps


def test_transient_retry_has_total_call_budget_and_never_paid(monkeypatch):
    calls, sleeps = _gateway(monkeypatch, {"secret-main": RuntimeError("429 quota")})
    result = gw.call("test", allow_paid=False, max_api_calls=2)
    assert not result["success"] and calls == ["secret-main"] * 2
    assert result["api_calls"] == 2 and len(sleeps) == 1


def test_gateway_diagnostics_do_not_expose_key(monkeypatch, caplog):
    _gateway(monkeypatch, {"secret-main": RuntimeError("400 key secret-main invalid")})
    result = gw.call("test")
    assert "secret-main" not in json.dumps(result) + caplog.text


def _row(tid="1", age=25, reason="RUN_CAP"):
    now = datetime.now(UTC)
    return {
        "reply_tweet_id": tid,
        "mode": "live",
        "responded": False,
        "response_tweet_id": None,
        "skip_reason": reason,
        "created_at": (now - timedelta(hours=age)).isoformat(),
        "error_message": policy.encode_metadata(
            {**tweet(tid, created_at=now - timedelta(hours=age)), "_account_user_id": "111"}
        ),
    }


def _sdk(monkeypatch, handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    api = SyncPostgrestClient("https://unit.test/rest/v1", http_client=http)
    monkeypatch.setattr(store, "get_client", lambda: SimpleNamespace(table=api.from_))
    return http


def test_expiry_cas_includes_publication_and_metadata_guards(monkeypatch):
    row, seen = _row(), []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[row])

    with _sdk(monkeypatch, handler):
        result = store.expire_deferred("111")
    assert result["expired"] == 1
    assert "created_at" not in dict(seen[0].url.params)  # no cutoff hiding old rows
    patch = seen[1]
    params = dict(patch.url.params)
    assert params["responded"] == "eq.false"
    assert params["response_tweet_id"] == "is.null"
    assert params["skip_reason"] == "eq.RUN_CAP"
    assert params["error_message"] == "eq." + row["error_message"]
    payload = json.loads(patch.content)
    assert payload["skip_reason"] == "EXPIRED_CAP"
    assert json.loads(payload["error_message"])["previous_reason"] == "RUN_CAP"
    assert "created_at" not in payload


def test_expiry_concurrent_claim_is_not_overwritten(monkeypatch):
    row = _row()

    def handler(request):
        # Another worker changed RUN_CAP -> PUBLISHING after SELECT.
        return httpx.Response(200, json=[row] if request.method == "GET" else [])

    with _sdk(monkeypatch, handler):
        result = store.expire_deferred("111")
    assert result["expired"] == 0 and result["conflicts"] == 1


@pytest.mark.parametrize("age,expired", [(23.99, 0), (24.01, 1)])
def test_expiry_boundary(monkeypatch, age, expired):
    row = _row(age=age)
    with _sdk(monkeypatch, lambda _: httpx.Response(200, json=[row])):
        assert store.expire_deferred("111")["expired"] == expired


def test_expiry_does_not_touch_other_account(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[_row()])

    with _sdk(monkeypatch, handler):
        assert store.expire_deferred("222")["expired"] == 0
    assert all(r.method == "GET" for r in requests)


def test_expiry_query_excludes_unknown_and_claimed_states(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    with _sdk(monkeypatch, handler):
        store.expire_deferred("111")
    reasons = dict(seen[0].url.params)["skip_reason"]
    assert "PUBLISH_UNKNOWN" not in reasons and "PUBLISHING" not in reasons


def test_collector_insert_conflict_never_overwrites_history(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])  # already durable, DO NOTHING

    with _sdk(monkeypatch, handler):
        assert store.persist_collected([tweet()], {}, "111", "run-test")
    assert len(seen) == 1 and seen[0].method == "POST"
    assert "resolution=ignore-duplicates" in seen[0].headers["prefer"]
    payload = json.loads(seen[0].content)[0]
    assert payload["skip_reason"] == "RECEIVED"
    meta = json.loads(payload["error_message"])
    assert meta["first_seen_at"] and meta["run_id"] == "run-test"


def _collector(monkeypatch, tmp_path, *, persist=True, complete=True):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPLY_ENABLED", "true")
    monkeypatch.setenv("REPLY_COLLECTOR_ENABLED", "true")
    monkeypatch.setenv("X_MY_USER_ID", "111")
    monkeypatch.setattr(
        store,
        "get_budget",
        lambda _: {
            "read_calls": 0,
            "write_calls": 0,
            "gemini_calls": 0,
            "est_cost_krw": 0,
        },
    )
    monkeypatch.setattr(store, "upsert_budget", lambda _: True)
    monkeypatch.setattr(store, "get_cursor", lambda _: {})
    calls = []
    monkeypatch.setattr(store, "persist_collected", lambda *args: calls.append("inbox") or persist)
    monkeypatch.setattr(store, "upsert_cursor", lambda account, *_: calls.append(account) or True)
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client,
        "fetch_mentions",
        lambda *args, **kwargs: {
            "success": True,
            "tweets": [tweet()],
            "users": {},
            "newest_id": "100",
            "collection_complete": complete,
        },
    )
    monkeypatch.setattr(x_client, "post_reply", lambda *_: pytest.fail("collector published"))
    monkeypatch.setattr(generator, "generate_batch", lambda *_: pytest.fail("collector generated"))
    return calls


def test_collection_is_durable_before_cursor(monkeypatch, tmp_path):
    calls = _collector(monkeypatch, tmp_path)
    result = collect_reply.main()
    assert result["success"] and result["actual_published"] == 0
    assert calls == ["inbox", "kr_main", collect_reply.HEARTBEAT]


@pytest.mark.parametrize("persist,complete", [(False, True), (True, False)])
def test_collection_failure_preserves_cursor(monkeypatch, tmp_path, persist, complete):
    calls = _collector(monkeypatch, tmp_path, persist=persist, complete=complete)
    result = collect_reply.main()
    assert not result["success"] and calls == ["inbox"]


def test_collection_disabled_has_no_db_or_x_calls(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REPLY_COLLECTOR_ENABLED", raising=False)
    monkeypatch.setattr(store, "get_budget", lambda _: pytest.fail("disabled DB"))
    assert collect_reply.main()["exit_reason"] == "DISABLED"


def test_collection_budget_failure_stops_before_x(monkeypatch, tmp_path):
    _collector(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "upsert_budget", lambda _: False)
    monkeypatch.setattr(x_client, "fetch_mentions", lambda *a, **kw: pytest.fail("unreserved read"))
    assert collect_reply.main()["exit_reason"] == "BUDGET_SAVE_FAILED"


def test_collected_candidate_is_admitted_by_worker(monkeypatch):
    row = _row(age=1, reason="RECEIVED")
    row.update(author_id="222", conversation_id="c1", comment_text="감사합니다")
    _, sent = setup_pipeline(monkeypatch, [])
    monkeypatch.setattr(store, "get_retryable_history", lambda _: [row])
    result = run_reply.main()
    assert len(sent) == 1 and result["recovered_failures"] == 1


@pytest.mark.parametrize(
    "hours,severity", [(0, "OK"), (5.99, "OK"), (6, "WARNING"), (12, "CRITICAL")]
)
def test_heartbeat_distinguishes_empty_success_from_outage(hours, severity):
    now = datetime.now(UTC)
    assert (
        reply_health.assess_heartbeat(
            {"updated_at": (now - timedelta(hours=hours)).isoformat()}, now=now
        )["severity"]
        == severity
    )


def test_missing_heartbeat_is_unknown():
    assert reply_health.assess_heartbeat(None)["severity"] == "UNKNOWN"


def test_shadow_report_never_counts_simulation_as_publication(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = {
        "mode": "shadow",
        "collected": 1,
        "processed": 1,
        "published": 1,
        "classified_pass": 1,
        "candidates": 1,
        "review": [{"origin": "new", "result": "SIMULATED"}],
    }
    run_reply._write_report(summary)
    assert summary["funnel"]["publish_rate_of_collected"] == 0
    assert summary["cohorts"]["new"] == {"reviewed": 1, "published": 0, "simulated": 1}


def test_new_and_recovered_publications_are_separate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = {
        "mode": "live",
        "collected": 1,
        "processed": 2,
        "published": 2,
        "recovered_failures": 1,
        "classified_pass": 2,
        "review": [
            {"origin": "new", "result": "PUBLISHED"},
            {"origin": "recovered", "result": "PUBLISHED"},
        ],
    }
    run_reply._write_report(summary)
    assert summary["funnel"]["publish_rate_of_collected"] is None
    assert summary["cohorts"]["new"]["published"] == 1
    assert summary["cohorts"]["recovered"]["published"] == 1


def test_metadata_keeps_identity_and_snapshot_datetime():
    now = datetime.now(UTC)
    meta = json.loads(
        policy.encode_metadata(
            {
                **tweet(),
                "_run_id": "run-1",
                "_first_seen_at": now.isoformat(),
                "_user_snapshot": {"created_at": now, "followers": 5},
            }
        )
    )
    assert meta["run_id"] == "run-1" and meta["first_seen_at"] == now.isoformat()
    assert meta["user_snapshot"]["created_at"] == now.isoformat()


def test_operational_workflow_does_not_enable_new_options():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/reply_engine_collect.yml"
    workflow = path.read_text()
    assert "vars.REPLY_COLLECTOR_ENABLED == 'true'" in workflow
    assert "vars.REPLY_RELEASE_SHA" in workflow
    assert "group: reply-engine" in workflow
    assert "needs: test" not in workflow  # collection independent, never publishing
