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
v1.3.0 (2026-08-30, R-10/B): env_bool 헬퍼 신설.
  R-10 — REPLY_CURSOR_STALE_WARN_HOURS (커서 정체 경고 임계).
  B안  — REPLY_FOREIGN_THREAD_ENABLED / _RUN_CAP (타인 스레드 저상한 허용).
"""

from __future__ import annotations

import os

VERSION = "1.3.0"


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


def env_bool(name: str, default: bool) -> bool:
    """
    bool 환경변수 파서 (R-10/B, 2026-08-30).
    미설정·빈 문자열이면 기본값. 'true'/'1'/'yes'만 참으로 본다(대소문자 무시).
    2026-08-20 사고(REPLY_MODE에 'true' 오입력)를 감안해 관대한 파싱은 하지 않는다.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes")


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
# 커서 무결성 관측 (R-10, 2026-08-30)
# ---------------------------------------------------------------------------
# 커서는 신규 멘션이 있을 때만 전진하므로, updated_at이 오래 정체됐다는 것은
# "장시간 신규 멘션 없음"을 뜻한다. 수집 0건이 정상(커서 동작)인지
# 이상(X_MY_USER_ID 오등록 등)인지 구분하는 지표로 쓴다.
REPLY_CURSOR_STALE_WARN_HOURS: int = env_int("REPLY_CURSOR_STALE_WARN_HOURS", 24)

# ---------------------------------------------------------------------------
# 타인 스레드 응답 (B안, 2026-08-30 마스터 승인)
# ---------------------------------------------------------------------------
# "내가 타인 게시글에 단 댓글"에 달린 대댓글은 in_reply_to_user_id가 나이므로
# 나에게 직접 말을 건 것이지만, 원 게시글 작성자가 타인이라 P-1에서 차단돼 왔다.
# 실측 표본에서 탈락분의 100%(수집의 37.5%)를 차지해 발행량 병목이었다.
# 남의 스레드에서의 자동 답글은 스팸으로 비칠 수 있으므로 회당 저상한을 둔다.
#
# 주의: 일일 상한이 아니라 '회당' 상한이다. kr_reply_history에 타인 스레드
# 여부를 저장하는 컬럼이 없어 DB 기준 일일 집계가 불가능하기 때문이며,
# 스키마 변경 없이 안전하게 제한하기 위한 설계다.
# cron 4회 기준 실질 일 상한 = REPLY_FOREIGN_THREAD_RUN_CAP × 4.
# ⚠️ 기본 비활성(opt-in). P-1(2026-08-18)은 실사고 대응 방어선이다:
#    내가 타인 글에 축하 댓글 → 글 주인이 "축하해주셔서 감사합니다" 답글 →
#    봇이 "축하해주셔서 진심으로 감사합니다"로 미러링한 주객전도 사고.
#    근본 원인은 원글 컨텍스트 부재이며 해당 패치(v1.1.0 원글 주입)는 아직 미반영이다.
#    따라서 이 값을 true로 켜면 그 사고 시나리오가 다시 열린다.
#    GATE_ECHO가 어휘 중복은 잡지만("축하해주셔서..." 재현 시 차단 실측 확인),
#    의미 역전("축하드려요! 🎉")은 통과하므로 2차 방어선은 불완전하다.
REPLY_FOREIGN_THREAD_ENABLED: bool = env_bool("REPLY_FOREIGN_THREAD_ENABLED", False)
REPLY_FOREIGN_THREAD_RUN_CAP: int = env_int("REPLY_FOREIGN_THREAD_RUN_CAP", 1)


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
