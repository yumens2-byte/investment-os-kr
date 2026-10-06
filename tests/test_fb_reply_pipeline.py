"""FB-1 통합: 실제 파이프라인·PostgREST SDK·Gemini 게이트웨이, 전송 경계(DB HTTP·Graph HTTP)만 대체.

X 하네스(tests/test_reply_sdk_integration.OfflineServices)를 그대로 재사용해 X와 FB를 같은
가짜 DB 위에서 함께 실행하고, 테이블 교차오염이 없음을 HTTP 경계에서 검증한다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests
import tweepy
from postgrest import SyncPostgrestClient

import run_fb_reply
import run_reply
from core import gemini_gateway
from db import supabase_client
from reply_engine.facebook import client as fb_client
from reply_engine.facebook import config as fbc
from reply_engine.policy import decode_metadata
from tests.test_reply_sdk_integration import OfflineServices

PAGE = "777"
TOKEN = "EAAfake-page-token-000000"


def _ts(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._payload, self.status_code, self.headers = payload, status, headers or {}

    def json(self):
        return self._payload


class FakeGraph:
    """Graph API 경계. 발행 POST는 DB claim(PUBLISHING) 이후에만 허용한다."""

    def __init__(self, db: OfflineServices):
        self.db = db
        self.posts = [{"id": f"{PAGE}_1", "message": "오늘의 시장 정리", "from": {"id": PAGE},
                       "is_published": True}]
        self.comments: dict[str, list[dict]] = {f"{PAGE}_1": []}
        self.gets: list[str] = []
        self.posts_made: list[tuple[str, str]] = []
        self.post_behaviors: list = []  # "ok" | "timeout" | {"code": ...}
        self.feed_error: dict | None = None
        self.comment_errors: dict[str, dict] = {}
        self.comment_headers: dict = {}

    def add_comment(self, cid, text="좋은 정리 감사합니다", author="u1", *, minutes=5,
                    parent=None, post=None, can_comment=True, include_from=True):
        raw = {"id": cid, "message": text, "created_time": _ts(minutes),
               "can_comment": can_comment}
        if include_from:
            raw["from"] = {"id": author, "name": "실명"}
        if parent:
            raw["parent"] = parent
        self.comments.setdefault(post or f"{PAGE}_1", []).insert(0, raw)

    def get(self, url, params=None, timeout=None):
        self.gets.append(url)
        if url.endswith("/feed"):
            if self.feed_error:
                return _Resp({"error": self.feed_error}, 400)
            return _Resp({"data": self.posts})
        post_id = url.rsplit("/", 2)[-2]
        if post_id in self.comment_errors:
            return _Resp({"error": self.comment_errors[post_id]}, 400)
        return _Resp({"data": list(self.comments.get(post_id, []))},
                     headers=self.comment_headers)

    def post(self, url, data=None, timeout=None):
        comment_id = url.rsplit("/", 2)[-2]
        assert any(
            row.get("skip_reason") == "PUBLISHING" and row["reply_tweet_id"] == comment_id
            for row in self.db.tables.get("fb_reply_history", [])
        ), "Graph publication must occur only after durable publication claim"
        behavior = self.post_behaviors.pop(0) if self.post_behaviors else "ok"
        self.posts_made.append((comment_id, data["message"]))
        if behavior == "timeout":
            raise requests.Timeout("transport timeout")
        if isinstance(behavior, dict):
            return _Resp({"error": behavior}, 400)
        return _Resp({"id": f"{comment_id}_r{len(self.posts_made)}"})


@pytest.fixture
def env(monkeypatch, tmp_path):
    db = OfflineServices()
    graph = FakeGraph(db)
    alerts: list[str] = []
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "FACE_REPLY_ENABLED": "true", "FACE_REPLY_MODE": "live", "FACE_PAGE_ID": PAGE,
        "FACE_PAGE_TOKEN": TOKEN, "GEMINI_API_KEY": "fake",
        # X (교차 실행 시나리오용)
        "REPLY_ENABLED": "true", "REPLY_MODE": "live", "X_MY_USER_ID": "111",
        "X_API_KEY": "fake", "X_API_SECRET": "fake", "X_ACCESS_TOKEN": "fake",
        "X_ACCESS_TOKEN_SECRET": "fake", "X_READ_COST_KRW": "0", "X_WRITE_COST_KRW": "0",
        "REPLY_LIKE_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("GEMINI_API_SUB_KEY", "GEMINI_API_SUB_SUB_KEY", "GEMINI_API_SUB_PAY_KEY",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALERT_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gemini_gateway, "GEMINI_API_KEY", "fake")
    for name in ("GEMINI_API_SUB_KEY", "GEMINI_API_SUB_SUB_KEY", "GEMINI_API_SUB_PAY_KEY"):
        monkeypatch.setattr(gemini_gateway, name, "")
    monkeypatch.setattr(
        gemini_gateway, "_get_client",
        lambda _key: SimpleNamespace(models=SimpleNamespace(generate_content=db.generate)),
    )
    monkeypatch.setattr(run_fb_reply.time, "sleep", lambda _s: None)
    monkeypatch.setattr(run_fb_reply, "send_admin_alert", alerts.append)
    monkeypatch.setattr(fb_client, "_http_get", graph.get)
    monkeypatch.setattr(fb_client, "_http_post", graph.post)
    # X 경계 (교차 시나리오) — run_reply 기존 하네스와 동일
    monkeypatch.setattr(run_reply, "REPLY_RUN_CAP", 2)
    monkeypatch.setattr(run_reply, "STARTUP_JITTER_MAX_SEC", 0)
    monkeypatch.setattr(run_reply.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        tweepy.Client, "request",
        lambda client, method, route, **kw: db.twitter_http(client, method, route, **kw),
    )
    with httpx.Client(transport=httpx.MockTransport(db.database_http)) as http:
        api = SyncPostgrestClient("https://offline.test/rest/v1", http_client=http)
        monkeypatch.setattr(supabase_client, "_client", SimpleNamespace(table=api.from_))
        yield SimpleNamespace(db=db, graph=graph, alerts=alerts, tmp=tmp_path)


def _tables_touched(db: OfflineServices, start: int = 0) -> set[str]:
    return {req.url.path.rsplit("/", 1)[-1] for req in db.db_requests[start:]}


def _mutations(db: OfflineServices, start: int = 0) -> list:
    return [r for r in db.db_requests[start:] if r.method in {"POST", "PATCH", "DELETE"}]


def _row(db, cid):
    return next(r for r in db.tables["fb_reply_history"] if r["reply_tweet_id"] == cid)


# ── FB-I03 live 정상 ─────────────────────────────────────────

def test_live_claim_publish_commit_cursor(env):
    env.graph.add_comment("c1")
    result = run_fb_reply.main()
    assert result["exit_reason"] == "EXIT_OK" and result["actual_published"] == 1
    row = _row(env.db, "c1")
    assert row["responded"] and row["response_tweet_id"] == "c1_r1"
    assert row["mode"] == "live" and row["author_username"] == ""
    meta = decode_metadata(row["error_message"])
    assert meta["publish_attempts"] == 1 and meta["platform"] == "facebook"
    assert meta["account_user_id"] == PAGE
    assert env.graph.posts_made[0][0] == "c1" and len(env.graph.posts_made[0][1]) <= 40
    assert result["cursor_advanced"]
    assert env.db.tables["fb_reply_cursor"][0]["account"] == "fb_main"
    budget = env.db.tables["fb_reply_budget"][0]
    assert budget["write_calls"] == 1 and budget["read_calls"] == 2  # feed 1 + comments 1
    # XC-01 / XC-03: FB 실행은 FB 테이블만, X API 호출 0
    assert _tables_touched(env.db) <= {
        "fb_reply_history", "fb_reply_cursor", "fb_reply_budget", "fb_reply_blacklist"}
    assert env.db.x_requests == []


def test_report_json_is_written(env):
    env.graph.add_comment("c1")
    run_fb_reply.main()
    reports = list(Path("logs").glob("fb_reply_report_*.json"))
    assert len(reports) == 1 and not list(Path("logs").glob("reply_report_*.json"))
    report = json.loads(reports[0].read_text())
    assert report["platform"] == "facebook" and report["budget"]["mode"] == "count"
    assert report["review"][0]["result"] == "PUBLISHED"
    assert Path("logs/fb_reply_events.jsonl").exists()
    assert TOKEN not in reports[0].read_text()


# ── FB-I01 / I02 모드 ────────────────────────────────────────

def test_dry_run_makes_no_db_mutation_and_no_publish(env, monkeypatch):
    monkeypatch.setenv("FACE_REPLY_MODE", "dry_run")
    env.graph.add_comment("c1")
    result = run_fb_reply.main()
    assert result["simulated"] == 1 and result["actual_published"] == 0
    assert _mutations(env.db) == [] and env.graph.posts_made == []


def test_shadow_records_then_live_reevaluates(env, monkeypatch):
    monkeypatch.setenv("FACE_REPLY_MODE", "shadow")
    env.graph.add_comment("c1")
    shadow = run_fb_reply.main()
    assert shadow["simulated"] == 1 and env.graph.posts_made == []
    assert _row(env.db, "c1")["mode"] == "shadow"
    assert env.db.tables["fb_reply_cursor"][0]["account"] == "fb_main:shadow"
    monkeypatch.setenv("FACE_REPLY_MODE", "live")
    live = run_fb_reply.main()
    assert live["actual_published"] == 1 and _row(env.db, "c1")["mode"] == "live"


def test_mode_typo_degrades_to_dry_run(env, monkeypatch):
    monkeypatch.setenv("FACE_REPLY_MODE", "true")
    env.graph.add_comment("c1")
    assert run_fb_reply.main()["mode"] == "dry_run" and env.graph.posts_made == []


def test_disabled_makes_no_external_call(env, monkeypatch):
    monkeypatch.setenv("FACE_REPLY_ENABLED", "false")
    env.graph.add_comment("c1")
    assert run_fb_reply.main()["exit_reason"] == "EXIT_DISABLED"
    assert env.graph.gets == [] and env.db.db_requests == []


def test_missing_credentials(env, monkeypatch):
    monkeypatch.setenv("FACE_PAGE_ID", "tiger18272")  # 숫자 아님 → 오등록 차단
    assert run_fb_reply.main()["exit_reason"] == "EXIT_NO_CREDENTIALS"
    assert env.graph.gets == []


# ── 멱등성 / 결과 불명 / 보류 복구 ───────────────────────────

def test_rerun_never_reposts(env):
    env.graph.add_comment("c1")
    run_fb_reply.main()
    second = run_fb_reply.main()
    assert len(env.graph.posts_made) == 1 and second["already_processed"] >= 1
    assert second["exit_reason"] == "EXIT_NO_COMMENTS"


def test_timeout_is_unknown_and_never_reposted(env):
    env.graph.add_comment("c1")
    env.graph.post_behaviors = ["timeout"]
    first = run_fb_reply.main()
    assert first["skip_reasons"].get("PUBLISH_UNKNOWN") == 1
    assert _row(env.db, "c1")["skip_reason"] == "PUBLISH_UNKNOWN"
    assert any("PUBLISH_UNKNOWN" in a for a in env.alerts)
    run_fb_reply.main()
    assert len(env.graph.posts_made) == 1  # 재발행 금지


def test_run_cap_defers_then_recovers(env):
    env.graph.add_comment("c1", author="u1", minutes=10)
    env.graph.add_comment("c2", author="u2", minutes=5)
    first = run_fb_reply.main()
    assert first["actual_published"] == 1 and first["skip_reasons"].get("RUN_CAP") == 1
    assert _row(env.db, "c2")["skip_reason"] == "RUN_CAP"
    second = run_fb_reply.main()
    assert second["recovered_failures"] == 1 and second["actual_published"] == 1
    assert {cid for cid, _ in env.graph.posts_made} == {"c1", "c2"}


def test_same_author_once_per_day_r2(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_RUN_CAP", 3)
    env.graph.add_comment("c1", author="u1", minutes=10)
    env.graph.add_comment("c2", text="정리 최고네요", author="u1", minutes=5)
    result = run_fb_reply.main()
    assert result["actual_published"] == 1
    assert result["skip_reasons"].get("AUTHOR_CAP_RUN") == 1


def test_daily_cap_counts_only_fb_history(env):
    # X 테이블에 오늘 발행 8건이 있어도 FB 일일 상한 판정에 섞이지 않는다 (XC-05).
    env.db.tables["kr_reply_history"] = [
        {"reply_tweet_id": str(900 + i), "responded": True, "response_tweet_id": str(i),
         "created_at": datetime.now(UTC).isoformat(), "mode": "live"} for i in range(8)
    ]
    env.graph.add_comment("c1")
    assert run_fb_reply.main()["actual_published"] == 1


# ── 플랫폼 오류 정지 ─────────────────────────────────────────

def test_throttle_on_publish_halts_and_returns_reservation(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_RUN_CAP", 2)
    env.graph.add_comment("c1", author="u1", minutes=10)
    env.graph.add_comment("c2", author="u2", minutes=5)
    env.graph.post_behaviors = [{"code": 4, "message": "Application request limit reached"}]
    result = run_fb_reply.main()
    assert result["publish_halt"] == "THROTTLE" and len(env.graph.posts_made) == 1
    assert _row(env.db, "c1")["skip_reason"] == "PUBLISH_RETRYABLE"
    assert not any(r["reply_tweet_id"] == "c2" for r in env.db.tables["fb_reply_history"])
    assert any(r["result"] == "HALTED_THROTTLE" for r in result["review"])
    assert any("THROTTLE" in a and a.startswith("[FB Reply]") for a in env.alerts)


def test_policy_block_368_halts(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_RUN_CAP", 2)
    env.graph.add_comment("c1", author="u1", minutes=10)
    env.graph.add_comment("c2", author="u2", minutes=5)
    env.graph.post_behaviors = [{"code": 368, "message": "blocked"}]
    result = run_fb_reply.main()
    assert result["publish_halt"] == "POLICY_BLOCK" and len(env.graph.posts_made) == 1
    assert _row(env.db, "c1")["skip_reason"] == "PUBLISH_REJECTED"


def test_feed_auth_error_exits_with_failure_code(env):
    env.graph.feed_error = {"code": 190, "message": f"expired {TOKEN}"}
    result = run_fb_reply.main()
    assert result["exit_reason"] == "EXIT_AUTH"
    mutated = {m.url.path.rsplit("/", 1)[-1] for m in _mutations(env.db)}
    assert mutated == {"fb_reply_budget"}  # 읽기 1콜 계상만, 이력·커서 변경 없음
    assert env.alerts and TOKEN not in json.dumps(result, default=str)


# ── 응답 범위 (D10) / 보수적 스킵 ────────────────────────────

def test_scope_rules_end_to_end(env):
    g = env.graph
    g.posts.append({"id": "visitor_post", "from": {"id": "u9"}})
    g.add_comment("v1", post="visitor_post")                       # 방문자 게시물 → 미수집
    g.add_comment("ok", text="좋은 정리 감사합니다", author="u1", minutes=30)
    g.add_comment("self", text="읽어주셔서 감사합니다", author=PAGE)  # 내 Page 댓글
    g.add_comment("third", text="축하해주셔서 감사합니다", author="u3",
                  parent={"id": "x", "from": {"id": "u4"}})          # 제3자 간 대화
    g.add_comment("thread", text="네 감사해요", author="u5",
                  parent={"id": "self", "from": {"id": PAGE}})       # 내 댓글의 대댓글
    g.add_comment("anon", text="감사", include_from=False)          # from 미반환
    g.add_comment("q", text="이거 어떻게 보세요?", author="u6")
    g.add_comment("closed", text="정리 고마워요", author="u7", can_comment=False)
    result = run_fb_reply.main()
    reasons = result["skip_reasons"]
    assert reasons.get("SELF") == 1 and reasons.get("OUT_OF_SCOPE") == 1
    assert reasons.get("OUT_OF_SCOPE_THREAD") == 1 and reasons.get("AUTHOR_UNVERIFIED") == 1
    assert reasons.get("CLASS_QUESTION") == 1 and reasons.get("CANNOT_REPLY") == 1
    assert [cid for cid, _ in env.graph.posts_made] == ["ok"]
    assert result["posts_skipped"] == 1
    assert not any(r["reply_tweet_id"] == "v1" for r in env.db.tables["fb_reply_history"])


def test_thread_reply_opt_in(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_THREAD_ENABLED", True)
    env.graph.add_comment("thread", text="네 감사해요", author="u5",
                          parent={"id": "mine", "from": {"id": PAGE},
                                  "message": "읽어주셔서 감사합니다"})
    result = run_fb_reply.main()
    assert result["actual_published"] == 1 and result["thread_replies"] == 1
    assert decode_metadata(_row(env.db, "thread")["error_message"])["fb_thread"] is True


def test_expired_comment_is_not_published(env):
    env.graph.add_comment("old", minutes=60 * 25)
    result = run_fb_reply.main()
    # 수집 단계 TTL 컷으로 미수집 (Graph 최신순 중단) → 발행 0
    assert env.graph.posts_made == [] and result["collected"] == 0


def test_foreign_language_uses_template_not_ai(env):
    env.graph.add_comment("en", text="Great summary, thanks a lot", author="u1")
    result = run_fb_reply.main()
    review = next(r for r in result["review"] if r["reply_tweet_id"] == "en")
    assert review["source"] == "TEMPLATE_NON_KR" and result["actual_published"] == 1


# ── X ↔ FB 교차 실행 (XC-01~04) ──────────────────────────────

def test_x_and_fb_runs_are_isolated(env):
    env.db.add_mention("500")
    x_start = len(env.db.db_requests)
    x_result = run_reply.main()
    x_tables = _tables_touched(env.db, x_start)
    fb_start = len(env.db.db_requests)
    env.graph.add_comment("c1")
    fb_result = run_fb_reply.main()
    fb_tables = _tables_touched(env.db, fb_start)

    assert x_result["actual_published"] == 1 and fb_result["actual_published"] == 1
    assert x_tables and all(t.startswith("kr_reply_") for t in x_tables)
    assert fb_tables and all(t.startswith("fb_reply_") for t in fb_tables)
    assert env.graph.gets and all("graph.facebook.com" in u for u in env.graph.gets)
    x_posts = [r for r in env.db.x_requests if r[0] == "POST"]
    assert len(x_posts) == 1 and len(env.graph.posts_made) == 1
    assert len(env.db.tables["kr_reply_history"]) == 1
    assert len(env.db.tables["fb_reply_history"]) == 1
    assert env.db.tables["kr_reply_cursor"][0]["account"] == "kr_main"
    assert env.db.tables["fb_reply_cursor"][0]["account"] == "fb_main"


# ── 코드리뷰 반영 회귀 (2026-10-07 1차 리뷰 #1·#2·#5·#8, 뮤턴트 M9) ──────────

def _seed_live_row(db, cid, *, skip_reason, author="u1", minutes=5, **meta_overrides):
    from reply_engine.policy import encode_metadata

    created = datetime.now(UTC) - timedelta(minutes=minutes)
    meta = json.loads(encode_metadata({
        "id": cid, "text": "좋은 정리 감사합니다", "author_id": author,
        "conversation_id": f"{PAGE}_1", "in_reply_to_user_id": PAGE, "created_at": created,
        "parent_text": "오늘의 시장 정리", "parent_id": f"{PAGE}_1", "parent_author_id": PAGE,
        "_account_user_id": PAGE,
    }))
    meta.update(platform="facebook", **meta_overrides)
    db.tables.setdefault("fb_reply_history", []).append({
        "reply_tweet_id": cid, "conversation_id": f"{PAGE}_1", "author_id": author,
        "author_username": "", "comment_text": "좋은 정리 감사합니다",
        "classification": "POSITIVE", "responded": False, "response_tweet_id": None,
        "response_text": "", "skip_reason": skip_reason, "dry_run": False, "mode": "live",
        "error_message": json.dumps(meta, ensure_ascii=False),
        "created_at": datetime.now(UTC).isoformat(),
    })


def test_unknown_publish_keeps_reservation_m9(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_RUN_CAP", 3)
    env.graph.add_comment("c1", author="u1", minutes=10)
    env.graph.add_comment("c2", text="정리 최고네요", author="u1", minutes=5)
    env.graph.post_behaviors = ["timeout"]
    result = run_fb_reply.main()
    # 결과 불명은 실제로 게시됐을 수 있으므로 작성자 슬롯을 반환하지 않는다.
    assert [cid for cid, _ in env.graph.posts_made] == ["c1"]
    assert result["skip_reasons"].get("AUTHOR_CAP_RUN") == 1


def test_retry_not_refetched_is_rescoped_when_thread_disabled_r1(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_THREAD_ENABLED", True)
    env.graph.add_comment("c1", author="u1", minutes=10)
    env.graph.add_comment("t1", text="네 감사해요", author="u5", minutes=5,
                          parent={"id": "mine", "from": {"id": PAGE}})
    first = run_fb_reply.main()
    assert first["actual_published"] == 1 and _row(env.db, "t1")["skip_reason"] == "RUN_CAP"
    monkeypatch.setattr(fbc, "FACE_REPLY_THREAD_ENABLED", False)
    env.graph.comments[f"{PAGE}_1"] = [c for c in env.graph.comments[f"{PAGE}_1"]
                                        if c["id"] != "t1"]  # 재수집되지 않는 상황
    second = run_fb_reply.main()
    assert second["skip_reasons"].get("OUT_OF_SCOPE_THREAD") == 1
    assert [cid for cid, _ in env.graph.posts_made] == ["c1"]


def test_retry_refetched_applies_current_pre_skip_r1(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_THREAD_ENABLED", True)
    env.graph.add_comment("c1", author="u1", minutes=10)
    env.graph.add_comment("t1", text="네 감사해요", author="u5", minutes=5,
                          parent={"id": "mine", "from": {"id": PAGE}})
    run_fb_reply.main()
    monkeypatch.setattr(fbc, "FACE_REPLY_THREAD_ENABLED", False)
    second = run_fb_reply.main()  # t1 재수집됨 → 정규화 사전 스킵 적용
    assert second["skip_reasons"].get("OUT_OF_SCOPE_THREAD") == 1
    assert [cid for cid, _ in env.graph.posts_made] == ["c1"]


def test_collection_buc_limit_halts_classification_and_publish_r2(env):
    env.graph.posts.append({"id": f"{PAGE}_2", "from": {"id": PAGE}})
    env.graph.add_comment("c1")
    env.graph.comment_headers = {
        "X-Business-Use-Case-Usage": json.dumps({PAGE: [{"call_count": 95}]})}
    result = run_fb_reply.main()
    assert result["collection_halt"] == "BUC_LIMIT" and result["publish_halt"] == "BUC_LIMIT"
    assert env.graph.posts_made == [] and env.db.model_requests == []
    assert not any(r["reply_tweet_id"] == "c1" for r in env.db.tables.get("fb_reply_history", []))
    assert any("BUC_LIMIT" in a for a in env.alerts)


def test_collection_throttle_halts_pipeline(env):
    env.graph.posts.insert(0, {"id": f"{PAGE}_0", "from": {"id": PAGE}})
    env.graph.comment_errors[f"{PAGE}_0"] = {"code": 32, "message": "Page request limit"}
    env.graph.add_comment("c1")
    result = run_fb_reply.main()
    assert result["collection_halt"] == "THROTTLE" and env.graph.posts_made == []
    assert any("THROTTLE" in a for a in env.alerts)


def test_auth_publish_failure_is_deferred_and_halts_r5(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_RUN_CAP", 2)
    env.graph.add_comment("c1", author="u1", minutes=10)
    env.graph.add_comment("c2", author="u2", minutes=5)
    env.graph.post_behaviors = [{"code": 190, "error_subcode": 463, "message": "expired"}]
    result = run_fb_reply.main()
    row = _row(env.db, "c1")
    assert row["skip_reason"] == "PUBLISH_RETRYABLE" and result["publish_halt"] == "AUTH"
    meta = decode_metadata(row["error_message"])
    assert meta["publish_attempts"] == 1 and meta["next_attempt_at"]
    assert "code=190" in meta["platform_error"]
    assert len(env.graph.posts_made) == 1


def test_ready_orphan_is_reevaluated_and_published_once_r8(env):
    _seed_live_row(env.db, "c1", skip_reason=None)
    env.graph.add_comment("c1")
    result = run_fb_reply.main()
    assert result["ready_orphans"] == 1 and result["actual_published"] == 1
    assert run_fb_reply.main()["actual_published"] == 0
    assert len(env.graph.posts_made) == 1


def test_publish_exhausted_after_third_throttle(env):
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    _seed_live_row(env.db, "c1", skip_reason="PUBLISH_RETRYABLE", publish_attempts=2,
                   next_attempt_at=past)
    env.graph.add_comment("c1")
    env.graph.post_behaviors = [{"code": 4, "message": "limit"}]
    result = run_fb_reply.main()
    assert result["recovered_failures"] == 1
    assert _row(env.db, "c1")["skip_reason"] == "PUBLISH_EXHAUSTED"


def test_db_confirmation_failure_alerts_and_blocks_repost(env, monkeypatch):
    from reply_engine import store

    monkeypatch.setattr(store, "mark_responded", lambda *a, **k: False)
    env.graph.add_comment("c1")
    result = run_fb_reply.main()
    assert result["review"][0]["result"] == "PUBLISHED_DB_UNCONFIRMED"
    assert _row(env.db, "c1")["skip_reason"] == "PUBLISHING"
    assert any("DB confirmation failed" in a for a in env.alerts)
    run_fb_reply.main()
    assert len(env.graph.posts_made) == 1


def test_claim_failure_never_publishes(env, monkeypatch):
    from reply_engine import store

    monkeypatch.setattr(store, "claim_publication", lambda *a, **k: None)
    env.graph.add_comment("c1")
    result = run_fb_reply.main()
    assert result["skip_reasons"].get("PUBLISH_CLAIM_FAIL") == 1
    assert env.graph.posts_made == [] and result["cursor_advanced"] is False


def test_expired_before_send_after_delay(env, monkeypatch):
    clock = {"offset": timedelta(0)}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + clock["offset"]

    monkeypatch.setattr(run_fb_reply, "datetime", FakeDateTime)
    def fake_sleep(seconds):
        clock["offset"] += timedelta(seconds=seconds)

    monkeypatch.setattr(run_fb_reply.time, "sleep", fake_sleep)
    monkeypatch.setattr(run_fb_reply.random, "randint", lambda a, b: b)  # 최대 지연
    env.graph.add_comment("c1", minutes=24 * 60 - 5)  # 만료 5분 전 수집 → 지연 15분 경과
    result = run_fb_reply.main()
    assert result["skip_reasons"].get("EXPIRED_BEFORE_SEND") == 1
    assert env.graph.posts_made == []
    assert _row(env.db, "c1")["skip_reason"] == "EXPIRED_BEFORE_SEND"


def test_thread_run_cap(env, monkeypatch):
    monkeypatch.setattr(fbc, "FACE_REPLY_THREAD_ENABLED", True)
    monkeypatch.setattr(fbc, "FACE_REPLY_RUN_CAP", 3)
    for cid, author in (("t1", "u1"), ("t2", "u2")):
        env.graph.add_comment(cid, text="네 감사해요", author=author,
                              parent={"id": "mine", "from": {"id": PAGE}})
    result = run_fb_reply.main()
    assert result["actual_published"] == 1 and result["thread_replies"] == 1
    assert result["skip_reasons"].get("FOREIGN_THREAD_CAP") == 1


def test_duplicate_comment_in_one_fetch_publishes_once(env):
    env.graph.add_comment("c1")
    env.graph.comments[f"{PAGE}_1"].append(dict(env.graph.comments[f"{PAGE}_1"][0]))
    run_fb_reply.main()
    assert len(env.graph.posts_made) == 1


def test_shadow_does_not_reevaluate_live_orphan_n1(env, monkeypatch):
    monkeypatch.setenv("FACE_REPLY_MODE", "shadow")
    _seed_live_row(env.db, "c1", skip_reason=None)
    env.graph.add_comment("c1")
    result = run_fb_reply.main()
    assert result.get("ready_orphans", 0) == 0 and result["already_processed"] == 1
    assert "HISTORY_INSERT_FAIL" not in result["skip_reasons"] and env.db.model_requests == []


def test_unrefetched_stale_orphan_is_flagged_by_db_audit_n2(env):
    from reply_engine.db_audit import audit_reply_db
    from scripts.check_fb_reply_db import FB_HISTORY_TABLE, FB_REQUIRED_TABLE_CONTRACTS

    _seed_live_row(env.db, "c1", skip_reason=None)
    env.db.tables["fb_reply_history"][0]["created_at"] = (
        datetime.now(UTC) - timedelta(hours=2)).isoformat()
    for table in ("fb_reply_cursor", "fb_reply_budget", "fb_reply_blacklist"):
        env.db.tables.setdefault(table, [])
    report = audit_reply_db(required_contracts=FB_REQUIRED_TABLE_CONTRACTS,
                            optional_contracts={}, history_table=FB_HISTORY_TABLE)
    assert report["healthy"] is False
    assert report["issues"]["stale_live_without_terminal_state"] == 1
