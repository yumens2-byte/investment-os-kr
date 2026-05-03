"""
KR Market OS — 메인 파이프라인
FRED + Yahoo Finance 수집 → 분석 → X/TG 발행 → Supabase 저장
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date

from collectors.kr_fred_client import collect_fred_data
from collectors.kr_yahoo_client import collect_yahoo_data
from db.supabase_client import upsert_snapshot
from engines.kr_market_engine import run_kr_engine
from publishers.kr_formatter import format_daily_tweet
from publishers.tg_publisher import publish_message
from publishers.x_publisher import publish_thread

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 로깅 설정
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info(f"[KR Pipeline] v{VERSION} 시작")

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        logger.info("[KR Pipeline] DRY_RUN 모드 — 발행 없음")

    # ------------------------------------------------------------------
    # Step 1: FRED 수집
    # ------------------------------------------------------------------
    fred_data = collect_fred_data()
    fred_ok = sum(
        1 for k, v in fred_data.items() if v is not None and k != "collected_at"
    )
    logger.info(f"[Step1] FRED 수집 완료: {fred_ok}개 항목")

    # ------------------------------------------------------------------
    # Step 2: Yahoo Finance 수집
    # ------------------------------------------------------------------
    yahoo_data = collect_yahoo_data()
    yahoo_ok = sum(
        1 for k, v in yahoo_data.items() if v is not None and k != "collected_at"
    )
    logger.info(f"[Step2] Yahoo 수집 완료: {yahoo_ok}개 항목")

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
    # Step 5: 포맷팅
    # ------------------------------------------------------------------
    tweets = format_daily_tweet(market_data, signal_result)
    logger.info(f"[Step5] 트윗 생성: {len(tweets)}개")

    # ------------------------------------------------------------------
    # Step 6: 발행
    # ------------------------------------------------------------------
    tweet_ids = publish_thread(tweets, dry_run=dry_run)
    logger.info(f"[Step6-X] 발행: {tweet_ids}")

    tg_text = "\n\n".join(tweets)
    tg_ok = publish_message(tg_text, dry_run=dry_run)
    logger.info(f"[Step6-TG] 발행: {'성공' if tg_ok else '실패'}")

    # ------------------------------------------------------------------
    # Step 7: Supabase 저장
    # ------------------------------------------------------------------
    snapshot = _build_snapshot(market_data, signal_result, tweet_ids, tg_ok)
    if not dry_run:
        db_ok = upsert_snapshot(snapshot)
        logger.info(f"[Step7] Supabase 저장: {'성공' if db_ok else '실패'}")
    else:
        logger.info("[Step7] DRY_RUN — Supabase 저장 스킵")

    logger.info("[KR Pipeline] 완료")


# ---------------------------------------------------------------------------
# 스냅샷 빌더
# ---------------------------------------------------------------------------

def _build_snapshot(
    market_data: dict,
    signal_result: dict,
    tweet_ids: list[str],
    tg_ok: bool,
) -> dict:
    """kr.daily_snapshots upsert용 dict 생성."""
    return {
        "snapshot_date": date.today().isoformat(),
        # Yahoo
        "kospi": market_data.get("kospi"),
        "kospi_chg_pct": market_data.get("kospi_chg_pct"),
        "kosdaq": market_data.get("kosdaq"),
        "kosdaq_chg_pct": market_data.get("kosdaq_chg_pct"),
        "samsung": market_data.get("samsung"),
        "samsung_chg_pct": market_data.get("samsung_chg_pct"),
        "skhynix": market_data.get("skhynix"),
        "skhynix_chg_pct": market_data.get("skhynix_chg_pct"),
        # FRED
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
        # 시그널
        "krw_regime": signal_result.get("krw_regime"),
        "foreign_pressure": signal_result.get("foreign_pressure"),
        "rate_burden": signal_result.get("rate_burden"),
        "yield_spread": signal_result.get("yield_spread"),
        "market_signal": signal_result.get("market_signal"),
        "signal_score": signal_result.get("signal_score"),
        # 발행
        "x_published": bool(tweet_ids),
        "tg_published": tg_ok,
        "x_tweet_ids": ",".join(tweet_ids) if tweet_ids else None,
    }


if __name__ == "__main__":
    main()
