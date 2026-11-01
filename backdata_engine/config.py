"""
backdata_engine/config.py — 백데이터 반전 카드 설정 (DN2026_0090)
==================================================================
지수 과거 데이터에서 '오늘 같은 날 다음엔 어땠나'를 통계 1건으로 X·Facebook에 발행한다.

[모드 — BACKDATA_MODE] (STD-MODE-01·02, 공유 모드 변수 금지 STD-MODE-05)
  dry_run — 수집~문안·게이트까지. DB·X·FB 쓰기 0
  shadow  — 원장 적재(pending), 발행 없음
  live    — 랜덤 지연 후 X·FB 각 1회 발행(무재시도)
  인식 불가·빈 값 → dry_run 강등

[긴급 정지] BACKDATA_ENABLED != 'true'
"""

from __future__ import annotations

import os
from datetime import timedelta, timezone

VERSION = "0.1.0"

KST = timezone(timedelta(hours=9))

MODES = ("dry_run", "shadow", "live")

# ── 대상 지수 (우선순위 순: 동률이면 앞쪽) ──
INDEXES: tuple[tuple[str, str], ...] = (
    ("^KS11", "코스피"),
    ("^KQ11", "코스닥"),
)
DATA_SOURCE_LABEL = "Yahoo Finance"

# ── 통계 ──
MIN_SAMPLE = 20                    # FR-02: 표본 미만이면 무발행 (default-deny)
HORIZONS: tuple[int, ...] = (1, 5, 20)
STREAK_MIN = 3                     # C2: 정확히 k일 연속, k ≥ 3
HIGH_LOW_WINDOW = 252              # C3: 직전 252거래일
# C1: 당일 등락률 구간 (단위 %, 하한 포함 여부는 stats.bucket_of 참조)
CHG_BUCKETS: tuple[tuple[str, float | None, float | None], ...] = (
    ("LE_M3", None, -3.0),
    ("M3_M2", -3.0, -2.0),
    ("M2_M1", -2.0, -1.0),
    ("FLAT", -1.0, 1.0),
    ("P1_P2", 1.0, 2.0),
    ("P2_P3", 2.0, 3.0),
    ("GE_P3", 3.0, None),
)

# ── 문안 ──
X_MAX_WEIGHTED = 280               # docs.x.com Counting characters (CJK 가중치 2)
FB_MAX_CHARS = 1000
HOOK_MAX_CHARS = 40
DISCLAIMER = "※ 과거 통계이며 투자 권유가 아닙니다"

# 기계 검사 금칙어 (default-deny, STD-LLM-08). G6에서 STD 정본으로 등록 예정(DN2026_0090 ④)
BANNED_TERMS: tuple[str, ...] = (
    "매수", "매도", "사세요", "파세요", "사라", "팔아라", "추천", "보장", "확실",
    "무조건", "100% 수익", "100% 확률", "급등 예정", "폭등 예정", "리딩", "종목 상담", "수익 인증",
    "원금", "대박", "손실 없",
)
# LLM 훅 전용 추가 금칙어 (R6-03: 우회 표현) — 템플릿 본문에는 적용하지 않는다
HOOK_BANNED_TERMS: tuple[str, ...] = (
    "담아", "담을", "매집", "줍줍", "주워", "기회", "타이밍", "전망", "들어가", "진입",
    "알림", "저장", "놓치", "지금 바로", "폭락", "폭등", "대폭",
)
# 반응 유도(engagement bait) — Meta 강등 정책
BAIT_TERMS: tuple[str, ...] = (
    "좋아요", "댓글", "공유해", "공유 부탁", "리트윗", "RT 부탁", "팔로우", "구독",
)

# ── 발행 ──
DELAY_MAX_DEFAULT_SEC = 1800       # STD-PUB-10 랜덤 지연 상한 기본값
DELAY_MAX_LIMIT_SEC = 2400         # yml timeout 60분 이내 유지 (STD-CI-13)
DAILY_POST_CAP = 1                 # D-2 전용 상한: 거래일당 1건 (원장 UNIQUE)

# ── 성과 수집 ──
METRIC_FIRST_HOURS = 24
METRIC_FINAL_HOURS = 72
METRIC_MAX_AGE_DAYS = 30           # X non_public/organic metrics 조회 가능 기간
FB_INSIGHT_METRICS = "post_media_view,post_clicks,post_reactions_by_type_total"


def is_enabled() -> bool:
    return os.environ.get("BACKDATA_ENABLED", "").strip().lower() == "true"


def get_mode() -> str:
    """BACKDATA_MODE 판독. 인식 불가·빈 값은 dry_run 강등 (fail-safe)."""
    mode = os.environ.get("BACKDATA_MODE", "").strip().lower()
    return mode if mode in MODES else "dry_run"


def get_delay_max_sec() -> int:
    """랜덤 지연 상한. 빈 값·비정상 값은 기본값, 상한 초과는 상한으로 자른다 (STD-CFG-01)."""
    raw = os.environ.get("BACKDATA_DELAY_MAX_SEC", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DELAY_MAX_DEFAULT_SEC
    return max(0, min(value, DELAY_MAX_LIMIT_SEC))
