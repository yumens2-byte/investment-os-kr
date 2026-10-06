"""
core/gemini_gateway.py
========================
Gemini API 호출 공통 모듈 (KR Market OS, 라이트 OS 적응판)

기능:
  - Main / Sub / Sub2 무료 키 자동 전환 (429 Rate Limit 시)
  - 무료 3키 전부 실패 시 Pay(유료) 키 fallback
  - 지수 백오프 재시도 (최대 3회)
  - 모델 선택 (flash-lite / flash / pro)
  - JSON 응답 파싱 지원

환경변수 (4키 chain):
  GEMINI_API_KEY          — 메인 키 (무료)
  GEMINI_API_SUB_KEY      — 서브 키 (무료)
  GEMINI_API_SUB_SUB_KEY  — 서브2 키 (무료)
  GEMINI_API_SUB_PAY_KEY  — 유료 키 (최후 fallback, 실제 과금)

키 전환 순서: main → sub → sub2 → pay

출처: investment-os 본 OS의 core/gemini_gateway.py v3.1.0 패턴을
      라이트 OS에 이식. DLQ 의존성은 라이트 OS에 모듈이 없어 제거.
      이미지 생성은 라이트 OS에서 미사용으로 제외.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

# ── 환경변수 (4키 chain) ──
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_SUB_KEY = os.getenv("GEMINI_API_SUB_KEY", "")
GEMINI_API_SUB_SUB_KEY = os.getenv("GEMINI_API_SUB_SUB_KEY", "")
GEMINI_API_SUB_PAY_KEY = os.getenv("GEMINI_API_SUB_PAY_KEY", "")  # 유료 fallback

# ── 모델 매핑 ──
MODEL_MAP: dict[str, str] = {
    "flash-lite": "gemini-2.5-flash-lite",
    "flash": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",
}

# ── 기본 설정 ──
MAX_RETRIES: int = 3
BACKOFF_BASE: int = 2
DEFAULT_MAX_TOKENS: int = 1024
MAX_API_CALLS: int = 12


def _error_status(exc: Exception) -> int | None:
    for value in (getattr(exc, "code", None), getattr(exc, "status_code", None)):
        if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
            return value
    match = re.search(r"\b([45][0-9]{2})\b", str(exc))
    return int(match.group(1)) if match else None


def _retryable(exc: Exception, status: int | None) -> bool:
    return status in {408, 429, 500, 502, 503, 504} or isinstance(
        exc, (TimeoutError, ConnectionError)
    ) or type(exc).__name__ in {"ReadTimeout", "ConnectTimeout", "ConnectError"}



# ---------------------------------------------------------------------------
# 키 빌더 (Main → Sub → Sub2 → Pay)
# ---------------------------------------------------------------------------


def _build_keys() -> list[tuple[str, str, bool]]:
    """
    Main/Sub/Sub2(무료) → Pay(유료) 키 리스트 구성.
    Returns: [(key_label, api_key, is_paid), ...]
    """
    keys: list[tuple[str, str, bool]] = []
    if GEMINI_API_KEY:
        keys.append(("main", GEMINI_API_KEY, False))
    if GEMINI_API_SUB_KEY:
        keys.append(("sub", GEMINI_API_SUB_KEY, False))
    if GEMINI_API_SUB_SUB_KEY:
        keys.append(("sub2", GEMINI_API_SUB_SUB_KEY, False))
    if GEMINI_API_SUB_PAY_KEY:
        keys.append(("pay", GEMINI_API_SUB_PAY_KEY, True))
    return keys


def _get_client(api_key: str):
    """google-genai Client 생성"""
    try:
        from google import genai

        return genai.Client(api_key=api_key)
    except ImportError as e:
        raise ImportError(
            "google-genai 미설치. requirements.txt에 'google-genai>=1.0.0' 추가 필요."
        ) from e


def is_available() -> bool:
    """4키 중 1개라도 등록되어 있으면 True"""
    return bool(_build_keys())


# ---------------------------------------------------------------------------
# 텍스트 호출 (call)
# ---------------------------------------------------------------------------


def call(
    prompt: str,
    model: str = "flash-lite",
    system_instruction: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.7,
    response_json: bool = False,
    fallback_value: str | None = None,
    allow_paid: bool = True,
    max_api_calls: int = MAX_API_CALLS,
) -> dict:
    """
    Gemini API 텍스트 호출 (Main → Sub → Sub2 → Pay 자동 전환).

    Args:
        prompt: 프롬프트 본문
        model: "flash-lite" | "flash" | "pro"
        system_instruction: 시스템 지시문 (선택)
        max_tokens: 최대 출력 토큰
        temperature: 0.0~1.0
        response_json: True 시 response_mime_type=application/json
        fallback_value: 전체 실패 시 text에 들어갈 기본값

    Returns:
        {
          "success": bool,
          "text": str,
          "data": dict | None,         # response_json=True일 때 파싱 결과
          "model": str,
          "key_used": str,             # main / sub / sub2 / pay
          "paid": bool,
          "error": str | None,
        }
    """
    keys = [key for key in _build_keys() if allow_paid or not key[2]]
    if not keys:
        logger.warning("[GeminiGW] API 키 미설정 (GEMINI_API_KEY 계열) — 호출 스킵")
        return _fail_result("API 키 미설정", fallback_value)

    try:
        from google.genai import types
    except ImportError as e:
        logger.error(f"[GeminiGW] google-genai SDK 미설치: {e}")
        return _fail_result(f"SDK 미설치: {e}", fallback_value)

    model_name = MODEL_MAP.get(model, MODEL_MAP["flash-lite"])
    last_error = ""
    api_calls = 0
    attempts = []
    call_limit = max(1, min(MAX_API_CALLS, int(max_api_calls)))

    for key_label, api_key, is_paid in keys:
        if api_calls >= call_limit:
            break
        # 유료 키 진입 시 경고 로그 (실제 과금)
        if is_paid:
            logger.warning(f"[GeminiGW] ⚠️ 무료 키 전부 실패 → 유료 키({key_label}) 사용")

        for attempt in range(1, MAX_RETRIES + 1):
            if api_calls >= call_limit:
                break
            request_started = False
            try:
                client = _get_client(api_key)

                config = types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                    system_instruction=system_instruction or None,
                )
                if response_json:
                    config.response_mime_type = "application/json"

                api_calls += 1
                request_started = True
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )

                attempts.append({"key": key_label, "model": model_name, "status": 200})
                text = (response.text or "").strip()

                result = {
                    "success": True,
                    "text": text,
                    "data": None,
                    "model": model_name,
                    "key_used": key_label,
                    "paid": is_paid,
                    "error": None,
                    "api_calls": api_calls,
                    "attempts": attempts,
                    "usage": {
                        name: getattr(getattr(response, "usage_metadata", None), name, None)
                        for name in (
                            "prompt_token_count",
                            "candidates_token_count",
                            "total_token_count",
                        )
                    },
                }

                # JSON 모드 시 파싱 시도
                if response_json:
                    try:
                        clean = text
                        if clean.startswith("```"):
                            clean = clean.split("\n", 1)[-1]
                        if clean.endswith("```"):
                            clean = clean.rsplit("```", 1)[0]
                        clean = clean.strip()
                        result["data"] = json.loads(clean)
                    except json.JSONDecodeError as je:
                        logger.warning(f"[GeminiGW] JSON 파싱 실패 (text는 유지): {je}")
                        # 파싱 실패해도 success=True (호출자가 text로 처리 가능)

                logger.info(
                    f"[GeminiGW] 호출 성공 | key={key_label} paid={is_paid} "
                    f"model={model_name} attempt={attempt} len={len(text)}"
                )
                return result

            except Exception as e:
                status = _error_status(e)
                last_error = f"{type(e).__name__}: {e}"
                for _, secret, _ in keys:
                    last_error = last_error.replace(secret, "[REDACTED]")
                attempts.append({
                    "key": key_label, "model": model_name, "status": status,
                    "request_started": request_started, "error_type": type(e).__name__,
                })
                logger.warning(
                    "[GeminiGW] failure key=%s model=%s attempt=%s status=%s error_type=%s",
                    key_label, model_name, attempt, status, type(e).__name__,
                )
                # Bad payloads do not improve on another key. Auth/model availability
                # can differ by project; 401/403/404 move directly to the next key.
                if status in {400, 422}:
                    result = _fail_result(last_error, fallback_value)
                    result.update(api_calls=api_calls, attempts=attempts)
                    return result
                if not _retryable(e, status):
                    break
                if attempt < MAX_RETRIES and api_calls < call_limit:
                    time.sleep(BACKOFF_BASE**attempt)

    # 모든 키 실패
    logger.error(f"[GeminiGW] 전체 키 실패: {last_error}")
    result = _fail_result(last_error, fallback_value)
    result["api_calls"] = api_calls
    result["attempts"] = attempts
    return result


# ---------------------------------------------------------------------------
# 실패 결과 빌더
# ---------------------------------------------------------------------------


def _fail_result(error: str, fallback_value: str | None = None) -> dict:
    return {
        "success": False,
        "text": fallback_value or "",
        "data": None,
        "model": "",
        "key_used": "",
        "paid": False,
        "error": error,
        "api_calls": 0,
    }
