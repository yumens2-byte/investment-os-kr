"""
following_engine/config.py
============================
Following Engagement Agent 설정 (요구사항서 v2 + 승인 Q2~Q5 반영).

환경변수 (GitHub Variables):
  FOLLOWING_ENABLED           — 'true'가 아니면 즉시 종료 (문서 34장 초기값 false)
  FOLLOWING_EXECUTION_MODE    — dry_run | shadow | live (불명 값 → dry_run 강등)
  FOLLOWING_MAX_ACTIONS_PER_RUN / FOLLOWING_MAX_ACTIONS_PER_DAY — 상한 오버라이드
"""

from __future__ import annotations

import os

from reply_engine.config import env_int

VERSION = "1.0.1"

# ── Decision 임계 (문서 13장, Q5 승인 초기값) ──
MIN_RELEVANCE_SCORE: int = env_int("FOLLOWING_MIN_RELEVANCE", 85)
MIN_CONTENT_VALUE: int = env_int("FOLLOWING_MIN_CONTENT", 80)
MIN_ENGAGEMENT_VALUE: int = env_int("FOLLOWING_MIN_ENGAGEMENT", 75)

MAX_ACTIONS_PER_RUN: int = env_int("FOLLOWING_MAX_ACTIONS_PER_RUN", 2)
MAX_ACTIONS_PER_DAY: int = env_int("FOLLOWING_MAX_ACTIONS_PER_DAY", 5)

AUTHOR_COOLDOWN_HOURS: int = env_int("FOLLOWING_AUTHOR_COOLDOWN_HOURS", 24)
DUP_SIMILARITY_THRESHOLD: float = 0.85   # 생성 텍스트 중복 (문서 13장)

# ── 수집/필터 (문서 7·9장) ──
MAX_FETCH: int = 100                     # 1콜 (Q4: 300 페이지네이션은 후순위)
MIN_TEXT_LENGTH: int = 30

# QUOTE 코멘트 규격
QUOTE_MAX_LENGTH: int = 200

# ── 관심 Topic (문서 10장 + 한국어 보강) ──
TOPICS_INCLUDE: tuple[str, ...] = (
    "ai", "artificial intelligence", "openai", "nvidia", "semiconductor",
    "data center", "stock", "market", "nasdaq", "s&p", "federal reserve",
    "inflation", "cpi", "ppi", "treasury", "interest rate", "energy", "oil",
    "defense", "economy",
    "인공지능", "엔비디아", "반도체", "데이터센터", "주식", "증시", "시장",
    "나스닥", "연준", "금리", "인플레이션", "물가", "국채", "유가", "방산",
    "경제", "실적", "환율", "코스피", "etf",
)
TOPICS_EXCLUDE: tuple[str, ...] = (
    "giveaway", "promotion", "discount", "이벤트 당첨", "추첨", "프로모션",
    "할인", "리딩방", "오픈채팅", "무료 체험", "수익 보장", "광고", "홍보",
)

# ── LIVE 허용 액션 (Q2 승인: QUOTE만. PERMITTED_REPLY는 REVIEW_ONLY 강등) ──
LIVE_ALLOWLIST: tuple[str, ...] = ("QUOTE",)


def is_enabled() -> bool:
    """기능 스위치 (문서 34장). 'true'일 때만 동작."""
    return os.environ.get("FOLLOWING_ENABLED", "").strip().lower() == "true"


def get_mode() -> str:
    """실행 모드. 불명 값은 dry_run 강등 — 임의 live 진입 금지 (문서 2장)."""
    mode = os.environ.get("FOLLOWING_EXECUTION_MODE", "dry_run").strip().lower()
    if mode not in ("dry_run", "shadow", "live"):
        return "dry_run"
    return mode
