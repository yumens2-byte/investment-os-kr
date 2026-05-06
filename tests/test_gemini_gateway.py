"""
tests/test_gemini_gateway.py
============================
Gemini Gateway 단위 테스트 (mock 기반)

검증 항목:
  - 4키 chain 빌더 (Main → Sub → Sub2 → Pay)
  - 키 미설정 시 fail_result 반환
  - is_available() 정확성
  - JSON 응답 파싱 (백틱 제거)
  - 백오프 재시도 동작 (mock)
  - 유료 키 진입 시 paid=True 반환
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 키 빌더 / is_available
# ---------------------------------------------------------------------------

class TestBuildKeys:
    def test_no_keys_empty_list(self):
        with patch.dict(os.environ, {}, clear=True):
            # 모듈 재로드해야 환경변수 다시 읽음
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)
            assert gw._build_keys() == []
            assert gw.is_available() is False

    def test_main_only(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1"}, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)
            keys = gw._build_keys()
            assert len(keys) == 1
            assert keys[0] == ("main", "k1", False)

    def test_all_four_keys_order(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "k1",
            "GEMINI_API_SUB_KEY": "k2",
            "GEMINI_API_SUB_SUB_KEY": "k3",
            "GEMINI_API_SUB_PAY_KEY": "k4",
        }, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)
            keys = gw._build_keys()
            assert len(keys) == 4
            assert keys[0] == ("main", "k1", False)
            assert keys[1] == ("sub", "k2", False)
            assert keys[2] == ("sub2", "k3", False)
            assert keys[3] == ("pay", "k4", True)

    def test_pay_only(self):
        with patch.dict(os.environ, {"GEMINI_API_SUB_PAY_KEY": "kpay"}, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)
            keys = gw._build_keys()
            assert len(keys) == 1
            assert keys[0] == ("pay", "kpay", True)
            assert gw.is_available() is True


# ---------------------------------------------------------------------------
# call() — 키 미설정
# ---------------------------------------------------------------------------

class TestCallNoKey:
    def test_no_key_returns_fail(self):
        with patch.dict(os.environ, {}, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)
            result = gw.call("test prompt")
            assert result["success"] is False
            assert "키 미설정" in result["error"]
            assert result["text"] == ""

    def test_no_key_uses_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)
            result = gw.call("test", fallback_value="fallback text")
            assert result["text"] == "fallback text"


# ---------------------------------------------------------------------------
# call() — 정상 호출 (mock)
# ---------------------------------------------------------------------------

class TestCallSuccess:
    def test_success_main_key(self):
        env = {"GEMINI_API_KEY": "k1"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            mock_response = MagicMock()
            mock_response.text = "안녕하세요"
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_response

            with patch.object(gw, "_get_client", return_value=mock_client):
                result = gw.call("프롬프트")

            assert result["success"] is True
            assert result["text"] == "안녕하세요"
            assert result["key_used"] == "main"
            assert result["paid"] is False
            assert result["error"] is None

    def test_json_mode_parses_response(self):
        env = {"GEMINI_API_KEY": "k1"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            mock_response = MagicMock()
            mock_response.text = '{"key": "value", "num": 42}'
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_response

            with patch.object(gw, "_get_client", return_value=mock_client):
                result = gw.call("p", response_json=True)

            assert result["data"] == {"key": "value", "num": 42}

    def test_json_mode_strips_backticks(self):
        env = {"GEMINI_API_KEY": "k1"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            mock_response = MagicMock()
            mock_response.text = '```json\n{"a": 1}\n```'
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_response

            with patch.object(gw, "_get_client", return_value=mock_client):
                result = gw.call("p", response_json=True)

            assert result["data"] == {"a": 1}


# ---------------------------------------------------------------------------
# call() — 키 chain 전환
# ---------------------------------------------------------------------------

class TestCallKeyFallback:
    def test_main_fails_sub_succeeds(self):
        env = {"GEMINI_API_KEY": "k1", "GEMINI_API_SUB_KEY": "k2"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            # Main 키 클라이언트는 항상 실패, Sub 키는 성공
            def get_client_side_effect(api_key):
                client = MagicMock()
                if api_key == "k1":
                    client.models.generate_content.side_effect = RuntimeError("429 quota")
                else:
                    mock_response = MagicMock()
                    mock_response.text = "Sub 응답"
                    client.models.generate_content.return_value = mock_response
                return client

            with patch.object(gw, "_get_client", side_effect=get_client_side_effect):
                # 시간 단축: time.sleep mock
                with patch("core.gemini_gateway.time.sleep"):
                    result = gw.call("p")

            assert result["success"] is True
            assert result["text"] == "Sub 응답"
            assert result["key_used"] == "sub"
            assert result["paid"] is False

    def test_free_keys_fail_pay_succeeds(self):
        env = {
            "GEMINI_API_KEY": "k1",
            "GEMINI_API_SUB_KEY": "k2",
            "GEMINI_API_SUB_SUB_KEY": "k3",
            "GEMINI_API_SUB_PAY_KEY": "kpay",
        }
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            def get_client_side_effect(api_key):
                client = MagicMock()
                if api_key == "kpay":
                    mock_response = MagicMock()
                    mock_response.text = "Paid 응답"
                    client.models.generate_content.return_value = mock_response
                else:
                    client.models.generate_content.side_effect = RuntimeError("429")
                return client

            with patch.object(gw, "_get_client", side_effect=get_client_side_effect):
                with patch("core.gemini_gateway.time.sleep"):
                    result = gw.call("p")

            assert result["success"] is True
            assert result["text"] == "Paid 응답"
            assert result["key_used"] == "pay"
            assert result["paid"] is True

    def test_all_keys_fail_returns_error(self):
        env = {"GEMINI_API_KEY": "k1", "GEMINI_API_SUB_KEY": "k2"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import core.gemini_gateway as gw
            importlib.reload(gw)

            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = RuntimeError("429 quota")

            with patch.object(gw, "_get_client", return_value=mock_client):
                with patch("core.gemini_gateway.time.sleep"):
                    result = gw.call("p", fallback_value="fb")

            assert result["success"] is False
            assert "429" in result["error"]
            assert result["text"] == "fb"
