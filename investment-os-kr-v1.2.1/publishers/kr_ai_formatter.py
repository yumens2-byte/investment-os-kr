"""
publishers/kr_ai_formatter.py
==============================
KR Market OS — Gemini AI 트윗 생성기 (Track C)

목적:
  - 기존 kr_formatter.py의 하드코딩 톤을 보완 (로봇같은 반복 방지)
  - Gemini로 3트윗을 1회 호출에 통합 생성
  - 매일 다른 자연스러운 한국어 표현

설계 원칙:
  - 기존 kr_formatter.py 무수정 (fallback 자동 작동)
  - run_market.py에서 USE_AI_TONE 분기로 진입
  - 실패 시 None 반환 → 호출자가 fallback 진행
  - 비한국어 문자 가드 (힌디/아랍/태국/러시아 등 차단)
  - 트윗별 280자 검증

호출 비용 (참고):
  - Flash-Lite 1회 호출 ≈ $0.0004 (input 1500 + output 600 토큰 추정)
  - 평일 1회 = 월 ~22회 = ~$0.01 (1센트 미만)
  - 무료 키 사용 시 비용 0
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

# ── 트윗 길이 제한 ──
_MAX_TWEET_LENGTH = 280

# ── 비한국어/비영어 문자 가드 (힌디, 아랍, 태국, 러시아, 일본 가나 등 차단) ──
# 허용: 한글(가-힣), 영문(A-Za-z), 숫자, 일반 문장부호, 이모지 일부
_NON_PUBLISHABLE_PATTERN = re.compile(
    r"[\u0900-\u097F"  # 데바나가리 (힌디)
    r"\u0600-\u06FF"   # 아랍어
    r"\u0E00-\u0E7F"   # 태국어
    r"\u0400-\u04FF"   # 키릴(러시아)
    r"\u3040-\u309F"   # 히라가나
    r"\u30A0-\u30FF"   # 가타카나
    r"]"
)


# ---------------------------------------------------------------------------
# 시스템 인스트럭션 (Gemini 톤 가이드)
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION = """당신은 한국어 미장 투자 콘텐츠 크리에이터입니다.

규칙:
1. 투자 권유 금지 — 정보 제공만
2. 매번 다른 자연스러운 표현 (로봇같은 반복 금지)
3. 한국어 + 영어 + 숫자 + 이모지만 사용 (다른 언어 절대 금지)
4. 직장인 투자자에게 친근하지만 전문적인 톤
5. 각 트윗은 280자 이내로 작성
6. 단정적 예측 금지, 가능성/관찰 표현 사용
7. 출력은 반드시 JSON: {"tweet1": "...", "tweet2": "...", "tweet3": "..."}
   - JSON 외 다른 설명, 마크다운, 백틱 모두 금지"""


# ---------------------------------------------------------------------------
# 공개 진입점
# ---------------------------------------------------------------------------

def generate_ai_thread(
    market_data: dict,
    signal_result: dict,
    sector_data: list[dict] | None = None,
) -> Optional[list[str]]:
    """
    Gemini로 3트윗 통합 생성.

    Args:
        market_data: 수집된 시장 데이터
        signal_result: kr_market_engine 결과
        sector_data: 섹터 데이터 (선택)

    Returns:
        검증 통과 시 ["tweet1", "tweet2", "tweet3"]
        실패 시 None (호출자가 fallback 처리)
    """
    try:
        from core.gemini_gateway import call, is_available
    except ImportError as e:
        logger.warning(f"[AIFormatter] gemini_gateway import 실패: {e}")
        return None

    if not is_available():
        logger.info("[AIFormatter] Gemini 키 미설정 → fallback 신호")
        return None

    prompt = _build_prompt(market_data, signal_result, sector_data)

    # 1차 시도
    result = call(
        prompt=prompt,
        model="flash-lite",
        system_instruction=_SYSTEM_INSTRUCTION,
        max_tokens=900,
        temperature=0.85,
        response_json=True,
    )

    if not result.get("success"):
        logger.warning(f"[AIFormatter] Gemini 호출 실패: {result.get('error')}")
        return None

    tweets = _parse_response(result)
    if tweets is None:
        return None

    # 검증
    validated = _validate_tweets(tweets)
    if validated is not None:
        logger.info(
            f"[AIFormatter] AI 트윗 생성 성공 | "
            f"key={result.get('key_used')} paid={result.get('paid')} "
            f"lengths={[len(t) for t in validated]}"
        )
        return validated

    # 1회 재시도 (검증 실패 시)
    logger.info("[AIFormatter] 검증 실패 → 1회 재시도")
    retry_prompt = (
        prompt
        + "\n\n"
        + "이전 응답이 길이 또는 언어 검증을 통과하지 못했습니다. "
        + "각 트윗을 280자 이내, 한국어/영어/숫자/이모지만 사용해서 다시 작성하세요. "
        + "JSON {\"tweet1\":..., \"tweet2\":..., \"tweet3\":...} 외에 어떤 텍스트도 출력하지 마세요."
    )
    retry_result = call(
        prompt=retry_prompt,
        model="flash-lite",
        system_instruction=_SYSTEM_INSTRUCTION,
        max_tokens=900,
        temperature=0.7,  # 재시도 시 안정성 우선
        response_json=True,
    )

    if not retry_result.get("success"):
        return None

    retry_tweets = _parse_response(retry_result)
    if retry_tweets is None:
        return None

    return _validate_tweets(retry_tweets)


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _build_prompt(
    market_data: dict,
    signal_result: dict,
    sector_data: list[dict] | None,
) -> str:
    """Gemini 프롬프트 생성. 운용 프레임 'Mini Flow 7:3' 라벨 포함 강제."""
    framework = os.getenv("THREAD_FRAMEWORK_LABEL", "Mini Flow 7:3")
    us_ratio = os.getenv("US_ALLOCATION_RATIO", "70")
    kr_ratio = os.getenv("KR_ALLOCATION_RATIO", "30")

    sector_block = _build_sector_block(sector_data) if sector_data else "(섹터 데이터 없음)"

    return f"""다음 시장 데이터를 바탕으로 한국어 X 쓰레드 3개를 작성하세요.

