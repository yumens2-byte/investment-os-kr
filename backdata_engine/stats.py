"""
backdata_engine/stats.py — 백데이터 통계 산출 (DN2026_0090, LLM 미사용)
=======================================================================
오늘(마지막 봉)이 해당하는 조건을 찾고, 과거 같은 조건 날짜의 이후 1·5·20거래일 성과를 집계한다.

조건
  C1 등락률 구간  — 당일 종가 등락률 구간 (FLAT 구간은 후보에서 제외)
  C2 연속 일수    — 오늘이 k일째 연속 상승(하락), k ≥ STREAK_MIN
  C3 52주 신고가·신저가 — 종가가 직전 252거래일 최고(최저) 갱신

선택: 오늘 해당 조건 중 과거 빈도(rarity)가 가장 낮고, 20거래일 표본이 MIN_SAMPLE 이상인 1개.
      동률이면 C3 > C2 > C1, 지수는 config.INDEXES 순서. 같은 입력이면 같은 결과(결정적).
표본: 오늘 이전 날짜 중 20거래일이 이미 지난 사례만. 1·5·20일 지표가 같은 표본(n)을 쓴다.
"""

from __future__ import annotations

import statistics
from datetime import date

from backdata_engine.config import (
    CHG_BUCKETS,
    HIGH_LOW_WINDOW,
    HORIZONS,
    MIN_SAMPLE,
    STREAK_MIN,
)

VERSION = "0.1.0"

_BUCKET_LABEL = {
    "LE_M3": "3% 이상 하락",
    "M3_M2": "2~3% 하락",
    "M2_M1": "1~2% 하락",
    "P1_P2": "1~2% 상승",
    "P2_P3": "2~3% 상승",
    "GE_P3": "3% 이상 상승",
}
_KIND_PRIORITY = {"C3": 0, "C2": 1, "C1": 2}


def returns_pct(closes: list[float]) -> list[float | None]:
    """r[i] = closes[i]/closes[i-1]-1 (%). r[0]은 None."""
    out: list[float | None] = [None]
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        out.append((closes[i] / prev - 1) * 100 if prev else None)
    return out


def bucket_of(r: float | None) -> str | None:
    """하락 구간은 (low, high], 상승 구간은 [low, high), FLAT은 (-1, 1)."""
    if r is None:
        return None
    for key, low, high in CHG_BUCKETS:
        if low is None:                       # LE_M3: r ≤ high
            if r <= high:
                return key
        elif high is None:                    # GE_P3: r ≥ low
            if r >= low:
                return key
        elif high <= 0:                       # 하락 구간
            if low < r <= high:
                return key
        elif low >= 0:                        # 상승 구간
            if low <= r < high:
                return key
        elif low < r < high:                  # FLAT
            return key
    return None


def streaks(rets: list[float | None]) -> list[int]:
    """s[i] > 0: i일까지 s[i]일째 연속 상승, < 0: 연속 하락, 0: 보합·없음."""
    out: list[int] = []
    for i, r in enumerate(rets):
        prev = out[i - 1] if i > 0 else 0
        if r is None or r == 0:
            out.append(0)
        elif r > 0:
            out.append(prev + 1 if prev > 0 else 1)
        else:
            out.append(prev - 1 if prev < 0 else -1)
    return out


def high_low_flags(closes: list[float], window: int = HIGH_LOW_WINDOW) -> list[str | None]:
    """'HIGH' / 'LOW' / None. 직전 window봉이 없으면 None."""
    out: list[str | None] = []
    for i, c in enumerate(closes):
        if i < window:
            out.append(None)
            continue
        past = closes[i - window:i]
        if c > max(past):
            out.append("HIGH")
        elif c < min(past):
            out.append("LOW")
        else:
            out.append(None)
    return out


def matured(matches: list[int], last: int) -> list[int]:
    """가장 긴 기간(20거래일)이 이미 지난 사례만 — 모든 기간을 같은 표본으로 집계한다."""
    longest = max(HORIZONS)
    return [j for j in matches if j + longest <= last]


