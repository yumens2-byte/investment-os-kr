"""SHOCK_NEWS 엔진 검증 (v1.0.0) — 중복 방지 4층 / 안전 게이트(완화 불가) / 모드 매트릭스."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import run_shock_news
from shock_news_engine import collector, gate, publisher, ranker
from shock_news_engine import config as scfg
from shock_news_engine import store as sstore

KST = scfg.KST


# ---------------------------------------------------------------------------
# config — 슬롯 판정 / 모드 fail-safe
# ---------------------------------------------------------------------------

def _kst(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=KST)


def test_slot_determination():
    assert scfg.determine_slot(_kst(2026, 8, 22, 15, 55)) == ("20260822-KR16", "KR")
    assert scfg.determine_slot(_kst(2026, 8, 22, 16, 30)) == ("20260822-KR16", "KR")
    assert scfg.determine_slot(_kst(2026, 8, 22, 3, 55)) == ("20260822-US04", "US")
    assert scfg.determine_slot(_kst(2026, 8, 22, 4, 59)) == ("20260822-US04", "US")
    assert scfg.determine_slot(_kst(2026, 8, 22, 12, 0)) is None
    assert scfg.determine_slot(_kst(2026, 8, 22, 17, 0)) is None


def test_slot_force_env(monkeypatch):
    monkeypatch.setenv("SHOCK_FORCE_SLOT", "US04")
    assert scfg.determine_slot(_kst(2026, 8, 22, 12, 0)) == ("20260822-US04", "US")


def test_mode_failsafe(monkeypatch):
    monkeypatch.setenv("SHOCK_EXECUTION_MODE", "true")   # M-1/M-2 계열 오설정 재현
    assert scfg.get_mode() == "dry_run"
    monkeypatch.setenv("SHOCK_EXECUTION_MODE", "live")
    assert scfg.get_mode() == "live"
    monkeypatch.delenv("SHOCK_EXECUTION_MODE")
    assert scfg.get_mode() == "dry_run"


def test_enabled_gate(monkeypatch):
    monkeypatch.delenv("SHOCK_ENABLED", raising=False)
    assert scfg.is_enabled() is False
    monkeypatch.setenv("SHOCK_ENABLED", "false")
    assert scfg.is_enabled() is False
    monkeypatch.setenv("SHOCK_ENABLED", "true")
    assert scfg.is_enabled() is True


# ---------------------------------------------------------------------------
# collector — 정규화/해시/24h
# ---------------------------------------------------------------------------

def test_url_normalize_and_hash_stability():
    a = collector.article_hash("https://n.example.com/art/1?utm_source=x&id=7#top")
    b = collector.article_hash("https://n.example.com/art/1?id=7")
    assert a == b                                     # 추적 파라미터·fragment 무시
    assert a != collector.article_hash("https://n.example.com/art/2?id=7")


_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss><channel>
  <item><title>60대 남성 살해 혐의 40대 체포</title>
    <link>https://news.example.com/a1</link>
    <pubDate>{fresh}</pubDate></item>
  <item><title>일주일 전 오래된 실종 기사</title>
    <link>https://news.example.com/a2</link>
    <pubDate>{stale}</pubDate></item>
  <item><title>공원 산책로 정비 소식</title>
    <link>https://news.example.com/a3</link>
    <pubDate>{fresh}</pubDate></item>
</channel></rss>"""


def _rss_fixture():
    now = datetime.now(UTC)
    fmt = "%a, %d %b %Y %H:%M:%S +0000"
    return _RSS_XML.format(fresh=now.strftime(fmt),
                           stale=(now - timedelta(days=7)).strftime(fmt))


