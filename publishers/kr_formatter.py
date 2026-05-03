"""
KR Market OS — X 콘텐츠 포맷터
날짜 기반 seed로 제목 / 포맷 타입 / 해시태그를 유동 선택.
동일 날짜 재실행 시 동일 결과 보장 (멱등성).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime

from config.settings import MAX_TWEET_LENGTH

VERSION = "1.1.0"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 유동화 풀 정의
# ---------------------------------------------------------------------------

TITLE_PATTERNS: list[str] = [
    "📊 국장 장전 거시체크 | {date}",
    "🇰🇷 오늘의 국장 환경 | {date}",
    "📋 국장 개장 전 점검 | {date}",
    "🔍 국장 거시 브리핑 | {date}",
    "📌 장전 체크리스트 | {date}",
    "🌏 국장 오전 거시지표 | {date}",
    "📈 국장 투자환경 점검 | {date}",
    "🧭 장전 나침반 | {date}",
    "📡 국장 신호 체크 | {date}",
    "💡 오늘 국장 포인트 | {date}",
    "⚡ 국장 거시 스냅샷 | {date}",
    "🎯 장전 핵심지표 | {date}",
    "🗺️ 국장 환경지도 | {date}",
    "📰 국장 매크로 브리핑 | {date}",
    "🏦 오늘 국장 거시환경 | {date}",
]

HASHTAG_POOL: list[str] = [
    # 국장 기본
    "#국장", "#KOSPI", "#코스피", "#KOSDAQ", "#코스닥",
    # 거시
    "#환율", "#거시경제", "#매크로", "#금리", "#달러", "#원달러",
    # 투자
    "#주식", "#주식투자", "#국내주식", "#ETF",
    # 종목
    "#삼성전자", "#SK하이닉스", "#반도체",
    # 미국
    "#미국금리", "#달러지수", "#연준", "#Fed",
    # 외인
    "#외인수급", "#외국인매매",
    # 시황
    "#장전시황", "#오늘시황", "#투자참고", "#시장분석",
]

# 섹터 해시태그 풀 — tweet3 전용 (seed 기반 3개 선택)
SECTOR_HASHTAG_POOL: list[str] = [
    "#섹터분석", "#국장섹터", "#코스피섹터", "#테마분석",
    "#반도체주", "#2차전지", "#AI주식", "#플랫폼주",
    "#바이오주", "#자동차주", "#금융주", "#원자재",
    "#수급분석", "#섹터흐름", "#투자테마",
]

# Type A 고정 (2026-05-04 확정 / B·C·D 함수는 향후 확장용으로 코드 보존)
FORMAT_TYPES: list[str] = ["A", "B", "C", "D"]

# ---------------------------------------------------------------------------
# 시그널 텍스트 매핑
# ---------------------------------------------------------------------------

SIGNAL_EMOJI: dict[str, str] = {
    "위험": "🔴",
    "주의": "🟡",
    "중립": "⚪",
    "우호": "🟢",
}

FOREIGN_PRESSURE_TEXT: dict[str, str] = {
    "HIGH": "달러 강세 지속 → 외인 매도 압박 구간",
    "MEDIUM": "외인 수급 방향성 중립",
    "LOW": "달러 약세 전환 → 외인 유입 우호",
}

RATE_BURDEN_TEXT: dict[str, str] = {
    "HIGH": "미국 금리 부담 높음 → EM 자금이탈 주의",
    "MEDIUM": "금리 영향 중립 구간",
    "LOW": "금리 부담 완화 → 밸류에이션 지지",
}

ACTION_TEXT: dict[str, str] = {
    "위험": "단기 방어 포지션 고려",
    "주의": "환율·외인 동향 모니터링",
    "중립": "시장 방향성 확인 후 대응",
    "우호": "위험자산 비중 확대 검토",
}

KRW_REGIME_LABEL: dict[str, str] = {
    "STRONG": "강세",
    "NEUTRAL": "보합",
    "WEAK": "약세",
}


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def format_daily_tweet(
    market_data: dict,
    signal_result: dict,
    seed: int | None = None,
    sector_data: list[dict] | None = None,
) -> list[str]:
    """
    X 발행용 트윗 생성.
    - tweet[0]: 지표 본문 (포맷 타입에 따라 변형)
    - tweet[1]: 시그널 판정 + 해시태그
    - tweet[2]: 섹터 흐름 비율 (sector_data 제공 시에만 추가)
    seed=None 이면 오늘 날짜 기반 자동 생성 (멱등성 보장).
    """
    if seed is None:
        seed = _get_daily_seed()

    title = _select_title(seed)
    fmt_type = _select_format_type(seed)
    hashtags = _select_hashtags(seed)

    logger.info(f"[Formatter] seed={seed} type={fmt_type} title='{title}'")

    # 포맷 타입별 tweet1 생성
    dispatch = {"A": _format_type_a, "B": _format_type_b, "C": _format_type_c, "D": _format_type_d}
    tweet1 = dispatch[fmt_type](market_data, title)
    tweet2 = _format_signal_tweet(signal_result, hashtags)

    tweets = [_truncate(tweet1), _truncate(tweet2)]

    # 섹터 데이터 있으면 tweet3 추가
    if sector_data:
        tweet3 = format_sector_tweet(sector_data, seed=seed)
        if tweet3:
            tweets.append(_truncate(tweet3))

    return tweets


# ---------------------------------------------------------------------------
# seed 기반 선택 함수
# ---------------------------------------------------------------------------

def _get_daily_seed() -> int:
    """오늘 날짜 기반 seed 생성 (100000 범위)."""
    today = date.today().isoformat()
    return int(hashlib.md5(today.encode()).hexdigest(), 16) % 100000


def _select_title(seed: int) -> str:
    today_str = datetime.now().strftime("%m/%d")
    pattern = TITLE_PATTERNS[seed % len(TITLE_PATTERNS)]
    return pattern.format(date=today_str)


def _select_format_type(seed: int) -> str:
    return FORMAT_TYPES[(seed // 3) % len(FORMAT_TYPES)]


def _select_hashtags(seed: int) -> str:
    """28개 풀에서 중복 없이 5개 분산 추출."""
    selected: list[str] = []
    used: set[int] = set()
    for i in range(5):
        idx = (seed * (i + 7) + i * 31) % len(HASHTAG_POOL)
        attempts = 0
        while idx in used and attempts < len(HASHTAG_POOL):
            idx = (idx + 1) % len(HASHTAG_POOL)
            attempts += 1
        used.add(idx)
        selected.append(HASHTAG_POOL[idx])
    return " ".join(selected)


# ---------------------------------------------------------------------------
# 포맷 타입별 tweet1 생성
# ---------------------------------------------------------------------------

def _format_type_a(data: dict, title: str) -> str:
    """Type A — 지표나열형 (기본)."""
    lines = [
        title,
        "",
        f"🇰🇷 KOSPI  {_fmt_price(data.get('kospi'))}  {_fmt_chg_pct(data.get('kospi_chg_pct'))}",
        f"🇰🇷 KOSDAQ {_fmt_price(data.get('kosdaq'))}  {_fmt_chg_pct(data.get('kosdaq_chg_pct'))}",
        f"💵 원/달러  {_fmt_krw(data.get('krw_usd'))}원  {_fmt_krw_chg(data.get('krw_usd_chg'))}",
        f"📈 미국10Y  {_fmt_rate(data.get('us10y'))}%  {_fmt_rate_chg(data.get('us10y_chg'))}",
        f"💹 달러지수  {_fmt_dxy(data.get('dxy'))}  {_fmt_pct(data.get('dxy_chg_pct'))}",
    ]
    return "\n".join(lines)


def _format_type_b(data: dict, title: str) -> str:
    """Type B — 비교형 (전일 대비 강조)."""
    lines = [
        title,
        "",
        f"KOSPI  {_fmt_price(data.get('kospi'))} ← {_fmt_price(data.get('kospi'))}"
        f"  {_fmt_chg_pct(data.get('kospi_chg_pct'))}",
        f"KOSDAQ {_fmt_price(data.get('kosdaq'))}"
        f"  {_fmt_chg_pct(data.get('kosdaq_chg_pct'))}",
        f"원/달러 {_fmt_krw(data.get('krw_usd'))}원"
        f"  (전일 {_fmt_krw(data.get('krw_usd_prev'))}원)",
        f"미국10Y {_fmt_rate(data.get('us10y'))}%"
        f"  (전일 {_fmt_rate(data.get('us10y_prev'))}%)",
        f"달러지수 {_fmt_dxy(data.get('dxy'))}"
        f"  ({_fmt_pct(data.get('dxy_chg_pct'))})",
    ]
    return "\n".join(lines)


def _format_type_c(data: dict, title: str) -> str:
    """Type C — 신호중심형 (시그널 먼저)."""
    lines = [
        title,
        "",
        f"▸ 원/달러 {_fmt_krw(data.get('krw_usd'))}원"
        f"  ({KRW_REGIME_LABEL.get(_get_krw_regime_label(data), '--')})",
        f"▸ 미국10Y {_fmt_rate(data.get('us10y'))}%"
        f"  {_fmt_rate_chg(data.get('us10y_chg'))}",
        f"▸ 달러지수 {_fmt_dxy(data.get('dxy'))}"
        f"  {_fmt_pct(data.get('dxy_chg_pct'))}",
        "",
        f"KOSPI {_fmt_price(data.get('kospi'))}  {_fmt_chg_pct(data.get('kospi_chg_pct'))}"
        f"  |  KOSDAQ {_fmt_price(data.get('kosdaq'))}"
        f"  {_fmt_chg_pct(data.get('kosdaq_chg_pct'))}",
    ]
    return "\n".join(lines)


def _format_type_d(data: dict, title: str) -> str:
    """Type D — 요약형 (핵심 압축)."""
    lines = [
        title,
        "",
        f"KOSPI {_fmt_price(data.get('kospi'))}({_fmt_chg_pct(data.get('kospi_chg_pct'))})"
        f"  KOSDAQ {_fmt_price(data.get('kosdaq'))}({_fmt_chg_pct(data.get('kosdaq_chg_pct'))})",
        f"원달러 {_fmt_krw(data.get('krw_usd'))}원"
        f"  |  미국10Y {_fmt_rate(data.get('us10y'))}%"
        f"  |  DXY {_fmt_dxy(data.get('dxy'))}",
    ]
    return "\n".join(lines)


def _format_signal_tweet(signal_result: dict, hashtags: str) -> str:
    """tweet2 — 시그널 판정 + 행동 지침 + 해시태그."""
    sig = signal_result.get("market_signal", "중립")
    fp = signal_result.get("foreign_pressure", "MEDIUM")
    rb = signal_result.get("rate_burden", "MEDIUM")
    emoji = SIGNAL_EMOJI.get(sig, "⚪")
    action = ACTION_TEXT.get(sig, "--")

    lines = [
        f"{emoji} 국장 환경: {sig}",
        "",
        FOREIGN_PRESSURE_TEXT.get(fp, "--"),
        RATE_BURDEN_TEXT.get(rb, "--"),
        "",
        action,
        "",
        hashtags,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 포맷 헬퍼
# ---------------------------------------------------------------------------

def _fmt_price(val: float | None) -> str:
    if val is None:
        return "--"
    return f"{val:,.0f}"


def _fmt_krw(val: float | None) -> str:
    if val is None:
        return "--"
    return f"{val:,.0f}"


def _fmt_krw_chg(val: float | None) -> str:
    if val is None:
        return ""
    arrow = "▲" if val > 0 else "▼" if val < 0 else "─"
    return f"{arrow}{abs(val):.0f}원"


def _fmt_rate(val: float | None) -> str:
    if val is None:
        return "--"
    return f"{val:.2f}"


def _fmt_rate_chg(val: float | None) -> str:
    if val is None:
        return ""
    arrow = "▲" if val > 0 else "▼" if val < 0 else "─"
    return f"{arrow}{abs(val):.2f}%p"


def _fmt_dxy(val: float | None) -> str:
    if val is None:
        return "--"
    return f"{val:.2f}"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return ""
    arrow = "▲" if val > 0 else "▼" if val < 0 else "─"
    return f"{arrow}{abs(val):.2f}%"


def _fmt_chg_pct(val: float | None) -> str:
    if val is None:
        return ""
    arrow = "▲" if val > 0 else "▼" if val < 0 else "─"
    return f"{arrow}{abs(val):.1f}%"


def _get_krw_regime_label(data: dict) -> str:
    """임시 환율 레짐 판정 (포맷 내부용)."""
    krw = data.get("krw_usd")
    if krw is None:
        return "NEUTRAL"
    if krw < 1320:
        return "STRONG"
    if krw >= 1380:
        return "WEAK"
    return "NEUTRAL"


def _truncate(text: str, limit: int = MAX_TWEET_LENGTH) -> str:
    """트윗 길이 초과 시 절단 (마지막 줄 단위로)."""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    result = ""
    for line in lines:
        candidate = result + ("\n" if result else "") + line
        if len(candidate) > limit - 3:
            break
        result = candidate
    return result + "…"

# ---------------------------------------------------------------------------
# 섹터 흐름 트윗 (tweet3)
# ---------------------------------------------------------------------------

_DIRECTION_EMOJI: dict[str, str] = {"up": "🟢", "down": "🔴", "flat": "⚪"}
_BAR_FULL = "█"
_BAR_HALF = "▌"
_MAX_BAR = 6  # 최대 바 길이


def format_sector_tweet(sector_data: list[dict], seed: int | None = None) -> str:
    """
    섹터 흐름 비율 트윗 생성.
    sector_data: run_sector_engine() 반환값 (강도 내림차순 정렬됨)
    seed: 해시태그 유동 선택용 (None이면 오늘 날짜 기반 자동 생성)
    반환: 트윗 문자열 (비어있으면 빈 문자열)
    """
    if not sector_data:
        return ""

    if seed is None:
        seed = _get_daily_seed()

    today_str = date.today().strftime("%m/%d")
    lines = [f"📊 섹터 흐름 | {today_str}", ""]

    for s in sector_data:
        emoji = _DIRECTION_EMOJI.get(s["direction"], "⚪")
        name = s["name"]
        chg = s["chg_pct"]
        ratio = s["ratio"]
        bar = _make_bar(ratio)
        chg_str = f"▲{chg:.1f}%" if chg > 0 else f"▼{abs(chg):.1f}%" if chg < 0 else "─0.0%"

        lines.append(f"{emoji} {name:<7} {chg_str:>7}  {bar}  {ratio:.0f}%")

    lines.append("")
    lines.append(_select_sector_hashtags(seed))

    return "\n".join(lines)


def _make_bar(ratio: float) -> str:
    """비율(0~100)을 바 형태로 변환. 최대 _MAX_BAR칸."""
    filled = round(ratio / 100 * _MAX_BAR)
    filled = max(1, min(filled, _MAX_BAR))
    return _BAR_FULL * filled + " " * (_MAX_BAR - filled)


def _select_sector_hashtags(seed: int) -> str:
    """SECTOR_HASHTAG_POOL에서 중복 없이 3개 분산 추출."""
    selected: list[str] = []
    used: set[int] = set()
    for i in range(3):
        idx = (seed * (i + 11) + i * 53) % len(SECTOR_HASHTAG_POOL)
        attempts = 0
        while idx in used and attempts < len(SECTOR_HASHTAG_POOL):
            idx = (idx + 1) % len(SECTOR_HASHTAG_POOL)
            attempts += 1
        used.add(idx)
        selected.append(SECTOR_HASHTAG_POOL[idx])
    return " ".join(selected)
