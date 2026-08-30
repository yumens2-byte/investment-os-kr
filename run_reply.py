"""
X Reply Engine — 메인 파이프라인 (v1.0.0)
==============================================
내 게시글에 달린 댓글(멘션 타임라인) 수집 → 필터 → 루트검증 → 분류 → 생성 → 게이트 → 답글 발행.
스코프: conversation root(원 게시글) 작성자가 내 계정인 스레드만 (P-1, 2026-08-18).

[정책 요약]
- 무응답이 기본값 (default-deny): POSITIVE / SUPPORTIVE_NEUTRAL만 답글
- 답글은 공백 포함 40자 이내 감사·호응만 (봇이 아닌 것처럼)
- 발행 재시도 없음 (이중 답글 방지 우선 — 승인 E)
- 24시간 경과 댓글 자동 폐기 (승인 D)

[모드 — REPLY_MODE]
  dry_run — 수집/분류/생성/게이트까지. DB 쓰기·X 발행 전면 금지, 커서 미전진
  shadow  — DB 기록 O (mode='shadow'), X 발행 X. 마스터 검수용 (HG-2)
  live    — 실발행. 발행 성공 즉시 responded 갱신 (발행-기록 짝 규약)

[긴급 정지] REPLY_ENABLED != 'true' → 즉시 종료 (HG-3)

[중복 방지 6층]
  L1 history PK / L2 Supabase 커서 / L3 yml concurrency /
  L4 사용자 상한 / L5 대화 상한 / L6 텍스트 유사도

v1.3.0 (2026-08-30, R-2/R-3/R-5):
  R-2 캡 이중 계수 — Step3 승인 시 CapContext in-run 카운터 점유,
      발행 루프에서 실발행 기준 2차 캡 재검증 (심층 방어).
      실사고: 동일 저자 2건 발행 (REPLY_AUTHOR_DAILY_CAP=1 위반).
  R-3 수집 포화 관측 — summary에 collection_saturated / oldest_id 기록.
  R-5 배치 조회 — 정적 필터 통과분으로 CapContext 1회 구성 (DB 3쿼리 고정).
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
from reply_engine import classifier, gate, generator, store, x_client
from reply_engine import filter as filter_mod
from reply_engine.config import (
    PUBLISH_JITTER_MAX_SEC,
    PUBLISH_JITTER_MIN_SEC,
    PUBLISH_START_DELAY_MAX_SEC,
    REPLY_AUTHOR_DAILY_CAP,
    REPLY_CONV_DAILY_CAP,
    REPLY_DAILY_CAP,
    REPLY_RECENT_COMPARE_COUNT,
    STARTUP_JITTER_MAX_SEC,
    get_mode,
    get_my_user_id,
    is_enabled,
)

VERSION = "1.3.0"

_ACCOUNT = "kr_main"  # kr_reply_cursor.account 키

_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    log_file = log_dir / f"reply_{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file)],
    )


def _write_report(summary: dict, guard=None) -> None:
    """실행 요약 JSON 리포트 (artifact 업로드 대상) — 실패해도 파이프라인 무영향.
    guard 전달 시 예산 스냅샷 포함 (B-3).
    """
    if guard is not None:
        summary["budget"] = guard.snapshot()
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"reply_report_{stamp}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        logger.info(f"[Report] 리포트 저장: {path}")
    except Exception as exc:
        logger.warning(f"[Report] 리포트 저장 실패 (무시): {exc}")


def main() -> dict:
    _setup_logging()
    mode = get_mode()
    logger.info(f"[ReplyEngine] v{VERSION} 시작 | mode={mode}")

    summary: dict = {
        "version": VERSION,
        "mode": mode,
        "success": False,
        "exit_reason": None,
        "collected": 0,
        "collection_saturated": False,   # R-3: 수집 상한 포화 (미수집분 존재 가능)
        "oldest_id": None,               # R-3: 유실 구간 사후 추적용
        "candidates": 0,
        "published": 0,
        "skip_reasons": {},
        "review": [],   # C-3: 건별 품질 검수 배열 / C-4(v1.2.1): 분류 스킵 건 포함
        "started_at": datetime.now(UTC).isoformat(),
    }

    def _skip(tweet_id: str, reason: str) -> None:
        summary["skip_reasons"][reason] = summary["skip_reasons"].get(reason, 0) + 1
        logger.info(f"[Skip] {tweet_id}: {reason}")

    # ── Step 0: 게이트 ────────────────────────────────────────
    if not is_enabled():
        logger.warning("[Step0] REPLY_ENABLED != 'true' — 긴급 정지 상태, 종료")
        summary["exit_reason"] = "EXIT_DISABLED"
        _write_report(summary)
        return summary

    if mode == "live" and STARTUP_JITTER_MAX_SEC > 0:
        jitter = random.randint(0, STARTUP_JITTER_MAX_SEC)
        logger.info(f"[Step0] 시작 지터 {jitter}초 대기 (안티봇)")
        time.sleep(jitter)

    db_write_allowed = mode in ("shadow", "live")

    # ── Step 1: 예산 ──────────────────────────────────────────
    today = store.kst_today()
    guard = budget_mod.BudgetGuard(store.get_budget(today))
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

    cursor = store.get_cursor(_ACCOUNT)
    # user_id 우선순위 (B-1): X_MY_USER_ID 변수 > 커서 캐시 > get_me (읽기 1콜)
    my_user_id = get_my_user_id() or (cursor or {}).get("my_user_id") or ""
    since_id = (cursor or {}).get("since_id") or None

    if my_user_id:
        logger.info(f"[Step2] user_id 확보 (get_me 생략): {my_user_id}")
    else:
        my_user_id = x_client.fetch_my_user_id(client) or ""
        guard.record_read()
        if not my_user_id:
            summary["exit_reason"] = "EXIT_GET_ME_FAIL"
            if db_write_allowed:
                store.upsert_budget(guard.row)  # 읽기 1콜 소모분 기록
            _write_report(summary, guard)
            return summary
        # get_me가 읽기 1콜을 소모했으므로 fetch 전 예산 재확인 (R-1)
        if not guard.can_read():
            summary["exit_reason"] = "EXIT_BUDGET"
            if db_write_allowed:
                store.upsert_budget(guard.row)
            _write_report(summary, guard)
            return summary

    fetched = x_client.fetch_mentions(client, my_user_id, since_id)
    guard.record_read()
    if not fetched["success"]:
        # N-1 (2026-08-25): 월간 지출 상한은 재시도로 풀리지 않는 플랫폼 사유 — 구분 보고
        if x_client.is_spend_cap_error(fetched.get("error")):
            logger.error(
                "[Step3] X API 월간 지출 상한 도달 — 코드 문제 아님. "
                "Developer Portal에서 spend cap 확인/상향 필요 (N-1)"
            )
            summary["exit_reason"] = "EXIT_SPEND_CAP"
        else:
            summary["exit_reason"] = "EXIT_FETCH_FAIL"
        if db_write_allowed:
            store.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    tweets = fetched["tweets"]
    users = fetched["users"]
    summary["collected"] = len(tweets)
    summary["collection_saturated"] = bool(fetched.get("saturated", False))   # R-3
    summary["oldest_id"] = fetched.get("oldest_id")
    logger.info(f"[Step2] 수집 {len(tweets)}건")

    # 커서 전진 (L2) — dry_run은 미전진
    if db_write_allowed and fetched["newest_id"]:
        store.upsert_cursor(_ACCOUNT, fetched["newest_id"], my_user_id)

    if not tweets:
        summary["success"] = True
        summary["exit_reason"] = "EXIT_NO_MENTIONS"
        if db_write_allowed:
            store.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    # ── Step 3: 필터 ──────────────────────────────────────────
    blacklist = store.get_blacklist_ids()

    # R-5: 정적 필터를 먼저 통과시킨 뒤 배치 스냅샷을 1회 구성 (DB 3쿼리 고정).
    # 기존에는 후보 N건 × 3쿼리 순차 실행이었다.
    static_ok: list[dict] = []
    for tweet in tweets:
        passed, reason = filter_mod.check_tweet(
            tweet, users.get(tweet["author_id"]), my_user_id, blacklist
        )
        if not passed:
            _skip(tweet["id"], reason)
            continue
        static_ok.append(tweet)

    cap_ctx = filter_mod.build_cap_context(static_ok) if static_ok else None

    # R-2: check_and_admit은 통과 시 in-run 카운터를 점유한다 (부수효과).
    candidates: list[dict] = []
    for tweet in static_ok:
        passed, reason = filter_mod.check_and_admit(tweet, cap_ctx)
        if not passed:
            _skip(tweet["id"], reason)
            continue
        candidates.append(tweet)

    logger.info(f"[Step3] 필터 통과 {len(candidates)}건")

    # ── Step 3.5: 대화 루트 소유자 검증 (P-1, 2026-08-18) ────
    # in_reply_to_user_id 조건만으로는 "내가 타인 글에 단 댓글의 대댓글"이 통과하므로,
    # conversation root(=원 게시글) 작성자가 나인 경우만 응답 대상으로 확정한다.
    if candidates:
        conv_ids = list(dict.fromkeys(t["conversation_id"] for t in candidates))
        roots: dict | None = None
        if guard.can_read():
            roots = x_client.fetch_conversation_roots(client, conv_ids)
            guard.record_read()
        else:
            logger.warning("[Step3.5] 읽기 예산 부족 — 루트 미검증 후보 전량 보수적 스킵")

        verified: list[dict] = []
        for tweet in candidates:
            root_author = (roots or {}).get(tweet["conversation_id"])
            if roots is None or root_author is None:
                _skip(tweet["id"], "THREAD_UNVERIFIED")   # 조회 실패/루트 삭제 → 보수적 스킵
            elif root_author != my_user_id:
                _skip(tweet["id"], "OUT_OF_SCOPE_THREAD")  # 타인 글 스레드 → 응답 금지
            else:
                verified.append(tweet)
        candidates = verified
        logger.info(f"[Step3.5] 루트 검증 통과 {len(candidates)}건")

    summary["candidates"] = len(candidates)

    # ── Step 4: 분류 ──────────────────────────────────────────
    pass_items: list[dict] = []
    labels: dict[str, str] = {}
    if candidates:
        labels = classifier.classify_batch(
            [{"id": t["id"], "text": t["text"]} for t in candidates]
        )
        guard.record_gemini()
        for tweet in candidates:
            label = labels.get(tweet["id"], "AMBIGUOUS")
            if label in classifier.PASS_LABELS:
                pass_items.append({**tweet, "label": label})
            else:
                _skip(tweet["id"], f"CLASS_{label}")
                # C-4 (v1.2.1, 2026-08-27): 분류 스킵 건도 review에 기록 —
                # artifact만으로 분류 품질(오판 여부) 검수 가능하게 함.
                # 스키마는 기존 review_entry와 동일 (reply_text 없음 → None).
                summary["review"].append({
                    "reply_tweet_id": tweet["id"],
                    "comment_preview": tweet["text"][:100],
                    "label": label,
                    "reply_text": None,
                    "result": f"CLASS_{label}",
                })

    logger.info(f"[Step4] 분류 통과 {len(pass_items)}건")

    # ── Step 5: 생성 ──────────────────────────────────────────
    replies: dict[str, str] = {}
    if pass_items:
        replies = generator.generate_batch(
            [{"id": t["id"], "text": t["text"], "label": t["label"]} for t in pass_items]
        )
        guard.record_gemini()

    # ── Step 6~8: 게이트 → 발행 → 기록 ───────────────────────
    recent_texts = store.get_recent_response_texts(REPLY_RECENT_COMPARE_COUNT)
    responded_today = store.count_responded_today()
    published_this_run = 0
    # R-2: 실발행 기준 2차 캡. Step3 승인 시점에 이미 상한이 걸리지만,
    # 게이트 탈락·발행 실패로 승인≠발행이 되는 경로가 있어 심층 방어로 재계수한다.
    published_author_run: dict[str, int] = {}
    published_conv_run: dict[str, int] = {}
    first_publish_delayed = False   # 첫 발행 직전 1회 랜덤 딜레이 (안티봇, 2026-08-17)

    for idx, tweet in enumerate(pass_items):
        tweet_id = tweet["id"]
        author_id = tweet["author_id"]
        conversation_id = tweet["conversation_id"]
        reply_text = (replies.get(tweet_id) or "").strip()

        # 발행 가능 여부 판정 → skip_reason 확정 (DB에 사유까지 기록 — 감사추적)
        skip_reason: str | None = None
        if responded_today + published_this_run >= REPLY_DAILY_CAP:
            skip_reason = "DAILY_CAP"
        elif published_author_run.get(author_id, 0) >= REPLY_AUTHOR_DAILY_CAP:
            skip_reason = "AUTHOR_CAP_RUN"      # R-2 2차 방어선
        elif published_conv_run.get(conversation_id, 0) >= REPLY_CONV_DAILY_CAP:
            skip_reason = "CONV_CAP_RUN"        # R-2 2차 방어선
        else:
            gate_ok, gate_reason = gate.check_reply(
                reply_text, recent_texts, comment_text=tweet["text"]
            )
            # F-2 (2026-08-20): 배치 내 동일 문구 연쇄 생성으로 인한 유사도 탈락 시,
            # 결정적 seed 풀 문구로 1회 한정 교체 후 게이트 전체 재검사 (커버리지 회복)
            if not gate_ok and gate_reason == "GATE_SIMILARITY":
                fallback_text = generator.pick_fallback(tweet["label"], tweet_id)
                fb_ok, _fb_reason = gate.check_reply(
                    fallback_text, recent_texts, comment_text=tweet["text"]
                )
                if fb_ok:
                    logger.info(
                        f"[Gate] 유사도 탈락 → 풀 fallback 대체: '{reply_text}' → '{fallback_text}'"
                    )
                    reply_text = fallback_text
                    gate_ok, gate_reason = True, None
            if not gate_ok:
                skip_reason = gate_reason
            elif mode == "live" and not guard.can_write():
                skip_reason = "BUDGET_WRITE"

        # 이력 기록 (L1) — dry_run은 DB 쓰기 금지
        record = {
            "reply_tweet_id": tweet_id,
            "conversation_id": conversation_id,
            "author_id": author_id,
            "author_username": users.get(author_id, {}).get("username", ""),
            "comment_text": tweet["text"][:500],
            "classification": tweet["label"],
            "responded": False,
            "skip_reason": skip_reason,
            "response_text": reply_text,
            "response_tweet_id": None,
            "dry_run": mode != "live",
            "mode": mode,
        }

        review_entry = {
            "reply_tweet_id": tweet_id,
            "comment_preview": tweet["text"][:100],
            "label": tweet["label"],
            "reply_text": reply_text,
            "result": None,
        }
        summary["review"].append(review_entry)

        if db_write_allowed:
            if not store.insert_history(record):
                # INSERT 실패(PK 충돌 포함) → 발행 금지 (L1 최종 방어)
                review_entry["result"] = "HISTORY_INSERT_FAIL"
                _skip(tweet_id, "HISTORY_INSERT_FAIL")
                continue

        if skip_reason:
            review_entry["result"] = skip_reason
            _skip(tweet_id, skip_reason)
            continue

        if mode != "live":
            logger.info(f"[{mode.upper()}] 발행 시뮬레이션: '{reply_text}' → {tweet_id}")
            review_entry["result"] = "SIMULATED"
            recent_texts.append(reply_text)
            published_this_run += 1
            # R-2: shadow에서도 캡이 실동작해야 검수가 유효하다
            published_author_run[author_id] = published_author_run.get(author_id, 0) + 1
            published_conv_run[conversation_id] = published_conv_run.get(conversation_id, 0) + 1
            continue

        # live: 첫 발행 직전 랜덤 딜레이 0~PUBLISH_START_DELAY_MAX_SEC (안티봇)
        # 발행 대상이 실제로 확정된 시점에만 대기 — 전량 스킵 실행에서는 대기 없음
        if not first_publish_delayed:
            first_publish_delayed = True
            delay = random.randint(0, PUBLISH_START_DELAY_MAX_SEC)
            logger.info(f"[Step7] 첫 발행 랜덤 딜레이 {delay}초 대기 (안티봇)")
            time.sleep(delay)

        # live: 발행 → 즉시 기록 (발행-기록 짝)
        response_tweet_id = x_client.post_reply(client, reply_text, tweet_id)
        guard.record_write()
        store.upsert_budget(guard.row)  # V-1: 발행마다 즉시 저장 (timeout 킬 시 집계 유실 방지)

        if response_tweet_id:
            store.mark_responded(tweet_id, response_tweet_id)
            review_entry["result"] = "PUBLISHED"
            recent_texts.append(reply_text)
            published_this_run += 1
            # R-2: 실발행 기준 캡 계수 (동일 저자·대화 중복 발행 차단)
            published_author_run[author_id] = published_author_run.get(author_id, 0) + 1
            published_conv_run[conversation_id] = published_conv_run.get(conversation_id, 0) + 1
        else:
            store.update_skip_reason(tweet_id, "PUBLISH_FAIL")  # 사유 사후 기록 (R-2)
            review_entry["result"] = "PUBLISH_FAIL"
            _skip(tweet_id, "PUBLISH_FAIL")

        # 발행 간 지터 (마지막 건 제외)
        if idx < len(pass_items) - 1:
            time.sleep(random.randint(PUBLISH_JITTER_MIN_SEC, PUBLISH_JITTER_MAX_SEC))

    summary["published"] = published_this_run

    # ── Step 8: 예산 저장 + 리포트 ────────────────────────────
    if db_write_allowed:
        store.upsert_budget(guard.row)

    summary["success"] = True
    summary["exit_reason"] = "EXIT_OK"
    logger.info(
        f"[ReplyEngine] 완료 | 수집={summary['collected']} 후보={summary['candidates']} "
        f"발행={published_this_run} skip={summary['skip_reasons']}"
    )
    _write_report(summary, guard)
    return summary


if __name__ == "__main__":
    result = main()
    # 파이프라인 자체 실패(수집 불가 등)만 비정상 종료. 발행 0건은 정상.
    fail_reasons = {"EXIT_NO_CREDENTIALS", "EXIT_GET_ME_FAIL", "EXIT_FETCH_FAIL"}
    sys.exit(1 if result.get("exit_reason") in fail_reasons else 0)
