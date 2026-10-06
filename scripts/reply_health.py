"""Read-only collection heartbeat monitor. Alerts, but never reruns or publishes."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from core.alert import send_admin_alert
from reply_engine import store
from scripts.collect_reply import HEARTBEAT


def assess_heartbeat(cursor: dict | None, *, now=None) -> dict:
    now = now or datetime.now(UTC)
    stamp = store.parse_utc((cursor or {}).get("updated_at"))
    if stamp is None:
        return {"severity": "UNKNOWN", "age_hours": None, "reason": "NO_HEARTBEAT"}
    age = (now - stamp).total_seconds() / 3600
    if age < 0:
        return {"severity": "UNKNOWN", "age_hours": age, "reason": "CLOCK_SKEW"}
    return {
        "severity": "CRITICAL" if age >= 12 else "WARNING" if age >= 6 else "OK",
        "age_hours": round(age, 2),
    }


def main() -> dict:
    report = assess_heartbeat(store.get_cursor(HEARTBEAT))
    if report["severity"] != "OK":
        send_admin_alert(
            f"Reply collection heartbeat: {report['severity']} age_hours={report['age_hours']}"
        )
    return report


if __name__ == "__main__":
    result = main()
    print(json.dumps(result))
    raise SystemExit(0 if result["severity"] == "OK" else 1)
