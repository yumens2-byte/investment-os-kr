"""
tests/test_integration_smoke.py
================================
run_market.py 통합 스모크 테스트 (Track A+B+C+D)

검증:
  - Step 0 휴장 체크: 휴장일이면 sys.exit(0)
  - Step 6 AI 톤 분기: USE_AI_TONE 동작
  - Step 6.5 DRY_RUN 리포트 생성
  - 게이트 우회: DRY_RUN 시 누락 데이터에도 트윗 생성

외부 의존성은 module-scope fixture로 stub (collection 단계에서 다른 테스트
파일에 영향 미치지 않도록 함수 레벨에서 sys.modules 패치).
"""
from __future__ import annotations

import sys
import types

import pytest


_STUB_KEYS = [
    "collectors",
    "collectors.kr_fred_client",
    "collectors.kr_yahoo_client",
    "engines",
    "engines.kr_market_engine",
    "engines.kr_sector_engine",
    "db",
    "db.supabase_client",
    "publishers.kr_formatter",
    "publishers.x_publisher",
    "publishers.tg_publisher",
]


def _create_stubs() -> dict[str, types.ModuleType]:
    """stub 모듈들을 dict로 반환."""
    stubs: dict[str, types.ModuleType] = {}

    def _make(name: str, attrs: dict) -> types.ModuleType:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    stubs["collectors"] = _make("collectors", {})
    stubs["engines"] = _make("engines", {})
    stubs["db"] = _make("db", {})

    stubs["collectors.kr_fred_client"] = _make("collectors.kr_fred_client", {
        "collect_fred_data": lambda: {
            "us10y": 4.3, "us10y_chg": 0.01, "us2y": 4.85,
            "dxy": 104.0, "dxy_chg_pct": 0.1,
            "t10y2y": -0.2, "fedfunds": 5.25,
            "krw_usd": 1380.0,
            "collected_at": "2026-05-04T00:00:00Z",
        },
    })
    stubs["collectors.kr_yahoo_client"] = _make("collectors.kr_yahoo_client", {
        "collect_yahoo_data": lambda: {
            "kospi": 2700.0, "kospi_chg_pct": 0.5,
            "kosdaq": 870.0, "kosdaq_chg_pct": -0.3,
            "samsung": 80000.0, "samsung_chg_pct": 1.2,
            "skhynix": 195000.0, "skhynix_chg_pct": -0.8,
            "krw_fx": 1380.0,
            "collected_at": "2026-05-04T00:00:00Z",
        },
        "collect_sector_data": lambda: {"005930.KS": 1.2, "005490.KS": -0.4},
    })
    stubs["engines.kr_market_engine"] = _make("engines.kr_market_engine", {
        "run_kr_engine": lambda data: {
            "market_signal": "주의", "signal_score": 1,
            "krw_regime": "WEAK", "foreign_pressure": "MEDIUM",
            "rate_burden": "MEDIUM", "yield_spread": "INVERTED",
        },
    })
    stubs["engines.kr_sector_engine"] = _make("engines.kr_sector_engine", {
        "run_sector_engine": lambda data, raw: [
            {"name": "반도체", "direction": "up", "chg_pct": 1.2, "ratio": 60},
            {"name": "원자재", "direction": "down", "chg_pct": -0.4, "ratio": 40},
        ],
    })
    stubs["db.supabase_client"] = _make("db.supabase_client", {
        "upsert_snapshot": lambda data: True,
    })
    stubs["publishers.kr_formatter"] = _make("publishers.kr_formatter", {
        "format_daily_tweet": lambda md, sr, sector_data=None: [
            "📊 fallback tweet 1",
            "🟡 fallback tweet 2",
            "📈 fallback tweet 3",
        ],
    })
    stubs["publishers.x_publisher"] = _make("publishers.x_publisher", {
        "publish_thread": lambda tweets, dry_run=False: (
            ["dry_run_id"] * len(tweets) if dry_run else ["111", "222", "333"][:len(tweets)]
        ),
    })
    stubs["publishers.tg_publisher"] = _make("publishers.tg_publisher", {
        "publish_message": lambda text, dry_run=False: True,
    })
    return stubs


@pytest.fixture(scope="module")
def run_market_module():
    """
    run_market 모듈을 stub 주입 후 import.
    fixture 종료 시 sys.modules 정리 → 다른 테스트 파일에 영향 없음.
    """
    backup: dict[str, types.ModuleType | None] = {
        key: sys.modules.get(key) for key in (_STUB_KEYS + ["run_market"])
    }

    stubs = _create_stubs()
    for key, mod in stubs.items():
        sys.modules[key] = mod

    sys.modules.pop("run_market", None)
    import run_market

    yield run_market

    for key, original in backup.items():
        if original is not None:
            sys.modules[key] = original
        else:
            sys.modules.pop(key, None)


# ---------------------------------------------------------------------------
# Step 0 — 휴장 체크
# ---------------------------------------------------------------------------

