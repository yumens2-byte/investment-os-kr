"""following_engine — 단위 + E2E 파일럿 테스트 (문서 13·29장 요구 매핑).

최중요 보안 테스트 (문서 29장):
  dry_run → X Write 호출 수 = 0
  shadow  → X Write 호출 수 = 0
  FOLLOWING_ENABLED=false → 파이프라인 미진입
  미지 실행모드 → live 진입 불가 (dry_run 강등)
  live Guard 실패 → Write = 0
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import run_following
from following_engine import analyzer, collector, config, decision, executor, prefilter, store
from reply_engine import x_client

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_enabled_and_mode_failsafe(monkeypatch):
    monkeypatch.setenv("FOLLOWING_ENABLED", "true")
    assert config.is_enabled() is True
    monkeypatch.setenv("FOLLOWING_ENABLED", "false")
    assert config.is_enabled() is False
    monkeypatch.delenv("FOLLOWING_ENABLED", raising=False)
    assert config.is_enabled() is False

    monkeypatch.setenv("FOLLOWING_EXECUTION_MODE", "LIVE")
    assert config.get_mode() == "live"
    monkeypatch.setenv("FOLLOWING_EXECUTION_MODE", "production")  # 미지 값
    assert config.get_mode() == "dry_run"
    monkeypatch.delenv("FOLLOWING_EXECUTION_MODE", raising=False)
    assert config.get_mode() == "dry_run"


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------

def test_collector_parses_timeline():
    tweet = SimpleNamespace(
        id=900, text="NVIDIA data center revenue is growing",
        author_id=555, conversation_id=900, created_at=datetime.now(UTC),
        public_metrics={"like_count": 10, "reply_count": 1, "retweet_count": 2,
                        "quote_count": 0, "impression_count": 500},
    )
    user = SimpleNamespace(id=555, username="fin_writer",
                           public_metrics={"followers_count": 1200})
    resp = SimpleNamespace(data=[tweet], includes={"users": [user]},
                           meta={"newest_id": "900"})

    class _Client:
        def get_home_timeline(self, **_kwargs):
            return resp

    result = collector.fetch_home_timeline(_Client(), since_id=None)
    assert result["success"] is True
    assert result["tweets"][0]["metrics"]["likes"] == 10
    assert result["users"]["555"]["username"] == "fin_writer"
    assert result["newest_id"] == "900"


def test_collector_failure_is_safe():
    class _Client:
        def get_home_timeline(self, **_kwargs):
            raise RuntimeError("403 Forbidden")   # 요금제 미허용 시나리오

    result = collector.fetch_home_timeline(_Client(), since_id="1")
    assert result["success"] is False
    assert result["tweets"] == []


# ---------------------------------------------------------------------------
# prefilter
# ---------------------------------------------------------------------------

def _post(**over):
    base = {
        "id": "p1", "author_id": "555", "conversation_id": "p1",
        "text": "미국 반도체 업황과 금리 전망에 대한 긴 분석 글입니다. 데이터 포함.",
        "created_at": datetime.now(UTC),
        "metrics": {"likes": 5, "replies": 0, "reposts": 0, "quotes": 0, "impressions": 100},
    }
    base.update(over)
    return base


def test_prefilter_static_rules():
    assert prefilter.check_static(_post(author_id="me"), "me", set())[1] == "SELF"
    assert prefilter.check_static(_post(), "me", {"555"})[1] == "BLACKLIST"
    assert prefilter.check_static(_post(text="짧은 글"), "me", set())[1] == "TOO_SHORT"
    assert prefilter.check_static(
        _post(text="반도체 관련 무료 체험 이벤트를 소개합니다 지금 참여하세요"), "me", set()
    )[1] == "TOPIC_EXCLUDE"
    assert prefilter.check_static(
        _post(text="오늘 점심 메뉴 고민이 많았던 하루였습니다 다들 뭐 드셨나요 저는 국밥"),
        "me", set()
    )[1] == "TOPIC_MISS"
    ok, reason = prefilter.check_static(_post(), "me", set())
    assert ok, reason


def test_prefilter_db_rules(monkeypatch):
    monkeypatch.setattr(prefilter.store, "action_exists", lambda _id: True)
    assert prefilter.check_db(_post(), "shadow")[1] == "DUP"

    monkeypatch.setattr(prefilter.store, "action_exists", lambda _id: False)
    monkeypatch.setattr(prefilter.store, "author_in_cooldown", lambda *_a: True)
    assert prefilter.check_db(_post(), "shadow")[1] == "AUTHOR_COOLDOWN"

    monkeypatch.setattr(prefilter.store, "author_in_cooldown", lambda *_a: False)
    assert prefilter.check_db(_post(), "shadow") == (True, None)


# ---------------------------------------------------------------------------
# analyzer
# ---------------------------------------------------------------------------

def _analysis(**over):
    base = {
        "relevant": True, "category": "SEMICONDUCTOR",
        "relevance_score": 90, "importance_score": 88,
        "engagement_value": 80, "content_value": 85,
        "summary": "요약", "recommended_action": "QUOTE", "reason": "근거",
        "generated_text": "HBM 수요 관련 데이터가 흥미로운 지점이네요.",
    }
    base.update(over)
    return base


def test_analyzer_parses_and_clamps(monkeypatch):
    monkeypatch.setattr(
        analyzer, "gemini_call",
        lambda **_k: {"success": True, "data": [{
            "id": "p1", "relevant": True, "category": "MACRO",
            "relevanceScore": 150, "importanceScore": -5,
            "engagementValue": "80", "contentValue": 90,
            "summary": "s", "recommendedAction": "quote", "reason": "r",
            "generatedText": "금리 경로 데이터가 인상적입니다",
        }]},
    )
    out = analyzer.analyze_batch([{"id": "p1", "author": "a", "text": "t", "metrics": {}}])
    assert out["p1"]["relevance_score"] == 100     # clamp 상한
    assert out["p1"]["importance_score"] == 0      # clamp 하한
    assert out["p1"]["recommended_action"] == "QUOTE"


def test_analyzer_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(
        analyzer, "gemini_call",
        lambda **_k: {"success": False, "data": None, "error": "429"},
    )
    assert analyzer.analyze_batch([{"id": "p1", "author": "", "text": "t", "metrics": {}}]) == {}


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------

def test_decision_order_and_mapping():
    assert decision.decide(_analysis(relevant=False), []) == ("SKIP", "SKIP_NOT_RELEVANT")
    # T-4 이후: 점수 미달이라도 R≥75 + 참여형 추천이면 REVIEW_ONLY 승격 (자동 발행 없음)
    assert decision.decide(_analysis(relevance_score=84), []) == (
        "REVIEW_ONLY", "NEAR_MISS_SCORE"
    )
    assert decision.decide(_analysis(content_value=79), []) == (
        "REVIEW_ONLY", "NEAR_MISS_SCORE"
    )
    assert decision.decide(_analysis(engagement_value=74), []) == (
        "REVIEW_ONLY", "NEAR_MISS_SCORE"
    )
    # 순수 SKIP_SCORE: R이 REVIEW_MIN(75) 미만
    assert decision.decide(_analysis(relevance_score=70), []) == ("SKIP", "SKIP_SCORE")
    assert decision.decide(_analysis(), []) == ("QUOTE", None)
    assert decision.decide(_analysis(recommended_action="PERMITTED_REPLY"), []) == (
        "REVIEW_ONLY", None
    )   # Q2 강등
    assert decision.decide(_analysis(recommended_action="POST"), []) == (
        "SKIP", "SKIPPED_POLICY"
    )   # Q3 제외


def test_decision_text_validation_and_similarity():
    for bad in ("", "지금 매수 타이밍", "어떻게 보시나요?", "#반도체 좋아요"):
        assert decision.decide(_analysis(generated_text=bad), [])[1] == "SKIP_TEXT_INVALID", bad
    same = _analysis()["generated_text"]
    assert decision.decide(_analysis(), [same])[1] == "SKIP_SIMILAR"


# ---------------------------------------------------------------------------
# executor guard
# ---------------------------------------------------------------------------

def test_live_safety_guard(monkeypatch):
    monkeypatch.setattr(store, "action_exists", lambda _id: False)
    monkeypatch.setattr(store, "author_in_cooldown", lambda *_a: False)
    cand = {"post_id": "p1", "author_id": "555", "action_type": "QUOTE",
            "generated_text": "데이터 흥미롭네요"}

    assert executor.live_safety_guard(cand, 0, 0, 2) == (True, None)
    assert executor.live_safety_guard(
        {**cand, "action_type": "REVIEW_ONLY"}, 0, 0, 2
    )[1] == "GUARD_ACTION_NOT_ALLOWED"
    assert executor.live_safety_guard(
        {**cand, "generated_text": " "}, 0, 0, 2
    )[1] == "GUARD_TEXT_BLANK"
    assert executor.live_safety_guard(cand, 5, 0, 2)[1] == "GUARD_DAILY_LIMIT"
    assert executor.live_safety_guard(cand, 0, 2, 2)[1] == "GUARD_RUN_LIMIT"

    monkeypatch.setattr(store, "action_exists", lambda _id: True)
    assert executor.live_safety_guard(cand, 0, 0, 2)[1] == "GUARD_DUPLICATE"


# ---------------------------------------------------------------------------
# E2E 파일럿 — 공용 목킹
# ---------------------------------------------------------------------------

class _FMem:
    def __init__(self):
        self.actions: dict[str, dict] = {}
        self.cursor_saved: list = []
        self.budget_saved: list = []
        self.writes: list = []   # X Write 호출 추적 (최중요)

    def install(self, monkeypatch, mode_env: str, timeline_ok=True, posts=None):
        monkeypatch.setenv("FOLLOWING_ENABLED", "true")
        monkeypatch.setenv("FOLLOWING_EXECUTION_MODE", mode_env)
        monkeypatch.setenv("X_MY_USER_ID", "111")

        monkeypatch.setattr(run_following, "get_blacklist_ids", lambda: set())
        monkeypatch.setattr(run_following, "get_cursor", lambda _a: None)
        monkeypatch.setattr(
            run_following, "upsert_cursor",
            lambda a, s, u: self.cursor_saved.append((a, s, u)) or True,
        )
        monkeypatch.setattr(
            run_following, "get_budget",
            lambda d: {"budget_date": d, "read_calls": 0, "write_calls": 0,
                       "gemini_calls": 0, "est_cost_krw": 0.0},
        )
        monkeypatch.setattr(
            run_following, "upsert_budget",
            lambda r: self.budget_saved.append(dict(r)) or True,
        )

        monkeypatch.setattr(x_client, "get_x_client", lambda: object())
        monkeypatch.setattr(
            collector, "fetch_home_timeline",
            lambda _c, _s: {
                "success": timeline_ok,
                "tweets": (posts if posts is not None else [_post()]) if timeline_ok else [],
                "users": {"555": {"username": "fin_writer", "followers": 1200}},
                "newest_id": "999" if timeline_ok else None,
                "error": None if timeline_ok else "403",
            },
        )

        monkeypatch.setattr(store, "action_exists", lambda pid: pid in self.actions)
        monkeypatch.setattr(store, "insert_action", self._insert)
        monkeypatch.setattr(
            store, "mark_executed",
            lambda pid, aid: self.actions[pid].update(
                {"action_status": "EXECUTED", "actual_x_post_id": aid}) or True,
        )
        monkeypatch.setattr(
            store, "mark_failed",
            lambda pid, c, m: self.actions[pid].update({"action_status": "FAILED"}) or True,
        )
        monkeypatch.setattr(store, "count_actions_today", lambda _m: 0)
        monkeypatch.setattr(store, "author_in_cooldown", lambda *_a: False)
        monkeypatch.setattr(store, "get_recent_generated_texts", lambda limit=30: [])

        monkeypatch.setattr(
            executor, "publish_quote",
            lambda _c, text, pid: self.writes.append((pid, text)) or f"q-{pid}",
        )
        monkeypatch.setattr(run_following.time, "sleep", lambda _s: None)
        monkeypatch.setattr(run_following.random, "randint", lambda _a, _b: 0)
        monkeypatch.setattr(
            analyzer, "gemini_call",
            lambda **_k: {"success": True, "data": [{
                "id": "p1", "relevant": True, "category": "SEMICONDUCTOR",
                "relevanceScore": 92, "importanceScore": 90, "engagementValue": 85,
                "contentValue": 88, "summary": "s", "recommendedAction": "QUOTE",
                "reason": "r", "generatedText": "HBM 수요 데이터가 흥미로운 지점이네요.",
            }]},
        )

    def _insert(self, record):
        pid = record["post_id"]
        if pid in self.actions:
            return False
        self.actions[pid] = dict(record)
        return True


def test_pilot_disabled(monkeypatch):
    monkeypatch.setenv("FOLLOWING_ENABLED", "false")
    result = run_following.main()
    assert result["exit_reason"] == "EXIT_DISABLED"


def test_pilot_dry_run_zero_writes_zero_db(monkeypatch):
    mem = _FMem()
    mem.install(monkeypatch, "dry_run")
    result = run_following.main()
    assert result["success"] is True
    assert result["would_execute"] == 1
    assert mem.writes == []            # 최중요: X Write = 0
    assert mem.actions == {}           # dry_run DB 무기록
    assert mem.cursor_saved == []      # 커서 미전진


def test_pilot_shadow_zero_writes_with_db(monkeypatch):
    mem = _FMem()
    mem.install(monkeypatch, "shadow")
    result = run_following.main()
    assert result["would_execute"] == 1
    assert mem.writes == []            # 최중요: X Write = 0
    assert mem.actions["p1"]["would_execute"] is True
    assert mem.actions["p1"]["action_status"] == "SHADOW_COMPLETED"
    assert mem.cursor_saved == [("kr_following", "999", "111")]


def test_pilot_unknown_mode_degrades_no_writes(monkeypatch):
    mem = _FMem()
    mem.install(monkeypatch, "production")   # 미지 값 → dry_run 강등
    result = run_following.main()
    assert result["mode"] == "dry_run"
    assert mem.writes == []


def test_pilot_live_quote_executed(monkeypatch):
    mem = _FMem()
    mem.install(monkeypatch, "live")
    result = run_following.main()
    assert result["actual_writes"] == 1
    assert mem.writes == [("p1", "HBM 수요 데이터가 흥미로운 지점이네요.")]
    assert mem.actions["p1"]["action_status"] == "EXECUTED"
    assert mem.actions["p1"]["actual_x_post_id"] == "q-p1"
    assert len(mem.budget_saved) >= 2   # 발행 직후 + 종료


def test_pilot_live_review_only_no_write(monkeypatch):
    mem = _FMem()
    mem.install(monkeypatch, "live")
    monkeypatch.setattr(
        analyzer, "gemini_call",
        lambda **_k: {"success": True, "data": [{
            "id": "p1", "relevant": True, "category": "MACRO",
            "relevanceScore": 92, "importanceScore": 90, "engagementValue": 85,
            "contentValue": 88, "summary": "s",
            "recommendedAction": "PERMITTED_REPLY", "reason": "r", "generatedText": "",
        }]},
    )
    result = run_following.main()
    assert result["actual_writes"] == 0
    assert mem.writes == []                               # 자동 Reply 금지 (Q2)
    assert mem.actions["p1"]["action_type"] == "REVIEW_ONLY"
    assert mem.actions["p1"]["action_status"] == "READY"  # 마스터 수동 처리 후보


def test_pilot_live_guard_blocks_run_limit(monkeypatch):
    mem = _FMem()
    posts = [_post(id="p1"), _post(id="p2", author_id="666"), _post(id="p3", author_id="777")]
    mem.install(monkeypatch, "live", posts=posts)
    monkeypatch.setattr(
        analyzer, "gemini_call",
        lambda **_k: {"success": True, "data": [
            {"id": pid, "relevant": True, "category": "AI", "relevanceScore": 92,
             "importanceScore": 90, "engagementValue": 85, "contentValue": 88,
             "summary": "s", "recommendedAction": "QUOTE", "reason": "r",
             "generatedText": f"의미 있는 데이터 포인트네요 {n}"}
            for n, pid in enumerate(["p1", "p2", "p3"])
        ]},
    )
    result = run_following.main()
    assert result["actual_writes"] == 2                   # per-run 상한 2 (Q5)
    assert result["skip_reasons"]["RUN_LIMIT"] == 1
    assert len(mem.writes) == 2


def test_pilot_timeline_fetch_fail_no_writes(monkeypatch):
    mem = _FMem()
    mem.install(monkeypatch, "live", timeline_ok=False)
    result = run_following.main()
    assert result["exit_reason"] == "EXIT_TIMELINE_FETCH_FAIL"
    assert mem.writes == []


def test_pilot_ai_fail_all_skip(monkeypatch):
    mem = _FMem()
    mem.install(monkeypatch, "shadow")
    monkeypatch.setattr(
        analyzer, "gemini_call",
        lambda **_k: {"success": False, "data": None, "error": "500"},
    )
    result = run_following.main()
    assert result["would_execute"] == 0
    assert result["skip_reasons"]["SKIP_AI_FAIL"] == 1
    assert mem.writes == []


def test_following_versions():
    """지침 5 — 버전 상수 확인."""
    from following_engine import analyzer as a
    from following_engine import collector as c
    from following_engine import decision as d
    from following_engine import executor as e
    from following_engine import prefilter as p
    from following_engine import store as s

    assert run_following.VERSION == "1.0.1"   # T-4 사유 보존
    assert config.VERSION == "1.0.2"   # T-4 REVIEW_MIN_RELEVANCE
    assert a.VERSION == "1.0.1"        # J-1 (응답 잘림) 수정 반영
    assert p.VERSION == "1.0.1"        # K-1 (RT 유입 차단) 수정 반영
    assert d.VERSION == "1.1.0"        # T-4 near-miss REVIEW_ONLY
    for mod in (c, e, s):
        assert mod.VERSION == "1.0.0"


# ---------------------------------------------------------------------------
# J-1 (2026-08-20 실사고): 응답 잘림 → JSON 파손 → 전건 SKIP
# ---------------------------------------------------------------------------

def _mk_items(n: int) -> list[dict]:
    return [
        {"id": str(1000 + i), "author": f"acct{i}", "metrics": {"likes": 5},
         "text": "반도체 업황 데이터 분석 게시물 " * 3}
        for i in range(n)
    ]


def _valid_rows(items):
    return [
        {"id": i["id"], "relevant": True, "category": "SEMICONDUCTOR",
         "relevanceScore": 90, "importanceScore": 85, "engagementValue": 80,
         "contentValue": 85, "summary": "요약", "recommendedAction": "QUOTE",
         "reason": "근거", "generatedText": "데이터 관점 코멘트"}
        for i in items
    ]


def test_j1_retry_recovers_from_invalid_json(monkeypatch):
    """실사고 재현: 1차 응답 JSON 파손(data=str) → 재시도 성공 시 분석 결과 확보."""
    from following_engine import analyzer as fan

    items = _mk_items(3)
    calls = {"n": 0}

    def _flaky(**kwargs):
        calls["n"] += 1
        assert kwargs["max_tokens"] == fan.ANALYZER_MAX_TOKENS  # 8192 적용 확인
        if calls["n"] == 1:
            return {"success": True, "data": '{"truncated": ', "error": None}  # 잘림 재현
        return {"success": True, "data": _valid_rows(items), "error": None}

    monkeypatch.setattr(fan, "gemini_call", _flaky)
    result = fan.analyze_batch(items)
    assert calls["n"] == 2                       # 재시도 정확히 1회
    assert len(result) == 3


def test_j1_retry_exhausted_failsafe(monkeypatch):
    """재시도까지 실패하면 기존 fail-safe 유지 (빈 결과 → Decision SKIP)."""
    from following_engine import analyzer as fan

    monkeypatch.setattr(
        fan, "gemini_call",
        lambda **_k: {"success": True, "data": "not-a-list", "error": None},
    )
    assert fan.analyze_batch(_mk_items(2)) == {}


def test_j1_chunking_boundaries(monkeypatch):
    """10/11/25건 → 1/2/3회 chunk 호출, 결과 병합 검증."""
    from following_engine import analyzer as fan

    for n, expected_calls in ((10, 1), (11, 2), (25, 3)):
        items = _mk_items(n)
        seen: list[int] = []

        def _ok(**kwargs):
            batch_ids = [
                line.split("|")[0].split(":")[1].strip()
                for line in kwargs["prompt"].splitlines() if line.startswith("- id:")
            ]
            seen.append(len(batch_ids))
            return {"success": True,
                    "data": _valid_rows([{"id": b} for b in batch_ids]),
                    "error": None}

        monkeypatch.setattr(fan, "gemini_call", _ok)
        result = fan.analyze_batch(items)
        assert len(seen) == expected_calls, n
        assert all(size <= fan.ANALYZER_BATCH_SIZE for size in seen)
        assert len(result) == n


def test_j1_prompt_slimming(monkeypatch):
    """응답 슬림화 규칙(40자 요약·QUOTE만 generatedText)이 프롬프트에 명시되는지."""
    from following_engine import analyzer as fan

    captured = {}

    def _cap(**kwargs):
        captured.update(kwargs)
        return {"success": True, "data": [], "error": None}

    monkeypatch.setattr(fan, "gemini_call", _cap)
    fan.analyze_batch(_mk_items(1))
    assert "40자 이내" in captured["prompt"]
    assert "QUOTE일 때만" in captured["prompt"]


def test_j1_version_bumped():
    from following_engine import analyzer as fan

    assert fan.VERSION == "1.0.1"


# ---------------------------------------------------------------------------
# K-1 (2026-08-20 실사고): RT 유입 — prefilter 이중 방어
# ---------------------------------------------------------------------------

def test_k1_retweet_blocked_static():
    """실측 픽스처: 첫 QUOTE 후보가 됐던 RT 게시물이 이제 정적 필터에서 차단돼야 한다."""
    from following_engine import prefilter

    rt_tweet = {
        "id": "2090559629071978926", "author_id": "999",
        "text": "RT @ohmahahm: 🚨 BMO, 반도체주 5종목에 '시장수익률 상회' 의견으로 커버리지 개시 "
                "마이크론 목표주가 상향 등 상세 내용",
    }
    ok, reason = prefilter.check_static(rt_tweet, "111", set())
    assert not ok and reason == "SKIP_RETWEET"


def test_k1_normal_and_quote_posts_pass():
    """정상 글·인용 코멘트 글(RT @ 로 시작하지 않음)은 통과해야 한다."""
    from following_engine import prefilter

    for text in (
        "BMO가 반도체주 5종목에 시장수익률 상회 의견으로 커버리지를 개시했습니다 상세 분석",
        "오늘 나스닥 반도체 섹터 데이터 정리: 수요 강세와 공급 제약이 동시에 관찰됩니다",
    ):
        ok, reason = prefilter.check_static(
            {"id": "1", "author_id": "999", "text": text}, "111", set()
        )
        assert ok, (text, reason)


def test_k1_rt_checked_before_topic():
    """RT는 topic 매칭 여부와 무관하게 우선 차단 (SELF 다음 순서)."""
    from following_engine import prefilter

    rt_text = "RT @x: 아무 주제나 상관없는 긴 텍스트입니다 반도체"
    ok, reason = prefilter.check_static(
        {"id": "1", "author_id": "999", "text": rt_text}, "111", set()
    )
    assert not ok and reason == "SKIP_RETWEET"


# ---------------------------------------------------------------------------
# T-4 (2026-08-24): near-miss REVIEW_ONLY 승격 — 실측 픽스처 기반
# ---------------------------------------------------------------------------

def _t4_analysis(r, c, e, action="QUOTE"):
    return {"relevant": True, "relevance_score": r, "content_value": c,
            "engagement_value": e, "recommended_action": action,
            "generated_text": "데이터 관점에서 흥미로운 지점입니다", "summary": "s", "reason": "r"}


def test_t4_near_miss_promoted_to_review_only():
    """실측 픽스처: 중국 메모리 글(R80/C75/E30) — SKIP_SCORE 대신 REVIEW_ONLY 승격."""
    action, reason = decision.decide(_t4_analysis(80, 75, 30), [])
    assert action == "REVIEW_ONLY" and reason == "NEAR_MISS_SCORE"


def test_t4_low_relevance_still_skipped():
    """R < REVIEW_MIN(75)은 승격 없이 기존대로 SKIP_SCORE."""
    action, reason = decision.decide(_t4_analysis(70, 70, 50), [])
    assert action == "SKIP" and reason == "SKIP_SCORE"


def test_t4_non_engage_action_not_promoted():
    """추천이 POST면 near-miss여도 승격하지 않는다."""
    action, reason = decision.decide(_t4_analysis(80, 75, 30, action="POST"), [])
    assert action == "SKIP" and reason == "SKIP_SCORE"


def test_t4_full_pass_still_quote(monkeypatch):
    """전 축 통과는 기존대로 QUOTE (회귀 무결)."""
    monkeypatch.setattr(decision, "MIN_ENGAGEMENT_VALUE", 65)
    action, reason = decision.decide(_t4_analysis(90, 80, 70), [])
    assert action == "QUOTE" and reason is None


def test_t4_e2e_review_only_never_publishes_keeps_text(monkeypatch):
    """near-miss 승격분은 live에서도 발행 0, 텍스트·사유가 DB에 보존 (수동 후보)."""
    mem = _FMem()
    mem.install(monkeypatch, "live")
    monkeypatch.setattr(
        analyzer, "gemini_call",
        lambda **_k: {"success": True, "data": [{
            "id": "p1", "relevant": True, "category": "SEMICONDUCTOR",
            "relevanceScore": 80, "importanceScore": 80, "engagementValue": 30,
            "contentValue": 75, "summary": "s", "recommendedAction": "QUOTE",
            "reason": "r", "generatedText": "공급망 데이터가 흥미롭습니다",
        }]},
    )
    result = run_following.main()
    assert mem.writes == []                               # 발행 절대 없음
    assert result["actual_writes"] == 0
    row = mem.actions["p1"]
    assert row["action_type"] == "REVIEW_ONLY"
    assert row["action_status"] == "READY"
    assert row["skip_reason"] == "NEAR_MISS_SCORE"
    assert row["generated_text"] == "공급망 데이터가 흥미롭습니다"
    assert row["would_execute"] is False
