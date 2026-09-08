"""
core/alert.py — 관리자 텔레그램 실패 알림 (2026-09-08 승인, 항목 3)
=====================================================================
목적: 발행 실패·플랫폼 오류가 사후 점검 때까지 묻히는 문제 해소
(08-29/09-02 PUBLISH_FAIL이 2주간 미인지된 사례).

설계:
- 대상은 **관리자 전용** TELEGRAM_ALERT_CHAT_ID. 기존 TELEGRAM_KR_FREE_CHANNEL_ID(공개 채널)는
  절대 사용하지 않는다 — 운영 내부 오류가 팔로워에게 노출되면 안 됨.
- 토큰/채팅ID 미설정이면 조용히 no-op (알림 부재가 파이프라인을 실패시키지 않음).
- 단일 시도, 실패해도 예외 전파 없음.
"""

from __future__ import annotations

import logging
import os

import requests

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 8


def send_admin_alert(text: str) -> bool:
    """관리자에게 1줄 알림. 미설정/실패 시 False (파이프라인 무영향)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("[Alert] TELEGRAM_ALERT_CHAT_ID 미설정 — 알림 생략")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True},
            timeout=_TIMEOUT_SEC,
        )
        ok = resp.status_code == 200
        if not ok:
            logger.warning(f"[Alert] 전송 실패 status={resp.status_code}")
        return ok
    except Exception as exc:
        logger.warning(f"[Alert] 전송 예외 (무시): {exc}")
        return False
