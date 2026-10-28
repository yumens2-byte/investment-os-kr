"""Reply decisions, bounded recovery metadata and intent-specific safe responses."""

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

POLICY_VERSION = "2.2.0"
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
        "요즘 변동이 커서 마음 쓰이시죠",
        "흔들리는 장이라 신경 쓰이실 만해요",
        "숫자가 크게 움직여서 긴장되는 날이네요",
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
_CHOICE_LETTER = re.compile(r"(?<![A-Za-z])[ABab](?![A-Za-z])")
_CHOICE_WORDS = re.compile(
    r"선택|고르|골라|고를|할래|갈래|픽|[ABab]\s*(?:요|로|를|가|!|입니다|이요|쪽)"
)


def is_choice_comment(text: str, parent_text: str = "") -> bool:
    """A/B 선택형 원글에 대한 선택 표명. 원글 표기 또는 선택 어휘가 함께 있어야 한다."""
    body = normalize_comment(text)
    if not _CHOICE_LETTER.search(body):
        return False
    parent = parent_text or ""
    parent_is_vote = bool(re.search(r"A\s*[)\.:]", parent) and re.search(r"B\s*[)\.:]", parent))
    return parent_is_vote or bool(_CHOICE_WORDS.search(body))


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


def intent_for(text: str, label: str, parent_text: str = "") -> str:
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
    if _is_worry(body):
        return "WORRY"
    if _GREETING_PATTERN.search(normalize_comment(body)):
        return "GREETING"
    if is_choice_comment(body, parent_text):
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
    for key in ("response_source", "draft_gate_reason", "intent"):
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
