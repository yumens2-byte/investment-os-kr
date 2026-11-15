"""
reply_engine/humanize.py
==========================
사람 문체 변형층 (DN2026_0104, X 전용).

근거 — 운영 DB 실측 (2026-10-01 ~ 11-15, 답글 70건 vs 사람 댓글 288건):
  지표                  답글   댓글
  끝 마침표 '.'          49%    9%
  😊🙂 이모지            24%    1%
  ㅋ·ㅎ                   3%   21%
  '…'·'~'                 0%   17%
  쉼표                   13%    3%
  구어체 어미(당·용·넵)    1%    7%
  어절당 한글 수          3.0   3.1   ← 띄어쓰기는 사람과 차이 없음
차이는 문장부호·이모지·말투에서 난다. 무작위 자모 오타는 넣지 않는다(마스터 승인 Q2).

변형 (순서대로, 답글마다 독립 시행):
  T6 구어체 사전 치환 1곳        6%
  T4 쉼표 → 공백                 70% (쉼표가 있을 때)
  T5 짧은 어절 1곳 붙여쓰기       5%
  T2 😊🙂 제거(60%)/'ㅎㅎ'·'ㅎ' 교체(40%)  70% (이모지가 있을 때)
  T1 끝 마침표 제거               80% (마침표로 끝날 때)
  T3 끝 여운 'ㅎㅎ'·'ㅎ'·'~' (웃음 의도는 'ㅋㅋ')  20% (끝에 웃음·이모지가 없을 때)

보호: 숫자·%·$·영문 대문자 티커·@·#·URL·'○○님' 토큰은 바꾸지도 붙이지도 않는다.
결정성: 난수는 (reply_tweet_id, 원문)으로 고정 — 같은 댓글은 항상 같은 결과(STD-PUB-06).
모드: REPLY_HUMANIZE_MODE = off | shadow | live (기본 shadow, 그 외 값은 shadow).
  shadow = 변형안을 메타데이터에만 남기고 원문 발행 / live = 변형안 발행.
호출부(run_reply)는 변형안을 게이트에 다시 통과시키고, 탈락하면 원문을 쓴다.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
from dataclasses import dataclass, field

VERSION = "1.0.0"  # DN2026_0104

MODES = ("off", "shadow", "live")

P_COLLOQUIAL = 0.06
P_COMMA = 0.70
P_MERGE_SPACE = 0.05
P_EMOJI = 0.70
P_EMOJI_TO_LAUGH = 0.40
P_PERIOD = 0.80
P_TAIL = 0.20

# T6 구어체 사전 (원형 → 변형). 문장 끝·어절 끝에서만 치환한다.
_COLLOQUIAL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"감사합니다(?=[\s.!~]|$)"), "감사합니당"),
    (re.compile(r"고맙습니다(?=[\s.!~]|$)"), "고맙습니당"),
    (re.compile(r"감사해요(?=[\s.!~]|$)"), "감사해용"),
    (re.compile(r"고마워요(?=[\s.!~]|$)"), "고마워용"),
    (re.compile(r"^네(?=[,!.\s])"), "넵"),
    (re.compile(r"그렇죠(?=[\s.!~]|$)"), "그쵸"),
)
_SMILE = re.compile(r"\s*[😊🙂](?:️)?")
_PROTECTED = re.compile(r"\d|%|\$|[A-Z]{2,}|@|#|https?://|님")
_HANGUL_TOKEN = re.compile(r"^[가-힣]{1,3}$")
_TAIL_STOP = re.compile(r"[ㅋㅎ~…]$|[\U0001F000-\U0001FAFF☀-➿]️?$")


@dataclass
class Result:
    text: str
    ops: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.ops)


def mode() -> str:
    raw = os.environ.get("REPLY_HUMANIZE_MODE", "").strip().lower()
    return raw if raw in MODES else "shadow"


def _rng(key: str, text: str) -> random.Random:
    digest = hashlib.sha256(f"{key}\x1f{text}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _merge_one_space(text: str, rng: random.Random) -> str | None:
    tokens = text.split(" ")
    spots = [
        i for i in range(len(tokens) - 1)
        if _HANGUL_TOKEN.match(tokens[i]) and _HANGUL_TOKEN.match(tokens[i + 1])
        and not _PROTECTED.search(tokens[i] + tokens[i + 1])
    ]
    if not spots:
        return None
    i = rng.choice(spots)
    return " ".join(tokens[:i] + [tokens[i] + tokens[i + 1]] + tokens[i + 2:])


def apply(text: str, key: str, intent: str = "") -> Result:
    """원문에 사람 문체 변형을 적용한다. 순수 함수 — 같은 (text, key)는 같은 결과."""
    original = (text or "").strip()
    if not original:
        return Result(original)
    rng = _rng(key, original)
    out, ops = original, []

    if rng.random() < P_COLLOQUIAL:  # T6
        for pattern, repl in rng.sample(_COLLOQUIAL, len(_COLLOQUIAL)):
            if pattern.search(out):
                out = pattern.sub(repl, out, count=1)
                ops.append("T6")
                break

    if re.search(r"(?<!\d),(?!\d)", out) and rng.random() < P_COMMA:  # T4
        out = re.sub(r"(?<!\d),(?!\d)\s*", " ", out)
        ops.append("T4")

    if rng.random() < P_MERGE_SPACE:  # T5
        merged = _merge_one_space(out, rng)
        if merged is not None:
            out = merged
            ops.append("T5")

    laughed = False
    if _SMILE.search(out) and rng.random() < P_EMOJI:  # T2
        if rng.random() < P_EMOJI_TO_LAUGH:
            out = _SMILE.sub(" " + rng.choice(("ㅎㅎ", "ㅎ")), out, count=1)
            laughed = True
            ops.append("T2:laugh")
        else:
            out = _SMILE.sub("", out)
            ops.append("T2:drop")

    out = re.sub(r"\s{2,}", " ", out).strip()
    if out.endswith(".") and not out.endswith("..") and rng.random() < P_PERIOD:  # T1
        out = out[:-1].rstrip()
        ops.append("T1")

    if not laughed and not _TAIL_STOP.search(out) and rng.random() < P_TAIL:  # T3
        if intent == "LAUGH":
            tail = " ㅋㅋ"
        elif out.endswith("!"):
            tail = rng.choice((" ㅎㅎ", " ㅎ"))
        else:
            tail = rng.choice((" ㅎㅎ", " ㅎ", "~"))
        out = out.rstrip(".") + tail
        ops.append("T3")

    out = re.sub(r"\s{2,}", " ", out).strip()
    if not out or out == original:
        return Result(original)
    return Result(out, ops)
