"""
following_engine/config.py
============================
Following Engagement Agent 설정 (요구사항서 v2 + 승인 Q2~Q5 반영).

환경변수 (GitHub Variables):
  FOLLOWING_ENABLED           — 'true'가 아니면 즉시 종료 (문서 34장 초기값 false)
  FOLLOWING_EXECUTION_MODE    — dry_run | shadow | live (불명 값 → dry_run 강등)
  FOLLOWING_RUN_TARGET_MIN / FOLLOWING_RUN_TARGET_MAX — 실행별 무작위 후보 상한 범위
  FOLLOWING_MAX_ACTIONS_PER_RUN / FOLLOWING_MAX_ACTIONS_PER_DAY — 절대 상한 오버라이드
  FOLLOWING_NEAR_MISS_REVIEW_ENABLED — 점수 미달 검수 후보 적재(기본 false)
"""

from __future__ import annotations

import os
import random

from reply_engine.config import env_int

VERSION = "1.3.0"

# ── Decision 임계 (문서 13장, Q5 승인 초기값) ──
MIN_RELEVANCE_SCORE: int = env_int("FOLLOWING_MIN_RELEVANCE", 85)
MIN_CONTENT_VALUE: int = env_int("FOLLOWING_MIN_CONTENT", 80)
MIN_ENGAGEMENT_VALUE: int = env_int("FOLLOWING_MIN_ENGAGEMENT", 75)

# T-4 (2026-08-24): 점수 소폭 미달 구간을 자동 발행 없이 REVIEW_ONLY(마스터 수동 후보)로 적재.
# 볼륨 보완의 안전 축 — R이 이 값 이상이면 near-miss 승격 대상.
REVIEW_MIN_RELEVANCE: int = env_int("FOLLOWING_REVIEW_MIN_RELEVANCE", 75)

MAX_ACTIONS_PER_RUN: int = env_int("FOLLOWING_MAX_ACTIONS_PER_RUN", 5)
MAX_ACTIONS_PER_DAY: int = env_int("FOLLOWING_MAX_ACTIONS_PER_DAY", 24)

# 한 시간마다 목표 수만 1~5에서 무작위로 정한다. 이는 발행 하한이 아니라 상한이다.
# 품질 게이트를 통과한 글이 없으면 0건이 정상이며 절대 상한을 넘을 수 없다.
RUN_TARGET_MIN: int = env_int("FOLLOWING_RUN_TARGET_MIN", 1)
RUN_TARGET_MAX: int = env_int("FOLLOWING_RUN_TARGET_MAX", 5)

AUTHOR_COOLDOWN_HOURS: int = env_int("FOLLOWING_AUTHOR_COOLDOWN_HOURS", 24)
DUP_SIMILARITY_THRESHOLD: float = 0.85   # 생성 텍스트 중복 (문서 13장)

# ── 수집/필터 (문서 7·9장) ──
MAX_FETCH: int = 100                     # 1콜 (Q4: 300 페이지네이션은 후순위)
MIN_TEXT_LENGTH: int = 30

# QUOTE 코멘트 규격
QUOTE_MAX_LENGTH: int = 200
# 실제 자동 생성 코멘트는 모바일 타임라인에서 한눈에 읽히는 한 문장으로 제한한다.
COMMENT_MAX_LENGTH: int = 60

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
    # 계정 핵심 범위 밖이며 맥락·정책 위험이 큰 정치/가상자산은 AI 키워드가 함께 있어도 제외.
    "trump", "biden", "election", "congress", "democrat", "republican",
    "대통령", "국회의원", "총선", "대선", "bitcoin", "crypto", "xrp", "rlusd",
    "비트코인", "암호화폐", "가상자산", "코인",
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


def is_live_publish_approved() -> bool:
    """타인 글 자동 인용의 이중 승인 스위치. 둘 다 정확히 true여야 한다."""
    enabled = os.environ.get("FOLLOWING_LIVE_PUBLISH_ENABLED", "").strip().lower()
    approved = os.environ.get("FOLLOWING_LIVE_APPROVED", "").strip().lower()
    return enabled == "true" and approved == "true"


def is_near_miss_review_enabled() -> bool:
    """점수 미달 후보는 명시적으로 opt-in한 shadow 검수에서만 승격한다."""
    return os.environ.get("FOLLOWING_NEAR_MISS_REVIEW_ENABLED", "").strip().lower() == "true"


def get_trusted_author_ids() -> frozenset[str]:
    """LIVE 인용을 허용한 X 사용자 ID. 숫자 ID만 인정하며 빈 목록은 전건 차단한다."""
    raw = os.environ.get("FOLLOWING_TRUSTED_AUTHOR_IDS", "")
    return frozenset(value.strip() for value in raw.split(",") if value.strip().isdigit())


def choose_run_target(rng: random.Random | random.SystemRandom | None = None) -> int:
    """이번 실행의 후보 상한을 선택한다(설정 오류는 보수적으로 보정)."""
    lower = max(1, min(5, RUN_TARGET_MIN))
    upper = max(1, min(5, RUN_TARGET_MAX))
    if lower > upper:
        lower, upper = upper, lower
    absolute_cap = max(0, min(5, MAX_ACTIONS_PER_RUN))
    if absolute_cap == 0:
        return 0
    return min((rng or random.SystemRandom()).randint(lower, upper), absolute_cap)
