"""DN2026_0090 문안 게이트·조립 테스트 (Gemini는 가짜 함수)."""

from __future__ import annotations

import pytest

from backdata_engine import composer, gate
from backdata_engine.config import DISCLAIMER

STATS = {
    "index": "^KS11", "index_name": "코스피", "kind": "C2",
    "condition_key": "C2_DOWN_5", "condition_label": "5일 연속 하락",
    "today": "2026-11-02", "today_chg_pct": -1.4, "n": 57, "n_all": 58,
    "rarity": 0.02, "period": ["1999-03-04", "2026-09-30"], "eligible": True,
    "next": {
        "d1": {"n": 57, "up_ratio": 0.5614, "median_pct": 0.3},
        "d5": {"n": 57, "up_ratio": 0.6316, "median_pct": 1.1},
        "d20": {"n": 57, "up_ratio": 0.7018, "median_pct": 2.46},
    },
}


def _allowed():
    return composer.numbers_of(STATS)["allowed"]


def test_weighted_length_rules():
    assert gate.weighted_length("a1 %") == 4
    assert gate.weighted_length("가") == 2
    assert gate.weighted_length("※") == 2           # U+203B는 가중치 100 구간 밖
    assert gate.weighted_length("’") == 1           # U+2019는 8208~8223 구간
    assert gate.weighted_length("…") == 2           # U+2026은 구간 밖


def test_x_length_limit_280():
    allowed = set()
    base = f"\n{DISCLAIMER}"
    room = 280 - gate.weighted_length(base)
    ok_text = "가" * (room // 2) + base
    assert gate.weighted_length(ok_text) <= 280
    assert gate.check_text(ok_text, allowed, "x") == (True, None)
    too_long = "가" * (room // 2 + 1) + base
    assert gate.check_text(too_long, allowed, "x") == (False, "GATE_LENGTH")


@pytest.mark.parametrize("snippet,reason", [
    ("지금 매수 타이밍", "GATE_BANNED"),
    ("수익 보장", "GATE_BANNED"),
    ("좋아요 눌러주세요", "GATE_BAIT"),
    ("댓글로 알려주세요", "GATE_BAIT"),
    ("#코스피", "GATE_FORMAT"),
    ("https://x.com", "GATE_FORMAT"),
    ("@someone", "GATE_FORMAT"),
    ("株価", "GATE_CHARSET"),
    ("Привет", "GATE_CHARSET"),
    ("상승 77%", "GATE_NUMBER"),
])
def test_gate_rejects(snippet, reason):
    text = f"{snippet}\n{DISCLAIMER}"
    assert gate.check_text(text, _allowed(), "x") == (False, reason)


def test_gate_requires_disclaimer():
    assert gate.check_text("코스피 5일 연속 하락", _allowed(), "x") == (False, "GATE_DISCLAIMER")


def test_josa():
    assert composer.josa_subject("코스피") == "코스피가"
    assert composer.josa_subject("코스닥") == "코스닥이"
    assert composer.josa_means("5일 연속 하락") == "5일 연속 하락으로"
    assert composer.josa_means("52주 신고가") == "52주 신고가로"


def _gemini(hook=None, success=True):
    calls = []

    def fake(prompt, **kw):
        calls.append(kw)
        if not success:
            return {"success": False, "data": None, "api_calls": 2, "paid": False,
                    "error": "x"}
        return {"success": True, "data": {"hook": hook}, "api_calls": 1, "paid": False,
                "error": None}
    fake.calls = calls
    return fake


def test_compose_llm_hook_used_and_free_only():
    fake = _gemini("코스피 5일 연속 하락, 다음엔 반등했을까?")
    out = composer.compose(STATS, fake)
    assert out["ok"] and out["hook_source"] == "llm"
    assert fake.calls[0]["allow_paid"] is False and fake.calls[0]["model"] == "flash-lite"
    assert out["x_weighted_len"] <= 280
    assert "같은 경우 57번(1999~2026)" in out["text_x"]
    assert "다음 날 상승 56%" in out["text_x"]
    assert "20일 뒤 중앙값 +2.5%" in out["text_x"]
    assert out["text_x"].endswith(DISCLAIMER) and out["text_fb"].endswith(DISCLAIMER)
    assert "코스피가 5일 연속 하락으로 마감했습니다" in out["text_fb"]
    assert len(out["text_hash"]) == 64


@pytest.mark.parametrize("hook", [
    "지금이 매수 기회일까?",          # 금칙어
    "과거 87% 반등한 신호",            # 통계에 없는 숫자
    "댓글로 의견 주세요",              # 반응 유도
    "아주 길게 늘어진 훅 문장이라서 사십 자를 훌쩍 넘겨 버리는 경우의 예시 문장입니다",
    # R6-01 치명: 통계·날짜·기간 숫자를 훅에 재사용해 본문과 모순되는 경우
    "20일 뒤 -2.5% 하락?",
    "코스피 11% 폭락 신호?",
    "다음 날 상승 확률 20%?",
    "57번 중 반등은?",
    # R6-03: 금칙어 우회
    "지금 담아야 할 때일까?",
    "저점 매집 타이밍?",
    "Buy the dip?",
    "반등 전망 나왔다",
    "알림 설정하고 지켜보세요",
    # R6-02 제안
    "코스피 5일 연속 하락, 지금이 바닥일까?",
    "5일 연속 하락, 5일 뒤엔?",
])
def test_compose_bad_llm_hook_falls_back(hook):
    out = composer.compose(STATS, _gemini(hook))
    assert out["ok"] and out["hook_source"] == "fixed"
    assert out["text_x"].startswith("코스피 5일 연속 하락")


def test_compose_llm_failure_falls_back():
    out = composer.compose(STATS, _gemini(success=False))
    assert out["ok"] and out["hook_source"] == "fixed"
    assert out["llm"]["success"] is False


def test_compose_fails_closed_when_template_breaks(monkeypatch):
    monkeypatch.setattr(composer, "DISCLAIMER", "※ 다른 문구")
    out = composer.compose(STATS, _gemini("코스피 5일 연속 하락, 그다음은?"))
    assert out["ok"] is False and out["text_x"] is None


def test_hook_may_use_label_numbers_only():
    ok, _ = gate.check_hook("코스피 5일 연속 하락, 다음은?", {5.0}, 40)
    assert ok
    assert gate.check_hook("코스피 6일 연속 하락?", {5.0}, 40) == (False, "GATE_NUMBER")


@pytest.mark.parametrize("pct,expected", [(-0.04, "0.0"), (0.049, "0.0"), (0.05, "+0.1"),
                                          (-1.26, "-1.3"), (2.46, "+2.5")])
def test_median_format(pct, expected):
    assert composer._fmt_med(pct) == expected
