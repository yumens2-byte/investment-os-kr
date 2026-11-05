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

v1.1.0 (2026-10-07, FB-1): 플랫폼 공통화.
  BudgetGuard(costs=, limit_krw=, fallback=, cost_labels=) 키워드 전용 인자 추가.
  미지정 시 X 환경변수·상수를 사용하므로 기존 동작과 동일하다.
  Facebook은 단가가 없으므로 costs=(None, None)로 count 모드만 사용한다.

v1.2.0 (2026-11-06, DN2026_0100): 읽기 리소스 단위 계상.
  X API는 반환 리소스 단위로 과금한다(docs.x.com pricing). X_READ_RESOURCE_COST_KRW가
  설정되고 호출부가 record_read(resources=n)으로 반환 건수를 넘기면 n × 리소스 단가로
  계상한다. 사전 판정(can_read / available_read_calls)은 기존 호출 단가 X_READ_COST_KRW를
  예약값으로 그대로 쓴다. resources를 넘기지 않는 호출부(following·shock 등)는 기존과 동일.
"""

from __future__ import annotations

import logging

from reply_engine.config import (
    FALLBACK_READ_CALLS_PER_DAY,
    FALLBACK_WRITE_CALLS_PER_DAY,
    get_cost_per_call,
    get_daily_budget_krw,
    get_read_resource_cost,
)

VERSION = "1.2.0"

logger = logging.getLogger(__name__)


class BudgetGuard:
    """당일 예산 행(dict)을 감싸는 판정기. 영속화는 store가 담당."""

    def __init__(
        self,
        budget_row: dict,
        *,
        costs: tuple[float | None, float | None] | None = None,
        limit_krw: float | None = None,
        fallback: tuple[int, int] | None = None,
        cost_labels: tuple[str, str] = ("X_READ_COST_KRW", "X_WRITE_COST_KRW"),
    ):
        self.row = dict(budget_row)
        self._initial_row = dict(budget_row)
        self.read_cost, self.write_cost = get_cost_per_call() if costs is None else costs
        self.limit_krw = get_daily_budget_krw() if limit_krw is None else float(limit_krw)
        if fallback is None:
            fallback = (FALLBACK_READ_CALLS_PER_DAY, FALLBACK_WRITE_CALLS_PER_DAY)
        self.fallback_read, self.fallback_write = fallback
        self.cost_mode = self.read_cost is not None and self.write_cost is not None
        # DN2026_0100: 리소스 단가는 X 기본 경로(costs 미지정)에서만 사용한다 (FB 등은 None).
        self.read_resource_cost = get_read_resource_cost() if costs is None else None
        self.resource_mode = self.cost_mode and self.read_resource_cost is not None
        self.read_resources = 0  # 이번 실행에서 계상한 읽기 리소스 수 (리포트용)
        self.config_warnings: list[str] = []
        if not self.cost_mode:
            logger.warning(
                "[Budget] 단가 미설정 → count 모드 fallback "
                f"(read≤{self.fallback_read}, write≤{self.fallback_write})"
            )
        else:
            # 단가 오설정 감지 (B-2): 콜 1회 단가가 일일예산 이상이면 사실상 전면 차단됨.
            # 정상 단가는 예산 대비 수십분의 일 수준이어야 함 (2026-08-17 오입력 사고 재발 방지)
            for label, cost in ((cost_labels[0], self.read_cost),
                                (cost_labels[1], self.write_cost)):
                if cost >= self.limit_krw:
                    msg = (
                        f"CONFIG WARNING: {label}={cost} ≥ DAILY_BUDGET_KRW={self.limit_krw} "
                        "— 콜 1회에 일일예산 소진. 단가 오입력 가능성 (변수 값 확인 필요)"
                    )
                    self.config_warnings.append(msg)
                    logger.warning(f"[Budget] {msg}")

    def snapshot(self) -> dict:
        """리포트 JSON용 예산 스냅샷 (B-3)."""
        run_delta = {
            "read_calls": int(self.row["read_calls"]) - int(self._initial_row["read_calls"]),
            "write_calls": int(self.row["write_calls"]) - int(self._initial_row["write_calls"]),
            "gemini_calls": (
                int(self.row["gemini_calls"]) - int(self._initial_row["gemini_calls"])
            ),
            "est_cost_krw": round(
                float(self.row["est_cost_krw"]) - float(self._initial_row["est_cost_krw"]), 4
            ),
        }
        return {
            "mode": "cost" if self.cost_mode else "count",
            "read_calls": int(self.row["read_calls"]),
            "write_calls": int(self.row["write_calls"]),
            "gemini_calls": int(self.row["gemini_calls"]),
            "est_cost_krw": float(self.row["est_cost_krw"]),
            "limit_krw": self.limit_krw,
            "read_cost_krw": self.read_cost,
            "read_resource_cost_krw": self.read_resource_cost,  # DN2026_0100
            "resource_mode": self.resource_mode,
            "read_resources_run": self.read_resources,
            "write_cost_krw": self.write_cost,
            "config_warnings": self.config_warnings,
            # 상위 값은 reply/following이 공유하는 당일 누계다. dry_run에서 기존 write_calls가
            # 보이는 혼동을 막기 위해 이번 실행 증분을 별도로 명시한다.
            "run_delta": run_delta,
        }

    # ── 판정 ──
    def can_read(self) -> bool:
        if self.cost_mode:
            projected = float(self.row["est_cost_krw"]) + self.read_cost
            allowed = projected <= self.limit_krw
        else:
            allowed = int(self.row["read_calls"]) < self.fallback_read
        if not allowed:
            logger.warning(f"[Budget] 읽기 차단: {self._snapshot()}")
        return allowed

    def available_read_calls(self, maximum: int, *, reserve_krw: float | None = None) -> int:
        """현재 예산으로 허용되는 읽기 수를 ``maximum`` 이내에서 계산한다.

        DN2026_0100: 리소스 모드에서 호출부가 콜당 최악 리소스 비용(reserve_krw)을 넘기면
        호출 단가와 그 값 중 큰 값으로 예약해, 사후 리소스 계상이 예산을 넘지 않게 한다.
        """
        maximum = max(0, int(maximum))
        if self.cost_mode:
            per_call = float(self.read_cost)
            if self.resource_mode and reserve_krw is not None:
                per_call = max(per_call, float(reserve_krw))
            if per_call == 0:
                return maximum
            remaining = max(0.0, self.limit_krw - float(self.row["est_cost_krw"]))
            return min(maximum, int(remaining // per_call))
        remaining = max(0, self.fallback_read - int(self.row["read_calls"]))
        return min(maximum, remaining)

    def can_write(self) -> bool:
        if self.cost_mode:
            projected = float(self.row["est_cost_krw"]) + self.write_cost
            allowed = projected <= self.limit_krw
        else:
            allowed = int(self.row["write_calls"]) < self.fallback_write
        if not allowed:
            logger.warning(f"[Budget] 쓰기 차단: {self._snapshot()}")
        return allowed

    # ── 기록 ──
    def record_read(self, resources: int | None = None) -> None:
        """읽기 1콜 계상. DN2026_0100: resources를 넘기면 리소스 단위로 계상한다."""
        self.row["read_calls"] = int(self.row["read_calls"]) + 1
        if not self.cost_mode:
            return
        if self.resource_mode and resources is not None:
            count = max(0, int(resources))
            self.read_resources += count
            self.row["est_cost_krw"] = (
                float(self.row["est_cost_krw"]) + count * float(self.read_resource_cost)
            )
        else:
            self.row["est_cost_krw"] = float(self.row["est_cost_krw"]) + self.read_cost

    def settle_read(self, resources: int) -> None:
        """DN2026_0100: 호출 전에 record_read()로 예약한 1콜을 실제 리소스 수로 정산한다.

        리소스 모드가 아니면 아무것도 하지 않는다 (예약한 호출 단가가 그대로 계상값).
        """
        if not self.resource_mode:
            return
        count = max(0, int(resources))
        self.read_resources += count
        adjusted = (
            float(self.row["est_cost_krw"]) - float(self.read_cost)
            + count * float(self.read_resource_cost)
        )
        self.row["est_cost_krw"] = max(float(self._initial_row["est_cost_krw"]), adjusted)

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
