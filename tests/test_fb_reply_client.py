"""FB-1 단위: Graph 클라이언트(HTTP 경계 목킹)와 댓글 정규화."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import requests

from reply_engine.facebook import client as fb
from reply_engine.facebook.normalize import normalize_comment, parse_graph_time

PAGE = "777"
TOKEN = "EAAsecret-token-1234567890"


class Resp:
    def __init__(self, payload=None, status=200, headers=None, raw=None):
        self.payload, self.status_code, self.headers, self.raw = payload, status, headers or {}, raw

    def json(self):
        if self.raw is not None:
            raise ValueError("not json")
        return self.payload


def _ts(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


# ── normalize ────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("2026-10-07T01:02:03+0000", datetime(2026, 10, 7, 1, 2, 3, tzinfo=UTC)),
    ("2026-10-07T10:02:03+09:00", datetime(2026, 10, 7, 1, 2, 3, tzinfo=UTC)),
    ("2026-10-07T01:02:03Z", datetime(2026, 10, 7, 1, 2, 3, tzinfo=UTC)),
])
def test_parse_graph_time_formats(value, expected):
    assert parse_graph_time(value) == expected


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-10-07T01:02:03", 1700000000])
def test_parse_graph_time_rejects_ambiguous(value):
    assert parse_graph_time(value) is None


POST = {"id": "777_1", "message": "오늘의 시장 정리", "from": {"id": PAGE}}


def test_top_level_comment_is_in_scope():
    raw = {"id": "c1", "message": "감사합니다", "from": {"id": "u1", "name": "홍길동"},
           "created_time": _ts(5), "can_comment": True}
    item, skip = normalize_comment(raw, POST, PAGE, thread_enabled=False)
    assert skip is None
    assert item["in_reply_to_user_id"] == PAGE and item["conversation_id"] == "777_1"
    assert item["parent_text"] == "오늘의 시장 정리" and item["parent_author_id"] == PAGE
    assert "홍길동" not in json.dumps(item, default=str, ensure_ascii=False)  # 실명 미적재


def test_reply_to_page_comment_requires_opt_in():
    raw = {"id": "c2", "message": "네 감사해요", "from": {"id": "u1"}, "created_time": _ts(5),
           "parent": {"id": "c0", "from": {"id": PAGE}, "message": "읽어주셔서 감사합니다"}}
    assert normalize_comment(raw, POST, PAGE, thread_enabled=False)[1] == "OUT_OF_SCOPE_THREAD"
    item, skip = normalize_comment(raw, POST, PAGE, thread_enabled=True)
    assert skip is None and item["fb_thread"] and item["in_reply_to_user_id"] == PAGE
    assert item["parent_text"] == "읽어주셔서 감사합니다" and item["parent_author_id"] == PAGE


def test_third_party_reply_is_left_out_of_scope():
    raw = {"id": "c3", "message": "축하해주셔서 감사합니다", "from": {"id": "u2"},
           "created_time": _ts(5), "parent": {"id": "c9", "from": {"id": "u1"}}}
    item, skip = normalize_comment(raw, POST, PAGE, thread_enabled=True)
    assert skip is None and item["in_reply_to_user_id"] == ""  # filter가 OUT_OF_SCOPE 판정


def test_missing_author_or_time_is_conservatively_skipped():
    assert normalize_comment({"id": "c4", "message": "hi", "created_time": _ts(1)}, POST, PAGE,
                             thread_enabled=False)[1] == "AUTHOR_UNVERIFIED"
    assert normalize_comment({"id": "c5", "from": {"id": "u"}, "created_time": "??"}, POST,
                             PAGE, thread_enabled=False)[1] == "TIME_UNVERIFIED"
    assert normalize_comment({"message": "no id"}, POST, PAGE, thread_enabled=False) == (
        None, "INVALID")


# ── 오류 분류 (공식 오류 코드표) ─────────────────────────────

@pytest.mark.parametrize("error, category, publish_state", [
    ({"code": 4}, "THROTTLE", "PUBLISH_RETRYABLE"),
    ({"code": 17}, "THROTTLE", "PUBLISH_RETRYABLE"),
    ({"code": 32}, "THROTTLE", "PUBLISH_RETRYABLE"),
    ({"code": 613}, "THROTTLE", "PUBLISH_RETRYABLE"),
    ({"code": 80001}, "THROTTLE", "PUBLISH_RETRYABLE"),
    ({"code": 341}, "THROTTLE", "PUBLISH_RETRYABLE"),
    # AUTH = 명시적 거절(미발행 확정) → 보류 복구 (토큰 교체 후 TTL 내 재시도, 리뷰 #5)
    ({"code": 190, "error_subcode": 463}, "AUTH", "PUBLISH_RETRYABLE"),
    ({"code": 102}, "AUTH", "PUBLISH_RETRYABLE"),
    ({"code": 10}, "AUTH", "PUBLISH_RETRYABLE"),
    ({"code": 200}, "AUTH", "PUBLISH_RETRYABLE"),
    ({"code": 299}, "AUTH", "PUBLISH_RETRYABLE"),
    ({"code": 368}, "POLICY_BLOCK", "PUBLISH_REJECTED"),
    ({"code": 100}, "REJECTED", "PUBLISH_REJECTED"),
    ({"code": 506}, "REJECTED", "PUBLISH_REJECTED"),
    ({"code": 1}, "UNKNOWN", "PUBLISH_UNKNOWN"),
    ({"code": 2}, "UNKNOWN", "PUBLISH_UNKNOWN"),
    ({"transport": "ReadTimeout"}, "UNKNOWN", "PUBLISH_UNKNOWN"),
    ({"http_status": 502, "message": "non-json response"}, "UNKNOWN", "PUBLISH_UNKNOWN"),
])
def test_error_classification(error, category, publish_state):
    assert fb.classify_error(error) == category
    assert fb.classify_publish_error(error) == publish_state


def test_no_error_classifies_none():
    assert fb.classify_error(None) is None


# ── HTTP 경계 ────────────────────────────────────────────────

def test_post_reply_success_and_payload(monkeypatch):
    seen = {}

    def fake_post(url, data, timeout):
        seen.update(url=url, data=data, timeout=timeout)
        return Resp({"id": "777_1_99"})

    monkeypatch.setattr(fb, "_http_post", fake_post)
    assert fb.post_reply("c1", "감사합니다", TOKEN) == ("777_1_99", None, None)
    assert seen["url"].endswith("/v25.0/c1/comments") and seen["data"]["message"] == "감사합니다"


def test_post_reply_timeout_is_unknown_and_token_masked(monkeypatch):
    def fake_post(url, data, timeout):
        raise requests.Timeout(f"timeout calling {url}?access_token={TOKEN}")

    monkeypatch.setattr(fb, "_http_post", fake_post)
    new_id, error, _ = fb.post_reply("c1", "감사합니다", TOKEN)
    assert new_id is None and fb.classify_publish_error(error) == "PUBLISH_UNKNOWN"
    assert TOKEN not in json.dumps(error) and TOKEN not in fb.format_error(error)


def test_post_reply_success_without_id_is_unknown(monkeypatch):
    monkeypatch.setattr(fb, "_http_post", lambda url, data, timeout: Resp({"success": True}))
    new_id, error, _ = fb.post_reply("c1", "감사합니다", TOKEN)
    assert new_id is None and fb.classify_publish_error(error) == "PUBLISH_UNKNOWN"


def test_graph_error_json_is_parsed_and_masked(monkeypatch):
    payload = {"error": {"message": f"Invalid OAuth {TOKEN}", "type": "OAuthException",
                         "code": 190, "error_subcode": 460, "fbtrace_id": "T"}}
    monkeypatch.setattr(fb, "_http_post", lambda url, data, timeout: Resp(payload, 400))
    _, error, _ = fb.post_reply("c1", "감사합니다", TOKEN)
    assert error["code"] == 190 and TOKEN not in error["message"]


def test_buc_header_parsing():
    header = json.dumps({PAGE: [{"type": "pages", "call_count": 12, "total_cputime": 85,
                                 "total_time": 40, "estimated_time_to_regain_access": 0}]})
    assert fb._buc_max_pct({"X-Business-Use-Case-Usage": header}) == 85
    assert fb._buc_max_pct({"X-Business-Use-Case-Usage": "garbage"}) is None
    assert fb._buc_max_pct({}) is None


class FakeGraph:
    """GET 경계: /feed, /{post}/comments (+paging.next)."""

    def __init__(self, posts, comments, *, headers=None, errors=None):
        self.posts, self.comments, self.headers = posts, comments, headers or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        for key, err in self.errors.items():
            if key in url:
                return Resp({"error": err}, 400)
        if url.endswith("/feed"):
            return Resp({"data": self.posts}, headers=self.headers)
        for post_id, pages in self.comments.items():
            if f"/{post_id}/comments" in url:
                page = 0 if params else int(url.rsplit("page=", 1)[1])
                body = {"data": pages[page]}
                if page + 1 < len(pages):
                    body["paging"] = {"next": f"https://graph/{post_id}/comments?page={page + 1}"}
                return Resp(body, headers=self.headers)
        return Resp({"data": []})


def _collect(monkeypatch, graph, **overrides):
    monkeypatch.setattr(fb, "_http_get", graph.get)
    kwargs = {"post_limit": 10, "max_pages": 2, "max_age_hours": 24, "read_allowance": 50,
              "buc_stop_pct": 80, **overrides}
    return fb.collect(PAGE, TOKEN, **kwargs)


def test_collect_scopes_posts_and_stops_at_ttl(monkeypatch):
    posts = [
        {"id": "p1", "from": {"id": PAGE}, "is_published": True},
        {"id": "p2", "from": {"id": "visitor"}},            # 방문자 게시물 → 범위 밖
        {"id": "p3", "from": {"id": PAGE}, "is_published": False},  # 미게시
        {"id": "p4"},                                        # 작성자 미확인
    ]
    comments = {"p1": [[{"id": "c1", "created_time": _ts(10)},
                        {"id": "c0", "created_time": _ts(60 * 30)}],  # TTL 밖 → 중단
                       [{"id": "never", "created_time": _ts(1)}]]}
    graph = FakeGraph(posts, comments)
    result = _collect(monkeypatch, graph)
    assert [raw["id"] for raw, _ in result["items"]] == ["c1"]
    assert result["posts_skipped"] == 3 and result["collection_complete"]
    assert result["pages_fetched"] == 2  # feed 1 + p1 1페이지 (TTL 도달로 next 미추적)


def test_collect_follows_paging_and_marks_incomplete_at_page_limit(monkeypatch):
    posts = [{"id": "p1", "from": {"id": PAGE}}]
    comments = {"p1": [[{"id": "a", "created_time": _ts(3)}],
                       [{"id": "b", "created_time": _ts(4)}],
                       [{"id": "c", "created_time": _ts(5)}]]}
    result = _collect(monkeypatch, FakeGraph(posts, comments), max_pages=2)
    assert [raw["id"] for raw, _ in result["items"]] == ["a", "b"]
    assert result["collection_complete"] is False and result["pages_fetched"] == 3


def test_collect_respects_read_allowance(monkeypatch):
    posts = [{"id": f"p{i}", "from": {"id": PAGE}} for i in range(5)]
    comments = {f"p{i}": [[{"id": f"c{i}", "created_time": _ts(1)}]] for i in range(5)}
    result = _collect(monkeypatch, FakeGraph(posts, comments), read_allowance=3)
    assert result["pages_fetched"] == 3 and result["halt_reason"] == "READ_BUDGET"
    assert result["collection_complete"] is False


def test_collect_halts_on_buc_limit(monkeypatch):
    header = {"X-Business-Use-Case-Usage": json.dumps({PAGE: [{"call_count": 95}]})}
    posts = [{"id": "p1", "from": {"id": PAGE}}]
    result = _collect(monkeypatch, FakeGraph(posts, {"p1": [[]]}, headers=header))
    assert result["halt_reason"] == "BUC_LIMIT" and result["pages_fetched"] == 1


def test_collect_feed_auth_error(monkeypatch):
    graph = FakeGraph([], {}, errors={"/feed": {"code": 190, "message": TOKEN}})
    result = _collect(monkeypatch, graph)
    assert result["success"] is False and result["error_category"] == "AUTH"
    assert TOKEN not in result["error"]


def test_collect_comment_throttle_halts_remaining_posts(monkeypatch):
    posts = [{"id": "p1", "from": {"id": PAGE}}, {"id": "p2", "from": {"id": PAGE}}]
    graph = FakeGraph(posts, {"p2": [[]]}, errors={"/p1/comments": {"code": 32}})
    result = _collect(monkeypatch, graph)
    assert result["success"] and result["halt_reason"] == "THROTTLE"
    assert not any("/p2/" in c for c in graph.calls)


def test_collect_zero_allowance_makes_no_call(monkeypatch):
    graph = FakeGraph([], {})
    result = _collect(monkeypatch, graph, read_allowance=0)
    assert graph.calls == [] and result["halt_reason"] == "READ_BUDGET"


def test_get_transport_error_masks_token_in_paging_url(monkeypatch):
    """paging.next URL에는 토큰이 포함된다 — 전송 예외 문자열에서도 마스킹돼야 한다."""
    posts = [{"id": "p1", "from": {"id": PAGE}}]
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if url.endswith("/feed"):
            return Resp({"data": posts})
        if calls["n"] == 2:
            return Resp({"data": [{"id": "a", "created_time": _ts(1)}],
                         "paging": {"next": f"https://graph/p1/comments?access_token={TOKEN}"}})
        raise requests.ConnectionError(f"failed: {url}")

    monkeypatch.setattr(fb, "_http_get", fake_get)
    result = fb.collect(PAGE, TOKEN, post_limit=1, max_pages=3, max_age_hours=24,
                        read_allowance=10, buc_stop_pct=80)
    assert result["collection_complete"] is False and result["error_category"] == "UNKNOWN"
    assert TOKEN not in result["error"] and "***" in result["error"]
    assert [raw["id"] for raw, _ in result["items"]] == ["a"]
