"""
ICG Video Track — main runner
Strict Isolation from image track (run_market.py).

Stages:
  data              : STEP 1V-2V — scheduler check + DB read (read-only)
  scenario          : STEP 3V — scenario selection (reuses v2.0)
  narrative         : STEP 4V — Claude video script generation
  persist_init      : STEP 5V — icg.video_assets INSERT (generating)
  veo               : STEP 6V — Veo 3.1 Lite x 3 cuts (I2V chaining)
  assembly          : STEP 7V — FFmpeg concat + audio + subtitle + render
  gate_notify       : PAUSE   — Telegram approval request to master
  manual_prompt_notify: PRE-OP — Telegram prompt delivery for manual Gemini generation
  publish_telegram  : STEP 8V-a — TG free + paid channel video publish
  publish_x         : STEP 8V-b — X (Twitter) chunked video upload
  publish_shorts    : STEP 8V-c — YouTube Shorts API upload
  persist_final     : STEP 8V-d — icg.video_assets status='published' update

Note on publish stages:
  publish_* and persist_final stages run ONLY after master approval
  (via callback or separate workflow trigger). The gate_notify stage
  is the last step of the main scheduled run.
"""

import argparse
import logging
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _ensure_repo_root_on_path() -> None:
    """Allow this script to import project packages when run as a file."""
    repo_root = Path(__file__).resolve().parents[1]
    repo_root_text = str(repo_root)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)


_ensure_repo_root_on_path()

VERSION = "2.1.0"

logger = logging.getLogger("run_video_trailer")

# Environment variables tracked for presence on every run (masked in logs).
# These follow the existing ICG repository Secret naming convention.
# Not all are required — missing ones are logged but don't fail execution.
_TRACKED_ENV_VARS = [
    # Core APIs (ICG 규약 준수)
    "ANTHROPIC_API_KEY",
    "GEMINI_API_SUB_PAY_KEY",  # Veo + TTS 공용 Paid 키
    "FRED_API_KEY",
    "NOTION_API_KEY",
    # Supabase
    "SUPABASE_URL",
    "SUPABASE_KEY",
    # X (Twitter)
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
    # Telegram (게이트 + 채널)
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_FREE_CHANNEL_ID",
    "TELEGRAM_PAID_CHANNEL_ID",
    "MASTER_CHAT_ID",  # Phase V5 게이트 전 등록 필요
    # Budget cap
    "VIDEO_BUDGET_USD_MONTHLY",
]


def _mask_secret(value: str, env_name: str) -> str:
    """Mask sensitive env values for logging. Show first 4 chars + **** for tokens."""
    if not value:
        return "(empty)"
    # Non-sensitive values display as-is (truncated if too long)
    non_sensitive = {"MASTER_CHAT_ID", "SUPABASE_URL", "VIDEO_BUDGET_USD_MONTHLY"}
    if env_name in non_sensitive or env_name.endswith("_ID") or env_name.endswith("_URL"):
        return value if len(value) <= 40 else value[:37] + "..."
    # Sensitive values: show prefix only
    return value[:4] + "****" if len(value) > 4 else "****"


