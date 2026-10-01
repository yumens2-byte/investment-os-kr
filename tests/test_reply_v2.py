"""Reply v2 behavior regressions: recovery, safety and context without external calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import run_reply
from core import gemini_gateway
from reply_engine import classifier, gate, generator, lang, store, x_client
from reply_engine import filter as filters
from reply_engine.policy import (
    BatchResult,
    classify_publish_error,
    decode_metadata,
    encode_metadata,
)
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet


def tweet(tid="100", text="감사합니다", author="222", conversation="c1", **fields):
    return {
        "id": tid,
        "text": text,
        "author_id": author,
        "conversation_id": conversation,
        "in_reply_to_user_id": "111",
        "created_at": datetime.now(UTC),
        **fields,
    }


def setup_pipeline(monkeypatch, tweets, mode="live"):
    _base_env(monkeypatch, mode)
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    sent = []
    _install_x(monkeypatch, sent)
    monkeypatch.setattr(
        x_client,
        "fetch_mentions",
        lambda *_args: {
            "success": True,
            "tweets": list(tweets),
            "users": {},
            "newest_id": "999" if tweets else None,
            "collection_complete": True,
        },
    )
    return mem, sent


@pytest.mark.parametrize("body", ["👍", "🙏", "❤️", "광고 없이 보기 좋아요", "홍보 안 해서 좋아요"])
def test_safe_short_and_non_promotion_comments_reach_classification(body):
    assert filters.check_tweet(tweet(text=body), None, "111", set()) == (True, None)


@pytest.mark.parametrize("body", ["리딩방 참여", "광고 문의 주세요", "카톡 가입 문의", "수익 보장"])
def test_actual_promotional_comments_stay_blocked(body):
    assert filters.check_tweet(tweet(text=body), None, "111", set())[1] == "SPAM_KEYWORD"


@pytest.mark.parametrize("body", ["별로 어렵지 않네요. 감사합니다", "왜 이렇게 유익해요 👍"])
def test_mixed_or_rhetorical_intent_is_not_hard_rejected(body):
    assert classifier.classify_by_rule(body) is None


@pytest.mark.parametrize("body", ["ありがとう", "谢谢", "Спасибо", "شكرا", "Cảm ơn"])
def test_non_latin_languages_use_safe_foreign_route(body):
    assert lang.is_non_korean(body)


@pytest.mark.parametrize(
    "error,expected",
    [
        (None, "PUBLISH_UNKNOWN"),
        ("timeout", "PUBLISH_UNKNOWN"),
        ("500 internal error", "PUBLISH_UNKNOWN"),
        ("429 Too Many Requests", "PUBLISH_RETRYABLE"),
        ("403 forbidden", "PUBLISH_REJECTED"),
        ("monthly spend cap reached", "SPEND_CAP"),
    ],
)
def test_publish_error_classes_preserve_uncertain_outcomes(error, expected):
    assert classify_publish_error(error) == expected


@pytest.mark.parametrize(
    "body,reason",
    [
        ("감사합니다 😊😊", "GATE_EMOJI"),
        ("오늘도 같이 가보시죠", "GATE_UNSUPPORTED"),
        ("자료 수정했습니다", "GATE_UNSUPPORTED"),
        ("감사합니다\n반가워요", "GATE_FORMAT"),
    ],
)
def test_format_and_unsupported_statements_are_blocked(body, reason):
    assert gate.check_reply(body, [])[1] == reason


def test_question_does_not_take_positive_comment_slot(monkeypatch):
    _, sent = setup_pipeline(
        monkeypatch,
        [
            tweet("q", "언제 발표하나요?"),
            tweet("100", "감사합니다"),
        ],
    )
    result = run_reply.main()
    assert [tid for tid, _ in sent] == ["100"]
    assert result["skip_reasons"]["CLASS_QUESTION"] == 1


def test_failed_gate_does_not_take_next_comment_slot(monkeypatch):
    _, sent = setup_pipeline(monkeypatch, [tweet("bad"), tweet("100")])
    monkeypatch.setattr(
        generator,
        "generate_batch",
        lambda _items: {
            "bad": "매수하세요",
            "100": "저야말로 감사합니다",
        },
    )
    monkeypatch.setattr(generator, "contextual_fallbacks", lambda _item: ())
    result = run_reply.main()
    assert [tid for tid, _ in sent] == ["100"]
    assert result["skip_reasons"]["GATE_BANNED_WORD"] == 1


def test_cap_deferred_comment_is_recovered_next_run(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [tweet("100"), tweet("101", author="333")])
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 1)
    first = run_reply.main()
    assert first["published"] == 1 and first["deferred"] == 1
    saved = dict(mem.history["101"])
    assert saved["skip_reason"] == "RUN_CAP"
    assert decode_metadata(saved["error_message"])["original_created_at"]
    monkeypatch.setattr(store, "get_retryable_history", lambda _limit: [saved])
    monkeypatch.setattr(
        x_client,
        "fetch_mentions",
        lambda *_args: {
            "success": True,
            "tweets": [],
            "users": {},
            "newest_id": None,
        },
    )
    monkeypatch.setattr(filters.store, "history_exists_bulk", lambda _ids: set())
    second = run_reply.main()
    assert second["published"] == 1
    assert [tid for tid, _ in sent] == ["100", "101"]
    assert mem.history["101"]["responded"]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("author_id", "blocked", "BLACKLIST"),
        ("created_at", datetime.now(UTC) - timedelta(hours=25), "EXPIRED"),
        ("in_reply_to_user_id", "999", "OUT_OF_SCOPE"),
    ],
)
def test_recovery_rechecks_static_policy(monkeypatch, field, value, reason):
    mem, sent = setup_pipeline(monkeypatch, [])
    source = tweet("old", **{field: value})
    meta = encode_metadata({**source, "_account_user_id": "111"})
    row = {
        "reply_tweet_id": "old",
        "author_id": source["author_id"],
        "conversation_id": "c1",
        "comment_text": "감사합니다",
        "error_message": meta,
    }
    mem.blacklist.add("blocked")
    monkeypatch.setattr(store, "get_retryable_history", lambda _limit: [row])
    result = run_reply.main()
    assert not sent
    assert result["skip_reasons"][reason] == 1


def test_unknown_publication_is_not_reposted_after_cursor_replay(monkeypatch):
    mem, _ = setup_pipeline(monkeypatch, [tweet()])
    monkeypatch.setattr(x_client, "post_reply", lambda *_args: (None, "timeout"))
    result = run_reply.main()
    assert result["skip_reasons"]["PUBLISH_UNKNOWN"] == 1
    assert mem.history["100"]["skip_reason"] == "PUBLISH_UNKNOWN"
    monkeypatch.setattr(
        filters.store,
        "history_exists_bulk",
        lambda ids: {tid for tid in ids if store._blocked_row(mem.history.get(tid, {}))},
    )
    monkeypatch.setattr(x_client, "post_reply", lambda *_args: pytest.fail("reposted unknown"))
    result = run_reply.main()
    assert result["skip_reasons"]["DUP"] == 1


def test_db_confirmation_failure_leaves_publication_claim_blocking(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [tweet()])
    monkeypatch.setattr(store, "mark_responded", lambda *_args: False)
    alerts = []
    monkeypatch.setattr(run_reply, "send_admin_alert", lambda message: alerts.append(message))
    result = run_reply.main()
    assert len(sent) == 1
    assert result["review"][0]["result"] == "PUBLISHED_DB_UNCONFIRMED"
    assert store._blocked_row(mem.history["100"])
    assert alerts


def test_claim_failure_prevents_external_publication(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [tweet()])
    monkeypatch.setattr(store, "claim_publication", lambda *_args: False)
    result = run_reply.main()
    assert sent == [] and mem.cursor_saved == []
    assert result["skip_reasons"]["PUBLISH_CLAIM_FAIL"] == 1


def test_rule_only_classification_records_no_api_call(monkeypatch):
    monkeypatch.setattr(classifier, "gemini_call", lambda **_args: pytest.fail("unneeded API"))
    result = classifier.classify_batch([{"id": "x", "text": "감사합니다"}])
    assert result["x"] == "POSITIVE" and result.api_calls == 0


def test_partial_duplicate_and_unknown_classifier_ids_are_not_trusted(monkeypatch):
    monkeypatch.setattr(
        classifier,
        "gemini_call",
        lambda **_args: {
            "success": True,
            "api_calls": 2,
            "data": [
                {"id": "x", "label": "POSITIVE"},
                {"id": "x", "label": "SPAM"},
                {"id": "not_requested", "label": "POSITIVE"},
            ],
        },
    )
    result = classifier.classify_batch([{"id": "x", "text": "음 그렇군요"}])
    assert result["x"] == "AMBIGUOUS"
    assert result.unavailable_ids == {"x"} and result.api_calls == 2
    assert "not_requested" not in result


def test_classifier_outage_is_deferred_and_bounded(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [tweet(text="음 그렇군요")])
    result = run_reply.main()
    assert sent == [] and result["deferred"] == 1
    row = mem.history["100"]
    assert row["skip_reason"] == "CLASSIFIER_UNAVAILABLE"
    assert decode_metadata(row["error_message"])["classification_attempts"] == 1


def test_foreign_prompt_preserves_parent_and_root_roles(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        generator,
        "gemini_call",
        lambda **kwargs: (
            captured.update(kwargs)
            or {
                "success": False,
                "data": None,
            }
        ),
    )
    generator.generate_batch(
        [
            {
                **tweet(text="축하해주셔서 감사합니다"),
                "label": "POSITIVE",
                "foreign_thread": True,
                "root_author_id": "other",
                "parent_author_id": "111",
                "parent_text": "축하드립니다",
            }
        ]
    )
    assert '"scope": "FOREIGN_DIRECT"' in captured["prompt"]
    assert '"root_author_id": "other"' in captured["prompt"]
    assert captured["allow_paid"] is False


def test_foreign_thread_missing_parent_identity_is_deferred(monkeypatch):
    _, sent = setup_pipeline(monkeypatch, [tweet()])
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_ENABLED", True)
    monkeypatch.setattr(x_client, "fetch_conversation_roots", lambda *_args: {"c1": "other"})
    result = run_reply.main()
    assert not sent and result["skip_reasons"]["THREAD_UNVERIFIED"] == 1


def test_fallback_intent_language_and_determinism():
    thanks = {**tweet(), "label": "POSITIVE"}
    assert generator.contextual_fallbacks(thanks) == generator.contextual_fallbacks(thanks)
    assert "도움이 됐다니 다행이에요" not in generator.contextual_fallbacks(thanks)
    foreign = {**tweet(text="ありがとう"), "label": "POSITIVE"}
    assert set(generator.contextual_fallbacks(foreign)) == set(generator._POOL_NON_KR)
    laugh = {**tweet(text="ㅋㅋ"), "label": "SUPPORTIVE_NEUTRAL"}
    assert "ㅎㅎ 😄" in generator.contextual_fallbacks(laugh)


def test_shadow_uses_separate_cursor(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [tweet()], "shadow")
    run_reply.main()
    assert sent == [] and all(account == "kr_main:shadow" for account, _, _ in mem.cursor_saved)


def test_paid_gateway_key_is_not_used_by_reply_opt_out(monkeypatch):
    monkeypatch.setattr(gemini_gateway, "_build_keys", lambda: [("pay", "secret", True)])
    monkeypatch.setattr(gemini_gateway, "_get_client", lambda *_args: pytest.fail("paid call"))
    result = gemini_gateway.call("test", allow_paid=False)
    assert result["success"] is False and result["api_calls"] == 0


class MemoryQuery:
    """Stateful storage double that evaluates conditional writes instead of ignoring filters."""

    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.operation = "select"
        self.payload = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters.append(lambda row: row.get(key) == value)
        return self

    def is_(self, key, _value):
        self.filters.append(lambda row: row.get(key) is None)
        return self

    def in_(self, key, values):
        self.filters.append(lambda row: row.get(key) in values)
        return self

    def gte(self, key, value):
        self.filters.append(lambda row: row.get(key, "") >= value)
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def insert(self, payload):
        self.operation, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.operation, self.payload = "update", payload
        return self

    def execute(self):
        if self.operation == "insert":
            key = self.payload["reply_tweet_id"]
            if key in self.rows:
                raise RuntimeError("duplicate key")
            self.rows[key] = dict(self.payload)
            return SimpleNamespace(data=[dict(self.payload)])
        found = [row for row in self.rows.values() if all(f(row) for f in self.filters)]
        if self.operation == "update":
            for row in found:
                row.update(self.payload)
        return SimpleNamespace(data=[dict(row) for row in found])


def install_storage(monkeypatch, rows):
    client = SimpleNamespace(table=lambda _table: MemoryQuery(rows))
    monkeypatch.setattr(store, "get_client", lambda: client)


def test_atomic_claim_is_successful_only_once(monkeypatch):
    rows = {
        "x": {
            "reply_tweet_id": "x",
            "mode": "live",
            "responded": False,
            "response_tweet_id": None,
            "skip_reason": None,
        }
    }
    install_storage(monkeypatch, rows)
    assert store.claim_publication("x", "{}")
    assert store.claim_publication("x", "{}") is None
    assert store.insert_history({**rows["x"], "skip_reason": None}) is False


def test_published_or_live_history_cannot_be_overwritten_by_shadow(monkeypatch):
    rows = {
        "x": {
            "reply_tweet_id": "x",
            "mode": "live",
            "responded": False,
            "response_tweet_id": None,
            "skip_reason": "RUN_CAP",
        }
    }
    install_storage(monkeypatch, rows)
    assert not store.insert_history({**rows["x"], "mode": "shadow"})
    rows["x"].update(responded=True, response_tweet_id="posted")
    assert not store.insert_history({**rows["x"], "responded": False, "response_tweet_id": None})
    assert rows["x"]["response_tweet_id"] == "posted"


@pytest.mark.parametrize(
    "case", ["eligible", "expired", "attempt_limit", "not_due", "unknown", "legacy"]
)
def test_recovery_lookup_respects_original_ttl_attempts_and_due_time(monkeypatch, case):
    original = datetime.now(UTC) - timedelta(hours=25 if case == "expired" else 1)
    meta = decode_metadata(
        encode_metadata({**tweet(created_at=original), "_account_user_id": "111"})
    )
    if case == "attempt_limit":
        meta["publish_attempts"] = 3
    if case == "not_due":
        meta["next_attempt_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    row = {
        "reply_tweet_id": "x",
        "mode": "live",
        "responded": False,
        "response_tweet_id": None,
        "created_at": datetime.now(UTC).isoformat(),
        "skip_reason": "PUBLISH_UNKNOWN" if case == "unknown" else "RUN_CAP",
        "error_message": "old error" if case == "legacy" else json.dumps(meta),
    }
    install_storage(monkeypatch, {"x": row})
    assert bool(store.get_retryable_history()) == (case == "eligible")


def test_metadata_preserves_original_time_across_recovery():
    source = tweet(created_at=datetime.now(UTC) - timedelta(hours=2))
    first = decode_metadata(encode_metadata(source, publish_attempt=True))
    second = decode_metadata(encode_metadata({**source, "_metadata": first}, publish_attempt=True))
    assert first["original_created_at"] == second["original_created_at"]
    assert second["publish_attempts"] == 2


def test_batch_usage_counts_actual_attempts_without_paid_charge_guess():
    result = BatchResult()
    result.record_usage({"api_calls": 3, "paid": False, "usage": {"total_token_count": 42}})
    assert result.api_calls == 3 and result.usage[0]["tokens"]["total_token_count"] == 42


def test_rescan_keeps_retry_attempt_count_instead_of_resetting(monkeypatch):
    original = datetime.now(UTC) - timedelta(hours=2)
    old_meta = decode_metadata(encode_metadata(tweet(created_at=original)))
    old_meta["publish_attempts"] = 2
    row = {
        "reply_tweet_id": "x",
        "mode": "live",
        "responded": False,
        "response_tweet_id": None,
        "skip_reason": "PUBLISH_RETRYABLE",
        "error_message": json.dumps(old_meta),
    }
    install_storage(monkeypatch, {"x": row})
    assert store.insert_history(
        {
            **row,
            "skip_reason": None,
            "error_message": encode_metadata(tweet()),
        }
    )
    assert decode_metadata(row["error_message"])["publish_attempts"] == 2
    claimed = store.claim_publication("x", encode_metadata(tweet(), publish_attempt=True))
    assert decode_metadata(claimed)["publish_attempts"] == 3
    assert decode_metadata(claimed)["original_created_at"] == original.isoformat()


def test_history_save_failure_returns_slot_for_next_safe_comment(monkeypatch):
    mem, sent = setup_pipeline(monkeypatch, [tweet("bad"), tweet("100")])
    save = mem._insert
    monkeypatch.setattr(
        store,
        "insert_history",
        lambda record: False if record["reply_tweet_id"] == "bad" else save(record),
    )
    result = run_reply.main()
    assert [tid for tid, _ in sent] == ["100"]
    assert result["cursor_advanced"] is False


def test_run_cap_includes_unknown_publication_attempts(monkeypatch):
    _, _sent = setup_pipeline(monkeypatch, [tweet("a"), tweet("b", author="333")])
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 1)
    calls = []
    monkeypatch.setattr(x_client, "post_reply", lambda *_args: calls.append(1) or (None, "timeout"))
    result = run_reply.main()
    assert len(calls) == 1 and result["publish_attempts"] == 1
    assert result["skip_reasons"]["RUN_CAP"] == 1


def test_link_review_opt_in_forces_context_classifier(monkeypatch):
    monkeypatch.setattr(filters, "REPLY_LINK_REVIEW_ENABLED", True)
    comment = "좋아요 https://example.test/source"
    assert filters.check_tweet(tweet(text=comment), None, "111", set())[0]
    calls = []
    monkeypatch.setattr(
        classifier,
        "gemini_call",
        lambda **_args: (
            calls.append(1)
            or {
                "success": True,
                "data": [{"id": "x", "label": "SPAM"}],
                "api_calls": 1,
            }
        ),
    )
    result = classifier.classify_batch([{"id": "x", "text": comment}])
    assert calls == [1] and result["x"] == "SPAM"


def test_root_lookup_batches_and_tracks_actual_reads():
    batches = []

    class Client:
        def get_tweets(self, ids, **_args):
            batches.append(ids)
            return SimpleNamespace(data=[SimpleNamespace(id=tid, author_id="111") for tid in ids])

    result = x_client.fetch_conversation_roots(Client(), [str(i) for i in range(230)], max_calls=2)
    assert [len(batch) for batch in batches] == [100, 100]
    assert len(result) == 200 and result.api_calls == 2
    assert "229" not in result


def test_postgrest_sdk_serializes_atomic_claim_filters(monkeypatch):
    import httpx
    from postgrest import SyncPostgrestClient

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[{"reply_tweet_id": "x", "error_message": "{}"}])

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        api = SyncPostgrestClient("https://unit.test/rest/v1", http_client=http)
        monkeypatch.setattr(store, "get_client", lambda: SimpleNamespace(table=api.from_))
        assert store.claim_publication("x", encode_metadata(tweet()))
    write = requests[-1]
    assert write.method == "PATCH"
    assert dict(write.url.params) == {
        "reply_tweet_id": "eq.x",
        "mode": "eq.live",
        "responded": "eq.False",
        "response_tweet_id": "is.null",
        "skip_reason": "is.null",
    }
    assert json.loads(write.content)["skip_reason"] == "PUBLISHING"


def test_unknown_publications_visible_in_audit_without_blocking_other_comments(monkeypatch):
    from reply_engine import db_audit

    row = {
        "reply_tweet_id": "x",
        "mode": "live",
        "responded": False,
        "response_tweet_id": None,
        "skip_reason": "PUBLISH_UNKNOWN",
    }
    monkeypatch.setattr(
        db_audit, "_sample", lambda table, *_args: [row] if table == "kr_reply_history" else []
    )
    result = db_audit.audit_reply_db()
    assert result["healthy"] is True
    assert result["issues"]["unresolved_publications"] == 1
