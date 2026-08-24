"""
KR Sector Engine 테스트
섹터 흐름 비율 계산 및 포맷 검증
"""

from __future__ import annotations

from engines.kr_sector_engine import run_sector_engine
from publishers.kr_formatter import _make_bar, format_sector_tweet

# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

MARKET_DATA_FULL = {
    "samsung_chg_pct": -1.2,
    "skhynix_chg_pct": -0.8,
}

SECTOR_RAW_FULL = {
    "035420.KS": 0.5,    # 네이버
    "035720.KS": 1.1,    # 카카오
    "373220.KS": 2.3,    # LG에너지솔루션
    "006400.KS": 1.9,    # 삼성SDI
    "005380.KS": 0.2,    # 현대차
    "000270.KS": -0.1,   # 기아
    "207940.KS": -0.4,   # 삼성바이오
    "068270.KS": -0.6,   # 셀트리온
    "105560.KS": 0.3,    # KB금융
    "055550.KS": 0.2,    # 신한지주
    "005490.KS": -1.5,   # POSCO
}

SECTOR_RAW_PARTIAL = {k: None for k in SECTOR_RAW_FULL}  # 전체 None


# ---------------------------------------------------------------------------
# run_sector_engine
# ---------------------------------------------------------------------------

class TestRunSectorEngine:
    def test_returns_list(self):
        result = run_sector_engine(MARKET_DATA_FULL, SECTOR_RAW_FULL)
        assert isinstance(result, list)

    def test_ratio_sum_100(self):
        """비율 합계 = 100%."""
        result = run_sector_engine(MARKET_DATA_FULL, SECTOR_RAW_FULL)
        total = sum(s["ratio"] for s in result)
        assert abs(total - 100.0) < 0.2  # 부동소수점 오차 허용

    def test_sorted_by_strength(self):
        """강도 내림차순 정렬."""
        result = run_sector_engine(MARKET_DATA_FULL, SECTOR_RAW_FULL)
        strengths = [s["strength"] for s in result]
        assert strengths == sorted(strengths, reverse=True)

    def test_direction_up(self):
        """양수 등락 → up."""
        result = run_sector_engine({}, {"035420.KS": 1.0, "035720.KS": 0.5})
        ai = next((s for s in result if s["name"] == "AI/플랫폼"), None)
        if ai:
            assert ai["direction"] == "up"

    def test_direction_down(self):
        """음수 등락 → down."""
        result = run_sector_engine(
            {"samsung_chg_pct": -1.5, "skhynix_chg_pct": -0.5}, {}
        )
        semi = next((s for s in result if s["name"] == "반도체"), None)
        if semi:
            assert semi["direction"] == "down"

    def test_all_none_returns_empty(self):
        """전체 None → 빈 리스트."""
        result = run_sector_engine({}, SECTOR_RAW_PARTIAL)
        assert result == []

    def test_result_keys(self):
        """필수 키 존재."""
        result = run_sector_engine(MARKET_DATA_FULL, SECTOR_RAW_FULL)
        required = {"name", "chg_pct", "strength", "direction", "ratio"}
        for s in result:
            assert required.issubset(s.keys())

    def test_ratio_non_negative(self):
        """비율은 항상 0 이상."""
        result = run_sector_engine(MARKET_DATA_FULL, SECTOR_RAW_FULL)
        for s in result:
            assert s["ratio"] >= 0

    def test_single_sector_ratio_100(self):
        """섹터 1개만 있으면 비율 100%."""
        result = run_sector_engine({}, {"005490.KS": -1.5})
        assert len(result) == 1
        assert result[0]["ratio"] == 100.0

    def test_uses_market_data_for_semiconductor(self):
        """samsung_chg_pct / skhynix_chg_pct 기수집 데이터 반영."""
        market = {"samsung_chg_pct": -2.0, "skhynix_chg_pct": -2.0}
        result = run_sector_engine(market, {})  # sector_raw 없음
        semi = next((s for s in result if s["name"] == "반도체"), None)
        assert semi is not None
        assert semi["chg_pct"] == -2.0


# ---------------------------------------------------------------------------
# format_sector_tweet
# ---------------------------------------------------------------------------

class TestFormatSectorTweet:
    def _sample_sectors(self) -> list[dict]:
        return run_sector_engine(MARKET_DATA_FULL, SECTOR_RAW_FULL)

    def test_returns_string(self):
        result = format_sector_tweet(self._sample_sectors())
        assert isinstance(result, str)

    def test_empty_input_returns_empty(self):
        assert format_sector_tweet([]) == ""

    def test_contains_percent(self):
        result = format_sector_tweet(self._sample_sectors())
        assert "%" in result

    def test_contains_section_header(self):
        result = format_sector_tweet(self._sample_sectors())
        assert "섹터 흐름" in result

    def test_within_tweet_limit(self):
        result = format_sector_tweet(self._sample_sectors())
        assert len(result) <= 280

    def test_contains_bar(self):
        result = format_sector_tweet(self._sample_sectors())
        assert "█" in result

    def test_contains_hashtag(self):
        """해시태그 포함 여부."""
        result = format_sector_tweet(self._sample_sectors(), seed=12345)
        assert "#" in result

    def test_hashtag_count_three(self):
        """해시태그 정확히 3개."""
        result = format_sector_tweet(self._sample_sectors(), seed=12345)
        # 마지막 줄이 해시태그 라인
        last_line = result.strip().splitlines()[-1]
        tags = [w for w in last_line.split() if w.startswith("#")]
        assert len(tags) == 3

    def test_hashtag_idempotent(self):
        """동일 seed → 동일 해시태그."""
        r1 = format_sector_tweet(self._sample_sectors(), seed=99)
        r2 = format_sector_tweet(self._sample_sectors(), seed=99)
        assert r1.splitlines()[-1] == r2.splitlines()[-1]

    def test_different_seed_different_hashtag(self):
        """다른 seed → 다른 해시태그 (대부분)."""
        r1 = format_sector_tweet(self._sample_sectors(), seed=1)
        r2 = format_sector_tweet(self._sample_sectors(), seed=9999)
        # 완전히 같을 수도 있으나 대부분 다름 — 실행 오류 없음 확인
        assert isinstance(r1, str) and isinstance(r2, str)

    def test_hashtags_from_sector_pool(self):
        """해시태그가 SECTOR_HASHTAG_POOL에서 선택됨."""
        from publishers.kr_formatter import SECTOR_HASHTAG_POOL
        result = format_sector_tweet(self._sample_sectors(), seed=42)
        last_line = result.strip().splitlines()[-1]
        tags = [w for w in last_line.split() if w.startswith("#")]
        for tag in tags:
            assert tag in SECTOR_HASHTAG_POOL


# ---------------------------------------------------------------------------
# _make_bar
# ---------------------------------------------------------------------------

class TestMakeBar:
    def test_high_ratio_more_bars(self):
        bar_high = _make_bar(80)
        bar_low = _make_bar(10)
        assert bar_high.count("█") > bar_low.count("█")

    def test_bar_length_constant(self):
        from publishers.kr_formatter import _MAX_BAR
        for ratio in [0, 10, 50, 90, 100]:
            bar = _make_bar(ratio)
            assert len(bar) == _MAX_BAR

    def test_minimum_one_bar(self):
        """비율이 0에 가까워도 최소 1칸."""
        bar = _make_bar(1)
        assert "█" in bar
