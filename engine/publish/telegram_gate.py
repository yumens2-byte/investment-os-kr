"""
Telegram Gate — Master personal approval gate (PAUSE stage).

Purpose:
  Send the final rendered 24s video to master's personal Telegram chat
  with inline buttons: [approve / regenerate / abort].
  Publishing (STEP 8V) executes ONLY after master explicit approval.

Flow:
  1. After STEP 7V (assembly), this module uploads the final mp4 to master.
  2. Master taps one of 3 buttons → Telegram callback_query triggers GitHub
     webhook (or separate workflow with manual input) → publish stage runs.
  3. If no response within 6 hours, workflow expires and logs to Supabase.

Requirements:
  TELEGRAM_BOT_TOKEN (env)  : bot token from @BotFather
  MASTER_CHAT_ID (env)      : master's personal Telegram user_id
                              (get via @userinfobot)

Telegram API:
  - sendVideo endpoint: max 50MB per bot upload (our 24s mp4 ≈ 20MB, OK)
  - inline_keyboard callback_data format: "{action}:{episode_id}"
"""
import logging
import os
from pathlib import Path

VERSION = "1.4.0"
logger = logging.getLogger(__name__)

# Telegram sendVideo body limit (bot API, standard server)
MAX_VIDEO_SIZE_MB = 50
# Caption max length
MAX_CAPTION_LEN = 1024


class TelegramGateError(Exception):
    """Raised when gate notification fails."""


def send_approval_request(
    video_path: str,
    episode_id: str,
    scenario_type: str,
    cost_usd: float,
    generation_ms: int,
    release_at_kst: str | None = None,
) -> dict:
    """
    Send final video to master with inline approval buttons.

    Args:
        video_path       : Final rendered mp4 path
        episode_id       : icg.video_assets episode_id
        scenario_type    : e.g. "ONE_VS_ONE"
        cost_usd         : Total Veo cost for this episode
        generation_ms    : Total generation time

    Returns:
        dict with keys: message_id, chat_id, sent_at

    Raises:
        TelegramGateError on upload failure.
    """
    if not Path(video_path).exists():
        raise TelegramGateError(f"video_path not found: {video_path}")

    size_mb = Path(video_path).stat().st_size / 1024 / 1024
    if size_mb > MAX_VIDEO_SIZE_MB:
        raise TelegramGateError(
            f"Video size {size_mb:.1f}MB exceeds Telegram bot limit {MAX_VIDEO_SIZE_MB}MB"
        )

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("MASTER_CHAT_ID")
    if not bot_token:
        raise TelegramGateError("TELEGRAM_BOT_TOKEN env not set")
    if not chat_id:
        raise TelegramGateError("MASTER_CHAT_ID env not set")

    caption = _build_caption(
        release_at_kst=release_at_kst,
        episode_id=episode_id,
        scenario_type=scenario_type,
        cost_usd=cost_usd,
        generation_ms=generation_ms,
        size_mb=size_mb,
    )
    reply_markup = _build_approval_keyboard(episode_id)

    logger.info(
        f"[telegram_gate] v{VERSION} sending approval request: "
        f"episode={episode_id} size={size_mb:.1f}MB"
    )
    logger.debug(
        "[telegram_gate] prepared payload: caption_len=%d, buttons=%d",
        len(caption),
        len(reply_markup["inline_keyboard"]),
    )

    # v1.1.0: TODO 실장 — Telegram Bot API sendVideo (requests 동기 호출).
    # python-telegram-bot(async) 대신 requests 사용: run_video_trailer 의
    # _send_telegram_text 와 동일한 동기 패턴 (GitHub Actions 단발 실행에 적합).
    if os.environ.get("DRY_RUN", "true").lower() == "true":
        logger.info("[telegram_gate] DRY_RUN — sendVideo 스킵")
        return {
            "message_id": None,
            "chat_id": chat_id,
            "sent_at": None,
            "status": "dry_run",
        }

    import json as _json

    import requests

    url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
    form = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": "true",
    }
    # v1.2.0: 빈 키보드는 전송하지 않는다 (Telegram 이 빈 markup 을 거부할 수 있음).
    if reply_markup.get("inline_keyboard"):
        form["reply_markup"] = _json.dumps(reply_markup)

    with open(video_path, "rb") as f:
        response = requests.post(
            url,
            data=form,
            files={"video": (Path(video_path).name, f, "video/mp4")},
            timeout=120,
        )

    payload = response.json()
    if not payload.get("ok"):
        raise TelegramGateError(
            f"sendVideo 실패: HTTP {response.status_code} — {payload.get('description')}"
        )

    msg = payload["result"]
    logger.info(
        "[telegram_gate] approval request sent: message_id=%s chat_id=%s",
        msg.get("message_id"),
        chat_id,
    )
    return {
        "message_id": msg.get("message_id"),
        "chat_id": chat_id,
        "sent_at": str(msg.get("date")),
        "status": "sent",
    }


