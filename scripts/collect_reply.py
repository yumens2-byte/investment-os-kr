"""Durable collection only. Never imports generation or publishes a reply/like.

Run only under reply-engine concurrency with REPLY_COLLECTOR_ENABLED=true.
The live worker consumes RECEIVED rows through its normal recovery admission.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from reply_engine import budget, config, store, telemetry, x_client

ACCOUNT = "kr_main"
HEARTBEAT = "kr_main:collection-heartbeat"


def main() -> dict:
    report = {
        **telemetry.new_run(),
        "mode": "collect_only",
        "success": False,
        "collected": 0,
        "actual_published": 0,
        "started_at": datetime.now(UTC).isoformat(),
    }
    guard = None
    try:
        if not config.is_enabled() or not config.env_bool("REPLY_COLLECTOR_ENABLED", False):
            report.update(success=True, exit_reason="DISABLED")
            return report
        guard = budget.BudgetGuard(store.get_budget(store.kst_today()))
        client = x_client.get_x_client()
        if client is None:
            raise RuntimeError("X_CREDENTIALS_UNAVAILABLE")
        cursor = store.get_cursor(ACCOUNT) or {}
        user_id = config.get_my_user_id() or cursor.get("my_user_id")
        if not user_id:
            # Reserve read before API; failed persistence stops all external calls.
            if not guard.can_read():
                raise RuntimeError("READ_BUDGET_EXHAUSTED")
            guard.record_read()
            if not store.upsert_budget(guard.row):
                raise RuntimeError("BUDGET_SAVE_FAILED")
            user_id = x_client.fetch_my_user_id(client)
        if not user_id:
            raise RuntimeError("ACCOUNT_UNVERIFIED")
        # One page keeps collector reservation bounded and leaves the worker read
        # budget for root verification. Incomplete scans never advance the cursor.
        if not guard.can_read():
            raise RuntimeError("READ_BUDGET_EXHAUSTED")
        guard.record_read()
        if not store.upsert_budget(guard.row):
            raise RuntimeError("BUDGET_SAVE_FAILED")
        fetched = x_client.fetch_mentions(client, user_id, cursor.get("since_id"), max_pages=1)
        # DN2026_0100: 예약한 1콜을 실제 반환 리소스(멘션 1건 = 본문·부모·작성자 최대 3)로 정산.
        guard.settle_read(3 * len(fetched.get("tweets") or []))
        if guard.resource_mode and not store.upsert_budget(guard.row):
            # 정산값이 예약값보다 클 수 있으므로 저장 실패 시 이후 처리를 중단한다(fail-closed).
            # 커서를 전진시키지 않으므로 다음 실행이 같은 구간을 다시 수집한다.
            raise RuntimeError("BUDGET_SAVE_FAILED")
        if not fetched.get("success"):
            raise RuntimeError("FETCH_FAILED")
        report["collected"] = len(fetched["tweets"])
        if not store.persist_collected(
            fetched["tweets"], fetched["users"], user_id, report["run_id"]
        ):
            raise RuntimeError("INBOX_SAVE_FAILED")
        complete = fetched.get("collection_complete", not fetched.get("saturated", False))
        report["collection_complete"] = complete
        if not complete:
            raise RuntimeError("COLLECTION_INCOMPLETE")
        if fetched.get("newest_id") and not store.upsert_cursor(
            ACCOUNT, fetched["newest_id"], user_id
        ):
            raise RuntimeError("CURSOR_SAVE_FAILED")
        # Unlike since_id, heartbeat moves on a successful empty collection too.
        if not store.upsert_cursor(HEARTBEAT, fetched.get("newest_id") or "0", user_id):
            raise RuntimeError("HEARTBEAT_SAVE_FAILED")
        report.update(success=True, exit_reason="COLLECTED")
    except Exception as exc:
        report["exit_reason"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        if guard:
            report["budget"] = guard.snapshot()
        telemetry.event(report, report.get("exit_reason", "UNKNOWN"))
        path = Path("logs/reply_collection_report.json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["success"] else 1)
