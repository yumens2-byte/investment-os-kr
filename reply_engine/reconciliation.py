"""Offline publication evidence review. No network, DB writes or retry actions.

The caller supplies an authenticated X API export. Matching exported evidence
is a proposal for operator review, never an instruction to republish or mutate DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reply_engine.policy import decode_metadata
from reply_engine.telemetry import text_hash

UNRESOLVED = frozenset({"PUBLISHING", "PUBLISH_UNKNOWN", "DB_CONFIRM_FAIL", "PUBLISH_FAIL"})


def _time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def review_publication(row: dict, evidence: list[dict], *, account_id: str) -> dict:
    """Require account, exact body, direct parent, timestamp and unique response.

    Evidence schema uses raw X API tweet fields: id, author_id, text, created_at,
    referenced_tweets=[{type: replied_to, id: target}]. Missing exports or absence
    of matches never prove non-publication. Legacy rows without request hashes
    remain unresolved for manual inspection.
    """
    target = str(row.get("reply_tweet_id") or "")
    result = {"target_tweet_id": target, "status": "UNRESOLVED", "may_retry": False}
    if row.get("responded") or row.get("response_tweet_id"):
        return {**result, "reason": "ALREADY_CONFIRMED"}
    if row.get("mode") != "live" or row.get("skip_reason") not in UNRESOLVED:
        return {**result, "reason": "NOT_UNRESOLVED_LIVE"}
    meta = decode_metadata(row.get("error_message"))
    start = _time(meta.get("requested_at") or meta.get("claimed_at"))
    digest = meta.get("response_text_sha256")
    if not all((target, account_id, meta.get("attempt_id"), start, digest)):
        return {**result, "reason": "MISSING_REQUEST_EVIDENCE"}
    if str(meta.get("account_user_id") or "") != account_id:
        return {**result, "reason": "ACCOUNT_MISMATCH"}
    if str(meta.get("target_tweet_id") or "") != target:
        return {**result, "reason": "TARGET_MISMATCH"}
    if text_hash(row.get("response_text") or "") != digest:
        return {**result, "reason": "REQUEST_HASH_MISMATCH"}
    matches = set()
    invalid = False
    for tweet in evidence:
        refs = tweet.get("referenced_tweets") or []
        direct = any(
            r.get("type") == "replied_to" and str(r.get("id") or "") == target
            for r in refs if isinstance(r, dict)
        )
        created = _time(tweet.get("created_at"))
        if (str(tweet.get("author_id") or "") != account_id or not direct
                or not isinstance(tweet.get("text"), str)
                or text_hash(tweet["text"]) != digest):
            continue
        if not created or not (start <= created <= start + timedelta(hours=1)):
            continue
        response_id = str(tweet.get("id") or "")
        if not response_id.isdecimal() or response_id == target:
            invalid = True
        else:
            matches.add(response_id)
    if invalid or len(matches) > 1:
        return {**result, "reason": "AMBIGUOUS_EVIDENCE"}
    if not matches:
        return {**result, "reason": "NO_MATCH_IS_NOT_PROOF_OF_ABSENCE"}
    return {
        **result,
        "status": "PUBLISHED_EVIDENCE_REVIEW_REQUIRED",
        "reason": "EXACT_DIRECT_REPLY_MATCH",
        "response_tweet_id": next(iter(matches)),
        "attempt_id": meta["attempt_id"],
        "response_text_sha256": digest,
    }
