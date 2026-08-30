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
  REPLY_DAILY_CAP   — 일일 답글 상한 (기본 8, 운영값은 GitHub Variables로 관리)

v1.0.6 (2026-08-27): 정책 상수 원복 — 운영 상한 변경은 코드가 아닌
  GitHub Variables(REPLY_DAILY_CAP)로 관리 (테스트 게이트 6건 실패 원인 해소).
  FALLBACK 상수는 단가 미설정 시 비상 보수 경로이므로 저상한(16/5) 유지.

v1.1.0 (2026-08-30, R-3): 멘션 수집 상한을 API 하한 5 → 상한 100으로 상향.
  읽기 콜 수는 max_results와 무관하게 1콜이므로 예산 영향 0.
  실측(08-30 artifact) collected=5 = 상한 포화 → 초과분이 커서 전진으로 영구 소실.
  범위 밖 변수 오입력이 X API 400을 유발하지 않도록 env_int_clamped 도입.
v1.2.0 (2026-08-30, R-9): REPLY_NON_KR_LATIN_THRESHOLD 신설.
  외국어 댓글에 AI가 의도를 지어낸 답글을 발행한 라이브 사고 대응.
  차단이 아니라 '정형 문구 전환' 임계로 사용한다 (마스터 확정 C안).
"""

from __future__ import annotations

import os

VERSION = "1.2.0"


def env_int(name: str, default: int) -> int:
    """
    int 환경변수 안전 파서 (H-1, 2026-08-20).
    yml이 미등록 GitHub Variable을 전달하면 env가 빈 문자열('')로 설정되어
    int('')가 import 시점에 크래시하는 결함의 근본 수정 — 빈 값/파싱 불가는 기본값.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_int_clamped(name: str, default: int, lo: int, hi: int) -> int:
    """
    범위 강제 int 환경변수 파서 (R-3, 2026-08-30).
    API 허용 범위를 벗어난 변수 오입력이 런타임 400을 유발하는 것을 차단한다.
    범위 밖 값은 조용히 잘라내지 않고 기본값으로 되돌린다 (오입력 은폐 방지).
    """
    value = env_int(name, default)
    if value < lo or value > hi:
        return default
    return value


# ---------------------------------------------------------------------------
# 답글 정책 상한
# ---------------------------------------------------------------------------
REPLY_DAILY_CAP: int = env_int("REPLY_DAILY_CAP", 8)           # 일일 답글 상한 (운영값: Variables)
REPLY_AUTHOR_DAILY_CAP: int = env_int("REPLY_AUTHOR_DAILY_CAP", 1)  # 사용자별/일
REPLY_CONV_DAILY_CAP: int = env_int("REPLY_CONV_DAILY_CAP", 3)      # 대화별/일
REPLY_MAX_AGE_HOURS: int = env_int("REPLY_MAX_AGE_HOURS", 24)  # 폐기 (승인 D)

# 답글 텍스트 규격
REPLY_MAX_LENGTH: int = 40          # 공백 포함 최대 길이 ("한 줄 미만" 정책)
REPLY_SIMILARITY_THRESHOLD: float = 0.6   # 최근 발행분 대비 자카드 유사도 상한
REPLY_RECENT_COMPARE_COUNT: int = 30      # 유사도 비교 대상 최근 발행 건수

# 발행 간 지터 (초) — live 모드 전용
PUBLISH_JITTER_MIN_SEC: int = 40
PUBLISH_JITTER_MAX_SEC: int = 180

# 실행 시작 지터 (초) — live 모드 전용
STARTUP_JITTER_MAX_SEC: int = 300

# 발행 직전 랜덤 딜레이 상한 (초) — live 모드 전용, 첫 발행 직전 1회 적용 (안티봇)
# cron 시각 + 처리시간으로 발행 시각이 고정 패턴화되는 것을 방지 (마스터 지시 2026-08-17)
PUBLISH_START_DELAY_MAX_SEC: int = env_int("REPLY_PUBLISH_DELAY_MAX_SEC", 600)

# ---------------------------------------------------------------------------
# 수집 설정
# ---------------------------------------------------------------------------
# R-3 (2026-08-30): 5(API 하한) → 100(API 상한).
# get_users_mentions는 max_results와 무관하게 읽기 1콜이므로 예산 증분 0.
# 범위 밖 변수는 env_int_clamped가 기본값으로 되돌린다 (X API 400 차단).
MENTIONS_MAX_RESULTS: int = env_int_clamped("REPLY_MENTIONS_MAX_RESULTS", 100, 5, 100)

# ---------------------------------------------------------------------------
# 예산 count 모드 fallback 상한 (단가 미설정 시)
# ---------------------------------------------------------------------------
# reply(멘션1+루트1)×4회 + following(타임라인1)×2회 + 여유 (2026-08-20 Q4 승인)
FALLBACK_READ_CALLS_PER_DAY: int = 16
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
# 언어 판정 (R-9, 2026-08-30)
# ---------------------------------------------------------------------------
# 멘션 제외 후 한글이 없고 라틴 문자가 이 값 이상이면 외국어 댓글로 판정한다.
# 외국어 댓글은 무응답이 아니라 AI 생성 대신 정형 문구 풀을 사용한다 (C안).
# 3자: "Nice", "GOOD" 등 짧은 영어 칭찬까지 정형 문구로 처리 — 보수적 기본값.
# 이모지·숫자·기호 전용 댓글("👍", "!!!")은 라틴 0자라 영향받지 않는다.
REPLY_NON_KR_LATIN_THRESHOLD: int = env_int("REPLY_NON_KR_LATIN_THRESHOLD", 3)


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


def get_my_user_id() -> str:
    """
    X_MY_USER_ID 변수 (선택). 설정 시 get_me 호출 생략 (읽기 1콜 절약).
    숫자 문자열만 유효 — 그 외는 미설정 취급.
    """
    raw = os.environ.get("X_MY_USER_ID", "").strip()
    return raw if raw.isdigit() else ""
