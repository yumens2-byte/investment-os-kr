"""
backdata_engine/notify.py — 운영자 전용 알림 (DN2026_0090, STD-OBS-06·STD-SEC-01)

core/alert.send_admin_alert와 같은 대상(TELEGRAM_ALERT_CHAT_ID)·단일 시도·무예외 동작이지만,
예외 로그에서 봇 토큰을 지운다. requests 예외 문자열에는 'url: /bot<TOKEN>/sendMessage'가
그대로 들어가고(실측), 이 파이프라인은 logs/를 artifact로 올리기 때문이다.
"""

from __future__ import annotations

import logging
import os

import requests

VERSION = "0.1.0"

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 8
_post = requests.post  # 테스트 교체 지점


def mask_id(value: object) -> str:
    """식별자 마스킹: '***' + 끝 4자리 (public 레포 Actions 로그 노출 방지, 회장 지시 11-02)."""
    text = str(value or "")
    return "***" if len(text) <= 4 else f"***{text[-4:]}"


def scrub(text: str, *secrets: str) -> str:
    """문자열에서 주어진 값(페이지 ID·토큰 등)을 '***'로 치환."""
    out = str(text or "")
    for s in secrets:
        if s and len(s) >= 4:
            out = out.replace(s, "***")
    return out


def send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("[BNotify] TELEGRAM_ALERT_CHAT_ID 미설정 — 알림 생략")
        return False
    try:
        resp = _post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True},
            timeout=_TIMEOUT_SEC,
        )
        ok = getattr(resp, "status_code", 0) == 200
        if not ok:
            logger.warning(f"[BNotify] 전송 실패 status={getattr(resp, 'status_code', None)}")
        return ok
    except Exception as exc:
        logger.warning(f"[BNotify] 전송 예외 (무시): {type(exc).__name__}: "
                       f"{str(exc).replace(token, '***')[:200]}")
        return False
