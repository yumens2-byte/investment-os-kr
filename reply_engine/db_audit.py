"""Reply Engine 운영 DB의 스키마 계약과 핵심 데이터 정합성을 점검한다."""

from __future__ import annotations

from collections import Counter
from typing import Any

from db.supabase_client import get_client

REQUIRED_TABLE_CONTRACTS = {
    "kr_reply_history": (
        "reply_tweet_id,conversation_id,author_id,responded,response_tweet_id,"
        "skip_reason,response_text,mode,created_at"
    ),
    "kr_reply_cursor": "account,since_id,my_user_id,updated_at",
    "kr_reply_budget": (
        "budget_date,read_calls,write_calls,gemini_calls,est_cost_krw,updated_at"
    ),
    "kr_reply_blacklist": "author_id",
}

OPTIONAL_TABLE_CONTRACTS = {
    "kr_reply_likes": "reply_tweet_id,author_id,mode,created_at",
}


def _sample(table: str, columns: str, limit: int) -> list[dict[str, Any]]:
    result = get_client().table(table).select(columns).limit(limit).execute()
    return list(result.data or [])


def audit_reply_db(history_limit: int = 5000, *, require_likes: bool = False) -> dict[str, Any]:
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
    }
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    contracts = {**REQUIRED_TABLE_CONTRACTS, **OPTIONAL_TABLE_CONTRACTS}
    for table, columns in contracts.items():
        try:
            limit = max(1, history_limit) if table == "kr_reply_history" else 1
            rows_by_table[table] = _sample(table, columns, limit)
        except Exception as exc:
            is_required = table in REQUIRED_TABLE_CONTRACTS or require_likes
            error_bucket = "schema_errors" if is_required else "optional_schema_errors"
            report[error_bucket][table] = type(exc).__name__
            if is_required:
                report["healthy"] = False

    history = rows_by_table.get("kr_reply_history", [])
    report["rows_checked"] = len(history)
    report["truncated"] = len(history) >= max(1, history_limit)
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

    for name, values in (
        ("publish_state_mismatch", state_mismatch),
        ("duplicate_response_tweet_id", duplicate_responses),
        ("live_without_terminal_state", invalid_live),
    ):
        report["issues"][name] = len(values)
        report["samples"][name] = values[:10]

    # 상태 불일치와 response ID 중복은 중복 발행/캡 누락으로 이어지는 치명적 이상이다.
    if state_mismatch or duplicate_responses:
        report["healthy"] = False
    return report
