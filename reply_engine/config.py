"""
reply_engine/config.py
========================
X Reply Engine 설정 상수 (config/settings.py 무수정 원칙 — 독립 관리).

환경변수 (GitHub Variables → workflow env):
  REPLY_ENABLED     — 'true'가 아니면 파이프라인 즉시 종료 (긴급 정지 스위치)
  REPLY_MODE        — dry_run | shadow | live (인식 불가 값은 dry_run으로 강등)
  DAILY_BUDGET_KRW  — 일일 비용 상한 (기본 1000)
  X_READ_COST_KRW   — X 읽기 1콜 단가 (미설정 시 count 모드 fallback)
  X_WRITE_COST_KRW  — X 쓰기 1콜 단가 (미설정 시 count 모드 fallback)
"""

from __future__ import annotations

import os

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 답글 정책 상한
# ---------------------------------------------------------------------------
REPLY_DAILY_CAP: int = int(os.environ.get("REPLY_DAILY_CAP", "8"))          # 일일 답글 상한
REPLY_AUTHOR_DAILY_CAP: int = int(os.environ.get("REPLY_AUTHOR_DAILY_CAP", "1"))  # 사용자별/일
REPLY_CONV_DAILY_CAP: int = int(os.environ.get("REPLY_CONV_DAILY_CAP", "3"))      # 대화별/일
REPLY_MAX_AGE_HOURS: int = int(os.environ.get("REPLY_MAX_AGE_HOURS", "24"))  # 폐기 (승인 D)

# 답글 텍스트 규격
REPLY_MAX_LENGTH: int = 40          # 공백 포함 최대 길이 ("한 줄 미만" 정책)
REPLY_SIMILARITY_THRESHOLD: float = 0.6   # 최근 발행분 대비 자카드 유사도 상한
REPLY_RECENT_COMPARE_COUNT: int = 30      # 유사도 비교 대상 최근 발행 건수

# 발행 간 지터 (초) — live 모드 전용
PUBLISH_JITTER_MIN_SEC: int = 40
PUBLISH_JITTER_MAX_SEC: int = 180

# 실행 시작 지터 (초) — live 모드 전용
STARTUP_JITTER_MAX_SEC: int = 300

# ---------------------------------------------------------------------------
# 수집 설정
# ---------------------------------------------------------------------------
MENTIONS_MAX_RESULTS: int = 50      # get_users_mentions 1콜 수집량 (5~100)

# ---------------------------------------------------------------------------
# 예산 count 모드 fallback 상한 (단가 미설정 시)
# ---------------------------------------------------------------------------
FALLBACK_READ_CALLS_PER_DAY: int = 8
FALLBACK_WRITE_CALLS_PER_DAY: int = 5

# ---------------------------------------------------------------------------
# 금지어 — 답글에 포함 시 발행 차단 (생성 오작동 신호로 간주)
# ---------------------------------------------------------------------------
BANNED_WORDS: tuple[str, ...] = (
    "매수", "매도", "수익 보장", "수익보장", "종목 추천", "종목추천",
    "리딩방", "오픈채팅", "텔레그램", "투자 권유",
)

# 스팸 휴리스틱 — 댓글에 포함 시 무응답
SPAM_KEYWORDS: tuple[str, ...] = (
    "리딩방", "오픈채팅", "수익 보장", "수익보장", "무료 체험", "무료체험",
    "종목 추천방", "카톡", "광고", "홍보",
)

# 스팸 계정 휴리스틱
SPAM_ACCOUNT_MIN_FOLLOWERS: int = 2
SPAM_ACCOUNT_MIN_AGE_DAYS: int = 30


# ---------------------------------------------------------------------------
# 환경변수 판독 헬퍼
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """긴급 정지 스위치. REPLY_ENABLED가 정확히 'true'일 때만 동작."""
    return os.environ.get("REPLY_ENABLED", "").strip().lower() == "true"


def get_mode() -> str:
    """REPLY_MODE 판독. 인식 불가 값은 dry_run으로 강등 (fail-safe)."""
    mode = os.environ.get("REPLY_MODE", "dry_run").strip().lower()
    if mode not in ("dry_run", "shadow", "live"):
        return "dry_run"
    return mode


def get_daily_budget_krw() -> float:
    """일일 비용 상한 (기본 1000원)."""
    raw = os.environ.get("DAILY_BUDGET_KRW", "1000").strip()
    try:
        return float(raw)
    except ValueError:
        return 1000.0


def get_cost_per_call() -> tuple[float | None, float | None]:
    """
    (읽기 단가, 쓰기 단가). 미설정/파싱 불가 시 None → count 모드 fallback.
    """
    def _parse(name: str) -> float | None:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    return _parse("X_READ_COST_KRW"), _parse("X_WRITE_COST_KRW")