def _build_caption(
    episode_id: str,
    scenario_type: str,
    cost_usd: float,
    generation_ms: int,
    size_mb: float,
    release_at_kst: str | None = None,
) -> str:
    """Build the master-facing caption with metadata."""
    header = (
        "🎬 <b>ICG Shorts — 자동 발행 예정</b>"
        if release_at_kst
        else "🎬 <b>ICG Video Trailer — 승인 대기</b>"
    )
    caption = (
        f"{header}\n\n"
        f"<b>Episode</b>: <code>{episode_id}</code>\n"
        f"<b>Scenario</b>: {scenario_type}\n"
        f"<b>Cost</b>: ${cost_usd:.4f}\n"
        f"<b>Time</b>: {generation_ms / 1000:.1f}s\n"
        f"<b>Size</b>: {size_mb:.2f} MB\n\n"
    )
    # v1.4.0 (2026-09-25): 주간 ID(icg-vw-YYYY-Www-NNN)는 날짜 슬라이스가 성립하지 않는다
    # (구 로직은 '-2026-W38-' 를 안내해 abort 가 불가능했다). 주간은 episode_id 로 안내한다.
    if episode_id.startswith("icg-vw-"):
        return _build_weekly_caption(caption, episode_id, release_at_kst)

    target_date = episode_id[6:16]
    if release_at_kst:
        # hold-and-release: 기본 발행. 마스터가 개입해야 중단된다.
        caption += (
            f"\n⏰ <b>{release_at_kst} KST 자동 발행</b>\n"
            f"그대로 두면 유튜브에 발행됩니다.\n\n"
            f"<b>중단하려면</b>\n"
            f"Actions → Run Video Trailer → Run workflow\n"
            f"  operation_mode=<code>abort</code>\n"
            f"  dry_run=<code>false</code>\n"
            f"  target_date=<code>{target_date}</code>"
        )
    else:
        caption += (
            f"\n<b>승인 방법</b>\n"
            f"Actions → Run Video Trailer → Run workflow\n"
            f"  operation_mode=<code>publish</code>\n"
            f"  dry_run=<code>false</code>\n"
            f"  confirm=<code>YES</code>\n"
            f"  target_date=<code>{target_date}</code>\n"
            f"발행하지 않으려면 아무 것도 하지 않으면 됩니다."
        )
    if len(caption) > MAX_CAPTION_LEN:
        caption = caption[: MAX_CAPTION_LEN - 3] + "..."
    return caption


def _build_weekly_caption(caption: str, episode_id: str, release_at_kst: str | None) -> str:
    """주간 다이제스트 안내 — 파일럿은 발행 없음, 정규는 episode_id 기준 중단 안내."""
    import re as _re

    if _re.search(r"-P\d{2}$", episode_id):
        caption += (
            "\n🧪 <b>파일럿 렌더 — 자동 발행 대상 아님</b>\n"
            "품질 확인용 검토본입니다. 발행 경로(release_at)는 기록되지 않습니다."
        )
    elif release_at_kst:
        caption += (
            f"\n⏰ <b>{release_at_kst} KST 자동 발행</b>\n"
            "그대로 두면 유튜브·X에 발행됩니다.\n\n"
            "<b>중단하려면</b>\n"
            "Actions → Run Video Trailer → Run workflow\n"
            "  operation_mode=<code>abort</code>\n"
            "  dry_run=<code>false</code>\n"
            f"  episode_id=<code>{episode_id}</code>"
        )
    else:
        caption += (
            "\n<b>발행 방법</b> (YouTube + X)\n"
            "Actions → Publish Shorts → Run workflow\n"
            f"  episode_id=<code>{episode_id}</code>\n"
            "  dry_run=<code>false</code>"
        )
    if len(caption) > MAX_CAPTION_LEN:
        caption = caption[: MAX_CAPTION_LEN - 3] + "..."
    return caption


def _build_approval_keyboard(episode_id: str) -> dict:
    """
    v1.2.0: 인라인 버튼을 제공하지 않는다 (빈 keyboard).

    근거: GitHub Actions 는 단발 실행이라 콜백을 수신할 상시 프로세스가 없다.
    버튼을 노출하면 눌러도 아무 일이 일어나지 않아 오히려 혼란을 준다
    (2026-08-29 마스터 리포트). 승인은 publish 모드 수동 실행으로 수행하며,
    실행 방법은 캡션에 안내한다. episode_id 는 시그니처 호환을 위해 유지.
    """
    _ = episode_id
    return {"inline_keyboard": []}


def handle_callback(callback_data: str) -> dict:
    """
    Parse callback_data from master tap.

    Args:
        callback_data: "approve:ICG-V-2026-04-19-001" style string

    Returns:
        dict with keys: action, episode_id, is_valid
    """
    try:
        action, episode_id = callback_data.split(":", 1)
    except ValueError:
        return {"action": None, "episode_id": None, "is_valid": False}

    if action not in ("approve", "regenerate", "abort"):
        return {"action": action, "episode_id": episode_id, "is_valid": False}

    return {"action": action, "episode_id": episode_id, "is_valid": True}
