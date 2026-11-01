"""
backdata_engine/gate.py — 문안 기계 검사 (DN2026_0090, default-deny)
=====================================================================
  GATE_EMPTY        빈 문안
  GATE_LENGTH       X 가중 글자 수 > 280 (CJK 2, docs.x.com Counting characters) / FB 길이 초과
  GATE_FORMAT       해시태그·URL·@계정 (STD-LLM-10, Meta 스팸 정책)
  GATE_BANNED       투자 권유·보장 표현 (STD-LLM-08)
  GATE_BAIT         반응 유도 문구 (Meta engagement bait)
  GATE_CHARSET      허용 외 문자 — 한자·가나·키릴 등 비한국어 누출 (STD-QUAL-04)
  GATE_NUMBER       통계가 만들지 않은 숫자 (STD-QUAL-03)
  GATE_DISCLAIMER   고정 고지 누락
알려진 한계: 개별 종목명 사전 검사는 없다
(템플릿에는 종목명이 없고, LLM 훅은 숫자·금칙어 검사만 받음).
"""

from __future__ import annotations

import re
import unicodedata

from backdata_engine.config import (
    BAIT_TERMS,
    BANNED_TERMS,
    DISCLAIMER,
    FB_MAX_CHARS,
    HOOK_BANNED_TERMS,
    X_MAX_WEIGHTED,
)

VERSION = "0.1.0"

# twitter-text v3 가중치 100(=1자) 구간. 그 밖은 200(=2자)
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))

_FORMAT_PATTERN = re.compile(r"#|@|https?://|www\.|t\.co/|\.com\b|\.kr\b", re.IGNORECASE)
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_EXTRA_ALLOWED = set("·※~→…‘’“”")


def weighted_length(text: str) -> int:
    total = 0
    for ch in unicodedata.normalize("NFC", text):
        code = ord(ch)
        total += 1 if any(lo <= code <= hi for lo, hi in _LIGHT_RANGES) else 2
    return total


def _char_allowed(ch: str) -> bool:
    code = ord(ch)
    if ch in "\n\t" or 0x20 <= code <= 0x7E:          # ASCII 출력 문자
        return True
    if 0xAC00 <= code <= 0xD7A3:                        # 한글 음절
        return True
    if 0x3131 <= code <= 0x318E:                        # 한글 호환 자모
        return True
    return ch in _EXTRA_ALLOWED


def _num(token: str) -> float:
    return round(float(token), 6)


def check_text(text: str, allowed_numbers: set[float], platform: str) -> tuple[bool, str | None]:
    body = (text or "").strip()
    if not body:
        return False, "GATE_EMPTY"
    if platform == "x" and weighted_length(body) > X_MAX_WEIGHTED:
        return False, "GATE_LENGTH"
    if platform == "fb" and len(body) > FB_MAX_CHARS:
        return False, "GATE_LENGTH"
    if _FORMAT_PATTERN.search(body):
        return False, "GATE_FORMAT"
    if any(term in body for term in BANNED_TERMS):
        return False, "GATE_BANNED"
    if any(term in body for term in BAIT_TERMS):
        return False, "GATE_BAIT"
    if not all(_char_allowed(ch) for ch in body):
        return False, "GATE_CHARSET"
    allowed = {round(v, 6) for v in allowed_numbers}
    for token in _NUMBER_PATTERN.findall(body):
        if _num(token) not in allowed:
            return False, "GATE_NUMBER"
    if DISCLAIMER not in body:
        return False, "GATE_DISCLAIMER"
    return True, None


def check_hook(hook: str, label_numbers: set[float], max_chars: int) -> tuple[bool, str | None]:
    """
    LLM 훅 1줄 사전 검사 (R6-01 치명 반영).
    숫자는 조건 라벨에 있는 숫자만 허용한다 — 통계 수치·날짜·기간 숫자를 훅에 쓰면
    부호·문맥 검증 없이 본문과 모순되는 문안이 나올 수 있다("20일 뒤 -2.5% 하락?").
    영문자는 금지(영어 권유문 우회 차단), 훅 전용 금칙어를 추가로 본다.
    """
    h = (hook or "").strip()
    if not h or "\n" in h:
        return False, "GATE_EMPTY"
    if len(h) > max_chars:
        return False, "GATE_LENGTH"
    if re.search(r"[A-Za-z]", h):
        return False, "GATE_CHARSET"
    if any(term in h for term in HOOK_BANNED_TERMS):
        return False, "GATE_BANNED"
    if re.search(r"[+\-−]\s*\d", h):
        return False, "GATE_NUMBER"
    probe = f"{h}\n{DISCLAIMER}"
    return check_text(probe, label_numbers, "x")
