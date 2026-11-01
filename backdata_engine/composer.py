"""
backdata_engine/composer.py — 문안 조립 (DN2026_0090)
=====================================================
수치·고지·표본 설명은 코드 템플릿이 만든다. LLM(gemini flash-lite, 무료 키만)은 훅 1줄만 쓴다.
훅이 실패하거나 검사에서 탈락하면 고정 훅으로 대체하고, 그래도 탈락하면 무발행(default-deny).
"""

from __future__ import annotations

import hashlib
import logging

from backdata_engine import gate
from backdata_engine.config import DATA_SOURCE_LABEL, DISCLAIMER, HOOK_MAX_CHARS, HORIZONS

VERSION = "0.1.0"

logger = logging.getLogger(__name__)

_SYSTEM = (
    "너는 한국 주식 데이터 계정의 에디터다. 주어진 조건으로 SNS 첫 줄(훅) 1개만 쓴다. "
    "규칙: 한국어 한 문장, 40자 이내, 호기심을 주는 질문형 권장, 숫자는 조건에 있는 숫자만, "
    "매수·매도·추천·전망·보장 표현 금지, 좋아요·댓글·공유 요청 금지, 해시태그·이모지·링크 금지. "
    'JSON {"hook": "..."} 으로만 답한다.'
)


def _has_batchim(word: str) -> tuple[bool, bool]:
    """(받침 있음, 받침이 ㄹ)."""
    ch = word[-1]
    code = ord(ch) - 0xAC00
    if not 0 <= code <= 11171:
        return False, False
    jong = code % 28
    return jong != 0, jong == 8


def josa_subject(word: str) -> str:
    has, _ = _has_batchim(word)
    return f"{word}{'이' if has else '가'}"


def josa_means(word: str) -> str:
    has, is_rieul = _has_batchim(word)
    return f"{word}{'으로' if has and not is_rieul else '로'}"


def _fmt_up(ratio: float) -> int:
    return round(ratio * 100)


def _fmt_med(pct: float) -> str:
    return f"{pct:+.1f}"


def numbers_of(stats: dict) -> dict:
    """문안에 쓸 수치 문자열과 허용 숫자 집합."""
    nxt = stats["next"]
    y0, y1 = stats["period"][0][:4], stats["period"][1][:4]
    today = stats["today"]
    values = {
        "n": stats["n"],
        "y0": y0,
        "y1": y1,
        "d1_up": _fmt_up(nxt["d1"]["up_ratio"]),
        "d5_up": _fmt_up(nxt["d5"]["up_ratio"]),
        "d20_up": _fmt_up(nxt["d20"]["up_ratio"]),
        "d20_med": _fmt_med(nxt["d20"]["median_pct"]),
        "today": today,
    }
    allowed: set[float] = {
        float(values["n"]), float(y0), float(y1),
        float(values["d1_up"]), float(values["d5_up"]), float(values["d20_up"]),
        abs(float(values["d20_med"])),
        *(float(h) for h in HORIZONS),
    }
    allowed.update(float(p) for p in today.split("-"))
    # 조건 라벨 속 숫자 (예: 5일 연속, 52주, 2~3%)
    for tok in gate._NUMBER_PATTERN.findall(stats["condition_label"]):
        allowed.add(float(tok))
    return {"values": values, "allowed": allowed}


def fixed_hooks(stats: dict) -> list[str]:
    head = f"{stats['index_name']} {stats['condition_label']}"
    return [
        f"{head}, 과거엔 그다음 어땠을까?",
        f"{head}. 과거 같은 날 이후는?",
    ]


