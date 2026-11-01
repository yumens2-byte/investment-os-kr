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
                        lambda r: rec.add("insert_pending", r) or (7, None))
    monkeypatch.setattr(rb.store, "update_channel",
                        lambda pid, ch, st, ext_id=None, error=None:
                        rec.add("update", ch, st) or True)
    monkeypatch.setattr(rb.gemini_gateway, "call",
                        lambda prompt, **kw: {"success": False, "data": None,
                                              "api_calls": 0, "error": "off"})
    monkeypatch.setattr(rb.notify, "send", lambda t: rec.add("alert", t) or True)
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
    monkeypatch.setattr(rb.store, "insert_pending", lambda r: (None, "conflict"))
    assert rb.run_post("live", now)["exit_reason"] == "EXIT_DUP_FINAL"
    assert "x_post" not in rec.names()


def test_insert_db_error_is_record_fail_with_alert(env, monkeypatch):
    rec, now, _ = env
    monkeypatch.setattr(rb.store, "insert_pending", lambda r: (None, "error"))
    out = rb.run_post("live", now)
    assert out["exit_reason"] == "EXIT_RECORD_FAIL" and out["success"] is False
    assert "x_post" not in rec.names() and any(c[0] == "alert" for c in rec.calls)


def test_slot_query_error_is_record_fail(env, monkeypatch):
    rec, now, _ = env
    monkeypatch.setattr(rb.store, "slot_taken", lambda d, m: None)
    assert rb.run_post("shadow", now)["exit_reason"] == "EXIT_RECORD_FAIL"
    assert "insert_pending" not in rec.names()


def test_one_index_fetch_failed_alerts_but_continues(env, monkeypatch):
    rec, now, rows = env
    monkeypatch.setattr(rb.history, "fetch_closes", lambda s: rows if s == "^KS11" else None)
    out = rb.run_post("dry_run", now)
    assert out["exit_reason"] == "EXIT_OK"
    assert out["warnings"] == ["fetch_failed=['^KQ11']"]
    assert any(c[0] == "alert" and "fetch_failed" in c[1] for c in rec.calls)


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
    monkeypatch.setattr(rb.notify, "send", lambda t: True)
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


