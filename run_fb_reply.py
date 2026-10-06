"""
Facebook Reply Engine — 메인 파이프라인 (v1.0.0, FB-1, 2026-10-07)
=====================================================================
Facebook Page(FACE_PAGE_ID) 게시물에 달린 댓글 수집 → 필터 → 분류 → 생성 → 게이트 → 답글 발행.

X Reply Engine(run_reply.py)과 같은 단계 계약을 따르며, 판단 로직은 공통 모듈을 그대로 사용한다:
  filter(check_tweet / CapContext / check_and_admit) · classifier · generator(platform="facebook")
  · gate · lang · policy · budget.BudgetGuard · store(bind(FB_TABLES)) · telemetry
플랫폼 어댑터만 Facebook 전용이다: reply_engine/facebook/{config,client,normalize}.py

[X와의 분리 — 상호 무영향]
  테이블 fb_reply_* (kr_reply_* 무접촉) / 변수 FACE_* (REPLY_* 미참조) /
  워크플로·concurrency group 별도 / 리포트 logs/fb_reply_report_*.json /
  이벤트 logs/fb_reply_events.jsonl / 관리자 알림 접두 [FB Reply]

[정책 — X와 동일]
  무응답 기본(default-deny): POSITIVE / SUPPORTIVE_NEUTRAL만 답글, 40자 이내 감사·호응
  결과 불명 발행 재시도 금지, 명시적 스로틀 거절만 제한 복구, 24시간 경과 댓글 폐기

[모드 — FACE_REPLY_MODE]  dry_run(DB·발행 금지) / shadow(DB O, 발행 X) / live
[긴급 정지] FACE_REPLY_ENABLED != 'true' → 즉시 종료

[중복 방지]
  L1 history PK(comment id) + 원자적 claim / L3 yml concurrency /
  L4 작성자 상한 / L5 게시물 상한 / L6 텍스트 유사도 / R-2 실발행 기준 2차 캡
  Graph comments에는 since_id가 없으므로 '이미 같은 모드로 처리된 댓글'은 신규에서 제외하고,
  보류 건은 X와 같은 이력 기반 복구 큐(get_retryable_history)로만 재처리한다.
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
from reply_engine import classifier, gate, generator, lang, store, telemetry
from reply_engine import filter as filter_mod
from reply_engine.facebook import client as fb_client
from reply_engine.facebook import config as fbc
from reply_engine.facebook.normalize import normalize_comment
from reply_engine.policy import DEFER_REASONS, decode_metadata, encode_metadata

VERSION = "1.0.0"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_JOURNAL = "logs/fb_reply_events.jsonl"
_REPORT_PREFIX = "fb_reply_report"

# 플랫폼 정지 사유: 같은 회차의 추가 Graph 호출을 중단한다 (rate-limiting 문서 권고).
_HALT_CATEGORIES = frozenset({"AUTH", "THROTTLE", "POLICY_BLOCK"})
# 수집 단계 정지 사유: 위 + Page 사용률(BUC) 임계 — 회차의 분류·발행까지 중단 (리뷰 #2).
_COLLECTION_HALTS = _HALT_CATEGORIES | {"BUC_LIMIT"}
# 토큰이 쿼리스트링에 포함되므로 네트워크 라이브러리 DEBUG 로그를 차단한다 (리뷰 #10).
_QUIET_LOGGERS = ("urllib3", "requests", "httpx", "httpcore", "hpack")

logger = logging.getLogger(__name__)


def _repo():
    """FB 테이블에 바인딩된 store 뷰. 호출 시점 바인딩이라 테스트 patch가 그대로 적용된다."""
    return store.bind(fbc.FB_TABLES)


def _alert(text: str) -> None:
    send_admin_alert(f"{fbc.ALERT_PREFIX} {text}")


def _setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / f"fb_reply_{today}.log"),
        ],
    )
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _write_report(summary: dict, guard=None) -> None:
    telemetry.write_run_report(summary, guard, prefix=_REPORT_PREFIX)


def _cursor_stale_hours(cursor: dict | None) -> int | None:
    raw = (cursor or {}).get("updated_at")
    if not raw:
        return None
    parsed = store.parse_utc(raw)
    if parsed is None:
        return None
    return max(0, int((datetime.now(UTC) - parsed).total_seconds() // 3600))


def _new_guard(repo, today: str):
    return budget_mod.BudgetGuard(
        repo.get_budget(today),
        costs=(None, None),  # Graph API: KRW 단가 없음 → count 모드
        limit_krw=0.0,
        fallback=(fbc.FACE_REPLY_READ_CALLS_PER_DAY, fbc.FACE_REPLY_WRITE_CALLS_PER_DAY),
        cost_labels=("FACE_READ_COST", "FACE_WRITE_COST"),
    )


def main() -> dict:
    _setup_logging()
    mode = fbc.get_face_mode()
    logger.info(f"[FBReplyEngine] v{VERSION} 시작 | mode={mode}")
    repo = _repo()

    summary: dict = {
        **telemetry.new_run(),
        "platform": fbc.PLATFORM,
        "journal_path": _JOURNAL,
        "version": VERSION,
        "mode": mode,
        "success": False,
        "exit_reason": None,
        "collected": 0,
        "posts_seen": 0,
        "posts_skipped": 0,
        "collection_pages": 0,
        "collection_complete": False,
        "collection_halt": None,
        "buc_max_pct": None,
        "cursor_advanced": False,
        "already_processed": 0,
        "candidates": 0,
        "classified_pass": 0,
        "non_kr_replies": 0,
        "thread_replies": 0,
        "cursor_stale_hours": None,
        "page_id_mismatch": False,
        "published": 0,
        "actual_published": 0,
        "simulated": 0,
        "publish_halt": None,
        "skip_reasons": {},
        "review": [],
        "history_metrics": None,
        "recovered_failures": 0,
        "deferred": 0,
        "model_usage": [],
        "started_at": datetime.now(UTC).isoformat(),
    }

    def _skip(comment_id: str, reason: str) -> None:
        summary["skip_reasons"][reason] = summary["skip_reasons"].get(reason, 0) + 1
        logger.info(f"[Skip] {comment_id}: {reason}")
        telemetry.event(summary, reason, comment_id)

    # ── Step 0: 게이트 ────────────────────────────────────────
    if not fbc.is_face_enabled():
        logger.warning("[Step0] FACE_REPLY_ENABLED != 'true' — 긴급 정지 상태, 종료")
        summary["exit_reason"] = "EXIT_DISABLED"
        _write_report(summary)
        return summary

    page_id = fbc.get_page_id()
    token = fbc.get_page_token()
    if not page_id or not token:
        logger.error("[Step0] FACE_PAGE_ID(숫자) / FACE_PAGE_TOKEN 미설정")
        summary["exit_reason"] = "EXIT_NO_CREDENTIALS"
        _write_report(summary)
        return summary

    if mode == "live" and fbc.STARTUP_JITTER_MAX_SEC > 0:
        jitter = random.randint(0, fbc.STARTUP_JITTER_MAX_SEC)
        logger.info(f"[Step0] 시작 지터 {jitter}초 대기 (안티봇)")
        time.sleep(jitter)

    db_write_allowed = mode in ("shadow", "live")

    # ── Step 1: 예산 ──────────────────────────────────────────
    today = store.kst_today()
    try:
        guard = _new_guard(repo, today)
    except Exception as exc:
        logger.error("[Step1] 예산 확인 실패 — 외부 호출 중단: %s", exc)
        summary["exit_reason"] = "EXIT_BUDGET_UNAVAILABLE"
        _write_report(summary)
        return summary
    summary["history_metrics"] = repo.get_history_metrics(days=7)
    if not guard.can_read():
        summary["exit_reason"] = "EXIT_BUDGET"
        _write_report(summary, guard)
        return summary

    # ── Step 2: 수집 ──────────────────────────────────────────
    cursor_account = fbc.ACCOUNT if mode != "shadow" else f"{fbc.ACCOUNT}:shadow"
    cursor = repo.get_cursor(cursor_account)
    cached_page_id = (cursor or {}).get("my_user_id") or ""
    # R-10: Page ID 오등록 흔적을 남긴다 (Page 교체 시 불일치는 정상이므로 중단하지 않음).
    summary["page_id_mismatch"] = bool(cached_page_id and cached_page_id != page_id)
    if summary["page_id_mismatch"]:
        logger.warning(f"[Step2] Page ID 불일치 — 변수={page_id} vs 커서 캐시={cached_page_id}")
    stale_hours = _cursor_stale_hours(cursor)
    summary["cursor_stale_hours"] = stale_hours
    if stale_hours is not None and stale_hours >= fbc.FACE_REPLY_CURSOR_STALE_WARN_HOURS:
        logger.warning(f"[Step2] 커서 {stale_hours}시간째 미전진 — 신규 댓글 없음 또는 수집 이상")

    if mode == "live":
        summary["expiry_maintenance"] = repo.expire_deferred(page_id)
        if summary["expiry_maintenance"].get("errors"):
            _alert("deferred expiry maintenance failed; see run report")

    max_reads = 1 + fbc.FACE_REPLY_POST_LOOKBACK * fbc.FACE_REPLY_COMMENT_MAX_PAGES
    collected = fb_client.collect(
        page_id,
        token,
        post_limit=fbc.FACE_REPLY_POST_LOOKBACK,
        max_pages=fbc.FACE_REPLY_COMMENT_MAX_PAGES,
        max_age_hours=fbc.FACE_REPLY_MAX_AGE_HOURS,
        read_allowance=guard.available_read_calls(max_reads),
        buc_stop_pct=fbc.FACE_REPLY_BUC_STOP_PCT,
    )
    for _ in range(int(collected.get("pages_fetched", 0))):
        guard.record_read()
    summary["collection_pages"] = int(collected.get("pages_fetched", 0))
    summary["collection_complete"] = bool(collected.get("collection_complete"))
    summary["collection_halt"] = collected.get("halt_reason")
    summary["buc_max_pct"] = collected.get("buc_max_pct")
    summary["posts_seen"] = collected.get("posts_seen", 0)
    summary["posts_skipped"] = collected.get("posts_skipped", 0)

    if not collected.get("success"):
        category = collected.get("error_category")
        summary["exit_reason"] = {
            "AUTH": "EXIT_AUTH",
            "THROTTLE": "EXIT_RATE_LIMIT",
            "POLICY_BLOCK": "EXIT_POLICY_BLOCK",
        }.get(category, "EXIT_FETCH_FAIL" if collected.get("pages_fetched") else "EXIT_BUDGET")
        summary["fetch_error"] = collected.get("error")
        if category in _HALT_CATEGORIES:
            _alert(f"collection stopped: {category} — {collected.get('error')}")
        if db_write_allowed:
            repo.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary
    if collected.get("halt_reason") in _COLLECTION_HALTS:
        _alert(f"collection partially stopped: {collected['halt_reason']}")

    # 정규화 (공통 item 형태)
    seen_at = datetime.now(UTC).isoformat()
    normalized: list[tuple[dict, str | None]] = []
    newest: datetime | None = None
    for raw, post in collected.get("items", []):
        item, pre_skip = normalize_comment(
            raw, post, page_id, thread_enabled=fbc.FACE_REPLY_THREAD_ENABLED
        )
        if item is None:
            summary["skip_reasons"]["INVALID"] = summary["skip_reasons"].get("INVALID", 0) + 1
            continue
        item.update(_run_id=summary["run_id"], _first_seen_at=seen_at)
        normalized.append((item, pre_skip))
        if item["created_at"] is not None and (newest is None or item["created_at"] > newest):
            newest = item["created_at"]
    summary["collected"] = len(normalized)

    # 같은 모드로 이미 처리된 댓글은 신규에서 제외 (since_id 부재 보완).
    # live 보류 건은 아래 복구 큐로만 재처리한다. shadow 행은 live가 재평가할 수 있다.
    # 단, READY 고아 행(이력 저장 후 claim 전에 실행이 중단된 live 행)은 X의 커서 미전진
    # 재수집과 같은 효과를 위해 신규로 재평가한다 — insert_history CAS가 상태를 보호한다.
    existing = repo.history_exists_bulk([item["id"] for item, _ in normalized])
    existing_rows = getattr(existing, "rows", {})
    fresh: list[tuple[dict, str | None]] = []
    for item, pre_skip in normalized:
        row = existing_rows.get(item["id"])
        # live 실행에서만 재평가한다 (shadow는 live 행을 덮어쓸 수 없음 — 2차 리뷰 N1).
        ready_orphan = bool(
            mode == "live" and row is not None and row.get("mode") == "live"
            and row.get("skip_reason") is None
            and not row.get("responded") and not row.get("response_tweet_id")
        )
        if row is not None and not ready_orphan and (
            row.get("mode") == "live" or mode == "shadow"
        ):
            summary["already_processed"] += 1
            continue
        if row is None and item["id"] in existing:  # 조회 실패 → 보수적 전건 차단
            summary["already_processed"] += 1
            continue
        summary["ready_orphans"] = summary.get("ready_orphans", 0) + int(ready_orphan)
        fresh.append((item, pre_skip))

    retry_rows = repo.get_retryable_history(100) if mode == "live" else []
    fetched_by_id = {item["id"]: (item, pre_skip) for item, pre_skip in normalized}
    items: list[dict] = []
    pre_skips: dict[str, str] = {}
    for item, pre_skip in fresh:
        items.append(item)
        if pre_skip:
            pre_skips[item["id"]] = pre_skip
    for row in retry_rows:
        meta = decode_metadata(row.get("error_message"))
        if meta.get("account_user_id") != page_id:
            continue
        cid = str(row["reply_tweet_id"])
        refetched = fetched_by_id.get(cid)
        # 복구 건도 '현재 설정·현재 데이터' 기준으로 범위를 재판정한다 (리뷰 #1).
        #   재수집됨 → 정규화 사전 스킵 그대로 적용 (from 누락·opt-in 해제 등)
        #   재수집 안 됨 → 저장 메타 기반 보수 판정
        if refetched is not None:
            base, retry_pre_skip = refetched
        else:
            base = {
                "id": cid,
                "text": meta.get("comment_text") or row.get("comment_text") or "",
                "author_id": str(row.get("author_id") or ""),
                "conversation_id": str(row.get("conversation_id") or ""),
                "in_reply_to_user_id": meta.get("in_reply_to_user_id", ""),
                "created_at": store.parse_utc(meta.get("original_created_at")),
                "parent_text": meta.get("parent_text", ""),
                "parent_id": meta.get("parent_id", ""),
                "parent_author_id": meta.get("parent_author_id", ""),
                "root_author_id": page_id,
                "fb_thread": bool(meta.get("fb_thread")),
                "fb_can_comment": True,
            }
            retry_pre_skip = None
            if not base["author_id"]:
                retry_pre_skip = "AUTHOR_UNVERIFIED"
            elif base["created_at"] is None:
                retry_pre_skip = "TIME_UNVERIFIED"
            elif base["fb_thread"] and not fbc.FACE_REPLY_THREAD_ENABLED:
                retry_pre_skip = "OUT_OF_SCOPE_THREAD"
        if retry_pre_skip:
            pre_skips[cid] = retry_pre_skip
        items.append({
            **base,
            "_metadata": meta,
            "_retry": True,
            "_run_id": summary["run_id"],
            "_stored_response_text": row.get("response_text") or "",
        })
    items.sort(key=lambda t: t.get("created_at") or datetime.min.replace(tzinfo=UTC))
    summary["recovered_failures"] = sum(bool(t.get("_retry")) for t in items)
    summary["processed"] = len(items)
    logger.info(
        f"[Step2] 수집 {summary['collected']}건 (기처리 {summary['already_processed']}, "
        f"복구 {summary['recovered_failures']})"
    )

    newest_iso = newest.isoformat() if newest else None
    cursor_can_advance = bool(db_write_allowed and newest_iso and summary["collection_complete"])
    cursor_safe_to_advance = cursor_can_advance
    if db_write_allowed and newest_iso and not summary["collection_complete"]:
        logger.warning("[Step2] 수집 미완료로 cursor를 보존한다")

    if not items:
        if cursor_can_advance:
            summary["cursor_advanced"] = repo.upsert_cursor(cursor_account, newest_iso, page_id)
        summary["success"] = True
        summary["exit_reason"] = "EXIT_NO_COMMENTS"
        if db_write_allowed:
            repo.upsert_budget(guard.row)
        _write_report(summary, guard)
        return summary

    def _metadata_for(item: dict, **kwargs) -> str:
        meta = json.loads(encode_metadata({**item, "_account_user_id": page_id}, **kwargs))
        meta["platform"] = fbc.PLATFORM
        meta["fb_thread"] = bool(item.get("fb_thread"))
        return json.dumps(meta, ensure_ascii=False)

    def _record_skip(item: dict, reason: str, label: str = "AMBIGUOUS") -> None:
        nonlocal cursor_safe_to_advance
        _skip(item["id"], reason)
        summary["deferred"] += int(reason in DEFER_REASONS)
        summary["review"].append({
            "reply_tweet_id": item["id"],
            "origin": "recovered" if item.get("_retry") else "new",
            "comment_preview": item["text"][:100],
            "parent_preview": item.get("parent_text", "")[:160],
            "label": label,
            "reply_text": None,
            "result": reason,
            "thread_reply": bool(item.get("fb_thread")),
        })
        if not db_write_allowed or reason == "DUP":
            return
        record = {
            "reply_tweet_id": item["id"],
            "conversation_id": item["conversation_id"],
            "author_id": item["author_id"],
            "author_username": "",  # 실명 미저장 (데이터 최소화) — 식별은 Page 범위 ID
            "comment_text": item["text"][:500],
            "classification": label,
            "responded": False,
            "response_tweet_id": None,
            "response_text": "",
            "skip_reason": reason,
            "dry_run": mode != "live",
            "mode": mode,
            "error_message": _metadata_for(item),
        }
        if not repo.insert_history(record):
            cursor_safe_to_advance = False
            _skip(item["id"], "HISTORY_INSERT_FAIL")

    # ── Step 3: 필터 ──────────────────────────────────────────
    blacklist = repo.get_blacklist_ids()
    static_ok: list[dict] = []
    blocked_ids = existing if not retry_rows else repo.history_exists_bulk(
        [item["id"] for item in items]
    )
    for item in items:
        if item["id"] in blocked_ids:
            _record_skip(item, "DUP")
            continue
        if item["id"] in pre_skips:
            _record_skip(item, pre_skips[item["id"]])
            continue
        passed, reason = filter_mod.check_tweet(
            item, None, page_id, blacklist, max_age_hours=fbc.FACE_REPLY_MAX_AGE_HOURS
        )
        if not passed:
            _record_skip(item, reason)
            continue
        if not item.get("fb_can_comment", True):
            _record_skip(item, "CANNOT_REPLY")
            continue
        static_ok.append(item)

    cap_ctx = (
        filter_mod.build_cap_context(static_ok, blocked_ids, repo=repo) if static_ok else None
    )
    caps = filter_mod.CapLimits(
        author_daily=fbc.FACE_REPLY_AUTHOR_DAILY_CAP,
        conversation_daily=fbc.FACE_REPLY_POST_DAILY_CAP,
    )
    candidates: list[dict] = []
    for item in static_ok:
        passed, reason = filter_mod.check_duplicate(item, cap_ctx)
        if not passed:
            _record_skip(item, reason)
            continue
        previous = cap_ctx.history_rows.get(item["id"], {})
        metadata = decode_metadata(previous.get("error_message"))
        if metadata and not item.get("_metadata"):
            item["_metadata"] = metadata
        candidates.append(item)
    summary["candidates"] = len(candidates)
    logger.info(f"[Step3] 필터 통과 {len(candidates)}건")

    # 수집 중 플랫폼 정지(토큰·스로틀·정책 차단)가 발생했으면 이 회차는 분류·발행하지 않는다.
    # 후보는 DB에 기록하지 않으므로 다음 실행에서 신규로 재평가된다 (Gemini 낭비·재차단 방지).
    halt: str | None = (
        collected.get("halt_reason")
        if collected.get("halt_reason") in _COLLECTION_HALTS else None
    )
    if halt:
        summary["publish_halt"] = halt
        for item in candidates:
            summary["review"].append({
                "reply_tweet_id": item["id"],
                "origin": "recovered" if item.get("_retry") else "new",
                "comment_preview": item["text"][:100],
                "label": None,
                "result": f"HALTED_{halt}",
            })
        candidates = []

    # ── Step 4: 분류 (공통 classifier) ───────────────────────
    pass_items: list[dict] = []
    if candidates:
        labels = classifier.classify_batch([
            {
                "id": t["id"],
                "text": t["text"],
                "parent_text": t.get("parent_text", ""),
                "foreign_thread": False,  # 내 Page 게시물 스레드만 수집 (OWN_ROOT)
            }
            for t in candidates
        ])
        for _ in range(getattr(labels, "api_calls", 0)):
            guard.record_gemini()
        summary["model_usage"].extend(getattr(labels, "usage", []))
        for item in candidates:
            label = labels.get(item["id"], "AMBIGUOUS")
            if label in classifier.PASS_LABELS:
                pass_items.append({**item, "label": label})
                continue
            reason = f"CLASS_{label}"
            if item["id"] in getattr(labels, "unavailable_ids", set()):
                reason = "CLASSIFIER_UNAVAILABLE"
                meta = dict(item.get("_metadata") or {})
                meta["classification_attempts"] = int(meta.get("classification_attempts", 0)) + 1
                meta["next_attempt_at"] = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
                item["_metadata"] = meta
                if meta["classification_attempts"] >= 3:
                    reason = "CLASSIFIER_EXHAUSTED"
            _record_skip(item, reason, label)
    summary["classified_pass"] = len(pass_items)
    logger.info(f"[Step4] 분류 통과 {len(pass_items)}건")

    # ── Step 5~8: 생성 → 게이트 → 발행 → 기록 ────────────────
    replies: dict[str, str] = {}
    reply_sources: dict[str, str] = {}
    generated_ids: set[str] = set()
    summary["non_kr_replies"] = sum(lang.is_non_korean(t["text"]) for t in pass_items)

    def generate_window(index: int, capacity: int) -> None:
        window = [t for t in pass_items[index:index + capacity] if t["id"] not in generated_ids]
        if not window:
            return
        batch = generator.generate_batch(window, platform=fbc.PLATFORM)
        replies.update(batch)
        reply_sources.update(getattr(batch, "sources", {}))
        generated_ids.update(t["id"] for t in window)
        for _ in range(getattr(batch, "api_calls", 0)):
            guard.record_gemini()
        summary["model_usage"].extend(getattr(batch, "usage", []))
        for entry in window:
            if entry.get("_retry") and entry.get("_stored_response_text"):
                replies[entry["id"]] = entry["_stored_response_text"]

    run_cap = fbc.FACE_REPLY_RUN_CAP
    summary["effective_run_cap"] = run_cap
    recent_texts = repo.get_recent_response_texts(fbc.FACE_REPLY_RECENT_COMPARE_COUNT)
    responded_today = repo.count_responded_today()
    published_this_run = 0
    publish_attempts_this_run = 0
    quota_used_this_run = 0
    thread_reserved_this_run = 0
    published_author_run: dict[str, int] = {}
    published_conv_run: dict[str, int] = {}
    first_publish_delayed = False

    for idx, item in enumerate(pass_items):
        comment_id = item["id"]
        author_id = item["author_id"]
        conversation_id = item["conversation_id"]

        review_entry = {
            "reply_tweet_id": comment_id,
            "origin": "recovered" if item.get("_retry") else "new",
            "comment_preview": item["text"][:100],
            "parent_preview": item.get("parent_text", "")[:160],
            "label": item["label"],
            "reply_text": None,
            "source": None,
            "draft_gate_reason": None,
            "thread_reply": bool(item.get("fb_thread")),
            "result": None,
        }
        summary["review"].append(review_entry)

        if halt:
            # 플랫폼 정지 이후 건은 DB에 기록하지 않는다 — 다음 실행에서 신규로 재평가된다.
            review_entry["result"] = f"HALTED_{halt}"
            continue

        skip_reason: str | None = None
        reserved = False
        reply_text = ""
        # 상한·예산으로 생성 전에 보류된 건은 출처가 없다 (리포트 오표기 방지).
        response_source: str | None = None
        if max(published_this_run, publish_attempts_this_run) >= run_cap:
            skip_reason = "RUN_CAP"
        elif responded_today + quota_used_this_run >= fbc.FACE_REPLY_DAILY_CAP:
            skip_reason = "DAILY_CAP"
        elif item.get("fb_thread") and thread_reserved_this_run >= fbc.FACE_REPLY_THREAD_RUN_CAP:
            # 공통 보류 사유 재사용 (스레드 저상한 = X 타인 스레드 상한과 같은 의미)
            skip_reason = "FOREIGN_THREAD_CAP"
        elif published_author_run.get(author_id, 0) >= fbc.FACE_REPLY_AUTHOR_DAILY_CAP:
            skip_reason = "AUTHOR_CAP_RUN"
        elif published_conv_run.get(conversation_id, 0) >= fbc.FACE_REPLY_POST_DAILY_CAP:
            skip_reason = "CONV_CAP_RUN"
        elif mode == "live" and not guard.can_write():
            skip_reason = "BUDGET_WRITE"
        else:
            if comment_id not in generated_ids:
                capacity = max(1, min(
                    run_cap - max(published_this_run, publish_attempts_this_run),
                    fbc.FACE_REPLY_DAILY_CAP - responded_today - quota_used_this_run,
                ))
                generate_window(idx, capacity)
            reply_text = (replies.get(comment_id) or "").strip()
            if item.get("_retry") and item.get("_stored_response_text"):
                response_source = "DB_RETRY"
            else:
                response_source = reply_sources.get(
                    comment_id,
                    "TEMPLATE_NON_KR" if lang.is_non_korean(item["text"]) else "AI",
                )
            gate_ok, gate_reason = gate.check_reply(
                reply_text, recent_texts, comment_text=item["text"]
            )
            review_entry["draft_gate_reason"] = gate_reason
            if not gate_ok:
                for fallback_text in generator.contextual_fallbacks(item):
                    if gate.check_reply(fallback_text, recent_texts, comment_text=item["text"])[0]:
                        logger.info(f"[Gate] 초안 {gate_reason} → 안전 fallback 대체")
                        reply_text = fallback_text
                        response_source = "TEMPLATE_FALLBACK"
                        gate_ok, gate_reason = True, None
                        break
            if not gate_ok:
                skip_reason = gate_reason
            elif mode == "live" and not guard.can_write():
                skip_reason = "BUDGET_WRITE"
            else:
                admitted, reason = filter_mod.check_and_admit(
                    item, cap_ctx, repo=repo, caps=caps
                )
                reserved = admitted
                if not admitted:
                    skip_reason = reason
        review_entry["reply_text"] = reply_text or None
        review_entry["source"] = response_source

        record = {
            "reply_tweet_id": comment_id,
            "conversation_id": conversation_id,
            "author_id": author_id,
            "author_username": "",
            "comment_text": item["text"][:500],
            "classification": item["label"],
            "responded": False,
            "skip_reason": skip_reason,
            "response_text": reply_text,
            "response_tweet_id": None,
            "dry_run": mode != "live",
            "mode": mode,
            "error_message": _metadata_for(item),
        }
        if db_write_allowed and not repo.insert_history(record):
            if reserved:
                filter_mod.release_admission(item, cap_ctx)
            cursor_safe_to_advance = False
            review_entry["result"] = "HISTORY_INSERT_FAIL"
            _skip(comment_id, "HISTORY_INSERT_FAIL")
            continue

        if skip_reason:
            review_entry["result"] = skip_reason
            _skip(comment_id, skip_reason)
            summary["deferred"] += int(skip_reason in DEFER_REASONS)
            continue

        if mode != "live":
            logger.info(f"[{mode.upper()}] 발행 시뮬레이션: '{reply_text}' → {comment_id}")
            review_entry["result"] = "SIMULATED"
            recent_texts.append(reply_text)
            published_this_run += 1
            quota_used_this_run += 1
            thread_reserved_this_run += int(bool(item.get("fb_thread")))
            published_author_run[author_id] = published_author_run.get(author_id, 0) + 1
            published_conv_run[conversation_id] = published_conv_run.get(conversation_id, 0) + 1
            continue

        metadata = _metadata_for(item, publish_attempt=True)
        claimed = repo.claim_publication(comment_id, metadata)
        if not claimed:
            filter_mod.release_admission(item, cap_ctx)
            review_entry["result"] = "PUBLISH_CLAIM_FAIL"
            _skip(comment_id, "PUBLISH_CLAIM_FAIL")
            cursor_safe_to_advance = False
            continue
        if isinstance(claimed, str):
            metadata = claimed

        if not first_publish_delayed:
            first_publish_delayed = True
            delay = random.randint(0, max(0, fbc.PUBLISH_START_DELAY_MAX_SEC))
            logger.info(f"[Step7] 첫 발행 부하 분산 딜레이 {delay}초 대기")
            time.sleep(delay)

        original = item.get("created_at")
        if original and datetime.now(UTC) - original > timedelta(
            hours=fbc.FACE_REPLY_MAX_AGE_HOURS
        ):
            if not repo.update_skip_reason(comment_id, "EXPIRED_BEFORE_SEND", metadata):
                cursor_safe_to_advance = False
                review_entry["persistence_error"] = "EXPIRY_SAVE_FAILED"
                _alert("expiry state save failed; publication was not attempted")
            filter_mod.release_admission(item, cap_ctx)
            review_entry["result"] = "EXPIRED_BEFORE_SEND"
            _skip(comment_id, "EXPIRED_BEFORE_SEND")
            continue

        telemetry.event(summary, "PUBLISH_REQUEST", comment_id,
                        origin=review_entry["origin"], source=response_source)
        quota_used_this_run += 1
        thread_reserved_this_run += int(bool(item.get("fb_thread")))
        response_id, publish_error, buc = fb_client.post_reply(comment_id, reply_text, token)
        publish_attempts_this_run += 1
        guard.record_write()
        repo.upsert_budget(guard.row)  # V-1: 발행마다 즉시 저장
        if buc is not None:
            summary["buc_max_pct"] = max(summary["buc_max_pct"] or 0, buc)

        if response_id:
            persisted = repo.mark_responded(comment_id, response_id)
            review_entry["result"] = "PUBLISHED"
            if not persisted:
                review_entry["result"] = "PUBLISHED_DB_UNCONFIRMED"
                _skip(comment_id, "DB_CONFIRM_FAIL")
                _alert(f"DB confirmation failed: comment={comment_id}, response={response_id}")
            telemetry.event(summary, review_entry["result"], comment_id,
                            response_id=response_id, origin=review_entry["origin"],
                            source=response_source)
            recent_texts.append(reply_text)
            published_this_run += 1
            published_author_run[author_id] = published_author_run.get(author_id, 0) + 1
            published_conv_run[conversation_id] = published_conv_run.get(conversation_id, 0) + 1
        else:
            failure = fb_client.classify_publish_error(publish_error)
            category = fb_client.classify_error(publish_error)
            error_meta = decode_metadata(metadata)
            if failure == "PUBLISH_RETRYABLE" and int(error_meta.get("publish_attempts", 0)) >= 3:
                failure = "PUBLISH_EXHAUSTED"
            error_meta["platform_error"] = fb_client.format_error(publish_error)[:1000]
            if not repo.update_skip_reason(
                comment_id, failure, json.dumps(error_meta, ensure_ascii=False)
            ):
                cursor_safe_to_advance = False
            review_entry["result"] = failure
            _skip(comment_id, failure)
            summary["deferred"] += int(failure in DEFER_REASONS)
            if failure in ("PUBLISH_RETRYABLE", "PUBLISH_REJECTED", "PUBLISH_EXHAUSTED"):
                # 명시적 거절 = 미발행 확정 → 회차 내 예약 반환 (결과 불명은 반환하지 않음)
                filter_mod.release_admission(item, cap_ctx)
                quota_used_this_run -= 1
                thread_reserved_this_run -= int(bool(item.get("fb_thread")))
            if category in _HALT_CATEGORIES:
                halt = category
                summary["publish_halt"] = category
                _alert(f"publishing halted: {category} — {fb_client.format_error(publish_error)}")

        if buc is not None and buc >= fbc.FACE_REPLY_BUC_STOP_PCT and not halt:
            halt = "BUC_LIMIT"
            summary["publish_halt"] = halt

        if idx < len(pass_items) - 1 and published_this_run < run_cap and not halt:
            time.sleep(random.randint(fbc.PUBLISH_JITTER_MIN_SEC, fbc.PUBLISH_JITTER_MAX_SEC))

    summary["thread_replies"] = sum(
        1 for r in summary["review"] if r.get("thread_reply") and r.get("result") in {
            "PUBLISHED", "PUBLISHED_DB_UNCONFIRMED", "SIMULATED"}
    )
    summary["published"] = published_this_run
    summary["publish_attempts"] = publish_attempts_this_run
    summary["actual_published"] = published_this_run if mode == "live" else 0
    summary["simulated"] = published_this_run if mode != "live" else 0

    failures = {
        key: value for key, value in summary["skip_reasons"].items()
        if key in {"PUBLISH_UNKNOWN", "PUBLISH_RETRYABLE", "PUBLISH_REJECTED",
                   "PUBLISH_EXHAUSTED"} and value
    }
    if failures:
        _alert("failure: " + ", ".join(f"{k}={v}" for k, v in failures.items()))

    # ── Step 8: 커서 확정 + 예산 저장 + 리포트 ───────────────
    if cursor_safe_to_advance:
        summary["cursor_advanced"] = repo.upsert_cursor(cursor_account, newest_iso, page_id)
        if not summary["cursor_advanced"]:
            logger.error("[Step8] cursor 저장 실패 — 관측 지표만 영향 (신규 판정은 이력 기준)")
    elif cursor_can_advance:
        logger.error("[Step8] history 저장 실패가 있어 cursor를 보존한다")

    if db_write_allowed:
        repo.upsert_budget(guard.row)

    summary["success"] = True
    summary["exit_reason"] = "EXIT_OK"
    logger.info(
        f"[FBReplyEngine] 완료 | 수집={summary['collected']} 후보={summary['candidates']} "
        f"발행={published_this_run} skip={summary['skip_reasons']}"
    )
    _write_report(summary, guard)
    return summary


if __name__ == "__main__":
    result = main()
    fail_reasons = {
        "EXIT_NO_CREDENTIALS", "EXIT_FETCH_FAIL", "EXIT_BUDGET_UNAVAILABLE", "EXIT_AUTH",
        "EXIT_POLICY_BLOCK",
    }
    sys.exit(1 if result.get("exit_reason") in fail_reasons else 0)
