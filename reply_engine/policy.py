"""Reply decisions, bounded recovery metadata and intent-specific safe responses.

v2.4.0 (2026-11-13, DN2026_0103): X 확장 경로(extended=True)에서 시점 인사
  ('금모닝', '불금', '토요모닝')를 GREETING으로 판정. 실측 "드디어 불타늘 금모닝!!",
  "불금 달려보시쥬ㅎ"가 ACK로 떨어져 범용 문구로 대체되던 문제. FB(extended=False) 불변.
v2.3.0 (2026-11-06, DN2026_0098): 선택 표지 A~D·1~4번·위/아래·한 표 인식, CHOICE를 WORRY보다
  먼저 판정, 재시도 저장 초안 유지 판정(retry_keeps_stored_text), metadata에 generator_version.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta

from reply_engine.config import (
    REPLY_LONG_COMMENT_CHARS,
    REPLY_MAX_LENGTH,
    REPLY_MAX_LENGTH_LONG,
)

POLICY_VERSION = "2.4.0"  # DN2026_0103
DEFER_REASONS = frozenset(
    {
        "RECEIVED",
        "RUN_CAP",
        "DAILY_CAP",
        "AUTHOR_CAP",
        "CONV_CAP",
        "AUTHOR_CAP_RUN",
        "CONV_CAP_RUN",
        "FOREIGN_THREAD_CAP",
        "BUDGET_WRITE",
        "THREAD_UNVERIFIED",
        "CLASSIFIER_UNAVAILABLE",
        "PUBLISH_RETRYABLE",
    }
)
BLOCKED_STATES = frozenset(
    {
        "PUBLISHING",
        "PUBLISH_UNKNOWN",
        "DB_CONFIRM_FAIL",
        "PUBLISH_FAIL",
        "PUBLISH_REJECTED",
        "PUBLISH_EXHAUSTED",
        "SPEND_CAP",
        "EXPIRED_CAP",
        "EXPIRED_DEFERRED",
        "EXPIRED_RETRY",
        "EXPIRED_BEFORE_SEND",
    }
)
REACTION_PATTERN = re.compile(r"^(?:[👍🙏❤🔥💯👏🙌😊😄😆🙂❤️]|[\ufe0f\u200d]|[!~. ])+$")
SAFE_POOLS = {
    "REACTION": (
        "반응 남겨주셔서 고마워요 👍",
        "반가운 반응이네요 😊",
        "ㅎㅎ 반갑습니다",
        "댓글 잘 봤어요",
    ),
    "MARKET_HYPE": ("댓글 잘 봤어요", "반응 남겨주셔서 고맙습니다", "관심 가져주셔서 감사해요"),
    "THANKS": (
        "저야말로 감사합니다 😊",
        "말씀 고맙습니다",
        "저도 감사드려요",
        "따뜻한 말씀 잘 받았어요",
        "댓글 반갑게 읽었어요",
        "한마디 남겨주셔서 고마워요",
    ),
    "PRAISE": ("좋게 봐주셔서 감사해요 😊", "읽어주셔서 고맙습니다", "댓글 남겨주셔서 감사해요"),
    "CONGRATULATIONS": (
        "축하해 주셔서 감사합니다 😊", "따뜻한 축하 말씀 고맙습니다", "함께 기뻐해 주셔서 감사해요",
    ),
    "LAUGH": ("ㅎㅎ 😄", "ㅋㅋ 반가워요", "ㅎㅎ 댓글 잘 봤어요"),
    "MARKET": ("관심 가는 흐름이네요", "변화가 눈에 들어오네요", "같은 부분을 보셨군요 🙂"),
    "ACK": ("ㅎㅎ 반가워요", "댓글 잘 봤어요 🙂", "반응 남겨주셔서 고맙습니다"),
    # 2026-10-28: 감정·상황 의도. 실측 오응답 "공포 지림요 ㅠ → ㅎㅎ 반가워요",
    # "좋은 하루 보내세요 → 댓글 잘 봤어요", "저는 B요 → 선택 감사합니다".
    "WORRY": (
        "마음 쓰이신다는 말씀 잘 읽었어요",
        "걱정되는 마음을 나눠주셔서 고마워요",
        "쉽지 않은 마음이 전해지네요",
        "불안한 마음 충분히 이해돼요",
    ),
    "GREETING": (
        "따뜻한 인사 고마워요, 편안한 시간 되세요 🙂",
        "인사 남겨주셔서 감사해요 😊",
        "덕분에 기분 좋아졌어요, 즐거운 시간 되세요",
        "고마워요, 오늘도 무탈한 하루 되세요",
    ),
    "CHOICE": (
        "고르신 쪽 잘 봤어요 🙂",
        "고민해서 골라주신 답 반갑게 읽었어요",
        "참여해 주셔서 고마워요 ㅎㅎ",
        "어느 쪽이든 고민되는 선택이죠",
    ),
    "OPINION": (
        "생각 자세히 적어주셔서 잘 읽었어요 😊",
        "꼼꼼하게 풀어주신 의견 고맙습니다",
        "정성스러운 의견 잘 읽었어요",
        "길게 적어주신 생각 반갑게 봤어요 🙂",
    ),
}

# 걱정·불안 신호. 강한 신호는 단독으로, 약한 신호('ㅠ', 어렵다 등)는 긍정 표현이
# 없을 때만 인정한다 ("감사합니다ㅠㅠ"는 감동이지 걱정이 아니다).
_WORRY_STRONG = re.compile(r"무섭|무서[워운]|공포|불안|걱정|쫄|버텨|버티|비상|겁나|멘붕|우울")
_WORRY_WEAK = re.compile(r"어렵|어려[워운]|힘들|힘드|[ㅠㅜ]")
_WORRY_NEGATED = re.compile(
    r"(?:무섭|무서|불안|걱정|어렵|어려|힘들|힘드)\S*\s*(?:않|없|안\s|마세요|말아|ㄴㄴ)"
    r"|(?:안|별로\s*안)\s*(?:무섭|어렵|힘들|불안)"
)
_WORRY_INDEX_NAMES = re.compile(r"공포\s*(?:탐욕|지수|&|와\s*탐욕)|공탐")
_POSITIVE_CUES = re.compile(r"감사|고맙|고마워|좋|최고|감동|멋|대박|축하|사랑|행복|유익|ㅋ|ㅎ|👍")


def _is_worry(body: str) -> bool:
    text = _WORRY_INDEX_NAMES.sub(" ", body)
    if _WORRY_NEGATED.search(text):
        return False
    if _WORRY_STRONG.search(text):
        return True
    return bool(_WORRY_WEAK.search(text)) and not _POSITIVE_CUES.search(text)
_GREETING_PATTERN = re.compile(
    r"(?:좋은|행복한|편안한|즐거운|따뜻한|멋진|활기찬)\s*(?:하루|밤|아침|저녁|주말|휴일|한\s*주|시간|명절)"
    r"|(?:하루|밤|아침|저녁|주말|휴일|연휴)\s*(?:\S+\s*){0,2}(?:보내세요|되세요|보내시길|되시길)"
    r"|^(?:좋은\s*아침|굿모닝|굿밤|안녕하세요)"
    # 2026-10-28 live 실측: "저녁 맛있게 드세용" → 덕담을 상대의 경험으로 오독.
    r"|(?:맛있게|맛나게|든든하게|맛난\s*거)\s*(?:\S+\s*){0,1}(?:드세|드시|먹어|챙겨)"
    r"|(?:식사|저녁|점심|아침)\s*(?:\S+\s*){0,2}(?:드세|챙기세|하세)"
    r"|맛저|맛점|굿나잇|좋은\s*꿈|푹\s*쉬세|편히\s*쉬세"
)
# DN2026_0103 (extended=True, X 전용): 시점 인사. '퇴근·출근'은 감상 댓글과 섞여 제외한다.
_TIME_GREETING_X = re.compile(r"모닝|불금|불토|불타는\s*금|불타늘\s*금")
# 기준(5c5246d) 정규식 — extended=False(FB 등) 경로에서 그대로 사용.
_CHOICE_LETTER_BASE = re.compile(r"(?<![A-Za-z])[ABab](?![A-Za-z])")
_CHOICE_WORDS_BASE = re.compile(
    r"선택|고르|골라|고를|할래|갈래|픽|[ABab]\s*(?:요|로|를|가|!|입니다|이요|쪽)"
)
# DN2026_0098 A3: A~D 확장 (기존 A/B만 → "C로 골라볼게요" 미인식 실측). extended=True(X) 전용.
_CHOICE_LETTER = re.compile(r"(?<![A-Za-z])[A-Da-d](?![A-Za-z])")
_CHOICE_WORDS = re.compile(
    r"선택|고르|골라|고를|할래|갈래|픽|한\s*표"
    r"|(?<![A-Za-z])[A-Da-d]\s*(?:요|로|를|가|!|입니다|이요|쪽|번)"
)
# DN2026_0098 A3: 번호(1~4번)·위/아래(쪽)·'한 표'는 단독으로도 선택 표지다.
# '위로'(위안)·'위험'·'2번이나'(횟수) 오인을 피하려고 뒤따르는 형태를 한정한다.
_CHOICE_NUMBER = re.compile(
    r"(?<!\d)([1-4])\s*번(?=\s*(?:요|이요|으로|!|선택|골|고르|고를|할래|갈래|한\s*표|입니다|"
    r"이에요|이(?!나|라도)|을|쪽|$))"
)
_CHOICE_POSITION = re.compile(r"(위|아래)\s*(?=쪽|꺼|거요|것|한\s*표|에\s*한\s*표)")
_CHOICE_VOTE = re.compile(r"한\s*표")


def choice_markers(text: str) -> set[str]:
    """댓글의 선택 표지 집합 (DN2026_0098 A3). 예: {'C'}, {'2번'}, {'위'}, {'한표'}."""
    body = normalize_comment(text)
    markers = {m.group(0).upper() for m in _CHOICE_LETTER.finditer(body)}
    markers.update(f"{m.group(1)}번" for m in _CHOICE_NUMBER.finditer(body))
    markers.update(m.group(1) for m in _CHOICE_POSITION.finditer(body))
    if _CHOICE_VOTE.search(body):
        markers.add("한표")
    return markers


def is_choice_comment(text: str, parent_text: str = "", *, extended: bool = False) -> bool:
    """선택형 원글에 대한 선택 표명.

    기본(extended=False): 기준 커밋과 동일 — A/B 문자 + 원글 표기 또는 선택 어휘.
    extended=True (DN2026_0098 A3, X 전용): 문자 A~D, 번호·위/아래·'한 표' 표지는 단독 인정.
    """
    body = normalize_comment(text)
    if extended and (_CHOICE_NUMBER.search(body) or _CHOICE_POSITION.search(body)
                     or _CHOICE_VOTE.search(body)):
        return True
    letter, words = ((_CHOICE_LETTER, _CHOICE_WORDS) if extended
                     else (_CHOICE_LETTER_BASE, _CHOICE_WORDS_BASE))
    if not letter.search(body):
        return False
    parent = parent_text or ""
    parent_is_vote = bool(re.search(r"A\s*[)\.:]", parent) and re.search(r"B\s*[)\.:]", parent))
    return parent_is_vote or bool(words.search(body))


def is_long_comment(text: str) -> bool:
    return len(normalize_comment(text)) >= REPLY_LONG_COMMENT_CHARS


def reply_max_length(comment_text: str) -> int:
    """긴 의견 댓글에만 넓은 상한을 적용한다. 댓글 미지정 시 기존 상한."""
    if comment_text and is_long_comment(comment_text):
        return REPLY_MAX_LENGTH_LONG
    return REPLY_MAX_LENGTH


class BatchResult(dict):
    """Mapping-compatible result with actual gateway usage and incomplete IDs."""

    def __init__(self):
        super().__init__()
        self.api_calls = 0
        self.usage = []
        self.unavailable_ids = set()
        self.sources = {}
        self.candidates = {}  # id → AI 후보 목록 (생성기 전용, 순서 보존)

    def record_usage(self, result: dict) -> None:
        # Mock/legacy gateways without telemetry count logical calls, explicitly marked.
        calls = int(result.get("api_calls", 1))
        self.api_calls += calls
        self.usage.append(
            {
                "api_calls": calls,
                "measured": "api_calls" in result,
                "model": result.get("model", ""),
                "paid": bool(result.get("paid")),
                "key_used": result.get("key_used", ""),
                "attempts": result.get("attempts", []),
                "tokens": result.get("usage", {}),
            }
        )


def intent_for(text: str, label: str, parent_text: str = "", *, extended: bool = False) -> str:
    """의도 판정. extended=True(X 전용, DN2026_0098 A3)만 확장 선택 인식·CHOICE 우선 순서."""
    body = re.sub(r"@\w+", "", text or "").strip()
    if re.search(r"돈\s*복사|가즈아|떡상|수익\s*보장|확정\s*수익", body):
        return "MARKET_HYPE"
    if REACTION_PATTERN.fullmatch(body) and any(ord(c) > 127 for c in body):
        return "REACTION"
    if re.fullmatch(r"[ㅋㅎ!~. ]{2,}", body):
        return "LAUGH"
    if re.search(r"축하|경하", body) and not re.search(
        r"(?:축하|경하).*?(?:주셔서|주신|말씀).*?(?:감사|고맙|고마워)", body
    ):
        return "CONGRATULATIONS"
    # DN2026_0098 A3 (extended만): 선택 표지가 있으면 걱정 어휘가 섞여도 선택 표명이다
    # ("B 고를래요 저축 걱정…"이 WORRY로 분류되던 실측).
    if extended and is_choice_comment(body, parent_text, extended=True):
        return "CHOICE"
    if _is_worry(body):
        return "WORRY"
    if _GREETING_PATTERN.search(normalize_comment(body)) or (
        extended and _TIME_GREETING_X.search(body)  # DN2026_0103
    ):
        return "GREETING"
    if not extended and is_choice_comment(body, parent_text):
        return "CHOICE"
    if any(word in body for word in ("감사", "고맙", "고마워")):
        return "THANKS"
    if reader_contributes_content(body):
        return "ACK"
    if praises_our_content(body):
        return "PRAISE"
    if label == "POSITIVE" and (
        any(word in body for word in ("잘 봤", "잘봤", "유익", "즐감"))
        or re.search(r"(?:잘|재미\s*있게)\s*보(?:고|았|겠)|참고\s*잘", body)
        or re.search(r"(?:자료|정리|글|설명|분석|콘텐츠).*(?:좋|멋)", body)
    ):
        return "PRAISE"
    if is_long_comment(body):
        return "OPINION"
    # Never infer agreement with a market prediction from an ambiguous reaction.
    return "ACK"


def praises_our_content(text: str) -> bool:
    """Recognize explicit benefit from our content, not the reader's contribution."""
    body = normalize_comment(text)
    if reader_contributes_content(body):
        return False
    if re.search(r"도움(?:이|은|도)?\s*(?:안|없)|유익하지\s*않", body):
        return False
    content = r"(?:지표|자료|정리|글|설명|분석|콘텐츠|정보)"
    benefit = r"(?:덕분|도움|유익|한눈에|이해.*(?:쉽|잘)|파악.*수\s*있)"
    return bool(re.search(content + r".*" + benefit, body))