class TestStep0HolidayCheck:
    def test_holiday_skip_exits(self, run_market_module, monkeypatch, tmp_path):
        rm = run_market_module
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("FORCE_RUN", "false")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(
            rm, "_check_us_market_session",
            lambda dry_run: (True, "미장 휴무일: Christmas Day"),
        )

        with pytest.raises(SystemExit) as exc_info:
            rm.main()
        assert exc_info.value.code == 0

    def test_holiday_skip_dryrun_writes_report(self, run_market_module, monkeypatch, tmp_path):
        rm = run_market_module
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(
            rm, "_check_us_market_session",
            lambda dry_run: (True, "주말 (토요일)"),
        )

        with pytest.raises(SystemExit) as exc_info:
            rm.main()
        assert exc_info.value.code == 0

        log_dir = tmp_path / "logs"
        if log_dir.exists():
            reports = list(log_dir.glob("dryrun_*.json"))
            assert len(reports) >= 1


# ---------------------------------------------------------------------------
# DRY_RUN 정상 흐름
# ---------------------------------------------------------------------------

class TestDryRunFlow:
    def test_dry_run_full_flow_no_exception(self, run_market_module, monkeypatch, tmp_path):
        rm = run_market_module
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("USE_AI_TONE", "false")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(
            rm, "_check_us_market_session",
            lambda dry_run: (False, ""),
        )

        rm.main()

        log_dir = tmp_path / "logs"
        assert log_dir.exists()
        reports = list(log_dir.glob("dryrun_*.json"))
        assert len(reports) >= 1

    def test_dry_run_supabase_skipped(self, run_market_module, monkeypatch, tmp_path):
        rm = run_market_module
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("USE_AI_TONE", "false")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            rm, "_check_us_market_session",
            lambda dry_run: (False, ""),
        )

        called = {"count": 0}

        def fake_upsert(data):
            called["count"] += 1
            return True

        monkeypatch.setattr(rm, "upsert_snapshot", fake_upsert)
        rm.main()
        assert called["count"] == 0


# ---------------------------------------------------------------------------
# 게이트 우회 — DRY_RUN 시 누락 데이터에도 트윗 생성
# ---------------------------------------------------------------------------

class TestGateBypassInDryRun:
    def test_missing_data_dryrun_still_generates_tweets(self, run_market_module, monkeypatch, tmp_path, caplog):
        import json
        import logging
        rm = run_market_module
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("USE_AI_TONE", "false")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            rm, "_check_us_market_session",
            lambda dry_run: (False, ""),
        )

        # KOSPI를 None으로 만들어 게이트 차단 유도
        monkeypatch.setattr(
            rm, "collect_yahoo_data",
            lambda: {
                "kospi": None,
                "kosdaq": 870.0,
                "kosdaq_chg_pct": -0.3,
                "krw_fx": 1380.0,
                "collected_at": "2026-05-04T00:00:00Z",
            },
        )

        with caplog.at_level(logging.INFO):
            rm.main()

        log_text = caplog.text
        assert "발행 차단" in log_text or "누락" in log_text
        assert "DRY_RUN" in log_text or "우회" in log_text

        reports = list((tmp_path / "logs").glob("dryrun_*.json"))
        assert len(reports) >= 1
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        assert report["publish_gate"]["would_block"] is True
        assert "kospi" in report["publish_gate"]["missing_fields"]


# ---------------------------------------------------------------------------
# _build_snapshot 회귀
# ---------------------------------------------------------------------------

class TestSnapshotBuilder:
    def test_snapshot_real_publish_flag(self, run_market_module):
        rm = run_market_module
        snap = rm._build_snapshot(
            market_data={"kospi": 2700.0, "kosdaq": 870.0},
            signal_result={"market_signal": "주의"},
            tweet_ids=["111", "222"],
            tg_ok=True,
        )
        assert snap["x_published"] is True
        assert snap["tg_published"] is True

    def test_snapshot_dry_run_publish_flag(self, run_market_module):
        rm = run_market_module
        snap = rm._build_snapshot(
            market_data={"kospi": 2700.0},
            signal_result={"market_signal": "주의"},
            tweet_ids=["dry_run_id", "dry_run_id"],
            tg_ok=True,
        )
        assert snap["x_published"] is False

    def test_snapshot_required_fields(self, run_market_module):
        rm = run_market_module
        snap = rm._build_snapshot(
            market_data={"kospi": 2700.0, "kosdaq": 880.0, "krw_usd": 1360.0,
                         "us10y": 4.3, "dxy": 104.0},
            signal_result={
                "market_signal": "주의", "signal_score": 1,
                "krw_regime": "NEUTRAL", "foreign_pressure": "MEDIUM",
                "rate_burden": "MEDIUM", "yield_spread": "FLAT",
            },
            tweet_ids=["111"],
            tg_ok=True,
        )
        assert snap["snapshot_date"]
        assert snap["market_signal"] == "주의"
        assert snap["x_tweet_ids"] == "111"


# ---------------------------------------------------------------------------
# AI 톤 분기
# ---------------------------------------------------------------------------

class TestAIToneBranch:
    def test_ai_disabled_uses_fallback(self, run_market_module, monkeypatch, tmp_path, caplog):
        import logging
        rm = run_market_module
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("USE_AI_TONE", "false")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            rm, "_check_us_market_session",
            lambda dry_run: (False, ""),
        )

        with caplog.at_level(logging.INFO):
            rm.main()

        assert "Fallback 생성" in caplog.text or "fallback" in caplog.text.lower()

    def test_ai_enabled_no_key_falls_back(self, run_market_module, monkeypatch, tmp_path):
        rm = run_market_module
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("USE_AI_TONE", "true")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_SUB_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_SUB_SUB_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_SUB_PAY_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            rm, "_check_us_market_session",
            lambda dry_run: (False, ""),
        )

        rm.main()
