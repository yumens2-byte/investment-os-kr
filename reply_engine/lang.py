"""
reply_engine/lang.py
======================
댓글 언어 판정 (R-9, 2026-08-30).

배경 (2026-08-30 라이브 사고):
  베트남어 댓글에 "현실적인 판단이라니, 동의합니다"라는 한국어 답글이 발행됨.
  상대는 '판단'을 내린 적이 없다 — LLM이 이해하지 못한 맥락에 의도를 지어낸 것.
  파이프라인 어디에도 '댓글 언어'를 보는 지점이 없었다.
  (gate._NON_KR_PATTERN은 답글 텍스트만 검사하며, 베트남어는 라틴 문자 기반이라
   해당 패턴(가나/한자/키릴/태국/아랍)에 애초에 걸리지도 않는다.)

정책 (마스터 확정 C안):
  외국어 댓글도 무응답 처리하지 않고 한국어로 응답하되,
  AI 생성 대신 의도를 단정하지 않는 정형 문구만 사용한다.

판정 규칙:
  멘션(@handle) 제거 후,
    - 한글(음절/자모)이 1자라도 있으면      → 한국어 (False)
    - 한글이 없고 라틴 문자가 임계 미만이면 → 한국어로 취급 (False)
      · 이모지·숫자·문장부호 전용 댓글("👍", "!!!")의 기존 동작을 보존하기 위함
    - 한글이 없고 라틴 문자가 임계 이상이면 → 외국어 (True)

의존성 없음 (config만 참조) — generator/run_reply 양쪽에서 안전하게 재사용한다.
"""

from __future__ import annotations

import re

from reply_engine.config import REPLY_NON_KR_LATIN_THRESHOLD

VERSION = "1.0.0"

# 멘션은 항상 라틴 문자이므로 판정 전 반드시 제거해야 한다.
# (제거하지 않으면 "@tiger18272 감사합니다"가 외국어로 오판된다)
_MENTION_PATTERN = re.compile(r"@\w+")

# 한글 음절 + 자모 (ㅋㅋ, ㅇㅈ 같은 자모 전용 댓글도 한국어로 인정)
_HANGUL_PATTERN = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")

_LATIN_PATTERN = re.compile(r"[A-Za-z]")


def strip_mentions(text: str) -> str:
    """멘션 핸들 제거 후 공백 정리."""
    return _MENTION_PATTERN.sub("", text or "").strip()


def has_hangul(text: str) -> bool:
    """한글(음절 또는 자모) 포함 여부."""
    return bool(_HANGUL_PATTERN.search(strip_mentions(text)))


def latin_char_count(text: str) -> int:
    """멘션 제외 라틴 알파벳 문자 수."""
    return len(_LATIN_PATTERN.findall(strip_mentions(text)))


def is_non_korean(text: str) -> bool:
    """
    외국어 댓글 판정.

    한글이 하나라도 있으면 무조건 한국어로 본다 (혼용 댓글은 AI 생성 유지).
    한글이 없을 때만 라틴 문자 수로 판정하며, 임계 미만(이모지·숫자·기호 전용)은
    기존 동작 보존을 위해 한국어로 취급한다.
    """
    body = strip_mentions(text)
    if not body:
        return False
    if _HANGUL_PATTERN.search(body):
        return False
    return len(_LATIN_PATTERN.findall(body)) >= REPLY_NON_KR_LATIN_THRESHOLD
