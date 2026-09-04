"""
reply_engine/generator.py
===========================
호응 답글 텍스트 생성.

정책 ("봇이 아닌 것처럼, 한 줄 미만, 감사·호응만"):
  - 공백 포함 40자 이내, 해시태그/링크/디스클레이머 금지, 이모지 0~1개
  - 1차: Gemini flash-lite 배치 1콜 — 댓글 내용을 반영한 자연스러운 한 줄
  - 2차 fallback: seed 기반 문구 풀 (reply_tweet_id 해시 → 결정적 선택, 멱등)

문구 풀 갱신은 HG-6 (월 1회 마스터 검수 배치 방식 — thread_builder 패턴).

v1.4.0 (2026-09-04, R-12): 원글(부모 트윗) 컨텍스트 주입.
  2026-08-18 「LLM 생성 컨텍스트 규약」 이행. 댓글 단문만으로 생성하면
  환각·주객전도가 필연이며 실사고 2건의 공통 근본 원인이었다.
  원글은 '맥락 파악용'이며 답글에 인용·요약하지 않는다.
  ⚠️ 원글 작성자 = 나(계정 운영자)를 전제로 한 프롬프트다.
     REPLY_FOREIGN_THREAD_ENABLED를 켜면 타인 글이 원글일 수 있으므로
     활성화 시 이 전제를 재검토해야 한다.

v1.3.0 (2026-08-30, R-9): 외국어 댓글 정형 문구 경로 분리.
  라이브 사고: 베트남어 댓글에 "현실적인 판단이라니, 동의합니다" 발행 —
  상대가 하지 않은 행동에 반응(프롬프트 규칙 3 위반). LLM이 이해하지 못하는
  언어에서는 의도 오독이 필연이므로, 외국어 건은 AI 배치에서 제외하고
  의도를 단정하지 않는 정형 문구 풀에서 결정적으로 선택한다 (마스터 확정 C안).
"""

from __future__ import annotations

import hashlib
import logging

from core.gemini_gateway import call as gemini_call
from reply_engine.config import REPLY_MAX_LENGTH
from reply_engine.lang import is_non_korean

VERSION = "1.4.0"

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


# ── 외국어 댓글 전용 정형 문구 풀 (R-9, HG-6 승인 대상) ──
# 요건: 댓글 내용을 해석·재사용하지 않고, 상대가 하지 않은 행동을 단정하지 않으며,
#       방문/관심에 대한 담백한 감사만 표현한다.
# 검증 완료(2026-08-30): 전 문구 게이트 통과, 풀 내부 최대 자카드 0.467(임계 0.6),
#       12건 연속 발행 시뮬레이션 전량 통과.
_POOL_NON_KR: tuple[str, ...] = (
    "관심 가져주셔서 감사합니다 🙏",
    "들러주셔서 고맙습니다 😊",
    "봐주셔서 감사해요!",
    "댓글 남겨주셔서 감사합니다 ㅎㅎ",
    "함께해 주셔서 감사드려요 🙌",
    "찾아주셔서 고마워요 😄",
    "관심 감사드립니다!",
    "읽어주셔서 감사합니다 😉",
    "반가워요, 감사합니다 🙂",
    "고맙습니다, 좋은 하루 되세요",
    "언제나 감사드려요 💪",
    "좋은 하루 보내세요 🙂",
)


def pick_fallback(category: str, seed_key: str) -> str:
    """GATE_SIMILARITY 탈락 시 재시도용 풀 문구 (F-2). 결정적 seed — 멱등."""
    return _pick_from_pool(category, seed_key)


def pick_non_kr(seed_key: str) -> str:
    """외국어 댓글용 정형 문구 (R-9). 결정적 seed — 동일 댓글 재처리 시 동일 문구."""
    digest = hashlib.sha256(f"nonkr:{seed_key}".encode()).hexdigest()
    return _POOL_NON_KR[int(digest[:8], 16) % len(_POOL_NON_KR)]


def _pick_from_pool(category: str, seed_key: str) -> str:
    """reply_tweet_id 기반 결정적 선택 (동일 댓글 재생성 시 동일 문구 — 멱등)."""
    pool = _POOL_POSITIVE if category == "POSITIVE" else _POOL_SUPPORTIVE
    digest = hashlib.sha256(seed_key.encode("utf-8")).hexdigest()
    return pool[int(digest[:8], 16) % len(pool)]


def _format_item(item: dict) -> str:
    """
    프롬프트 1행 구성 (R-12). 원글이 없으면 '(확인 불가)'로 명시해
    LLM이 맥락을 지어내지 않도록 한다 (침묵보다 명시가 안전하다).
    """
    parent = (item.get("parent_text") or "").strip().replace("\n", " ")
    parent_part = f'"{parent[:160]}"' if parent else "(확인 불가)"
    body = (item.get("text") or "").replace("\n", " ")
    return f'- id: {item["id"]} | 원글: {parent_part} | 댓글: "{body[:200]}"'


def generate_batch(items: list[dict]) -> dict[str, str]:
    """
    items: [{"id": str, "text": str, "label": str}, ...]  (label은 PASS 라벨)
    반환: {id: 답글 텍스트}. AI 실패 건은 풀 fallback으로 전건 보장.

    R-9: 외국어 댓글은 AI 배치에서 제외하고 정형 문구 풀에서 결정적 선택한다.
    프롬프트에 외국어 원문이 섞이면 다른 건의 생성 품질까지 오염되므로,
    분리는 품질·비용 양쪽에서 이득이다. AI 대상이 0건이면 Gemini 호출도 생략한다.
    """
    if not items:
        return {}

    replies: dict[str, str] = {}

    ai_items: list[dict] = []
    for item in items:
        if is_non_korean(item["text"]):
            replies[item["id"]] = pick_non_kr(item["id"])
            logger.info(f"[Generator] id={item['id']} 외국어 댓글 → 정형 문구 (R-9)")
        else:
            ai_items.append(item)

    if not ai_items:
        logger.info("[Generator] AI 생성 대상 없음 — Gemini 호출 생략")
        return replies

    prompt_items = "\n".join(_format_item(i) for i in ai_items)
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
        "10. 답글끼리 표현이 겹치지 않게 각각 다르게 — 서로 다른 단어로 시작하라\n"
        "11. 각 항목의 '원글'은 그 댓글이 달린 내 게시글 본문이다. 댓글의 의도를 "
        "원글 맥락에서 해석하라. 단 원글 내용을 답글에 인용·요약·설명하지 말 것 "
        "— 맥락 파악 전용이다. 원글이 '(확인 불가)'면 맥락을 추측하지 말고 "
        "담백한 호응만 하라\n"
        "12. 원글을 쓴 사람은 나다. 내가 원글에서 이미 한 말(축하·설명·의견 등)을 "
        "댓글 작성자가 한 것처럼 되받지 마라 — 역할이 뒤집힌다\n\n"
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

    for item in ai_items:
        reply = ai_replies.get(item["id"], "")
        if not reply:
            reply = _pick_from_pool(item["label"], item["id"])
            logger.info(f"[Generator] id={item['id']} 풀 fallback 사용")
        replies[item["id"]] = reply

    return replies
