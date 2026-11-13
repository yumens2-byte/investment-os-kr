"""테스트 간 프로세스 전역 상태 격리."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_gemini_unavailable_models(monkeypatch):
    # 404 키 생략 캐시는 운영에서 프로세스(=1회 실행) 수명이다. 테스트는 한 프로세스에서
    # 여러 시나리오를 돌리므로 시나리오마다 비운다.
    import core.gemini_gateway as gw

    monkeypatch.setattr(gw, "_UNAVAILABLE_MODELS", set())


# DN2026_0103: 시점 표현 게이트(GATE_TIME_MISMATCH)는 현재 KST에 따라 판정이 달라진다.
# 파이프라인 테스트가 실행 시각에 따라 흔들리지 않도록 시계를 고정한다.
# 2026-11-14(토) 13:00 KST — 아침·저녁·밤·불금 표현은 불일치, 점심·주말·토요일은 일치.
PINNED_UTC = "2026-11-14T04:00:00+00:00"


@pytest.fixture(autouse=True)
def _pin_reply_clock(monkeypatch):
    from datetime import datetime

    from reply_engine import timectx

    monkeypatch.setattr(timectx, "_utcnow", lambda: datetime.fromisoformat(PINNED_UTC))
