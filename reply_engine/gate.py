"""
reply_engine/gate.py
======================
발행 전 답글 텍스트 품질 게이트. 하나라도 실패하면 무응답 (재생성 없음 — 보수적).

검증 항목:
  GATE_EMPTY        — 빈 텍스트
  GATE_LENGTH       — 공백 포함 REPLY_MAX_LENGTH 초과
  GATE_NON_KR       — 비허용 스크립트 (가나/한자/키릴/태국/아랍 등)
  GATE_BANNED_WORD  — 금지어 (투자 조언성 표현 — 생성 오작동 신호)
  GATE_IMPERATIVE   — 지시형/안내형 표현 (감사·호응만 정책 위반, C-2)
  GATE_QUESTION     — 답글 내 물음표 (대화 유도 금지, P-2)
  GATE_ECHO         — 원 댓글 미러링 (역할 반전 방지, P-2 — 2026-08-18 축하 사고)
  GATE_FORMAT       — 해시태그/링크/멘션 포함
  GATE_SIMILARITY   — 최근 발행분 또는 동일 배치 내 유사 (자카드, L6 — X 403 방지)
  GATE_TONE         — 걱정·불안 댓글에 웃음·들뜬 표현 (2026-10-28 "공포 지림요 ㅠ → ㅎㅎ 반가워요")
  GATE_RESTATE      — 댓글 복창형 ("B를 선택하셨군요", "그 긴장감이 느껴지시는군요"), DN2026_0098
  GATE_INTERPRET    — 지어낸 해석 활용형 ("편하셨다니 다행이에요"), DN2026_0098
  GATE_GENERIC      — 긴 의견(OPINION)에 댓글 핵심어 없는 범용 AI 답글, DN2026_0098
  GATE_TIME_MISMATCH — 답글 시점 표현(아침·저녁·요일·불금·주말) ↔ 게시 시각(KST) 불일치,
                       DN2026_0103

v2.2.0 (2026-10-28): 길이 상한은 댓글 길이 기준 2단계(reply_max_length).
  CHOICE 의도에서 선택 지지 표현 차단. choose_reply로 AI 후보 → 안전 풀 순서 선택.

v2.4.0 (2026-11-13, DN2026_0103): GATE_TIME_MISMATCH 신설
  (strict이고 context에 _now_kst가 있을 때만).
  실측: 금 16:19 "기분 좋은 아침 시작이네요", 토 01:05 "금요일도 활기차게 보내시길 바라요".
  안전 풀 문구도 같은 게이트를 거친다 (choose_reply → check_reply).

v2.3.0 (2026-11-06, DN2026_0098): GATE_RESTATE·GATE_INTERPRET·GATE_GENERIC 신설
  (14일 실발행 27건 중 복창형 12건 — 문자 bigram GATE_ECHO 실측 0.00~0.46로 전부 통과).
"""

from __future__ import annotations

import logging
import re
import unicodedata

from reply_engine import timectx
from reply_engine.config import (
    BANNED_WORDS,
    REPLY_SIMILARITY_THRESHOLD,
)
from reply_engine.policy import (
    choice_markers,
    intent_for,
    praises_our_content,
    reply_max_length,
)

VERSION = "2.4.0"  # DN2026_0103

logger = logging.getLogger(__name__)

# 비허용 스크립트: 가나(일) / CJK 한자 / 키릴 / 태국 / 아랍
_NON_KR_PATTERN = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\u0400-\u04ff\u0e00-\u0e7f\u0600-\u06ff]")

_FORMAT_PATTERN = re.compile(r"#|https?://|t\.co/|@\w+", re.IGNORECASE)

