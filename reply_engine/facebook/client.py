"""
reply_engine/facebook/client.py
================================
Facebook Graph API 래퍼 (FB-1, 2026-10-07). requests 사용 (requirements-reply.lock 고정).

공식 문서로 확인한 범위만 사용한다 (추측 명칭 금지 — 프로젝트 0번 규칙):
  - GET  /{page-id}/feed            Page 게시물 목록 (page/feed 레퍼런스)
  - GET  /{post-id}/comments        filter=stream, order=reverse_chronological
                                    (object/comments 레퍼런스, pages-api/comments-mentions)
  - POST /{comment-id}/comments     message — 댓글에 답글 (pages_manage_engagement)
  - X-Business-Use-Case-Usage       Page BUC 사용률 헤더 (rate-limiting 문서)
  - 오류 코드: 1/2 일시, 4/17/32/341/613/80001 스로틀, 102·190·10·200~299 토큰/권한,
    368 정책 차단 (graph-api/guides/error-handling, overview/rate-limiting)

미확정(Preflight로 실측): 개발 모드 앱의 일반 사용자 from 반환, 대댓글 평탄화 위치,
릴스 댓글, comments 엣지의 since 지원, created_time 형식. 그래서 본 모듈은
since 파라미터를 쓰지 않고 시간 창은 클라이언트에서 거른다.

재시도 정책 (X 승인 E 동일): 발행 POST는 재시도하지 않는다. 결과 불명은 재발행 금지.
토큰은 어떤 로그·오류 문자열에도 남기지 않는다 (_mask).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from reply_engine.facebook import config as fbc
from reply_engine.facebook.normalize import parse_graph_time

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

# 테스트는 이 두 경계만 교체한다 (HTTP 경계 목킹 — #166 교훈: 내부 함수 목킹 지양).
_http_get = requests.get
_http_post = requests.post

THROTTLE_CODES = frozenset({4, 17, 32, 341, 613, 80001})
TRANSIENT_CODES = frozenset({1, 2})
POLICY_BLOCK_CODES = frozenset({368})

POST_FIELDS = "id,message,created_time,from,is_published"
COMMENT_FIELDS = "id,message,from,created_time,parent{id,from,message},can_comment"

_MASK = "***"


def _mask(text: str, token: str) -> str:
    text = str(text or "")
    if token and len(token) >= 8:
        text = text.replace(token, _MASK)
    return text


def _is_auth_code(code: int) -> bool:
    return code in (102, 190, 10) or 200 <= code <= 299


def classify_error(error: dict | None) -> str | None:
    """Graph 오류 → THROTTLE | AUTH | POLICY_BLOCK | REJECTED | UNKNOWN (None=오류 없음).

    코드가 없거나(전송 오류·타임아웃·비JSON) 일시 오류(1/2)는 결과 불명으로 본다.
    POST가 실제로 반영됐는지 알 수 없으므로 재발행 근거가 될 수 없다.
    """
    if not error:
        return None
    try:
        code = int(error.get("code"))
    except (TypeError, ValueError):
        return "UNKNOWN"
    if code in THROTTLE_CODES:
        return "THROTTLE"
    if _is_auth_code(code):
        return "AUTH"
    if code in POLICY_BLOCK_CODES:
        return "POLICY_BLOCK"
    if code in TRANSIENT_CODES:
        return "UNKNOWN"
    return "REJECTED"


def classify_publish_error(error: dict | None) -> str:
    """발행 실패 → 공통 상태어휘 (policy.DEFER_REASONS / BLOCKED_STATES와 동일 체계).

    THROTTLE·AUTH는 명시적 거절(미발행 확정)이므로 X의 429와 같이 PUBLISH_RETRYABLE
    (보류 복구 대상, 최대 3회·15분 간격·TTL 이내)이다. 토큰 만료로 댓글이 영구 소실되지 않게 한다.
    POLICY_BLOCK(368)은 같은 문구 재시도가 재차단을 부를 수 있어 영구 거절로 둔다.
    """
    category = classify_error(error)
    if category in ("THROTTLE", "AUTH"):
        return "PUBLISH_RETRYABLE"
    if category in ("POLICY_BLOCK", "REJECTED"):
        return "PUBLISH_REJECTED"
    return "PUBLISH_UNKNOWN"


def format_error(error: dict | None) -> str:
    """DB·리포트 저장용 진단 문자열 (토큰은 이미 마스킹된 상태)."""
    if not error:
        return ""
    parts = [
        f"{key}={error.get(key)}"
        for key in ("code", "error_subcode", "type", "http_status", "transport")
        if error.get(key) not in (None, "")
    ]
    parts.append(f"message={str(error.get('message', ''))[:300]}")
    return "graph_error " + " ".join(parts)


def _buc_max_pct(headers: Any) -> int | None:
    """X-Business-Use-Case-Usage 헤더의 최대 사용률(%)을 반환. 없거나 파싱 불가면 None."""
    try:
        raw = headers.get("X-Business-Use-Case-Usage") if headers is not None else None
        if not raw:
            return None
        payload = json.loads(raw)
        values: list[int] = []
        for entries in (payload or {}).values():
            for entry in entries or []:
                for key in ("call_count", "total_cputime", "total_time"):
                    if isinstance(entry.get(key), (int, float)):
                        values.append(int(entry[key]))
        return max(values) if values else None
    except (ValueError, TypeError, AttributeError):
        return None


def _call(method: str, url: str, token: str, *, params: dict | None = None,
          data: dict | None = None) -> tuple[dict | None, dict | None, int | None]:
    """(payload, error, buc_pct). 예외를 던지지 않는다."""
    try:
        if method == "GET":
            resp = _http_get(url, params=params, timeout=fbc.HTTP_TIMEOUT_SEC)
        else:
            resp = _http_post(url, data=data, timeout=fbc.HTTP_TIMEOUT_SEC)
    except requests.RequestException as exc:
        return None, {"transport": type(exc).__name__, "message": _mask(exc, token)}, None
    buc = _buc_max_pct(getattr(resp, "headers", None))
    status = int(getattr(resp, "status_code", 0) or 0)
    try:
        payload = resp.json()
    except ValueError:
        return None, {"http_status": status, "message": "non-json response"}, buc
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        return None, {
            "code": err.get("code"),
            "error_subcode": err.get("error_subcode"),
            "type": err.get("type"),
            "http_status": status,
            "message": _mask(err.get("message", ""), token),
            "fbtrace_id": err.get("fbtrace_id"),
        }, buc
    if status >= 400 or not isinstance(payload, dict):
        return None, {"http_status": status, "message": "unexpected response"}, buc
    return payload, None, buc


def _max_pct(a: int | None, b: int | None) -> int | None:
    values = [v for v in (a, b) if v is not None]
    return max(values) if values else None


def fetch_page_posts(page_id: str, token: str, limit: int) -> tuple[list[dict], dict | None,
                                                                     int | None]:
    """최근 Page 게시물 (게시·미게시 혼재 — 호출부에서 is_published/from 검증)."""
    payload, error, buc = _call(
        "GET",
        f"{fbc.GRAPH_BASE}/{page_id}/feed",
        token,
        params={"fields": POST_FIELDS, "limit": max(1, min(100, limit)), "access_token": token},
    )
    if error:
        return [], error, buc
    data = payload.get("data") if isinstance(payload.get("data"), list) else []
    return [p for p in data if isinstance(p, dict)], None, buc


def fetch_post_comments(
    post_id: str,
    token: str,
    *,
    max_pages: int,
    not_before: datetime,
) -> dict[str, Any]:
    """게시물 댓글(모든 단계, 최신순). not_before보다 오래된 댓글에 도달하면 완결로 본다."""
    url = f"{fbc.GRAPH_BASE}/{post_id}/comments"
    params: dict | None = {
        "fields": COMMENT_FIELDS,
        "filter": "stream",
        "order": "reverse_chronological",
        "limit": fbc.COMMENTS_PAGE_SIZE,
        "access_token": token,
    }
    comments: list[dict] = []
    pages = 0
    buc: int | None = None
    complete = False
    while pages < max(1, max_pages):
        payload, error, page_buc = _call("GET", url, token, params=params)
        pages += 1
        buc = _max_pct(buc, page_buc)
        if error:
            return {"success": pages > 1, "comments": comments, "pages": pages,
                    "complete": False, "error": error, "buc_pct": buc}
        reached_old = False
        for raw in payload.get("data") or []:
            if not isinstance(raw, dict):
                continue
            created = parse_graph_time(raw.get("created_time"))
            if created is not None and created < not_before:
                reached_old = True
                break
            comments.append(raw)
        next_url = ((payload.get("paging") or {}).get("next")) if not reached_old else None
        if not next_url:
            complete = True
            break
        # 공식 문서: 커서 저장 금지 — 같은 실행 내에서만 paging.next를 따른다.
        url, params = next_url, None
    return {"success": True, "comments": comments, "pages": pages, "complete": complete,
            "error": None, "buc_pct": buc}


def collect(
    page_id: str,
    token: str,
    *,
    post_limit: int,
    max_pages: int,
    max_age_hours: int,
    read_allowance: int,
    buc_stop_pct: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """피드 게시물 → 게시물별 댓글 수집 (D1: 피드만).

    반환: success, items[(raw_comment, post)], pages_fetched(=실제 읽기 호출 수),
          collection_complete, error, error_category, buc_max_pct, posts_seen,
          posts_skipped(타 작성자/미게시/작성자 미확인).
    """
    now = now or datetime.now(UTC)
    not_before = now - timedelta(hours=max_age_hours)
    result: dict[str, Any] = {
        "success": False, "items": [], "pages_fetched": 0, "collection_complete": False,
        "error": None, "error_category": None, "buc_max_pct": None,
        "posts_seen": 0, "posts_skipped": 0, "halt_reason": None,
    }
    if read_allowance < 1:
        result["halt_reason"] = "READ_BUDGET"
        return result
    posts, error, buc = fetch_page_posts(page_id, token, post_limit)
    result["pages_fetched"] = 1
    result["buc_max_pct"] = buc
    if error:
        result["error"] = format_error(error)
        result["error_category"] = classify_error(error)
        return result
    result["success"] = True
    complete = True
    for post in posts[:post_limit]:
        result["posts_seen"] += 1
        owner = str((post.get("from") or {}).get("id") or "")
        # 방문자 게시물·미게시·작성자 미확인 게시물의 댓글은 응답 범위 밖 (P-1 방어).
        if owner != page_id or post.get("is_published") is False or not post.get("id"):
            result["posts_skipped"] += 1
            continue
        if (result["buc_max_pct"] or 0) >= buc_stop_pct:
            result["halt_reason"] = "BUC_LIMIT"
            complete = False
            break
        remaining = read_allowance - result["pages_fetched"]
        if remaining < 1:
            result["halt_reason"] = "READ_BUDGET"
            complete = False
            break
        fetched = fetch_post_comments(
            str(post["id"]), token, max_pages=min(max_pages, remaining), not_before=not_before
        )
        result["pages_fetched"] += fetched["pages"]
        result["buc_max_pct"] = _max_pct(result["buc_max_pct"], fetched["buc_pct"])
        result["items"].extend((raw, post) for raw in fetched["comments"])
        if fetched["error"]:
            complete = False
            category = classify_error(fetched["error"])
            result["error"] = format_error(fetched["error"])
            result["error_category"] = category
            if category in ("AUTH", "THROTTLE", "POLICY_BLOCK"):
                result["halt_reason"] = category
                break
        elif not fetched["complete"]:
            complete = False
    result["collection_complete"] = complete
    logger.info(
        "[FBClient] 수집 게시물 %s(스킵 %s) 댓글 %s건/%s콜 complete=%s buc=%s",
        result["posts_seen"], result["posts_skipped"], len(result["items"]),
        result["pages_fetched"], complete, result["buc_max_pct"],
    )
    return result


def post_reply(
    comment_id: str, message: str, token: str
) -> tuple[str | None, dict | None, int | None]:
    """댓글에 답글 1건. 재시도 없음. (새 댓글 id | None, 오류 | None, BUC %)."""
    payload, error, buc = _call(
        "POST",
        f"{fbc.GRAPH_BASE}/{comment_id}/comments",
        token,
        data={"message": message, "access_token": token},
    )
    if error:
        logger.error("[FBClient] 답글 발행 실패 (재시도 없음): %s", format_error(error))
        return None, error, buc
    new_id = str(payload.get("id") or "")
    if not new_id:
        # 성공 응답에 id가 없으면 반영 여부를 알 수 없다 → 결과 불명.
        return None, {"message": "success response without id"}, buc
    logger.info("[FBClient] 답글 발행 완료: %s → reply_to=%s", new_id, comment_id)
    return new_id, None, buc


def graph_get(path: str, token: str, params: dict) -> tuple[dict | None, dict | None, int | None]:
    """읽기 전용 GET (Preflight 진단용). 쓰기 경로는 post_reply만 존재한다."""
    return _call(
        "GET", f"{fbc.GRAPH_BASE}/{path}", token, params={**params, "access_token": token}
    )


def fetch_page_identity(page_id: str, token: str) -> tuple[dict | None, dict | None]:
    """Preflight 전용: GET /{page-id}?fields=id,name — 토큰·Page 일치 확인."""
    payload, error, _buc = graph_get(page_id, token, {"fields": "id,name"})
    return payload, error


def utc_now() -> datetime:
    return datetime.now(UTC)
