"""
reply_engine/facebook/normalize.py
===================================
Graph 댓글 → 공통 판단 로직(filter/classifier/generator/gate/policy) 입력 형태 변환 (FB-1).

공통 item 키 (reply_engine 계약, x_client.fetch_mentions와 동일):
  id, text, author_id, conversation_id, in_reply_to_user_id, created_at,
  parent_text, parent_id, parent_author_id (+ root_author_id)

매핑:
  conversation_id     = post id (게시물 단위 일일 상한 — X 대화 상한과 동일 개념)
  in_reply_to_user_id = 응답 범위 안일 때만 Page ID. 범위 밖이면 공통 filter가
                        OUT_OF_SCOPE로 판정한다 (filter.check_tweet 첫 검사).
  응답 범위 (D10):
    - 최상위 댓글(parent 없음)                      → 범위 안
    - 내 Page 댓글에 달린 대댓글(parent.from == Page) → FACE_REPLY_THREAD_ENABLED일 때만
    - 그 외 대댓글(제3자 간 대화)                     → 범위 밖 (P-1 방어)

보수적 사전 스킵 (판정 불가 = 무응답):
  AUTHOR_UNVERIFIED   — from 누락 (권한·개발 모드로 작성자 미반환)
  TIME_UNVERIFIED     — created_time 파싱 불가 (만료 검사 우회 방지)
  OUT_OF_SCOPE_THREAD — 내 댓글의 대댓글이지만 opt-in 비활성
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

VERSION = "1.0.0"

_COMPACT_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_graph_time(value) -> datetime | None:
    """Graph created_time → aware UTC. '+0000' 형식과 ISO 확장 형식 모두 허용, 실패 시 None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _COMPACT_OFFSET.sub(r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # 시간대 없는 값은 해석을 단정하지 않는다
    return parsed.astimezone(UTC)


def normalize_comment(
    raw: dict,
    post: dict,
    page_id: str,
    *,
    thread_enabled: bool,
) -> tuple[dict | None, str | None]:
    """(item | None, 사전 스킵 사유 | None). id가 없으면 기록 불가이므로 (None, 'INVALID')."""
    comment_id = str(raw.get("id") or "")
    if not comment_id:
        return None, "INVALID"

    author_id = str((raw.get("from") or {}).get("id") or "")
    parent = raw.get("parent") if isinstance(raw.get("parent"), dict) else {}
    parent_id = str(parent.get("id") or "")
    parent_author = str((parent.get("from") or {}).get("id") or "")
    is_reply = bool(parent_id)
    created = parse_graph_time(raw.get("created_time"))

    item = {
        "id": comment_id,
        "text": str(raw.get("message") or ""),
        "author_id": author_id,
        "conversation_id": str(post.get("id") or ""),
        "in_reply_to_user_id": "",
        "created_at": created,
        "parent_text": str((parent.get("message") if is_reply else post.get("message")) or ""),
        "parent_id": parent_id if is_reply else str(post.get("id") or ""),
        "parent_author_id": parent_author if is_reply else page_id,
        "root_author_id": page_id,
        "fb_thread": False,
        "fb_can_comment": raw.get("can_comment") is not False,
    }

    if not author_id:
        return item, "AUTHOR_UNVERIFIED"
    if created is None:
        return item, "TIME_UNVERIFIED"

    if not is_reply:
        item["in_reply_to_user_id"] = page_id
    elif parent_author and parent_author == page_id:
        if not thread_enabled:
            return item, "OUT_OF_SCOPE_THREAD"
        item["in_reply_to_user_id"] = page_id
        item["fb_thread"] = True
    # 그 외(제3자 간 대댓글)는 in_reply_to_user_id="" → filter.check_tweet이 OUT_OF_SCOPE
    return item, None