# Count rendered sequences, including skin tones, ZWJ and paired flag indicators.
_EMOJI_BASE = r"[\U0001F000-\U0001F3FA\U0001F400-\U0001FAFF\u2600-\u27BF]"
_EMOJI_UNIT = _EMOJI_BASE + r"[\ufe0e\ufe0f]?[\U0001F3FB-\U0001F3FF]?"
_EMOJI_PATTERN = re.compile(
    r"[\U0001F1E6-\U0001F1FF]{2}|[0-9#*]\ufe0f?\u20e3|"
    + _EMOJI_UNIT + r"(?:\u200d" + _EMOJI_UNIT + r")*"
)

# C-2 (2026-08-17): 지시형/안내형 표현 감지 — "감사·호응만" 정책 위반 차단.
# dry_run 2차에서 '네, 바로 확인해 보세요!' 통과 사고 재발 방지.
# 인사 관용구("좋은 하루 되세요", "~보내세요")는 매칭되지 않도록 패턴 한정.
# 주의: "행복하세요" 류 기원문도 탈락하나, 무응답은 안전한 방향이므로 허용 손실로 간주.
_IMPERATIVE_PATTERN = re.compile(
    r"(해\s?보세요|해\s?주세요|해\s?주시길|(?<!행복)하세요|하십시오|바랍니다|해야\s?합니다|가보시죠|가시죠)"
)

# P-2 (2026-08-18): 에코 게이트 정규화 — 멘션/비문자(이모지·구두점·공백) 제거 후 비교
_ECHO_STRIP_PATTERN = re.compile(r"@\w+|[^0-9A-Za-z가-힣]")
ECHO_SIMILARITY_THRESHOLD: float = 0.5

# F-1 (2026-08-20): 정중 상투어 — 에코 비교 전 양측에서 제거하는 토큰 (긴 것 우선).
# 순수 감사 댓글("감사합니다^^")에 대한 모범 답글("저야말로 감사합니다")이
# 임계 0.5에 걸려 오탈락한 실측 사례의 해소책. 에코 게이트의 본래 목적은
# 상황어(축하/반갑 등) 미러링 차단이므로, 상투어를 걷어낸 "잔여"끼리만 비교한다.
_COURTESY_TOKENS: tuple[str, ...] = (
    "감사드립니다",
    "감사드려요",
    "감사합니다",
    "감사해요",
    "감사",
    "고맙습니다",
    "고마워요",
    "저야말로",
    "진심으로",
    "정말",
    "너무",
    "항상",
)


# DN2026_0098 A2: 복창 어미 — 상대 문장을 다시 서술하거나 상대 심리를 해석하는 형태.
_RESTATE_ENDING = re.compile(
    r"(?:시|셨)(?:는)?군요|이시(?:군요|네요)|나\s*(?:봐요|보네요|봅니다)"
    r"|느껴지(?:시)?(?:네요|는군요)|선택하셨|고르셨|골라\s*주셨|짚어\s*주셨|짚으셨|겠죠"
)
# DN2026_0098 A5: 지어낸 해석 활용형 ("보이기 편하셨다니 다행이에요", "들어온다니 뿌듯하네요")
# 과 자기 평가("네, 현명한 판단이었죠"). '(셨|ㄴ|는)다니 + 감정어'와 '셨다니'만 대상이며
# '도움이 됐다니 다행이에요'(독자 감사에 대한 정상 호응)는 대상이 아니다.
_INTERPRET_PATTERN = re.compile(r"셨다니|(?:현명|탁월|훌륭)한\s*(?:판단|선택|결정)")
_INTERPRET_DANI = re.compile(
    r"([가-힣])다니\s*(?:정말\s*|너무\s*)?(?:다행|뿌듯|기쁘|기뻐|기쁜|반갑|반가)"
)
_COURTESY_CUE = re.compile(r"감사|고마|고맙")


def _has_nieun_batchim(ch: str) -> bool:
    code = ord(ch) - 0xAC00
    return 0 <= code < 11172 and code % 28 == 4  # 종성 ㄴ


