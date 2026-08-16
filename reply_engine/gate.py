"""
reply_engine/gate.py
======================
발행 전 답글 텍스트 품질 게이트. 하나라도 실패하면 무응답 (재생성 없음 — 보수적).

검증 항목:
  GATE_EMPTY        — 빈 텍스트
  GATE_LENGTH       — 공백 포함 REPLY_MAX_LENGTH 초과
  GATE_NON_KR       — 비허용 스크립트 (가나/한자/키릴/태국/아랍 등)
  GATE_BANNED_WORD  — 금지어 (투자 조언성 표현 — 생성 오작동 신호)
  GATE_IMPERATIVE   — 지시형/안내형 표현 (감사·호응만 정책 위반, C-2)
  GATE_FORMAT       — 해시태그/링크/멘션 포함
  GATE_SIMILARITY   — 최근 발행분 또는 동일 배치 내 유사 (자카드, L6 — X 403 방지)
"""

from __future__ import annotations

import logging
import re

from reply_engine.config import (
    BANNED_WORDS,
    REPLY_MAX_LENGTH,
    REPLY_SIMILARITY_THRESHOLD,
)

VERSION = "1.0.1"

logger = logging.getLogger(__name__)

# 비허용 스크립트: 가나(일) / CJK 한자 / 키릴 / 태국 / 아랍
_NON_KR_PATTERN = re.compile(
    r"[\u3040-\u30ff\u4e00-\u9fff\u0400-\u04ff\u0e00-\u0e7f\u0600-\u06ff]"
)

_FORMAT_PATTERN = re.compile(r"#|https?://|t\.co/|@\w+", re.IGNORECASE)

# C-2 (2026-08-17): 지시형/안내형 표현 감지 — "감사·호응만" 정책 위반 차단.
# dry_run 2차에서 '네, 바로 확인해 보세요!' 통과 사고 재발 방지.
# 인사 관용구("좋은 하루 되세요", "~보내세요")는 매칭되지 않도록 패턴 한정.
# 주의: "행복하세요" 류 기원문도 탈락하나, 무응답은 안전한 방향이므로 허용 손실로 간주.
_IMPERATIVE_PATTERN = re.compile(
    r"(해\s?보세요|해\s?주세요|해\s?주시길|하세요|하십시오|바랍니다|해야\s?합니다)"
)


def _char_bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def jaccard_similarity(a: str, b: str) -> float:
    """문자 bigram 자카드 유사도 (0.0~1.0)."""
    set_a, set_b = _char_bigrams(a), _char_bigrams(b)
    if not set_a or not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def check_reply(text: str, recent_texts: list[str]) -> tuple[bool, str | None]:
    """
    단건 게이트. recent_texts에는 최근 발행분 + 동일 배치 내 선행 통과분을 함께 전달.
    반환: (통과 여부, 실패 코드 | None)
    """
    if not text or not text.strip():
        return False, "GATE_EMPTY"

    text = text.strip()

    if len(text) > REPLY_MAX_LENGTH:
        return False, "GATE_LENGTH"

    if _NON_KR_PATTERN.search(text):
        return False, "GATE_NON_KR"

    for word in BANNED_WORDS:
        if word in text:
            return False, "GATE_BANNED_WORD"

    if _IMPERATIVE_PATTERN.search(text):
        return False, "GATE_IMPERATIVE"

    if _FORMAT_PATTERN.search(text):
        return False, "GATE_FORMAT"

    for prev in recent_texts:
        similarity = jaccard_similarity(text, prev)
        if similarity >= REPLY_SIMILARITY_THRESHOLD:
            logger.info(f"[Gate] 유사도 탈락 ({similarity:.2f}): '{text}' ≈ '{prev}'")
            return False, "GATE_SIMILARITY"

    return True, None