def _setup_logging(stage: str) -> Path:
    """
    Configure root logger with Console (INFO) + File (DEBUG) handlers.

    Returns:
        Path to the log file written for this stage.
    """
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    console_level = getattr(logging, log_level_name, logging.INFO)

    root = logging.getLogger()
    # Remove any pre-existing handlers (e.g., from prior basicConfig)
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter

    # ── SECURITY (v1.6.2, 2026-08-29 회고) ─────────────────────────────
    # hpack 은 DEBUG 레벨에서 HTTP/2 요청 헤더 전문을 덤프하는데,
    # supabase-py 의 'apikey' 헤더는 sensitive 마킹이 없어 service_role JWT 가
    # 로그 파일(artifact)에 평문 노출됐다. 네트워크 계층 로거는 레벨 무관하게
    # WARNING 으로 고정해 레코드 생성 자체를 차단한다 (파일 핸들러 DEBUG 포함).
    for noisy in ("hpack", "httpcore", "httpx", "h2", "hyperframe", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (stdout; GitHub Actions captures this)
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    # File handler (always DEBUG for full detail)
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    log_dir = Path("logs") / str(run_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{stage}.log"

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    return log_file


def _log_environment_info(stage: str, log_file: Path) -> None:
    """Dump runtime context at the start of each stage for easier debugging."""
    logger.info("=" * 72)
    logger.info(f"STAGE START: {stage}")
    logger.info("=" * 72)
    logger.info(f"run_video_trailer v{VERSION}")
    logger.info(
        f"Python {platform.python_version()} on "
        f"{platform.system()} {platform.release()} ({platform.machine()})"
    )
    logger.info(f"Working directory: {Path.cwd()}")
    logger.info(f"Log file: {log_file.resolve()}")

    # GitHub Actions context
    gh_run_id = os.environ.get("GITHUB_RUN_ID", "N/A")
    gh_workflow = os.environ.get("GITHUB_WORKFLOW", "N/A")
    gh_ref = os.environ.get("GITHUB_REF_NAME", "N/A")
    gh_sha = os.environ.get("GITHUB_SHA", "N/A")
    logger.info(
        f"GitHub: workflow={gh_workflow} run_id={gh_run_id} "
        f"ref={gh_ref} sha={gh_sha[:8] if gh_sha != 'N/A' else gh_sha}"
    )

    # Runtime flags
    dry_run = os.environ.get("DRY_RUN", "false")
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logger.info(f"Flags: DRY_RUN={dry_run} LOG_LEVEL={log_level}")

    # Environment variables check (masked)
    present = []
    missing = []
    for env in _TRACKED_ENV_VARS:
        val = os.environ.get(env, "")
        if val:
            present.append(env)
            logger.debug(f"  ENV {env}={_mask_secret(val, env)} (set)")
        else:
            missing.append(env)

    logger.info(
        f"Env vars: {len(present)}/{len(_TRACKED_ENV_VARS)} set"
        + (f", missing={missing}" if missing else "")
    )
    logger.info("-" * 72)


def _resolve_target_date() -> str:
    """대상 날짜: env TARGET_DATE (YYYY-MM-DD) 우선, 없으면 오늘(KST)."""
    override = os.environ.get("TARGET_DATE", "").strip()
    if override:
        return override
    return str(datetime.now(ZoneInfo("Asia/Seoul")).date())


def stage_data():
    """STEP S1: Daily Battle Shorts 게이트 — 메이저 이벤트 AND 배틀 존재 시에만 진행.

    스토리 소스는 이미지 트랙 산출물(icg.episode_assets)을 읽기 전용 재사용한다 (R2).
    게이트 미통과는 skipped 기록 후 정상 종료(rc=0) — 알림 스팸 없음 (R3).
    """
    from engine.video.shorts_pipeline import persist_gate_result, run_gate

    target_date = _resolve_target_date()
    logger.info(f"[S1] gate check start: date={target_date}")

    gate = run_gate(target_date)
    status = persist_gate_result(gate)

    if not gate.passed:
        logger.info(
            f"[S1] gate BLOCKED — reason={gate.reason} status={status} "
            f"(정상 종료, 영상 미생성)"
        )
        sys.exit(0)

    logger.info(
        f"[S1] gate PASS: event_type={gate.event_type} "
        f"scenario={gate.scenario_type} episode_id={gate.episode_id}"
    )
    return {"episode_date": target_date, "episode_id": gate.episode_id}


def stage_scenario():
    """STEP S1(재검): 시나리오는 episode_assets 확정값 승계 — 선택 로직 재실행 금지 (R2/R5)."""
    from engine.video.shorts_pipeline import run_gate

    target_date = _resolve_target_date()
    gate = run_gate(target_date)
    if not gate.passed:
        logger.info(f"[S1-recheck] gate BLOCKED — reason={gate.reason} (정상 종료)")
        sys.exit(0)
    logger.info(f"[S1-recheck] scenario 승계: {gate.scenario_type} (episode_assets 확정값)")
    return gate.scenario_type


def stage_narrative():
    """STEP S2: Claude 각색 — 8패널 스크립트 → 인트로+3컷+아웃트로 쇼츠 시나리오.

    승패/캐스팅은 Immutable Facts 강제 주입 + Consistency Guard 로 불변 보장.
    DRY_RUN 이면 Claude 미호출(비용 0) — 게이트 결과만 기록.
    """
    from engine.video.shorts_pipeline import (
        generate_shorts_scenario,
        persist_scenario,
        run_gate,
    )

    target_date = _resolve_target_date()
    gate = run_gate(target_date)
    if not gate.passed:
        logger.info(f"[S2] gate BLOCKED — reason={gate.reason} (정상 종료)")
        sys.exit(0)

    scenario, cost = generate_shorts_scenario(gate)
    if scenario is None:
        logger.info("[S2] DRY_RUN — 각색 스킵 (게이트 기록만 유지)")
        return

    persist_scenario(gate, scenario)
    logger.info(
        f"[S2] 각색 저장 완료: episode_id={gate.episode_id} "
        f"total={scenario.total_duration_sec()}s cost=${cost:.4f}"
    )


def _get_episode_id(today: str) -> str:
    """
    Generate deterministic episode_id for the given date.

    Format: icg-v-YYYY-MM-DD-001

    V2 MVP Phase 1: always suffix "-001" (single episode per day).
    Future: allow multi-episode per day by incrementing suffix.
    """
    return f"icg-v-{today}-001"


def _resolve_publish_episode_id(target_date: str) -> str:
    """Return the exact release target selected by the publish workflow.

    Weekly IDs cannot be reconstructed from ``episode_date`` because they use the
    ``icg-vw-YYYY-Www-NNN`` format.  The hold-and-release workflow therefore
    supplies TARGET_EPISODE_ID; retain the date-derived ID only for legacy/manual
    daily publishing.
    """
    return os.environ.get("TARGET_EPISODE_ID", "").strip() or _get_episode_id(
        target_date
    )


def _load_cut1_prompt() -> tuple[str, str]:
    """
    Load cut1 prompt from config/prompts/cut1_prompt.txt.

    File format (section-based):
        ===PROMPT===
        <main prompt text>

        ===CHARACTER_LOCK===
        <character lock block>

        ===NEGATIVE_PROMPT===
        <negative prompt text>

    Returns:
        (full_prompt, negative_prompt)
        full_prompt = PROMPT section + "\n\n" + CHARACTER_LOCK section
    """
    from pathlib import Path

    prompt_path = Path("config/prompts/cut1_prompt.txt")
    if not prompt_path.exists():
        raise FileNotFoundError(f"Cut1 prompt file not found: {prompt_path}")

    text = prompt_path.read_text(encoding="utf-8")
    sections = {}
    current_key = None
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("===") and stripped.endswith("==="):
            if current_key is not None:
                sections[current_key] = "\n".join(buf).strip()
            current_key = stripped.strip("= ").strip()
            buf = []
        elif current_key is not None:
            buf.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(buf).strip()

    main_prompt = sections.get("PROMPT", "")
    character_lock = sections.get("CHARACTER_LOCK", "")
    negative = sections.get("NEGATIVE_PROMPT", "")

    if not main_prompt:
        raise ValueError("PROMPT section missing or empty in cut1_prompt.txt")

    full_prompt = main_prompt
    if character_lock:
        full_prompt = f"{main_prompt}\n\n[CHARACTER LOCK]\n{character_lock}"

    return full_prompt, negative


def _create_dummy_mp4(output_path: str) -> int:
    """
    Create a 1KB placeholder mp4 for DRY_RUN mode.
    Returns the file size in bytes.
    """
    from pathlib import Path

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Minimal MP4 signature so the file is not totally invalid
    dummy_bytes = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1016
    out.write_bytes(dummy_bytes)
    return out.stat().st_size


def _get_supabase_client():
    """Lazy-import supabase client; raise informative error if not installed."""
    try:
        from supabase import create_client
    except ImportError as e:
        raise RuntimeError("supabase package not installed. Run: pip install supabase") from e

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL or SUPABASE_KEY env variable not set")
    return create_client(url, key)


def _is_missing_video_column_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "artifact_run_id" in message and ("column" in message or "schema" in message)


def _safe_video_assets_upsert(sb, payload: dict, *, context: str):
    """Upsert video_assets, retrying without optional artifact_run_id on old schemas."""
    try:
        return (
            sb.schema("icg")
            .table("video_assets")
            .upsert(
                payload,
                on_conflict="episode_id",
            )
            .execute()
        )
    except Exception as exc:
        if "artifact_run_id" in payload and _is_missing_video_column_error(exc):
            fallback = dict(payload)
            fallback.pop("artifact_run_id", None)
            logger.warning(
                "[%s] video_assets.artifact_run_id column missing; retrying without artifact link",
                context,
            )
            return (
                sb.schema("icg")
                .table("video_assets")
                .upsert(
                    fallback,
                    on_conflict="episode_id",
                )
                .execute()
            )
        raise


def _safe_video_assets_update(sb, episode_id: str, payload: dict, *, context: str):
    """Update video_assets, retrying without optional artifact_run_id on old schemas."""
    try:
        return (
            sb.schema("icg")
            .table("video_assets")
            .update(payload)
            .eq("episode_id", episode_id)
            .execute()
        )
    except Exception as exc:
        if "artifact_run_id" in payload and _is_missing_video_column_error(exc):
            fallback = dict(payload)
            fallback.pop("artifact_run_id", None)
            logger.warning(
                "[%s] video_assets.artifact_run_id column missing; retrying without artifact link",
                context,
            )
            return (
                sb.schema("icg")
                .table("video_assets")
                .update(fallback)
                .eq("episode_id", episode_id)
                .execute()
            )
        raise


def stage_persist_init():
    """STEP 5V: icg.video_assets UPSERT (status='generating')."""
    logger.info("[5V] persist init to icg.video_assets")

    today_kst = datetime.now(ZoneInfo("Asia/Seoul")).date()
    today_str = today_kst.isoformat()
    episode_id = _get_episode_id(today_str)
    scenario_type = "ONE_VS_ONE"  # V2 MVP Phase 1: hardcoded

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        logger.info(f"[5V] DRY_RUN: skipping Supabase upsert (episode_id={episode_id})")
        logger.info(f"[5V] record initialized (dry_run): episode_id={episode_id}")
        return

    sb = _get_supabase_client()
    payload = {
        "episode_id": episode_id,
        "episode_date": today_str,
        "scenario_type": scenario_type,
        "status": "generating",
        "artifact_run_id": os.environ.get("GITHUB_RUN_ID"),
    }
    try:
        result = _safe_video_assets_upsert(sb, payload, context="5V")
        logger.debug(f"[5V] upsert result rows: {len(result.data or [])}")
    except Exception as e:
        logger.exception(f"[5V] Supabase upsert failed: {e}")
        raise

    logger.info(f"[5V] record initialized: episode_id={episode_id} status=generating")


def stage_veo():
    """STEP S3+S4: 북엔드 이미지(Gemini) + 본편 3컷(Veo T2V) 생성.

    v1.6.0: shorts_scenario_json 기반으로 개편 (구 cut1_prompt.txt 단일컷 방식 대체).
    DRY_RUN 판정을 공통 지침 규약(기본 'true')으로 교정 — 구현체는 shorts_media 참조.
    부분 실패 시 당일 생성 중단(부분 발행 금지). 이미지 트랙에는 영향 없음.
    """
    from engine.video.shorts_media import (
        MediaResult,
        generate_bookend_images,
        generate_cut_videos,
        persist_media,
    )
    from engine.video.shorts_pipeline import load_scenario, run_gate

    target_date = _resolve_target_date()
    gate = run_gate(target_date)
    if not gate.passed:
        logger.info(f"[S3/S4] gate BLOCKED — reason={gate.reason} (정상 종료)")
        sys.exit(0)

    scenario = load_scenario(gate.episode_id)
    if scenario is None:
        # DRY_RUN narrative 단계에서는 각색을 스킵하므로 시나리오가 없을 수 있다.
        logger.warning(
            "[S3/S4] shorts_scenario_json 없음 — narrative(S2) 실발행 실행 필요 (정상 종료)"
        )
        sys.exit(0)

    # v1.8.0: 유료 호출 이전 사전 체크리스트 (예산/도구/폰트/발행자격) 1회 실행
    from engine.video.shorts_media import preflight_check

    report = preflight_check(scenario)
    logger.info(f"[S3/S4] preflight: {report}")

    out_dir = Path(f"output/videos/{gate.episode_id}")
    media: MediaResult = generate_bookend_images(scenario, out_dir / "images")
    media = generate_cut_videos(scenario, out_dir / "cuts", media)

    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    if dry_run:
        logger.info("[S3/S4] DRY_RUN — Supabase 기록 스킵 (더미 산출물만 생성)")
        return

    persist_media(gate.episode_id, media)
    logger.info(
        f"[S3/S4] 미디어 생성 완료: episode_id={gate.episode_id} "
        f"cost=${media.total_cost_usd:.4f}"
    )


def stage_assembly():
    """STEP S5: 조립 — [인트로clip+3컷+아웃트로clip] concat → 자막 번인 → TTS 믹스 → 최종 렌더.

    v1.6.0 실장. 산출물: output/videos/{episode_id}/assembly/final_shorts.mp4
    TTS 개별 실패는 무음 fallback (자막으로 정보 전달 — 조립 중단 없음).
    """
    from engine.video.shorts_media import (
        MediaResult,
        assemble_shorts,
        persist_assembled,
    )
    from engine.video.shorts_pipeline import load_scenario, run_gate

    target_date = _resolve_target_date()
    gate = run_gate(target_date, for_generation=False)  # 조립은 과금 없음
    if not gate.passed:
        logger.info(f"[S5] gate BLOCKED — reason={gate.reason} (정상 종료)")
        sys.exit(0)

    scenario = load_scenario(gate.episode_id)
    if scenario is None:
        logger.warning("[S5] shorts_scenario_json 없음 — S2 선행 필요 (정상 종료)")
        sys.exit(0)

    out_dir = Path(f"output/videos/{gate.episode_id}")
    images_dir = out_dir / "images"
    cuts_dir = out_dir / "cuts"

    media = MediaResult(
        intro_image=images_dir / "P91.png",
        outro_image=images_dir / "P92.png",
        cut_paths=[cuts_dir / f"cut{i}.mp4" for i in (1, 2, 3)],
    )
    missing = [
        str(p)
        for p in [media.intro_image, media.outro_image, *media.cut_paths]
        if not Path(p).exists()
    ]
    if missing:
        raise FileNotFoundError(f"[S5] 조립 입력 누락 (S3/S4 선행 필요): {missing}")

    final_path = assemble_shorts(scenario, media, out_dir / "assembly")

    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    if dry_run:
        logger.info(f"[S5] DRY_RUN — Supabase 기록 스킵 (final={final_path})")
        return

    persist_assembled(gate.episode_id, final_path)
    logger.info(f"[S5] 조립 완료: {final_path}")


def stage_gate_notify():
    """PAUSE(S6): 마스터 승인 요청 — 게이트 통과 + 조립 완료일에만 발송.

    v1.6.1 실장 + 갭 보완: 게이트 미통과(skipped)일에는 알림을 보내지 않는다
    (2026-08-29 dry_run #33216557560 점검 — 게이트 체크 없이 매일 실행되던 갭).
    발송 성공 시 status='pending_approval' 전이. DRY_RUN 이면 발송/전이 스킵.
    """
    from engine.video.shorts_pipeline import load_scenario, run_gate

    target_date = _resolve_target_date()
    gate = run_gate(target_date, for_generation=False)  # 알림은 과금 없음
    if not gate.passed:
        logger.info(f"[S6] gate BLOCKED — reason={gate.reason} (알림 미발송, 정상 종료)")
        sys.exit(0)

    scenario = load_scenario(gate.episode_id)
    final_path = Path(f"output/videos/{gate.episode_id}/assembly/final_shorts.mp4")

    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    if dry_run:
        logger.info(
            f"[S6] DRY_RUN — 승인 요청 스킵 (episode_id={gate.episode_id}, "
            f"final_exists={final_path.exists()})"
        )
        return

    if scenario is None or not final_path.exists():
        raise RuntimeError(
            f"[S6] 승인 요청 불가 — scenario={'있음' if scenario else '없음'} "
            f"final_mp4={'있음' if final_path.exists() else '없음'} (S2~S5 선행 필요)"
        )

    from engine.publish.telegram_gate import send_approval_request

    sb_row = None
    try:
        from engine.video.shorts_pipeline import _load_video_asset_row

        sb_row = _load_video_asset_row(gate.episode_id)
    except Exception as exc:
        logger.warning(f"[S6] video_assets 조회 실패 (비용 0 표기로 진행): {exc}")

    cost_usd = float((sb_row or {}).get("veo_cost_usd") or 0.0)

    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo as _ZI

    _hold = float(os.environ.get("PUBLISH_HOLD_HOURS", "6"))
    _release_kst = (_dt.now(_UTC) + _td(hours=_hold)).astimezone(_ZI("Asia/Seoul"))

    send_approval_request(
        video_path=str(final_path),
        episode_id=gate.episode_id,
        scenario_type=gate.scenario_type,
        cost_usd=cost_usd,
        generation_ms=int((sb_row or {}).get("generation_ms") or 0),
        release_at_kst=_release_kst.strftime("%m/%d %H:%M"),
    )

    from datetime import UTC, datetime, timedelta

    from engine.common.supabase_client import icg_table

    # v1.7.0 hold-and-release: 기본은 자동 발행, 마스터가 개입하면 중단.
    # release_at 이후 Publish Shorts 워크플로가 자동으로 업로드한다.
    hold_hours = float(os.environ.get("PUBLISH_HOLD_HOURS", "6"))
    release_at = datetime.now(UTC) + timedelta(hours=hold_hours)

    icg_table("video_assets").update(
        {"status": "pending_approval", "release_at": release_at.isoformat()}
    ).eq("episode_id", gate.episode_id).execute()

    logger.info(
        f"[S6] 승인 요청 발송 + pending_approval 전이: {gate.episode_id} "
        f"(hold={hold_hours}h, release_at={release_at.isoformat()})"
    )
    logger.info("[PAUSE] 홀드 시작 — 중단하지 않으면 release_at 이후 자동 발행")


def _send_telegram_text(chat_id: str, text: str) -> dict:
    """Send plain text message via Telegram Bot API."""
    import requests

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env not set")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    result = data.get("result", {})
    return {
        "chat_id": result.get("chat", {}).get("id"),
        "message_id": result.get("message_id"),
    }


def stage_manual_prompt_notify():
    """
    PRE-OP stage: Send Veo prompt to Telegram for manual generation in Gemini chat.

    Why:
      Video quality is not finalized yet, so before production rollout we only deliver
      the prompt package to operator Telegram chat and generate manually in Gemini.
      In this mode, X publishing is intentionally disabled.
    """
    logger.info("[PRE-OP] manual prompt notify start")

    master_chat_id = os.environ.get("MASTER_CHAT_ID") or os.environ.get("TELEGRAM_FREE_CHANNEL_ID")
    if not master_chat_id:
        raise RuntimeError("MASTER_CHAT_ID (or TELEGRAM_FREE_CHANNEL_ID fallback) env not set")

    full_prompt, negative_prompt = _load_cut1_prompt()
    today_kst = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    episode_id = _get_episode_id(today_kst)
    operation_mode = os.environ.get("OPERATION_MODE", "manual_prompt").lower()
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    message = (
        "🎬 ICG VIDEO PRE-OP MODE\n"
        f"episode_id: {episode_id}\n"
        f"operation_mode: {operation_mode}\n"
        "policy: manual Gemini generation / no X publish\n\n"
        "[PROMPT]\n"
        f"{full_prompt}\n\n"
        "[NEGATIVE_PROMPT]\n"
        f"{negative_prompt}"
    )
    max_len = 3900
    if len(message) > max_len:
        message = message[: max_len - 24] + "\n\n...(truncated in Telegram)"

    if dry_run:
        logger.info(
            "[PRE-OP] DRY_RUN: skip Telegram send "
            f"(chat_id={master_chat_id}, message_len={len(message)})"
        )
        logger.debug("[PRE-OP] payload preview:\n%s", message)
        return

    result = _send_telegram_text(chat_id=master_chat_id, text=message)
    logger.info(
        f"[PRE-OP] prompt sent to Telegram: chat_id={result['chat_id']} "
        f"message_id={result['message_id']}"
    )


def stage_publish_telegram():
    """STEP 8V-a: Publish to TG free + paid channels (runs AFTER master approval)."""
    logger.info("[8V-a] Telegram channels publish start")

    # TODO: Load episode metadata (approved=True) from icg.video_assets
    # TODO: Call video publisher (NOT the existing telegram_publisher.py which is for images)
    # from engine.publish.telegram_video_publisher import (
    #     publish_to_free_channel, publish_to_paid_channel
    # )
    #
    # free_result = publish_to_free_channel(
    #     video_path=final_mp4_path,
    #     episode_id=episode_id,
    #     title=title,
    #     hashtags=hashtags,
    #     teaser_line=teaser_line,
    #     paid_channel_invite_link=PAID_INVITE_URL,
    # )
    # paid_result = publish_to_paid_channel(
    #     video_path=final_mp4_path,
    #     episode_id=episode_id,
    #     title=title,
    #     hashtags=hashtags,
    #     full_narrative=full_narrative,
    #     market_context=market_context,
    # )
    # Update icg.video_assets.published_tg = NOW()

    logger.info("[8V-a] Telegram publish done")


def stage_publish_x():
    """STEP 8V-b: Publish to X (Twitter) via chunked upload."""
    operation_mode = os.environ.get("OPERATION_MODE", "manual_prompt").lower()
    if operation_mode == "manual_prompt":
        logger.warning("[8V-b] skipped: OPERATION_MODE=manual_prompt (no X publish policy)")
        return

    logger.info("[8V-b] X video publish start")

    # TODO: from engine.publish.x_video_publisher import publish_video_to_x
    # result = publish_video_to_x(
    #     video_path=final_mp4_path,
    #     caption=x_caption,  # ≤280 chars
    #     episode_id=episode_id,
    # )
    # Update icg.video_assets.tweet_id = result["tweet_id"],
    #                       .published_x = NOW()

    logger.info("[8V-b] X publish done")


def stage_publish_shorts():
    """STEP S7: YouTube Shorts 업로드 (마스터 승인 후 publish 모드에서만 실행).

    v1.6.0 실장. 발행-기록 짝 규약: 업로드 성공 즉시 같은 함수 안에서
    youtube_video_id + published_shorts 를 기록한다 (기록 누락 = 중복 발행 리스크).
    DRY_RUN(기본 true)이면 실제 업로드 없이 메타데이터 검증만 수행.
    """
    from engine.publish.youtube_shorts_publisher import publish_to_youtube_shorts
    from engine.video.shorts_pipeline import load_scenario

    target_date = _resolve_target_date()
    episode_id = _resolve_publish_episode_id(target_date)
    logger.info(f"[S7] YouTube Shorts publish start: episode_id={episode_id}")

    if episode_id.startswith("icg-vw-"):
        # v2.1.0: 주간은 v1(ShortsScenario)/v2(WeeklyScenarioV2) 스키마가 공존한다.
        from engine.video.weekly_pipeline import is_pilot_episode
        from engine.video.weekly_v2 import load_weekly_any, publish_fields

        if is_pilot_episode(episode_id):
            raise RuntimeError(f"[S7] 파일럿 에피소드는 발행 대상이 아니다: {episode_id}")
        scenario = load_weekly_any(episode_id)
    else:
        scenario = load_scenario(episode_id)
    if scenario is None:
        raise RuntimeError(f"[S7] shorts_scenario_json 없음: {episode_id} — S2 선행 필요")

    final_path = Path(f"output/videos/{episode_id}/assembly/final_shorts.mp4")
    if not final_path.exists():
        raise FileNotFoundError(
            f"[S7] final mp4 없음: {final_path} — publish 모드에서는 "
            f"artifact 복원 step(Restore assembled artifact)이 선행되어야 한다"
        )

    if episode_id.startswith("icg-vw-"):
        fields = publish_fields(scenario)
        title, description = fields["title"], fields["description"]
    else:
        title, description = scenario.youtube_title, scenario.youtube_description

    result = publish_to_youtube_shorts(
        video_path=str(final_path),
        title=title,
        description=description,
        episode_id=episode_id,
        tags=["미국주식", "시장분석", "투자코믹", "EDT"],
    )

    if result["status"] == "dry_run":
        logger.info("[S7] DRY_RUN — 업로드/기록 스킵")
        return

    # 발행-기록 짝: 업로드 성공 즉시 기록 (실패 시 예외 전파 — persist_final 미진행)
    from datetime import UTC

    from engine.common.supabase_client import icg_table

    icg_table("video_assets").update(
        {
            "youtube_video_id": result["youtube_video_id"],
            "published_shorts": datetime.now(UTC).isoformat(),
        }
    ).eq("episode_id", episode_id).execute()
    logger.info(
        f"[S7] 업로드 완료 + 기록: video_id={result['youtube_video_id']} "
        f"url={result['youtube_url']}"
    )



# ════════════════════════════════════════════════════════════
# Weekly Digest (v2.0.0) — 월~금 누적 → 월요일 오전 18초 1편
# 일일 생성 스테이지(stage_data/narrative/veo/assembly)는 유지하되,
# 정규 운영 경로는 아래 weekly_* 스테이지다.
# ════════════════════════════════════════════════════════════


def _weekly_gate_or_exit(for_generation: bool = True):
    """주간 게이트 통과 시 gate 반환, 미통과면 정상 종료(rc=0).

    for_generation=False 는 이미 생성된 자산을 소비하는 후속 단계(W6/W7)용으로,
    중복 과금 가드를 적용하지 않는다 (2026-09-07 run #34101547854 회고).
    """
    from engine.video.weekly_pipeline import run_weekly_gate

    gate = run_weekly_gate(for_generation=for_generation)
    if not gate.passed:
        logger.info(f"[W] gate BLOCKED — reason={gate.reason} (정상 종료, 영상 미생성)")
        sys.exit(0)
    return gate


def stage_weekly_gate():
    """W1: 주간 게이트 — 지난주 월~금 에피소드 수집 및 판정."""
    from engine.video.weekly_pipeline import persist_weekly_gate, run_weekly_gate

    gate = run_weekly_gate()
    status = persist_weekly_gate(gate)
    if not gate.passed:
        logger.info(f"[W1] gate BLOCKED — reason={gate.reason} status={status}")
        # v2.0.1: 주 1회뿐인 발행이 조용히 빠지면 마스터가 인지할 수 없다.
        # 일일 트랙과 달리 스팸이 아니므로 차단 사유를 알린다 (DRY_RUN 제외).
        if os.environ.get("DRY_RUN", "true").lower() != "true":
            chat_id = os.environ.get("MASTER_CHAT_ID", "")
            if chat_id:
                try:
                    _send_telegram_text(
                        chat_id,
                        "⚠️ <b>주간 다이제스트 생성 안 함</b>\n"
                        f"구간: {gate.week_start} ~ {gate.week_end}\n"
                        f"사유: <code>{gate.reason}</code>\n"
                        f"에피소드: {gate.episode_count}건\n"
                        "(이미지 트랙 결손 여부를 확인하세요)",
                    )
                except Exception as exc:
                    logger.warning(f"[W1] 차단 알림 발송 실패 (무시): {exc}")
        sys.exit(0)
    logger.info(
        f"[W1] gate PASS: {gate.week_start}~{gate.week_end} "
        f"episodes={gate.episode_count} battles={gate.battle_count} id={gate.episode_id}"
    )


def stage_weekly_narrative():
    """W3: 주간 각색. WEEKLY_FORMAT=v2 이면 4샷 액션 시나리오(v2), 아니면 v1(2컷)."""
    from engine.video.weekly_v2 import weekly_format

    gate = _weekly_gate_or_exit()

    if weekly_format() == "v2":
        from engine.video.weekly_media import record_spend
        from engine.video.weekly_v2 import generate_v2_scenario, persist_v2_scenario

        try:
            scenario, cost = generate_v2_scenario(gate)
        except Exception as exc:
            spent = float(getattr(exc, "cost_usd", 0.0) or 0.0)
            if spent > 0 and os.environ.get("DRY_RUN", "true").lower() != "true":
                try:
                    record_spend(gate.episode_id, spent, note="weekly_v2_narrative_failed")
                except Exception as rec_exc:
                    logger.error(f"[W3v2] 실패 각색 비용 기록 실패: {rec_exc}")
            raise
        if scenario is None:
            logger.info("[W3v2] DRY_RUN — 각색 스킵 (게이트 기록만 유지)")
            return
        persist_v2_scenario(gate, scenario)
        # 각색 비용도 회차 원장에 누적한다 (예산 가드가 실지출을 보도록)
        record_spend(gate.episode_id, cost, note="weekly_v2_narrative")
        logger.info(
            f"[W3v2] 각색 저장 완료: {gate.episode_id} shots={len(scenario.shots)} "
            f"heroes={scenario.hero_ids} villain={scenario.villain_id} cost=${cost:.4f}"
        )
        return

    from engine.video.weekly_pipeline import (
        generate_weekly_scenario,
        persist_weekly_scenario,
    )

    scenario, cost = generate_weekly_scenario(gate)
    if scenario is None:
        logger.info("[W3] DRY_RUN — 각색 스킵 (게이트 기록만 유지)")
        return
    persist_weekly_scenario(gate, scenario)
    logger.info(f"[W3] 각색 저장 완료: {gate.episode_id} cost=${cost:.4f}")


def _load_v2_or_exit(episode_id: str, stage: str):
    """v2 시나리오 로드. 없으면 정상 종료, v1 행이면 포맷 불일치로 실패."""
    from engine.video.weekly_v2 import WeeklyScenarioV2, load_weekly_any

    scenario = load_weekly_any(episode_id)
    if scenario is None:
        logger.warning(f"[{stage}] shorts_scenario_json 없음 — W3 선행 필요 (정상 종료)")
        sys.exit(0)
    if not isinstance(scenario, WeeklyScenarioV2):
        raise RuntimeError(
            f"[{stage}] {episode_id} 시나리오가 v1 형식인데 WEEKLY_FORMAT=v2 로 실행됨 — "
            "force_regenerate=true 로 v2 각색부터 다시 수행하거나 WEEKLY_FORMAT=v1 로 실행하라"
        )
    return scenario


def stage_weekly_media():
    """W4+W5. v2: 키프레임 4장(REF) + Veo I2V 4샷 + Motion QA. v1: 북엔드 2장 + T2V 2컷."""
    from engine.video.weekly_v2 import weekly_format

    gate = _weekly_gate_or_exit()

    if weekly_format() == "v2":
        from engine.video.weekly_media import (
            V2MediaResult,
            generate_keyframes,
            generate_shots,
            persist_v2_media,
            preflight_v2,
            record_spend,
        )

        scenario = _load_v2_or_exit(gate.episode_id, "W4/W5v2")
        report = preflight_v2(scenario)
        logger.info(f"[W4/W5v2] preflight: {report}")

        out_dir = Path(f"output/videos/{gate.episode_id}")
        media = V2MediaResult()
        dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
        try:
            generate_keyframes(scenario, out_dir, media)
            generate_shots(scenario, out_dir, media)
        except Exception:
            # 중단돼도 이미 지출된 비용은 원장에 남긴다 (덮어쓰기 금지, 누적)
            if not dry_run and media.total_cost_usd > 0:
                try:
                    record_spend(
                        gate.episode_id,
                        media.total_cost_usd,
                        note="weekly_v2_media_partial_failure",
                        manifest=media.manifest(),
                    )
                except Exception as exc:
                    logger.error(f"[W4/W5v2] 부분 지출 기록 실패: {exc}")
            raise

        if dry_run:
            logger.info("[W4/W5v2] DRY_RUN — Supabase 기록 스킵")
            return
        try:
            persist_v2_media(gate.episode_id, media)
        except Exception:
            # 미디어($)는 이미 만들어졌다. 최소한 지출·manifest 는 남기고 실패를 알린다
            # (artifact 는 upload-artifact if: always() 로 보존된다).
            logger.exception("[W4/W5v2] persist 실패 — 지출만 누적 기록 후 실패 처리")
            try:
                record_spend(
                    gate.episode_id,
                    media.total_cost_usd,
                    note="weekly_v2_media_persist_failed",
                    manifest={**media.manifest(), "artifact_run_id": os.environ.get("GITHUB_RUN_ID")},
                )
            except Exception as rec_exc:
                logger.error(f"[W4/W5v2] 지출 기록도 실패: {rec_exc}")
            raise
        logger.info(
            f"[W4/W5v2] 미디어 완료: cost=${media.total_cost_usd:.4f} "
            f"regen={media.regenerations} motion={media.motion}"
        )
        return

    from engine.video.shorts_media import (
        MediaResult,
        generate_bookend_images,
        generate_cut_videos,
        persist_media,
        preflight_check,
    )
    from engine.video.weekly_pipeline import load_weekly_scenario

    scenario = load_weekly_scenario(gate.episode_id)
    if scenario is None:
        logger.warning("[W4/W5] shorts_scenario_json 없음 — W3 실발행 선행 필요 (정상 종료)")
        sys.exit(0)

    report = preflight_check(scenario)
    logger.info(f"[W4/W5] preflight: {report}")

    out_dir = Path(f"output/videos/{gate.episode_id}")
    media: MediaResult = generate_bookend_images(scenario, out_dir / "images")
    media = generate_cut_videos(scenario, out_dir / "cuts", media)

    if os.environ.get("DRY_RUN", "true").lower() == "true":
        logger.info("[W4/W5] DRY_RUN — Supabase 기록 스킵")
        return
    persist_media(gate.episode_id, media)
    logger.info(f"[W4/W5] 미디어 완료: cost=${media.total_cost_usd:.4f}")


def _restore_failure_hint(episode_id: str) -> str:
    hint = "W4/W5 선행 필요"
    try:
        from engine.video.weekly_pipeline import _load_video_asset_row

        row = _load_video_asset_row(episode_id) or {}
        if row.get("status") in {"media_generated", "assembled", "pending_approval"}:
            hint = (
                f"DB status={row.get('status')} 로 미디어는 이미 생성됨 → "
                f"artifact 복원 실패가 원인 (artifact_run_id={row.get('artifact_run_id')}). "
                "워크플로의 'Resolve/Restore prior artifact' step 로그를 확인하십시오"
            )
    except Exception as exc:
        logger.warning(f"[W6] 상태 조회 실패 (기본 안내로 진행): {exc}")
    return hint


def stage_weekly_assembly():
    """W6. v2: 4샷 단일 패스 렌더(정지 0초). v1: 18초 조립 (인트로3 + 6초×2 + 아웃트로3)."""
    from engine.video.shorts_media import persist_assembled
    from engine.video.weekly_v2 import WeeklyScenarioV2, load_weekly_any

    gate = _weekly_gate_or_exit(for_generation=False)
    out_dir = Path(f"output/videos/{gate.episode_id}")

    # 조립은 이미 생성된 자산을 소비하므로 env 가 아니라 저장된 시나리오 형식으로 분기한다
    # (주중에 WEEKLY_FORMAT 을 바꿔도 해당 주차 재조립이 깨지지 않게).
    saved = load_weekly_any(gate.episode_id)
    if isinstance(saved, WeeklyScenarioV2):
        from engine.video.weekly_media import expected_paths
        from engine.video.weekly_render import render_weekly_v2

        scenario = saved
        _, shot_paths = expected_paths(out_dir)
        missing = [str(p) for p in shot_paths if not p.exists()]
        if missing:
            raise FileNotFoundError(
                f"[W6v2] 렌더 입력 누락 ({_restore_failure_hint(gate.episode_id)}): {missing}"
            )
        final_path, report = render_weekly_v2(scenario, shot_paths, out_dir / "assembly")
        logger.info(f"[W6v2] 렌더 완료: {final_path} report={report}")
        if os.environ.get("DRY_RUN", "true").lower() == "true":
            logger.info("[W6v2] DRY_RUN — Supabase 기록 스킵")
            return
        persist_assembled(gate.episode_id, final_path)
        return

    from engine.video.shorts_media import MediaResult, assemble_shorts
    from engine.video.weekly_pipeline import WEEKLY_TOTAL_SEC, load_weekly_scenario

    scenario = load_weekly_scenario(gate.episode_id)
    if scenario is None:
        logger.warning("[W6] shorts_scenario_json 없음 — W3 선행 필요 (정상 종료)")
        sys.exit(0)

    media = MediaResult(
        intro_image=out_dir / "images/P91.png",
        outro_image=out_dir / "images/P92.png",
        cut_paths=[out_dir / f"cuts/cut{i}.mp4" for i in range(1, len(scenario.cuts) + 1)],
    )
    missing = [
        str(p)
        for p in [media.intro_image, media.outro_image, *media.cut_paths]
        if not Path(p).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"[W6] 조립 입력 누락 ({_restore_failure_hint(gate.episode_id)}): {missing}"
        )

    final_path = assemble_shorts(scenario, media, out_dir / "assembly")
    logger.info(f"[W6] 조립 완료: {final_path} (규격 {WEEKLY_TOTAL_SEC}초)")

    if os.environ.get("DRY_RUN", "true").lower() == "true":
        logger.info("[W6] DRY_RUN — Supabase 기록 스킵")
        return
    persist_assembled(gate.episode_id, final_path)


def stage_weekly_notify():
    """W7: 텔레그램 검토본 발송 + release_at 기록 (hold-and-release). 파일럿은 발행 경로 미기록."""
    from datetime import UTC, datetime, timedelta

    from engine.video.weekly_pipeline import is_pilot_episode
    from engine.video.weekly_v2 import WeeklyScenarioV2, load_weekly_any

    gate = _weekly_gate_or_exit(for_generation=False)
    final_path = Path(f"output/videos/{gate.episode_id}/assembly/final_shorts.mp4")

    if os.environ.get("DRY_RUN", "true").lower() == "true":
        logger.info(
            f"[W7] DRY_RUN — 승인 요청 스킵 (id={gate.episode_id}, "
            f"final_exists={final_path.exists()})"
        )
        return

    scenario = load_weekly_any(gate.episode_id)
    if scenario is None or not final_path.exists():
        raise RuntimeError("[W7] 승인 요청 불가 — 시나리오/조립본 누락 (W3~W6 선행 필요)")

    from engine.common.supabase_client import icg_table
    from engine.publish.telegram_gate import send_approval_request

    row = None
    try:
        from engine.video.weekly_pipeline import _load_video_asset_row

        row = _load_video_asset_row(gate.episode_id)
    except Exception as exc:
        logger.warning(f"[W7] video_assets 조회 실패 (비용 0 표기로 진행): {exc}")

    scenario_label = (
        "WEEKLY_DIGEST_V2" if isinstance(scenario, WeeklyScenarioV2) else "WEEKLY_DIGEST"
    )
    cost = float((row or {}).get("veo_cost_usd") or 0.0)

    if is_pilot_episode(gate.episode_id):
        send_approval_request(
            video_path=str(final_path),
            episode_id=gate.episode_id,
            scenario_type=f"{scenario_label}_PILOT",
            cost_usd=cost,
            generation_ms=0,
            release_at_kst=None,
        )
        logger.info(f"[W7] 파일럿 검토본 발송 — release_at 미기록 (발행 대상 아님): {gate.episode_id}")
        return

    hold_hours = float(os.environ.get("PUBLISH_HOLD_HOURS", "6"))
    release_at = datetime.now(UTC) + timedelta(hours=hold_hours)
    release_kst = release_at.astimezone(ZoneInfo("Asia/Seoul")).strftime("%m/%d %H:%M")

    send_approval_request(
        video_path=str(final_path),
        episode_id=gate.episode_id,
        scenario_type=scenario_label,
        cost_usd=cost,
        generation_ms=0,
        release_at_kst=release_kst,
    )
    icg_table("video_assets").update(
        {"status": "pending_approval", "release_at": release_at.isoformat()}
    ).eq("episode_id", gate.episode_id).execute()
    logger.info(
        f"[W7] 승인 요청 발송 + pending_approval: {gate.episode_id} "
        f"(hold={hold_hours}h, release={release_at.isoformat()})"
    )


def stage_weekly_publish_x():
    """홀드 경과 후 주간 영상을 X에 게시하고 발행 이력을 즉시 기록한다 (v1/v2 공용)."""
    from engine.publish.x_video_publisher import (
        build_weekly_x_caption,
        publish_video_to_x,
    )
    from engine.video.weekly_pipeline import is_pilot_episode
    from engine.video.weekly_v2 import load_weekly_any, publish_fields

    episode_id = os.environ.get("TARGET_EPISODE_ID", "").strip()
    if not episode_id:
        episode_id = _weekly_gate_or_exit(for_generation=False).episode_id
    if not episode_id.startswith("icg-vw-"):
        raise RuntimeError(f"[X-WEEKLY] 주간 episode_id 형식이 아님: {episode_id}")
    if is_pilot_episode(episode_id):
        raise RuntimeError(f"[X-WEEKLY] 파일럿 에피소드는 게시 대상이 아니다: {episode_id}")

    scenario = load_weekly_any(episode_id)
    final_path = Path(f"output/videos/{episode_id}/assembly/final_shorts.mp4")
    if os.environ.get("DRY_RUN", "true").lower() == "true" and scenario is None:
        logger.info(
            "[W8] DRY_RUN — X 게시 스킵 (id=%s, scenario 없음, final_exists=%s)",
            episode_id,
            final_path.exists(),
        )
        return
    if scenario is None or not final_path.exists():
        raise RuntimeError("[W8] X 게시 불가 — 시나리오/조립본 누락 (W3~W6 선행 필요)")

    fields = publish_fields(scenario)
    caption = build_weekly_x_caption(fields["title"], fields["story_parts"])
    if os.environ.get("DRY_RUN", "true").lower() == "true":
        logger.info("[W8] DRY_RUN — X 게시 스킵: %s\n%s", final_path, caption)
        return

    # 기존 웹툰 발행과 같은 published_comics 이력으로 재실행 중복 게시를 차단한다.
    from engine.common.supabase_client import icg_table

    episode_no = int(episode_id.rsplit("-", 1)[-1])
    existing = (
        icg_table("published_comics")
        .select("tweet_id")
        .eq("publish_date", fields["episode_date"])
        .eq("episode_no", episode_no)
        .eq("comic_type", "WEEKLY_DIGEST")
        .limit(1)
        .execute()
    )
    if existing.data and existing.data[0].get("tweet_id"):
        logger.info(
            "[W8] X 중복 게시 스킵: episode=%s tweet_id=%s",
            episode_id,
            existing.data[0]["tweet_id"],
        )
        return

    result = publish_video_to_x(str(final_path), caption, episode_id)
    icg_table("published_comics").insert(
        {
            "publish_date": fields["episode_date"],
            "comic_type": "WEEKLY_DIGEST",
            "episode_no": episode_no,
            "risk_level": "MEDIUM",
            "tweet_id": result["tweet_id"],
            "cut_count": fields["cut_count"],
            "cost_usd": 0,
            "status": "published",
        }
    ).execute()
    logger.info("[W8] X 게시 완료: episode=%s tweet_id=%s", episode_id, result["tweet_id"])


def stage_verify_auth():
    """YouTube 자격증명 사전 검증 (업로드/쿼터 소모 없음).

    v1.7.1: 2026-08-29 run #33240050576 에서 발행 시점에야 invalid_grant 이
    드러났다. 생성($3.7) 전이나 발행 전에 미리 확인할 수 있어야 한다.
    """
    from engine.publish.youtube_shorts_publisher import verify_youtube_credentials

    result = verify_youtube_credentials()
    if not result["valid"]:
        raise RuntimeError(f"[VERIFY] YouTube 자격증명 무효: {result['detail']}")
    logger.info("[VERIFY] YouTube 자격증명 정상 — 발행 가능")


def stage_abort():
    """홀드 중인 에피소드의 자동 발행을 중단한다 (마스터 개입 경로).

    v1.7.0: hold-and-release 구조에서 '중단' 은 마스터의 유일한 개입 수단이므로
    별도 스테이지로 제공한다. status='aborted' 로 전이해 Publish 대상에서 제외.
    """
    from engine.common.supabase_client import icg_table
    from engine.video.shorts_pipeline import _load_video_asset_row

    target_date = _resolve_target_date()
    episode_id = _resolve_publish_episode_id(target_date)
    reason = os.environ.get("ABORT_REASON", "master_abort")

    row = _load_video_asset_row(episode_id)
    if not row:
        raise RuntimeError(f"[ABORT] video_assets 행 없음: {episode_id}")
    if row.get("youtube_video_id"):
        raise RuntimeError(
            f"[ABORT] 이미 발행됨({row['youtube_video_id']}) — 중단 불가. "
            f"YouTube Studio 에서 직접 삭제/비공개 처리하라"
        )

    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    if dry_run:
        logger.info(f"[ABORT] DRY_RUN — 중단 처리 스킵 (대상 status={row.get('status')})")
        return

    icg_table("video_assets").update(
        {"status": "aborted", "abort_reason": reason}
    ).eq("episode_id", episode_id).execute()
    logger.info(f"[ABORT] 자동 발행 중단 완료: {episode_id} (reason={reason})")


def stage_persist_final():
    """STEP S8: status='published' 확정 (youtube_video_id 기록 확인 후에만)."""
    from engine.common.supabase_client import icg_table
    from engine.video.shorts_pipeline import _load_video_asset_row

    target_date = _resolve_target_date()
    episode_id = _resolve_publish_episode_id(target_date)

    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    if dry_run:
        logger.info(f"[S8] DRY_RUN — 상태 확정 스킵: {episode_id}")
        return

    row = _load_video_asset_row(episode_id)
    if not row or not row.get("youtube_video_id"):
        raise RuntimeError(
            f"[S8] youtube_video_id 미기록 상태에서 published 확정 불가: {episode_id}"
        )

    icg_table("video_assets").update({"status": "published"}).eq(
        "episode_id", episode_id
    ).execute()
    logger.info(f"[S8] published 확정: {episode_id}")


STAGES = {
    "data": stage_data,
    "scenario": stage_scenario,
    "narrative": stage_narrative,
    "persist_init": stage_persist_init,
    "veo": stage_veo,
    "assembly": stage_assembly,
    "gate_notify": stage_gate_notify,
    "manual_prompt_notify": stage_manual_prompt_notify,
    "publish_telegram": stage_publish_telegram,
    "publish_x": stage_publish_x,
    "publish_shorts": stage_publish_shorts,
    "persist_final": stage_persist_final,
    "abort": stage_abort,
    "verify_auth": stage_verify_auth,
    # Weekly Digest (v2.0.0 정규 경로)
    "weekly_gate": stage_weekly_gate,
    "weekly_narrative": stage_weekly_narrative,
    "weekly_media": stage_weekly_media,
    "weekly_assembly": stage_weekly_assembly,
    "weekly_notify": stage_weekly_notify,
    "weekly_publish_x": stage_weekly_publish_x,
}


def main():
    parser = argparse.ArgumentParser(description="ICG Video Track runner")
    parser.add_argument(
        "--stage",
        required=True,
        choices=list(STAGES.keys()),
        help="Pipeline stage to execute",
    )
    args = parser.parse_args()

    # Setup logging FIRST (console + file)
    log_file = _setup_logging(args.stage)

    # Dump environment / runtime context
    _log_environment_info(args.stage, log_file)

    # Execute the stage with timing
    start_ts = time.monotonic()
    exit_code = 0
    try:
        STAGES[args.stage]()
    except SystemExit as exc:
        # stage_scenario raises SystemExit(0) on NO_BATTLE; preserve code
        exit_code = exc.code if isinstance(exc.code, int) else 1
        logger.info(f"[run_video_trailer] stage={args.stage} exited with code={exit_code}")
    except Exception:
        exit_code = 1
        logger.exception(f"[run_video_trailer] stage={args.stage} failed with exception")
    finally:
        elapsed = time.monotonic() - start_ts
        logger.info("-" * 72)
        logger.info(f"STAGE END: {args.stage} | elapsed={elapsed:.3f}s | exit_code={exit_code}")
        logger.info(f"Log file saved: {log_file.resolve()}")
        logger.info("=" * 72)
        # Ensure file handler flushes before exit
        logging.shutdown()

    if exit_code != 0:
        sys.exit(exit_code)
    logger.info(f"[run_video_trailer] stage={args.stage} 완료")


if __name__ == "__main__":
    main()