def _is_interpretation(text: str) -> bool:
    if _INTERPRET_PATTERN.search(text):
        return True
    return any(m.group(1) == "는" or _has_nieun_batchim(m.group(1))
               for m in _INTERPRET_DANI.finditer(text))
# DN2026_0098 A2: 핵심어 비교에서 제외하는 상투·기능어 앞 2글자.
_KEYWORD_STOP: frozenset[str] = frozenset({
    "감사", "고마", "고맙", "저야", "진심", "정말", "너무", "항상", "저도", "저는", "제가",
    "그쵸", "그러", "그렇", "그런", "이런", "좋은", "좋아", "좋네", "좋습", "보내", "되세",
    "댓글", "말씀", "읽어", "봐주", "있어", "있네", "있습", "같아", "같이", "함께", "진짜",
    "완전", "조금", "많이", "계속", "이제", "아직", "그냥", "네요", "해요", "합니", "하네",
})
_KEYWORD_TOKEN = re.compile(r"[가-힣]+|[A-Za-z0-9]+")
RESTATE_KEYWORD_RATIO: float = 0.6
_RESTATE_RATIO_EXEMPT = frozenset({"GREETING", "THANKS", "CONGRATULATIONS"})


def _keywords(text: str) -> set[str]:
    """핵심어: 2자 이상 한글 어절의 앞 2글자, 또는 영문/숫자 토큰(소문자) (DN2026_0098 A2)."""
    keys: set[str] = set()
    for token in _KEYWORD_TOKEN.findall(re.sub(r"@\w+", " ", text or "")):
        if re.fullmatch(r"[가-힣]+", token):
            if len(token) >= 2 and token[:2] not in _KEYWORD_STOP:
                keys.add(token[:2])
        else:
            keys.add(token.lower())
    return keys


def _choice_marker_in_reply(text: str, markers: set[str]) -> bool:
    """댓글의 선택 표지(문자/번호/위·아래/한 표)를 답글이 되풀이하는지 (DN2026_0098 A3)."""
    for marker in markers:
        if marker == "한표":
            pattern = r"한\s*표"
        elif marker == "위":
            pattern = r"위\s*(?:쪽|꺼|거|것)"
        elif marker == "아래":
            pattern = r"아래"
        elif marker.endswith("번"):
            pattern = rf"(?<!\d){marker[:-1]}\s*번"
        else:
            pattern = rf"(?<![A-Za-z]){re.escape(marker)}(?![A-Za-z])"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def restate_reason(text: str, comment_text: str, intent: str) -> str | None:
    """복창·해석 판정 (DN2026_0098 A2/A3/A5). 탈락 사유 또는 None."""
    if _is_interpretation(text):
        return "GATE_INTERPRET"
    if not comment_text:
        return None
    if intent == "CHOICE" and _choice_marker_in_reply(text, choice_markers(comment_text)):
        return "GATE_RESTATE"
    # DN2026_0098 A3 보완(G4 실측 재생): 선택 응답은 참여 감사만 허용하므로 핵심어 겹침과
    # 무관하게 상대 심리·선택 해석 어미("짝을 찾는 마음이 느껴지네요")를 차단한다.
    if intent == "CHOICE" and _RESTATE_ENDING.search(text):
        return "GATE_RESTATE"
    reply_keys = _keywords(text)
    shared = reply_keys & _keywords(comment_text)
    if _RESTATE_ENDING.search(text) and shared:
        return "GATE_RESTATE"
    # 감사 표현이 있는 답글("자료 공유 감사합니다")은 상대 말에 대한 반응이므로 비율 검사 제외.
    if (intent not in _RESTATE_RATIO_EXEMPT and not _COURTESY_CUE.search(text)
            and len(reply_keys) >= 2
            and len(shared) / len(reply_keys) >= RESTATE_KEYWORD_RATIO):
        return "GATE_RESTATE"
    return None


