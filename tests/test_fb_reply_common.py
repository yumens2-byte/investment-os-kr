"""FB-1 공통화 회귀: X 기본 동작 보존(바이트·테이블·상한) + 플랫폼 파라미터 동작."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import run_reply
from reply_engine import budget as budget_mod
from reply_engine import classifier, config, db_audit, generator, store, telemetry
from reply_engine import filter as filter_mod
from reply_engine.facebook import config as fbc

ROOT = Path(__file__).resolve().parents[1]

# 2026-10-11 역할 반전 방지 지침을 명시적으로 추가한 생성 프롬프트 기준.
# 분류 프롬프트는 2026-10-07 공통화 직전(main 352db47) 원본을 유지한다.
X_GENERATOR_PROMPT_SHA256 = "11d5c831a3427e1159315b9dc7e0ddbcbb81b8f9ff34cf594d5b9b968ebca912"
X_CLASSIFIER_PROMPT_SHA256 = "cab72b465686c338af8d8582f8466db6c146773bd8cb958f97c79bde06586c90"

_GEN_ITEMS = [
    {"id": "500", "text": "자료 정리 감사합니다", "label": "POSITIVE",
     "parent_text": "시장 정보 원문", "parent_author_id": "111", "root_author_id": "111"},
    {"id": "501", "text": "ㅋㅋㅋ 오늘 장 미쳤네요", "label": "SUPPORTIVE_NEUTRAL"},
]


def _capture(monkeypatch, module):
    prompts: list[str] = []

    def fake(**kwargs):
        prompts.append(kwargs["prompt"])
        assert kwargs["allow_paid"] is False  # 무료 키 우선 원칙 (X·FB 공통)
        return {"success": False, "error": "offline"}

    monkeypatch.setattr(module, "gemini_call", fake)
    return prompts


# ── X 바이트 보존 (X-R02) ─────────────────────────────────────

def test_x_generator_prompt_is_byte_identical(monkeypatch):
    prompts = _capture(monkeypatch, generator)
    generator.generate_batch(_GEN_ITEMS)
    assert hashlib.sha256(prompts[0].encode()).hexdigest() == X_GENERATOR_PROMPT_SHA256


def test_x_classifier_prompt_is_byte_identical(monkeypatch):
    prompts = _capture(monkeypatch, classifier)
    classifier.classify_batch([{"id": "600", "text": "음 글쎄 어떨지", "parent_text": "원문"}])
    assert hashlib.sha256(prompts[0].encode()).hexdigest() == X_CLASSIFIER_PROMPT_SHA256


def test_facebook_prompt_changes_only_persona(monkeypatch):
    prompts = _capture(monkeypatch, generator)
    generator.generate_batch(_GEN_ITEMS)
    generator.generate_batch(_GEN_ITEMS, platform="facebook")
    x_prompt, fb_prompt = prompts
    assert fb_prompt.startswith(generator.PERSONAS["facebook"])
    x_body = x_prompt[len(generator.PERSONAS["x"]):]
    fb_body = fb_prompt[len(generator.PERSONAS["facebook"]):]
    assert x_body == fb_body


def test_unknown_platform_is_rejected_not_defaulted(monkeypatch):
    _capture(monkeypatch, generator)
    with pytest.raises(ValueError):
        generator.generate_batch(_GEN_ITEMS, platform="threads")


def test_chunked_generation_keeps_platform(monkeypatch):
    prompts = _capture(monkeypatch, generator)
    items = [{"id": str(i), "text": "감사합니다", "label": "POSITIVE"} for i in range(25)]
    generator.generate_batch(items, platform="facebook")
    assert len(prompts) == 2 and all("Facebook 페이지" in p for p in prompts)


# ── store 테이블 분리 (FB-S01/S02, XC) ───────────────────────

class _Recorder:
    def __init__(self, rows=None):
        self.tables: list[str] = []
        self.rows = rows or []

    def table(self, name):
        self.tables.append(name)
        return self

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": list(self.rows), "count": 0})()


def test_x_tables_and_defaults_unchanged():
    assert (store._T_HISTORY, store._T_CURSOR, store._T_BUDGET, store._T_BLACKLIST,
            store._T_LIKES) == ("kr_reply_history", "kr_reply_cursor", "kr_reply_budget",
                                "kr_reply_blacklist", "kr_reply_likes")
    assert store.X_TABLES.history == "kr_reply_history"
    assert store.X_TABLES.max_age_hours is None  # 호출 시점 모듈 전역 사용 (patch 규약 보존)


def test_x_default_calls_hit_only_kr_tables(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(store, "get_client", lambda: rec)
    store.history_exists("1")
    store.count_responded_today()
    store.get_recent_response_texts()
    store.get_cursor("kr_main")
    store.get_blacklist_ids()
    store.get_retryable_history()
    assert rec.tables and all(t.startswith("kr_reply_") for t in rec.tables)


def test_bind_routes_every_function_to_fb_tables(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(store, "get_client", lambda: rec)  # 바인딩 뷰에도 patch가 적용돼야 함
    repo = store.bind(fbc.FB_TABLES)
    repo.history_exists("1")
    repo.insert_history({"reply_tweet_id": "1"})
    repo.count_responded_today()
    repo.history_exists_bulk(["1"])
    repo.count_author_responded_today_bulk(["a"])
    repo.count_conversation_responded_today_bulk(["c"])
    repo.get_recent_response_texts(5)
    repo.get_cursor("fb_main")
    repo.upsert_cursor("fb_main", "2026-10-07T00:00:00+00:00", "777")
    repo.get_budget("2026-10-07")
    repo.upsert_budget({"budget_date": "2026-10-07"})
    repo.get_blacklist_ids()
    repo.get_retryable_history(10)
    repo.expire_deferred("777")
    assert rec.tables and all(t.startswith("fb_reply_") for t in rec.tables)
    assert not any("kr_reply" in t for t in rec.tables)


def test_bind_refuses_x_tables():
    with pytest.raises(ValueError):
        store.bind(store.X_TABLES)


def test_fb_time_windows_are_independent(monkeypatch):
    monkeypatch.setattr(store, "REPLY_RETRY_WINDOW_HOURS", 1)
    tables = store.ReplyTables("h", "c", "b", "l", retry_window_hours=48, max_age_hours=12)
    fb_cut = datetime.fromisoformat(store._retry_cutoff_iso(tables=tables))
    x_cut = datetime.fromisoformat(store._retry_cutoff_iso())
    assert x_cut - fb_cut > timedelta(hours=46)
    assert store._max_age_hours(tables) == 12


# ── filter / budget / config 파라미터 ────────────────────────

def _tweet(**overrides):
    base = {"id": "1", "text": "좋은 정보 감사합니다", "author_id": "222", "conversation_id": "9",
            "in_reply_to_user_id": "777", "created_at": datetime.now(UTC) - timedelta(hours=30)}
    return {**base, **overrides}


def test_check_tweet_max_age_override():
    assert filter_mod.check_tweet(_tweet(), None, "777", set()) == (False, "EXPIRED")
    assert filter_mod.check_tweet(_tweet(), None, "777", set(), max_age_hours=48) == (True, None)


def test_check_and_admit_caps_override_without_touching_x_constants():
    ctx = filter_mod.CapContext(bulk_ready=True, author_today={"222": 1})
    caps = filter_mod.CapLimits(author_daily=2, conversation_daily=5)
    assert filter_mod.check_and_admit(_tweet(), ctx, caps=caps) == (True, None)
    ctx2 = filter_mod.CapContext(bulk_ready=True, author_today={"222": 1})
    assert filter_mod.check_and_admit(_tweet(), ctx2) == (False, "AUTHOR_CAP")


def test_build_cap_context_uses_injected_repo():
    class Repo:
        def __init__(self):
            self.calls = []

        def history_exists_bulk(self, ids):
            self.calls.append("dup")
            return set()

        def count_author_responded_today_bulk(self, ids):
            self.calls.append("author")
            return {}

        def count_conversation_responded_today_bulk(self, ids):
            self.calls.append("conv")
            return {}

    repo = Repo()
    filter_mod.build_cap_context([_tweet()], repo=repo)
    assert repo.calls == ["dup", "author", "conv"]


def test_budget_injection_and_x_default(monkeypatch):
    monkeypatch.delenv("X_READ_COST_KRW", raising=False)
    monkeypatch.delenv("X_WRITE_COST_KRW", raising=False)
    row = {"read_calls": 16, "write_calls": 5, "gemini_calls": 0, "est_cost_krw": 0}
    x_guard = budget_mod.BudgetGuard(row)
    assert (x_guard.can_read(), x_guard.can_write()) == (False, False)  # X fallback 16/5 유지
    fb_guard = budget_mod.BudgetGuard(row, costs=(None, None), limit_krw=0.0, fallback=(120, 10))
    assert (fb_guard.can_read(), fb_guard.can_write()) == (True, True)
    assert fb_guard.available_read_calls(200) == 104
    assert fb_guard.snapshot()["mode"] == "count"


def test_kill_switch_and_mode_are_independent(monkeypatch):
    monkeypatch.setenv("REPLY_ENABLED", "true")
    monkeypatch.setenv("REPLY_MODE", "live")
    monkeypatch.delenv("FACE_REPLY_ENABLED", raising=False)
    monkeypatch.setenv("FACE_REPLY_MODE", "true")  # 2026-08-20 오입력 유형 → dry_run 강등
    assert config.is_enabled() and config.get_mode() == "live"
    assert not fbc.is_face_enabled() and fbc.get_face_mode() == "dry_run"
    monkeypatch.setenv("FACE_REPLY_ENABLED", "TRUE ")
    monkeypatch.setenv("REPLY_ENABLED", "false")
    assert fbc.is_face_enabled() and not config.is_enabled()


def test_face_caps_do_not_read_x_variables():
    """X-R03/XC-06: 프로세스 격리로 import 시점 상수를 검증한다 (reload 오염 방지)."""
    env = {**os.environ, "REPLY_DAILY_CAP": "50", "REPLY_RUN_CAP": "10",
           "FACE_REPLY_DAILY_CAP": "3", "FACE_REPLY_RUN_CAP": "999"}
    code = (
        "import json, run_reply; from reply_engine.facebook import config as f;"
        "print(json.dumps([run_reply.REPLY_DAILY_CAP, run_reply.REPLY_RUN_CAP,"
        " f.FACE_REPLY_DAILY_CAP, f.FACE_REPLY_RUN_CAP]))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True,
                         capture_output=True, text=True).stdout.strip().splitlines()[-1]
    assert json.loads(out) == [50, 10, 3, 1]  # 범위 밖 FACE 값은 기본값 복귀


def test_fb_config_defaults():
    assert fbc.FB_TABLES.history == "fb_reply_history"
    assert fbc.GRAPH_BASE == "https://graph.facebook.com/v25.0"
    assert fbc.FACE_REPLY_THREAD_ENABLED is False  # D10 opt-in 기본 off


# ── db_audit / telemetry 공통 ────────────────────────────────

def test_db_audit_fb_contracts(monkeypatch):
    from scripts.check_fb_reply_db import FB_REQUIRED_TABLE_CONTRACTS

    sampled: list[str] = []

    def fake_sample(table, columns, limit):
        sampled.append(table)
        if table == "fb_reply_cursor":
            raise type("APIError", (Exception,), {"code": "42P01"})()
        return []

    monkeypatch.setattr(db_audit, "_sample", fake_sample)
    report = db_audit.audit_reply_db(
        required_contracts=FB_REQUIRED_TABLE_CONTRACTS, optional_contracts={},
        history_table="fb_reply_history",
    )
    assert set(sampled) == set(FB_REQUIRED_TABLE_CONTRACTS)
    assert not any(t.startswith("kr_") for t in sampled)
    assert report["healthy"] is False and report["schema_error_codes"] == {
        "fb_reply_cursor": "42P01"}
    assert FB_REQUIRED_TABLE_CONTRACTS["fb_reply_history"] == (
        db_audit.REQUIRED_TABLE_CONTRACTS["kr_reply_history"])


def test_db_audit_rejects_history_outside_required():
    with pytest.raises(ValueError):
        db_audit.audit_reply_db(required_contracts={"a": "x"}, history_table="b")


def test_x_report_and_journal_paths_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = {"mode": "dry_run", "collected": 1, "processed": 1, "review": [], "run_id": "r"}
    run_reply._write_report(summary)
    telemetry.event({"run_id": "r"}, "TEST", "1")
    telemetry.event({"run_id": "r", "journal_path": "logs/fb_reply_events.jsonl"}, "TEST", "2")
    assert len(list((tmp_path / "logs").glob("reply_report_*.json"))) == 1
    assert (tmp_path / "logs/reply_events.jsonl").read_text().count("\n") == 1
    assert (tmp_path / "logs/fb_reply_events.jsonl").read_text().count("\n") == 1
    assert summary["funnel"]["candidate_rate"] == 0.0
