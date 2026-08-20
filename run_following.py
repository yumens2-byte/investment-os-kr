"""
X Following Engagement Agent — 메인 파이프라인 (v1.0.0)
==========================================================
팔로잉 홈 타임라인 수집 → PreFilter → AI 분석 → Decision → 모드별 실행.

[정책 — 승인 확정]
- LIVE 허용 액션: QUOTE만. PERMITTED_REPLY는 REVIEW_ONLY로 강등 (자동 Reply 금지)
- POST 액션: 범위 제외 (SKIPPED_POLICY)
- X 쓰기 무재시도 / 발행-기록 짝 / 예산 즉시 저장 (기존 규약 승계)

[모드 — FOLLOWING_EXECUTION_MODE]  (문서 3~6장)
  dry_run — 전 파이프라인 실행, 로그만. DB 무기록·커서 미전진
  shadow  — kr_following_action 적재(would_execute) + 커서/예산 기록, X 쓰기 0
  live    — Safety Guard 통과 QUOTE만 실발행

[게이트] FOLLOWING_ENABLED != 'true' → 즉시 종료 (문서 34장, 초기 배포 false)
[예산] kr_reply_budget 공유 — timeline 읽기/Gemini/쓰기 전부 계상 (문서 보안 9장)
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from following_engine import analyzer, collector, decision, executor, prefilter, store
from following_engine.config import MAX_ACTIONS_PER_DAY, MAX_ACTIONS_PER_RUN, get_mode, is_enabled
from reply_engine import budget as budget_mod
from reply_engine import x_client
from reply_engine.config import (
    PUBLISH_JITTER_MAX_SEC,
    PUBLISH_JITTER_MIN_SEC,
    get_my_user_id,
)
from reply_engine.store import (
    get_blacklist_ids,
    get_budget,
    get_cursor,
    kst_today,
    upsert_budget,
    upsert_cursor,
)

VERSION = "1.0.0"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / f"following_{today}.log"),
        ],
    )


def _write_report(summary: dict, guard=None) -> None:
    """실행 요약 JSON (artifact + Job Summary 원본) — 실패해도 파이프라인 무영향."""
    if guard is not None:
        summary["budget"] = guard.snapshot()
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"following_report_{stamp}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        logger.info(f"[Report] 리포트 저장: {path}")
    except Exception as exc:
        logger.warning(f"[Report] 리포트 저장 실패 (무시): {exc}")


def main() -> dict:
    _setup_logging()
    mode = get_mode()
    logger.info(f"[FollowingAgent] v{VERSION} 시작 | mode={mode}")

    summary: dict = {
        "version": VERSION,
        "mode": mode,
        "success": False,
        "exit_reason": None,
        "fetched": 0,
        "prefiltered": 0,
        "analyzed": 0,
        "candidates": 0,
        "would_execute": 0,
        "actual_writes": 0,
        "skip_reasons": {},
        "review": [],
        "started_at": datetime.now(UTC).isoformat(),
    }

    def _skip(post_id: str, reason: str) -> None:
        summary["skip_reasons"][reason] = summary["skip_reasons"].get(reason, 0) + 1
        logger.info(f"[Skip] {post_id}: {reason}")

    # ── Step 0: 게이트 ────────────────────────────────────────
    if not is_enabled():
        logger.warning("[Step0] FOLLOWING_ENABLED != 'true' — 기능 비활성, 종료")
        summary["exit_reason"] = "EXIT_DISABLED"
        _write_report(summary)
        return summary

    db_write_allowed = mode in ("shadow", "live")

    # ── Step 1: 예산 (kr_reply_budget 공유) ───────────────────
    guard = budget_mod.BudgetGuard(get_budget(kst_today()))
    if not guard.can_read():
        summary["exit_reason"] = "EXIT_BUDGET"
        _write_report(summary, guard)
        return summary

    # ── Step 2: 수집 ──────────────────────────────────────────
    client = x_client.get_x_client()
    if client is None:
        summary["exit_reason"] = "EXIT_NO_CREDENTIALS"
        _write_report(summary, guard)
        return summary

    my_user_id = get_my_user_id()
    cursor = get_cursor(store.CURSOR_ACCOUNT)
    if not my_user_id:
        my_user_id = (cursor or {}).get("my_user_id") or ""
    if not my_user_id:
        # 최후 수단 get_me (읽기 1콜) — X_MY_USER_ID 등록 시 발생하지 않음
        my_user_id = x_client.fetch_my_user_id(client) or ""
        guard.record_read()
        if not my_user_id:
            summary["exit_reason"] = "EXIT_GET_ME_FAIL"
            if db_write_allowed:
                upsert_budget(guard.row)
            _write_report(summary, guard)
            return summary
        if not guard.can_read():
            summary["exit_reason"] = "EXIT_BUDGET"
            if db_write_allowed:
                upsert_budget(guard.row)
            _write_report(summary, guard)
            return summary

    since_id = (cursor or {}).get("since_id") or None
    fetched = collector.fetch_home_timeline(client, since_id)
    guard.record_read()
    if not fetched["success"]:
        # 요금제 미허용(403 등) 포함 — 명확 종료, 쓰기 0
        summary["exit_reason"] = "EXIT_TIMELINE_FETCH_FAIL"
        summary["fetch_error"] = fetched["error"]
        if db_write_allowed:
            upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    tweets = fetched["tweets"]
    users = fetched["users"]
    summary["fetched"] = len(tweets)

    if db_write_allowed and fetched["newest_id"]:
        upsert_cursor(store.CURSOR_ACCOUNT, fetched["newest_id"], my_user_id)

    if not tweets:
        summary["success"] = True
        summary["exit_reason"] = "EXIT_NO_NEW_POSTS"
        if db_write_allowed:
            upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    # ── Step 3: PreFilter ─────────────────────────────────────
    blacklist = get_blacklist_ids()
    passed: list[dict] = []
    for tweet in tweets:
        ok, reason = prefilter.check_static(tweet, my_user_id, blacklist)
        if not ok:
            _skip(tweet["id"], reason)
            continue
        ok, reason = prefilter.check_db(tweet, mode)
        if not ok:
            _skip(tweet["id"], reason)
            continue
        passed.append(tweet)
    summary["prefiltered"] = len(passed)
    logger.info(f"[Step3] PreFilter 통과 {len(passed)}건 / {len(tweets)}건")

    # ── Step 4: AI 분석 ───────────────────────────────────────
    analyses: dict[str, dict] = {}
    if passed:
        analyses = analyzer.analyze_batch(
            [
                {
                    "id": t["id"],
                    "author": users.get(t["author_id"], {}).get("username", ""),
                    "text": t["text"],
                    "metrics": t["metrics"],
                }
                for t in passed
            ]
        )
        guard.record_gemini()
    summary["analyzed"] = len(analyses)

    # ── Step 5~7: Decision → 모드별 실행 ─────────────────────
    recent_texts = store.get_recent_generated_texts()
    executed_today = store.count_actions_today(mode) if mode != "dry_run" else 0
    executed_this_run = 0

    for tweet in passed:
        post_id = tweet["id"]
        analysis = analyses.get(post_id)
        if analysis is None:
            _skip(post_id, "SKIP_AI_FAIL")   # fail-safe (문서 19장)
            continue

        action_type, skip_reason = decision.decide(analysis, recent_texts)

        candidate = {
            "post_id": post_id,
            "author_id": tweet["author_id"],
            "author_username": users.get(tweet["author_id"], {}).get("username", ""),
            "post_text": tweet["text"],
            "action_type": action_type,
            **analysis,
        }

        entry = {
            "post_id": post_id,
            "author": candidate["author_username"],
            "post_preview": tweet["text"][:100],
            "action_type": action_type,
            "scores": (
                f"R{analysis['relevance_score']}/C{analysis['content_value']}"
                f"/E{analysis['engagement_value']}"
            ),
            "generated_text": candidate.get("generated_text", ""),
            "result": None,
        }
        summary["review"].append(entry)

        # SKIP 계열 — dry_run은 로그만, shadow/live는 감사추적 기록
        if action_type == "SKIP":
            entry["result"] = skip_reason
            _skip(post_id, skip_reason)
            if db_write_allowed:
                store.insert_action(
                    executor.build_record(candidate, mode, False, "SKIPPED", skip_reason)
                )
            continue

        # 상한 (per-run / per-day) — QUOTE에만 적용, REVIEW_ONLY는 무제한 적재
        if action_type == "QUOTE":
            if executed_this_run >= MAX_ACTIONS_PER_RUN:
                entry["result"] = "RUN_LIMIT"
                _skip(post_id, "RUN_LIMIT")
                if db_write_allowed:
                    store.insert_action(
                        executor.build_record(candidate, mode, False, "SKIPPED", "RUN_LIMIT")
                    )
                continue
            if executed_today + executed_this_run >= MAX_ACTIONS_PER_DAY:
                entry["result"] = "DAILY_LIMIT"
                _skip(post_id, "DAILY_LIMIT")
                if db_write_allowed:
                    store.insert_action(
                        executor.build_record(candidate, mode, False, "SKIPPED", "DAILY_LIMIT")
                    )
                continue

        # ── 모드 라우팅 (문서 18장) ──
        if mode == "dry_run":
            entry["result"] = "DRY_RUN_COMPLETED"
            logger.info(
                f"[DRY_RUN] postId={post_id} action={action_type} "
                f"text='{candidate.get('generated_text', '')}' writeExecuted=false"
            )
            if action_type == "QUOTE":
                executed_this_run += 1
                summary["would_execute"] += 1
                recent_texts.append(candidate.get("generated_text", ""))
            continue

        if mode == "shadow":
            would = action_type == "QUOTE"
            status = "SHADOW_COMPLETED" if would else "READY"
            if not store.insert_action(
                executor.build_record(candidate, mode, would, status, None)
            ):
                entry["result"] = "HISTORY_INSERT_FAIL"
                _skip(post_id, "HISTORY_INSERT_FAIL")
                continue
            entry["result"] = status
            if would:
                executed_this_run += 1
                summary["would_execute"] += 1
                recent_texts.append(candidate.get("generated_text", ""))
            continue

        # ── live ──
        if action_type == "REVIEW_ONLY":
            store.insert_action(executor.build_record(candidate, mode, False, "READY", None))
            entry["result"] = "READY"   # 마스터 수동 처리 후보 (문서 27장)
            continue

        guard_ok, guard_code = executor.live_safety_guard(
            candidate, executed_today, executed_this_run, MAX_ACTIONS_PER_RUN
        )
        if not guard_ok:
            entry["result"] = guard_code
            _skip(post_id, guard_code)
            store.insert_action(
                executor.build_record(candidate, mode, False, "SKIPPED_POLICY", guard_code)
            )
            continue

        if not guard.can_write():
            entry["result"] = "BUDGET_WRITE"
            _skip(post_id, "BUDGET_WRITE")
            store.insert_action(
                executor.build_record(candidate, mode, False, "SKIPPED", "BUDGET_WRITE")
            )
            continue

        if not store.insert_action(
            executor.build_record(candidate, mode, True, "READY", None)
        ):
            entry["result"] = "HISTORY_INSERT_FAIL"
            _skip(post_id, "HISTORY_INSERT_FAIL")
            continue

        actual_id = executor.publish_quote(client, candidate["generated_text"], post_id)
        guard.record_write()
        upsert_budget(guard.row)   # 예산 즉시 저장 규약

        if actual_id:
            store.mark_executed(post_id, actual_id)
            entry["result"] = "EXECUTED"
            executed_this_run += 1
            summary["actual_writes"] += 1
            recent_texts.append(candidate["generated_text"])
        else:
            store.mark_failed(post_id, "PUBLISH_FAIL", "create_tweet 실패 (무재시도)")
            entry["result"] = "FAILED"
            _skip(post_id, "PUBLISH_FAIL")

        time.sleep(random.randint(PUBLISH_JITTER_MIN_SEC, PUBLISH_JITTER_MAX_SEC))

    summary["candidates"] = sum(
        1 for e in summary["review"] if e["action_type"] in ("QUOTE", "REVIEW_ONLY")
    )

    # ── Step 8: 마감 ──────────────────────────────────────────
    if db_write_allowed:
        upsert_budget(guard.row)
    summary["success"] = True
    summary["exit_reason"] = "EXIT_OK"
    logger.info(
        f"[FollowingAgent] 완료 | fetched={summary['fetched']} "
        f"prefiltered={summary['prefiltered']} candidates={summary['candidates']} "
        f"would={summary['would_execute']} writes={summary['actual_writes']} "
        f"skip={summary['skip_reasons']}"
    )
    _write_report(summary, guard)
    return summary


if __name__ == "__main__":
    result = main()
    fail_reasons = {"EXIT_NO_CREDENTIALS", "EXIT_GET_ME_FAIL", "EXIT_TIMELINE_FETCH_FAIL"}
    sys.exit(1 if result.get("exit_reason") in fail_reasons else 0)
