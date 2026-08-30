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

v1.1.0 (2026-08-30, R-4): 룰 판정 순서 보정.
  기존에는 '?' 존재만으로 QUESTION을 선점해 감탄형 반응이 전량 무응답 처리됐다.
  실사고(08-30 artifact): "오호? 👍" → 👍가 _POSITIVE_MARKERS에 있음에도
  QUESTION 확정되어 스킵. '?'를 질문의 충분조건에서 제외하고
  의문 어미/의문사를 1차 기준으로 승격한다.
"""

from __future__ import annotations

import logging
import re

from core.gemini_gateway import call as gemini_call

VERSION = "1.1.0"

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

# R-4: 의문 어미/의문사 — 이것만으로 QUESTION 확정 (문말 한정 없이 탐색)
# 주의: 어간 활용형을 포괄하려면 '인가요'가 아니라 '가요'로 잡아야 한다.
# ('유익한가요'는 인가요/건가요 어느 쪽에도 매칭되지 않는다 — 초기 설계 오류)
# '가요' 오탐(가요계 등)은 QUESTION=무응답이라 안전한 방향이므로 감수한다.
_INTERROGATIVE_PATTERN = re.compile(
    r"(가요|까요|나요|어떻게|어떤|왜|언제|얼마|어디|뭐예요)"
)

# R-4: '?' 단독 — 감탄형("오호?", "대박?")과 질문을 구분하지 못하므로 보조 신호로만 사용
_QUESTION_MARK_PATTERN = re.compile(r"[?？]")

# 하위호환: 기존 이름 참조처 보존 (판정에는 사용하지 않음)
_QUESTION_PATTERN = _INTERROGATIVE_PATTERN


def classify_by_rule(text: str) -> str | None:
    """
    룰 1차 분류. 확정 불가 시 None (AI로 위임).

    판정 순서 (R-4):
      1) 의문 어미/의문사        → QUESTION
      2) 부정 마커               → NEGATIVE
      3) '?' 있고 긍정 마커 없음 → QUESTION (보수 유지)
      4) 긍정 마커               → POSITIVE
      5) 그 외                   → None (AI 위임)
    """
    body = re.sub(r"@\w+", "", text or "").strip()

    if _INTERROGATIVE_PATTERN.search(body):
        return "QUESTION"

    for marker in _NEGATIVE_MARKERS:
        if marker in body:
            return "NEGATIVE"

    has_positive = any(marker in body for marker in _POSITIVE_MARKERS)

    if _QUESTION_MARK_PATTERN.search(body) and not has_positive:
        return "QUESTION"

    if has_positive:
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
