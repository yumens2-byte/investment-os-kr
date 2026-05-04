"""
배포 CI 안정성 점검용 스모크 테스트.

목표:
- GitHub Actions 배포 전, 파이프라인 진입점이 예외 없이 동작하는지 확인
- 외부 API/발행/DB 없이도 핵심 플로우 회귀(regression) 감지
"""

from __future__ import annotations

import run_market



def test_main_dry_run_smoke(monkeypatch):
    """run_market.main()이 DRY_RUN에서 예외 없이 종료되어야 한다."""

    monkeypatch.setenv("DRY_RUN", "true")

    monkeypatch.setattr(
        run_market,
        "collect_fred_data",
        lambda: {
            "us10y": 4.3,
            "us10y_chg": 0.01,
            "dxy": 104.0,
            "dxy_chg_pct": 0.1,
            "t10y2y": -0.2,
            "fedfunds": 5.25,
            "collected_at": "2026-05-04T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        run_market,
        "collect_yahoo_data",
        lambda: {
            "kospi": 2700.0,
            "kospi_chg_pct": 0.2,
            "kosdaq": 880.0,
            "kosdaq_chg_pct": 0.1,
            "samsung": 80000.0,
            "samsung_chg_pct": 1.0,
            "skhynix": 190000.0,
            "skhynix_chg_pct": 0.8,
            "krw_usd": 1360.0,
            "krw_usd_prev": 1355.0,
            "krw_usd_chg": 5.0,
            "collected_at": "2026-05-04T00:00:00Z",
        },
    )
    monkeypatch.setattr(run_market, "collect_sector_data", lambda: {"035420.KS": 1.2, "005490.KS": -0.4})

    # DRY_RUN에서 호출되면 안 되는 경로(보호 장치)
    monkeypatch.setattr(run_market, "publish_thread", lambda *args, **kwargs: [])
    monkeypatch.setattr(run_market, "publish_message", lambda *args, **kwargs: True)
    monkeypatch.setattr(run_market, "upsert_snapshot", lambda *_args, **_kwargs: True)

    run_market.main()



def test_snapshot_builder_fields():
    """스냅샷 빌더가 CI에서 필요한 핵심 필드를 생성하는지 검증."""
    snapshot = run_market._build_snapshot(  # noqa: SLF001
        market_data={
            "kospi": 2700.0,
            "kosdaq": 880.0,
            "krw_usd": 1360.0,
            "us10y": 4.3,
            "dxy": 104.0,
        },
        signal_result={
            "market_signal": "주의",
            "signal_score": 1,
            "krw_regime": "NEUTRAL",
            "foreign_pressure": "MEDIUM",
            "rate_burden": "MEDIUM",
            "yield_spread": "FLAT",
        },
        tweet_ids=["111", "222"],
        tg_ok=True,
    )

    assert snapshot["snapshot_date"]
    assert snapshot["market_signal"] == "주의"
    assert snapshot["x_published"] is True
    assert snapshot["tg_published"] is True
    assert snapshot["x_tweet_ids"] == "111,222"
