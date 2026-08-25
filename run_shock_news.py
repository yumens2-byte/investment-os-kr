"""
SHOCK_NEWS — 당일 충격 사건 발행 파이프라인 (v1.0.0)
======================================================
KST 16시대(한국)/04시대(미국) 슬롯에 직전 24h 최고 충격 기사 1건 + 놀람 코멘트 발행.

[모드 — SHOCK_EXECUTION_MODE]
  dry_run — 수집~게이트까지. DB·X 쓰기 전무
  shadow  — kr_shock_news_history 적재(would_execute), X 쓰기 없음
  live    — 슬롯 내 랜덤 분 대기 후 실 발행 (무재시도)

[중복 방지 4층] L1 article_hash PK / L2 (slot_key,mode) UNIQUE / L3 제목 유사도 0.6 /
L4 yml concurrency + 무재시도 + 발행-기록 짝
[완화 불가 게이트] 실명·유죄 단정·잔혹 상세 (R-A)
[긴급 정지] SHOCK_ENABLED != 'true'
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from reply_engine import budget as budget_mod
from reply_engine import store as rstore
from reply_engine import x_client
from shock_news_engine import collector, gate, publisher, ranker
from shock_news_engine import store as sstore
from shock_news_engine.config import (
    KST,
    PUBLISH_WINDOW_SEC,
    determine_slot,
    get_mode,
    is_enabled,
)

VERSION = "1.1.0"

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / f"shock_{today}.log"),
        ],
    )


def _write_report(summary: dict, guard=None) -> None:
    if guard is not None:
        summary["budget"] = guard.snapshot()
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        (log_dir / f"shock_report_{stamp}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str)
        )
    except Exception as exc:
        logger.warning(f"[SReport] 리포트 저장 실패 (무시): {exc}")


def main() -> dict:
    _setup_logging()
    mode = get_mode()
    now_kst = datetime.now(KST)
    logger.info(f"[ShockNews] v{VERSION} 시작 | mode={mode} | kst={now_kst.isoformat()}")

    summary: dict = {
        "version": VERSION, "mode": mode, "success": False, "exit_reason": None,
        "slot_key": None, "session": None, "collected": 0, "tiered": 0,
        "chosen": None, "published": 0, "skip_reasons": {},
        "started_at": datetime.now(UTC).isoformat(),
    }

    def _skip(reason: str) -> None:
        summary["skip_reasons"][reason] = summary["skip_reasons"].get(reason, 0) + 1

    # ── Step 0: 게이트 ─────────────────────────────────────────
    if not is_enabled():
        logger.warning("[Step0] SHOCK_ENABLED != 'true' — 비활성, 종료")
        summary["exit_reason"] = "EXIT_DISABLED"
        _write_report(summary)
        return summary

    slot = determine_slot(now_kst, mode)
    if slot is None:
        logger.warning(
            "[Step0] 슬롯 시간대 아님 — 종료 (live는 KST 15~16시/03~04시만. "
            "dry_run/shadow는 시간 무관 실행)"
        )
        summary["exit_reason"] = "EXIT_OFF_SLOT"
        _write_report(summary)
        return summary
    slot_key, session = slot
    summary["slot_key"], summary["session"] = slot_key, session
    db_write_allowed = mode in ("shadow", "live")

    # ── Step 1: 슬롯 중복 사전 확인 (L2 조회형 — INSERT 충돌은 최종 방어) ──
    if db_write_allowed and sstore.slot_taken(slot_key, mode):
        logger.warning(f"[Step1] 슬롯 기처리: {slot_key}/{mode} — 종료")
        summary["exit_reason"] = "EXIT_SLOT_TAKEN"
        _write_report(summary)
        return summary

    # ── Step 2: 예산 ───────────────────────────────────────────
    guard = budget_mod.BudgetGuard(rstore.get_budget(rstore.kst_today()))

    # ── Step 3: 수집 (X API 미사용 — RSS) ──────────────────────
    articles = collector.fetch_articles(session)
    summary["collected"] = len(articles)
    if not articles:
        summary["success"] = True
        summary["exit_reason"] = "EXIT_NO_ARTICLES"
        _write_report(summary, guard)
        return summary

    # ── Step 4: L1 — 기발행 기사 제거 ──────────────────────────
    fresh = [a for a in articles if not sstore.article_exists(a["article_hash"])] \
        if db_write_allowed else articles
    for _ in range(len(articles) - len(fresh)):
        _skip("DUP_ARTICLE")

    # ── Step 5: 티어링 ─────────────────────────────────────────
    candidates = ranker.select_candidates(fresh)
    summary["tiered"] = len(candidates)
    if not candidates:
        summary["success"] = True
        summary["exit_reason"] = "EXIT_NO_TIER_MATCH"
        _write_report(summary, guard)
        return summary

    # ── Step 6: L3 — 동일 사건(이종 기사) 제거 ─────────────────
    recent_titles = sstore.get_recent_titles() if db_write_allowed else []
    deduped = []
    for cand in candidates:
        ok, reason = gate.check_title_duplicate(cand["title"], recent_titles)
        if not ok:
            _skip(reason)
            continue
        deduped.append(cand)
    if not deduped:
        summary["success"] = True
        summary["exit_reason"] = "EXIT_ALL_DUP_EVENT"
        _write_report(summary, guard)
        return summary

    # ── Step 7: Gemini 랭킹 + 코멘트 생성 ──────────────────────
    chosen = ranker.rank_and_generate(deduped, session)
    guard.record_gemini()
    if chosen is None:
        summary["exit_reason"] = "EXIT_RANK_FAIL"
        if db_write_allowed:
            rstore.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    # ── Step 8: 안전 게이트 (완화 불가) ────────────────────────
    ok, reason = gate.check_comment(chosen["comment"])
    if not ok:
        logger.warning(f"[Step8] 게이트 탈락({reason}) — 무발행 (default-deny)")
        _skip(reason)
        summary["exit_reason"] = "EXIT_GATE_FAIL"
        if db_write_allowed:
            rstore.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    summary["chosen"] = {
        "slot": slot_key, "tier": chosen["tier"], "title": chosen["title"],
        "url": chosen["url"], "comment": chosen["comment"], "picked_by": chosen["picked_by"],
    }

    # ── Step 9: 모드별 실행 ────────────────────────────────────
    if mode == "dry_run":
        logger.info(
            f"[DRY_RUN] tier={chosen['tier']} title='{chosen['title'][:60]}' "
            f"comment='{chosen['comment']}' writeExecuted=false"
        )
        summary["success"] = True
        summary["exit_reason"] = "EXIT_OK"
        _write_report(summary, guard)
        return summary

    record = {
        "article_hash": chosen["article_hash"],
        "slot_key": slot_key,
        "session": session,
        "tier": chosen["tier"],
        "title": chosen["title"],
        "url": chosen["url"],
        "comment_text": chosen["comment"],
        "mode": mode,
        "would_execute": True,
        "picked_by": chosen["picked_by"],
    }

    # L1+L2 최종 방어: INSERT 실패 시 발행 금지
    if not sstore.insert_history(record):
        summary["exit_reason"] = "EXIT_DUP_FINAL"
        rstore.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    if mode == "shadow":
        logger.info(f"[SHADOW] 적재 완료 (발행 없음): {slot_key} '{chosen['comment']}'")
        summary["success"] = True
        summary["exit_reason"] = "EXIT_OK"
        rstore.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    # ── live: 슬롯 내 랜덤 분 대기 (안티봇) → 발행 (무재시도) ──
    if not guard.can_write():
        sstore.mark_failed(chosen["article_hash"], "BUDGET_WRITE", slot_key)
        summary["exit_reason"] = "EXIT_BUDGET_WRITE"
        rstore.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    delay = random.randint(0, PUBLISH_WINDOW_SEC)
    logger.info(f"[Step9] 발행 랜덤 딜레이 {delay}초 대기 (안티봇)")
    time.sleep(delay)

    client = x_client.get_x_client()
    if client is None:
        sstore.mark_failed(chosen["article_hash"], "NO_CREDENTIALS", slot_key)
        summary["exit_reason"] = "EXIT_NO_CREDENTIALS"
        rstore.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    tweet_id, publish_error = publisher.post_shock(client, chosen["comment"], chosen["url"])
    guard.record_write()
    rstore.upsert_budget(guard.row)   # 발행마다 즉시 저장 (V-1 규약)

    if tweet_id:
        sstore.mark_posted(chosen["article_hash"], tweet_id)
        summary["published"] = 1
        summary["success"] = True
        summary["exit_reason"] = "EXIT_OK"
    else:
        # N-1: spend cap은 재시도로 풀리지 않는 플랫폼 사유 — 구분 보고
        cap = x_client.is_spend_cap_error(publish_error)
        reason = "SPEND_CAP" if cap else "PUBLISH_FAIL"
        if cap:
            logger.error(
                "[Step9] X API 월간 지출 상한 도달 — 코드 문제 아님. "
                "Developer Portal에서 spend cap 확인/상향 필요 (N-1)"
            )
        # N-2: 발행 실패 시 정규 슬롯 반환 (재시도 가능하게)
        sstore.mark_failed(chosen["article_hash"], reason, slot_key)
        summary["exit_reason"] = "EXIT_SPEND_CAP" if cap else "EXIT_PUBLISH_FAIL"

    logger.info(f"[ShockNews] 완료 | published={summary['published']}")
    _write_report(summary, guard)
    return summary


if __name__ == "__main__":
    result = main()
    fail = {"EXIT_NO_CREDENTIALS", "EXIT_RANK_FAIL", "EXIT_PUBLISH_FAIL", "EXIT_SPEND_CAP"}
    sys.exit(1 if result.get("exit_reason") in fail else 0)
