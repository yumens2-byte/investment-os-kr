"""FB-1 Preflight(읽기 전용) 판정 규칙 — 리뷰 #3 반영. Graph GET 경계만 대체, POST는 금지."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reply_engine.facebook import client as fb
from scripts import fb_reply_preflight as pf

PAGE = "777"
TOKEN = "EAApreflight-token-123456"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.headers = payload, status, {}

    def json(self):
        return self._payload


def _ts(minutes=5):
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class Graph:
    def __init__(self):
        self.identity = {"id": PAGE, "name": "Tiger18272"}
        self.posts = [{"id": "p1", "from": {"id": PAGE}, "is_published": True}]
        self.comments = [{"id": "c1", "from": {"id": "u1"}, "created_time": _ts(),
                          "can_comment": True}]
        self.comment_error = None
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/feed"):
            return _Resp({"data": self.posts})
        if url.endswith("/comments"):
            if self.comment_error:
                return _Resp({"error": self.comment_error}, 400)
            return _Resp({"data": self.comments})
        if url.endswith("/video_reels"):
            return _Resp({"data": []})
        return _Resp(self.identity)


@pytest.fixture
def graph(monkeypatch):
    g = Graph()
    monkeypatch.setenv("FACE_PAGE_ID", PAGE)
    monkeypatch.setenv("FACE_PAGE_TOKEN", TOKEN)
    monkeypatch.setattr(fb, "_http_get", g.get)

    def _no_post(*_a, **_k):
        raise AssertionError("preflight must never write")

    monkeypatch.setattr(fb, "_http_post", _no_post)
    return g


def test_ready_and_live_ready_when_non_page_from_verified(graph):
    report = pf.run_preflight()
    assert report["verdict"] == "READY_FOR_DRY_RUN" and report["from_visibility"] == "VERIFIED"
    assert report["live_ready"] is True and report["checks"]["identity"]["ok"]


def test_comment_permission_error_blocks(graph):
    graph.comment_error = {"code": 10, "message": "Permission denied"}
    report = pf.run_preflight()
    assert report["verdict"] == "BLOCKED" and report["live_ready"] is False


def test_other_comment_error_needs_review(graph):
    graph.comment_error = {"code": 100, "message": "Unsupported get request"}
    assert pf.run_preflight()["verdict"] == "NEEDS_REVIEW"


def test_missing_from_needs_review(graph):
    graph.comments = [{"id": "c1", "created_time": _ts()}]
    report = pf.run_preflight()
    assert report["from_visibility"] == "MISSING" and report["verdict"] == "NEEDS_REVIEW"


def test_no_comments_is_not_verified_and_not_live_ready(graph):
    graph.comments = []
    report = pf.run_preflight()
    assert report["from_visibility"] == "NOT_VERIFIED"
    assert report["verdict"] == "READY_FOR_DRY_RUN" and report["live_ready"] is False


def test_only_page_comments_is_not_live_ready(graph):
    graph.comments = [{"id": "c1", "from": {"id": PAGE}, "created_time": _ts()}]
    report = pf.run_preflight()
    assert report["from_visibility"] == "NO_NON_PAGE_COMMENTS_OBSERVED"
    assert report["live_ready"] is False


def test_identity_mismatch_blocks(graph):
    graph.identity = {"id": "999", "name": "other"}
    report = pf.run_preflight()
    assert report["verdict"] == "BLOCKED" and "feed" not in report["checks"]


def test_time_format_failure_needs_review(graph):
    graph.comments = [{"id": "c1", "from": {"id": "u1"}, "created_time": "yesterday"}]
    assert pf.run_preflight()["verdict"] == "NEEDS_REVIEW"


def test_missing_credentials_make_no_call(monkeypatch, graph):
    monkeypatch.setenv("FACE_PAGE_ID", "not-a-number")
    report = pf.run_preflight()
    assert report["verdict"] == "BLOCKED" and graph.calls == []


def test_report_never_contains_token_or_names(graph, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    graph.comments[0]["from"]["name"] = "실명"
    assert pf.main() == 0
    text = (tmp_path / "logs/fb_reply_preflight.json").read_text(encoding="utf-8")
    assert TOKEN not in text and "실명" not in text
