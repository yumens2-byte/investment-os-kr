"""
KR Market OS — 메인 파이프라인
FRED + Yahoo Finance 수집 → 분석 → X/TG 발행 → Supabase 저장

[보안 정책]
- 필수 데이터 누락 시 X/TG 발행 전면 차단 (Supabase 저장은 유지)
- API 키/토큰은 로그에 절대 출력하지 않음
- 예외 상세 정보는 로컬 로그 전용 (외부 채널 미노출)
- 환경변수 미설정 시 조기 경고 후 계속 진행 (수집 데이터 보존 우선)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from collectors.kr_fred_client import collect_fred_data
from collectors.kr_yahoo_client import collect_sector_data, collect_yahoo_data
from db.supabase_client import upsert_snapshot
from engines.kr_market_engine import run_kr_engine
from engines.kr_sector_engine import run_sector_engine
from publishers.kr_formatter import format_daily_tweet
from publishers.tg_publisher import publish_message
from publishers.x_publisher import publish_thread

VERSION = "1.3.0"

# ---------------------------------------------------------------------------
# 발행 필수 데이터 게이트 — 1개라도 None이면 X/TG 발행 차단
# ---------------------------------------------------------------------------
_REQUIRED_FOR_PUBLISH: list[str] = [
    "kospi",    # KOSPI 지수
    "kosdaq",   # KOSDAQ 지수
    "krw_usd",  # 원/달러 환율
    "us10y",    # 미국 10년물 금리
    "dxy",      # 달러 인덱스
]

# ---------------------------------------------------------------------------
# 로깅 설정 — stdout + 날짜별 파일 동시 출력
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def _setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    today = datetime.now(UTC).strftime("%Y%m%d")
    log_file = log_dir / f"kr_market_{today}.log"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]

    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=handlers)
    for lib in ("httpx", "httpcore", "yfinance", "urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)


_setup_logging()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 보안 유틸
# ---------------------------------------------------------------------------

def _check_env_secrets() -> None:
    """
    필수 환경변수 조기 점검.
    미설정 항목은 이름만 WARNING 로그 (값은 절대 출력하지 않음).
    """
    required = [
        "FRED_API_KEY",
        "X_API_KEY", "X_API_SECRET",
        "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_KR_FREE_CHANNEL_ID",
        "SUPABASE_URL", "SUPABASE_KEY",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        logger.warning(f"[Security] 미설정 환경변수: {missing} — 해당 기능 비활성화")
    else:
        logger.info("[Security] 환경변수 전체 확인 완료")


# ---------------------------------------------------------------------------
# 발행 게이트
# ---------------------------------------------------------------------------

def _validate_required_data(market_data: dict) -> list[str]:
    """
    발행 전 필수 데이터 검증.
    반환: 누락 필드명 리스트 (빈 리스트 = 발행 가능)
    필수 항목: KOSPI, KOSDAQ, KRW/USD, 미국10Y, DXY
    """
    return [f for f in _REQUIRED_FOR_PUBLISH if market_data.get(f) is None]


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info(f"[KR Pipeline] v{VERSION} 시작")

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        logger.info("[KR Pipeline] DRY_RUN 모드 — 발행 없음")

    # 환경변수 점검 (보안: 값 미출력)
    _check_env_secrets()

    # ------------------------------------------------------------------
    # Step 1: FRED 수집
    # ------------------------------------------------------------------
    fred_data = collect_fred_data()
    fred_ok = sum(1 for k, v in fred_data.items() if v is not None and k != "collected_at")
    logger.info(f"[Step1] FRED 수집 완료: {fred_ok}개 항목")

    # ------------------------------------------------------------------
    # Step 2: Yahoo Finance 수집
    # ------------------------------------------------------------------
    yahoo_data = collect_yahoo_data()
    yahoo_ok = sum(1 for k, v in yahoo_data.items() if v is not None and k != "collected_at")
    logger.info(f"[Step2] Yahoo 수집 완료: {yahoo_ok}개 항목")

    # ------------------------------------------------------------------
    # Step 2-S: 섹터 대표 종목 수집
    # ------------------------------------------------------------------
    sector_raw = collect_sector_data()
    sector_ok = sum(1 for v in sector_raw.values() if v is not None)
    logger.info(f"[Step2-S] 섹터 종목 수집 완료: {sector_ok}/{len(sector_raw)}개")

    # ------------------------------------------------------------------
    # Step 3: 데이터 조립
    # ------------------------------------------------------------------
    market_data: dict = {**fred_data, **yahoo_data}
    logger.info(f"[Step3] 데이터 조립 완료: {len(market_data)}개 키")

    # ------------------------------------------------------------------
    # Step 4: 분석 엔진
    # ------------------------------------------------------------------
    signal_result = run_kr_engine(market_data)
    logger.info(f"[Step4] 시그널: {signal_result['market_signal']}")

    # ------------------------------------------------------------------
    # Step 4-S: 섹터 흐름 분석
    # ------------------------------------------------------------------
    sector_data = run_sector_engine(market_data, sector_raw)
    logger.info(f"[Step4-S] 섹터 분석 완료: {len(sector_data)}개 섹터")

    # ------------------------------------------------------------------
    # Step 5: 발행 게이트 — 필수 데이터 누락 시 X/TG 발행 차단
    # ------------------------------------------------------------------
    missing_fields = _validate_required_data(market_data)
    can_publish = not missing_fields

    if not can_publish:
        logger.warning(
            f"[Step5] 발행 차단 — 필수 데이터 누락: {missing_fields}"
            " → X/TG 발행 스킵, Supabase 저장만 진행"
        )
    else:
        logger.info("[Step5] 발행 게이트 통과 — 전체 데이터 정상")

    # ------------------------------------------------------------------
    # Step 6: 포맷팅 및 발행 (게이트 통과 시에만 실행)
    # ------------------------------------------------------------------
    tweet_ids: list[str] = []
    tg_ok = False

    if can_publish:
        tweets = format_daily_tweet(market_data, signal_result, sector_data=sector_data)
        logger.info(f"[Step6] 트윗 생성: {len(tweets)}개")

        tweet_ids = publish_thread(tweets, dry_run=dry_run)
        logger.info(f"[Step6-X] 발행: {len(tweet_ids)}건")

        tg_text = "\n\n".join(tweets)
        tg_ok = publish_message(tg_text, dry_run=dry_run)
        logger.info(f"[Step6-TG] 발행: {'성공' if tg_ok else '실패'}")
    else:
        logger.info("[Step6] 발행 스킵")

    # ------------------------------------------------------------------
    # Step 7: Supabase 저장 (데이터 누락 여부와 무관하게 항상 저장)
    # ------------------------------------------------------------------
    snapshot = _build_snapshot(market_data, signal_result, tweet_ids, tg_ok)
    if not dry_run:
        db_ok = upsert_snapshot(snapshot)
        logger.info(f"[Step7] Supabase 저장: {'성공' if db_ok else '실패'}")
    else:
        logger.info("[Step7] DRY_RUN — Supabase 저장 스킵")

    logger.info(f"[KR Pipeline] 완료 | 발행: {'Y' if can_publish else 'N(데이터부족)'}")


# ---------------------------------------------------------------------------
# 스냅샷 빌더
# ---------------------------------------------------------------------------

def _build_snapshot(
    market_data: dict,
    signal_result: dict,
    tweet_ids: list[str],
    tg_ok: bool,
) -> dict:
    """public.kr_daily_snapshots upsert용 dict 생성."""
    return {
        "snapshot_date": date.today().isoformat(),
        "kospi": market_data.get("kospi"),
        "kospi_chg_pct": market_data.get("kospi_chg_pct"),
        "kosdaq": market_data.get("kosdaq"),
        "kosdaq_chg_pct": market_data.get("kosdaq_chg_pct"),
        "samsung": market_data.get("samsung"),
        "samsung_chg_pct": market_data.get("samsung_chg_pct"),
        "skhynix": market_data.get("skhynix"),
        "skhynix_chg_pct": market_data.get("skhynix_chg_pct"),
        "krw_usd": market_data.get("krw_usd"),
        "krw_usd_prev": market_data.get("krw_usd_prev"),
        "krw_usd_chg": market_data.get("krw_usd_chg"),
        "us10y": market_data.get("us10y"),
        "us10y_prev": market_data.get("us10y_prev"),
        "us10y_chg": market_data.get("us10y_chg"),
        "us2y": market_data.get("us2y"),
        "dxy": market_data.get("dxy"),
        "dxy_prev": market_data.get("dxy_prev"),
        "dxy_chg_pct": market_data.get("dxy_chg_pct"),
        "fedfunds": market_data.get("fedfunds"),
        "t10y2y": market_data.get("t10y2y"),
        "krw_regime": signal_result.get("krw_regime"),
        "foreign_pressure": signal_result.get("foreign_pressure"),
        "rate_burden": signal_result.get("rate_burden"),
        "yield_spread": signal_result.get("yield_spread"),
        "market_signal": signal_result.get("market_signal"),
        "signal_score": signal_result.get("signal_score"),
        "x_published": bool(tweet_ids),
        "tg_published": tg_ok,
        "x_tweet_ids": ",".join(tweet_ids) if tweet_ids else None,
    }


if __name__ == "__main__":
    main()
