"""
reply_engine/generator.py
===========================
호응 답글 텍스트 생성.

정책 ("봇이 아닌 것처럼, 한 줄 미만, 감사·호응만"):
  - 공백 포함 40자 이내, 해시태그/링크/디스클레이머 금지, 이모지 0~1개
  - 1차: Gemini flash-lite 배치 1콜 — 댓글 내용을 반영한 자연스러운 한 줄
  - 2차 fallback: seed 기반 문구 풀 (reply_tweet_id 해시 → 결정적 선택, 멱등)

문구 풀 갱신은 HG-6 (월 1회 마스터 검수 배치 방식 — thread_builder 패턴).
"""

from __future__ import annotations

import hashlib
import logging

from core.gemini_gateway import call as gemini_call
from reply_engine.config import REPLY_MAX_LENGTH

VERSION = "1.0.1"

logger = logging.getLogger(__name__)

# ── fallback 문구 풀 (HG-6 승인 대상) ──
_POOL_POSITIVE: tuple[str, ...] = (
    "좋게 봐주셔서 감사합니다 🙏",
    "감사합니다! 큰 힘이 됩니다",
    "따뜻한 댓글 감사드려요",
    "이렇게 봐주시니 감사할 따름입니다",
    "감사합니다 😊 앞으로도 잘 부탁드려요",
    "댓글 남겨주셔서 감사합니다",
    "좋은 말씀 감사합니다!",
    "응원 감사드립니다 🙏",
    "읽어주셔서 감사해요",
    "감사합니다, 오늘도 좋은 하루 되세요",
)

_POOL_SUPPORTIVE: tuple[str, ...] = (
    "맞습니다, 저도 같은 생각이에요",
    "공감해 주셔서 감사합니다",
    "좋은 관점이시네요 👍",
    "그 부분 저도 동의합니다",
    "의견 나눠주셔서 감사해요",
    "저도 그렇게 보고 있습니다",
    "좋은 의견 감사합니다",
    "함께 지켜보시죠 🙂",
)


def _pick_from_pool(category: str, seed_key: str) -> str:
    """reply_tweet_id 기반 결정적 선택 (동일 댓글 재생성 시 동일 문구 — 멱등)."""
    pool = _POOL_POSITIVE if category == "POSITIVE" else _POOL_SUPPORTIVE
    digest = hashlib.sha256(seed_key.encode("utf-8")).hexdigest()
    return pool[int(digest[:8], 16) % len(pool)]


def generate_batch(items: list[dict]) -> dict[str, str]:
    """
    items: [{"id": str, "text": str, "label": str}, ...]  (label은 PASS 라벨)
    반환: {id: 답글 텍스트}. AI 실패 건은 풀 fallback으로 전건 보장.
    """
    if not items:
        return {}

    replies: dict[str, str] = {}

    prompt_items = "\n".join(f'- id: {i["id"]} | 댓글: "{i["text"][:200]}"' for i in items)
    prompt = (
        "당신은 한국 투자 정보 X 계정 운영자다. 아래 각 댓글에 대한 답글을 작성하라.\n"
        "규칙:\n"
        f"1. 공백 포함 {REPLY_MAX_LENGTH}자 이내, 한 문장\n"
        "2. 오직 감사 또는 공감·호응 표현만 허용\n"
        "3. 절대 금지: 질문에 대한 답변, 정보 제공, 행동 안내·권유·지시"
        "(예: '확인해 보세요', '~하세요', '~해 주세요' 류), 투자 조언·전망\n"
        "4. 댓글이 질문이어도 답하지 말고 관심에 대한 감사만 표현\n"
        "5. 자연스러운 한국어 존댓말, 사람이 쓴 것처럼\n"
        "6. 해시태그·링크·자기소개 금지, 이모지는 최대 1개 (없어도 됨)\n"
        "7. 답글끼리 표현이 겹치지 않게 각각 다르게\n\n"
        f"{prompt_items}\n\n"
        'JSON 배열로만 응답: [{"id": "...", "reply": "..."}]'
    )

    result = gemini_call(
        prompt=prompt,
        model="flash-lite",
        max_tokens=1024,
        temperature=0.9,
        response_json=True,
    )

    ai_replies: dict[str, str] = {}
    if result.get("success") and isinstance(result.get("data"), list):
        for row in result["data"]:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("id", ""))
            reply = str(row.get("reply", "")).strip().strip('"').strip("'")
            if row_id and reply:
                ai_replies[row_id] = reply
    else:
        logger.warning(f"[Generator] Gemini 생성 실패 → 전건 풀 fallback: {result.get('error')}")

    for item in items:
        reply = ai_replies.get(item["id"], "")
        if not reply:
            reply = _pick_from_pool(item["label"], item["id"])
            logger.info(f"[Generator] id={item['id']} 풀 fallback 사용")
        replies[item["id"]] = reply

    return replies