def generic_reason(text: str, comment_text: str, intent: str) -> str | None:
    """긴 의견(OPINION) AI 답글은 댓글 핵심어 1개 이상 필수 (DN2026_0098 A6).

    안전 풀 문구에는 적용하지 않는다 (choose_reply가 AI 후보에만 호출).
    """
    if intent == "OPINION" and comment_text and not (_keywords(text) & _keywords(comment_text)):
        return "GATE_GENERIC"
    return None


def _normalize_for_echo(text: str) -> str:
    return _ECHO_STRIP_PATTERN.sub("", text or "")


def _strip_courtesy(normalized: str) -> str:
    """정규화 텍스트에서 정중 상투어를 제거한 잔여(상황어) 반환 (F-1)."""
    for token in _COURTESY_TOKENS:
        normalized = normalized.replace(token, "")
    return normalized


def _char_bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def jaccard_similarity(a: str, b: str) -> float:
    """문자 bigram 자카드 유사도 (0.0~1.0)."""
    set_a, set_b = _char_bigrams(a), _char_bigrams(b)
    if not set_a or not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def check_reply(
    text: str,
    recent_texts: list[str],
    comment_text: str = "",
    *,
    context: dict | None = None,
    strict: bool = False,
) -> tuple[bool, str | None]:
    """
    단건 게이트. recent_texts에는 최근 발행분 + 동일 배치 내 선행 통과분을 함께 전달.
    comment_text 전달 시 원 댓글 미러링(GATE_ECHO)까지 검증한다 (P-2).
    strict=True (DN2026_0098, X 전용): GATE_RESTATE·GATE_INTERPRET·CHOICE 확장 판정 적용.
    strict=False는 기준 커밋 5c5246d와 같은 판정이다 (FB 등).
    반환: (통과 여부, 실패 코드 | None)
    """
    if not text or not text.strip():
        return False, "GATE_EMPTY"

    text = text.strip()

    if len(text) > reply_max_length(comment_text):
        return False, "GATE_LENGTH"

    emoji_count = len(_EMOJI_PATTERN.findall(text))
    if emoji_count > 1:
        return False, "GATE_EMOJI"
    if "\n" in text or "\r" in text:
        return False, "GATE_FORMAT"
    if any(unicodedata.category(ch) == "Cf" and ch not in {"\u200d"} for ch in text):
        return False, "GATE_FORMAT"

    if _NON_KR_PATTERN.search(text):
        return False, "GATE_NON_KR"

    for word in BANNED_WORDS:
        if word in text:
            return False, "GATE_BANNED_WORD"

    if "?" in text or "？" in text:
        return False, "GATE_QUESTION"

    if re.search(
        r"(?:수익|상승|하락).*(?:확실|보장)|(?:같이|함께).*(?:가보|가시|사시)|확인했습니다|수정했습니다|저도.*(?:놀랐|경험)",
        text,
    ):
        return False, "GATE_UNSUPPORTED"

    if _IMPERATIVE_PATTERN.search(text):
        return False, "GATE_IMPERATIVE"


    # Content validity is separate from lexical echo/similarity. No market hype
    # may be amplified merely because the generated wording differs from input.
    if re.search(r"돈\s*복사|확정\s*수익|떡상|가즈아", text):
        return False, "GATE_MARKET_HYPE"
    intent = intent_for(comment_text, "POSITIVE", (context or {}).get("parent_text", ""),
                        extended=strict)  # DN2026_0098: X만 확장 의도 판정
    # Congratulatory replies must acknowledge congratulations, not give them
    # back to the reader. Unknown roles use the same conservative direction.
    if intent == "CONGRATULATIONS" and re.search(r"축하|경하", text):
        outward = re.search(r"(?:축하|경하)(?:드립니다|드려요|합니다|해요|해드립니다)", text)
        if outward or not re.search(
            r"(?:축하|경하).*?(?:주셔서|주신|말씀).*?(?:감사|고맙|고마워)", text
        ):
            return False, "GATE_ROLE_REVERSAL"
    # 실측(10-12): "100화 축하드립니다" → "ㅎㅎ 반가워요". 축하를 받았으면 감사로 응답한다.
    if intent == "CONGRATULATIONS" and not re.search(r"축하|감사|고맙|고마워", text):
        return False, "GATE_INTENT"
    if context and context.get("foreign_thread"):
        account = str(context.get("_account_user_id") or "")
        if not account or str(context.get("parent_author_id") or "") != account:
            return False, "GATE_CONTEXT_UNVERIFIED"
    if intent == "MARKET_HYPE" and re.search(
        r"기대|응원|성투|대박|수익|오르|상승|달려|함께|같이|동의|같은\s*마음", text
    ):
        return False, "GATE_MARKET_HYPE"
    if intent in {"REACTION", "LAUGH"} and re.search(r"의견|분석|설명|정리|통찰", text):
        return False, "GATE_INTENT"
    if intent == "WORRY" and re.search(
        r"[ㅋㅎ]{2,}|반가|반갑|기대|화이팅|파이팅|축하|신나|재미|즐거|[😄😆😁🤣😂🚀🔥🎉]", text
    ):
        return False, "GATE_TONE"
    # 덕담·기원("맛있게 드세요")을 상대가 이미 겪은 일로 바꾸는 오독 차단 (10-28 live 실측).
    if intent == "GREETING" and re.search(
        r"(?:되|드|하|보내|먹|드시|지내)셨다니|(?:드|되|보내|지내)셨(?:군요|네요)"
        r"|(?:먹었|쉬었|보냈|즐겼|잘\s*잤|드셨|쉬셨|보내셨|즐기셨|주무셨)"
        r"(?:다니|군요|네요|어요|겠습니다)", text
    ):
        return False, "GATE_ROLE_REVERSAL"
    # A lexically safe acknowledgement can still discard the explicit social intent.
    # Exact generic phrases only: avoid guessing semantic relevance from token overlap.
    generic = re.sub(r"[^가-힣A-Za-z0-9]", "", text)
    if intent in {"GREETING", "WORRY", "OPINION", "CHOICE"} and generic in {
        "댓글잘봤어요", "댓글잘보았습니다", "반응남겨주셔서고맙습니다",
        "댓글남겨주셔서감사해요", "반가워요", "반갑습니다",
    }:
        return False, "GATE_INTENT"
    if intent == "CHOICE" and re.search(
        r"좋은\s*선택|현명|정답|탁월|맞는\s*선택|잘\s*고르|동의|저도\s*[ABab]|[ABab]\s*(?:가|이)\s*(?:낫|좋|맞)",
        text,
    ):
        return False, "GATE_INTENT"
    # DN2026_0098 A3 (strict만): 선택지 A~D 확장, 선택 응원·결과 예고("결과가 궁금하네요")도 평가.
    if strict and intent == "CHOICE" and re.search(
        r"응원|저도\s*[A-Da-d]|[A-Da-d]\s*(?:가|이)\s*(?:낫|좋|맞)"
        r"|결과\S*\s*(?:가\s*)?(?:궁금|기대)",
        text,
    ):
        return False, "GATE_INTENT"
    if praises_our_content(comment_text) and re.search(
        r"(?:지표|자료|정리|글|설명|분석|콘텐츠|정보)(?:을|를|에)?\s*"
        r"(?:(?:정리|공유|제공|분석|설명)\s*)?"
        r"(?:해\s*주셔서|해\s*주신\s*(?:것|점)|해\s*주심에|해줘서|주셔서)?\s*"
        r"(?:감사|고맙|고마워)", text
    ):
        return False, "GATE_ROLE_REVERSAL"

    # DN2026_0098 A2/A3/A5: 복창·해석 활용형. 기존 GATE_ECHO(문자 bigram)는 아래에서 그대로 유지.
    restate = restate_reason(text, comment_text, intent) if strict else None
    if restate:
        # DN2026_0098 STD-OBS-07: 공개 Actions 로그 — 타인 댓글 원문 미출력(길이만).
        logger.info(f"[Gate] {restate} 탈락: '{text}' (댓글 {len(comment_text or '')}자)")
        return False, restate

    if comment_text:
        # F-1: 상투어를 걷어낸 상황어 잔여끼리 비교.
        # 댓글 잔여가 비면(순수 감사 댓글) 미러링할 상황어가 없으므로 에코 검사 생략.
        residual_reply = _strip_courtesy(_normalize_for_echo(text))
        residual_comment = _strip_courtesy(_normalize_for_echo(comment_text))
        if residual_reply and residual_comment:
            echo = jaccard_similarity(residual_reply, residual_comment)
            if echo >= ECHO_SIMILARITY_THRESHOLD:
                logger.info(f"[Gate] 에코 탈락 ({echo:.2f}): '{text}' ≈ 댓글 '{comment_text}'")
                return False, "GATE_ECHO"

    # DN2026_0103: 의미 게이트(역할·복창·에코) 다음에 둔다 — 탈락 사유는 의미 오류를 우선 기록.
    # 시점 표현 ↔ 게시 시각 불일치 (X strict + _now_kst 주입 시에만).
    if strict and context and context.get("_now_kst") is not None:
        time_reason = timectx.mismatch(text, context["_now_kst"])
        if time_reason:
            logger.info(f"[Gate] GATE_TIME_MISMATCH 탈락: '{text}' ({time_reason})")
            return False, "GATE_TIME_MISMATCH"

    if _FORMAT_PATTERN.search(text):
        return False, "GATE_FORMAT"

    for prev in recent_texts:
        similarity = jaccard_similarity(text, prev)
        if similarity >= REPLY_SIMILARITY_THRESHOLD:
            logger.info(f"[Gate] 유사도 탈락 ({similarity:.2f}): '{text}' ≈ '{prev}'")
            return False, "GATE_SIMILARITY"

    return True, None


