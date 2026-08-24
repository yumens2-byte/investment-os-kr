"""
shock_news_engine/gate.py — 콘텐츠 안전 게이트 (v1.0.0)

[완화 불가 항목 — R-A 법적 리스크]
  GATE_REAL_NAME  실명·이니셜+나이 패턴 ("김OO(31)", "홍길동씨")
  GATE_VERDICT    유죄 단정 표현 ("살해했다" 등 — '혐의'는 허용)
  GATE_GRAPHIC    잔혹 상세 묘사어
그 외:
  GATE_EMPTY / GATE_LENGTH / GATE_FORMAT(해시태그·링크) / GATE_DUP_EVENT(L3 제목 유사)
"""

from __future__ import annotations

import logging
import re

from reply_engine.gate import jaccard_similarity
from shock_news_engine.config import COMMENT_MAX_LENGTH, TITLE_SIMILARITY_THRESHOLD

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

# 실명 패턴: "홍길동씨/군/양"(조사 결합형 포함), "김OO(31)", "이모(42)씨", "A(19)군"
# 주의: \b는 한글+조사 사이에서 성립하지 않아 사용 금지 ("김철수씨가" 미검출 사고, 테스트로 고정).
# "아저씨" 류 일반명사 오탈락 가능 — 무발행 방향의 안전한 손실로 수용.
_REAL_NAME_PATTERN = re.compile(
    r"([가-힣]{2,4}\s?(씨|군|양))|([가-힣A-Za-z]{1,4}[OoＯ○]{0,2}\s?\(\d{1,2}\))"
)
# 유죄 단정 (허용: 혐의, ~로 알려졌다, 체포, 입건)
_VERDICT_PATTERN = re.compile(r"(살해했|죽였|범인이다|살인자|저질렀다)")
_GRAPHIC_WORDS = ("토막", "절단", "난도질", "참수", "훼손된 시신", "피투성이")
_FORMAT_PATTERN = re.compile(r"#|https?://|t\.co/", re.IGNORECASE)

_TITLE_NORM = re.compile(r"[^0-9A-Za-z가-힣]")


def _norm_title(title: str) -> str:
    return _TITLE_NORM.sub("", title or "")


def check_comment(comment: str) -> tuple[bool, str | None]:
    text = (comment or "").strip()
    if not text:
        return False, "GATE_EMPTY"
    if len(text) > COMMENT_MAX_LENGTH:
        return False, "GATE_LENGTH"
    if _REAL_NAME_PATTERN.search(text):
        return False, "GATE_REAL_NAME"
    if _VERDICT_PATTERN.search(text):
        return False, "GATE_VERDICT"
    if any(w in text for w in _GRAPHIC_WORDS):
        return False, "GATE_GRAPHIC"
    if _FORMAT_PATTERN.search(text):
        return False, "GATE_FORMAT"
    return True, None


def check_title_duplicate(title: str, recent_titles: list[str]) -> tuple[bool, str | None]:
    """L3: 동일 사건 이종 기사 차단 — 최근 발행 제목과 유사도 비교."""
    norm = _norm_title(title)
    if not norm:
        return True, None
    for prev in recent_titles:
        sim = jaccard_similarity(norm, _norm_title(prev))
        if sim >= TITLE_SIMILARITY_THRESHOLD:
            logger.info(f"[SGate] 동일 사건 판정 ({sim:.2f}): '{title}' ≈ '{prev}'")
            return False, "GATE_DUP_EVENT"
    return True, None
