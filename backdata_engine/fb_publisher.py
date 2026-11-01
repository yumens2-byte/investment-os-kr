"""
backdata_engine/fb_publisher.py — Facebook 페이지 본글 발행·인사이트 조회 (DN2026_0090)

공식 문서 범위만 사용:
  POST /{page_id}/feed          message — 페이지 텍스트 게시 (pages_manage_posts, Pages API posts)
  GET  /{post_id}/insights      metric=post_media_view,post_clicks,post_reactions_by_type_total
                                (post_impressions 계열은 2026-06-15 전 버전 폐기 — 사용 금지)
HTTP·토큰 마스킹은 reply_engine/facebook/client._call을 그대로 쓴다(예외를 던지지 않음).
발행은 재시도하지 않는다. 결과 불명은 재발행 금지.
"""

from __future__ import annotations

import logging

from backdata_engine.config import FB_INSIGHT_METRICS
from reply_engine.facebook import client as fbclient
from reply_engine.facebook import config as fbc

VERSION = "0.1.0"

logger = logging.getLogger(__name__)


def post_feed(page_id: str, token: str, message: str) -> tuple[str | None, str | None]:
    payload, error, _buc = fbclient._call(
        "POST", f"{fbc.GRAPH_BASE}/{page_id}/feed", token,
        data={"message": message, "access_token": token},
    )
    if error:
        text = fbclient.format_error(error)
        logger.error(f"[BFBPub] 발행 실패 (재시도 안 함): {text}")
        return None, text
    post_id = str((payload or {}).get("id") or "")
    if not post_id:
        return None, "UNKNOWN: success response without id"
    logger.info(f"[BFBPub] 발행 성공: {post_id}")
    return post_id, None


def fetch_insights(post_id: str, token: str) -> tuple[dict | None, str | None]:
    payload, error, _buc = fbclient.graph_get(
        f"{post_id}/insights", token, {"metric": FB_INSIGHT_METRICS}
    )
    if error:
        return None, fbclient.format_error(error)
    values: dict[str, object] = {}
    for item in (payload or {}).get("data", []) or []:
        vals = item.get("values") or []
        values[item.get("name")] = vals[-1].get("value") if vals else None
    views = values.get("post_media_view")
    clicks = values.get("post_clicks")
    reactions = values.get("post_reactions_by_type_total")
    reaction_sum = sum(v for v in reactions.values() if isinstance(v, int)) \
        if isinstance(reactions, dict) else None
    parts = [x for x in (clicks, reaction_sum) if isinstance(x, int)]
    return {
        "impressions": views if isinstance(views, int) else None,
        "engagements": sum(parts) if parts else None,
        "raw": values,
    }, None