[운용 프레임]
{framework} (미장 {us_ratio} : 국장 {kr_ratio})

[국장 지표]
- KOSPI: {market_data.get('kospi')} ({_fmt_pct(market_data.get('kospi_chg_pct'))})
- KOSDAQ: {market_data.get('kosdaq')} ({_fmt_pct(market_data.get('kosdaq_chg_pct'))})
- 삼성전자: {market_data.get('samsung')} ({_fmt_pct(market_data.get('samsung_chg_pct'))})
- SK하이닉스: {market_data.get('skhynix')} ({_fmt_pct(market_data.get('skhynix_chg_pct'))})

[환율/매크로]
- 원/달러: {market_data.get('krw_usd')}원 (변동 {market_data.get('krw_usd_chg')})
- 미국 10Y: {market_data.get('us10y')}% (변동 {market_data.get('us10y_chg')})
- 미국 2Y: {market_data.get('us2y')}%
- 달러지수(DXY): {market_data.get('dxy')} ({_fmt_pct(market_data.get('dxy_chg_pct'))})
- 장단기 스프레드: {market_data.get('t10y2y')}

[시그널 판정 (kr_market_engine)]
- 종합 시그널: {signal_result.get('market_signal')}
- 점수: {signal_result.get('signal_score')}
- 환율 레짐: {signal_result.get('krw_regime')}
- 외인 압박: {signal_result.get('foreign_pressure')}
- 금리 부담: {signal_result.get('rate_burden')}
- 수익률 곡선: {signal_result.get('yield_spread')}

[섹터 흐름]
{sector_block}

[작성 가이드]
- tweet1: 오늘의 시장 한 줄 요약 + 핵심 지표 3~4개. 친근한 한국어 톤.
- tweet2: 시그널 해석 + "{framework}" 라벨 명시. 외인/금리 영향을 1~2줄로 풀어 설명.
- tweet3: 섹터 흐름 또는 행동 가이드. 해시태그 3~5개 포함 (#KOSPI #국장 #환율 등).

JSON 형식으로만 응답:
{{"tweet1": "...", "tweet2": "...", "tweet3": "..."}}"""


def _build_sector_block(sector_data: list[dict] | None) -> str:
    """섹터 데이터를 프롬프트용 텍스트로 변환."""
    if not sector_data:
        return "(섹터 데이터 없음)"
    lines: list[str] = []
    for s in sector_data[:5]:  # 상위 5개만
        if not isinstance(s, dict):
            continue
        name = s.get("name", "?")
        chg = s.get("chg_pct")
        direction = s.get("direction", "")
        if chg is None:
            lines.append(f"- {name}: 데이터 없음")
        else:
            lines.append(f"- {name}: {chg:+.2f}% ({direction})")
    return "\n".join(lines) if lines else "(섹터 데이터 없음)"


def _parse_response(result: dict) -> Optional[dict]:
    """Gemini 응답에서 tweets dict 추출."""
    # response_json=True로 호출했으므로 data가 있어야 함
    data = result.get("data")
    if isinstance(data, dict):
        return data

    # 백업 파싱: text에서 JSON 추출 시도
    text = result.get("text", "").strip()
    if not text:
        return None

    # ```json ``` 백틱 제거
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError as e:
        logger.warning(f"[AIFormatter] JSON 파싱 실패: {e}")

    return None


def _validate_tweets(tweets_dict: dict) -> Optional[list[str]]:
    """
    트윗 검증:
      1) tweet1/2/3 키 존재
      2) 각 트윗 280자 이내
      3) 비한국어 문자(힌디/아랍/태국/러시아 등) 없음
    실패 시 None.
    """
    if not isinstance(tweets_dict, dict):
        return None

    tweets: list[str] = []
    for key in ("tweet1", "tweet2", "tweet3"):
        text = tweets_dict.get(key)
        if not isinstance(text, str) or not text.strip():
            logger.warning(f"[AIFormatter] {key} 누락 또는 빈 문자열")
            return None
        text = text.strip()

        if len(text) > _MAX_TWEET_LENGTH:
            logger.warning(f"[AIFormatter] {key} 길이 초과: {len(text)}자")
            return None

        non_kr = _detect_non_publishable_chars(text)
        if non_kr:
            logger.warning(f"[AIFormatter] {key} 비한국어 문자 감지: '{non_kr}'")
            return None

        tweets.append(text)

    return tweets


def _detect_non_publishable_chars(text: str) -> Optional[str]:
    """비한국어 문자 감지 → 첫 문자 반환 또는 None"""
    match = _NON_PUBLISHABLE_PATTERN.search(text)
    return match.group(0) if match else None


def _fmt_pct(value) -> str:
    """퍼센트 포맷 (None 안전)."""
    if value is None:
        return "N/A"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "N/A"
