"""
Veo 3.1 Fast API wrapper.

Model        : veo-3.1-fast-generate-preview
Resolution   : 720p (9:16 vertical) — B-2 conservative option
Duration     : 4/6/8 seconds per cut
Pricing:
  - 코드 기본값 $0.15/s (2026-04-19 기준 기록값).
  - v1.4.0: 단가는 env VEO_UNIT_PRICE_USD 로 주입한다. 청구 단가가 확인되면
    워크플로 Variable 만 바꾸면 비용 원장·예산 가드가 함께 정정된다.
  - Audio-off 할인은 Gemini API 에서 적용되지 않는다 (오디오 항상 생성).

Why `generate_audio` parameter removed in v1.3.1:
  - `veo-3.1-fast-generate-preview` Gemini API returned 400 on 2026-04-20 run #6.
  - Veo default (audio ON) applies.

Why `person_generation` and `number_of_videos` removed in v1.3.2 (2026-04-20):
  - Run #8 returned 400 INVALID_ARGUMENT:
      "allow_adult for personGeneration is currently not supported."
  - Final minimal config: aspect_ratio + resolution + duration_seconds + negative_prompt.

v1.4.0 (2026-09-25, Weekly Digest v2):
  - generate_image_to_video 실장 — 첫 프레임 이미지(키프레임)를 입력으로 받는 I2V.
    SDK 시그니처 실측(google-genai 2.25.0):
      Models.generate_videos(*, model, prompt, image: types.Image, config)
      types.Image(image_bytes=..., mime_type=...)
  - T2V/I2V 공용 폴링·다운로드 로직을 _run_operation 으로 분리 (T2V 동작 불변).
  - 단가 env 주입 (unit_price_per_sec).

SynthID      : Auto-watermarked (invisible)

IMPORTANT: Veo retains generated videos on Google servers for 2 days only.
           Download immediately after generation.

Reference:
  - https://ai.google.dev/gemini-api/docs/veo
  - https://github.com/googleapis/python-genai (official SDK)
"""
import logging
import os
import time
from pathlib import Path
from typing import Optional

VERSION = "1.4.0"
MODEL = "veo-3.1-fast-generate-preview"
DEFAULT_RESOLUTION = "720p"
DEFAULT_ASPECT_RATIO = "9:16"
DEFAULT_DURATION_SEC = 8
# NOTE: DEFAULT_PERSON_GENERATION kept for backward-compat of public API only.
DEFAULT_PERSON_GENERATION = "allow_adult"

# 기록 기본 단가 (USD/sec). 실제 적용값은 unit_price_per_sec() — env 우선.
UNIT_PRICE = 0.15

# Polling configuration
POLL_INTERVAL_SEC = 15
POLL_TIMEOUT_SEC = 600  # 10 minutes — Veo typically takes 30~120s

_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

logger = logging.getLogger(__name__)


class VeoGenerationError(RuntimeError):
    """Raised when Veo video generation fails (API error, timeout, policy violation)."""


class VeoTimeoutError(VeoGenerationError):
    """Raised when Veo operation polling exceeds POLL_TIMEOUT_SEC."""


def unit_price_per_sec() -> float:
    """
    Veo 초당 단가. env VEO_UNIT_PRICE_USD 우선, 없거나 잘못된 값이면 UNIT_PRICE.

    원장(video_assets.veo_cost_usd)과 예산 가드가 같은 값을 쓰도록 단일 소스로 둔다.
    """
    raw = os.environ.get("VEO_UNIT_PRICE_USD", "").strip()
    if not raw:
        return UNIT_PRICE
    try:
        value = float(raw)
    except ValueError:
        logger.warning("[VeoClient] invalid VEO_UNIT_PRICE_USD=%r — 기본 $%.2f 사용", raw, UNIT_PRICE)
        return UNIT_PRICE
    if value <= 0:
        logger.warning("[VeoClient] VEO_UNIT_PRICE_USD<=0 (%r) — 기본 $%.2f 사용", raw, UNIT_PRICE)
        return UNIT_PRICE
    return value