def choose_reply(
    drafts: list[str] | tuple[str, ...],
    fallbacks: tuple[str, ...],
    recent_texts: list[str],
    comment_text: str = "",
    *,
    context: dict | None = None,
    strict: bool = False,
) -> tuple[str | None, str, str | None]:
    """AI 후보를 순서대로 검사하고, 전부 탈락하면 의도별 안전 풀을 검사한다.

    반환: (선택 문구 | None, source, 첫 후보의 탈락 사유 | None)
      source — AI(첫 후보) / AI_ALT(2번째 이후 후보) / TEMPLATE_FALLBACK / NONE
    전부 탈락하면 (None, "NONE", 첫 탈락 사유)로 무응답 처리하게 한다.
    """
    first_reason: str | None = None
    intent = intent_for(comment_text, "POSITIVE", (context or {}).get("parent_text", ""),
                        extended=strict)
    for index, draft in enumerate(d for d in drafts if isinstance(d, str)):
        ok, reason = check_reply(draft, recent_texts, comment_text, context=context,
                                 strict=strict)
        if ok and strict:
            # DN2026_0098 A6 (strict만): AI 후보만 범용 문구 검사 (안전 풀은 의도별 검수 문구).
            reason = generic_reason(draft.strip(), comment_text, intent)
            ok = reason is None
        if ok:
            return draft.strip(), ("AI" if index == 0 else "AI_ALT"), first_reason
        if first_reason is None:
            first_reason = reason
    if first_reason is None:
        first_reason = "GATE_EMPTY"
    for fallback in fallbacks:
        if check_reply(fallback, recent_texts, comment_text, context=context, strict=strict)[0]:
            return fallback, "TEMPLATE_FALLBACK", first_reason
    return None, "NONE", first_reason