def _forward(closes: list[float], matches: list[int], last: int) -> dict:
    result = {}
    for h in HORIZONS:
        rets = [(closes[j + h] / closes[j] - 1) * 100 for j in matches]
        if rets:
            result[f"d{h}"] = {
                "n": len(rets),
                "up_ratio": sum(1 for x in rets if x > 0) / len(rets),
                "median_pct": statistics.median(rets),
            }
        else:
            result[f"d{h}"] = {"n": 0, "up_ratio": None, "median_pct": None}
    return result


def _today_conditions(rets, strk, flags, last) -> list[tuple[str, str, str]]:
    """(kind, condition_key, label) 목록."""
    conds: list[tuple[str, str, str]] = []
    if flags[last] == "HIGH":
        conds.append(("C3", "C3_HIGH_52W", "52주 신고가"))
    elif flags[last] == "LOW":
        conds.append(("C3", "C3_LOW_52W", "52주 신저가"))
    s = strk[last]
    if abs(s) >= STREAK_MIN:
        word = "상승" if s > 0 else "하락"
        conds.append(("C2", f"C2_{'UP' if s > 0 else 'DOWN'}_{abs(s)}", f"{abs(s)}일 연속 {word}"))
    b = bucket_of(rets[last])
    if b is not None and b != "FLAT":
        conds.append(("C1", f"C1_{b}", _BUCKET_LABEL[b]))
    return conds


def _matches(kind: str, key: str, rets, strk, flags, last) -> tuple[list[int], int]:
    """(과거 일치 인덱스, 판정 가능 일수). 오늘(last)은 제외."""
    if kind == "C3":
        target = "HIGH" if key.endswith("HIGH_52W") else "LOW"
        eligible = range(HIGH_LOW_WINDOW, last)
        return [j for j in eligible if flags[j] == target], len(eligible)
    if kind == "C2":
        today = strk[last]
        eligible = range(1, last)
        return [j for j in eligible if strk[j] == today], len(eligible)
    bucket = key[len("C1_"):]
    eligible = range(1, last)
    return [j for j in eligible if bucket_of(rets[j]) == bucket], len(eligible)


def analyze_index(symbol: str, name: str, rows: list[tuple[date, float]]) -> list[dict]:
    """오늘 해당 조건별 통계 후보 목록 (표본 기준 미달 포함, 'eligible'로 표시)."""
    if len(rows) < 2:
        return []
    dates = [d for d, _ in rows]
    closes = [c for _, c in rows]
    last = len(closes) - 1
    rets = returns_pct(closes)
    strk = streaks(rets)
    flags = high_low_flags(closes)

    out = []
    for kind, key, label in _today_conditions(rets, strk, flags, last):
        all_matches, base = _matches(kind, key, rets, strk, flags, last)
        matches = matured(all_matches, last)
        fwd = _forward(closes, matches, last)
        n_min = len(matches)
        out.append({
            "index": symbol,
            "index_name": name,
            "kind": kind,
            "condition_key": key,
            "condition_label": label,
            "today": dates[last].isoformat(),
            "today_chg_pct": rets[last],
            "n": len(matches),
            "n_all": len(all_matches),
            "rarity": (len(all_matches) / base) if base else None,
            "period": [dates[matches[0]].isoformat(), dates[matches[-1]].isoformat()]
            if matches else None,
            "next": fwd,
            "eligible": n_min >= MIN_SAMPLE,
        })
    return out


def select(candidates: list[dict], index_order: list[str]) -> dict | None:
    pool = [c for c in candidates if c["eligible"] and c["rarity"] is not None]
    if not pool:
        return None
    pool.sort(key=lambda c: (
        c["rarity"],
        _KIND_PRIORITY[c["kind"]],
        index_order.index(c["index"]) if c["index"] in index_order else 99,
    ))
    return pool[0]
