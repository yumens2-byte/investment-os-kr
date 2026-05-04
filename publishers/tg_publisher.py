"""
KR Market OS — Telegram 발행기
Bot API sendMessage 기반 국장 프리 채널 발행
parse_mode 미사용 — 이모지/특수문자 포함 텍스트의 400 에러 방지
"""

from __future__ import annotations

import logging
import os

import requests

from config.settings import TG_BASE_URL, TG_TIMEOUT_SEC
from utils.retry import with_retry

VERSION = "1.2.0"

logger = logging.getLogger(__name__)


def publish_message(text: str, dry_run: bool = False) -> bool:
    """
    국장 프리 채널에 메시지 발행 (3회 재시도 / 2초 간격).
    parse_mode 미사용 — HTML 예약문자 포함 시 400 에러 방지.
    반환: 성공 True / 실패 False
    dry_run=True 이면 발행 없이 로그만 출력.
    """
    if dry_run:
        logger.info(f"[TG DRY_RUN]\n{text}\n---")
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    channel_id = os.environ.get("TELEGRAM_KR_FREE_CHANNEL_ID", "")

    if not token or not channel_id:
        logger.error("[TG] TELEGRAM_BOT_TOKEN / TELEGRAM_KR_FREE_CHANNEL_ID 미설정")
        return False

    url = f"{TG_BASE_URL}/bot{token}/sendMessage"
    payload = {
        "chat_id": channel_id,
        "text": text,
        "disable_web_page_preview": True,
        # parse_mode 미설정 — 일반 텍스트 발행
        # 이모지, 화살표(→), 특수문자 포함 시 HTML 파싱 오류 방지
    }

    def _do_send() -> dict:
        resp = requests.post(url, json=payload, timeout=TG_TIMEOUT_SEC)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            raise ValueError(f"TG API 오류: {result}")
        return result

    result = with_retry(_do_send, label="TG sendMessage")
    if result is None:
        logger.error("[TG] 발행 최종 실패")
        return False

    logger.info("[TG] 발행 완료")
    return True
