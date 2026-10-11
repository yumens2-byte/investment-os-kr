"""Reply decisions, bounded recovery metadata and intent-specific safe responses."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta

POLICY_VERSION = "2.1.0"
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
    "LAUGH": ("ㅎㅎ 😄", "ㅋㅋ 반가워요", "ㅎㅎ 댓글 잘 봤어요"),
    "MARKET": ("관심 가는 흐름이네요", "변화가 눈에 들어오네요", "같은 부분을 보셨군요 🙂"),
    "ACK": ("ㅎㅎ 반가워요", "댓글 잘 봤어요 🙂", "반응 남겨주셔서 고맙습니다"),
}


class BatchResult(dict):
    """Mapping-compatible result with actual gateway usage and incomplete IDs."""

    def __init__(self):
        super().__init__()
        self.api_calls = 0
        self.usage = []
        self.unavailable_ids = set()
        self.sources = {}

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


def intent_for(text: str, label: str) -> str:
    body = re.sub(r"@\w+", "", text or "").strip()
    if re.search(r"돈\s*복사|가즈아|떡상|수익\s*보장|확정\s*수익", body):
        return "MARKET_HYPE"
    if REACTION_PATTERN.fullmatch(body) and any(ord(c) > 127 for c in body):
        return "REACTION"
    if re.fullmatch(r"[ㅋㅎ!~. ]{2,}", body):
        return "LAUGH"
    if any(word in body for word in ("감사", "고맙", "고마워")):
        return "THANKS"
    if praises_our_content(body):
        return "PRAISE"
    if label == "POSITIVE" and (
        any(word in body for word in ("잘 봤", "잘봤", "유익", "즐감"))
        or re.search(r"(?:잘|재미\s*있게)\s*보(?:고|았|겠)|참고\s*잘", body)
        or re.search(r"(?:자료|정리|글|설명|분석|콘텐츠).*(?:좋|멋)", body)
    ):
        return "PRAISE"
    # Never infer agreement with a market prediction from an ambiguous reaction.
    return "ACK"


def praises_our_content(text: str) -> bool:
    """Recognize explicit benefit from our content, not the reader's contribution."""
    body = re.sub(r"@\w+", "", text or "").strip()
    content = r"(?:지표|자료|정리|글|설명|분석|콘텐츠|정보)"
    benefit = r"(?:덕분|도움|유익|한눈에|이해.*(?:쉽|잘)|파악.*수\s*있)"
    return bool(re.search(content + r".*" + benefit, body))


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
