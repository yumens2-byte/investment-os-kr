"""DN2026_0090 발행기·알림 단위 테스트 (R6-02·05 회귀, HTTP 경계만 가짜)."""

from __future__ import annotations

import requests
import tweepy

from backdata_engine import fb_publisher, notify, x_publisher


class _Client:
    def __init__(self, exc=None, data=None):
        self.exc, self.data, self.calls = exc, data, 0

    def create_tweet(self, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc

        class R:
            pass
        r = R()
        r.data = self.data
        return r


def test_x_timeout_is_unknown_and_single_attempt():
    c = _Client(exc=requests.ReadTimeout("read timed out"))
    tid, err = x_publisher.post_text(c, "t")
    assert tid is None and err.startswith("UNKNOWN:") and c.calls == 1


def test_x_server_error_is_unknown():
    resp = requests.Response()
    resp.status_code = 503
    tid, err = x_publisher.post_text(_Client(exc=tweepy.TwitterServerError(resp)), "t")
    assert err.startswith("UNKNOWN:")


def test_x_chunked_encoding_is_unknown():
    exc = requests.exceptions.ChunkedEncodingError("connection broken")
    assert x_publisher.post_text(_Client(exc=exc), "t")[1].startswith("UNKNOWN:")


def test_x_error_text_not_truncated_for_spend_cap():
    long_msg = "x" * 500 + " SpendCapReached"
    err = x_publisher.post_text(_Client(exc=RuntimeError(long_msg)), "t")[1]
    assert err.endswith("SpendCapReached")


def test_x_client_error_is_failed_not_unknown():
    resp = requests.Response()
    resp.status_code = 403
    tid, err = x_publisher.post_text(_Client(exc=tweepy.Forbidden(resp)), "t")
    assert tid is None and not err.startswith("UNKNOWN")


def test_x_success_without_id_is_unknown():
    assert x_publisher.post_text(_Client(data={}), "t")[1].startswith("UNKNOWN:")


def test_x_success():
    assert x_publisher.post_text(_Client(data={"id": 123}), "t") == ("123", None)


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.headers = status, payload, {}

    def json(self):
        return self._p


def test_fb_timeout_is_unknown(monkeypatch):
    calls = []

    def boom(url, data=None, timeout=None):
        calls.append(url)
        raise requests.ReadTimeout("t")
    monkeypatch.setattr(fb_publisher.fbclient, "_http_post", boom)
    pid, err = fb_publisher.post_feed("page", "tok", "m")
    assert pid is None and err.startswith("UNKNOWN:") and len(calls) == 1
    assert calls[0].endswith("/page/feed")


def test_fb_transient_code_is_unknown(monkeypatch):
    monkeypatch.setattr(fb_publisher.fbclient, "_http_post",
                        lambda url, data=None, timeout=None:
                        _Resp(500, {"error": {"code": 2, "message": "tmp"}}))
    assert fb_publisher.post_feed("page", "tok", "m")[1].startswith("UNKNOWN:")


def test_fb_permission_error_is_failed(monkeypatch):
    monkeypatch.setattr(fb_publisher.fbclient, "_http_post",
                        lambda url, data=None, timeout=None:
                        _Resp(403, {"error": {"code": 200, "message": "perm EAAB1234567890"}}))
    pid, err = fb_publisher.post_feed("page", "EAAB1234567890", "m")
    assert pid is None and not err.startswith("UNKNOWN")
    assert "EAAB1234567890" not in err and "***" in err


def test_fb_success(monkeypatch):
    monkeypatch.setattr(fb_publisher.fbclient, "_http_post",
                        lambda url, data=None, timeout=None: _Resp(200, {"id": "p_1"}))
    assert fb_publisher.post_feed("page", "tok", "m") == ("p_1", None)


def test_notify_masks_token_in_exception_log(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET123")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "1")

    def boom(url, json=None, timeout=None):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(notify, "_post", boom)
    with caplog.at_level("WARNING"):
        assert notify.send("hi") is False
    assert "SECRET123" not in caplog.text and "***" in caplog.text


def test_notify_noop_without_config(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    assert notify.send("hi") is False
