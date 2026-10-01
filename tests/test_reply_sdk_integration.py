"""Offline integration: real pipeline and SDKs, only transport boundaries replaced."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import requests
import tweepy
from postgrest import SyncPostgrestClient

import run_reply
from core import gemini_gateway
from db import supabase_client
from reply_engine import store
from reply_engine.policy import decode_metadata, encode_metadata


class OfflineServices:
    """Stateful HTTP fixtures with PostgREST filters and real Tweepy response parsing."""

    def __init__(self):
        self.tables = {}
        self.db_requests = []
        self.x_requests = []
        self.model_requests = []
        self.mentions = []
        self.timeout = False
        self.classifier_unavailable = False

    @staticmethod
    def matches(row, params):
        for name, expression in params.items():
            if name in {"select", "order", "limit", "offset", "on_conflict"}:
                continue
            if name == "or":
                if not (
                    row.get("responded")
                    or row.get("skip_reason")
                    in {"PUBLISHING", "PUBLISH_UNKNOWN", "DB_CONFIRM_FAIL"}
                ):
                    return False
                continue
            op, value = expression.split(".", 1)
            actual = row.get(name)
            if op == "eq":
                expected = value.lower() if isinstance(actual, bool) else value
                normalized = str(actual).lower() if isinstance(actual, bool) else str(actual)
                if normalized != expected:
                    return False
            elif op == "is" and value == "null" and actual is not None:
                return False
            elif op == "in" and str(actual) not in value.strip("()").replace('"', "").split(","):
                return False
            elif op == "gte" and str(actual or "") < value:
                return False
        return True

    def database_http(self, request):
        self.db_requests.append(request)
        table = request.url.path.rsplit("/", 1)[-1]
        all_rows = self.tables.setdefault(table, [])
        params = dict(request.url.params)
        rows = [row for row in all_rows if self.matches(row, params)]
        if request.method == "POST":
            body = json.loads(request.content)
            bodies = body if isinstance(body, list) else [body]
            primary = {
                "kr_reply_history": "reply_tweet_id",
                "kr_reply_cursor": "account",
                "kr_reply_budget": "budget_date",
            }.get(table, "reply_tweet_id")
            rows = []
            for body in bodies:
                old = next((r for r in all_rows if r.get(primary) == body.get(primary)), None)
                if old is not None and "resolution=merge-duplicates" in request.headers.get(
                    "Prefer", ""
                ):
                    old.update(body)
                    rows.append(old)
                elif old is not None:
                    return httpx.Response(409, json={"message": "duplicate key", "code": "23505"})
                else:
                    row = {"created_at": datetime.now(UTC).isoformat(), **body}
                    all_rows.append(row)
                    rows.append(row)
        elif request.method == "PATCH":
            for row in rows:
                row.update(json.loads(request.content))
        if params.get("order"):
            name, _, direction = params["order"].partition(".")
            rows = sorted(rows, key=lambda r: str(r.get(name) or ""), reverse=direction == "desc")
        total = len(rows)
        rows = rows[int(params.get("offset", 0)) :]
        rows = rows[: int(params.get("limit", len(rows)))]
        columns = params.get("select", "*")
        payload = (
            deepcopy(rows)
            if columns == "*"
            else [{column: row.get(column) for column in columns.split(",")} for row in rows]
        )
        return httpx.Response(
            200, json=payload, headers={"Content-Range": f"0-{max(0, total - 1)}/{total}"}
        )

    def twitter_http(self, _client, method, route, **kwargs):
        self.x_requests.append((method, route, kwargs))
        if method == "POST":
            assert any(
                row.get("skip_reason") == "PUBLISHING"
                and row["reply_tweet_id"] == kwargs["json"]["reply"]["in_reply_to_tweet_id"]
                for row in self.tables["kr_reply_history"]
            ), "HTTP publication must occur only after durable publication claim"
            if self.timeout:
                raise requests.Timeout("transport timeout")
            payload = {"data": {"id": str(900 + self.post_count), "text": kwargs["json"]["text"]}}
        elif route.endswith("/mentions"):
            since = kwargs.get("params", {}).get("since_id")
            mentions = [m for m in self.mentions if not since or int(m["id"]) > int(since)]
            payload = {"meta": {"result_count": len(mentions)}}
            if mentions:
                payload.update(
                    data=mentions,
                    includes={
                        "tweets": [
                            {
                                "id": "400",
                                "edit_history_tweet_ids": ["400"],
                                "text": "시장 정보 원문",
                                "author_id": "111",
                            }
                        ]
                    },
                )
                payload["meta"].update(
                    newest_id=max(m["id"] for m in mentions),
                    oldest_id=min(m["id"] for m in mentions),
                )
        else:
            ids = kwargs["params"]["ids"].split(",")
            payload = {
                "data": [
                    {
                        "id": tid,
                        "edit_history_tweet_ids": [tid],
                        "text": "시장 정보 원문",
                        "author_id": "111",
                    }
                    for tid in ids
                ]
            }
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    @property
    def post_count(self):
        return sum(method == "POST" for method, _, _ in self.x_requests)

    def generate(self, *, contents, **_kwargs):
        self.model_requests.append(contents)
        lines = contents.splitlines()
        rows = [json.loads(line) for line in lines if line.startswith('{"id":')]
        payload = [{"id": row["id"], "reply": "저야말로 감사합니다"} for row in rows]
        if not rows:
            rows = next(json.loads(line) for line in lines if line.startswith('[{"id":'))
            payload = [{"id": row["id"], "label": "POSITIVE"} for row in rows]
            if self.classifier_unavailable:
                payload = []
        return SimpleNamespace(text=json.dumps(payload), usage_metadata=None)

    def add_mention(self, tid, author="222", text="감사합니다"):
        self.mentions.append(
            {
                "id": tid,
                "edit_history_tweet_ids": [tid],
                "text": text,
                "author_id": author,
                "conversation_id": "400",
                "in_reply_to_user_id": "111",
                "created_at": (datetime.now(UTC) - timedelta(minutes=5)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                "referenced_tweets": [{"type": "replied_to", "id": "400"}],
            }
        )


@pytest.fixture
def services(monkeypatch, tmp_path):
    fake = OfflineServices()
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "REPLY_ENABLED": "true",
        "REPLY_MODE": "live",
        "X_MY_USER_ID": "111",
        "X_API_KEY": "fake",
        "X_API_SECRET": "fake",
        "X_ACCESS_TOKEN": "fake",
        "X_ACCESS_TOKEN_SECRET": "fake",
        "GEMINI_API_KEY": "fake",
        "X_READ_COST_KRW": "0",
        "X_WRITE_COST_KRW": "0",
        "REPLY_LIKE_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "GEMINI_API_SUB_KEY",
        "GEMINI_API_SUB_SUB_KEY",
        "GEMINI_API_SUB_PAY_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALERT_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gemini_gateway, "GEMINI_API_KEY", "fake")
    for name in ("GEMINI_API_SUB_KEY", "GEMINI_API_SUB_SUB_KEY", "GEMINI_API_SUB_PAY_KEY"):
        monkeypatch.setattr(gemini_gateway, name, "")
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 2)
    monkeypatch.setattr(run_reply, "STARTUP_JITTER_MAX_SEC", 0)
    monkeypatch.setattr(run_reply.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        tweepy.Client,
        "request",
        lambda client, method, route, **kw: fake.twitter_http(client, method, route, **kw),
    )
    monkeypatch.setattr(
        gemini_gateway,
        "_get_client",
        lambda _key: SimpleNamespace(models=SimpleNamespace(generate_content=fake.generate)),
    )
    with httpx.Client(transport=httpx.MockTransport(fake.database_http)) as http:
        api = SyncPostgrestClient("https://offline.test/rest/v1", http_client=http)
        monkeypatch.setattr(supabase_client, "_client", SimpleNamespace(table=api.from_))
        yield fake


def test_real_pipeline_live_claim_publish_commit_cursor(services):
    services.add_mention("500")
    result = run_reply.main()
    assert result["actual_published"] == 1 and result["cursor_advanced"]
    row = services.tables["kr_reply_history"][0]
    assert row["responded"] and row["response_tweet_id"] == "901"
    assert decode_metadata(row["error_message"])["publish_attempts"] == 1
    assert services.tables["kr_reply_cursor"][0]["since_id"] == "500"
    assert services.tables["kr_reply_budget"][0]["write_calls"] == 1
    assert services.post_count == 1 and len(services.model_requests) == 1
    assert result["review"][0]["parent_preview"] == "시장 정보 원문"


def test_real_pipeline_timeout_is_durable_and_replay_blocked(services):
    services.add_mention("500")
    services.timeout = True
    first = run_reply.main()
    assert first["skip_reasons"]["PUBLISH_UNKNOWN"] == 1
    assert services.tables["kr_reply_history"][0]["skip_reason"] == "PUBLISH_UNKNOWN"
    services.tables["kr_reply_cursor"].clear()  # Actual rescan, independent of cursor guard.
    second = run_reply.main()
    assert second["skip_reasons"]["DUP"] == 1
    assert services.post_count == 1


def test_real_pipeline_run_cap_recovers_from_database_next_invocation(services, monkeypatch):
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 1)
    services.add_mention("500")
    services.add_mention("501", author="333")
    first = run_reply.main()
    assert first["actual_published"] == 1 and first["deferred"] == 1
    assert services.tables["kr_reply_history"][1]["skip_reason"] == "RUN_CAP"
    second = run_reply.main()
    assert second["collected"] == 0 and second["recovered_failures"] == 1
    assert second["actual_published"] == 1 and services.post_count == 2
    assert all(row["responded"] for row in services.tables["kr_reply_history"])


def test_real_pipeline_dry_run_has_zero_database_mutations_and_zero_x_posts(services, monkeypatch):
    monkeypatch.setenv("REPLY_MODE", "dry_run")
    services.add_mention("500")
    result = run_reply.main()
    assert result["simulated"] == 1 and result["actual_published"] == 0
    assert services.post_count == 0
    assert all(request.method == "GET" for request in services.db_requests)
    assert not any(services.tables.values())


def test_real_sdk_prior_day_publication_claim_reserves_today_quota(services):
    original = datetime.fromisoformat(store.kst_day_start_utc_iso()) - timedelta(minutes=1)
    metadata = encode_metadata({"id": "500", "text": "감사합니다", "created_at": original})
    services.tables["kr_reply_history"] = [
        {
            "reply_tweet_id": "500",
            "author_id": "222",
            "conversation_id": "400",
            "mode": "live",
            "responded": False,
            "response_tweet_id": None,
            "skip_reason": None,
            "created_at": original.isoformat(),
            "error_message": metadata,
        }
    ]
    assert store.claim_publication("500", metadata)
    assert store.count_responded_today() == 1
    assert store.count_author_responded_today_bulk(["222"]) == {"222": 1}
    assert store.count_conversation_responded_today_bulk(["400"]) == {"400": 1}
    assert (
        decode_metadata(services.tables["kr_reply_history"][0]["error_message"])[
            "original_created_at"
        ]
        == original.isoformat()
    )


def test_real_pipeline_fresh_rescans_preserve_classifier_failure_limit(services):
    services.add_mention("500", text="그 흐름 인상적이네요")
    services.classifier_unavailable = True
    for expected in (1, 2, 3):
        result = run_reply.main()
        row = services.tables["kr_reply_history"][0]
        metadata = decode_metadata(row["error_message"])
        assert metadata["classification_attempts"] == expected
        assert services.post_count == 0
        services.tables["kr_reply_cursor"].clear()
        metadata["next_attempt_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        row["error_message"] = json.dumps(metadata)
    assert result["skip_reasons"]["CLASSIFIER_EXHAUSTED"] == 1
    exhausted = run_reply.main()
    assert exhausted["skip_reasons"]["DUP"] == 1
    assert len(services.model_requests) == 3
