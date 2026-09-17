"""Reply Engine 운영 DB의 스키마 계약과 핵심 데이터 정합성을 점검한다."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from db.supabase_client import get_client

REQUIRED_TABLE_CONTRACTS = {
    "kr_reply_history": (
        "reply_tweet_id,conversation_id,author_id,author_username,comment_text,"
        "classification,responded,response_tweet_id,skip_reason,response_text,"
        "error_message,dry_run,mode,created_at"
    ),
    "kr_reply_cursor": "account,since_id,my_user_id,updated_at",
    "kr_reply_budget": (
        "budget_date,read_calls,write_calls,gemini_calls,est_cost_krw,updated_at"
    ),
    "kr_reply_blacklist": "author_id",
}

OPTIONAL_TABLE_CONTRACTS = {
    "kr_reply_likes": "reply_tweet_id,author_id,mode,would_like,created_at",
}


def _sample(table: str, columns: str, limit: int) -> list[dict[str, Any]]:
    query = get_client().table(table).select(columns)
    if table == "kr_reply_history":
        query = query.order("created_at", desc=True)
    result = query.limit(limit).execute()
    return list(result.data or [])


def _is_older_than(value: Any, cutoff: datetime) -> bool:
    """Treat only valid timestamps as stale; malformed values remain visible in the sample."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed < cutoff
    except (TypeError, ValueError):
        return False


def audit_reply_db(
    history_limit: int = 5000,
    *,
    require_likes: bool = False,
    terminal_grace_minutes: int = 60,
) -> dict[str, Any]:
    """테이블 계약과 history 불변식을 읽기 전용으로 검사한다.

    ``healthy``는 필수 스키마 조회가 모두 성공하고 중복 발행 ID나 발행 상태
    불일치가 없을 때만 참이다. 좋아요 테이블은 기능이 활성화된 경우에만 필수다.
    운영 DB 전체 덤프를 리포트에 노출하지 않고 건수와 ID 표본만 반환한다.
    """
    report: dict[str, Any] = {
        "healthy": True,
        "schema_errors": {},
        "optional_schema_errors": {},
        "rows_checked": 0,
        "issues": {},
        "samples": {},
        "truncated": False,
        "terminal_grace_minutes": max(0, terminal_grace_minutes),
    }
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    contracts = {**REQUIRED_TABLE_CONTRACTS, **OPTIONAL_TABLE_CONTRACTS}
    for table, columns in contracts.items():
        try:
            # history는 limit+1을 읽어 잘림 여부를 정확히 판정한다. 나머지 테이블은
            # 컬럼 계약 확인만 하므로 한 행이면 충분하다.
            limit = max(1, history_limit) + 1 if table == "kr_reply_history" else 1
            rows_by_table[table] = _sample(table, columns, limit)
        except Exception as exc:
            is_required = table in REQUIRED_TABLE_CONTRACTS or require_likes
            error_bucket = "schema_errors" if is_required else "optional_schema_errors"
            report[error_bucket][table] = type(exc).__name__
            if is_required:
                report["healthy"] = False

    requested_limit = max(1, history_limit)
    sampled_history = rows_by_table.get("kr_reply_history", [])
    report["truncated"] = len(sampled_history) > requested_limit
    history = sampled_history[:requested_limit]
    report["rows_checked"] = len(history)
    if not history:
        return report

    state_mismatch = [
        str(row.get("reply_tweet_id"))
        for row in history
        if bool(row.get("responded")) != bool(row.get("response_tweet_id"))
    ]
    response_counts = Counter(
        str(row["response_tweet_id"])
        for row in history
        if row.get("response_tweet_id")
    )
    duplicate_responses = [key for key, count in response_counts.items() if count > 1]
    invalid_live = [
        str(row.get("reply_tweet_id"))
        for row in history
        if row.get("mode") == "live"
        and not row.get("responded")
        and not row.get("skip_reason")
    ]
    grace_cutoff = datetime.now(UTC) - timedelta(minutes=max(0, terminal_grace_minutes))
    stale_invalid_live = [
        str(row.get("reply_tweet_id"))
        for row in history
        if row.get("mode") == "live"
        and not row.get("responded")
        and not row.get("skip_reason")
        and _is_older_than(row.get("created_at"), grace_cutoff)
    ]

    for name, values in (
        ("publish_state_mismatch", state_mismatch),
        ("duplicate_response_tweet_id", duplicate_responses),
        ("live_without_terminal_state", invalid_live),
        ("stale_live_without_terminal_state", stale_invalid_live),
    ):
        report["issues"][name] = len(values)
        report["samples"][name] = values[:10]

    # 상태 불일치와 response ID 중복은 중복 발행/캡 누락으로 이어지는 치명적 이상이다.
    if state_mismatch or duplicate_responses or stale_invalid_live:
        report["healthy"] = False
    return report