def normalize_comment(text: str) -> str:
    """Preserve words while folding input line breaks and spacing."""
    return re.sub(r"\s+", " ", re.sub(r"@\w+", "", text or "")).strip()


def reader_contributes_content(text: str) -> bool:
    """Require explicit first-person subject and affirmative sharing action."""
    body = normalize_comment(text)
    return bool(re.search(
        r"(?:제가|저도|저는|내가|저희가)[^.!?。！？]{0,80}?"
        r"(?:공유|첨부)(?:합니다|해요|할게요|했어요|했습니다|드립니다)", body
    ))


def decode_metadata(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) and data.get("reply_policy") else {}
    except (TypeError, ValueError):
        return {}


def encode_metadata(tweet: dict, *, publish_attempt: bool = False) -> str:
    previous = dict(tweet.get("_metadata") or {})
    created = tweet.get("created_at")
    previous.update(
        {
            "reply_policy": POLICY_VERSION,
            "original_created_at": created.isoformat()
            if isinstance(created, datetime)
            else created,
            "comment_text": tweet.get("text", ""),
            "parent_text": tweet.get("parent_text", ""),
            "parent_id": tweet.get("parent_id", ""),
            "parent_author_id": tweet.get("parent_author_id", ""),
            "root_author_id": tweet.get("root_author_id", ""),
            "foreign_thread": bool(tweet.get("foreign_thread")),
            "in_reply_to_user_id": tweet.get("in_reply_to_user_id", ""),
            "account_user_id": tweet.get("_account_user_id", ""),
            "publish_attempts": int(previous.get("publish_attempts", 0)) + int(publish_attempt),
            "run_id": tweet.get("_run_id")
            or previous.get("run_id")
            or os.getenv("GITHUB_RUN_ID", ""),
            "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", ""),
            "commit_sha": os.getenv("REPLY_RUNTIME_SHA", os.getenv("GITHUB_SHA", "")),
            "first_seen_at": previous.get("first_seen_at") or tweet.get("_first_seen_at"),
            "user_snapshot": previous.get("user_snapshot") or tweet.get("_user_snapshot"),
            "last_decision_at": datetime.now(UTC).isoformat(),
        }
    )
    # 생성 근거 (2026-10-28). 호출부가 넘긴 경우에만 갱신해 이전 근거를 보존한다.
    # DN2026_0098 A4: generator_version — 초안을 만든 생성기 버전 (전달 시에만 갱신, 기존 키 호환).
    for key in ("response_source", "draft_gate_reason", "intent", "generator_version"):
        if f"_{key}" in tweet:
            previous[key] = tweet[f"_{key}"]
    if publish_attempt:
        previous["next_attempt_at"] = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    snapshot = previous.get("user_snapshot")
    if isinstance(snapshot, dict):
        previous["user_snapshot"] = {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in snapshot.items()
            if k in {"username", "created_at", "followers"}
        }
    return json.dumps(previous, ensure_ascii=False)


