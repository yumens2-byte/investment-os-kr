"""
tests/test_kr_ai_formatter.py
==============================
AI 트윗 생성기 단위 테스트 (mock 기반)

검증:
  - is_available() False → None 반환
  - 정상 응답 JSON 파싱 → 3트윗 반환
  - 백틱 감싼 JSON 파싱
  - 길이 초과 → 재시도 → 성공
  - 비한국어 문자 감지 → 재시도
  - 모든 재시도 실패 → None
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from publishers.kr_ai_formatter import (
    _build_prompt,
    _detect_non_publishable_chars,
    _parse_response,
    _validate_tweets,
    generate_ai_thread,
)


@pytest.fixture()
def market_data() -> dict:
    return {
        "kospi": 2700.0, "kospi_chg_pct": 0.5,
        "kosdaq": 870.0, "kosdaq_chg_pct": -0.3,
        "samsung": 80000.0, "samsung_chg_pct": 1.2,
        "skhynix": 195000.0, "skhynix_chg_pct": -0.8,
        "krw_usd": 1380.0, "krw_usd_chg": -2.5,
        "us10y": 4.45, "us10y_chg": 0.02,
        "us2y": 4.85,
        "dxy": 104.1, "dxy_chg_pct": 0.1,
        "t10y2y": -0.40,
    }


@pytest.fixture()
def signal_result() -> dict:
    return {
        "market_signal": "주의",
        "signal_score": 1,
        "krw_regime": "WEAK",
        "foreign_pressure": "MEDIUM",
        "rate_burden": "MEDIUM",
        "yield_spread": "INVERTED",
    }


@pytest.fixture()
def sector_data() -> list[dict]:
    return [
        {"name": "반도체", "direction": "up", "chg_pct": 1.5, "ratio": 60},
        {"name": "2차전지", "direction": "down", "chg_pct": -1.2, "ratio": 40},
    ]


# ---------------------------------------------------------------------------
# 비한국어 가드
# ---------------------------------------------------------------------------

class TestNonPublishableChars:
    def test_korean_only(self):
        assert _detect_non_publishable_chars("안녕하세요 KOSPI 2700") is None

    def test_english_numbers_emoji_ok(self):
        assert _detect_non_publishable_chars("📊 SPY +1.5% #KOSPI") is None

    def test_hindi_blocked(self):
        # 데바나가리 문자
        assert _detect_non_publishable_chars("नमस्ते KOSPI") is not None

    def test_arabic_blocked(self):
        assert _detect_non_publishable_chars("مرحبا KOSPI") is not None

    def test_thai_blocked(self):
        assert _detect_non_publishable_chars("สวัสดี KOSPI") is not None

    def test_russian_blocked(self):
        assert _detect_non_publishable_chars("Привет KOSPI") is not None

    def test_japanese_kana_blocked(self):
        assert _detect_non_publishable_chars("こんにちは KOSPI") is not None


# ---------------------------------------------------------------------------
# _validate_tweets
# ---------------------------------------------------------------------------

class TestValidateTweets:
    def test_valid_three_tweets(self):
        tweets_dict = {"tweet1": "A", "tweet2": "B", "tweet3": "C"}
        result = _validate_tweets(tweets_dict)
        assert result == ["A", "B", "C"]

    def test_missing_key_returns_none(self):
        tweets_dict = {"tweet1": "A", "tweet2": "B"}
        assert _validate_tweets(tweets_dict) is None

    def test_empty_string_returns_none(self):
        tweets_dict = {"tweet1": "A", "tweet2": "", "tweet3": "C"}
        assert _validate_tweets(tweets_dict) is None

    def test_length_over_280_returns_none(self):
        tweets_dict = {"tweet1": "A", "tweet2": "X" * 300, "tweet3": "C"}
        assert _validate_tweets(tweets_dict) is None

    def test_non_korean_returns_none(self):
        tweets_dict = {"tweet1": "안녕", "tweet2": "नमस्ते", "tweet3": "C"}
        assert _validate_tweets(tweets_dict) is None

    def test_not_dict_returns_none(self):
        assert _validate_tweets(["a", "b", "c"]) is None
        assert _validate_tweets(None) is None


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_data_field_dict(self):
        result = {"data": {"tweet1": "A", "tweet2": "B"}, "text": ""}
        assert _parse_response(result) == {"tweet1": "A", "tweet2": "B"}

    def test_text_field_json(self):
        result = {"data": None, "text": '{"tweet1": "A"}'}
        assert _parse_response(result) == {"tweet1": "A"}

    def test_text_field_with_backticks(self):
        result = {"data": None, "text": '```json\n{"tweet1": "A"}\n```'}
        assert _parse_response(result) == {"tweet1": "A"}

    def test_invalid_json_returns_none(self):
        result = {"data": None, "text": "not json at all"}
        assert _parse_response(result) is None

    def test_empty_text_returns_none(self):
        result = {"data": None, "text": ""}
        assert _parse_response(result) is None


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_includes_market_data(self, market_data, signal_result):
        prompt = _build_prompt(market_data, signal_result, None)
        assert "2700" in prompt or "2700.0" in prompt
        assert "1380" in prompt or "1380.0" in prompt

    def test_includes_signal(self, market_data, signal_result):
        prompt = _build_prompt(market_data, signal_result, None)
        assert "주의" in prompt
        assert "WEAK" in prompt

    def test_includes_framework_label(self, market_data, signal_result):
        prompt = _build_prompt(market_data, signal_result, None)
        # 디폴트 라벨 또는 환경변수
        assert "Mini Flow" in prompt or "7:3" in prompt

    def test_sector_block_when_provided(self, market_data, signal_result, sector_data):
        prompt = _build_prompt(market_data, signal_result, sector_data)
        assert "반도체" in prompt
        assert "2차전지" in prompt

    def test_sector_block_empty_when_none(self, market_data, signal_result):
        prompt = _build_prompt(market_data, signal_result, None)
        assert "데이터 없음" in prompt or "없음" in prompt


# ---------------------------------------------------------------------------
# generate_ai_thread (통합)
# ---------------------------------------------------------------------------

class TestGenerateAiThread:
    def test_no_api_key_returns_none(self, market_data, signal_result):
        with patch.dict(os.environ, {}, clear=True):
            # gemini_gateway 다시 로드
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)
            result = generate_ai_thread(market_data, signal_result)
            assert result is None

    def test_success_first_attempt(self, market_data, signal_result):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1"}, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            mock_response_text = (
                '{"tweet1": "📊 KOSPI 2700 +0.5% 좋은 흐름", '
                '"tweet2": "주의 시그널, Mini Flow 7:3 유지", '
                '"tweet3": "반도체 강세, #KOSPI #국장"}'
            )
            from unittest.mock import MagicMock
            mock_resp = MagicMock()
            mock_resp.text = mock_response_text
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_resp

            with patch.object(gw, "_get_client", return_value=mock_client):
                result = generate_ai_thread(market_data, signal_result)

            assert result is not None
            assert len(result) == 3
            assert "KOSPI" in result[0]
            assert "Mini Flow" in result[1] or "7:3" in result[1]

    def test_first_too_long_retry_succeeds(self, market_data, signal_result):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1"}, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            from unittest.mock import MagicMock
            # 1차: tweet2가 300자 (검증 실패) → 재시도
            # 2차: 정상
            long_tweet = "X" * 300
            responses = [
                MagicMock(text=f'{{"tweet1": "A", "tweet2": "{long_tweet}", "tweet3": "C"}}'),
                MagicMock(text='{"tweet1": "정상1", "tweet2": "정상2", "tweet3": "정상3"}'),
            ]
            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = responses

            with patch.object(gw, "_get_client", return_value=mock_client):
                with patch("core.gemini_gateway.time.sleep"):
                    result = generate_ai_thread(market_data, signal_result)

            assert result == ["정상1", "정상2", "정상3"]

    def test_all_fail_returns_none(self, market_data, signal_result):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1"}, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            from unittest.mock import MagicMock
            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = RuntimeError("API down")

            with patch.object(gw, "_get_client", return_value=mock_client):
                with patch("core.gemini_gateway.time.sleep"):
                    result = generate_ai_thread(market_data, signal_result)

            assert result is None
