"""테스트 간 프로세스 전역 상태 격리."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_gemini_unavailable_models(monkeypatch):
    # 404 키 생략 캐시는 운영에서 프로세스(=1회 실행) 수명이다. 테스트는 한 프로세스에서
    # 여러 시나리오를 돌리므로 시나리오마다 비운다.
    import core.gemini_gateway as gw

    monkeypatch.setattr(gw, "_UNAVAILABLE_MODELS", set())
