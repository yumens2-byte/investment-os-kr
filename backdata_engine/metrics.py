"""
backdata_engine/metrics.py — 성과 수집 체크포인트 판정 (DN2026_0090)

first: 게시 후 24시간 이상 72시간 미만에 처음 수집 / final: 72시간 이상에 처음 수집.
30일이 지난 게시물은 X 비공개 지표 조회 기간 밖이라 대상에서 뺀다.
72시간이 지나도록 first를 못 모은 경우 first는 건너뛴다(경과 시간은 final 행에 기록).
"""

from __future__ import annotations

from datetime import datetime

from backdata_engine.config import METRIC_FINAL_HOURS, METRIC_FIRST_HOURS, METRIC_MAX_AGE_DAYS

VERSION = "0.1.0"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def due_checkpoints(post: dict, now: datetime) -> list[tuple[str, str, float]]:
    """[(channel, checkpoint, elapsed_hours)]."""
    out = []
    collected = post.get("collected", set())
    for channel in ("x", "fb"):
        if not post.get(f"{channel}_post_id"):
            continue
        posted = _parse(post.get(f"{channel}_posted_at"))
        if posted is None:
            continue
        hours = (now - posted).total_seconds() / 3600
        if hours < METRIC_FIRST_HOURS or hours > METRIC_MAX_AGE_DAYS * 24:
            continue
        if hours >= METRIC_FINAL_HOURS:
            if (channel, "final") not in collected:
                out.append((channel, "final", round(hours, 2)))
        elif (channel, "first") not in collected:
            out.append((channel, "first", round(hours, 2)))
    return out
