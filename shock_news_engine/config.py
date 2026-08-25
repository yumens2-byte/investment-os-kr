"""
shock_news_engine/config.py
=============================
SHOCK_NEWS — 당일 충격 사건 발행 설정 (2026-08-22 마스터 승인 설계).

승인 확정:
  - 슬롯: KST 16시대(한국 기사) / 04시대(미국 기사), 발행 분은 랜덤 (안티봇)
  - 티어: 1 살인 > 2 실종 > 3 폭행·강력 > 4 보조(사망사고 등)
  - 안전 게이트(완화 불가): 실명·유죄 단정·잔혹 상세 금지
  - 의견 유도 문구는 결정적 seed 기반 50% 포함 (Q3 권고안)
  - 초기 배포: SHOCK_ENABLED=false, SHOCK_EXECUTION_MODE=dry_run

소재 전환 대비: 티어 키워드는 아래 튜플만 교체하면 경제 사건 버전으로 전환 가능.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from reply_engine.config import env_int

VERSION = "1.2.0"

KST = timezone(timedelta(hours=9))

# ── 슬롯 정의 ──
# cron(KST 15:55/03:55) 진입 후 슬롯 시간대(16시대/04시대) 내 랜덤 분에 발행
SLOT_KR_HOURS = (15, 16)     # KST 15~16시 진입 → KR16 슬롯
SLOT_US_HOURS = (3, 4)       # KST 03~04시 진입 → US04 슬롯

# ── 발행 분 랜덤화 (안티봇) — live 전용 ──
PUBLISH_WINDOW_SEC: int = env_int("SHOCK_PUBLISH_WINDOW_SEC", 3500)  # 슬롯 정시 후 0~58분

# ── 소재 티어 (숫자 낮을수록 우선) ──
TIER_KEYWORDS: dict[int, tuple[str, ...]] = {
    1: ("살인", "살해", "피살", "숨진 채", "시신", "타살",
        "murder", "murdered", "homicide", "shot dead", "stabbed to death"),
    2: ("실종", "행방불명", "missing", "disappeared", "vanished"),
    3: ("폭행", "상해", "흉기", "강도", "납치", "감금", "성폭행", "학대",
        "assault", "attacked", "kidnap", "robbery", "abuse"),
    4: ("사망", "추락", "참변", "화재", "붕괴", "충돌",
        "dead", "death", "fatal", "crash", "collapse"),
}

# ── 미성년 관련 사건 제외 (O-1, 2026-08-25 마스터 지시) ──
# 계정 리스크: 미성년 피해자·피의자 사건은 확산 시 2차 가해 논란으로 이어질 수 있고,
# 국내법상 소년범 신상 보도 제약도 강하다. 게이트가 아니라 **후보 단계에서 배제**한다.
# 오탈락(성인 사건인데 걸림)은 무발행 방향이라 안전한 손실로 수용.
MINOR_KEYWORDS: tuple[str, ...] = (
    # KR
    "미성년", "청소년", "초등학생", "중학생", "고등학생", "여중생", "남중생",
    "여고생", "남고생", "여학생", "남학생", "10대", "십대", "아동", "유아",
    "어린이", "영아", "신생아", "소년범", "학대아동", "보육원", "어린이집",
    "유치원", "초등학교", "중학교", "고등학교", "미취학", "친딸", "친아들",
    # EN
    "minor", "teen", "teenage", "teenager", "child", "children", "toddler",
    "infant", "baby", "schoolgirl", "schoolboy", "juvenile", "kindergarten",
    "elementary school", "high school", "middle school",
)

# 나이 표기 감지 — 18세 미만이면 제외 ("16세", "17-year-old", "15살")
_MINOR_AGE_PATTERN = re.compile(
    r"(\d{1,2})\s?(?:세|살|-?\s?year[-\s]?old|yo\b)", re.IGNORECASE
)
MINOR_AGE_THRESHOLD = 18


def involves_minor(title: str) -> bool:
    """제목에 미성년 관련 신호가 있으면 True (O-1)."""
    text = (title or "").lower()
    if any(kw.lower() in text for kw in MINOR_KEYWORDS):
        return True
    for raw_age in _MINOR_AGE_PATTERN.findall(text):
        try:
            if int(raw_age) < MINOR_AGE_THRESHOLD:
                return True
        except ValueError:
            continue
    return False


# ── RSS 소스 (키 불요) ──
RSS_SOURCES: dict[str, tuple[str, ...]] = {
    "KR": (
        "https://www.yna.co.kr/rss/society.xml",
        "https://news.google.com/rss/search?q=%EC%82%B4%EC%9D%B8%20OR%20%EC%8B%A4%EC%A2%85%20OR%20%ED%8F%AD%ED%96%89%20when:1d&hl=ko&gl=KR&ceid=KR:ko",
    ),
    "US": (
        "https://news.google.com/rss/search?q=murder%20OR%20missing%20OR%20assault%20when:1d&hl=en-US&gl=US&ceid=US:en",
    ),
}
RSS_TIMEOUT_SEC = 10
MAX_ARTICLES_PER_SOURCE = 50
ARTICLE_MAX_AGE_HOURS = 24

# ── 선정/생성 ──
RANK_CANDIDATE_LIMIT = 10
COMMENT_MAX_LENGTH = 200
ENGAGE_PROMPT_RATE = 0.5          # 의견 유도 문구 포함 비율 (결정적 seed)
TITLE_SIMILARITY_THRESHOLD = 0.6  # L3: 동일 사건 이종 기사 차단
RECENT_TITLE_DAYS = 7


def is_enabled() -> bool:
    return os.environ.get("SHOCK_ENABLED", "").strip().lower() == "true"


def get_mode() -> str:
    """SHOCK_EXECUTION_MODE 판독. 인식 불가 값은 dry_run 강등 (fail-safe)."""
    mode = os.environ.get("SHOCK_EXECUTION_MODE", "dry_run").strip().lower()
    if mode not in ("dry_run", "shadow", "live"):
        return "dry_run"
    return mode


def determine_slot(now_kst: datetime, mode: str = "live") -> tuple[str, str] | None:
    """
    현재 KST 시각 → (slot_key, session) | None.

    live: 정규 슬롯 시간대(KST 15~16시 / 03~04시)에만 실행. 그 외 None.
    dry_run/shadow (2026-08-24 승인): **시간 무관 실행**. 슬롯 밖이면 시간대로 세션을 유추하고
      slot_key에 '-adhoc-HHMM' 접미를 붙인다 —
      ① 검증 실행을 반복해도 매번 통과 (L2에 막히지 않음)
      ② 그날 정규 슬롯의 L2 방어는 그대로 보존 (애드혹 키와 정규 키가 다름)
      ③ 동일 기사 반복 적재는 L1(article_hash PK)이 계속 차단
    slot_key 예: 정규 '20260824-KR16' / 애드혹 '20260824-KR16-adhoc-1432'.
    SHOCK_FORCE_SLOT(KR16|US04)은 세션 강제 지정용으로 계속 유효.
    """
    date_str = now_kst.strftime("%Y%m%d")
    forced = os.environ.get("SHOCK_FORCE_SLOT", "").strip().upper()

    if forced in ("KR16", "US04"):
        session = "KR" if forced == "KR16" else "US"
        in_regular = (
            now_kst.hour in (SLOT_KR_HOURS if forced == "KR16" else SLOT_US_HOURS)
        )
        if in_regular or mode == "live":
            return f"{date_str}-{forced}", session
        return f"{date_str}-{forced}-adhoc-{now_kst.strftime('%H%M')}", session

    if now_kst.hour in SLOT_KR_HOURS:
        return f"{date_str}-KR16", "KR"
    if now_kst.hour in SLOT_US_HOURS:
        return f"{date_str}-US04", "US"

    if mode in ("dry_run", "shadow"):
        # 슬롯 밖 검증 실행 — 시간대로 세션 유추 (KST 정오 이후는 한국, 이전은 미국)
        slot_name = "KR16" if now_kst.hour >= 12 else "US04"
        session = "KR" if slot_name == "KR16" else "US"
        return f"{date_str}-{slot_name}-adhoc-{now_kst.strftime('%H%M')}", session

    return None