def test_metrics_x_credentials_missing_still_collects_fb(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    now = datetime(2026, 11, 10, tzinfo=UTC)
    ts = (now - timedelta(hours=30)).isoformat()
    monkeypatch.setattr(rb.store, "list_metric_targets", lambda n, d: [
        {"id": 7, "x_post_id": "111", "x_posted_at": ts, "fb_post_id": "p_222",
         "fb_posted_at": ts, "collected": set()}])
    rows = []
    monkeypatch.setattr(rb.store, "insert_metric", lambda r: rows.append(r) or True)
    monkeypatch.setattr(rb.x_client, "get_x_client", lambda: None)
    monkeypatch.setattr(rb.fbpub, "fetch_insights", lambda pid, tok: (
        {"impressions": 300, "engagements": 9, "raw": {}}, None))
    monkeypatch.setattr(rb.notify, "send", lambda t: True)
    monkeypatch.setenv("FACE_PAGE_TOKEN", "tok")
    out = rb.run_metrics("live", now)
    assert [r["channel"] for r in rows] == ["fb"]
    assert {"channel": "x", "error": "NO_CREDENTIALS"} in out["errors"]


def test_metrics_all_null_does_not_consume_checkpoint(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    now = datetime(2026, 11, 10, tzinfo=UTC)
    ts = (now - timedelta(hours=30)).isoformat()
    monkeypatch.setattr(rb.store, "list_metric_targets", lambda n, d: [
        {"id": 7, "x_post_id": None, "x_posted_at": None, "fb_post_id": "p_222",
         "fb_posted_at": ts, "collected": set()}])
    rows = []
    monkeypatch.setattr(rb.store, "insert_metric", lambda r: rows.append(r) or True)
    monkeypatch.setattr(rb.fbpub, "fetch_insights", lambda pid, tok: (
        {"impressions": None, "engagements": None, "raw": {}}, None))
    monkeypatch.setenv("FACE_PAGE_TOKEN", "tok")
    out = rb.run_metrics("live", now)
    assert rows == [] and out["collected"] == 0


def test_finish_logs_summary_line(env, caplog):
    _, now, _ = env
    with caplog.at_level("INFO"):
        rb.run_post("dry_run", now)
    line = [r.message for r in caplog.records if "[Backdata] 종료" in r.message]
    assert line and "EXIT_OK" in line[0] and "condition_key" in line[0]


def test_preflight_logs_failed_checks_with_masked_token(monkeypatch, tmp_path, caplog):
    monkeypatch.chdir(tmp_path)
    token = "EAAB_SECRET_TOKEN_123"
    monkeypatch.setenv("FACE_PAGE_ID", "556677889900")
    monkeypatch.setenv("FACE_PAGE_TOKEN", token)
    sent = []
    monkeypatch.setattr(rb.notify, "send", lambda t: sent.append(t) or True)
    monkeypatch.setattr(rb.fbpub.fbclient, "fetch_page_identity",
                        lambda p, t: ({"id": "556677889900"}, None))
    def gg(path, t, params):
        if path == "me":
            return {"id": "556677889900"}, None, None
        return {"data": [{"id": "p_1"}]}, None, None
    monkeypatch.setattr(rb.fbpub.fbclient, "graph_get", gg)
    monkeypatch.setattr(rb.fbpub, "fetch_insights",
                        lambda pid, t: (None, "graph_error code=100 message=invalid metric"))
    monkeypatch.setattr(rb.x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(rb, "_x_account_ok", lambda c: True)
    with caplog.at_level("INFO"):
        out = rb.run_preflight("dry_run")
    assert out["exit_reason"] == "EXIT_PREFLIGHT_FAIL" and out["failed"] == ["fb_insights_read"]
    assert any("invalid metric" in r.message for r in caplog.records)
    assert sent and "fb_insights_read=graph_error code=100" in sent[0]
    assert token not in caplog.text and all(token not in s for s in sent)


def test_preflight_does_not_log_x_user_id(monkeypatch, tmp_path, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FACE_PAGE_ID", raising=False)
    monkeypatch.delenv("FACE_PAGE_TOKEN", raising=False)
    monkeypatch.setattr(rb.notify, "send", lambda t: True)

    class Me:
        id = 1914000000000000555

    class Resp:
        data = Me()

    class C:
        def get_me(self, user_auth=True):
            return Resp()
    monkeypatch.setattr(rb.x_client, "get_x_client", lambda: C())
    with caplog.at_level("INFO"):
        out = rb.run_preflight("dry_run")
    assert out["checks"]["x_get_me"] == "ok"
    assert "1914000000000000555" not in caplog.text


def test_preflight_scrubs_page_id_from_checks(monkeypatch, tmp_path, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FACE_PAGE_ID", "998877665544")
    monkeypatch.setenv("FACE_PAGE_TOKEN", "EAAB_SECRET_TOKEN_123")
    sent = []
    monkeypatch.setattr(rb.notify, "send", lambda t: sent.append(t) or True)
    monkeypatch.setattr(rb.fbpub.fbclient, "fetch_page_identity",
                        lambda p, t: (None, {"code": 100, "message": "no object 998877665544"}))
    monkeypatch.setattr(rb.fbpub.fbclient, "graph_get", lambda path, t, params: (None, None, None))
    monkeypatch.setattr(rb, "_x_account_ok", lambda c: True)
    monkeypatch.setattr(rb.x_client, "get_x_client", lambda: object())
    with caplog.at_level("INFO"):
        rb.run_preflight("dry_run")
    assert "998877665544" not in caplog.text and all("998877665544" not in s for s in sent)



def _preflight_env(monkeypatch, tmp_path, routes):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FACE_PAGE_ID", "998877665544")
    monkeypatch.setenv("FACE_PAGE_TOKEN", "EAAB_SECRET_TOKEN_123")
    monkeypatch.setattr(rb.notify, "send", lambda t: True)
    monkeypatch.setattr(rb.fbpub.fbclient, "fetch_page_identity",
                        lambda p, t: ({"id": p}, None))
    monkeypatch.setattr(rb.fbpub.fbclient, "graph_get",
                        lambda path, t, params: routes.get(path.split("/")[-1] if "/" in path
                                                           else path, (None, None, None)))
    monkeypatch.setattr(rb, "_x_account_ok", lambda c: True)
    monkeypatch.setattr(rb.x_client, "get_x_client", lambda: object())


def test_preflight_detects_user_token(monkeypatch, tmp_path, caplog):
    feed_err = {"code": 10, "message": "requires pages_read_engagement"}
    _preflight_env(monkeypatch, tmp_path, {
        "me": ({"id": "1111222233334444"}, None, None),
        "permissions": ({"data": [
            {"permission": "pages_read_engagement", "status": "granted"},
            {"permission": "pages_manage_posts", "status": "granted"},
            {"permission": "read_insights", "status": "declined"}]}, None, None),
        "feed": (None, feed_err, None),
    })
    with caplog.at_level("INFO"):
        out = rb.run_preflight("dry_run")
    c = out["checks"]
    assert c["fb_token_kind"].startswith("user_token")
    assert c["fb_token_permissions"] == "pages_manage_posts,pages_read_engagement"
    assert c["fb_feed_read"].startswith("graph_error code=10")
    assert c["fb_insights_read"].startswith("skipped")
    assert set(out["failed"]) == {"fb_token_kind", "fb_feed_read"}
    assert "1111222233334444" not in caplog.text and "998877665544" not in caplog.text


def test_preflight_page_token_match_passes(monkeypatch, tmp_path):
    _preflight_env(monkeypatch, tmp_path, {
        "me": ({"id": "998877665544"}, None, None),
        "feed": ({"data": [{"id": "998877665544_1"}]}, None, None),
    })
    monkeypatch.setattr(rb.fbpub, "fetch_insights", lambda pid, t: ({"raw": {}}, None))
    out = rb.run_preflight("dry_run")
    assert out["exit_reason"] == "EXIT_OK"
    assert out["checks"]["fb_token_kind"] == "page_token_match"
