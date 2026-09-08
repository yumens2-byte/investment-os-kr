"""
reply_engine/classifier.py
============================
댓글 의도 분류. 무응답이 기본값(default-deny).

라벨:
  POSITIVE            — 긍정/감사/칭찬 → 답글 대상
  SUPPORTIVE_NEUTRAL  — 호응성 중립 (공감/맞장구) → 답글 대상
  NEGATIVE / QUESTION / SPAM / AMBIGUOUS — 무응답

2단계:
  1차 룰: 명백한 긍정/부정/질문을 AI 호출 없이 확정 (비용 절약)
  2차 AI: 잔여 건만 Gemini flash-lite 배치 1콜 (JSON) — 실패 시 전건 AMBIGUOUS
"""

from __future__ import annotations

import logging
import re

from core.gemini_gateway import call as gemini_call

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

PASS_LABELS: frozenset[str] = frozenset({"POSITIVE", "SUPPORTIVE_NEUTRAL"})
ALL_LABELS: frozenset[str] = frozenset(
    {"POSITIVE", "SUPPORTIVE_NEUTRAL", "NEGATIVE", "QUESTION", "SPAM", "AMBIGUOUS"}
)

# ── 룰 1차 패턴 ──
_POSITIVE_MARKERS: tuple[str, ...] = (
    "감사", "고맙", "좋아요", "좋네요", "좋습니다", "최고", "굿", "훌륭",
    "잘 봤", "잘봤", "유익", "도움", "화이팅", "응원", "멋지", "대박",
    "👍", "🙏", "❤", "🔥", "💯", "짱",
)
_NEGATIVE_MARKERS: tuple[str, ...] = (
    "틀렸", "별로", "사기", "거짓", "엉터리", "쓰레기", "허접", "실망",
)

_QUESTION_PATTERN = re.compile(
    r"\?|？|(인가요|일까요|건가요|나요|까요|어떻게|왜|뭔가요|뭐예요)\s*$"
)


def classify_by_rule(text: str) -> str | None:
    """룰 1차 분류. 확정 불가 시 None (AI로 위임)."""
    body = re.sub(r"@\w+", "", text or "").strip()

    if _QUESTION_PATTERN.search(body):
        return "QUESTION"
    for marker in _NEGATIVE_MARKERS:
        if marker in body:
            return "NEGATIVE"
    for marker in _POSITIVE_MARKERS:
        if marker in body:
            return "POSITIVE"
    return None


def classify_batch(items: list[dict]) -> dict[str, str]:
    """
    items: [{"id": str, "text": str}, ...]
    반환: {id: label}. 룰 확정 건은 AI 미호출, 잔여 건만 배치 1콜.
    AI 실패/파싱 불가 건은 AMBIGUOUS (무응답).
    """
    labels: dict[str, str] = {}
    pending: list[dict] = []

    for item in items:
        rule_label = classify_by_rule(item["text"])
        if rule_label is not None:
            labels[item["id"]] = rule_label
        else:
            pending.append(item)

    if not pending:
        return labels

    prompt_items = "\n".join(f'- id: {i["id"]} | 댓글: "{i["text"][:200]}"' for i in pending)
    prompt = (
        "다음은 투자 정보 X(트위터) 계정 게시글에 달린 댓글 목록이다.\n"
        "각 댓글의 의도를 아래 라벨 중 하나로 분류하라.\n"
        "라벨: POSITIVE(긍정/감사/칭찬), SUPPORTIVE_NEUTRAL(공감/맞장구성 중립), "
        "NEGATIVE(부정/비판), QUESTION(질문), SPAM(광고/홍보), AMBIGUOUS(판단 불가)\n\n"
        f"{prompt_items}\n\n"
        'JSON 배열로만 응답: [{"id": "...", "label": "..."}]'
    )

    result = gemini_call(
        prompt=prompt,
        model="flash-lite",
        max_tokens=1024,
        temperature=0.1,
        response_json=True,
    )

    ai_labels: dict[str, str] = {}
    if result.get("success") and isinstance(result.get("data"), list):
        for row in result["data"]:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("id", ""))
            label = str(row.get("label", "")).strip().upper()
            if row_id and label in ALL_LABELS:
                ai_labels[row_id] = label
    else:
        logger.warning(
            f"[Classifier] Gemini 분류 실패 → 잔여 전건 AMBIGUOUS: {result.get('error')}"
        )

    for item in pending:
        labels[item["id"]] = ai_labels.get(item["id"], "AMBIGUOUS")

    return labels
