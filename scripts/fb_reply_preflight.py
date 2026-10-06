"""Facebook Reply Engine 읽기 전용 Preflight (FB-1, D6).

목적: 공식 문서로 확정되지 않은 항목을 실제 Page 토큰으로 실측해, dry_run 착수 가능 여부를
판정한다. Graph 쓰기 호출과 DB 호출은 하지 않는다 (GET만, 최대 약 10콜).

실측 항목:
  1. 토큰 ↔ FACE_PAGE_ID 일치 (GET /{page-id}?fields=id,name)
  2. 피드 게시물 조회와 게시물 from 반환 여부 (GET /{page-id}/feed)
  3. 댓글 조회와 일반 사용자 from 반환 여부 — 개발 모드 앱의 핵심 미확정 항목
     (GET /{post-id}/comments, filter=stream, order=reverse_chronological)
  4. created_time 원문 형식, parent·can_comment 반환 여부
  5. 릴스 목록·릴스 댓글 조회 가능 여부 (D1 후속 판단용, 엔진은 사용하지 않음)
  6. X-Business-Use-Case-Usage 사용률

판정(verdict):
  BLOCKED            — 자격증명·Page 불일치·피드 조회 실패·댓글 조회 권한(AUTH) 오류
  NEEDS_REVIEW       — 댓글 조회 오류, from 누락, created_time 파싱 실패
  READY_FOR_DRY_RUN  — 위 문제 없음
live_ready: READY_FOR_DRY_RUN이면서 일반 사용자 댓글의 from을 실제로 관측(VERIFIED)한 경우만 True.
  from_visibility = NOT_VERIFIED(내 게시물·댓글 표본 없음) / NO_NON_PAGE_COMMENTS_OBSERVED /
                    VERIFIED / MISSING
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from reply_engine.facebook import client as fb_client
from reply_engine.facebook import config as fbc
from reply_engine.facebook.normalize import parse_graph_time

VERSION = "1.0.0"

_POST_SAMPLE = 3
_COMMENT_SAMPLE = 25


def _err(error: dict | None) -> str | None:
    return fb_client.format_error(error) if error else None


def run_preflight() -> dict:
    report: dict = {
        "version": VERSION,
        "checked_at": datetime.now(UTC).isoformat(),
        "graph_base": fbc.GRAPH_BASE,
        "checks": {},
        "buc_max_pct": None,
        "verdict": "BLOCKED",
        "live_ready": False,
    }
    checks = report["checks"]
    page_id = fbc.get_page_id()
    token = fbc.get_page_token()
    checks["credentials"] = {"page_id_numeric": bool(page_id), "token_present": bool(token)}
    if not page_id or not token:
        return report

    identity, error = fb_client.fetch_page_identity(page_id, token)
    checks["identity"] = {
        "ok": bool(identity) and str((identity or {}).get("id")) == page_id,
        "error": _err(error),
        "error_category": fb_client.classify_error(error),
    }
    if not checks["identity"]["ok"]:
        return report

    posts, error, buc = fb_client.fetch_page_posts(page_id, token, _POST_SAMPLE)
    report["buc_max_pct"] = buc
    own = [p for p in posts if str((p.get("from") or {}).get("id") or "") == page_id]
    checks["feed"] = {
        "ok": error is None,
        "error": _err(error),
        "posts": len(posts),
        "posts_with_from": sum(bool((p.get("from") or {}).get("id")) for p in posts),
        "own_posts": len(own),
        "is_published_present": sum("is_published" in p for p in posts),
    }
    if error is not None:
        return report

    comment_stats = {
        "posts_checked": 0, "comments": 0, "with_from": 0, "missing_from": 0,
        "non_page_authors": 0, "with_parent": 0, "can_comment_present": 0,
        "created_time_samples": [], "created_time_parse_failures": 0, "errors": [],
        "error_categories": [],
    }
    for post in own[:_POST_SAMPLE]:
        payload, error, buc = fb_client.graph_get(
            f"{post['id']}/comments",
            token,
            {
                "fields": fb_client.COMMENT_FIELDS,
                "filter": "stream",
                "order": "reverse_chronological",
                "limit": _COMMENT_SAMPLE,
            },
        )
        observed = [v for v in (report["buc_max_pct"], buc) if v is not None]
        report["buc_max_pct"] = max(observed) if observed else None
        comment_stats["posts_checked"] += 1
        if error:
            comment_stats["errors"].append(_err(error))
            comment_stats["error_categories"].append(fb_client.classify_error(error))
            continue
        for raw in (payload or {}).get("data") or []:
            comment_stats["comments"] += 1
            author = str((raw.get("from") or {}).get("id") or "")
            comment_stats["with_from" if author else "missing_from"] += 1
            comment_stats["non_page_authors"] += int(bool(author) and author != page_id)
            comment_stats["with_parent"] += int(bool(raw.get("parent")))
            comment_stats["can_comment_present"] += int("can_comment" in raw)
            value = raw.get("created_time")
            if len(comment_stats["created_time_samples"]) < 3:
                comment_stats["created_time_samples"].append(value)
            comment_stats["created_time_parse_failures"] += int(parse_graph_time(value) is None)
    checks["comments"] = comment_stats

    reels, error, _buc = fb_client.graph_get(
        f"{page_id}/video_reels", token, {"fields": "id,updated_time", "limit": 3}
    )
    reel_ids = [r.get("id") for r in (reels or {}).get("data") or [] if r.get("id")]
    reel_comments_error = None
    reel_comments_ok = None
    if reel_ids:
        payload, reel_error, _buc = fb_client.graph_get(
            f"{reel_ids[0]}/comments", token, {"fields": "id,from,created_time", "limit": 5}
        )
        reel_comments_ok = reel_error is None
        reel_comments_error = _err(reel_error)
    checks["reels"] = {
        "list_ok": error is None,
        "list_error": _err(error),
        "reels": len(reel_ids),
        "comments_ok": reel_comments_ok,
        "comments_error": reel_comments_error,
    }

    if comment_stats["missing_from"]:
        visibility = "MISSING"
    elif comment_stats["non_page_authors"]:
        visibility = "VERIFIED"
    elif not own or not comment_stats["comments"]:
        visibility = "NOT_VERIFIED"
    else:
        visibility = "NO_NON_PAGE_COMMENTS_OBSERVED"
    report["from_visibility"] = visibility
    if "AUTH" in comment_stats["error_categories"]:
        report["verdict"] = "BLOCKED"
    elif (comment_stats["errors"] or visibility == "MISSING"
          or comment_stats["created_time_parse_failures"]):
        report["verdict"] = "NEEDS_REVIEW"
    else:
        report["verdict"] = "READY_FOR_DRY_RUN"
    report["live_ready"] = report["verdict"] == "READY_FOR_DRY_RUN" and visibility == "VERIFIED"
    return report


def main() -> int:
    for name in ("urllib3", "requests"):  # 토큰 포함 URL의 DEBUG 로그 차단
        logging.getLogger(name).setLevel(logging.WARNING)
    report = run_preflight()
    output = Path("logs/fb_reply_preflight.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["verdict"] == "READY_FOR_DRY_RUN" else 1


if __name__ == "__main__":
    sys.exit(main())
