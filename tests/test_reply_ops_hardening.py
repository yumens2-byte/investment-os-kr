"""2026-09-08 운영 점검 후속 (전부 승인): Q-1 외국어 무응답 / 항목1 like opt-in /
항목2 오류 원문 저장·SPEND_CAP 구분 / 항목3 관리자 알림 / Q-3 선택형 중립 규칙."""

from __future__ import annotations

from datetime import UTC, datetime

import run_reply
from core import alert
from reply_engine import config, generator, store, x_client
from reply_engine import filter as filter_mod
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet

_BASE = {"id": "1", "author_id": "999", "in_reply_to_user_id": "111",
         "conversation_id": "c", "created_at": datetime.now(UTC)}


def _tw(text):
    return {**_BASE, "text": text}


# ── Q-1 ────────────────────────────────────────────────────────────────
def test_q1_foreign_comment_skipped():
    """실사고 픽스처: 베트남어 댓글은 SKIP_FOREIGN (아는 척 응답 원천 차단)."""
    ok, reason = filter_mod.check_tweet(
        _tw("@tiger18272 30 chuyến/ngày thì đúng là biến nhiều"), None, "111", set()
    )
    assert not ok and reason == "SKIP_FOREIGN"
    ok, reason = filter_mod.check_tweet(_tw("@tiger18272 Nice chart, thanks!"), None, "111", set())
    assert not ok and reason == "SKIP_FOREIGN"


def test_q1_korean_and_mixed_pass():
    for text in ("@tiger18272 잘보고 있어요^^", "@tiger18272 ㅋㅋㅋ ㅇㅈ",
                 "@tiger18272 SCHD 오늘 좋네요 ETF 최고", "@tiger18272 👍👍👍"):
        ok, reason = filter_mod.check_tweet(_tw(text), None, "111", set())
        assert reason != "SKIP_FOREIGN", (text, reason)


def test_q1_ratio_helper():
    assert filter_mod.is_korean_dominant("ㅋㅋㅋ") is True          # 자모도 한글
    assert filter_mod.is_korean_dominant("👍 !!") is True           # 문자 없음 → 위임
    assert filter_mod.is_korean_dominant("hello world") is False
    assert filter_mod.is_korean_dominant("SCHD 좋네요") is True     # 4/7 ≥ 0.3


# ── 항목 1 ─────────────────────────────────────────────────────────────
def test_like_opt_in_default_false(monkeypatch):
    monkeypatch.delenv("REPLY_LIKE_ENABLED", raising=False)
    assert config.is_like_enabled() is False
    monkeypatch.setenv("REPLY_LIKE_ENABLED", "TRUE")
    assert config.is_like_enabled() is True


# ── 항목 2 ─────────────────────────────────────────────────────────────
def test_publish_fail_stores_error_and_spend_cap(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    captured = {}
    monkeypatch.setattr(
        store, "update_skip_reason",
        lambda tid, reason, error_message=None: captured.update(
            {"tid": tid, "reason": reason, "err": error_message}) or True,
    )
    monkeypatch.setattr(
        x_client, "post_reply",
        lambda _c, _t, _i: (None, "403 Forbidden: Your monthly spend cap has been reached."),
    )
    monkeypatch.setattr(alert, "send_admin_alert", lambda text: True)
    monkeypatch.setattr(run_reply, "send_admin_alert", alert.send_admin_alert)

    result = run_reply.main()
    assert captured["reason"] == "SPEND_CAP"
    assert "spend cap" in captured["err"]
    assert result["skip_reasons"]["SPEND_CAP"] == 1


# ── 항목 3 ─────────────────────────────────────────────────────────────
def test_alert_noop_without_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    calls = []
    monkeypatch.setattr(alert.requests, "post", lambda *a, **k: calls.append(1))
    assert alert.send_admin_alert("x") is False
    assert calls == []                                   # 공개 채널로 새지 않음


def test_alert_sends_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "12345")
    sent = {}

    class _R:
        status_code = 200

    monkeypatch.setattr(
        alert.requests, "post", lambda url, json, timeout: sent.update(json) or _R()
    )
    assert alert.send_admin_alert("hello") is True
    assert sent["chat_id"] == "12345" and sent["text"] == "hello"


def test_alert_triggered_only_on_failure(monkeypatch):
    """정상 실행은 무알림, PUBLISH_FAIL 발생 시 1회 알림."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)
    sent: list = []
    monkeypatch.setattr(run_reply, "send_admin_alert", lambda text: sent.append(text) or True)

    run_reply.main()
    assert sent == []                                    # 정상 → 무알림

    monkeypatch.setattr(x_client, "post_reply", lambda _c, _t, _i: (None, "500"))
    mem2 = _MemStore()
    mem2.install(monkeypatch)
    run_reply.main()
    assert len(sent) == 1 and "PUBLISH_FAIL=1" in sent[0]


# ── Q-3 ────────────────────────────────────────────────────────────────
def test_q3_prompt_neutral_rule(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **kw: captured.update(kw) or {"success": False, "data": None, "error": "x"},
    )
    generator.generate_batch([{"id": "a", "text": "B 선택할게요", "label": "POSITIVE"}])
    assert "선택형(A/B 투표)" in captured["prompt"] and "중립 감사만" in captured["prompt"]


def test_versions_ops_hardening():
    assert run_reply.VERSION == "1.4.0"
    assert filter_mod.VERSION == "1.1.0"
    assert config.VERSION == "1.0.7"
    assert store.VERSION == "1.2.0"
    assert x_client.VERSION == "1.4.0"
    assert generator.VERSION == "1.2.1"
    assert alert.VERSION == "1.0.0"
