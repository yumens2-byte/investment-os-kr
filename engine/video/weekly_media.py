"""
engine/video/weekly_media.py
ICG Weekly Digest v2 — 키프레임(REF 기반) → Veo I2V 4샷 + Motion QA.

재사용 (시그니처 실측 — 추측 사용 금지 원칙):
  - engine/image/gemini_client.generate_panel(panel_idx, prompt_text, ref_paths,
      output_dir, log_path, aspect_ratio) -> (Path|None, cost_usd)
  - engine/image/ref_loader.get_refs_for_panel(char_ids) -> list[Path]
  - engine/video/veo_client.VeoClient.generate_image_to_video(prompt, start_frame_path,
      output_path, duration_sec, resolution, aspect_ratio, negative_prompt) -> dict
  - engine/video/budget_checker.check_before_generation(estimated_cost_usd) -> dict
  - engine/video/shorts_media.NO_TEXT_RULE / _write_dummy_png / _write_dummy_mp4

Motion QA (2026-09-25 설계):
  - freezedetect 로 정지 비율, scene score 평균으로 움직임 점수를 샷마다 측정한다.
  - 정지 비율 > WEEKLY_FREEZE_MAX(기본 0.25) 이면 샷당 WEEKLY_MOTION_REGEN_MAX(기본 1)회
    재생성하고, 두 결과 중 정지 비율이 낮은 쪽을 채택한다.
  - 움직임 점수 기준값은 파일럿 실측 전이므로 기록만 한다 (판정 미사용).

비용 원장 (P0 반영):
  - 성공한 생성 호출마다 비용을 누적한다. 중간 실패로 중단돼도 이미 지출된 비용을
    video_assets.veo_cost_usd 에 누적 기록한다 (덮어쓰기 금지).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.video.weekly_v2 import (
    MOTION_SUFFIX,
    NEGATIVE_PROMPT,
    SHOT_COUNT,
    SHOT_SEC,
    WeeklyScenarioV2,
)

VERSION = "1.0.0"
logger = logging.getLogger(__name__)

KEYFRAME_IDX_BASE = 100  # generate_panel 파일명 P{idx}.png — 본편(1~8)/북엔드(91,92)와 분리
KEYFRAME_ASPECT = "9:16"
VEO_RESOLUTION = "720p"
MAX_REFS_PER_KEYFRAME = 3
# 이미지 단가: DB 실측(2026-09-07~21 북엔드 2장 $0.0779) 기준 장당 ≈ $0.039
IMAGE_UNIT_COST_USD = 0.039
# 각색(Claude) + TTS 부대비용 추정 (예산 사전검사용)
SIDE_COST_USD = 0.10

FREEZE_NOISE = "-60dB"
FREEZE_MIN_SEC = 0.5

_MANIFEST_COL = "media_manifest_json"


class WeeklyMediaError(Exception):
    """v2 미디어 생성 실패."""


def _is_dry_run(dry_run: Optional[bool] = None) -> bool:
    if dry_run is not None:
        return dry_run
    return os.environ.get("DRY_RUN", "true").lower() == "true"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        logger.warning("[weekly_media] invalid %s — 기본 %s 사용", name, default)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        logger.warning("[weekly_media] invalid %s — 기본 %s 사용", name, default)
        return default


# ────────────────────────────────────────────────────────
# 비용 추정 / 사전검사
# ────────────────────────────────────────────────────────


def estimate_shot_cost() -> float:
    from engine.video.veo_client import unit_price_per_sec

    return round(unit_price_per_sec() * SHOT_SEC, 4)


def estimate_v2_cost(scenario: WeeklyScenarioV2) -> float:
    """회차 예상비용 = Veo(샷 초수 합 × 단가) + 키프레임 + 부대비용."""
    from engine.video.veo_client import unit_price_per_sec

    veo = sum(s.duration_sec for s in scenario.shots) * unit_price_per_sec()
    images = len(scenario.shots) * IMAGE_UNIT_COST_USD
    return round(veo + images + SIDE_COST_USD, 4)


def preflight_v2(scenario: WeeklyScenarioV2, dry_run: Optional[bool] = None) -> dict:
    """유료 호출 전 사전검사 — 예산(fail-closed)·ffmpeg·CJK 폰트·YouTube 자격(경고)."""
    estimated = estimate_v2_cost(scenario)
    report: dict = {"estimated_cost_usd": estimated}
    if _is_dry_run(dry_run):
        logger.info("[weekly_media] DRY_RUN — preflight 스킵 (예상 $%.4f)", estimated)
        report["skipped"] = True
        return report

    from engine.video.budget_checker import check_before_generation

    report["budget"] = check_before_generation(estimated_cost_usd=estimated)

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise WeeklyMediaError(f"preflight 실패: {tool} 없음")
    if shutil.which("fc-list"):
        fonts = subprocess.run(["fc-list"], capture_output=True, text=True).stdout.lower()
        if "cjk" not in fonts and "nanum" not in fonts:
            raise WeeklyMediaError("preflight 실패: CJK 폰트 미설치 — 자막이 깨진다")
    try:
        from engine.publish.youtube_shorts_publisher import verify_youtube_credentials

        auth = verify_youtube_credentials()
        report["youtube_auth"] = auth["valid"]
        if not auth["valid"]:
            logger.warning("[weekly_media] YouTube 자격증명 무효 — 발행 전 재발급 필요: %s", auth["detail"])
    except Exception as exc:
        report["youtube_auth"] = None
        logger.warning("[weekly_media] YouTube 자격증명 검사 생략: %s", exc)

    logger.info("[weekly_media] v%s preflight 통과 — 예상 $%.4f", VERSION, estimated)
    return report


# ────────────────────────────────────────────────────────
# Motion QA
# ────────────────────────────────────────────────────────


def _probe_duration(path: Path) -> float:
    """영상 트랙 길이 (weekly_render.probe_duration 단일 소스 — 컨테이너 길이 사용 금지)."""
    from engine.video.weekly_render import WeeklyRenderError, probe_duration

    try:
        return probe_duration(path)
    except WeeklyRenderError as exc:
        raise WeeklyMediaError(str(exc)) from exc


def parse_freeze_ratio(stderr: str, duration: float) -> float:
    """freezedetect 로그 → 정지 구간 비율. 끝나지 않은 freeze_start 는 영상 끝까지로 본다."""
    starts = [float(x) for x in re.findall(r"freeze_start:\s*([0-9.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"freeze_end:\s*([0-9.]+)", stderr)]
    if duration <= 0:
        return 0.0
    frozen = 0.0
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else duration
        frozen += max(0.0, min(end, duration) - start)
    return round(min(frozen / duration, 1.0), 4)


def measure_motion(path: Path) -> dict:
    """샷 1개의 정지 비율과 평균 scene score(움직임 점수)."""
    duration = _probe_duration(path)
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-vf",
            f"freezedetect=n={FREEZE_NOISE}:d={FREEZE_MIN_SEC},"
            "select='gte(scene\\,0)',metadata=print:key=lavfi.scene_score:file=-",
            "-an", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WeeklyMediaError(f"motion 측정 실패: {path} — {result.stderr[-300:]}")
    scores = [float(x) for x in re.findall(r"lavfi\.scene_score=([0-9.]+)", result.stdout)]
    return {
        "duration_sec": round(duration, 3),
        "freeze_ratio": parse_freeze_ratio(result.stderr, duration),
        "motion_score": round(sum(scores) / len(scores), 5) if scores else 0.0,
        "frames": len(scores),
    }


# ────────────────────────────────────────────────────────
# 결과 컨테이너
# ────────────────────────────────────────────────────────


@dataclass
class V2MediaResult:
    keyframes: list[Path] = field(default_factory=list)
    shots: list[Path] = field(default_factory=list)
    motion: list[dict] = field(default_factory=list)
    image_cost_usd: float = 0.0
    veo_cost_usd: float = 0.0
    regenerations: int = 0
    failed_attempts: int = 0  # API 실패 호출 수 (과금 여부 미확인 — 감사용 기록, 원장 미가산)
    veo_results: list[dict] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return round(self.image_cost_usd + self.veo_cost_usd, 4)

    def manifest(self) -> dict:
        return {
            "schema": "weekly_v2_media",
            "version": VERSION,
            "keyframes": [str(p) for p in self.keyframes],
            "shots": [str(p) for p in self.shots],
            "motion": self.motion,
            "image_cost_usd": self.image_cost_usd,
            "veo_cost_usd": self.veo_cost_usd,
            "regenerations": self.regenerations,
            "failed_attempts": self.failed_attempts,
        }


def expected_paths(out_dir: Path) -> tuple[list[Path], list[Path]]:
    """조립/복원 단계가 참조하는 파일 경로 (생성 규칙의 단일 소스)."""
    out_dir = Path(out_dir)
    keyframes = [out_dir / "keyframes" / f"P{KEYFRAME_IDX_BASE + i}.png" for i in range(1, SHOT_COUNT + 1)]
    shots = [out_dir / "shots" / f"shot{i}.mp4" for i in range(1, SHOT_COUNT + 1)]
    return keyframes, shots


# ────────────────────────────────────────────────────────
# 키프레임
# ────────────────────────────────────────────────────────


def generate_keyframes(
    scenario: WeeklyScenarioV2,
    out_dir: Path,
    result: V2MediaResult,
    dry_run: Optional[bool] = None,
) -> V2MediaResult:
    """샷별 첫 프레임 이미지 (샷 캐스트 REF 이미지 주입, 9:16)."""
    from engine.video.shorts_media import NO_TEXT_RULE, _write_dummy_png

    kf_dir = Path(out_dir) / "keyframes"
    kf_dir.mkdir(parents=True, exist_ok=True)

    if _is_dry_run(dry_run):
        for shot in scenario.shots:
            path = kf_dir / f"P{KEYFRAME_IDX_BASE + shot.seq}.png"
            _write_dummy_png(path)
            result.keyframes.append(path)
        logger.info("[weekly_media] DRY_RUN — 키프레임 더미 %d장 (비용 0)", len(scenario.shots))
        return result

    from engine.image.gemini_client import generate_panel
    from engine.image.ref_loader import get_refs_for_panel

    log_path = kf_dir / "gemini_run.log"
    for shot in scenario.shots:
        try:
            refs = get_refs_for_panel(list(shot.cast))[:MAX_REFS_PER_KEYFRAME]
        except Exception as exc:
            logger.warning("[weekly_media] shot%d REF 로드 실패 (REF 없이 진행): %s", shot.seq, exc)
            refs = []
        path, cost = generate_panel(
            panel_idx=KEYFRAME_IDX_BASE + shot.seq,
            prompt_text=f"{NO_TEXT_RULE}\n\n{shot.keyframe_prompt}",
            ref_paths=refs,
            output_dir=kf_dir,
            log_path=log_path,
            aspect_ratio=KEYFRAME_ASPECT,
        )
        result.image_cost_usd = round(result.image_cost_usd + float(cost or 0.0), 4)
        if path is None:
            raise WeeklyMediaError(f"shot{shot.seq} 키프레임 생성 실패 (refs={len(refs)})")
        result.keyframes.append(Path(path))
        logger.info("[weekly_media] shot%d 키프레임: %s refs=%d cost=$%.4f", shot.seq, path, len(refs), cost)
    return result


# ────────────────────────────────────────────────────────
# Veo I2V + Motion QA
# ────────────────────────────────────────────────────────


def _generate_one_shot(
    client, shot, keyframe: Path, output_path: Path, max_retry: int, result: "V2MediaResult"
) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, max_retry + 2):
        if attempt > 1:
            result.failed_attempts += 1
        try:
            return client.generate_image_to_video(
                prompt=f"{shot.motion_prompt}\n\n{MOTION_SUFFIX}",
                start_frame_path=str(keyframe),
                output_path=str(output_path),
                duration_sec=shot.duration_sec,
                resolution=VEO_RESOLUTION,
                aspect_ratio=KEYFRAME_ASPECT,
                negative_prompt=NEGATIVE_PROMPT,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[weekly_media] shot%d I2V 실패 attempt=%d/%d: %s",
                shot.seq, attempt, max_retry + 1, exc,
            )
    result.failed_attempts += 1
    raise WeeklyMediaError(f"shot{shot.seq} I2V 최종 실패 (부분 발행 금지): {last_exc}") from last_exc


def generate_shots(
    scenario: WeeklyScenarioV2,
    out_dir: Path,
    result: V2MediaResult,
    dry_run: Optional[bool] = None,
    max_retry_per_shot: int = 2,
) -> V2MediaResult:
    """키프레임 → Veo I2V 4샷 + Motion QA (정지 비율 초과 시 샷당 제한 횟수 재생성)."""
    from engine.video.shorts_media import _write_dummy_mp4

    shot_dir = Path(out_dir) / "shots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    if len(result.keyframes) != len(scenario.shots):
        raise WeeklyMediaError(
            f"키프레임 수 불일치: {len(result.keyframes)} / 샷 {len(scenario.shots)}"
        )

    if _is_dry_run(dry_run):
        for shot in scenario.shots:
            path = shot_dir / f"shot{shot.seq}.mp4"
            _write_dummy_mp4(path)
            result.shots.append(path)
        logger.info("[weekly_media] DRY_RUN — 샷 더미 %d개 (비용 0)", len(scenario.shots))
        return result

    from engine.video.budget_checker import check_before_generation
    from engine.video.veo_client import VeoClient

    freeze_max = _env_float("WEEKLY_FREEZE_MAX", 0.25)
    regen_max = _env_int("WEEKLY_MOTION_REGEN_MAX", 1)
    client = VeoClient()

    for shot, keyframe in zip(scenario.shots, result.keyframes):
        path = shot_dir / f"shot{shot.seq}.mp4"
        res = _generate_one_shot(client, shot, keyframe, path, max_retry_per_shot, result)
        result.veo_results.append(res)
        result.veo_cost_usd = round(result.veo_cost_usd + float(res.get("cost_usd") or 0.0), 4)
        metrics = measure_motion(path)
        attempts = 0
        while metrics["freeze_ratio"] > freeze_max and attempts < regen_max:
            attempts += 1
            # 이번 실행 지출분은 아직 원장에 없으므로 함께 더해 예산을 재확인한다.
            check_before_generation(estimated_cost_usd=result.total_cost_usd + estimate_shot_cost())
            logger.warning(
                "[weekly_media] shot%d 정지 비율 %.2f > %.2f — 재생성 %d/%d",
                shot.seq, metrics["freeze_ratio"], freeze_max, attempts, regen_max,
            )
            alt = path.with_name(f"shot{shot.seq}_regen{attempts}.mp4")
            res2 = _generate_one_shot(client, shot, keyframe, alt, max_retry_per_shot, result)
            result.veo_results.append(res2)
            result.veo_cost_usd = round(result.veo_cost_usd + float(res2.get("cost_usd") or 0.0), 4)
            result.regenerations += 1
            metrics2 = measure_motion(alt)
            if metrics2["freeze_ratio"] < metrics["freeze_ratio"]:
                alt.replace(path)
                metrics = metrics2
            else:
                alt.unlink(missing_ok=True)
        metrics.update({"seq": shot.seq, "regenerated": attempts, "camera_move": shot.camera_move})
        if metrics["freeze_ratio"] > freeze_max:
            logger.warning("[weekly_media] shot%d 정지 비율 %.2f 기준 초과 상태로 채택", shot.seq, metrics["freeze_ratio"])
        result.motion.append(metrics)
        result.shots.append(path)
        logger.info(
            "[weekly_media] shot%d 완료: freeze=%.2f motion=%.4f cost 누계=$%.4f",
            shot.seq, metrics["freeze_ratio"], metrics["motion_score"], result.total_cost_usd,
        )
    return result


# ────────────────────────────────────────────────────────
# 원장 / 저장
# ────────────────────────────────────────────────────────


def _is_missing_manifest_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return _MANIFEST_COL in message and ("column" in message or "schema" in message)


def _update_row(episode_id: str, payload: dict) -> None:
    """video_assets 업데이트 — media_manifest_json 컬럼 미적용 환경이면 해당 키만 빼고 재시도."""
    from engine.common.supabase_client import icg_table

    try:
        icg_table("video_assets").update(payload).eq("episode_id", episode_id).execute()
    except Exception as exc:
        if _MANIFEST_COL in payload and _is_missing_manifest_error(exc):
            logger.warning("[weekly_media] %s 컬럼 없음 — manifest 제외 후 재시도", _MANIFEST_COL)
            fallback = {k: v for k, v in payload.items() if k != _MANIFEST_COL}
            icg_table("video_assets").update(fallback).eq("episode_id", episode_id).execute()
            return
        raise


def accumulated_cost(episode_id: str, add_usd: float) -> float:
    """기존 원장값 + 이번 지출 (덮어쓰기 금지)."""
    from engine.video.weekly_pipeline import _load_video_asset_row

    row = _load_video_asset_row(episode_id) or {}
    prior = float(row.get("veo_cost_usd") or 0.0)
    return round(prior + float(add_usd or 0.0), 4)


def record_spend(episode_id: str, add_usd: float, note: str, manifest: Optional[dict] = None) -> float:
    """상태 변경 없이 지출만 원장에 누적한다 (중간 실패 시 호출)."""
    if add_usd <= 0:
        return 0.0
    total = accumulated_cost(episode_id, add_usd)
    payload: dict = {"veo_cost_usd": total}
    if manifest is not None:
        payload[_MANIFEST_COL] = {**manifest, "note": note}
    _update_row(episode_id, payload)
    logger.info("[weekly_media] 지출 누적 기록(%s): +$%.4f → $%.4f", note, add_usd, total)
    return total


def persist_v2_media(episode_id: str, media: V2MediaResult) -> None:
    """미디어 완료 → status='media_generated' + manifest + 누적 비용 + artifact_run_id."""
    uris = [str(p) for p in media.shots]
    payload = {
        "status": "media_generated",
        "cut1_video_uri": uris[0] if len(uris) > 0 else None,
        "cut2_video_uri": uris[1] if len(uris) > 1 else None,
        "cut3_video_uri": uris[2] if len(uris) > 2 else None,
        "intro_image_uri": None,
        "outro_image_uri": None,
        "veo_cost_usd": accumulated_cost(episode_id, media.total_cost_usd),
        "artifact_run_id": os.environ.get("GITHUB_RUN_ID"),
        _MANIFEST_COL: media.manifest(),
    }
    _update_row(episode_id, payload)
    logger.info(
        "[weekly_media] persist v2 media: %s cost(+%.4f) regen=%d",
        episode_id, media.total_cost_usd, media.regenerations,
    )