class VeoClient:
    """Thin wrapper around google-genai's generate_videos operation."""

    def __init__(self):
        try:
            from google import genai
        except ImportError as e:
            raise RuntimeError(
                "google-genai package not installed. Run: pip install google-genai"
            ) from e

        api_key = os.environ.get("GEMINI_API_SUB_PAY_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_SUB_PAY_KEY env variable not set")

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        logger.info(f"[VeoClient] v{VERSION} initialized (model={MODEL})")

    # ────────────────────────────────────────────────────────
    # 공용 실행부 (T2V / I2V)
    # ────────────────────────────────────────────────────────

    def _run_operation(
        self,
        *,
        mode: str,
        prompt: str,
        output_path: str,
        duration_sec: int,
        resolution: str,
        aspect_ratio: str,
        negative_prompt: Optional[str],
        image=None,
    ) -> dict:
        from google.genai import types

        config_kwargs = {
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "duration_seconds": duration_sec,
        }
        if negative_prompt:
            config_kwargs["negative_prompt"] = negative_prompt

        request_kwargs = {
            "model": MODEL,
            "prompt": prompt,
            "config": types.GenerateVideosConfig(**config_kwargs),
        }
        if image is not None:
            request_kwargs["image"] = image

        start_ts = time.time()
        try:
            operation = self.client.models.generate_videos(**request_kwargs)
        except Exception as e:
            raise VeoGenerationError(f"Veo API call failed ({mode}): {e}") from e

        poll_count = 0
        while not operation.done:
            if (time.time() - start_ts) > POLL_TIMEOUT_SEC:
                raise VeoTimeoutError(
                    f"Veo operation did not complete within {POLL_TIMEOUT_SEC}s"
                )
            poll_count += 1
            logger.info(
                f"[VeoClient] polling ({poll_count}x, elapsed={int(time.time() - start_ts)}s)..."
            )
            time.sleep(POLL_INTERVAL_SEC)
            try:
                operation = self.client.operations.get(operation)
            except Exception as e:
                raise VeoGenerationError(f"Operation polling failed: {e}") from e

        if not getattr(operation, "response", None):
            err = getattr(operation, "error", None)
            raise VeoGenerationError(
                f"Veo generation failed without response. error={err}"
            )

        try:
            generated_video = operation.response.generated_videos[0]
        except (AttributeError, IndexError, TypeError) as e:
            raise VeoGenerationError(f"No generated_videos in response: {e}") from e

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.files.download(file=generated_video.video)
            generated_video.video.save(str(out))
        except Exception as e:
            raise VeoGenerationError(f"Video download/save failed: {e}") from e

        if not out.exists() or out.stat().st_size == 0:
            raise VeoGenerationError(f"Downloaded file empty or missing: {out}")

        elapsed_ms = int((time.time() - start_ts) * 1000)
        cost_usd = unit_price_per_sec() * duration_sec
        file_size_mb = out.stat().st_size / 1024 / 1024
        logger.info(
            f"[VeoClient] {mode} done: path={out} size={file_size_mb:.2f}MB "
            f"elapsed={elapsed_ms}ms cost=${cost_usd:.4f}"
        )
        return {
            "video_uri": str(out),
            "duration_sec": duration_sec,
            "cost_usd": round(cost_usd, 4),
            "generation_ms": elapsed_ms,
            "file_size_mb": round(file_size_mb, 2),
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "audio_generated": True,  # Veo default ON
            "mode": mode,
        }

    # ────────────────────────────────────────────────────────
    # T2V
    # ────────────────────────────────────────────────────────

    def generate_text_to_video(
        self,
        prompt: str,
        output_path: str,
        duration_sec: int = DEFAULT_DURATION_SEC,
        resolution: str = DEFAULT_RESOLUTION,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        negative_prompt: Optional[str] = None,
        person_generation: Optional[str] = None,  # v1.3.2: ignored, kept for backward-compat
        generate_audio: bool = False,
    ) -> dict:
        """
        Text-to-Video generation.

        Args:
            prompt           : Scene description text
            output_path      : Local path to save the resulting mp4
            duration_sec     : 4, 6, or 8
            resolution       : "720p" or "1080p"
            aspect_ratio     : "9:16" (vertical) or "16:9" (landscape)
            negative_prompt  : What NOT to generate
            person_generation: IGNORED (Vertex AI-only field). Kept for compatibility.
            generate_audio   : IGNORED at API level (audio always ON).

        Returns:
            dict with video_uri, duration_sec, cost_usd, generation_ms, file_size_mb,
                 resolution, aspect_ratio, audio_generated, mode

        Raises:
            VeoGenerationError on API errors
            VeoTimeoutError on polling timeout
        """
        if generate_audio is False:
            logger.warning(
                "[VeoClient] generate_audio=False requested but NOT applied — "
                "Gemini API generates audio regardless. Strip via ffmpeg if needed."
            )
        self._warn_person_generation(person_generation)

        logger.info(
            f"[VeoClient] T2V start: model={MODEL} resolution={resolution} "
            f"aspect={aspect_ratio} duration={duration_sec}s prompt_len={len(prompt)}"
        )
        return self._run_operation(
            mode="T2V",
            prompt=prompt,
            output_path=output_path,
            duration_sec=duration_sec,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            negative_prompt=negative_prompt,
        )

    # ────────────────────────────────────────────────────────
    # I2V (v1.4.0 실장)
    # ────────────────────────────────────────────────────────

    def generate_image_to_video(
        self,
        prompt: str,
        start_frame_path: str,
        output_path: str,
        duration_sec: int = DEFAULT_DURATION_SEC,
        resolution: str = DEFAULT_RESOLUTION,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        negative_prompt: Optional[str] = None,
        person_generation: Optional[str] = None,  # ignored, kept for backward-compat
        generate_audio: bool = False,  # ignored at API level
    ) -> dict:
        """
        Image-to-Video: start_frame_path 이미지를 첫 프레임으로 영상을 생성한다.

        캐릭터 외형을 REF 기반 키프레임으로 고정하기 위한 경로다
        (T2V 는 텍스트 묘사만으로 외형을 맞춰야 해 드리프트가 발생했다).

        Raises:
            VeoGenerationError: 키프레임 누락/형식 오류/API 오류
        """
        from google.genai import types

        frame = Path(start_frame_path)
        if not frame.exists() or frame.stat().st_size == 0:
            raise VeoGenerationError(f"start frame not found or empty: {frame}")
        mime = _IMAGE_MIME.get(frame.suffix.lower())
        if mime is None:
            raise VeoGenerationError(f"unsupported start frame type: {frame.suffix}")
        self._warn_person_generation(person_generation)

        image = types.Image(image_bytes=frame.read_bytes(), mime_type=mime)
        logger.info(
            f"[VeoClient] I2V start: model={MODEL} resolution={resolution} "
            f"aspect={aspect_ratio} duration={duration_sec}s prompt_len={len(prompt)} "
            f"frame={frame.name}"
        )
        return self._run_operation(
            mode="I2V",
            prompt=prompt,
            output_path=output_path,
            duration_sec=duration_sec,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            negative_prompt=negative_prompt,
            image=image,
        )

    @staticmethod
    def _warn_person_generation(person_generation: Optional[str]) -> None:
        if person_generation is not None:
            logger.warning(
                "[VeoClient] person_generation=%r received but NOT sent to API "
                "(Vertex AI only). Ignoring.",
                person_generation,
            )
