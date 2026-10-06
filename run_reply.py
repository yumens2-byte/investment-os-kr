"""
X Reply Engine — 메인 파이프라인 (v2.0.0)
==============================================
내 게시글에 달린 댓글(멘션 타임라인) 수집 → 필터 → 루트검증 → 분류 → 생성 → 게이트 → 답글 발행.
스코프: conversation root(원 게시글) 작성자가 내 계정인 스레드만 (P-1, 2026-08-18).

[정책 요약]
- 무응답이 기본값 (default-deny): POSITIVE / SUPPORTIVE_NEUTRAL만 답글
- 답글은 공백 포함 40자 이내 감사·호응만 (자연스럽고 관련성 있게)
- 결과 불명 발행 재시도 금지, 명시적 429 거절만 제한 복구
- 24시간 경과 댓글 자동 폐기 (승인 D)

[모드 — REPLY_MODE]
  dry_run — 수집/분류/생성/게이트까지. DB 쓰기·X 발행 전면 금지, 커서 미전진
  shadow  — DB 기록 O (mode='shadow'), X 발행 X. 독립 shadow 커서로 검수
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

v1.5.0 (2026-08-30, R-10/B):
  R-10 user_id 무결성 — X_MY_USER_ID와 커서 캐시 불일치를 경고로 노출한다.
       B-1 규약(변수 > 커서)상 계정 교체 시 불일치는 정상이므로 중단하지 않는다.
       커서 정체(updated_at) 관측으로 "정상 0건"과 "이상 0건"을 구분한다.
  B안  타인 스레드 응답 — 회당 저상한 내 허용. P-1 실사고(주객전도) 방어선을
       해제하는 변경이므로 기본 비활성(opt-in)이며 Variables로만 켠다.

v1.4.0 (2026-08-30, R-9):
  외국어 댓글은 AI 생성 대신 정형 문구를 사용한다 (마스터 확정 C안).
  AI 생성 대상이 0건이면 Gemini 호출이 없으므로 예산 계상도 하지 않는다.
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.alert import send_admin_alert
from reply_engine import budget as budget_mod
from reply_engine import classifier, gate, generator, lang, store, telemetry, x_client
from reply_engine import filter as filter_mod
from reply_engine.config import (
    MENTIONS_MAX_PAGES,
    PUBLISH_JITTER_MAX_SEC,
    PUBLISH_JITTER_MIN_SEC,
    PUBLISH_START_DELAY_MAX_SEC,
    REPLY_AUTHOR_DAILY_CAP,
    REPLY_CONV_DAILY_CAP,
    REPLY_CURSOR_STALE_WARN_HOURS,
    REPLY_DAILY_CAP,
    REPLY_FOREIGN_THREAD_ENABLED,
    REPLY_FOREIGN_THREAD_RUN_CAP,
    REPLY_LIKE_PER_DAY,
    REPLY_LIKE_PER_RUN,
    REPLY_RECENT_COMPARE_COUNT,
    REPLY_RUN_CAP,
    STARTUP_JITTER_MAX_SEC,
    env_bool,
    env_int_clamped,
    get_mode,
    get_my_user_id,
    is_enabled,
    is_like_enabled,
)
from reply_engine.policy import (
    DEFER_REASONS,
    classify_publish_error,
    decode_metadata,
    encode_metadata,
)

VERSION = "2.1.0"

_ACCOUNT = "kr_main"  # kr_reply_cursor.account 키

_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

logger = logging.getLogger(__name__)

# 테스트와 기존 통합 코드가 ``x_client.post_reply``를 교체하는 규약을 보존하면서,
# 운영 기본 경로에서는 오류 원문까지 회수하기 위한 기준 참조다.
_DEFAULT_POST_REPLY = x_client.post_reply
_DEFAULT_FETCH_MENTIONS = x_client.fetch_mentions
_DEFAULT_FETCH_ROOTS = x_client.fetch_conversation_roots


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
    collected = int(summary.get("collected") or 0)
    processed = int(summary.get("processed", collected) or 0)
    candidates = int(summary.get("candidates") or 0)
    classified_pass = int(summary.get("classified_pass") or 0)
    published = int(summary.get("actual_published", summary.get("published")) or 0)
    if summary.get("mode") in {"dry_run", "shadow"}:
        published = 0
    summary["funnel"] = {
        "candidate_rate": round(candidates / processed, 4) if processed else 0.0,
        "classification_pass_rate": (round(classified_pass / candidates, 4) if candidates else 0.0),
        "publish_rate_of_processed": round(published / processed, 4) if processed else 0.0,
        "publish_rate_of_collected": (
            round(published / collected, 4)
            if collected and not summary.get("recovered_failures")
            else None
        ),
        "publish_rate_of_pass": round(published / classified_pass, 4) if classified_pass else 0.0,
    }
    review = summary.get("review", [])
    summary["cohorts"] = {
        origin: {
            "reviewed": sum(r.get("origin") == origin for r in review),
            "published": sum(r.get("origin") == origin and r.get("result") in {
                "PUBLISHED", "PUBLISHED_DB_UNCONFIRMED"
            } for r in review),
            "simulated": sum(r.get("origin") == origin and r.get("result") == "SIMULATED"
                             for r in review),
        } for origin in ("new", "recovered")
    }
    summary["finished_at"] = datetime.now(UTC).isoformat()
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


def _cursor_stale_hours(cursor: dict | None) -> int | None:
    """
    커서 updated_at 기준 경과 시간(시). 값이 없거나 파싱 실패 시 None (R-10).
    관측 지표이므로 어떤 예외도 파이프라인을 중단시키지 않는다.
    """
    raw = (cursor or {}).get("updated_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, int((datetime.now(UTC) - parsed).total_seconds() // 3600))
    except (ValueError, TypeError) as exc:
        logger.warning(f"[Step2] 커서 updated_at 파싱 실패 (관측 생략): {exc}")
        return None


def main() -> dict:
    _setup_logging()
    mode = get_mode()
    logger.info(f"[ReplyEngine] v{VERSION} 시작 | mode={mode}")

    summary: dict = {
        **telemetry.new_run(),
        "version": VERSION,
        "mode": mode,
        "success": False,
        "exit_reason": None,
        "collected": 0,
        "collection_saturated": False,  # R-3: 수집 상한 포화 (미수집분 존재 가능)
        "collection_pages": 0,
        "cursor_advanced": False,
        "oldest_id": None,  # R-3: 유실 구간 사후 추적용
        "candidates": 0,
        "classified_pass": 0,
        "non_kr_replies": 0,  # R-9: 정형 문구로 처리된 외국어 건수
        "foreign_thread_replies": 0,  # B안: 타인 스레드 응답 건수
        "cursor_stale_hours": None,  # R-10: 커서 정체 시간 (0건 원인 구분용)
        "user_id_mismatch": False,  # R-10: 변수 vs 커서 캐시 불일치 경고
        "published": 0,
        "likes": {"targets": 0, "liked": 0, "skipped": {}},
        "skip_reasons": {},
        "review": [],  # C-3: 건별 품질 검수 배열 / C-4(v1.2.1): 분류 스킵 건 포함
        "history_metrics": None,  # 최근 DB 이력 기반 실제 응답 전환율
        "recovered_failures": 0,
        "deferred": 0,
        "model_usage": [],  # 커서 뒤 DB에서 복구한 live 발행 실패
        "started_at": datetime.now(UTC).isoformat(),
    }

    def _skip(tweet_id: str, reason: str) -> None:
        summary["skip_reasons"][reason] = summary["skip_reasons"].get(reason, 0) + 1
        logger.info(f"[Skip] {tweet_id}: {reason}")
        telemetry.event(summary, reason, tweet_id)

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
    try:
        guard = budget_mod.BudgetGuard(store.get_budget(today))
    except Exception as exc:
        logger.error("[Step1] 예산 확인 실패 — 외부 호출 중단: %s", exc)
        summary["exit_reason"] = "EXIT_BUDGET_UNAVAILABLE"
        _write_report(summary)
        return summary
    summary["history_metrics"] = store.get_history_metrics(days=7)
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

    cursor_account = _ACCOUNT if mode != "shadow" else f"{_ACCOUNT}:shadow"
    cursor = store.get_cursor(cursor_account)
    # user_id 우선순위 (B-1): X_MY_USER_ID 변수 > 커서 캐시 > get_me (읽기 1콜)
    env_user_id = get_my_user_id()
    cached_user_id = (cursor or {}).get("my_user_id") or ""

    # R-10: 변수와 커서 캐시가 다르면 오등록 가능성을 경고한다.
    # 중단하지는 않는다 — B-1 규약(변수 > 커서 캐시)상 계정 교체 시 불일치는
    # 정상 시나리오이며, 중단시키면 교체 자체가 불가능해진다.
    # 오등록이면 남의 멘션이 전건 OUT_OF_SCOPE로 걸러져 '에러 없이 0건'이 되므로,
    # 로그와 리포트에 흔적을 남겨 조기 발견을 돕는 것이 목적이다.
    user_id_mismatch = bool(env_user_id and cached_user_id and env_user_id != cached_user_id)
    summary["user_id_mismatch"] = user_id_mismatch
    if user_id_mismatch:
        logger.warning(
            f"[Step2] user_id 불일치 — X_MY_USER_ID={env_user_id} vs "
            f"커서 캐시={cached_user_id}. 계정 교체가 아니라면 변수 오등록이다 (R-10)"
        )

    my_user_id = env_user_id or cached_user_id
    since_id = (cursor or {}).get("since_id") or None

    # R-10: 커서는 신규 멘션이 있을 때만 전진한다. 정체가 길면 '정상 0건'이 아니라
    # 수집 경로 이상일 수 있으므로 관측 지표로 남긴다 (읽기 콜 0).
    stale_hours = _cursor_stale_hours(cursor)
    summary["cursor_stale_hours"] = stale_hours
    if stale_hours is not None and stale_hours >= REPLY_CURSOR_STALE_WARN_HOURS:
        logger.warning(
            f"[Step2] 커서 {stale_hours}시간째 미전진 — 장시간 신규 멘션 없음. "
            "X_MY_USER_ID 및 계정 상태 점검 권장 (R-10)"
        )

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

    if mode == "live":
        summary["expiry_maintenance"] = store.expire_deferred(my_user_id)
        if summary["expiry_maintenance"].get("errors"):
            send_admin_alert("Reply deferred expiry maintenance failed; see run report")

    # 운영 기본 경로는 DB의 당일 예산 잔여량 안에서만 추가 페이지를 읽는다.
    # 대화 루트 소유자 검증 1콜을 남겨 수집만 성공하고 전건 미검증 스킵되는 상황을 막는다.
    # 테스트/통합 코드가 레거시 3-인자 함수를 교체한 경우에는 기존 규약을 보존한다.
    if x_client.fetch_mentions is _DEFAULT_FETCH_MENTIONS:
        available_reads = guard.available_read_calls(MENTIONS_MAX_PAGES + 1)
        fetch_pages = max(1, min(MENTIONS_MAX_PAGES, available_reads - 1))
        fetched = x_client.fetch_mentions(client, my_user_id, since_id, max_pages=fetch_pages)
    else:
        fetched = x_client.fetch_mentions(client, my_user_id, since_id)
    for _ in range(max(1, int(fetched.get("pages_fetched", 1)))):
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
    seen_at = datetime.now(UTC).isoformat()
    for tweet in tweets:
        tweet.update(_run_id=summary["run_id"], _first_seen_at=seen_at,
                     _user_snapshot=users.get(tweet["author_id"]))
    retry_rows = store.get_retryable_history(100) if mode == "live" else []
    summary["collected"] = len(tweets)
    summary["collection_saturated"] = bool(fetched.get("saturated", False))  # R-3
    summary["oldest_id"] = fetched.get("oldest_id")
    logger.info(f"[Step2] 수집 {len(tweets)}건")

    # 좋아요는 답글 필터와 독립이며 SELF/블랙리스트/기처리만 제외한다.
    if is_like_enabled():
        like_summary = summary["likes"]
        blacklist_for_like = store.get_blacklist_ids()
        existing_likes = store.get_existing_like_ids([t["id"] for t in tweets])
        eligible = [
            t
            for t in tweets
            if t.get("author_id") != my_user_id
            and t.get("author_id") not in blacklist_for_like
            and t["id"] not in existing_likes
        ]
        like_summary["targets"] = min(len(eligible), REPLY_LIKE_PER_RUN)
        already = sum(t["id"] in existing_likes for t in tweets)
        if already:
            like_summary["skipped"]["ALREADY"] = already
        if len(eligible) > REPLY_LIKE_PER_RUN:
            like_summary["skipped"]["RUN_CAP"] = len(eligible) - REPLY_LIKE_PER_RUN
        day_left = max(0, REPLY_LIKE_PER_DAY - store.count_likes_today())
        targets = eligible[:REPLY_LIKE_PER_RUN]
        if day_left < len(targets):
            like_summary["skipped"]["DAY_CAP"] = len(targets) - day_left
            targets = targets[:day_left]
        for target_index, tweet in enumerate(targets):
            if mode == "dry_run":
                like_summary["liked"] += 1
                continue
            record = {
                "reply_tweet_id": tweet["id"],
                "author_id": tweet.get("author_id", ""),
                "mode": mode,
                "would_like": mode == "shadow",
            }
            if mode == "live":
                if not guard.can_write():
                    remaining = len(targets) - target_index
                    like_summary["skipped"]["BUDGET_WRITE"] = remaining
                    break
                ok, error = x_client.post_like(client, tweet["id"])
                guard.record_write()
                store.upsert_budget(guard.row)
                if not ok:
                    if x_client.is_spend_cap_error(error):
                        like_summary["spend_cap"] = True
                        break
                    skipped = like_summary["skipped"]
                    skipped["LIKE_FAIL"] = skipped.get("LIKE_FAIL", 0) + 1
                    continue
                record["liked_at"] = datetime.now(UTC).isoformat()
            if store.insert_like(record):
                like_summary["liked"] += 1

    # 커서 정보는 여기서 계산만 한다. 실제 전진은 모든 후보 처리가 끝난 뒤 수행한다.
    # 수집 직후 전진하면 분류/생성/이력 저장 중 프로세스가 종료됐을 때 아직 DB에
    # 기록되지 않은 멘션이 커서 뒤로 영구 유실될 수 있다.
    summary["collection_pages"] = int(fetched.get("pages_fetched", 1))
    collection_complete = bool(fetched.get("collection_complete", not fetched.get("saturated")))
    cursor_can_advance = bool(db_write_allowed and fetched["newest_id"] and collection_complete)
    cursor_safe_to_advance = cursor_can_advance
    if db_write_allowed and fetched["newest_id"] and not collection_complete:
        logger.warning("[Step2] 수집 미완료로 cursor를 보존한다 — 다음 실행에서 backlog 재수집")

    if not tweets and not retry_rows:
        if cursor_can_advance:
            summary["cursor_advanced"] = store.upsert_cursor(
                cursor_account, fetched["newest_id"], my_user_id
            )
        summary["success"] = True
        summary["exit_reason"] = "EXIT_NO_MENTIONS"
        if db_write_allowed:
            store.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    current_tweets = {tweet["id"]: tweet for tweet in tweets}
    current_ids = set(current_tweets)
    for row in retry_rows:
        meta = decode_metadata(row.get("error_message"))
        if meta.get("account_user_id") != my_user_id:
            continue
        tid = str(row["reply_tweet_id"])
        if tid in current_ids:
            current_tweets[tid]["_metadata"] = meta
            continue
        tweets.append(
            {
                "id": tid,
                "text": meta.get("comment_text") or row.get("comment_text") or "",
                "author_id": str(row.get("author_id") or ""),
                "conversation_id": str(row.get("conversation_id") or ""),
                "in_reply_to_user_id": meta.get("in_reply_to_user_id", ""),
                "created_at": meta.get("original_created_at"),
                "parent_text": meta.get("parent_text", ""),
                "parent_id": meta.get("parent_id", ""),
                "parent_author_id": meta.get("parent_author_id", ""),
                "_metadata": meta,
                "_retry": True,
                "_run_id": summary["run_id"],
                "_stored_response_text": row.get("response_text") or "",
            }
        )
        current_ids.add(tid)
        snapshot = meta.get("user_snapshot")
        if isinstance(snapshot, dict):
            users.setdefault(str(row.get("author_id") or ""), snapshot)
    # Oldest first; a bounded queue query prevents unbounded work per invocation.
    tweets.sort(key=lambda t: store.parse_utc(t.get("created_at"))
                or datetime.min.replace(tzinfo=UTC))
    summary["recovered_failures"] = sum(bool(t.get("_retry")) for t in tweets)
    summary["processed"] = len(tweets)

    def _record_skip(tweet: dict, reason: str, label: str = "AMBIGUOUS") -> None:
        nonlocal cursor_safe_to_advance
        _skip(tweet["id"], reason)
        summary["deferred"] += int(reason in DEFER_REASONS)
        summary["review"].append(
            {
                "reply_tweet_id": tweet["id"],
                "origin": "recovered" if tweet.get("_retry") else "new",
                "comment_preview": tweet["text"][:100],
                "parent_preview": tweet.get("parent_text", "")[:160],
                "label": label,
                "reply_text": None,
                "result": reason,
                "foreign_thread": bool(tweet.get("foreign_thread")),
            }
        )
        # Published/unknown rows must never be overwritten merely to log a duplicate.
        if not db_write_allowed or reason == "DUP":
            return
        record = {
            "reply_tweet_id": tweet["id"],
            "conversation_id": tweet["conversation_id"],
            "author_id": tweet["author_id"],
            "author_username": users.get(tweet["author_id"], {}).get("username", ""),
            "comment_text": tweet["text"][:500],
            "classification": label,
            "responded": False,
            "response_tweet_id": None,
            "response_text": "",
            "skip_reason": reason,
            "dry_run": mode != "live",
            "mode": mode,
            "error_message": encode_metadata({**tweet, "_account_user_id": my_user_id}),
        }
        if not store.insert_history(record):
            cursor_safe_to_advance = False
            _skip(tweet["id"], "HISTORY_INSERT_FAIL")

    # ── Step 3: 필터 ──────────────────────────────────────────
    blacklist = store.get_blacklist_ids()

    # R-5: 정적 필터를 먼저 통과시킨 뒤 배치 스냅샷을 1회 구성 (DB 3쿼리 고정).
    # 기존에는 후보 N건 × 3쿼리 순차 실행이었다.
    static_ok: list[dict] = []
    existing_ids = store.history_exists_bulk([tweet["id"] for tweet in tweets])
    for tweet in tweets:
        # Already durable publication states take precedence over expiry/spam changes.
        # Attempting to replace them with a fresh skip would fail CAS and stall the cursor.
        if tweet["id"] in existing_ids:
            _record_skip(tweet, "DUP")
            continue
        passed, reason = filter_mod.check_tweet(
            tweet, users.get(tweet["author_id"]), my_user_id, blacklist
        )
        if not passed:
            _record_skip(tweet, reason)
            continue
        static_ok.append(tweet)

    cap_ctx = filter_mod.build_cap_context(static_ok, existing_ids) if static_ok else None

    # 이 단계는 중복만 확인한다. 슬롯 예약은 게이트 통과 후 수행한다.
    candidates: list[dict] = []
    for tweet in static_ok:
        passed, reason = filter_mod.check_duplicate(tweet, cap_ctx)
        if not passed:
            _record_skip(tweet, reason)
            continue
        previous = cap_ctx.history_rows.get(tweet["id"], {})
        metadata = decode_metadata(previous.get("error_message"))
        if metadata:
            tweet["_metadata"] = metadata
        candidates.append(tweet)

    logger.info(f"[Step3] 필터 통과 {len(candidates)}건")

    # ── Step 3.5: 대화 루트 소유자 검증 (P-1, 2026-08-18) ────
    # in_reply_to_user_id 조건만으로는 "내가 타인 글에 단 댓글의 대댓글"이 통과하므로,
    # conversation root(=원 게시글) 작성자가 나인 경우만 응답 대상으로 확정한다.
    if candidates:
        conv_ids = list(dict.fromkeys(t["conversation_id"] for t in candidates))
        roots: dict | None = None
        if guard.can_read():
            if x_client.fetch_conversation_roots is _DEFAULT_FETCH_ROOTS:
                roots = x_client.fetch_conversation_roots(
                    client, conv_ids, max_calls=guard.available_read_calls(3)
                )
            else:
                roots = x_client.fetch_conversation_roots(client, conv_ids)
            for _ in range(getattr(roots, "api_calls", 1)):
                guard.record_read()
        else:
            logger.warning("[Step3.5] 읽기 예산 부족 — 루트 미검증 후보 전량 보수적 스킵")

        verified: list[dict] = []
        foreign_admitted = 0
        for tweet in candidates:
            root_author = (roots or {}).get(tweet["conversation_id"])
            tweet["root_author_id"] = root_author
            if roots is None or root_author is None:
                _record_skip(tweet, "THREAD_UNVERIFIED")  # 조회 실패/루트 삭제 → 보수적 스킵
            elif root_author != my_user_id:
                # B안 (2026-08-30): 이 건은 in_reply_to_user_id == 나를 이미 통과했다.
                # 즉 '나에게 직접 말을 건' 댓글이며, 원 게시글만 타인 것이다.
                # 남의 스레드 자동 답글은 스팸으로 비칠 수 있어 회당 저상한을 둔다.
                if not REPLY_FOREIGN_THREAD_ENABLED:
                    _record_skip(tweet, "OUT_OF_SCOPE_THREAD")
                elif tweet.get("parent_author_id") != my_user_id:
                    _record_skip(tweet, "THREAD_UNVERIFIED")
                else:
                    foreign_admitted += 1
                    tweet["foreign_thread"] = True
                    verified.append(tweet)
            else:
                verified.append(tweet)
        candidates = verified
        summary["foreign_thread_replies"] = foreign_admitted
        logger.info(
            f"[Step3.5] 루트 검증 통과 {len(candidates)}건 (타인 스레드 {foreign_admitted}건)"
        )

    summary["candidates"] = len(candidates)

    # ── Step 4: 분류 ──────────────────────────────────────────
    pass_items: list[dict] = []
    labels: dict[str, str] = {}
    if candidates:
        labels = classifier.classify_batch(
            [
                {
                    "id": t["id"],
                    "text": t["text"],
                    "parent_text": t.get("parent_text", ""),
                    "foreign_thread": bool(t.get("foreign_thread")),
                }
                for t in candidates
            ]
        )
        for _ in range(getattr(labels, "api_calls", 0)):
            guard.record_gemini()
        summary["model_usage"].extend(getattr(labels, "usage", []))
        for tweet in candidates:
            label = labels.get(tweet["id"], "AMBIGUOUS")
            if label in classifier.PASS_LABELS:
                pass_items.append({**tweet, "label": label})
            else:
                reason = f"CLASS_{label}"
                if tweet["id"] in getattr(labels, "unavailable_ids", set()):
                    reason = "CLASSIFIER_UNAVAILABLE"
                    meta = dict(tweet.get("_metadata") or {})
                    meta["classification_attempts"] = (
                        int(meta.get("classification_attempts", 0)) + 1
                    )
                    meta["next_attempt_at"] = (
                        datetime.now(UTC) + timedelta(minutes=15)
                    ).isoformat()
                    tweet["_metadata"] = meta
                    if meta["classification_attempts"] >= 3:
                        reason = "CLASSIFIER_EXHAUSTED"
                _record_skip(tweet, reason, label)

    logger.info(f"[Step4] 분류 통과 {len(pass_items)}건")
    summary["classified_pass"] = len(pass_items)

    run_cap = REPLY_RUN_CAP
    if env_bool("REPLY_URGENT_DRAIN_ENABLED", False) and any(
        (original := store.parse_utc(t.get("created_at"))) is not None
        and timedelta(hours=max(0, filter_mod.REPLY_MAX_AGE_HOURS - 6))
        <= datetime.now(UTC) - original < timedelta(hours=filter_mod.REPLY_MAX_AGE_HOURS)
        for t in pass_items
    ):
        run_cap = max(run_cap, env_int_clamped("REPLY_URGENT_RUN_CAP", 4, 1, 10))
    summary["effective_run_cap"] = run_cap

    # ── Step 5: 생성 ──────────────────────────────────────────
    replies: dict[str, str] = {}
    reply_sources: dict[str, str] = {}
    generated_ids: set[str] = set()
    summary["non_kr_replies"] = sum(lang.is_non_korean(t["text"]) for t in pass_items)

    def generate_window(index: int, capacity: int) -> None:
        # Generate only a bounded window that can fit the remaining publication slots.
        window = [t for t in pass_items[index:index + capacity] if t["id"] not in generated_ids]
        if not window:
            return
        batch = generator.generate_batch(window)
        replies.update(batch)
        reply_sources.update(getattr(batch, "sources", {}))
        generated_ids.update(t["id"] for t in window)
        for _ in range(getattr(batch, "api_calls", 0)):
            guard.record_gemini()
        summary["model_usage"].extend(getattr(batch, "usage", []))
        for item in window:
            if item.get("_retry") and item.get("_stored_response_text"):
                replies[item["id"]] = item["_stored_response_text"]

    # ── Step 6~8: 게이트 → 발행 → 기록 ───────────────────────
    recent_texts = store.get_recent_response_texts(REPLY_RECENT_COMPARE_COUNT)
    responded_today = store.count_responded_today()
    published_this_run = 0
    publish_attempts_this_run = 0
    quota_used_this_run = 0
    foreign_reserved_this_run = 0
    # R-2: 실발행 기준 2차 캡. Step3 승인 시점에 이미 상한이 걸리지만,
    # 게이트 탈락·발행 실패로 승인≠발행이 되는 경로가 있어 심층 방어로 재계수한다.
    published_author_run: dict[str, int] = {}
    published_conv_run: dict[str, int] = {}
    foreign_published = 0
    first_publish_delayed = False  # 첫 발행 직전 1회 부하 분산 딜레이

    for idx, tweet in enumerate(pass_items):
        tweet_id = tweet["id"]
        author_id = tweet["author_id"]
        conversation_id = tweet["conversation_id"]
        reply_text = (replies.get(tweet_id) or "").strip()
        if tweet.get("_retry"):
            response_source = "DB_RETRY"
        else:
            response_source = getattr(replies, "sources", {}).get(
                tweet_id, "TEMPLATE_NON_KR" if lang.is_non_korean(tweet["text"]) else "AI"
            )

        # 발행 가능 여부 판정 → skip_reason 확정 (DB에 사유까지 기록 — 감사추적)
        draft_gate_reason = None
        reserved = False
        skip_reason: str | None = None
        if max(published_this_run, publish_attempts_this_run) >= run_cap:
            skip_reason = "RUN_CAP"
        elif responded_today + quota_used_this_run >= REPLY_DAILY_CAP:
            skip_reason = "DAILY_CAP"
        elif (tweet.get("foreign_thread")
              and foreign_reserved_this_run >= REPLY_FOREIGN_THREAD_RUN_CAP):
            skip_reason = "FOREIGN_THREAD_CAP"
        elif published_author_run.get(author_id, 0) >= REPLY_AUTHOR_DAILY_CAP:
            skip_reason = "AUTHOR_CAP_RUN"  # R-2 2차 방어선
        elif published_conv_run.get(conversation_id, 0) >= REPLY_CONV_DAILY_CAP:
            skip_reason = "CONV_CAP_RUN"  # R-2 2차 방어선
        elif mode == "live" and not guard.can_write():
            skip_reason = "BUDGET_WRITE"
        else:
            if tweet_id not in generated_ids:
                capacity = max(1, min(
                    run_cap - max(published_this_run, publish_attempts_this_run),
                    REPLY_DAILY_CAP - responded_today - quota_used_this_run,
                ))
                generate_window(idx, capacity)
            reply_text = (replies.get(tweet_id) or "").strip()
            if not tweet.get("_retry"):
                response_source = reply_sources.get(
                    tweet_id, "TEMPLATE_NON_KR" if lang.is_non_korean(tweet["text"]) else "AI"
                )
            gate_ok, gate_reason = gate.check_reply(
                reply_text, recent_texts, comment_text=tweet["text"]
            )
            draft_gate_reason = gate_reason
            # 배치 내 동일 문구 연쇄 생성으로 유사도 탈락 시, 결정적 순서의 안전 풀을
            # 순회하며 게이트 전체를 재검사한다. 전부 탈락할 때만 무응답 처리한다.
            if not gate_ok:
                for fallback_text in generator.contextual_fallbacks(tweet):
                    fb_ok, _fb_reason = gate.check_reply(
                        fallback_text, recent_texts, comment_text=tweet["text"]
                    )
                    if not fb_ok:
                        continue
                    logger.info(
                        f"[Gate] 초안 {draft_gate_reason} → 안전 fallback 대체: "
                        f"'{reply_text}' → '{fallback_text}'"
                    )
                    reply_text = fallback_text
                    response_source = "TEMPLATE_FALLBACK"
                    gate_ok, gate_reason = True, None
                    break
            if not gate_ok:
                skip_reason = gate_reason
            elif mode == "live" and not guard.can_write():
                skip_reason = "BUDGET_WRITE"
            else:
                admitted, reason = filter_mod.check_and_admit(tweet, cap_ctx)
                reserved = admitted
                if not admitted:
                    skip_reason = reason

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
            "error_message": encode_metadata({**tweet, "_account_user_id": my_user_id}),
        }

        review_entry = {
            "reply_tweet_id": tweet_id,
            "origin": "recovered" if tweet.get("_retry") else "new",
            "comment_preview": tweet["text"][:100],
            "parent_preview": tweet.get("parent_text", "")[:160],
            "label": tweet["label"],
            "reply_text": reply_text,
            "source": response_source,
            "draft_gate_reason": draft_gate_reason,
            "foreign_thread": bool(tweet.get("foreign_thread")),
            "result": None,
        }
        summary["review"].append(review_entry)

        if db_write_allowed:
            if not store.insert_history(record):
                if reserved:
                    filter_mod.release_admission(tweet, cap_ctx)
                # 조건부 이력 저장 실패 → 발행 금지 (L1 방어)
                cursor_safe_to_advance = False
                review_entry["result"] = "HISTORY_INSERT_FAIL"
                _skip(tweet_id, "HISTORY_INSERT_FAIL")
                continue

        if skip_reason:
            review_entry["result"] = skip_reason
            _skip(tweet_id, skip_reason)
            summary["deferred"] += int(skip_reason in DEFER_REASONS)
            continue

        if mode != "live":
            logger.info(f"[{mode.upper()}] 발행 시뮬레이션: '{reply_text}' → {tweet_id}")
            review_entry["result"] = "SIMULATED"
            recent_texts.append(reply_text)
            published_this_run += 1
            quota_used_this_run += 1
            foreign_reserved_this_run += int(bool(tweet.get("foreign_thread")))
            foreign_published += int(bool(tweet.get("foreign_thread")))
            # R-2: shadow에서도 캡이 실동작해야 검수가 유효하다
            published_author_run[author_id] = published_author_run.get(author_id, 0) + 1
            published_conv_run[conversation_id] = published_conv_run.get(conversation_id, 0) + 1
            continue

        metadata = encode_metadata({**tweet, "_account_user_id": my_user_id}, publish_attempt=True)
        claimed_metadata = store.claim_publication(tweet_id, metadata)
        if not claimed_metadata:
            filter_mod.release_admission(tweet, cap_ctx)
            review_entry["result"] = "PUBLISH_CLAIM_FAIL"
            _skip(tweet_id, "PUBLISH_CLAIM_FAIL")
            cursor_safe_to_advance = False
            continue

        if isinstance(claimed_metadata, str):
            metadata = claimed_metadata

        # live: 동시 실행/순간 부하를 줄이는 첫 발행 지연. 탐지 회피 수단이 아니다.
        # 발행 대상이 실제로 확정된 시점에만 대기 — 전량 스킵 실행에서는 대기 없음
        if not first_publish_delayed:
            first_publish_delayed = True
            delay = random.randint(0, PUBLISH_START_DELAY_MAX_SEC)
            logger.info(f"[Step7] 첫 발행 부하 분산 딜레이 {delay}초 대기")
            time.sleep(delay)

        # A long start delay may cross the TTL. A claim is not permission to
        # publish an expired comment; no X request has been made on this path.
        original = store.parse_utc(tweet.get("created_at"))
        if original and datetime.now(UTC) - original > timedelta(
            hours=filter_mod.REPLY_MAX_AGE_HOURS
        ):
            if not store.update_skip_reason(tweet_id, "EXPIRED_BEFORE_SEND", metadata):
                cursor_safe_to_advance = False
                review_entry["persistence_error"] = "EXPIRY_SAVE_FAILED"
                send_admin_alert("Reply expiry state save failed; publication was not attempted")
            filter_mod.release_admission(tweet, cap_ctx)
            review_entry["result"] = "EXPIRED_BEFORE_SEND"
            _skip(tweet_id, "EXPIRED_BEFORE_SEND")
            continue

        telemetry.event(summary, "PUBLISH_REQUEST", tweet_id, origin=review_entry["origin"],
                        source=response_source)
        # live: 결과 불명도 quota를 점유한다. 명시적 거절일 때만 반환한다.
        quota_used_this_run += 1
        foreign_reserved_this_run += int(bool(tweet.get("foreign_thread")))
        # live: 발행 → 즉시 기록 (발행-기록 짝)
        # 테스트/외부 사용처가 레거시 post_reply를 교체할 수 있어 해당 경우에는
        # 기존 호출 규약을 유지하고, 실제 클라이언트 경로에서는 오류 원문도 받는다.
        if x_client.post_reply is _DEFAULT_POST_REPLY:
            publish_result = x_client.post_reply_with_error(client, reply_text, tweet_id)
        else:
            publish_result = x_client.post_reply(client, reply_text, tweet_id)
        publish_attempts_this_run += 1
        if isinstance(publish_result, tuple):
            response_tweet_id, publish_error = publish_result
        else:
            response_tweet_id, publish_error = publish_result, None
        guard.record_write()
        store.upsert_budget(guard.row)  # V-1: 발행마다 즉시 저장 (timeout 킬 시 집계 유실 방지)

        if response_tweet_id:
            persisted = store.mark_responded(tweet_id, response_tweet_id)
            review_entry["result"] = "PUBLISHED"
            if not persisted:
                review_entry["result"] = "PUBLISHED_DB_UNCONFIRMED"
                _skip(tweet_id, "DB_CONFIRM_FAIL")
                send_admin_alert(
                    "Reply Engine DB confirmation failed: "
                    f"tweet={tweet_id}, response={response_tweet_id}"
                )
            telemetry.event(summary, review_entry["result"], tweet_id,
                            response_id=response_tweet_id, origin=review_entry["origin"],
                            source=response_source)
            recent_texts.append(reply_text)
            published_this_run += 1
            foreign_published += int(bool(tweet.get("foreign_thread")))
            # R-2: 실발행 기준 캡 계수 (동일 저자·대화 중복 발행 차단)
            published_author_run[author_id] = published_author_run.get(author_id, 0) + 1
            published_conv_run[conversation_id] = published_conv_run.get(conversation_id, 0) + 1
        else:
            failure = classify_publish_error(publish_error)
            confirmed_rejection = failure in {"PUBLISH_RETRYABLE", "PUBLISH_REJECTED", "SPEND_CAP"}
            error_meta = decode_metadata(metadata)
            if failure == "PUBLISH_RETRYABLE" and int(error_meta.get("publish_attempts", 0)) >= 3:
                failure = "PUBLISH_EXHAUSTED"
            error_meta["platform_error"] = str(publish_error or "")[:1000]
            if not store.update_skip_reason(
                tweet_id, failure, json.dumps(error_meta, ensure_ascii=False)
            ):
                cursor_safe_to_advance = False
            review_entry["result"] = failure
            _skip(tweet_id, failure)
            summary["deferred"] += int(failure in DEFER_REASONS)
            # Confirmed non-publication returns the in-run reservation.
            if confirmed_rejection:
                filter_mod.release_admission(tweet, cap_ctx)
                quota_used_this_run -= 1
                foreign_reserved_this_run -= int(bool(tweet.get("foreign_thread")))

        # 발행 간 지터 (마지막 건 제외)
        if idx < len(pass_items) - 1 and published_this_run < run_cap:
            time.sleep(random.randint(PUBLISH_JITTER_MIN_SEC, PUBLISH_JITTER_MAX_SEC))

    summary["foreign_thread_replies"] = foreign_published
    summary["published"] = published_this_run
    summary["publish_attempts"] = publish_attempts_this_run
    summary["actual_published"] = published_this_run if mode == "live" else 0
    summary["simulated"] = published_this_run if mode != "live" else 0

    failures = {
        key: value
        for key, value in summary["skip_reasons"].items()
        if key in {"PUBLISH_UNKNOWN", "PUBLISH_RETRYABLE", "PUBLISH_REJECTED",
                   "PUBLISH_EXHAUSTED", "SPEND_CAP"}
        and value
    }
    if failures:
        failure_counts = ", ".join(f"{key}={value}" for key, value in failures.items())
        send_admin_alert(f"Reply Engine failure: {failure_counts}")

    # ── Step 8: 커서 확정 + 예산 저장 + 리포트 ───────────────
    # 모든 후보가 terminal 처리된 뒤에만 커서를 전진한다. 이 지점 전 crash는 다음
    # 실행에서 멘션을 재수집하며, history 멱등성 가드가 이미 처리된 건을 차단한다.
    if cursor_safe_to_advance:
        summary["cursor_advanced"] = store.upsert_cursor(
            cursor_account, fetched["newest_id"], my_user_id
        )
        if not summary["cursor_advanced"]:
            logger.error("[Step8] cursor 저장 실패 — 다음 실행에서 안전하게 재수집한다")
    elif cursor_can_advance:
        logger.error("[Step8] history 저장 실패가 있어 cursor를 보존한다")

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
    fail_reasons = {
        "EXIT_NO_CREDENTIALS", "EXIT_GET_ME_FAIL", "EXIT_FETCH_FAIL", "EXIT_BUDGET_UNAVAILABLE"
    }
    sys.exit(1 if result.get("exit_reason") in fail_reasons else 0)
