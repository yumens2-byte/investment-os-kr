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

VERSION = "1.2.0"

logger = logging.getLogger(__name__)

# ── fallback 문구 풀 (HG-6 승인 대상) ──
_POOL_POSITIVE: tuple[str, ...] = (
    "오 감사합니다!! 😆",
    "좋게 봐주셔서 감사해요 ㅎㅎ",
    "앗 감동이에요, 감사합니다 🙌",
    "이런 댓글이 큰 힘이 돼요 😄",
    "봐주셔서 감사해요! 오늘도 화이팅입니다 💪",
    "헉 감사합니다, 더 열심히 할게요 ㅎㅎ",
    "관심 가져주셔서 감사해요~ 🙏",
    "댓글 보고 기분 좋아졌어요 ㅎㅎ 감사합니다",
    "따뜻한 말씀 감사드려요 😊",
)

_POOL_SUPPORTIVE: tuple[str, ...] = (
    "오늘도 같이 가보시죠 ㅎㅎ 🙌",
    "같은 마음이에요 😄",
    "저도 그 마음 알죠 ㅎㅎ",
    "함께 지켜봐요~ 🙂",
    "공감합니다! 다들 화이팅이에요 💪",
    "그쵸 ㅎㅎ 좋은 하루 되세요",
    "저도 같은 생각이에요 😊",
    "오 완전 공감이에요 ㅎㅎ",
    "함께해 주셔서 든든하네요 🙌",
)


def pick_fallback(category: str, seed_key: str) -> str:
    """GATE_SIMILARITY 탈락 시 재시도용 풀 문구 (F-2). 결정적 seed — 멱등."""
    return _pick_from_pool(category, seed_key)


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
        "당신은 한국·미국 주식 데이터를 다루는 투자 정보 X 계정 운영자다. "
        "내 게시글에 달린 각 댓글에 짧은 답글을 작성하라.\n"
        "댓글 배경: 이 계정의 댓글에는 종목 은어와 시장 환호가 자주 등장한다 "
        "(예: '돈복사'=수익 기대 환호, '슈드'=SCHD ETF 같은 종목 애칭, '가즈아'류 상승 기원).\n"
        "규칙:\n"
        f"1. 공백 포함 {REPLY_MAX_LENGTH}자 이내, 한 문장\n"
        "2. 댓글의 방향을 먼저 판단하라:\n"
        "   [A] 계정·콘텐츠를 향한 감사/칭찬 → 감사 인사\n"
        "   [B] 시장·종목에 대한 관찰/환호 → 감사가 아니라 가벼운 공감 인사 "
        "('같은 마음입니다 🙂', '함께 지켜보시죠' 류)\n"
        "   [C] 의미가 불명확한 은어/짧은 반응 → 의도를 단정하지 말고 담백한 짧은 호응만\n"
        "3. 의도 라벨('응원/언급/관심/의견'+감사) 접두는 그 의도가 댓글에 명백할 때만 허용. "
        "불확실하면 라벨 없이 답하라 — 상대가 하지 않은 행동에 감사하면 어색해진다\n"
        "4. 절대 금지: 질문에 대한 답변, 정보 제공, 행동 안내·권유·지시, 투자 조언·전망, "
        "물음표 사용, 댓글 내용에 대한 해석·놀람 표현, "
        "댓글의 단어나 상황어(축하/생일/명절 등) 재사용, "
        "모르는 맥락을 아는 척하기, 내 감정·경험 지어내기('저도 놀랐어요' 등)\n"
        "5. 댓글이 질문이어도 답하지 말고 관심에 대한 감사만 표현\n"
        "6. 상대가 나에게 감사를 표현한 댓글이면 '저야말로 감사합니다' 방향으로만\n"
        "7. 짧은 댓글(ㅋㅋ, ㅇㅈ 등)에는 짧고 담백한 감사만 — 과장 수식 금지\n"
        "8. 톤: 가볍고 친근한 SNS 존댓말 — 딱딱한 격식체 금지. "
        "'오/와/앗/헉' 같은 감탄사, 'ㅎㅎ/ㅋㅋ', '~요!', '~죠~' 표현 적극 활용. "
        "기계적인 '~합니다.' 종결만 반복하지 말 것\n"
        "9. 해시태그·링크·자기소개 금지. 이모지는 0~2개 — 답글 절반 이상에 "
        "자연스럽게 넣되 매번 같은 이모지 금지 (🙂만 반복 금지)\n"
        "10. 답글끼리 표현이 겹치지 않게 각각 다르게 — 서로 다른 단어로 시작하라\n\n"
        "예시 (좋음/나쁨):\n"
        '- 댓글 "가자 돈복사!!!" (시장 환호) → 좋음: "오늘도 같이 가보시죠 ㅎㅎ 🙌" / '
        '나쁨: "응원 감사해요" (나를 응원한 게 아닌데 감사 — 의도 오독)\n'
        '- 댓글 "슈드 잘 가네요 ㅋ" (종목 시황 관찰) → 좋음: "보기만 해도 흐뭇하죠 😄" / '
        '나쁨: "언급 감사해요" (상대가 하지 않은 행동에 감사)\n'
        '- 댓글 "축하해주셔서 감사합니다" → 좋음: "앗 저야말로 감사드려요 ㅎㅎ" / '
        '나쁨: "축하해주셔서 감사합니다" (댓글을 그대로 되풀이 — 역할이 뒤집힘)\n'
        '- 댓글 "ㅇㅈ" → 좋음: "ㅎㅎ 공감 감사해요!" / '
        '나쁨: "정성스러운 의견 감사합니다" (과장 수식 금지)\n'
        '- 댓글 "그나마 제대로된 회사네요" → 좋음: "의견 감사합니다" / '
        '나쁨: "좋은 회사라니 다행입니다" (모르는 맥락에 개입 — 아는 척)\n'
        '- 댓글 "앜ㅋㅋㅋ" → 좋음: "웃음 포인트 맞았다니 다행이에요 😆" / '
        '나쁨: "정말요? 저도 놀랐어요" (질문 + 지어낸 감정)\n\n'
        "댓글의 단어나 상황어 재사용 금지 규칙은 예시의 종목 은어에도 동일 적용된다. "
        "댓글이 질문이어도 답하지 말고 감사만 남겨라.\n\n"
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
