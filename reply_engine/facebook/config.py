"""
reply_engine/facebook/config.py
================================
Facebook Reply Engine 설정 (FB-1, 2026-10-07).

명명 규칙: Facebook 전용 Secret/Variable은 FACE_ 접두를 사용한다.
판독 규칙은 X와 같은 공통 헬퍼(env_int_clamped / env_bool / is_enabled / get_mode)를 쓴다.
X 변수(REPLY_*)는 읽지 않으므로 한쪽 조정이 다른 쪽에 영향을 주지 않는다.

Secrets (GitHub Secrets → workflow env):
  FACE_PAGE_ID      — 숫자 Page ID (investment-os main.yml과 동일하게 Secret)
  FACE_PAGE_TOKEN   — Page access token

Variables:
  FACE_REPLY_ENABLED — 'true'가 아니면 즉시 종료 (X와 독립된 긴급 정지 스위치)
  FACE_REPLY_MODE    — dry_run | shadow | live (인식 불가 값은 dry_run 강등)
  FACE_REPLY_*       — 아래 상수 참조 (범위 밖 값은 기본값으로 복귀)

결정 근거 (이해관계자 5인 논의, 2026-10-07):
  D5 상한 — 테스트 모드 앱·정책 차단 코드(368) 존재를 고려한 보수 시작값 5/1/1/2.
  D10 범위 — 최상위 댓글만 기본. 내 Page 댓글에 달린 대댓글은 opt-in, 회당 1.
"""

from __future__ import annotations

import os

from reply_engine.config import env_bool, env_int_clamped, get_mode, is_enabled
from reply_engine.store import ReplyTables

VERSION = "1.0.0"

PLATFORM = "facebook"

# investment-os config/settings.py FACE_GRAPH_BASE와 같은 버전을 사용한다.
GRAPH_BASE = "https://graph.facebook.com/v25.0"
HTTP_TIMEOUT_SEC = 30

# ── 답글 정책 상한 (D5) ──
FACE_REPLY_DAILY_CAP: int = env_int_clamped("FACE_REPLY_DAILY_CAP", 5, 1, 50)
# 상한 5: 최악 지연 300 + 600 + 4×180 = 1620초(27분) < job timeout 45분 (리뷰 #7)
FACE_REPLY_RUN_CAP: int = env_int_clamped("FACE_REPLY_RUN_CAP", 1, 1, 5)
FACE_REPLY_AUTHOR_DAILY_CAP: int = env_int_clamped("FACE_REPLY_AUTHOR_DAILY_CAP", 1, 1, 10)
# conversation = 게시물(post) 단위
FACE_REPLY_POST_DAILY_CAP: int = env_int_clamped("FACE_REPLY_POST_DAILY_CAP", 2, 1, 20)
FACE_REPLY_MAX_AGE_HOURS: int = env_int_clamped("FACE_REPLY_MAX_AGE_HOURS", 24, 1, 168)
FACE_REPLY_RETRY_WINDOW_HOURS: int = env_int_clamped("FACE_REPLY_RETRY_WINDOW_HOURS", 24, 1, 168)

# ── 응답 범위 (D10) ──
# 내 Page 댓글에 직접 달린 대댓글 응답. P-1(주객전도) 방어상 기본 비활성.
FACE_REPLY_THREAD_ENABLED: bool = env_bool("FACE_REPLY_THREAD_ENABLED", False)
FACE_REPLY_THREAD_RUN_CAP: int = env_int_clamped("FACE_REPLY_THREAD_RUN_CAP", 1, 1, 3)

# ── 수집 범위 (D1: 피드 게시물만) ──
FACE_REPLY_POST_LOOKBACK: int = env_int_clamped("FACE_REPLY_POST_LOOKBACK", 10, 1, 25)
FACE_REPLY_COMMENT_MAX_PAGES: int = env_int_clamped("FACE_REPLY_COMMENT_MAX_PAGES", 2, 1, 5)
COMMENTS_PAGE_SIZE: int = 100

# ── 호출 예산 (count 모드 — Graph API에는 X식 KRW 단가가 없다) ──
# 4회/일 × (피드 1 + 게시물 10 × 최대 2페이지) = 84 + Preflight 여유.
FACE_REPLY_READ_CALLS_PER_DAY: int = env_int_clamped(
    "FACE_REPLY_READ_CALLS_PER_DAY", 120, 10, 1000
)
FACE_REPLY_WRITE_CALLS_PER_DAY: int = env_int_clamped(
    "FACE_REPLY_WRITE_CALLS_PER_DAY", 10, 1, 100
)
# X-Business-Use-Case-Usage 최댓값(%)이 이 이상이면 회차 내 추가 호출을 중단한다.
FACE_REPLY_BUC_STOP_PCT: int = env_int_clamped("FACE_REPLY_BUC_STOP_PCT", 80, 10, 100)

# ── 안티봇 지연 (live 전용, X와 독립 값) ──
STARTUP_JITTER_MAX_SEC: int = 300
PUBLISH_JITTER_MIN_SEC: int = 40
PUBLISH_JITTER_MAX_SEC: int = 180
PUBLISH_START_DELAY_MAX_SEC: int = env_int_clamped("FACE_REPLY_PUBLISH_DELAY_MAX_SEC", 600, 0, 600)

# ── 관측 ──
# Page 게시 빈도가 X보다 낮아 24h 무신규가 정상일 수 있으므로 X(24h)보다 길게 둔다.
FACE_REPLY_CURSOR_STALE_WARN_HOURS: int = env_int_clamped(
    "FACE_REPLY_CURSOR_STALE_WARN_HOURS", 72, 1, 720
)
FACE_REPLY_RECENT_COMPARE_COUNT: int = 30

ACCOUNT = "fb_main"  # fb_reply_cursor.account 키

ALERT_PREFIX = "[FB Reply]"

# D8: X와 컬럼 계약이 같은 FB 전용 테이블 (kr_reply_* 무접촉).
FB_TABLES = ReplyTables(
    history="fb_reply_history",
    cursor="fb_reply_cursor",
    budget="fb_reply_budget",
    blacklist="fb_reply_blacklist",
    likes=None,
    max_age_hours=FACE_REPLY_MAX_AGE_HOURS,
    retry_window_hours=FACE_REPLY_RETRY_WINDOW_HOURS,
)


def is_face_enabled() -> bool:
    """FB 긴급 정지 스위치. FACE_REPLY_ENABLED가 정확히 'true'일 때만 동작."""
    return is_enabled("FACE_REPLY_ENABLED")


def get_face_mode() -> str:
    """FACE_REPLY_MODE 판독. 인식 불가 값은 dry_run 강등."""
    return get_mode("FACE_REPLY_MODE")


def get_page_id() -> str:
    """FACE_PAGE_ID — 숫자 문자열만 유효 (R-10: 오등록 조기 차단)."""
    raw = os.environ.get("FACE_PAGE_ID", "").strip()
    return raw if raw.isdigit() else ""


def get_page_token() -> str:
    return os.environ.get("FACE_PAGE_TOKEN", "").strip()
