"""
core/dryrun_reporter.py
========================
DRY_RUN 모드 결과 리포트 생성기

목적:
  - DRY_RUN 실행 시 생성된 트윗/시그널/스냅샷을 JSON 파일로 저장
  - GitHub Actions Artifact로 다운로드 가능 (운영자 사후 검토용)
  - 콘솔에도 요약 출력

기능:
  - logs/dryrun_YYYYMMDD_HHMMSS.json 생성
  - market_snapshot, signal_result, sector_summary, tweets_preview 포함
  - publish_gate 정보 (필수 데이터 누락 여부)

사용 예:
  from core.dryrun_reporter import write_report
  report_path = write_report(
      market_data=market_data,
      signal_result=signal_result,
      sector_data=sector_data,
      tweets=tweets,
      missing_fields=missing,
      skipped_reason=None,
  )
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

# 리포트 저장 디렉토리
_DEFAULT_LOG_DIR = Path("logs")


def write_report(
    market_data: dict,
    signal_result: dict,
    sector_data: list[dict] | dict | None,
    tweets: list[str],
    missing_fields: list[str],
    skipped_reason: str | None = None,
    log_dir: Path | None = None,
) -> str:
    """
    DRY_RUN 결과를 JSON 파일로 저장 + 콘솔 요약.

    Args:
        market_data: 수집/조립된 시장 데이터 dict
        signal_result: kr_market_engine 결과 dict
        sector_data: kr_sector_engine 결과 (list[dict] 권장, dict도 허용)
        tweets: 생성된 트윗 list
        missing_fields: 발행 게이트 누락 필드 list
        skipped_reason: 스킵 이유 (휴장 등). None이면 정상 실행
        log_dir: 저장 디렉토리 (기본 ./logs)

    Returns:
        저장된 리포트 파일의 절대 경로 문자열
    """
    log_dir = log_dir or _DEFAULT_LOG_DIR
    log_dir.mkdir(exist_ok=True)

    now = datetime.now(UTC)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    report_path = log_dir / f"dryrun_{timestamp}.json"

    report = {
        "version": VERSION,
        "generated_at": now.isoformat(),
        "skipped_reason": skipped_reason,
        "market_snapshot": _extract_snapshot(market_data),
        "signal_result": _safe_dict(signal_result),
        "sector_summary": _extract_sector_summary(sector_data),
        "tweets_preview": _build_tweets_preview(tweets),
        "publish_gate": {
            "missing_fields": list(missing_fields or []),
            "would_block": bool(missing_fields),
        },
    }

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info(f"[DryRun] 리포트 저장: {report_path} ({report_path.stat().st_size} bytes)")
    _print_console_summary(report, tweets)

    return str(report_path.resolve())


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _extract_snapshot(market_data: dict) -> dict:
    """market_data 중 핵심 스냅샷 키만 추출 (None 포함, 운영자가 한눈에 보도록)."""
    if not isinstance(market_data, dict):
        return {}
    keys = [
        # 국장
        "kospi", "kospi_chg_pct", "kosdaq", "kosdaq_chg_pct",
        "samsung", "samsung_chg_pct", "skhynix", "skhynix_chg_pct",
        # 환율
        "krw_usd", "krw_usd_chg",
        # 미국 매크로
        "us10y", "us10y_chg", "us2y", "dxy", "dxy_chg_pct",
        "fedfunds", "t10y2y",
    ]
    return {k: market_data.get(k) for k in keys if k in market_data}


def _safe_dict(value: Any) -> dict:
    """Any → dict 안전 변환."""
    if isinstance(value, dict):
        return value
    return {"raw": str(value)} if value is not None else {}


def _extract_sector_summary(sector_data: Any) -> list[dict] | dict:
    """sector_data 정규화 — list[dict] 또는 dict 허용."""
    if sector_data is None:
        return []
    if isinstance(sector_data, list):
        # list[dict] 형식 — 그대로 반환 (None 값은 제외)
        return [s for s in sector_data if s]
    if isinstance(sector_data, dict):
        return {k: v for k, v in sector_data.items() if v is not None}
    return {"raw": str(sector_data)}


def _build_tweets_preview(tweets: list[str]) -> list[dict]:
    """트윗 미리보기 dict 생성 (인덱스, 길이, 본문)."""
    if not tweets:
        return []
    preview: list[dict] = []
    for i, tweet in enumerate(tweets):
        text = str(tweet) if tweet is not None else ""
        preview.append({
            "index": i,
            "length": len(text),
            "within_x_limit": len(text) <= 280,
            "text": text,
        })
    return preview


def _print_console_summary(report: dict, tweets: list[str]) -> None:
    """콘솔에 핵심 요약 출력 (운영자 즉시 확인용)."""
    sep = "─" * 60
    logger.info(sep)
    logger.info("[DryRun] === 결과 요약 ===")
    if report.get("skipped_reason"):
        logger.info(f"[DryRun] 스킵 사유: {report['skipped_reason']}")
        logger.info(sep)
        return

    snap = report.get("market_snapshot", {})
    sig = report.get("signal_result", {})
    gate = report.get("publish_gate", {})

    logger.info(
        f"[DryRun] KOSPI={snap.get('kospi')} KOSDAQ={snap.get('kosdaq')} "
        f"KRW/USD={snap.get('krw_usd')} US10Y={snap.get('us10y')}% "
        f"DXY={snap.get('dxy')}"
    )
    logger.info(
        f"[DryRun] 시그널={sig.get('market_signal')} "
        f"점수={sig.get('signal_score')} "
        f"환율={sig.get('krw_regime')} 외인={sig.get('foreign_pressure')} "
        f"금리={sig.get('rate_burden')}"
    )
    logger.info(
        f"[DryRun] 발행 게이트: 차단={gate.get('would_block')} "
        f"누락={gate.get('missing_fields')}"
    )

    if tweets:
        for i, tweet in enumerate(tweets):
            text = str(tweet) if tweet is not None else ""
            logger.info(f"[DryRun] tweet[{i}] ({len(text)}자):")
            for line in text.splitlines() or [""]:
                logger.info(f"[DryRun]   {line}")
            logger.info("[DryRun] ---")
    else:
        logger.info("[DryRun] (생성된 트윗 없음)")
    logger.info(sep)
