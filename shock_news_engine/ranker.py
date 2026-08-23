"""
shock_news_engine/ranker.py — 티어링(결정적) + Gemini 랭킹·코멘트 생성 (v1.0.0)

1단계 티어링: 제목 키워드 → 1(살인) > 2(실종) > 3(폭행·강력) > 4(보조). 무매칭 제외.
2단계 Gemini: 상위 후보(≤10) 중 대중 반응 유발력 최대 1건 선정 + 놀람 코멘트 생성.
  - Invalid JSON 1회 재시도 (J-1 규약)
  - 실패 시 티어 1위 기사 + 내장 안전 템플릿 fallback (실명·단정 없음 보장)
의견 유도 문구: article_hash 기반 결정적 50% (ENGAGE_PROMPT_RATE).
"""

from __future__ import annotations

import logging

from core.gemini_gateway import call as gemini_call
from shock_news_engine.config import (
    COMMENT_MAX_LENGTH,
    ENGAGE_PROMPT_RATE,
    RANK_CANDIDATE_LIMIT,
    TIER_KEYWORDS,
)

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

_FALLBACK_TEMPLATES = (
    "이런 일이 실제로 있었다니 믿기지 않네요.",
    "기사 보고 한참을 멍하게 있었습니다.",
    "요즘 세상이 어떻게 돌아가는 건지, 말문이 막히는 소식입니다.",
    "뉴스를 보다가 손이 멈췄습니다. 안타까운 일입니다.",
    "이런 소식은 볼 때마다 마음이 무겁습니다.",
    "쉽게 지나칠 수 없는 사건이라 공유합니다.",
)
_ENGAGE_SUFFIX = " 여러분은 어떻게 보셨나요."


def assign_tier(title: str) -> int | None:
    """제목 키워드 티어링 — 낮은 번호 우선. 무매칭 None."""
    lowered = title.lower()
    for tier in sorted(TIER_KEYWORDS):
        if any(kw.lower() in lowered for kw in TIER_KEYWORDS[tier]):
            return tier
    return None


def select_candidates(articles: list[dict]) -> list[dict]:
    """티어 부여 → (티어 asc, 발행시각 desc) 정렬 상위 RANK_CANDIDATE_LIMIT."""
    tiered = []
    for art in articles:
        tier = assign_tier(art["title"])
        if tier is None:
            continue
        tiered.append({**art, "tier": tier})
    tiered.sort(
        key=lambda a: (a["tier"], -(a["published"].timestamp() if a["published"] else 0))
    )
    return tiered[:RANK_CANDIDATE_LIMIT]


def wants_engage_prompt(seed_key: str) -> bool:
    """의견 유도 문구 포함 여부 — 결정적 seed (재실행 멱등)."""
    bucket = int(seed_key[:8], 16) % 100 if seed_key else 0
    return bucket < int(ENGAGE_PROMPT_RATE * 100)


def _fallback(candidates: list[dict]) -> dict:
    top = candidates[0]
    idx = int(top["article_hash"][:8], 16) % len(_FALLBACK_TEMPLATES)
    comment = _FALLBACK_TEMPLATES[idx]
    if wants_engage_prompt(top["article_hash"]):
        comment += _ENGAGE_SUFFIX
    return {**top, "comment": comment, "picked_by": "fallback"}


def rank_and_generate(candidates: list[dict], session: str) -> dict | None:
    """후보 중 1건 선정 + 코멘트 생성. 후보 없음 → None."""
    if not candidates:
        return None

    lines = "\n".join(
        f'- id: {c["article_hash"][:12]} | tier: {c["tier"]} | 제목: "{c["title"][:120]}"'
        for c in candidates
    )
    engage = wants_engage_prompt(candidates[0]["article_hash"])
    engage_rule = (
        "마지막에 독자 의견을 묻는 짧은 문장 1개를 자연스럽게 붙여라."
        if engage else "의견을 묻는 문장은 넣지 마라."
    )
    prompt = (
        "너는 한국어 SNS 사용자다. 아래 사건 기사 후보 중 대중이 가장 놀라고 "
        "댓글을 많이 달 만한 기사 1건을 고르고, 그 기사에 대한 놀람 코멘트를 작성하라.\n"
        "코멘트 규칙:\n"
        f"1. 한국어 2~3문장, 공백 포함 {COMMENT_MAX_LENGTH}자 이내\n"
        "2. 절대 금지: 사람 실명·이니셜·나이 언급, 유죄 단정 표현"
        "('살해했다'식 단정 금지 — '혐의', '~로 알려졌다'만 허용), "
        "잔혹한 상세 묘사, 해시태그, 링크, 조롱·혐오 표현\n"
        "3. 놀람과 안타까움 위주의 자연스러운 사람 말투, 이모지 최대 1개\n"
        f"4. {engage_rule}\n\n"
        f"{lines}\n\n"
        '반드시 JSON만 응답: {"chosen_id": "...", "comment": "..."}'
    )

    result = None
    for attempt in (1, 2):   # Invalid JSON 1회 재시도 (J-1 규약)
        result = gemini_call(
            prompt=prompt, model="flash-lite", max_tokens=1024,
            temperature=0.4, response_json=True,
        )
        if result.get("success") and isinstance(result.get("data"), dict):
            break
        logger.warning(f"[SRanker] 랭킹 응답 JSON 불가 (attempt={attempt}): {result.get('error')}")

    if not (result and result.get("success") and isinstance(result.get("data"), dict)):
        logger.warning("[SRanker] Gemini 실패 → 티어 1위 + 템플릿 fallback")
        return _fallback(candidates)

    data = result["data"]
    chosen_id = str(data.get("chosen_id", "")).strip()
    comment = str(data.get("comment", "")).strip()
    chosen = next((c for c in candidates if c["article_hash"].startswith(chosen_id)), None)
    if chosen is None or not comment:
        logger.warning("[SRanker] 선정 결과 불일치/코멘트 공백 → fallback")
        return _fallback(candidates)

    return {**chosen, "comment": comment, "picked_by": "gemini"}
