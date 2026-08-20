"""
following_engine/analyzer.py
==============================
Gemini 기반 게시물 분석 (문서 12장 — Structured JSON).
기존 gemini_gateway 4키 체인 재사용 (문서 보안 5장: OpenAI 신규 도입 안 함).

실패/파싱 불가 건은 결과에서 제외 → Decision에서 자동 SKIP (fail-safe, 문서 19장).
"""

from __future__ import annotations

import logging

from core.gemini_gateway import call as gemini_call
from following_engine.config import QUOTE_MAX_LENGTH

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset({"SKIP", "QUOTE", "POST", "PERMITTED_REPLY", "REVIEW_ONLY"})


def _clamp(value, default: int = 0) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def analyze_batch(items: list[dict]) -> dict[str, dict]:
    """
    items: [{"id","author","text","metrics"}...]
    반환: {post_id: 검증된 분석 dict}. 실패 건은 미포함.
    """
    if not items:
        return {}

    lines = []
    for i in items:
        m = i.get("metrics", {})
        lines.append(
            f'- id: {i["id"]} | author: {i.get("author", "")} | '
            f'likes={m.get("likes", 0)} reposts={m.get("reposts", 0)} | '
            f'text: "{i["text"][:400]}"'
        )
    prompt = (
        "당신은 한국 투자 정보 X 계정의 콘텐츠 분석가다. 아래 팔로잉 게시물 각각을 평가하라.\n"
        "이 계정의 관심 도메인: AI/반도체/미국·한국 증시/거시경제(금리·물가·연준)/에너지/방산.\n"
        "각 게시물에 대해:\n"
        "- relevant: 도메인 관련 여부 (true/false)\n"
        "- category: 짧은 영문 대문자 카테고리 (예: AI_INFRASTRUCTURE, MACRO, SEMICONDUCTOR)\n"
        "- relevanceScore/importanceScore/engagementValue/contentValue: 0~100 정수\n"
        "- summary: 한 줄 한국어 요약\n"
        "- recommendedAction: QUOTE(인용 코멘트 가치 있음) / PERMITTED_REPLY / SKIP 중 하나\n"
        "- reason: 판단 근거 한 줄\n"
        f"- generatedText: QUOTE일 때만, 한국어 {QUOTE_MAX_LENGTH}자 이내 인용 코멘트.\n"
        "  코멘트 규칙: 데이터·사실 중심 관찰 톤, 매수/매도 지시·수익 보장·확정적 전망 금지,\n"
        "  해시태그·링크 금지, 질문으로 끝내지 말 것, 원문 문장 복사 금지\n\n"
        + "\n".join(lines)
        + '\n\nJSON 배열로만 응답: [{"id":"...","relevant":true,"category":"...",'
        '"relevanceScore":0,"importanceScore":0,"engagementValue":0,"contentValue":0,'
        '"summary":"...","recommendedAction":"...","reason":"...","generatedText":"..."}]'
    )

    result = gemini_call(
        prompt=prompt,
        model="flash-lite",
        max_tokens=2048,
        temperature=0.3,
        response_json=True,
    )

    analyses: dict[str, dict] = {}
    if not (result.get("success") and isinstance(result.get("data"), list)):
        logger.warning(f"[FAnalyzer] Gemini 분석 실패 → 전건 SKIP: {result.get('error')}")
        return analyses

    for row in result["data"]:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id", ""))
        action = str(row.get("recommendedAction", "SKIP")).strip().upper()
        if not row_id or action not in _VALID_ACTIONS:
            continue
        analyses[row_id] = {
            "relevant": bool(row.get("relevant", False)),
            "category": str(row.get("category", ""))[:40],
            "relevance_score": _clamp(row.get("relevanceScore")),
            "importance_score": _clamp(row.get("importanceScore")),
            "engagement_value": _clamp(row.get("engagementValue")),
            "content_value": _clamp(row.get("contentValue")),
            "summary": str(row.get("summary", ""))[:300],
            "recommended_action": action,
            "reason": str(row.get("reason", ""))[:300],
            "generated_text": str(row.get("generatedText", "") or "").strip()[:QUOTE_MAX_LENGTH],
        }
    return analyses