def llm_hook(stats: dict, gemini_call) -> tuple[str | None, dict]:
    """(훅 | None, 호출 메타). gemini_call은 core.gemini_gateway.call 호환."""
    prompt = (
        f"조건: {stats['index_name']} {stats['condition_label']} (오늘 종가 기준)\n"
        "과거 같은 조건 이후의 통계를 보여주는 게시물의 첫 줄을 써라."
    )
    result = gemini_call(
        prompt,
        model="flash-lite",
        system_instruction=_SYSTEM,
        max_tokens=200,
        temperature=0.7,
        response_json=True,
        allow_paid=False,
        max_api_calls=2,
    )
    meta = {
        "success": bool(result.get("success")),
        "paid": bool(result.get("paid")),
        "api_calls": result.get("api_calls", 0),
        "error": result.get("error"),
    }
    data = result.get("data") if result.get("success") else None
    hook = data.get("hook") if isinstance(data, dict) else None
    return (hook.strip() if isinstance(hook, str) else None), meta


def build_x(hook: str, v: dict) -> str:
    return (
        f"{hook}\n\n"
        f"같은 경우 {v['n']}번({v['y0']}~{v['y1']})\n"
        f"· 다음 날 상승 {v['d1_up']}%\n"
        f"· 5일 뒤 상승 {v['d5_up']}%\n"
        f"· 20일 뒤 중앙값 {v['d20_med']}%\n\n"
        f"{DISCLAIMER}"
    )


def build_fb(hook: str, stats: dict, v: dict) -> str:
    name = stats["index_name"]
    return (
        f"{hook}\n\n"
        f"오늘({v['today']}) {josa_subject(name)} {josa_means(stats['condition_label'])} "
        "마감했습니다.\n"
        f"{v['y0']}년 이후 같은 경우는 {v['n']}번 있었습니다.\n\n"
        f"· 다음 날 상승한 비율 {v['d1_up']}%\n"
        f"· 5거래일 뒤 상승한 비율 {v['d5_up']}%\n"
        f"· 20거래일 뒤 상승한 비율 {v['d20_up']}%, 수익률 중앙값 {v['d20_med']}%\n\n"
        f"표본: {v['y0']}~{v['y1']} {name} 일봉 종가 기준, 20거래일이 지난 사례만 "
        f"(데이터: {DATA_SOURCE_LABEL})\n"
        f"{DISCLAIMER}"
    )


def compose(stats: dict, gemini_call) -> dict:
    """
    반환: {ok, reason, hook_source, text_x, text_fb, text_hash, x_weighted_len, llm}
    """
    nums = numbers_of(stats)
    v, allowed = nums["values"], nums["allowed"]
    hook, llm_meta = llm_hook(stats, gemini_call)
    candidates: list[tuple[str, str]] = []
    if hook:
        ok, reason = gate.check_hook(hook, allowed, HOOK_MAX_CHARS)
        if ok:
            candidates.append(("llm", hook))
        else:
            logger.warning(f"[BCompose] LLM 훅 탈락({reason}) — 고정 훅으로 대체")
    day_seed = int(stats["today"].replace("-", ""))
    fixed = fixed_hooks(stats)
    candidates.append(("fixed", fixed[day_seed % len(fixed)]))

    last_reason = None
    for source, h in candidates:
        text_x = build_x(h, v)
        text_fb = build_fb(h, stats, v)
        ok_x, rx = gate.check_text(text_x, allowed, "x")
        ok_fb, rf = gate.check_text(text_fb, allowed, "fb")
        if ok_x and ok_fb:
            return {
                "ok": True, "reason": None, "hook_source": source,
                "text_x": text_x, "text_fb": text_fb,
                "text_hash": hashlib.sha256(text_x.encode("utf-8")).hexdigest(),
                "x_weighted_len": gate.weighted_length(text_x),
                "llm": llm_meta,
            }
        last_reason = rx or rf
        logger.warning(f"[BCompose] {source} 문안 탈락 x={rx} fb={rf}")
    return {"ok": False, "reason": last_reason, "hook_source": None,
            "text_x": None, "text_fb": None, "text_hash": None,
            "x_weighted_len": None, "llm": llm_meta}
