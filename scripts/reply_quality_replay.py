"""Bounded historical quality replay. Only API budget may be written to the DB.

Run with ``python -m scripts.reply_quality_replay`` under reply-engine concurrency.
Historical comments bypass age/duplicate/cap admission deliberately; nothing publishes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reply_engine import budget, classifier, config, gate, generator, store, x_client

REPORT_PATH = Path("logs/reply_quality_replay.json")
MAX_SAMPLES = 10


def _samples() -> list[dict]:
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    return (
        store.get_client().table("kr_reply_history")
        .select("reply_tweet_id,author_id,comment_text,response_text,created_at")
        .eq("responded", True).eq("dry_run", False)
        .gte("created_at", cutoff).order("created_at", desc=True).limit(MAX_SAMPLES)
        .execute()
    ).data or []


def main() -> dict:
    report = {
        "success": False, "stage": "budget", "sample_limit": MAX_SAMPLES,
        "started_at": datetime.now(UTC).isoformat(), "review": [], "generated": 0,
        "verified": 0, "model_usage": [], "actual_published": 0, "likes": 0,
        "history_changed": False, "cursor_changed": False,
        "simulated_pass": 0, "model_verified": False, "gate_verified": False,
        "quality_verified": False, "human_review_required": True, "degraded": False,
    }
    guard = None

    def checkpoint() -> None:
        if not store.upsert_budget(guard.row):
            raise RuntimeError("API budget persistence failed; replay stopped")

    def read(call):
        if not guard.can_read():
            raise RuntimeError("Daily X read budget exhausted")
        previous = dict(guard.row)
        guard.record_read()
        try:
            checkpoint()  # Reserve one attempt before contacting X, including API failures.
        except Exception:
            guard.row = previous  # No X attempt occurred; do not report one as actual usage.
            report["budget_reservation_unconfirmed"] = True
            raise
        return call()

    def account_model(result):
        for _ in range(getattr(result, "api_calls", 0)):
            guard.record_gemini()
        report["model_usage"].extend(getattr(result, "usage", []))
        checkpoint()

    try:
        guard = budget.BudgetGuard(store.get_budget(store.kst_today()))
        report["stage"] = "history"
        rows = _samples()[:MAX_SAMPLES]
        report["samples"] = len(rows)
        if not rows:
            raise RuntimeError("No live responded samples in the last seven days")
        client = x_client.get_x_client()
        if client is None:
            raise RuntimeError("X credentials unavailable")
        report["stage"] = "identity"
        my_id = read(lambda: x_client.fetch_my_user_id(client))
        if not my_id:
            raise RuntimeError("Authenticated X identity unavailable")
        configured_id = config.get_my_user_id()
        if configured_id and configured_id != my_id:
            raise RuntimeError("Configured and authenticated X identity mismatch")
        report["stage"] = "context"
        response = read(lambda: client.get_tweets(
            ids=list(dict.fromkeys(str(row["reply_tweet_id"]) for row in rows)),
            tweet_fields=["author_id", "conversation_id", "in_reply_to_user_id",
                          "created_at", "referenced_tweets"],
            expansions=["referenced_tweets.id"], user_auth=True,
        ))
        actual = {str(t.id): t for t in (getattr(response, "data", None) or [])}
        parents = {
            str(t.id): t for t in ((getattr(response, "includes", None) or {}).get("tweets", []))
        }
        conv_ids = list(dict.fromkeys(
            str(t.conversation_id) for t in actual.values() if getattr(t, "conversation_id", None)
        ))
        roots = read(lambda: x_client.fetch_conversation_roots(client, conv_ids, max_calls=1)) \
            if conv_ids else {}
        verified = []
        for row in rows:
            tid = str(row["reply_tweet_id"])
            review = {
                "id": tid, "previous_comment": row.get("comment_text", ""),
                "previous_reply": row.get("response_text", ""),
                "context_verified": False, "draft": None, "final_reply": None,
            }
            report["review"].append(review)
            t = actual.get(tid)
            if t is None:
                review["result"] = "COMMENT_UNAVAILABLE"
                continue
            parent_id = x_client._parent_id(t)
            parent = parents.get(parent_id)
            root = roots.get(str(getattr(t, "conversation_id", "")))
            review.update(comment=t.text, parent_id=parent_id,
                          parent_text=getattr(parent, "text", "") or "")
            if (str(getattr(t, "author_id", "")) != str(row.get("author_id", ""))
                    or str(getattr(t, "in_reply_to_user_id", "")) != my_id
                    or not parent or str(getattr(parent, "author_id", "")) != my_id
                    or not review["parent_text"] or root is None):
                review["result"] = "CONTEXT_UNVERIFIED"
                continue
            foreign = root != my_id
            if foreign and not config.REPLY_FOREIGN_THREAD_ENABLED:
                review["result"] = "OUT_OF_SCOPE_THREAD"
                continue
            review.update(context_verified=True, root_author_id=root, foreign_thread=foreign)
            verified.append({
                "id": tid, "text": t.text, "parent_text": review["parent_text"],
                "parent_author_id": my_id, "root_author_id": root, "foreign_thread": foreign,
            })
        report["verified"] = len(verified)
        if not verified:
            raise RuntimeError("No samples with independently verified X parent/root context")
        report["stage"] = "classification"
        labels = classifier.classify_batch(verified)
        account_model(labels)
        passing = [{**item, "label": labels.get(item["id"], "AMBIGUOUS")}
                   for item in verified if labels.get(item["id"]) in classifier.PASS_LABELS]
        report["stage"] = "generation"
        replies = generator.generate_batch(passing)
        account_model(replies)
        report["generated"] = len(replies)
        recent = store.get_recent_response_texts(config.REPLY_RECENT_COMPARE_COUNT)
        by_id = {item["id"]: item for item in passing}
        for review in report["review"]:
            tid = review["id"]
            if not review["context_verified"]:
                continue
            review["label"] = labels.get(tid, "AMBIGUOUS")
            if tid not in by_id:
                review["result"] = "CLASSIFIER_UNAVAILABLE" if tid in getattr(
                    labels, "unavailable_ids", set()
                ) else f"CLASS_{review['label']}"
                continue
            draft = replies.get(tid, "")
            ok, reason = gate.check_reply(draft, recent, comment_text=review["comment"])
            review.update(draft=draft, draft_gate=reason,
                          source=getattr(replies, "sources", {}).get(tid, "UNKNOWN"))
            final = draft
            if not ok:
                for candidate in generator.contextual_fallbacks(by_id[tid]):
                    allowed, _ = gate.check_reply(candidate, recent, comment_text=review["comment"])
                    if allowed:
                        final, ok, reason = candidate, True, None
                        review["source"] = "TEMPLATE_FALLBACK"
                        break
            review.update(final_reply=final if ok else None, final_gate=reason,
                          result="SIMULATED_PASS" if ok else reason)
            if ok:
                recent.append(final)
                report["simulated_pass"] += 1
                if review["source"] == "AI" and final == draft:
                    report["model_verified"] = True
        report["gate_verified"] = report["simulated_pass"] > 0
        if not report["gate_verified"]:
            raise RuntimeError(
                "No simulated reply passed classification and the final quality gate"
            )
        report["degraded"] = not report["model_verified"]
        if report["degraded"]:
            report["degraded_reason"] = "Only reviewed templates passed; model output not verified"
        report["success"] = True
        report["stage"] = "completed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if guard:
            report["budget"] = guard.snapshot()
        report["finished_at"] = datetime.now(UTC).isoformat()
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report


if __name__ == "__main__":
    result = main()
    print(json.dumps({key: result.get(key) for key in (
        "success", "stage", "samples", "verified", "generated", "simulated_pass",
        "model_verified", "quality_verified", "degraded", "error"
    )}, ensure_ascii=False))
    raise SystemExit(0 if result["success"] else 1)