def test_collector_24h_filter(monkeypatch):
    class _Resp:
        text = _rss_fixture()
        def raise_for_status(self):
            pass

    monkeypatch.setattr(collector.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setitem(collector.RSS_SOURCES, "KR", ("https://feed.example.com/rss",))
    arts = collector.fetch_articles("KR")
    titles = [a["title"] for a in arts]
    assert "60대 남성 살해 혐의 40대 체포" in titles
    assert "일주일 전 오래된 실종 기사" not in titles       # 24h 초과 제외
    assert all("article_hash" in a for a in arts)


def test_collector_source_failure_partial(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(collector.requests, "get", _boom)
    assert collector.fetch_articles("KR") == []           # 전 소스 실패 → 빈 목록 (크래시 없음)


# ---------------------------------------------------------------------------
# ranker — 티어 우선순위 / Gemini / fallback
# ---------------------------------------------------------------------------

def _art(title, h="a" * 64, published=None):
    return {"title": title, "url": f"https://e.com/{h[:6]}", "published": published,
            "source": "s", "article_hash": h}


def test_tier_priority():
    assert ranker.assign_tier("아파트서 이웃 살해 혐의") == 1
    assert ranker.assign_tier("여고생 실종 나흘째") == 2
    assert ranker.assign_tier("길거리 흉기 난동에 시민 부상") == 3
    assert ranker.assign_tier("공장 화재로 3명 사망") == 4
    assert ranker.assign_tier("금리 동결 전망") is None


def test_select_candidates_orders_by_tier():
    arts = [_art("공장 화재 사망", "c" * 64), _art("실종 신고", "b" * 64),
            _art("살해 혐의 체포", "a" * 64), _art("주가 급등", "d" * 64)]
    cands = ranker.select_candidates(arts)
    assert [c["tier"] for c in cands] == [1, 2, 4]        # 무매칭 제외 + 티어 정렬


def test_ranker_gemini_choice(monkeypatch):
    cands = ranker.select_candidates([_art("살해 혐의 체포", "a" * 64),
                                      _art("실종 신고", "b" * 64)])
    monkeypatch.setattr(
        ranker, "gemini_call",
        lambda **_k: {"success": True,
                      "data": {"chosen_id": "a" * 12,
                               "comment": "믿기 힘든 소식이네요. 안타깝습니다."},
                      "error": None},
    )
    out = ranker.rank_and_generate(cands, "KR")
    assert out["picked_by"] == "gemini" and out["tier"] == 1


def test_ranker_invalid_json_retry_then_fallback(monkeypatch):
    cands = ranker.select_candidates([_art("살해 혐의 체포", "a" * 64)])
    calls = {"n": 0}

    def _always_bad(**_k):
        calls["n"] += 1
        return {"success": True, "data": "{broken", "error": None}

    monkeypatch.setattr(ranker, "gemini_call", _always_bad)
    out = ranker.rank_and_generate(cands, "KR")
    assert calls["n"] == 2                                # 재시도 1회 (J-1 규약)
    assert out["picked_by"] == "fallback"
    ok, reason = gate.check_comment(out["comment"])
    assert ok, reason                                     # fallback 템플릿은 게이트 통과 보장


def test_engage_prompt_deterministic():
    assert ranker.wants_engage_prompt("00000000abc") is True    # bucket 0
    assert ranker.wants_engage_prompt("ffffffffabc") is False   # bucket 99
    assert ranker.wants_engage_prompt("00000000abc") == ranker.wants_engage_prompt("00000000abc")


# ---------------------------------------------------------------------------
# gate — 완화 불가 항목 (R-A)
# ---------------------------------------------------------------------------

def test_gate_real_name_blocked():
    for text in ("김철수씨가 변을 당했다니 충격입니다",
                 "피해자 이모(42)씨 소식에 마음이 무겁습니다",
                 "A(19)군 사건, 말문이 막힙니다"):
        ok, reason = gate.check_comment(text)
        assert not ok and reason == "GATE_REAL_NAME", text


def test_gate_verdict_blocked_but_hyemi_allowed():
    ok, reason = gate.check_comment("이웃을 살해했다니 충격입니다")
    assert not ok and reason == "GATE_VERDICT"
    ok, _ = gate.check_comment("살해 혐의로 체포됐다는 소식, 믿기지 않네요")
    assert ok                                             # '혐의' 화법은 허용


def test_gate_graphic_format_length():
    assert gate.check_comment("시신이 토막 난 채 발견됐다니")[1] == "GATE_GRAPHIC"
    assert gate.check_comment("충격 소식 #사건")[1] == "GATE_FORMAT"
    assert gate.check_comment("링크 https://x.com/1 보세요")[1] == "GATE_FORMAT"
    assert gate.check_comment("가" * 201)[1] == "GATE_LENGTH"
    assert gate.check_comment("")[1] == "GATE_EMPTY"


def test_gate_title_duplicate_l3():
    recent = ["서울 아파트 이웃 살해 혐의 40대 체포"]
    ok, reason = gate.check_title_duplicate("아파트 이웃 살해 혐의 40대 체포돼", recent)
    assert not ok and reason == "GATE_DUP_EVENT"          # 동일 사건 이종 기사
    ok, _ = gate.check_title_duplicate("부산 공장 화재로 3명 부상", recent)
    assert ok


# ---------------------------------------------------------------------------
# 파이프라인 E2E — 모드 매트릭스 + 중복 4층
# ---------------------------------------------------------------------------

class _SMem:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.slot_keys: set[tuple] = set()
        self.budget_saved: list = []

    def install(self, monkeypatch):
        monkeypatch.setattr(sstore, "article_exists", lambda h: h in self.rows)
        monkeypatch.setattr(
            sstore, "slot_taken", lambda sk, m: (sk, m) in self.slot_keys
        )
        monkeypatch.setattr(sstore, "get_recent_titles", lambda days=7: [
            r["title"] for r in self.rows.values()
        ])

        def _insert(record):
            key = (record["slot_key"], record["mode"])
            if record["article_hash"] in self.rows or key in self.slot_keys:
                return False
            self.rows[record["article_hash"]] = dict(record)
            self.slot_keys.add(key)
            return True

        monkeypatch.setattr(sstore, "insert_history", _insert)
        monkeypatch.setattr(
            sstore, "mark_posted",
            lambda h, tid: self.rows[h].update({"posted_tweet_id": tid}),
        )
        monkeypatch.setattr(
            sstore, "mark_failed",
            lambda h, r: self.rows.get(h, {}).update({"skip_reason": r}),
        )
        # reply 예산 재사용부
        monkeypatch.setattr(
            run_shock_news.rstore, "get_budget",
            lambda d: {"budget_date": d, "read_calls": 0, "write_calls": 0,
                       "gemini_calls": 0, "est_cost_krw": 0.0},
        )
        monkeypatch.setattr(
            run_shock_news.rstore, "upsert_budget",
            lambda row: self.budget_saved.append(dict(row)),
        )


def _pipeline_env(monkeypatch, mode):
    monkeypatch.setenv("SHOCK_ENABLED", "true")
    monkeypatch.setenv("SHOCK_EXECUTION_MODE", mode)
    monkeypatch.setenv("SHOCK_FORCE_SLOT", "KR16")
    monkeypatch.setattr(run_shock_news.time, "sleep", lambda _s: None)
    monkeypatch.setattr(run_shock_news.random, "randint", lambda a, b: 0)
    monkeypatch.setattr(
        collector, "fetch_articles",
        lambda session: [
            {"title": "아파트 이웃 살해 혐의 40대 체포", "url": "https://n.com/1",
             "published": datetime.now(UTC), "source": "s", "article_hash": "a" * 64},
            {"title": "한강공원 산책 명소 소개", "url": "https://n.com/2",
             "published": datetime.now(UTC), "source": "s", "article_hash": "b" * 64},
        ],
    )
    monkeypatch.setattr(run_shock_news.collector, "fetch_articles", collector.fetch_articles)
    monkeypatch.setattr(
        ranker, "gemini_call",
        lambda **_k: {"success": True,
                      "data": {"chosen_id": "a" * 12,
                               "comment": "이런 일이 있었다니 믿기지 않네요. 안타깝습니다."},
                      "error": None},
    )


def _publish_spy(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        publisher, "post_shock",
        lambda _c, comment, url: calls.append((comment, url)) or "90001",
    )
    monkeypatch.setattr(run_shock_news.publisher, "post_shock", publisher.post_shock)
    monkeypatch.setattr(run_shock_news.x_client, "get_x_client", lambda: object())
    return calls


def test_pipeline_disabled(monkeypatch):
    monkeypatch.setenv("SHOCK_ENABLED", "false")
    result = run_shock_news.main()
    assert result["exit_reason"] == "EXIT_DISABLED"


def test_pipeline_off_slot(monkeypatch):
    monkeypatch.setenv("SHOCK_ENABLED", "true")
    monkeypatch.delenv("SHOCK_FORCE_SLOT", raising=False)
    monkeypatch.setattr(
        run_shock_news, "determine_slot", lambda now: None
    )
    result = run_shock_news.main()
    assert result["exit_reason"] == "EXIT_OFF_SLOT"


def test_pipeline_dry_run_no_writes(monkeypatch):
    mem = _SMem()
    mem.install(monkeypatch)
    _pipeline_env(monkeypatch, "dry_run")
    calls = _publish_spy(monkeypatch)

    result = run_shock_news.main()
    assert result["success"] is True
    assert result["chosen"]["tier"] == 1
    assert calls == [] and mem.rows == {} and mem.budget_saved == []


def test_pipeline_shadow_records_no_publish(monkeypatch):
    mem = _SMem()
    mem.install(monkeypatch)
    _pipeline_env(monkeypatch, "shadow")
    calls = _publish_spy(monkeypatch)

    result = run_shock_news.main()
    assert result["success"] is True
    assert calls == []                                    # X 쓰기 0 (spy)
    assert len(mem.rows) == 1
    row = next(iter(mem.rows.values()))
    assert row["mode"] == "shadow" and row["would_execute"] is True
    assert "posted_tweet_id" not in row


def test_pipeline_live_publish_and_pair(monkeypatch):
    mem = _SMem()
    mem.install(monkeypatch)
    _pipeline_env(monkeypatch, "live")
    calls = _publish_spy(monkeypatch)

    result = run_shock_news.main()
    assert result["published"] == 1
    assert len(calls) == 1
    assert calls[0][1] == "https://n.com/1"               # 링크 첨부
    row = next(iter(mem.rows.values()))
    assert row["posted_tweet_id"] == "90001"              # 발행-기록 짝
    assert len(mem.budget_saved) >= 1                     # 발행 직후 예산 저장 (V-1)


def test_pipeline_l2_slot_rerun_blocked(monkeypatch):
    """같은 슬롯 재실행 → EXIT_SLOT_TAKEN (중복 발행 불가)."""
    mem = _SMem()
    mem.install(monkeypatch)
    _pipeline_env(monkeypatch, "live")
    calls = _publish_spy(monkeypatch)

    assert run_shock_news.main()["published"] == 1
    result2 = run_shock_news.main()                       # 동일 슬롯 재실행
    assert result2["exit_reason"] == "EXIT_SLOT_TAKEN"
    assert len(calls) == 1                                # 추가 발행 없음


def test_pipeline_l1_article_dedup(monkeypatch):
    """이미 발행된 기사(L1)는 후보에서 제거 → 다른 슬롯이어도 재발행 없음."""
    mem = _SMem()
    mem.install(monkeypatch)
    _pipeline_env(monkeypatch, "shadow")
    _publish_spy(monkeypatch)

    assert run_shock_news.main()["success"] is True       # KR16 슬롯 적재
    monkeypatch.setenv("SHOCK_FORCE_SLOT", "US04")        # 다른 슬롯
    result2 = run_shock_news.main()
    assert result2["skip_reasons"].get("DUP_ARTICLE") == 1
    assert len(mem.rows) == 1                             # 신규 적재 없음 (잔여 후보 무티어)


def test_pipeline_gate_fail_no_publish(monkeypatch):
    """생성 코멘트가 실명 게이트에 걸리면 무발행 (default-deny)."""
    mem = _SMem()
    mem.install(monkeypatch)
    _pipeline_env(monkeypatch, "live")
    calls = _publish_spy(monkeypatch)
    monkeypatch.setattr(
        ranker, "gemini_call",
        lambda **_k: {"success": True,
                      "data": {"chosen_id": "a" * 12,
                               "comment": "피해자 김철수씨 소식에 충격입니다"},
                      "error": None},
    )

    result = run_shock_news.main()
    assert result["exit_reason"] == "EXIT_GATE_FAIL"
    assert result["skip_reasons"]["GATE_REAL_NAME"] == 1
    assert calls == [] and mem.rows == {}


def test_shock_versions():
    """지침 5 — 버전 상수."""
    assert run_shock_news.VERSION == "1.0.0"
    for mod in (scfg, collector, ranker, gate, sstore, publisher):
        assert mod.VERSION == "1.0.0"
