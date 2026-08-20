"""
following_engine/decision.py
==============================
Decision Engine (문서 13장 판단 순서) + Action 매핑 (Q2/Q3 승인 반영).

매핑: QUOTE→QUOTE / PERMITTED_REPLY→REVIEW_ONLY(강등) / POST→SKIPPED_POLICY(범위 제외)
skip 코드: SKIP_NOT_RELEVANT / SKIP_SCORE / SKIP_TEXT_INVALID / SKIP_SIMILAR
           / SKIPPED_POLICY / SKIP
"""

from __future__ import annotations

import logging

from following_engine.config import (
    DUP_SIMILARITY_THRESHOLD,
    MIN_CONTENT_VALUE,
    MIN_ENGAGEMENT_VALUE,
    MIN_RELEVANCE_SCORE,
    QUOTE_MAX_LENGTH,
)
from reply_engine.config import BANNED_WORDS
from reply_engine.gate import jaccard_similarity

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def _validate_quote_text(text: str) -> bool:
    if not text or len(text) > QUOTE_MAX_LENGTH:
        return False
    if "#" in text or "http" in text.lower():
        return False
    if text.rstrip().endswith(("?", "？")):
        return False
    return all(word not in text for word in BANNED_WORDS)


def decide(analysis: dict, recent_texts: list[str]) -> tuple[str, str | None]:
    """
    (action_type, skip_reason) 반환. action_type: QUOTE / REVIEW_ONLY / SKIP.
    판단 순서(문서 13장): relevant → 점수 → 텍스트 검증 → 유사도 → 매핑.
    """
    if not analysis.get("relevant"):
        return "SKIP", "SKIP_NOT_RELEVANT"

    if (
        analysis["relevance_score"] < MIN_RELEVANCE_SCORE
        or analysis["content_value"] < MIN_CONTENT_VALUE
        or analysis["engagement_value"] < MIN_ENGAGEMENT_VALUE
    ):
        return "SKIP", "SKIP_SCORE"

    recommended = analysis["recommended_action"]

    if recommended == "QUOTE":
        text = analysis.get("generated_text", "")
        if not _validate_quote_text(text):
            return "SKIP", "SKIP_TEXT_INVALID"
        for prev in recent_texts:
            if jaccard_similarity(text, prev) >= DUP_SIMILARITY_THRESHOLD:
                return "SKIP", "SKIP_SIMILAR"
        return "QUOTE", None

    if recommended == "PERMITTED_REPLY":
        return "REVIEW_ONLY", None            # Q2: 자동 Reply 금지 — 마스터 승인형 후보

    if recommended == "POST":
        return "SKIP", "SKIPPED_POLICY"       # Q3: Phase 1 범위 제외

    return "SKIP", "SKIP"