# DN2026_0098 A4: 발행 요청이 나갔을 수 있는 상태. 이 경우 저장 문구를 그대로 써야 한다
# (STD-PUB-03 무재시도·STD-PUB-06 동일 문구). PUBLISH_RETRYABLE(429)은 요청 이력이 있다.
_PUBLISH_TOUCHED_STATES = BLOCKED_STATES | {"PUBLISH_RETRYABLE"}


def retry_keeps_stored_text(metadata: dict | None, skip_reason: str | None = None) -> bool:
    """재시도 건의 저장 초안을 유지해야 하면 True (DN2026_0098 A4).

    발행 시도 0회(또는 키 없음)이고 결과 불명류 상태가 아니면 False → 새로 생성한 답글을 쓴다.
    판단 근거가 불완전(값 파싱 실패)하면 보수적으로 유지(True)한다.
    """
    meta = metadata or {}
    if str(skip_reason or "") in _PUBLISH_TOUCHED_STATES:
        return True
    if any(meta.get(k) for k in ("attempt_id", "claimed_at", "response_text_sha256")):
        return True
    try:
        return int(meta.get("publish_attempts") or 0) > 0
    except (TypeError, ValueError):
        return True


def classify_publish_error(error: str | None) -> str:
    """Only explicit rejection responses can be retried; 5xx/timeouts stay unknown."""
    value = str(error or "").lower()
    if any(word in value for word in ("spend cap", "monthly spend", "usage cap")):
        return "SPEND_CAP"
    if re.search(r"\b429\b|too many requests", value):
        return "PUBLISH_RETRYABLE"
    if re.search(r"\b(400|401|403|404|422)\b", value):
        return "PUBLISH_REJECTED"
    return "PUBLISH_UNKNOWN"
