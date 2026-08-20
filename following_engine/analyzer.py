"""
following_engine/analyzer.py
==============================
Gemini 기반 게시물 분석 (문서 12장 — Structured JSON).
기존 gemini_gateway 4키 체인 재사용 (문서 보안 5장: OpenAI 신규 도입 안 함).

실패/파싱 불가 건은 결과에서 제외 → Decision에서 자동 SKIP (fail-safe, 문서 19장).

J-1 (2026-08-20 실사고 — 11건 배치 응답이 max_tokens=2048에 잘려 JSON 파손, 전건 SKIP):
  ① max_tokens 8192  ② 10건 단위 chunk 분할  ③ 응답 슬림화(summary/reason 40자,
  generatedText는 QUOTE만)  ④ chunk별 Invalid JSON 1회 재시도 (문서 25장)
"""

from __future__ import annotations

import logging

from core.gemini_gateway import call as gemini_call
from following_engine.config import QUOTE_MAX_LENGTH

VERSION = "1.0.1"

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset({"SKIP", "QUOTE", "POST", "PERMITTED_REPLY", "REVIEW_ONLY"})

ANALYZER_BATCH_SIZE = 10       # J-1: chunk 분할 크기
ANALYZER_MAX_TOKENS = 8192     # J-1: 2048 잘림 사고 수정 (flash-lite 지원 범위)


def _clamp(value, default: int = 0) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def analyze_batch(items: list[dict]) -> dict[str, dict]:
    """
    items: [{"id","author","text","metrics"}...]
    반환: {post_id: 검증된 분석 dict}. 실패 건(chunk 단위)은 미포함 → Decision에서 SKIP.
    J-1: ANALYZER_BATCH_SIZE 단위 분할 호출 — 응답 잘림 방지.
    """
    analyses: dict[str, dict] = {}
    for start in range(0, len(items), ANALYZER_BATCH_SIZE):
        analyses.update(_analyze_chunk(items[start:start + ANALYZER_BATCH_SIZE]))
    return analyses


def _analyze_chunk(items: list[dict]) -> dict[str, dict]:
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
        "- summary: 한국어 40자 이내 요약 (반드시 짧게)\n"
        "- recommendedAction: QUOTE(인용 코멘트 가치 있음) / PERMITTED_REPLY / SKIP 중 하나\n"
        "- reason: 판단 근거, 한국어 40자 이내 (반드시 짧게)\n"
        f"- generatedText: recommendedAction이 QUOTE일 때만 작성 (그 외는 빈 문자열 \"\"), "
        f"한국어 {QUOTE_MAX_LENGTH}자 이내 인용 코멘트.\n"
        "  코멘트 규칙: 데이터·사실 중심 관찰 톤, 매수/매도 지시·수익 보장·확정적 전망 금지,\n"
        "  해시태그·링크 금지, 질문으로 끝내지 말 것, 원문 문장 복사 금지\n\n"
        + "\n".join(lines)
        + '\n\nJSON 배열로만 응답: [{"id":"...","relevant":true,"category":"...",'
        '"relevanceScore":0,"importanceScore":0,"engagementValue":0,"contentValue":0,'
        '"summary":"...","recommendedAction":"...","reason":"...","generatedText":"..."}]'
    )

    analyses: dict[str, dict] = {}

    # J-1: Invalid JSON 1회 재시도 (문서 25장 — 제한된 횟수 재요청)
    result = None
    for attempt in (1, 2):
        result = gemini_call(
            prompt=prompt,
            model="flash-lite",
            max_tokens=ANALYZER_MAX_TOKENS,
            temperature=0.3,
            response_json=True,
        )
        if result.get("success") and isinstance(result.get("data"), list):
            break
        logger.warning(
            f"[FAnalyzer] 분석 응답 JSON 불가 (attempt={attempt}): {result.get('error')}"
        )

    if not (result and result.get("success") and isinstance(result.get("data"), list)):
        logger.warning("[FAnalyzer] Gemini 분석 실패 → 해당 chunk 전건 SKIP")
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
