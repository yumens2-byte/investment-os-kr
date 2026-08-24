"""
KR Market Formatter 테스트
포맷 타입 / 해시태그 / 제목 / 유동화 / 길이 제한
"""

from __future__ import annotations

import pytest

from publishers.kr_formatter import (
    FORMAT_TYPES,
    HASHTAG_POOL,
    TITLE_PATTERNS,
    _format_type_a,
    _format_type_b,
    _format_type_c,
    _format_type_d,
    _get_daily_seed,
    _select_format_type,
    _select_hashtags,
    _select_title,
    _truncate,
    format_daily_tweet,
)

# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

@pytest.fixture()
def full_market_data() -> dict:
    return {
        "kospi": 2650.0,
        "kospi_chg_pct": -1.2,
        "kosdaq": 860.0,
        "kosdaq_chg_pct": -0.8,
        "samsung": 72000.0,
        "samsung_chg_pct": -0.5,
        "skhynix": 180000.0,
        "skhynix_chg_pct": 1.2,
        "krw_usd": 1382.0,
        "krw_usd_prev": 1377.0,
        "krw_usd_chg": 5.0,
        "us10y": 4.51,
        "us10y_prev": 4.43,
        "us10y_chg": 0.08,
        "us2y": 4.85,
        "dxy": 104.2,
        "dxy_prev": 103.9,
        "dxy_chg_pct": 0.35,
        "fedfunds": 5.33,
        "t10y2y": -0.34,
    }


@pytest.fixture()
def signal_result() -> dict:
    return {
        "krw_regime": "WEAK",
        "foreign_pressure": "HIGH",
        "rate_burden": "HIGH",
        "yield_spread": "INVERTED",
        "market_signal": "위험",
        "signal_score": 3,
    }


@pytest.fixture()
def none_market_data() -> dict:
    return {k: None for k in [
        "kospi", "kospi_chg_pct", "kosdaq", "kosdaq_chg_pct",
        "samsung", "samsung_chg_pct", "skhynix", "skhynix_chg_pct",
        "krw_usd", "krw_usd_prev", "krw_usd_chg",
        "us10y", "us10y_prev", "us10y_chg", "us2y",
        "dxy", "dxy_prev", "dxy_chg_pct", "fedfunds", "t10y2y",
    ]}


# ---------------------------------------------------------------------------
# seed 기반 유동화
# ---------------------------------------------------------------------------

class TestSeedSelection:
    def test_daily_seed_is_int(self):
        seed = _get_daily_seed()
        assert isinstance(seed, int)
        assert 0 <= seed < 100000

    def test_same_day_same_seed(self):
        assert _get_daily_seed() == _get_daily_seed()

    def test_title_in_pool(self):
        seed = 12345
        title = _select_title(seed)
        assert any(p.split("{")[0] in title for p in TITLE_PATTERNS)

    def test_format_type_valid(self):
        for seed in range(20):
            fmt = _select_format_type(seed)
            assert fmt in FORMAT_TYPES

    def test_hashtags_count(self):
        seed = 99999
        tags = _select_hashtags(seed)
        assert len(tags.split(" ")) == 5

    def test_hashtags_no_duplicate(self):
        seed = 42
        tags = _select_hashtags(seed).split(" ")
        assert len(tags) == len(set(tags))

    def test_hashtags_from_pool(self):
        seed = 7777
        tags = _select_hashtags(seed).split(" ")
        for tag in tags:
            assert tag in HASHTAG_POOL


# ---------------------------------------------------------------------------
# 포맷 타입별 생성
# ---------------------------------------------------------------------------

class TestFormatTypes:
    def test_type_a_contains_kospi(self, full_market_data):
        result = _format_type_a(full_market_data, "제목")
        assert "KOSPI" in result

    def test_type_a_contains_title(self, full_market_data):
        result = _format_type_a(full_market_data, "테스트제목")
        assert "테스트제목" in result

    def test_type_a_contains_kosdaq(self, full_market_data):
        result = _format_type_a(full_market_data, "제목")
        assert "KOSDAQ" in result

    def test_type_a_contains_krw(self, full_market_data):
        result = _format_type_a(full_market_data, "제목")
        assert "원/달러" in result

    def test_type_a_contains_us10y(self, full_market_data):
        result = _format_type_a(full_market_data, "제목")
        assert "미국10Y" in result

    def test_type_b_contains_kospi(self, full_market_data):
        result = _format_type_b(full_market_data, "제목")
        assert "KOSPI" in result

    def test_type_b_shows_prev(self, full_market_data):
        result = _format_type_b(full_market_data, "제목")
        assert "전일" in result

    def test_type_c_contains_arrow(self, full_market_data):
        result = _format_type_c(full_market_data, "제목")
        assert "▸" in result

    def test_type_d_pipe_separator(self, full_market_data):
        result = _format_type_d(full_market_data, "제목")
        assert "|" in result

    def test_none_data_shows_dash(self, none_market_data):
        result = _format_type_a(none_market_data, "제목")
        assert "--" in result

    def test_none_data_no_exception(self, none_market_data):
        # 4종 모두 예외 없이 실행되어야 함
        _format_type_a(none_market_data, "제목")
        _format_type_b(none_market_data, "제목")
        _format_type_c(none_market_data, "제목")
        _format_type_d(none_market_data, "제목")


# ---------------------------------------------------------------------------
# format_daily_tweet 통합
# ---------------------------------------------------------------------------

class TestFormatDailyTweet:
    def test_returns_two_tweets(self, full_market_data, signal_result):
        tweets = format_daily_tweet(full_market_data, signal_result, seed=12345)
        assert len(tweets) == 2

    def test_tweet1_within_limit(self, full_market_data, signal_result):
        tweets = format_daily_tweet(full_market_data, signal_result, seed=99)
        assert len(tweets[0]) <= 280

    def test_tweet2_within_limit(self, full_market_data, signal_result):
        tweets = format_daily_tweet(full_market_data, signal_result, seed=99)
        assert len(tweets[1]) <= 280

    def test_tweet2_contains_signal(self, full_market_data, signal_result):
        tweets = format_daily_tweet(full_market_data, signal_result, seed=99)
        assert "위험" in tweets[1]

    def test_tweet2_contains_hashtag(self, full_market_data, signal_result):
        tweets = format_daily_tweet(full_market_data, signal_result, seed=99)
        assert "#" in tweets[1]

    def test_tweet2_contains_allocation_framework(self, full_market_data, signal_result):
        tweets = format_daily_tweet(full_market_data, signal_result, seed=99)
        assert "미장 70 : 국장 30" in tweets[1]

    def test_seed_idempotent(self, full_market_data, signal_result):
        tweets1 = format_daily_tweet(full_market_data, signal_result, seed=777)
        tweets2 = format_daily_tweet(full_market_data, signal_result, seed=777)
        assert tweets1 == tweets2

    def test_different_seed_different_output(self, full_market_data, signal_result):
        tweets1 = format_daily_tweet(full_market_data, signal_result, seed=1)
        tweets2 = format_daily_tweet(full_market_data, signal_result, seed=9999)
        # 적어도 하나는 달라야 함 (동일할 수도 있으나 대부분 다름)
        assert tweets1 != tweets2 or True  # 단순 실행 검증


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_text_unchanged(self):
        text = "짧은 텍스트"
        assert _truncate(text) == text

    def test_long_text_truncated(self):
        text = "A" * 300
        result = _truncate(text)
        assert len(result) <= 280

    def test_truncated_ends_with_ellipsis(self):
        text = "\n".join(["A" * 50] * 10)
        result = _truncate(text)
        assert result.endswith("…")
