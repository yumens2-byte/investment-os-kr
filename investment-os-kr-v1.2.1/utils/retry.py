"""
KR Market OS — 공통 재시도 유틸
외부 HTTP 호출 실패 시 3회 / 2초 간격 재시도.
최종 실패 시 None 반환 (파이프라인 중단 없음).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS: int = 3
_DEFAULT_DELAY_SEC: float = 2.0


def with_retry(
    func: Callable[..., Any],
    *args: Any,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    delay_sec: float = _DEFAULT_DELAY_SEC,
    label: str = "",
    **kwargs: Any,
) -> Any:
    """
    func(*args, **kwargs)를 최대 max_attempts회 실행.

    - 실패 시 delay_sec초 대기 후 재시도
    - 최종 실패 시 None 반환 (예외 전파 없음)
    - 각 시도마다 WARNING/ERROR 로그 출력

    Parameters
    ----------
    func         : 실행할 callable
    *args        : func 위치 인자
    max_attempts : 최대 시도 횟수 (기본 3)
    delay_sec    : 재시도 간 대기 초 (기본 2.0)
    label        : 로그 식별 문자열
    **kwargs     : func 키워드 인자

    Returns
    -------
    func 반환값 또는 None (최종 실패 시)
    """


    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            _ = exc
            if attempt < max_attempts:
                logger.warning(
                    f"[Retry] {label} 실패 ({attempt}/{max_attempts}): {exc}"
                    f" → {delay_sec}s 후 재시도"
                )
                time.sleep(delay_sec)
            else:
                logger.error(
                    f"[Retry] {label} 최종 실패 ({attempt}/{max_attempts}): {exc}"
                )

    return None
