"""
engine/video/weekly_render.py
ICG Weekly Digest v2 — 단일 패스 최종 렌더 (정지 구간 0초).

v1 조립 경로 대비 변경 (2026-09-25 실측 근거):
  - v1: 스틸클립 인코딩 → concat(copy, 해상도 혼재) → 자막 번인(libx264 기본 CRF23)
        → 오디오 믹스 → 최종 CRF18 재인코딩. 손실 인코딩이 2세대 누적되고
        720p→1080p 업스케일은 기본 스케일러로 암묵 처리됐다. 음량 정규화 없음.
  - v2: 샷 편집·자막·오디오를 filter_complex 하나로 처리하고 최종 1회만 인코딩한다.

편집 규칙:
  - 샷 원본(720x1280) → lanczos 1080x1920 (cover crop)
  - 샷 중간(duration/2)에서 1.12배 펀치인 → 평균 샷 길이 2초
  - 샷 사이 0.1초 fadewhite 전환 (xfade) — 타임라인은 실제 길이 기반으로 산출
  - 자막: 상단 제목 팝(0~1.8s), 샷 자막, 마지막 2.5초 면책 문구
  - 오디오: Veo 효과음(기본 -9dB, WEEKLY_USE_VEO_AUDIO) + 나레이션 → loudnorm -14 LUFS

보안: 모델 생성 텍스트는 ASS 오버라이드 태그({ } \\)를 제거한 뒤 기록한다.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from engine.video.weekly_v2 import WeeklyScenarioV2

VERSION = "1.0.0"
logger = logging.getLogger(__name__)

TARGET_W, TARGET_H, TARGET_FPS = 1080, 1920, 24
PUNCH_SCALE = 1.12
XFADE_SEC = 0.1
XFADE_TRANSITION = "fadewhite"
TITLE_END_SEC = 1.8
DISCLAIMER_SEC = 2.5
DISCLAIMER_TEXT = "투자 참고용 · 투자 권유 아님"
NARRATION_LEAD_SEC = 0.15
TARGET_LUFS = -14
FINAL_CRF = 18
RENDER_TOLERANCE_SEC = 0.12  # 24fps 약 3프레임

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,Noto Sans CJK KR,84,&H0000E5FF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,3,8,60,60,230,1
Style: Caption,Noto Sans CJK KR,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,5,2,2,60,60,330,1
Style: Disclaimer,Noto Sans CJK KR,34,&H00DDDDDD,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


class WeeklyRenderError(Exception):
    """v2 렌더 실패."""


def _is_dry_run(dry_run: Optional[bool] = None) -> bool:
    if dry_run is not None:
        return dry_run
    return os.environ.get("DRY_RUN", "true").lower() == "true"


def _preset() -> str:
    """x264 preset (기본 slow — 품질 우선). 테스트에서만 ultrafast 로 단축한다."""
    value = os.environ.get("WEEKLY_RENDER_PRESET", "slow").strip()
    allowed = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower"}
    return value if value in allowed else "slow"


def _use_veo_audio() -> bool:
    return os.environ.get("WEEKLY_USE_VEO_AUDIO", "true").strip().lower() == "true"


def _veo_audio_gain() -> float:
    try:
        value = float(os.environ.get("WEEKLY_VEO_AUDIO_GAIN", "0.35"))
    except ValueError:
        return 0.35
    return min(max(value, 0.0), 1.0)


# ────────────────────────────────────────────────────────
# 타임라인
# ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShotSlot:
    seq: int
    start_sec: float
    duration_sec: float  # 원본 샷 길이
    visible_end_sec: float  # 다음 샷 전환 시작 시점 (마지막 샷은 총 길이)


def build_timeline(durations: list[float], xfade: float = XFADE_SEC) -> tuple[list[ShotSlot], float]:
    """실제 샷 길이 기반 타임라인. xfade 로 겹치는 만큼 시작 시점을 당긴다."""
    if not durations:
        raise WeeklyRenderError("샷이 없다")
    if any(d <= 2 * xfade for d in durations):
        raise WeeklyRenderError(f"샷 길이가 전환 길이보다 짧다: {durations}")
    slots: list[ShotSlot] = []
    cursor = 0.0
    total = sum(durations) - xfade * (len(durations) - 1)
    for i, d in enumerate(durations):
        start = round(cursor, 3)
        is_last = i == len(durations) - 1
        visible_end = round(total if is_last else start + d - xfade, 3)
        slots.append(ShotSlot(i + 1, start, round(d, 3), visible_end))
        cursor += d - xfade
    return slots, round(total, 3)


# ────────────────────────────────────────────────────────
# 자막 (ASS)
# ────────────────────────────────────────────────────────


def sanitize_ass_text(text: str) -> str:
    """ASS 오버라이드/개행 제어 문자 제거 (모델 생성 텍스트 인젝션 방지)."""
    cleaned = re.sub(r"[{}\\]", "", str(text or ""))
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _t(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_v2_ass(scenario: WeeklyScenarioV2, slots: list[ShotSlot], total: float, path: Path) -> Path:
    lines: list[str] = []
    title = sanitize_ass_text(scenario.hook_title)
    # 제목 팝: 60%→100% 스케일 180ms, 페이드인 80ms / 아웃 150ms (고정 태그, 사용자 텍스트 아님)
    lines.append(
        f"Dialogue: 1,{_t(0.0)},{_t(min(TITLE_END_SEC, total))},Title,,0,0,0,,"
        "{\\fad(80,150)\\fscx60\\fscy60\\t(0,180,\\fscx100\\fscy100)}" + title
    )
    disclaimer_start = max(total - DISCLAIMER_SEC, 0.0)
    for shot, slot in zip(scenario.shots, slots):
        start = slot.start_sec + NARRATION_LEAD_SEC
        end = slot.visible_end_sec - 0.05
        if slot.seq == len(slots):
            end = min(end, disclaimer_start + 0.8)
        if end <= start:
            continue
        lines.append(
            f"Dialogue: 0,{_t(start)},{_t(end)},Caption,,0,0,0,,"
            "{\\fad(60,60)}" + sanitize_ass_text(shot.caption)
        )
    lines.append(
        f"Dialogue: 2,{_t(disclaimer_start)},{_t(total)},Disclaimer,,0,0,0,,{DISCLAIMER_TEXT}"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ASS_HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    return path


# ────────────────────────────────────────────────────────
# ffprobe helpers
# ────────────────────────────────────────────────────────


def _ffprobe_value(path: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["ffprobe", "-v", "error", *args, "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WeeklyRenderError(f"ffprobe 실패: {path} — {result.stderr[-200:]}")
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def probe_duration(path: Path) -> float:
    """
    영상 트랙 길이(초). 타임라인은 반드시 영상 트랙 기준이어야 한다.

    컨테이너 길이(format=duration)는 오디오가 더 길면 오디오 길이가 된다. 그 값으로
    xfade offset 을 잡으면 이전 클립 영상 끝을 넘어 ffmpeg 가 이후 영상을 조용히 버린다
    (2026-09-25 리뷰 재현: 영상 4.0s/오디오 4.06s → 최종 영상 4.08s). 영상 트랙 값이
    없을 때만 컨테이너 길이로 대체한다.
    """
    value = _ffprobe_value(path, ["-select_streams", "v:0", "-show_entries", "stream=duration"])
    if not value or value == "N/A":
        value = _ffprobe_value(path, ["-show_entries", "format=duration"])
    try:
        return float(value)
    except ValueError as exc:
        raise WeeklyRenderError(f"길이 판독 불가: {path} ({value!r})") from exc


def has_audio(path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


# ────────────────────────────────────────────────────────
# 나레이션
# ────────────────────────────────────────────────────────


def generate_v2_narrations(
    scenario: WeeklyScenarioV2,
    slots: list[ShotSlot],
    out_dir: Path,
    dry_run: Optional[bool] = None,
) -> list[tuple[float, Path]]:
    """샷별 TTS → 슬롯 길이 보정 → [(start_sec, wav)]. TTS 실패 샷은 무음(자막 유지)."""
    if _is_dry_run(dry_run):
        logger.info("[weekly_render] DRY_RUN — TTS 스킵")
        return []

    from engine.video.audio_overlay import generate_tts
    from engine.video.shorts_media import fit_narration_to_slot

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    segments: list[tuple[float, Path]] = []
    for shot, slot in zip(scenario.shots, slots):
        wav = out_dir / f"narr_shot{shot.seq}.wav"
        start = slot.start_sec + NARRATION_LEAD_SEC
        slot_sec = slot.visible_end_sec - start
        try:
            generate_tts(text=shot.narration_tts, output_path=str(wav))
            fitted = fit_narration_to_slot(wav, slot_sec=slot_sec, output_path=out_dir / f"narr_shot{shot.seq}_fit.wav")
        except Exception as exc:
            logger.warning("[weekly_render] shot%d 나레이션 실패 — 무음 진행: %s", shot.seq, exc)
            continue
        segments.append((start, Path(fitted)))
    return segments


# ────────────────────────────────────────────────────────
# filter_complex 구성
# ────────────────────────────────────────────────────────


def _even(value: float) -> int:
    n = int(round(value))
    return n if n % 2 == 0 else n + 1


def build_filter_complex(
    slots: list[ShotSlot],
    total: float,
    ass_path: Path,
    narration_starts: list[float],
    use_shot_audio: bool,
    veo_gain: float,
) -> str:
    n = len(slots)
    narration_count = len(narration_starts)
    pw, ph = _even(TARGET_W * PUNCH_SCALE), _even(TARGET_H * PUNCH_SCALE)
    parts: list[str] = []

    for i, slot in enumerate(slots):
        half = round(slot.duration_sec / 2, 3)
        parts.append(
            f"[{i}:v]fps={TARGET_FPS},setsar=1,split=2[s{i}a][s{i}b];"
            f"[s{i}a]trim=0:{half},setpts=PTS-STARTPTS,"
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={TARGET_W}:{TARGET_H},setsar=1[p{i}a];"
            f"[s{i}b]trim=start={half},setpts=PTS-STARTPTS,"
            f"scale={pw}:{ph}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={TARGET_W}:{TARGET_H},setsar=1[p{i}b];"
            f"[p{i}a][p{i}b]concat=n=2:v=1:a=0,format=yuv420p,settb=AVTB[v{i}]"
        )

    prev = "v0"
    for i in range(1, n):
        parts.append(
            f"[{prev}][v{i}]xfade=transition={XFADE_TRANSITION}:duration={XFADE_SEC}:"
            f"offset={slots[i].start_sec}[x{i}]"
        )
        prev = f"x{i}"
    ass_arg = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    parts.append(f"[{prev}]ass='{ass_arg}'[vout]")

    # 오디오 베드: 샷 오디오(Veo 효과음) 또는 무음
    if use_shot_audio:
        for i, slot in enumerate(slots):
            parts.append(
                f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,"
                f"apad,atrim=0:{slot.duration_sec},asetpts=PTS-STARTPTS[a{i}]"
            )
        prev_a = "a0"
        for i in range(1, n):
            parts.append(f"[{prev_a}][a{i}]acrossfade=d={XFADE_SEC}[ax{i}]")
            prev_a = f"ax{i}"
        parts.append(f"[{prev_a}]volume={veo_gain}[bed]")
    else:
        silent_idx = n + narration_count
        parts.append(f"[{silent_idx}:a]atrim=0:{total},asetpts=PTS-STARTPTS[bed]")

    mix_inputs = ["[bed]"]
    for k, start in enumerate(narration_starts):
        idx = n + k
        ms = int(round(start * 1000))
        mix_inputs.append(f"[n{k}]")
        parts.append(
            f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[n{k}]"
        )
    parts.append(
        "".join(mix_inputs)
        + f"amix=inputs={len(mix_inputs)}:duration=first:normalize=0,"
        f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11,aresample=48000[aout]"
    )
    return ";".join(parts)


# ────────────────────────────────────────────────────────
# 렌더
# ────────────────────────────────────────────────────────


def render_weekly_v2(
    scenario: WeeklyScenarioV2,
    shot_paths: list[Path],
    out_dir: Path,
    dry_run: Optional[bool] = None,
    narrations: Optional[list[tuple[float, Path]]] = None,
) -> tuple[Path, dict]:
    """
    최종 렌더 (1회 인코딩). Returns (final_path, report).

    narrations 를 넘기면 TTS 를 생성하지 않는다 (테스트/재사용용).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(shot_paths) != len(scenario.shots):
        raise WeeklyRenderError(f"샷 파일 수 불일치: {len(shot_paths)} / {len(scenario.shots)}")
    missing = [str(p) for p in shot_paths if not Path(p).exists()]
    if missing:
        raise WeeklyRenderError(f"샷 파일 누락: {missing}")

    durations = [probe_duration(Path(p)) for p in shot_paths]
    slots, total = build_timeline(durations)
    ass_path = build_v2_ass(scenario, slots, total, out_dir / "subs_v2.ass")

    if narrations is None:
        narrations = generate_v2_narrations(scenario, slots, out_dir / "tts", dry_run=dry_run)

    use_shot_audio = _use_veo_audio() and all(has_audio(Path(p)) for p in shot_paths)
    if _use_veo_audio() and not use_shot_audio:
        logger.warning("[weekly_render] 일부 샷에 오디오 없음 — Veo 효과음 미사용(무음 베드)")

    fc = build_filter_complex(
        slots, total, ass_path, [s for s, _ in narrations], use_shot_audio, _veo_audio_gain()
    )

    cmd: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in shot_paths:
        cmd += ["-i", str(p)]
    for _, wav in narrations:
        cmd += ["-i", str(wav)]
    if not use_shot_audio:
        cmd += ["-f", "lavfi", "-t", f"{total}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    final_path = out_dir / "final_shorts.mp4"
    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", _preset(), "-crf", str(FINAL_CRF),
        "-pix_fmt", "yuv420p", "-r", str(TARGET_FPS),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-t", f"{total}",
        "-movflags", "+faststart",
        str(final_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise WeeklyRenderError(f"ffmpeg 렌더 실패: {result.stderr[-800:]}")

    # 사후 검증: 영상/오디오 트랙 길이가 타임라인과 일치해야 한다 (조용한 절단 차단)
    video_sec = probe_duration(final_path)
    audio_raw = _ffprobe_value(final_path, ["-select_streams", "a:0", "-show_entries", "stream=duration"])
    audio_sec = float(audio_raw) if audio_raw and audio_raw != "N/A" else 0.0
    if abs(video_sec - total) > RENDER_TOLERANCE_SEC or abs(audio_sec - total) > RENDER_TOLERANCE_SEC:
        raise WeeklyRenderError(
            f"렌더 길이 불일치: video={video_sec:.3f}s audio={audio_sec:.3f}s expected={total:.3f}s "
            f"(허용 ±{RENDER_TOLERANCE_SEC}s) — 발행 차단"
        )

    report = {
        "total_sec": total,
        "shot_durations": durations,
        "slots": [s.__dict__ for s in slots],
        "narrations": len(narrations),
        "veo_audio": use_shot_audio,
        "encode_passes": 1,
        "video_sec": round(video_sec, 3),
        "audio_sec": round(audio_sec, 3),
    }
    logger.info(
        "[weekly_render] v%s 렌더 완료: %s (총 %.2fs, 샷 %d, 나레이션 %d, veo_audio=%s)",
        VERSION, final_path, total, len(slots), len(narrations), use_shot_audio,
    )
    return final_path, report
