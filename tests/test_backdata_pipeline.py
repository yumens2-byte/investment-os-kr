"""DN2026_0090 파이프라인 모드 행렬·성과 수집 테스트 (외부 호출 전부 가짜)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import run_backdata as rb
from backdata_engine import config, metrics
from backdata_engine.config import KST
from tests.test_backdata_stats import make_rows


class Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def add(self, *item):
        self.calls.append(item)

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rec = Recorder()
    rows = make_rows(60)
    today = rows[-1][0]
    now_kst = datetime(today.year, today.month, today.day, 18, 0, tzinfo=KST)

    monkeypatch.setattr(rb.history, "fetch_closes", lambda s: rows)
    monkeypatch.setattr(rb.store, "slot_taken", lambda d, m: rec.add("slot_taken") or False)
    monkeypatch.setattr(rb.store, "insert_pending",
                        lambda r: rec.add("insert_pending", r) or 7)
    monkeypatch.setattr(rb.store, "update_channel",
                        lambda pid, ch, st, ext_id=None, error=None:
                        rec.add("update", ch, st) or True)
    monkeypatch.setattr(rb.gemini_gateway, "call",
                        lambda prompt, **kw: {"success": False, "data": None,
                                              "api_calls": 0, "error": "off"})
    monkeypatch.setattr(rb.alert, "send_admin_alert", lambda t: rec.add("alert", t) or True)
    monkeypatch.setattr(rb, "_sleep", lambda s: rec.add("sleep", s))
    monkeypatch.setattr(rb.x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(rb.xpub, "post_text", lambda c, t: rec.add("x_post") or ("111", None))
    monkeypatch.setattr(rb.fbpub, "post_feed",
                        lambda p, tok, m: rec.add("fb_post") or ("p_222", None))
    monkeypatch.setenv("FACE_PAGE_ID", "page")
    monkeypatch.setenv("FACE_PAGE_TOKEN", "tok")
    return rec, now_kst, rows


def test_dry_run_has_no_writes(env):
    rec, now, _ = env
    out = rb.run_post("dry_run", now)
    assert out["exit_reason"] == "EXIT_OK"
    assert not {"slot_taken", "insert_pending", "update", "x_post", "fb_post"} & set(rec.names())
    assert out["calls"] == {"gemini": 0, "x_write": 0, "fb_write": 0, "db_write": 0}
    assert out["compose"]["hook_source"] == "fixed"


def test_shadow_writes_ledger_only(env):
    rec, now, _ = env
    out = rb.run_post("shadow", now)
    assert out["exit_reason"] == "EXIT_OK"
    assert rec.names().count("insert_pending") == 1
    assert "x_post" not in rec.names() and "fb_post" not in rec.names()
    record = [c for c in rec.calls if c[0] == "insert_pending"][0][1]
    assert record["x_status"] == record["fb_status"] == "pending"
    assert record["mode"] == "shadow"


def test_live_posts_each_channel_once(env):
    rec, now, _ = env
    out = rb.run_post("live", now)
    assert out["exit_reason"] == "EXIT_OK"
    names = rec.names()
    assert names.count("x_post") == 1 and names.count("fb_post") == 1
    assert names.index("insert_pending") < names.index("sleep") < names.index("x_post")
    assert ("update", "x", "posted") in rec.calls and ("update", "fb", "posted") in rec.calls


def test_live_fb_failure_is_partial_without_retry(env, monkeypatch):
    rec, now, _ = env
    monkeypatch.setattr(rb.fbpub, "post_feed",
                        lambda p, tok, m: rec.add("fb_post") or (None, "code=200 perm"))
    out = rb.run_post("live", now)
    assert out["exit_reason"] == "EXIT_PARTIAL"
    assert rec.names().count("fb_post") == 1 and rec.names().count("x_post") == 1
    assert ("update", "fb", "failed") in rec.calls
    assert any(c[0] == "alert" for c in rec.calls)


def test_live_unknown_result_recorded_as_unknown(env, monkeypatch):
    rec, now, _ = env
    monkeypatch.setattr(rb.xpub, "post_text",
                        lambda c, t: rec.add("x_post") or (None, "UNKNOWN: no id"))
    out = rb.run_post("live", now)
    assert ("update", "x", "unknown") in rec.calls
    assert out["exit_reason"] == "EXIT_PARTIAL"


def test_live_no_credentials(env, monkeypatch):
    rec, now, _ = env
    monkeypatch.delenv("FACE_PAGE_TOKEN")
    out = rb.run_post("live", now)
    assert out["exit_reason"] == "EXIT_NO_CREDENTIALS"
    assert "x_post" not in rec.names()


def test_stale_bar_no_publish(env):
    rec, now, _ = env
    out = rb.run_post("live", now + timedelta(days=1))
    assert out["exit_reason"] == "EXIT_STALE" and out["success"] is True
    assert "insert_pending" not in rec.names()


def test_slot_taken(env, monkeypatch):
    rec, now, _ = env
    monkeypatch.setattr(rb.store, "slot_taken", lambda d, m: True)
    assert rb.run_post("live", now)["exit_reason"] == "EXIT_SLOT_TAKEN"
    assert "insert_pending" not in rec.names()


def test_duplicate_insert_blocks_publish(env, monkeypatch):
    rec, now, _ = env
    monkeypatch.setattr(rb.store, "insert_pending", lambda r: None)
    assert rb.run_post("live", now)["exit_reason"] == "EXIT_DUP_FINAL"
    assert "x_post" not in rec.names()


def test_fetch_failure_is_data_fail(env, monkeypatch):
    _, now, _ = env
    monkeypatch.setattr(rb.history, "fetch_closes", lambda s: None)
    assert rb.run_post("dry_run", now)["exit_reason"] == "EXIT_DATA_FAIL"


@pytest.mark.parametrize("raw,expected", [
    ("", "dry_run"), ("prod", "dry_run"), ("LIVE ", "live"), ("shadow", "shadow"),
    ("dry-run", "dry_run"),
])
def test_mode_parser(monkeypatch, raw, expected):
    monkeypatch.setenv("BACKDATA_MODE", raw)
    assert config.get_mode() == expected


@pytest.mark.parametrize("raw,expected", [("", 1800), ("abc", 1800), ("60", 60),
                                          ("99999", 2400), ("-5", 0)])
def test_delay_parser(monkeypatch, raw, expected):
    monkeypatch.setenv("BACKDATA_DELAY_MAX_SEC", raw)
    assert config.get_delay_max_sec() == expected


def test_disabled(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BACKDATA_ENABLED", "false")
    monkeypatch.setattr(rb.alert, "send_admin_alert", lambda t: True)
    assert rb.main(["--step", "post"])["exit_reason"] == "EXIT_DISABLED"


def test_due_checkpoints():
    now = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)

    def post(hours, collected=()):
        ts = (now - timedelta(hours=hours)).isoformat()
        return {"x_post_id": "1", "x_posted_at": ts, "fb_post_id": None,
                "fb_posted_at": None, "collected": set(collected)}

    assert metrics.due_checkpoints(post(10), now) == []
    assert metrics.due_checkpoints(post(30), now) == [("x", "first", 30.0)]
    assert metrics.due_checkpoints(post(30, {("x", "first")}), now) == []
    assert metrics.due_checkpoints(post(80), now) == [("x", "final", 80.0)]
    assert metrics.due_checkpoints(post(80, {("x", "final")}), now) == []
    assert metrics.due_checkpoints(post(24 * 31), now) == []


def test_metrics_skipped_outside_live(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def boom(*a, **k):
        raise AssertionError("dry_run에서 DB 조회 금지")
    monkeypatch.setattr(rb.store, "list_metric_targets", boom)
    out = rb.run_metrics("dry_run", datetime.now(UTC))
    assert out["exit_reason"] == "EXIT_OK"


def test_metrics_live_inserts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    now = datetime(2026, 11, 10, tzinfo=UTC)
    ts = (now - timedelta(hours=30)).isoformat()
    monkeypatch.setattr(rb.store, "list_metric_targets", lambda n, d: [
        {"id": 7, "x_post_id": "111", "x_posted_at": ts, "fb_post_id": "p_222",
         "fb_posted_at": ts, "collected": set()}])
    rows = []
    monkeypatch.setattr(rb.store, "insert_metric", lambda r: rows.append(r) or True)
    monkeypatch.setattr(rb.x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(rb.xpub, "fetch_metrics", lambda c, ids: (
        {"111": {"impressions": 500, "engagements": 12, "raw": {}}}, None))
    monkeypatch.setattr(rb.fbpub, "fetch_insights", lambda pid, tok: (
        {"impressions": 300, "engagements": 9, "raw": {}}, None))
    monkeypatch.setenv("FACE_PAGE_TOKEN", "tok")
    out = rb.run_metrics("live", now)
    assert out["collected"] == 2
    assert {(r["channel"], r["checkpoint"]) for r in rows} == {("x", "first"), ("fb", "first")}
