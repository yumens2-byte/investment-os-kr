"""
engine/video/weekly_pipeline.py
ICG Weekly Digest Shorts — 주간 다이제스트 파이프라인.

설계 근거 (2026-08-29 마스터 확정):
  W1 월~금은 영상을 만들지 않는다 (평일 비용 0, 이미지 트랙만 운영)
  W2 평일 episode_assets 를 주간 단위로 누적 수집 (신규 생성 없음)
  W3 한 주의 메인 스토리를 하나의 흐름으로 정리
  W4 월요일 오전 7시(KST) 주 1회 발행
  W5 총 18초 = 인트로 3s + 6초 × 2컷 + 아웃트로 3s  (B안 확정)

비용: Veo 6s × 2 = $1.20 + 부대비용 ≈ $1.32/주 (월 약 $5.7)
      기존 일일안(회차 $3.78) 대비 65% 절감.

게이트 정책 (2026-08-29 확정):
  일일 트랙은 'scenario_type != NO_BATTLE' 를 요구했으나, 실측 결과 최근
  모든 에피소드가 NO_BATTLE 로 기록되어 있어(이미지 트랙 데이터 이슈, 별건)
  그 기준으로는 매주 skip 된다. 주간 다이제스트는 "그 주에 이야기가 있었나"가
  기준이므로 **에피소드 N건 이상**으로 판정하고, 전투 유무는 각색 소재로만 쓴다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from engine.video.shorts_pipeline import (
    CanonGuardError,
    ConsistencyGuardError,
    ShortsPipelineError,
    ShortsScenario,
    _extract_json,
    build_canon_visual_block,
    enforce_canon_visuals,
)

VERSION = "1.5.0"
logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# ── 주간 다이제스트 규격 (B안) ────────────────────────────────────
WEEKLY_CUT_COUNT = 2
WEEKLY_CUT_SEC = 6
WEEKLY_BOOKEND_SEC = 3
WEEKLY_TOTAL_SEC = WEEKLY_CUT_SEC * WEEKLY_CUT_COUNT + WEEKLY_BOOKEND_SEC * 2  # 18

# 나레이션 상한 = 슬롯 × 5자/초 (한국어 TTS 실측 속도)
WEEKLY_CUT_NARRATION_MAX = WEEKLY_CUT_SEC * 5  # 30자
WEEKLY_BOOKEND_NARRATION_MAX = WEEKLY_BOOKEND_SEC * 5  # 15자

MIN_EPISODES_FOR_DIGEST = 2

_MODEL = "claude-sonnet-4-6"
_MAX_RETRIES = 2
_SYSTEM_PROMPT = (
    "당신은 투자 코믹 유니버스 'ICG'의 주간 다이제스트 각색 작가다. "
    "한 주 동안 누적된 에피소드들의 사실관계를 바꾸지 않고, 하나의 메인 스토리 "
    "흐름으로 압축해 18초 세로형 숏폼 시나리오로 각색한다. "
    "항상 유효한 JSON 하나만 출력한다."
)


class WeeklyPipelineError(ShortsPipelineError):
    """주간 다이제스트 파이프라인 실패."""


def _is_dry_run(dry_run: Optional[bool] = None) -> bool:
    if dry_run is not None:
        return dry_run
    return os.environ.get("DRY_RUN", "true").lower() == "true"


# ────────────────────────────────────────────────────────
# 주간 구간 계산
# ────────────────────────────────────────────────────────


def resolve_week_window(run_date: Optional[date] = None) -> tuple[date, date]:
    """
    실행일 기준 '직전 ISO 주(월~일)' 구간을 계산한다.

    v1.1.0 (2026-09-05 5일 운영 점검 회고):
      run_market 은 KST 새벽(화~일)에 전일 미국장을 처리하므로 episode_date 는
      **화~토** 에 찍힌다 (토요일 = 금요일 미국장). 실측 6주 분포: Tue 2 / Wed 2 /
      Thu 4 / Fri 4 / Sat 4 / Sun 1 / Mon 0. 기존 '월~금' 창은 토요일(금요일장)
      에피소드를 누락시켜 게이트 미달을 유발했다. 지난주 월~일 전체를 잡는다.

    월요일 오전 실행이 정상 경로이며, 화~일 수동 재실행해도 같은 구간이 나온다.
    """
    today = run_date or datetime.now(KST).date()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


_PILOT_TAG_RE = re.compile(r"^P\d{2}$")


def pilot_tag() -> str:
    """
    파일럿 태그 (env WEEKLY_PILOT_TAG, 형식 P01~P99). 없으면 빈 문자열.

    v1.5.0 (2026-09-25 v2 파일럿): 파일럿은 정규 주차와 다른 episode_id 를 써서
    정규 행/발행 이력과 섞이지 않게 한다. 형식이 틀리면 즉시 실패한다.
    """
    tag = os.environ.get("WEEKLY_PILOT_TAG", "").strip().upper()
    if not tag:
        return ""
    if not _PILOT_TAG_RE.match(tag):
        raise WeeklyPipelineError(f"WEEKLY_PILOT_TAG 형식 오류: {tag!r} (P01~P99)")
    return tag


def is_pilot_episode(episode_id: str) -> bool:
    return bool(re.search(r"-P\d{2}$", episode_id or ""))


def build_weekly_episode_id(week_end: date) -> str:
    """주간 식별자 — 일일 트랙('icg-v-')과 접두사로 구분한다. 파일럿은 -Pnn 접미사."""
    iso = week_end.isocalendar()
    suffix = pilot_tag() or "001"
    return f"icg-vw-{iso.year}-W{iso.week:02d}-{suffix}"


# ────────────────────────────────────────────────────────
# DB 로더 (테스트에서 monkeypatch 대상)
# ────────────────────────────────────────────────────────


def _load_week_episodes(start: date, end: date) -> list[dict]:
    from engine.common.supabase_client import icg_table

    rows = (
        icg_table("episode_assets")
        .select("episode_date, event_type, scenario_type, script_json, battle_json, heroes_json")
        .gte("episode_date", start.isoformat())
        .lte("episode_date", end.isoformat())
        .order("episode_date", desc=False)
        .execute()
    )
    return rows.data or []


def _load_video_asset_row(episode_id: str) -> dict | None:
    from engine.common.supabase_client import icg_table

    rows = (
        icg_table("video_assets")
        # v1.3.0/1.6.0 (2026-09-07 run #34117473966 회고): artifact_run_id 가
        # SELECT 에 빠져 있어 항상 None 이었다 → 복원 불가 판정 → 재조립 실패.
        # 소비처가 쓰는 컬럼을 모두 명시한다 (veo_cost_usd 는 승인 캡션 비용 표기용).
        .select(
            "episode_id, episode_date, status, youtube_video_id, "
            "artifact_run_id, veo_cost_usd, release_at"
        )
        .eq("episode_id", episode_id)
        .limit(1)
        .execute()
    )
    return rows.data[0] if rows.data else None


# ────────────────────────────────────────────────────────
# W1 — 주간 게이트
# ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WeeklyGateResult:
    passed: bool
    reason: str
    episode_id: str
    week_start: str
    week_end: str
    episode_count: int = 0
    battle_count: int = 0
    episodes: list[dict] | None = field(default=None, compare=False)

    def to_json(self) -> dict:
        payload = asdict(self)
        payload.pop("episodes", None)
        return payload


def run_weekly_gate(
    run_date: Optional[date] = None,
    *,
    for_generation: bool = True,
) -> WeeklyGateResult:
    """
    주간 다이제스트 생성 여부 판정.

    Args:
        for_generation:
            True  — 신규 생성 진입점(W1/W3/W4·W5)에서 사용. 이미 미디어가 만들어진
                    주차는 차단해 Veo 재과금을 막는다.
            False — 이미 생성된 자산을 소비하는 후속 단계(W6 조립 / W7 알림)에서 사용.
                    v1.2.0 (2026-09-07 run #34101547854 회고): 중복 과금 가드가
                    같은 실행의 후속 스테이지까지 차단해, W4/W5 가 media_generated 를
                    기록한 직후 W6·W7 이 'already_generated' 로 조기 종료됐다.
                    비용은 이미 지출됐는데 조립·발행이 되지 않는 최악의 형태였다.
                    후속 단계는 과금을 유발하지 않으므로 이 가드를 적용하지 않는다.
    """
    start, end = resolve_week_window(run_date)
    episode_id = build_weekly_episode_id(end)
    logger.info(
        "[weekly_pipeline] v%s gate start: window=%s~%s episode_id=%s",
        VERSION,
        start,
        end,
        episode_id,
    )

    existing = _load_video_asset_row(episode_id)
    if existing:
        status = existing.get("status")
        if status == "published" or existing.get("youtube_video_id"):
            return WeeklyGateResult(
                False, "already_published", episode_id, str(start), str(end)
            )
        # 중복 과금 방지: 이미 미디어가 생성된 주차는 '재생성'하지 않는다.
        # 후속 소비 단계(for_generation=False)는 과금이 없으므로 통과시킨다.
        if for_generation and status in {"media_generated", "assembled", "pending_approval"}:
            if os.environ.get("FORCE_REGENERATE", "false").lower() != "true":
                return WeeklyGateResult(
                    False, f"already_generated:{status}", episode_id, str(start), str(end)
                )
            logger.warning(
                "[weekly_pipeline] FORCE_REGENERATE=true — status=%s 재생성 (비용 재발생)",
                status,
            )

    episodes = [e for e in _load_week_episodes(start, end) if e.get("script_json")]
    if len(episodes) < MIN_EPISODES_FOR_DIGEST:
        return WeeklyGateResult(
            False,
            f"insufficient_episodes:{len(episodes)}<{MIN_EPISODES_FOR_DIGEST}",
            episode_id,
            str(start),
            str(end),
            episode_count=len(episodes),
        )

    battles = sum(1 for e in episodes if str(e.get("event_type", "")).upper().startswith("BATTLE"))
    result = WeeklyGateResult(
        True,
        "weekly_digest",
        episode_id,
        str(start),
        str(end),
        episode_count=len(episodes),
        battle_count=battles,
        episodes=episodes,
    )
    logger.info(
        "[weekly_pipeline] gate PASS: episodes=%d battles=%d", len(episodes), battles
    )
    return result


# ────────────────────────────────────────────────────────
# W3 — 주간 각색
# ────────────────────────────────────────────────────────


def extract_weekly_facts(gate: WeeklyGateResult) -> dict:
    """주간 불변 사실 추출 — 등장 캐릭터/전투 결과는 원본에서만 가져온다."""
    episodes = gate.episodes or []
    heroes: list[str] = []
    villains: list[str] = []
    beats: list[dict] = []

    for ep in episodes:
        heroes_json = ep.get("heroes_json") or []
        if isinstance(heroes_json, str):
            heroes_json = json.loads(heroes_json)
        battle = ep.get("battle_json") or {}
        if isinstance(battle, str):
            battle = json.loads(battle)

        for h in heroes_json:
            if h and h not in heroes:
                heroes.append(str(h))
        vid = battle.get("villain_id")
        if vid and vid not in villains:
            villains.append(str(vid))

        beats.append(
            {
                "date": str(ep.get("episode_date")),
                "event_type": ep.get("event_type"),
                "outcome": battle.get("outcome"),
            }
        )

    if not heroes:
        raise WeeklyPipelineError(
            "주간 히어로를 특정할 수 없음 — episode_assets.heroes_json 확인 필요"
        )

    return {
        "episode_id": gate.episode_id,
        "week_start": gate.week_start,
        "week_end": gate.week_end,
        "episode_count": gate.episode_count,
        "battle_count": gate.battle_count,
        # 다이제스트 주인공은 그 주 최다 등장 히어로가 아니라 전체 등장 히어로 목록.
        "hero_ids": heroes,
        "villain_id": villains[0] if villains else "",
        "villain_ids": villains,
        "weekly_beats": beats,
    }


# 이미지 프롬프트에서 금지할 "텍스트 렌더링 요청" 신호어 (2026-09-07 W36 회고)
_TEXT_REQUEST_MARKERS = (
    "text overlay",
    "title card",
    "disclaimer text",
    "text in korean",
    "korean text",
    "caption",
    "subtitle",
    "typography",
    "lettering",
    "watermark",
)

_TEXT_NEGATION_RE = re.compile(
    r"\b(?:no|without|avoid|avoiding|exclude|excluding|omit|omitting|never)\b"
    r"|\bdo\s+not\b|\bdon't\b|\bfree\s+of\b",
    re.IGNORECASE,
)


def _requested_text_markers(prompt: str) -> list[str]:
    """Return text markers that are requested rather than explicitly prohibited.

    Image prompts commonly end with safeguards such as ``no captions or
    watermarks``. Treating those words as positive rendering instructions caused
    all three W3 narrative attempts to fail even though the model complied with
    the no-text rule. Negation is evaluated within the same sentence/clause so a
    later positive instruction (``No logo. Add a caption``) is still rejected.
    """
    hits: list[str] = []
    clauses = re.split(r"[.;\n]+", prompt.lower())
    for clause in clauses:
        for marker in _TEXT_REQUEST_MARKERS:
            marker_match = re.search(rf"\b{re.escape(marker)}s?\b", clause)
            if not marker_match:
                continue
            prefix = clause[: marker_match.start()]
            if not _TEXT_NEGATION_RE.search(prefix):
                hits.append(marker)
    return hits


def enforce_no_text_in_images(scenario: ShortsScenario) -> None:
    """
    북엔드 image_prompt 가 이미지 안에 글자를 넣으라고 요구하면 차단한다.

    근거: 이미지 생성 모델은 한글을 제대로 렌더링하지 못한다. W36 실측에서
    "Legal disclaimer text overlay in Korean" 지시가 그대로 반영되어 아웃트로
    면책 문구가 깨진 글자로 나왔다. 면책·제목은 자막과 유튜브 설명이 담당한다.
    """
    problems: list[str] = []
    for label, bookend in (("intro", scenario.intro), ("outro", scenario.outro)):
        hits = _requested_text_markers(bookend.image_prompt)
        # 한글이 프롬프트에 직접 들어가면 그 자체가 렌더링 요구일 가능성이 높다.
        if any("\uac00" <= ch <= "\ud7a3" for ch in bookend.image_prompt):
            hits.append("한글 문자 포함")
        if hits:
            problems.append(f"{label}: {', '.join(hits)}")

    if problems:
        raise ValueError(
            "image_prompt 에 텍스트 렌더링 요청이 있습니다 (이미지에 글자를 넣으면 깨진다): "
            + " | ".join(problems)
        )


def enforce_weekly_limits(scenario: ShortsScenario) -> None:
    """18초 규격 준수 검증 (컷 수·길이·나레이션 상한)."""
    if len(scenario.cuts) != WEEKLY_CUT_COUNT:
        raise ValueError(f"주간 다이제스트는 {WEEKLY_CUT_COUNT}컷 고정. got={len(scenario.cuts)}")
    for cut in scenario.cuts:
        if cut.duration_sec != WEEKLY_CUT_SEC:
            raise ValueError(f"컷 길이는 {WEEKLY_CUT_SEC}초 고정. got={cut.duration_sec}")
        if len(cut.narration_tts) > WEEKLY_CUT_NARRATION_MAX:
            raise ValueError(
                f"cut{cut.seq} 나레이션 {len(cut.narration_tts)}자 > "
                f"{WEEKLY_CUT_NARRATION_MAX}자 (음성이 다음 장면과 겹침)"
            )
    for label, bookend in (("intro", scenario.intro), ("outro", scenario.outro)):
        if len(bookend.narration_tts) > WEEKLY_BOOKEND_NARRATION_MAX:
            raise ValueError(
                f"{label} 나레이션 {len(bookend.narration_tts)}자 > "
                f"{WEEKLY_BOOKEND_NARRATION_MAX}자 (18초 규격 초과)"
            )


def _build_weekly_prompt(facts: dict, episodes: list[dict]) -> str:
    canon_block = build_canon_visual_block([*facts["hero_ids"], *facts["villain_ids"]])
    schema_hint = {
        "episode_id": facts["episode_id"],
        "episode_date": facts["week_end"],
        "event_type": "WEEKLY_DIGEST",
        "scenario_type": "DIGEST",
        "outcome": "WEEKLY_SUMMARY",
        "hero_ids": facts["hero_ids"],
        "villain_id": facts["villain_id"],
        "intro": {"caption": "...", "narration_tts": "...", "image_prompt": "..."},
        "cuts": [
            {
                "seq": 1,
                "caption": "...",
                "narration_tts": "...",
                "video_prompt": "...",
                "duration_sec": WEEKLY_CUT_SEC,
            }
        ],
        "outro": {"caption": "...", "narration_tts": "...", "image_prompt": "..."},
        "youtube_title": "...",
        "youtube_description": "...",
    }
    scripts = [
        {"date": str(e.get("episode_date")), "script": e.get("script_json")}
        for e in episodes
    ]
    return (
        "아래는 한 주(월~금) 동안 누적된 ICG 에피소드들이다. 이를 하나의 "
        "메인 스토리 흐름으로 정리해 18초 세로형 주간 다이제스트로 각색하라.\n\n"
        "[IMMUTABLE FACTS — 절대 변경 금지]\n"
        f"{json.dumps({k: v for k, v in facts.items() if k != 'weekly_beats'}, ensure_ascii=False, indent=2)}\n\n"
        "[주간 전개 요약]\n"
        f"{json.dumps(facts['weekly_beats'], ensure_ascii=False)}\n\n"
        f"{canon_block}\n\n"
        "[각색 규칙]\n"
        "1. hero_ids / villain_id 는 위 값을 그대로 복사한다. event_type='WEEKLY_DIGEST', "
        "scenario_type='DIGEST', outcome='WEEKLY_SUMMARY' 로 고정 출력한다.\n"
        "2. 한 주의 흐름을 **2컷**으로 압축한다: cut1=주 초반의 위기·긴장, "
        "cut2=주 후반의 전환·결말. 원본에 없는 사건을 창작하지 않는다.\n"
        f"3. cuts 는 정확히 {WEEKLY_CUT_COUNT}개, duration_sec 는 {WEEKLY_CUT_SEC} 고정. "
        "video_prompt 는 영어, cinematic vertical 9:16, Manhwa style.\n"
        "4. **[CANON 캐릭터 외형]의 VISUAL 문구를 반영**하고 종족 필수 단어를 축어로 포함한다. "
        "영상 생성기는 참조 이미지를 받지 못하므로 종족을 쓰지 않으면 다른 캐릭터가 만들어진다.\n"
        f"5. narration_tts 글자 수 상한 절대 준수: cuts 각 {WEEKLY_CUT_NARRATION_MAX}자 이내, "
        f"intro/outro 각 {WEEKLY_BOOKEND_NARRATION_MAX}자 이내 (초과 시 음성이 겹친다). "
        "공백·문장부호 포함으로 센다.\n"
        f"6. outro 는 {WEEKLY_BOOKEND_NARRATION_MAX}자 제약이 크므로 '투자 참고, 권유 아님' 취지를 "
        "최대한 짧게 압축한다. 상세 면책은 youtube_description 에 넣는다.\n"
        "7. youtube_title 은 한 주 요약이 드러나는 한국어 60자 이내(과장 금지). "
        "youtube_description 에 면책 문구를 포함한다.\n"
        "8. **image_prompt 에는 이미지 안에 글자를 넣으라는 지시를 절대 쓰지 마라.** "
        "'title card overlay', 'disclaimer text', 'text in Korean', 자막/캡션/워터마크 "
        "요구는 금지이며, 프롬프트에 한글을 쓰지 마라(영어로만 작성). "
        "이미지 생성기는 한글을 깨진 글자로 렌더링한다. 제목·면책은 자막과 "
        "youtube_description 이 담당하므로 이미지는 순수 비주얼만 묘사한다.\n"
        "9. 출력은 아래 구조의 JSON 하나만. 마크다운/설명/백틱 금지.\n\n"
        "[출력 JSON 구조]\n"
        f"{json.dumps(schema_hint, ensure_ascii=False, indent=2)}\n\n"
        "[원본 주간 스크립트]\n"
        f"{json.dumps(scripts, ensure_ascii=False)}"
    )


def generate_weekly_scenario(
    gate: WeeklyGateResult,
    dry_run: Optional[bool] = None,
) -> tuple[Optional[ShortsScenario], float]:
    """주간 각색 실행. Returns (scenario|None, cost_usd)."""
    if not gate.passed or not gate.episodes:
        raise WeeklyPipelineError(f"gate 미통과 상태에서 각색 호출: reason={gate.reason}")

    facts = extract_weekly_facts(gate)

    if _is_dry_run(dry_run):
        logger.info(
            "[weekly_pipeline] DRY_RUN — Claude 각색 스킵 (episodes=%d heroes=%s)",
            facts["episode_count"],
            facts["hero_ids"],
        )
        return None, 0.0

    from anthropic import Anthropic

    from engine.narrative.claude_client import (
        _build_messages_create_kwargs,
        estimate_cost,
    )

    prompt = _build_weekly_prompt(facts, gate.episodes)
    client = Anthropic()

    last_error: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 2):
        start = time.monotonic()
        create_kwargs = _build_messages_create_kwargs(
            client.messages.create,
            model=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        response = client.messages.create(**create_kwargs)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        raw = "".join(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        )
        cost = estimate_cost(
            response.usage.input_tokens, response.usage.output_tokens, model=_MODEL
        )
        try:
            scenario = ShortsScenario(**json.loads(_extract_json(raw)))
            if sorted(scenario.hero_ids) != sorted(facts["hero_ids"]):
                raise ConsistencyGuardError(
                    f"hero_ids mismatch: expected={facts['hero_ids']} got={scenario.hero_ids}"
                )
            enforce_weekly_limits(scenario)
            enforce_no_text_in_images(scenario)
            enforce_canon_visuals(scenario)
            logger.info(
                "[weekly_pipeline] 각색 완료: %s attempt=%d elapsed=%dms "
                "in=%d out=%d cost=$%.4f total=%ds",
                gate.episode_id,
                attempt,
                elapsed_ms,
                response.usage.input_tokens,
                response.usage.output_tokens,
                cost,
                WEEKLY_TOTAL_SEC,
            )
            return scenario, cost
        except (json.JSONDecodeError, ValueError, ConsistencyGuardError, CanonGuardError) as exc:
            last_error = exc
            logger.warning(
                "[weekly_pipeline] 각색 검증 실패 attempt=%d/%d cost=$%.4f: %s",
                attempt,
                _MAX_RETRIES + 1,
                cost,
                exc,
            )
            prompt = (
                f"{prompt}\n\n[재시도 피드백]\n이전 응답이 검증에 실패했다: {exc}\n"
                "IMMUTABLE FACTS 를 그대로 복사하고 글자 수 상한을 반드시 지켜 다시 생성하라. "
                "image_prompt 검증 실패라면 금지어를 부정문으로도 반복하지 말고, "
                "글자와 관련된 표현을 완전히 제거한 순수 장면 묘사만 출력하라."
            )

    raise WeeklyPipelineError(
        f"주간 각색 {_MAX_RETRIES + 1}회 실패: {gate.episode_id} last={last_error}"
    )


# ────────────────────────────────────────────────────────
# 저장
# ────────────────────────────────────────────────────────


def persist_weekly_gate(gate: WeeklyGateResult) -> str:
    from engine.common.supabase_client import icg_table

    status = "gated" if gate.passed else "skipped"
    existing = _load_video_asset_row(gate.episode_id)
    if existing and existing.get("status") not in (None, "", "skipped", "gated"):
        logger.info(
            "[weekly_pipeline] persist_gate skip — status=%s 유지", existing.get("status")
        )
        return str(existing.get("status"))

    icg_table("video_assets").upsert(
        {
            "episode_id": gate.episode_id,
            "episode_date": gate.week_end,
            "scenario_type": "DIGEST",
            "status": status,
            "gate_result_json": gate.to_json(),
        },
        on_conflict="episode_id",
    ).execute()
    logger.info(
        "[weekly_pipeline] gate 저장: %s status=%s reason=%s",
        gate.episode_id,
        status,
        gate.reason,
    )
    return status


def persist_weekly_scenario(gate: WeeklyGateResult, scenario: ShortsScenario) -> None:
    from engine.common.supabase_client import icg_table

    icg_table("video_assets").upsert(
        {
            "episode_id": gate.episode_id,
            "episode_date": gate.week_end,
            "scenario_type": "DIGEST",
            "status": "scenario_ready",
            "gate_result_json": gate.to_json(),
            "shorts_scenario_json": scenario.model_dump(),
        },
        on_conflict="episode_id",
    ).execute()
    logger.info(
        "[weekly_pipeline] scenario 저장: %s (%d컷 × %ds, 총 %ds)",
        gate.episode_id,
        len(scenario.cuts),
        WEEKLY_CUT_SEC,
        WEEKLY_TOTAL_SEC,
    )


def load_weekly_scenario(episode_id: str) -> Optional[ShortsScenario]:
    from engine.common.supabase_client import icg_table

    rows = (
        icg_table("video_assets")
        .select("shorts_scenario_json")
        .eq("episode_id", episode_id)
        .limit(1)
        .execute()
    )
    if not rows.data:
        return None
    payload = rows.data[0].get("shorts_scenario_json")
    if not payload:
        return None
    if isinstance(payload, str):
        payload = json.loads(payload)
    return ShortsScenario(**payload)
