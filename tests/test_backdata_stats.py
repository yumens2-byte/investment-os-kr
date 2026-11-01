"""DN2026_0090 백데이터 통계 산출 테스트 (고정 시계열, 네트워크 없음)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backdata_engine import stats

# 10일 주기: 5일 상승 → 3일 하락 → 2일 소폭 상승. 마지막 날이 '3일 연속 하락' 3일째가 되게 자른다.
_PATTERN = [1.5, 1.5, 1.5, 1.5, 1.5, -1.2, -1.2, -1.2, 0.5, 0.5]


def make_rows(cycles: int, end_offset: int = 7, start=date(2020, 1, 1)):
    pcts = (_PATTERN * cycles)[: (cycles - 1) * 10 + end_offset + 1]
    closes = [100.0]
    for p in pcts:
        closes.append(closes[-1] * (1 + p / 100))
    return [(start + timedelta(days=i), c) for i, c in enumerate(closes)]


@pytest.mark.parametrize("r,expected", [
    (-5.0, "LE_M3"), (-3.0, "LE_M3"), (-2.5, "M3_M2"), (-2.0, "M3_M2"),
    (-1.5, "M2_M1"), (-1.0, "M2_M1"), (-0.99, "FLAT"), (0.0, "FLAT"), (0.99, "FLAT"),
    (1.0, "P1_P2"), (1.99, "P1_P2"), (2.0, "P2_P3"), (3.0, "GE_P3"), (7.0, "GE_P3"),
    (None, None),
])
def test_bucket_boundaries(r, expected):
    assert stats.bucket_of(r) == expected


def test_streaks():
    rets = [None, 1.0, 1.0, -1.0, -1.0, -1.0, 0.0, 2.0]
    assert stats.streaks(rets) == [0, 1, 2, -1, -2, -3, 0, 1]


def test_high_low_flags():
    closes = [5, 4, 3, 6, 1, 3]
    assert stats.high_low_flags(closes, window=3) == [None, None, None, "HIGH", "LOW", None]


def test_analyze_selects_rarest_condition():
    rows = make_rows(60)
    cands = stats.analyze_index("^KS11", "코스피", rows)
    keys = {c["condition_key"] for c in cands}
    assert "C2_DOWN_3" in keys and "C1_M2_M1" in keys
    chosen = stats.select(cands, ["^KS11", "^KQ11"])
    assert chosen["condition_key"] == "C2_DOWN_3"   # 주기당 1회 < C1 주기당 3회
    assert chosen["condition_label"] == "3일 연속 하락"
    assert chosen["n"] >= 20
    # 3일 하락 다음 날은 항상 +0.5% → 상승 비율 100%
    assert chosen["next"]["d1"]["up_ratio"] == 1.0
    assert chosen["next"]["d1"]["n"] == chosen["next"]["d20"]["n"] == chosen["n"]


def test_today_and_unmatured_excluded():
    rows = make_rows(60)
    chosen = stats.select(stats.analyze_index("^KS11", "코스피", rows), ["^KS11"])
    last_date = rows[-1][0].isoformat()
    assert chosen["period"][1] < last_date
    # 20거래일이 지나지 않은 최근 사례(직전 1주기)는 n에서 빠지고 n_all에는 남는다
    assert chosen["n_all"] - chosen["n"] >= 1
    first, last = chosen["period"]
    assert (rows[-1][0] - date.fromisoformat(last)).days >= 20


def test_rare_condition_below_min_sample_falls_to_next():
    rows = make_rows(12)   # C2(주기당 1회) 표본 < 20, C1(주기당 3회) 표본 ≥ 20
    cands = {c["condition_key"]: c for c in stats.analyze_index("^KS11", "코스피", rows)}
    assert cands["C2_DOWN_3"]["eligible"] is False
    assert stats.select(list(cands.values()), ["^KS11"])["condition_key"] == "C1_M2_M1"


def test_min_sample_default_deny():
    rows = make_rows(5)    # 모든 조건 표본 < 20
    cands = stats.analyze_index("^KS11", "코스피", rows)
    assert cands and not any(c["eligible"] for c in cands)
    assert stats.select(cands, ["^KS11"]) is None


def test_hundred_percent_stat_passes_gate():
    from backdata_engine import composer

    chosen = stats.select(stats.analyze_index("^KS11", "코스피", make_rows(60)), ["^KS11"])
    assert chosen["next"]["d1"]["up_ratio"] == 1.0
    out = composer.compose(chosen, lambda p, **k: {"success": False, "api_calls": 0})
    assert out["ok"] and "다음 날 상승 100%" in out["text_x"]


def test_deterministic():
    rows = make_rows(60)
    a = stats.select(stats.analyze_index("^KS11", "코스피", rows), ["^KS11"])
    b = stats.select(stats.analyze_index("^KS11", "코스피", rows), ["^KS11"])
    assert a == b


def test_flat_day_has_no_c1_candidate():
    rows = make_rows(60, end_offset=9)   # 마지막 날 +0.5% (FLAT), 2일 연속 상승
    cands = stats.analyze_index("^KS11", "코스피", rows)
    assert all(not c["condition_key"].startswith("C1_") for c in cands)
