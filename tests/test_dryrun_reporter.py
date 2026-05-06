"""
tests/test_dryrun_reporter.py
=============================
DRY_RUN 리포트 생성기 단위 테스트

검증:
  - JSON 파일 생성 + 스키마 검증
  - 트윗 미리보기 (인덱스/길이/280자 검증)
  - 발행 게이트 누락 정보 기록
  - 휴장 스킵 시 분기
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.dryrun_reporter import VERSION, write_report


@pytest.fixture()
def sample_market_data() -> dict:
    return {
        "kospi": 2700.0,
        "kospi_chg_pct": 0.5,
        "kosdaq": 870.0,
        "kosdaq_chg_pct": -0.3,
        "krw_usd": 1380.0,
        "krw_usd_chg": -2.5,
        "us10y": 4.45,
        "us10y_chg": 0.02,
        "us2y": 4.85,
        "dxy": 104.1,
        "dxy_chg_pct": 0.1,
        "fedfunds": 5.33,
        "t10y2y": -0.40,
        "samsung": 80000.0,
        "skhynix": 195000.0,
    }


@pytest.fixture()
def sample_signal_result() -> dict:
    return {
        "market_signal": "주의",
        "signal_score": 1,
        "krw_regime": "WEAK",
        "foreign_pressure": "MEDIUM",
        "rate_burden": "MEDIUM",
        "yield_spread": "INVERTED",
    }


@pytest.fixture()
def sample_tweets() -> list[str]:
    return [
        "📊 국장 장전 거시체크 | 05/05\n\nKOSPI 2700 +0.5%\nKOSDAQ 870 -0.3%",
        "🟡 시그널: 주의 (점수 1)\n달러 약세 → 외인 우호\n\n#KOSPI #국장 #환율",
    ]


# ---------------------------------------------------------------------------
# 정상 케이스
# ---------------------------------------------------------------------------

class TestWriteReportNormal:
    def test_creates_file(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        path = write_report(
            market_data=sample_market_data,
            signal_result=sample_signal_result,
            sector_data=None,
            tweets=sample_tweets,
            missing_fields=[],
            log_dir=tmp_path,
        )
        assert Path(path).exists()
        assert Path(path).suffix == ".json"

    def test_report_has_version(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        path = write_report(
            sample_market_data, sample_signal_result, None, sample_tweets, [],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        assert report["version"] == VERSION

    def test_market_snapshot_includes_kospi(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        path = write_report(
            sample_market_data, sample_signal_result, None, sample_tweets, [],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        assert report["market_snapshot"]["kospi"] == 2700.0
        assert report["market_snapshot"]["krw_usd"] == 1380.0

    def test_signal_result_preserved(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        path = write_report(
            sample_market_data, sample_signal_result, None, sample_tweets, [],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        assert report["signal_result"]["market_signal"] == "주의"
        assert report["signal_result"]["krw_regime"] == "WEAK"

    def test_tweets_preview_has_index_and_length(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        path = write_report(
            sample_market_data, sample_signal_result, None, sample_tweets, [],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        preview = report["tweets_preview"]
        assert len(preview) == 2
        assert preview[0]["index"] == 0
        assert preview[0]["length"] == len(sample_tweets[0])
        assert preview[0]["text"] == sample_tweets[0]
        assert preview[0]["within_x_limit"] is True

    def test_publish_gate_pass(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        path = write_report(
            sample_market_data, sample_signal_result, None, sample_tweets, [],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        assert report["publish_gate"]["would_block"] is False
        assert report["publish_gate"]["missing_fields"] == []


# ---------------------------------------------------------------------------
# 발행 게이트 누락 케이스
# ---------------------------------------------------------------------------

class TestWriteReportGateBlocked:
    def test_missing_fields_recorded(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        path = write_report(
            sample_market_data, sample_signal_result, None, sample_tweets,
            missing_fields=["kospi", "us10y"],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        assert report["publish_gate"]["would_block"] is True
        assert "kospi" in report["publish_gate"]["missing_fields"]
        assert "us10y" in report["publish_gate"]["missing_fields"]


# ---------------------------------------------------------------------------
# 휴장 스킵 케이스
# ---------------------------------------------------------------------------

class TestWriteReportSkipped:
    def test_skipped_reason_recorded(self, tmp_path):
        path = write_report(
            market_data={},
            signal_result={},
            sector_data=None,
            tweets=[],
            missing_fields=[],
            skipped_reason="미장 휴무일: Christmas Day",
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        assert report["skipped_reason"] == "미장 휴무일: Christmas Day"


# ---------------------------------------------------------------------------
# 트윗 길이 검증 (280자 초과 케이스)
# ---------------------------------------------------------------------------

class TestTweetLengthCheck:
    def test_long_tweet_flagged(self, tmp_path, sample_market_data, sample_signal_result):
        long_tweet = "A" * 300
        path = write_report(
            sample_market_data, sample_signal_result, None,
            [long_tweet],
            missing_fields=[],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        assert report["tweets_preview"][0]["length"] == 300
        assert report["tweets_preview"][0]["within_x_limit"] is False


# ---------------------------------------------------------------------------
# 섹터 데이터 케이스
# ---------------------------------------------------------------------------

class TestSectorData:
    def test_list_sector_data(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        sector_list = [
            {"name": "반도체", "direction": "up", "ratio": 60, "chg_pct": 1.5},
            {"name": "2차전지", "direction": "down", "ratio": 40, "chg_pct": -1.2},
        ]
        path = write_report(
            sample_market_data, sample_signal_result, sector_list, sample_tweets, [],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        assert isinstance(report["sector_summary"], list)
        assert len(report["sector_summary"]) == 2

    def test_dict_sector_data(self, tmp_path, sample_market_data, sample_signal_result, sample_tweets):
        sector_dict = {"반도체": 1.5, "2차전지": -1.2, "바이오": None}
        path = write_report(
            sample_market_data, sample_signal_result, sector_dict, sample_tweets, [],
            log_dir=tmp_path,
        )
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        # None 제외
        assert "바이오" not in report["sector_summary"]
        assert report["sector_summary"]["반도체"] == 1.5
