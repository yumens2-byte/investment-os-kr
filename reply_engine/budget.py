"""
reply_engine/budget.py
========================
일일 API 비용 가드.

동작 모드 (자동 판별):
  cost 모드  — X_READ_COST_KRW / X_WRITE_COST_KRW 둘 다 설정 시.
               est_cost + 다음 호출 예상비용 > DAILY_BUDGET_KRW → 차단
  count 모드 — 단가 미설정 시 보수 fallback.
               read_calls < FALLBACK_READ_CALLS_PER_DAY (8),
               write_calls < FALLBACK_WRITE_CALLS_PER_DAY (5)

Gemini 무료 키 체인은 비용 0으로 취급하되 호출 수는 gemini_calls로 추적한다.
"""

from __future__ import annotations

import logging

from reply_engine.config import (
    FALLBACK_READ_CALLS_PER_DAY,
    FALLBACK_WRITE_CALLS_PER_DAY,
    get_cost_per_call,
    get_daily_budget_krw,
)

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


class BudgetGuard:
    """당일 예산 행(dict)을 감싸는 판정기. 영속화는 store가 담당."""

    def __init__(self, budget_row: dict):
        self.row = dict(budget_row)
        self.read_cost, self.write_cost = get_cost_per_call()
        self.limit_krw = get_daily_budget_krw()
        self.cost_mode = self.read_cost is not None and self.write_cost is not None
        if not self.cost_mode:
            logger.warning(
                "[Budget] 단가 미설정 → count 모드 fallback "
                f"(read≤{FALLBACK_READ_CALLS_PER_DAY}, write≤{FALLBACK_WRITE_CALLS_PER_DAY})"
            )

    # ── 판정 ──
    def can_read(self) -> bool:
        if self.cost_mode:
            projected = float(self.row["est_cost_krw"]) + self.read_cost
            allowed = projected <= self.limit_krw
        else:
            allowed = int(self.row["read_calls"]) < FALLBACK_READ_CALLS_PER_DAY
        if not allowed:
            logger.warning(f"[Budget] 읽기 차단: {self._snapshot()}")
        return allowed

    def can_write(self) -> bool:
        if self.cost_mode:
            projected = float(self.row["est_cost_krw"]) + self.write_cost
            allowed = projected <= self.limit_krw
        else:
            allowed = int(self.row["write_calls"]) < FALLBACK_WRITE_CALLS_PER_DAY
        if not allowed:
            logger.warning(f"[Budget] 쓰기 차단: {self._snapshot()}")
        return allowed

    # ── 기록 ──
    def record_read(self) -> None:
        self.row["read_calls"] = int(self.row["read_calls"]) + 1
        if self.cost_mode:
            self.row["est_cost_krw"] = float(self.row["est_cost_krw"]) + self.read_cost

    def record_write(self) -> None:
        self.row["write_calls"] = int(self.row["write_calls"]) + 1
        if self.cost_mode:
            self.row["est_cost_krw"] = float(self.row["est_cost_krw"]) + self.write_cost

    def record_gemini(self) -> None:
        self.row["gemini_calls"] = int(self.row["gemini_calls"]) + 1

    def _snapshot(self) -> str:
        return (
            f"read={self.row['read_calls']} write={self.row['write_calls']} "
            f"gemini={self.row['gemini_calls']} est={self.row['est_cost_krw']}KRW "
            f"limit={self.limit_krw}KRW mode={'cost' if self.cost_mode else 'count'}"
        )
