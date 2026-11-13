"""DN2026_0103 회귀 — X 답글 시간 맥락(D8)과 cron 지연 관측(D10).

실측 사례(kr_reply_history, 2026-10-30~11-07)의 답글 문구와 시각을 그대로 쓴다.
외부 호출(X·Gemini·DB)은 전부 대역이다 (STD-TEST-04). 시계는 tests/conftest.py가 고정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

import run_reply
from reply_engine import classifier, gate, generator, policy, schedule, timectx, x_client
from scripts import reply_schedule_gate
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet

KST = timezone(timedelta(hours=9))


def _kst(*args) -> datetime:
    return datetime(*args, tzinfo=KST)


# ── ① 실측 사례: 시점 표현 ↔ 게시 시각 ─────────────────────────

@pytest.mark.parametrize("when,reply,bad", [
    # 금 오후 4시 아침 인사 / 토 새벽 금요일 기원 / 일 0시 주말 일치 / 시점어 없음 / 시각 일치
    (_kst(2026, 10, 30, 16, 19), "기분 좋은 아침 시작이네요! 😊", True),
    (_kst(2026, 10, 31, 1, 5), "금요일도 활기차게 보내시길 바라요!", True),
    (_kst(2026, 11, 1, 0, 32), "주말도 즐겁게 보내세요! 😊", False),
    (_kst(2026, 11, 7, 10, 10), "오늘도 좋은 하루 보내세요! 😊", False),
    (_kst(2026, 10, 28, 21, 4), "맛있는 저녁 되셨다니 다행이에요! 😊", False),  # 역할 역전은 별도
])
def test_incident_time_mismatch(when, reply, bad):
    assert (timectx.mismatch(reply, when) is not None) is bad


@pytest.mark.parametrize("when,reply,bad", [
    (_kst(2026, 11, 13, 8, 0), "좋은 아침이에요 🙂", False),
    (_kst(2026, 11, 13, 11, 0), "좋은 아침이에요 🙂", True),          # 경계: 11시부터 아침 불일치
    (_kst(2026, 11, 13, 12, 30), "맛점하세요", False),
    (_kst(2026, 11, 13, 19, 0), "저녁 든든히 챙기세요", False),
    (_kst(2026, 11, 13, 23, 30), "편안한 밤 보내세요", False),
    (_kst(2026, 11, 13, 2, 0), "굿나잇이에요", False),                 # 자정 넘는 구간
    (_kst(2026, 11, 13, 14, 0), "굿밤이에요", True),
    (_kst(2026, 11, 13, 20, 0), "불금 즐겁게 보내세요", False),         # 금 20시
    (_kst(2026, 11, 14, 4, 30), "불금 마무리 잘 하세요", False),        # 토 04:30까지 허용
    (_kst(2026, 11, 14, 9, 0), "불금 즐겁게 보내세요", True),
    (_kst(2026, 11, 12, 18, 0), "주말 잘 보내세요", True),             # 목요일
    (_kst(2026, 11, 13, 12, 0), "주말 잘 보내세요", False),            # 금 12시부터
    (_kst(2026, 11, 16, 9, 0), "월요일 힘내세요", False),
    (_kst(2026, 11, 17, 9, 0), "월요일 힘내세요", True),
    (_kst(2026, 11, 13, 23, 0), "오늘 하루도 고생 많으셨어요", False),  # '오늘·하루'는 판정 제외
    (_kst(2026, 10, 31, 1, 7), "퇴근길 조심히 들어가세요 🙂", True),   # 파일럿: 토 새벽 퇴근
    (_kst(2026, 11, 13, 18, 30), "퇴근길 조심히 들어가세요 🙂", False),
    (_kst(2026, 11, 13, 8, 0), "출근길 힘내세요", False),
    (_kst(2026, 11, 15, 8, 0), "출근길 힘내세요", True),               # 일요일
])
def test_daypart_weekday_rules(when, reply, bad):
    assert (timectx.mismatch(reply, when) is not None) is bad


def test_label_and_age():
    now = _kst(2026, 11, 13, 23, 11)
    assert timectx.label(now) == "2026-11-13(금) 23:11"
    assert timectx.age_minutes(datetime(2026, 11, 13, 10, 11, tzinfo=UTC), now) == 240
    assert timectx.age_minutes("2026-11-13T10:11:00Z", now) == 240
    assert timectx.age_minutes(None, now) is None
    assert timectx.age_minutes("not-a-date", now) is None
    assert timectx.age_minutes(datetime(2026, 11, 14, tzinfo=UTC), now) is None  # 미래 시각


# ── ② 게이트: strict + _now_kst일 때만 적용 (FB·기존 호출 불변) ─────────

def test_gate_applies_only_with_context_now():
    comment = "드디어 불타늘 금모닝!! 오늘도 좋은소통 이어서 가시죠ㅎ"
    reply = "기분 좋은 아침 시작이네요! 😊"
    ctx = {"_now_kst": _kst(2026, 10, 30, 16, 19)}
    assert gate.check_reply(reply, [], comment, context=ctx, strict=True)[1] == "GATE_TIME_MISMATCH"
    assert gate.check_reply("아침부터 반가운 인사 고마워요 🙂", [], comment,
                            context={"_now_kst": _kst(2026, 10, 30, 8, 0)},
                            strict=True) == (True, None)
    # 시계가 없는 호출(FB·기존 테스트)·strict=False는 판정하지 않는다.
    assert gate.check_reply(reply, [], comment, strict=True)[1] != "GATE_TIME_MISMATCH"
    loose = gate.check_reply(reply, [], comment, context=ctx, strict=False)
    assert loose[1] != "GATE_TIME_MISMATCH"


def test_semantic_reason_recorded_before_time_reason():
    """역할 역전 같은 의미 오류는 시점 오류보다 먼저 기록한다."""
    ctx = {"_now_kst": _kst(2026, 11, 13, 23, 30)}  # 저녁 불일치 시각
    result = gate.check_reply("맛있는 저녁 되셨다니 다행이에요! 😊", [],
                              "류님 저녁 맛있게 드세용 😄😄", context=ctx, strict=True)
    assert result[1] == "GATE_ROLE_REVERSAL"


def test_choose_reply_falls_through_to_time_neutral_pool():
    comment = "불금 달려보시쥬ㅎ"
    ctx = {"_now_kst": _kst(2026, 10, 31, 1, 5)}
    item = {"id": "7", "text": comment, "label": "POSITIVE"}
    pool = generator.contextual_fallbacks(item, extended=True)
    chosen, source, reason = gate.choose_reply(
        ["금요일도 활기차게 보내시길 바라요!"], pool, [], comment, context=ctx, strict=True)
    assert reason == "GATE_TIME_MISMATCH"
    assert source == "TEMPLATE_FALLBACK" and chosen in pool
    assert timectx.mismatch(chosen, ctx["_now_kst"]) is None


def test_safe_pools_are_time_neutral_all_week():
    """안전 풀 문구는 어느 요일·시각에도 시점 게이트에 걸리지 않아야 한다."""
    start = _kst(2026, 11, 9, 0, 0)  # 월요일
    texts = {t for pool in policy.SAFE_POOLS.values() for t in pool}
    texts |= set(generator._POOL_NON_KR) | set(generator._POOL_POSITIVE)
    texts |= set(generator._POOL_SUPPORTIVE)
    for hour in range(0, 24 * 7):
        now = start + timedelta(hours=hour)
        bad = [t for t in texts if timectx.mismatch(t, now)]
        assert not bad, (now, bad)


# ── ③ 생성 프롬프트: _now_kst 주입 시에만 시간 필드·규칙 ───────────────

def _capture_prompt(monkeypatch, item):
    captured = {}

    def fake(**kw):
        captured["prompt"] = kw["prompt"]
        return {"success": False, "error": "mock"}

    monkeypatch.setattr(generator, "gemini_call", fake)
    generator.generate_batch([item])
    return captured["prompt"]


def test_prompt_includes_time_context_only_when_injected(monkeypatch):
    base = {"id": "1", "text": "불금 달려보시쥬ㅎ", "label": "POSITIVE",
            "created_at": datetime(2026, 10, 30, 9, 0, tzinfo=UTC)}
    with_now = _capture_prompt(monkeypatch, {**base, "_now_kst": _kst(2026, 10, 31, 1, 5)})
    assert '"now_kst": "2026-10-31(토) 01:05"' in with_now
    assert '"comment_age_min": 425' in with_now
    assert generator._TIME_RULE in with_now
    without = _capture_prompt(monkeypatch, base)
    assert "now_kst" not in without and "comment_age_min" not in without
    assert with_now.replace(generator._TIME_RULE, "").count("\n") == without.count("\n")


def test_prompt_omits_age_when_created_at_unknown(monkeypatch):
    item = {"id": "1", "text": "좋은 아침이에요", "label": "POSITIVE",
            "_now_kst": _kst(2026, 11, 13, 9, 0)}
    prompt = _capture_prompt(monkeypatch, item)
    assert '"now_kst"' in prompt and "comment_age_min\":" not in prompt


# ── ④ 파이프라인: 실측 사고 재현 (시계 고정 2026-11-14(토) 13:00 KST) ────

@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_pipeline_blocks_stale_morning_greeting(monkeypatch, mode):
    _base_env(monkeypatch, mode)
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published = []
    _install_x(monkeypatch, published)
    monkeypatch.setattr(x_client, "fetch_mentions", lambda *_a, **_k: {
        "success": True, "tweets": [{
            "id": "100", "text": "드디어 토요모닝!! 오늘도 좋은소통 이어서 가시죠ㅎ",
            "author_id": "222", "conversation_id": "300",
            "in_reply_to_user_id": "111", "created_at": datetime.now(UTC),
        }], "users": {}, "newest_id": "100", "error": None,
    })
    monkeypatch.setattr(classifier, "gemini_call", lambda **_k: {
        "success": True, "data": [{"id": "100", "label": "POSITIVE"}],
    })
    prompts = []

    def fake_generate(**kw):
        prompts.append(kw["prompt"])
        return {"success": True, "data": [{"id": "100", "replies": [
            "기분 좋은 아침 시작이네요! 😊", "좋은 소통 이어가요, 반가워요 🙂",
        ]}]}

    monkeypatch.setattr(generator, "gemini_call", fake_generate)
    result = run_reply.main()
    entry = next(r for r in result["review"] if r["reply_tweet_id"] == "100")
    assert entry["reply_text"] == "좋은 소통 이어가요, 반가워요 🙂"
    assert entry["source"] == "AI_ALT"
    assert entry["draft_gate_reason"] == "GATE_TIME_MISMATCH"
    assert '"now_kst": "2026-11-14(토) 13:00"' in prompts[0]
    assert generator._TIME_RULE in prompts[0]
    if mode == "live":
        assert all("아침" not in text for _id, text in published)


# ── ⑤ D10: 스케줄 게이트 지연 관측 ─────────────────────────────

def test_late_minutes_only_for_run_due():
    tick = datetime(2026, 11, 13, 14, 8, tzinfo=UTC)
    due = schedule.Decision(schedule.RUN_DUE, tick, "next_run_at 도래",
                            datetime(2026, 11, 13, 10, 26, tzinfo=UTC))
    assert reply_schedule_gate.late_minutes(due) == 222.0
    skip = schedule.Decision(schedule.SKIP_NOT_DUE, tick, "미도래",
                             datetime(2026, 11, 13, 15, 0, tzinfo=UTC))
    assert reply_schedule_gate.late_minutes(skip) is None
    dispatch = schedule.Decision(schedule.RUN_DUE, tick, "workflow_dispatch")
    assert reply_schedule_gate.late_minutes(dispatch) is None


def test_decide_log_includes_late_min(monkeypatch, capsys):
    tick = datetime(2026, 11, 13, 14, 8, tzinfo=UTC)
    state = {"account": "kr_main", "next_run_at": "2026-11-13T10:26:23+00:00",
             "last_tick_at": "2026-11-13T08:31:01+00:00"}
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(reply_schedule_gate, "load_state", lambda: state)
    monkeypatch.setattr(reply_schedule_gate, "claim", lambda row, prev: True)
    d = reply_schedule_gate.run_decide(now=tick)
    assert d.decision == schedule.RUN_DUE
    assert "late_min=221.6" in capsys.readouterr().out


def test_versions():
    assert timectx.VERSION == "1.0.0"
    assert generator.VERSION == "2.5.0"
    assert gate.VERSION == "2.4.0"
    assert run_reply.VERSION == "2.5.0"
    assert reply_schedule_gate.VERSION == "1.1.0"


# ── ⑥ 시점 인사 의도 (X 확장 경로만 GREETING) ───────────────────

@pytest.mark.parametrize("comment", [
    "드디어 불타늘 금모닝!!  오늘도 좋은소통 이어서 가시죠ㅎ",
    "불금 달려보시쥬ㅎ",
    "불금 잘끝내고 이제 토요일 토요모닝입니다 ㅎㅎ",
])
def test_time_greeting_is_greeting_on_x_only(comment):
    assert policy.intent_for(comment, "POSITIVE", extended=True) == "GREETING"
    assert policy.intent_for(comment, "POSITIVE", extended=False) != "GREETING" or (
        policy._GREETING_PATTERN.search(policy.normalize_comment(comment)))


def test_time_greeting_fallback_is_greeting_pool():
    comment = "불금 달려보시쥬ㅎ"
    ctx = {"_now_kst": _kst(2026, 10, 31, 1, 5)}
    pool = generator.contextual_fallbacks({"id": "7", "text": comment, "label": "POSITIVE"},
                                          extended=True)
    chosen, source, reason = gate.choose_reply(
        ["금요일도 활기차게 보내시길 바라요!"], pool, [], comment, context=ctx, strict=True)
    assert reason == "GATE_TIME_MISMATCH" and source == "TEMPLATE_FALLBACK"
    assert chosen in policy.SAFE_POOLS["GREETING"]
