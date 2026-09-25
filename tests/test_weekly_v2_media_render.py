"""tests/test_weekly_v2_media_render.py — v2 미디어(I2V·Motion QA·원장)·렌더·발행 연동."""

from __future__ import annotations

import inspect
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

from engine.video import weekly_media as wm
from engine.video import weekly_render as wr
from engine.video import weekly_v2 as v2
from tests.test_weekly_v2 import _valid_payload

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _scenario():
    return v2.WeeklyScenarioV2(**_valid_payload())


# ── Veo 단가 / I2V ───────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [("", 0.15), ("0.10", 0.10), ("abc", 0.15), ("-1", 0.15)])
def test_unit_price_env(monkeypatch, raw, expected):
    from engine.video.veo_client import unit_price_per_sec

    monkeypatch.setenv("VEO_UNIT_PRICE_USD", raw)
    assert unit_price_per_sec() == pytest.approx(expected)


def test_estimate_v2_cost_follows_unit_price(monkeypatch):
    monkeypatch.setenv("VEO_UNIT_PRICE_USD", "0.10")
    # 16s × 0.10 + 4장 × 0.039 + 0.10
    assert wm.estimate_v2_cost(_scenario()) == pytest.approx(1.6 + 0.156 + 0.10)
    monkeypatch.setenv("VEO_UNIT_PRICE_USD", "0.15")
    assert wm.estimate_v2_cost(_scenario()) == pytest.approx(2.4 + 0.156 + 0.10)


class _FakeOperation:
    done = True

    def __init__(self, out_bytes=b"mp4data"):
        video = types.SimpleNamespace(save=lambda p: Path(p).write_bytes(out_bytes))
        self.response = types.SimpleNamespace(generated_videos=[types.SimpleNamespace(video=video)])


def _veo_client_with_fake(captured: dict):
    from engine.video.veo_client import VeoClient

    client = object.__new__(VeoClient)

    def generate_videos(**kwargs):
        captured.update(kwargs)
        return _FakeOperation()

    client.client = types.SimpleNamespace(
        models=types.SimpleNamespace(generate_videos=generate_videos),
        files=types.SimpleNamespace(download=lambda file: None),
        operations=types.SimpleNamespace(get=lambda op: op),
    )
    return client


def test_i2v_sends_image_and_negative_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("VEO_UNIT_PRICE_USD", "0.10")
    frame = tmp_path / "k.png"
    frame.write_bytes(b"\x89PNG....")
    captured: dict = {}
    client = _veo_client_with_fake(captured)

    res = client.generate_image_to_video(
        prompt="orbit as the knight charges",
        start_frame_path=str(frame),
        output_path=str(tmp_path / "s.mp4"),
        duration_sec=4,
        resolution="720p",
        aspect_ratio="9:16",
        negative_prompt="static shot",
    )
    assert captured["image"].mime_type == "image/png"
    assert captured["image"].image_bytes == b"\x89PNG...."
    assert captured["config"].duration_seconds == 4
    assert captured["config"].negative_prompt == "static shot"
    assert res["mode"] == "I2V" and res["cost_usd"] == pytest.approx(0.4)


def test_i2v_rejects_missing_frame(tmp_path):
    from engine.video.veo_client import VeoGenerationError

    client = _veo_client_with_fake({})
    with pytest.raises(VeoGenerationError, match="start frame"):
        client.generate_image_to_video("p", str(tmp_path / "none.png"), str(tmp_path / "o.mp4"))


def test_t2v_path_unchanged_no_image(tmp_path):
    captured: dict = {}
    client = _veo_client_with_fake(captured)
    res = client.generate_text_to_video("prompt text", str(tmp_path / "t.mp4"), duration_sec=6)
    assert "image" not in captured
    assert res["mode"] == "T2V"


# ── Motion QA ────────────────────────────────────────────────


def test_parse_freeze_ratio_open_ended_freeze():
    """정지가 끝까지 이어지면 freeze_end 가 없다 (로컬 ffmpeg 실측 형식)."""
    assert wm.parse_freeze_ratio("lavfi.freezedetect.freeze_start: 0\n", 3.0) == 1.0
    log = "freeze_start: 1.0\nfreeze_duration: 1.0\nfreeze_end: 2.0\n"
    assert wm.parse_freeze_ratio(log, 4.0) == 0.25
    assert wm.parse_freeze_ratio("", 4.0) == 0.0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 필요")
def test_measure_motion_distinguishes_still_and_moving(tmp_path):
    moving = tmp_path / "moving.mp4"
    still = tmp_path / "still.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc2=size=320x568:rate=24", "-t", "2", "-pix_fmt", "yuv420p", str(moving)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=gray:size=320x568:rate=24", "-t", "2", "-pix_fmt", "yuv420p", str(still)],
        check=True,
    )
    m, s = wm.measure_motion(moving), wm.measure_motion(still)
    assert m["freeze_ratio"] == 0.0
    assert s["freeze_ratio"] >= 0.9
    assert m["motion_score"] > s["motion_score"]


class _FakeVeo:
    def __init__(self, fail_first: int = 0):
        self.calls: list[dict] = []
        self.fail_first = fail_first

    def generate_image_to_video(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_first:
            raise RuntimeError("transient")
        Path(kwargs["output_path"]).write_bytes(b"x")
        return {"cost_usd": 0.4}


def _install_media_fakes(monkeypatch, veo, motions):
    monkeypatch.setitem(
        sys.modules, "engine.video.veo_client",
        types.SimpleNamespace(VeoClient=lambda: veo, unit_price_per_sec=lambda: 0.10),
    )
    budget_calls: list[float] = []
    monkeypatch.setitem(
        sys.modules, "engine.video.budget_checker",
        types.SimpleNamespace(check_before_generation=lambda estimated_cost_usd: budget_calls.append(estimated_cost_usd)),
    )
    seq = iter(motions)
    monkeypatch.setattr(wm, "measure_motion", lambda p: dict(next(seq)))
    return budget_calls


def _keyframed(tmp_path):
    result = wm.V2MediaResult()
    for i in range(1, 5):
        kf = tmp_path / f"k{i}.png"
        kf.write_bytes(b"png")
        result.keyframes.append(kf)
    return result


def test_generate_shots_regenerates_frozen_shot_once(tmp_path, monkeypatch):
    monkeypatch.setenv("WEEKLY_FREEZE_MAX", "0.25")
    monkeypatch.setenv("WEEKLY_MOTION_REGEN_MAX", "1")
    veo = _FakeVeo()
    good = {"duration_sec": 4, "freeze_ratio": 0.0, "motion_score": 0.1, "frames": 96}
    frozen = {"duration_sec": 4, "freeze_ratio": 0.8, "motion_score": 0.0, "frames": 96}
    budget = _install_media_fakes(monkeypatch, veo, [frozen, good, good, good, good])

    result = wm.generate_shots(_scenario(), tmp_path, _keyframed(tmp_path), dry_run=False)

    assert len(veo.calls) == 5  # 4샷 + 재생성 1
    assert result.regenerations == 1
    assert result.veo_cost_usd == pytest.approx(2.0)  # 5회 × $0.4 (재생성 비용 누락 금지)
    assert result.motion[0]["freeze_ratio"] == 0.0 and result.motion[0]["regenerated"] == 1
    assert len(budget) == 1 and budget[0] == pytest.approx(0.4 + 0.4)
    assert all(NEG in c["negative_prompt"] for c in veo.calls for NEG in ("static shot", "text"))
    assert all(v2.MOTION_SUFFIX in c["prompt"] for c in veo.calls)
    assert [p.name for p in result.shots] == [f"shot{i}.mp4" for i in range(1, 5)]


def test_generate_shots_keeps_better_take_when_regen_is_worse(tmp_path, monkeypatch):
    monkeypatch.setenv("WEEKLY_MOTION_REGEN_MAX", "1")
    veo = _FakeVeo()
    a = {"duration_sec": 4, "freeze_ratio": 0.5, "motion_score": 0.01, "frames": 96}
    b = {"duration_sec": 4, "freeze_ratio": 0.9, "motion_score": 0.0, "frames": 96}
    ok = {"duration_sec": 4, "freeze_ratio": 0.0, "motion_score": 0.1, "frames": 96}
    _install_media_fakes(monkeypatch, veo, [a, b, ok, ok, ok])
    result = wm.generate_shots(_scenario(), tmp_path, _keyframed(tmp_path), dry_run=False)
    assert result.motion[0]["freeze_ratio"] == 0.5
    assert not (tmp_path / "shots" / "shot1_regen1.mp4").exists()


def test_generate_shots_retries_transient_api_error(tmp_path, monkeypatch):
    veo = _FakeVeo(fail_first=1)
    ok = {"duration_sec": 4, "freeze_ratio": 0.0, "motion_score": 0.1, "frames": 96}
    _install_media_fakes(monkeypatch, veo, [ok] * 4)
    result = wm.generate_shots(_scenario(), tmp_path, _keyframed(tmp_path), dry_run=False)
    assert len(veo.calls) == 5 and len(result.shots) == 4
    assert result.veo_cost_usd == pytest.approx(1.6)  # 실패 호출은 과금 미가산


def test_generate_shots_dry_run_makes_dummies(tmp_path):
    result = wm.generate_shots(_scenario(), tmp_path, _keyframed(tmp_path), dry_run=True)
    assert len(result.shots) == 4 and result.veo_cost_usd == 0.0


def test_expected_paths_match_generation_names(tmp_path):
    kfs, shots = wm.expected_paths(tmp_path)
    assert [p.name for p in kfs] == ["P101.png", "P102.png", "P103.png", "P104.png"]
    assert [p.name for p in shots] == ["shot1.mp4", "shot2.mp4", "shot3.mp4", "shot4.mp4"]


# ── 원장 누적 (덮어쓰기 금지) ─────────────────────────────────


class _Table:
    def __init__(self, store, fail_manifest=False):
        self.store = store
        self.fail_manifest = fail_manifest
        self.payload = None

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, *_):
        return self

    def execute(self):
        if self.fail_manifest and "media_manifest_json" in self.payload:
            raise Exception("column media_manifest_json does not exist in schema cache")
        self.store.append(self.payload)
        return types.SimpleNamespace(data=[])


def _patch_db(monkeypatch, prior_cost, fail_manifest=False):
    store: list[dict] = []
    monkeypatch.setitem(
        sys.modules, "engine.common.supabase_client",
        types.SimpleNamespace(icg_table=lambda name: _Table(store, fail_manifest)),
    )
    from engine.video import weekly_pipeline

    monkeypatch.setattr(weekly_pipeline, "_load_video_asset_row", lambda e: {"veo_cost_usd": prior_cost})
    return store


def test_record_spend_accumulates(monkeypatch):
    store = _patch_db(monkeypatch, "0.0612")
    total = wm.record_spend("icg-vw-2026-W38-P01", 1.2, note="partial", manifest={"a": 1})
    assert total == pytest.approx(1.2612)
    assert store[-1]["veo_cost_usd"] == pytest.approx(1.2612)
    assert store[-1]["media_manifest_json"]["note"] == "partial"


def test_persist_media_accumulates_and_falls_back_without_manifest_column(monkeypatch, tmp_path):
    store = _patch_db(monkeypatch, 1.0, fail_manifest=True)
    media = wm.V2MediaResult(
        shots=[tmp_path / f"shot{i}.mp4" for i in range(1, 5)], image_cost_usd=0.156, veo_cost_usd=1.6
    )
    wm.persist_v2_media("icg-vw-2026-W38-001", media)
    saved = store[-1]
    assert saved["status"] == "media_generated"
    assert saved["veo_cost_usd"] == pytest.approx(2.756)
    assert "media_manifest_json" not in saved  # 컬럼 미적용 환경 폴백
    assert saved["cut1_video_uri"].endswith("shot1.mp4")


# ── 렌더 ─────────────────────────────────────────────────────


def test_timeline_accounts_for_transitions():
    slots, total = wr.build_timeline([4.0, 4.0, 4.0, 4.0])
    assert total == pytest.approx(15.7)
    assert [s.start_sec for s in slots] == [0.0, 3.9, 7.8, 11.7]
    assert slots[-1].visible_end_sec == pytest.approx(15.7)
    slots2, total2 = wr.build_timeline([4.04, 3.96, 4.0, 4.0])  # 실제 길이 편차 반영
    assert slots2[2].start_sec == pytest.approx(4.04 + 3.96 - 0.2)
    assert total2 == pytest.approx(16.0 - 0.3)


def test_timeline_rejects_too_short_shot():
    with pytest.raises(wr.WeeklyRenderError):
        wr.build_timeline([4.0, 0.15])


def test_ass_sanitizes_model_text(tmp_path):
    payload = _valid_payload()
    payload["shots"][0]["caption"] = r"{\pos(0,0)\c&H0000FF&}해킹"
    payload["hook_title"] = "제목{\\b1}"
    sc = v2.WeeklyScenarioV2(**payload)
    slots, total = wr.build_timeline([4.0] * 4)
    text = wr.build_v2_ass(sc, slots, total, tmp_path / "s.ass").read_text(encoding="utf-8")
    events = [line for line in text.splitlines() if line.startswith("Dialogue")]
    # 오버라이드 태그 문법({ } \\)이 제거되어 문자로만 남는다 (렌더러가 해석 불가)
    assert "\\pos" not in text and "{\\c" not in text
    assert any("pos(0,0)c&H0000FF&해킹" in e for e in events)
    assert events[0].endswith("}제목b1")  # 고정 팝 태그 뒤 정제된 제목
    assert any(wr.DISCLAIMER_TEXT in e for e in events)


def _make_clip(path: Path, with_audio: bool, freq: int = 440):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=24"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000"]
    cmd += ["-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-ac", "2"] if with_audio else []
    cmd.append(str(path))
    subprocess.run(cmd, check=True)


def _probe(path: Path, entries: str) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 필요")
@pytest.mark.parametrize("with_audio", [True, False])
def test_render_single_pass_no_freeze(tmp_path, monkeypatch, with_audio):
    monkeypatch.setenv("WEEKLY_USE_VEO_AUDIO", "true")
    monkeypatch.setenv("WEEKLY_RENDER_PRESET", "ultrafast")  # 테스트 시간 단축 (운영 기본 slow)
    shots = []
    for i in range(1, 5):
        p = tmp_path / f"shot{i}.mp4"
        _make_clip(p, with_audio, 200 * i)
        shots.append(p)
    narr = tmp_path / "n.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=900:sample_rate=24000",
         "-t", "2", str(narr)],
        check=True,
    )
    final, report = wr.render_weekly_v2(
        _scenario(), shots, tmp_path / "out", dry_run=False, narrations=[(0.15, narr), (4.05, narr)]
    )
    assert final.exists()
    assert report["encode_passes"] == 1 and report["veo_audio"] is with_audio
    assert float(_probe(final, "format=duration")) == pytest.approx(15.7, abs=0.08)
    dims = _probe(final, "stream=width,height").split()
    assert dims[:2] == ["1080", "1920"]
    assert wm.measure_motion(final)["freeze_ratio"] == 0.0  # 정지 구간 0초
    assert _probe(final, "stream=codec_type").split().count("audio") == 1


# ── 발행 연동 / 워크플로 ─────────────────────────────────────


def test_weekly_caption_uses_episode_id_for_abort():
    from engine.publish.telegram_gate import _build_caption

    cap = _build_caption("icg-vw-2026-W39-001", "WEEKLY_DIGEST_V2", 1.9, 0, 20.0, release_at_kst="09/28 13:13")
    assert "episode_id=<code>icg-vw-2026-W39-001</code>" in cap
    assert "operation_mode=<code>abort</code>" in cap
    assert "-2026-W39-" not in cap.replace("icg-vw-2026-W39-001", "")


def test_pilot_caption_has_no_publish_instruction():
    from engine.publish.telegram_gate import _build_caption

    cap = _build_caption("icg-vw-2026-W38-P01", "WEEKLY_DIGEST_V2_PILOT", 1.9, 0, 20.0)
    assert "파일럿" in cap and "operation_mode" not in cap


def test_daily_caption_unchanged():
    from engine.publish.telegram_gate import _build_caption

    cap = _build_caption("icg-v-2026-04-22-001", "ALLIANCE", 3.72, 190000, 23.4, release_at_kst="04/22 09:17")
    assert "target_date=<code>2026-04-22</code>" in cap


def test_publish_x_and_shorts_reject_pilot(monkeypatch):
    from scripts import run_video_trailer as rvt

    monkeypatch.setenv("TARGET_EPISODE_ID", "icg-vw-2026-W38-P01")
    with pytest.raises(RuntimeError, match="파일럿"):
        rvt.stage_weekly_publish_x()
    with pytest.raises(RuntimeError, match="파일럿"):
        rvt.stage_publish_shorts()


def test_weekly_stages_branch_on_format():
    from scripts import run_video_trailer as rvt

    for fn in (rvt.stage_weekly_narrative, rvt.stage_weekly_media):
        assert "weekly_format()" in inspect.getsource(fn)
    # 조립/알림은 저장된 시나리오 형식으로 분기 (주중 포맷 전환에도 재조립 안전)
    assert "isinstance(saved, WeeklyScenarioV2)" in inspect.getsource(rvt.stage_weekly_assembly)
    assert "load_weekly_any" in inspect.getsource(rvt.stage_weekly_notify)
    assert "load_weekly_any" in inspect.getsource(rvt.stage_publish_shorts)


def test_weekly_workflow_v2_wiring():
    wf = yaml.safe_load(Path(".github/workflows/run_weekly_shorts.yml").read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)
    inputs = on["workflow_dispatch"]["inputs"]
    env = wf["jobs"]["weekly_digest"]["env"]
    assert {"pilot", "pilot_tag"} <= set(inputs)
    assert env["VIDEO_BUDGET_USD_MONTHLY"] == "16"
    assert "vars.WEEKLY_FORMAT || 'v1'" in env["WEEKLY_FORMAT"]
    assert "inputs.pilot == 'true' && 'v2'" in env["WEEKLY_FORMAT"]
    assert "VEO_UNIT_PRICE_USD" in env and "WEEKLY_USE_VEO_AUDIO" in env


def test_trailer_workflow_accepts_episode_id():
    wf = yaml.safe_load(Path(".github/workflows/run_video_trailer.yml").read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)
    assert "episode_id" in on["workflow_dispatch"]["inputs"]
    assert wf["jobs"]["video_pipeline"]["env"]["TARGET_EPISODE_ID"] == "${{ inputs.episode_id || '' }}"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 필요")
def test_render_uses_video_track_length_when_audio_is_longer(tmp_path, monkeypatch):
    """리뷰 재현(High): 영상 4.0s/오디오 4.06s 클립 → 컨테이너 길이로 offset 을 잡으면
    최종 영상이 4.08s 로 잘렸다. 영상 트랙 기준 + 사후 길이 검증으로 차단."""
    monkeypatch.setenv("WEEKLY_USE_VEO_AUDIO", "true")
    monkeypatch.setenv("WEEKLY_RENDER_PRESET", "ultrafast")
    shots = []
    for i in range(1, 5):
        p = tmp_path / f"shot{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000",
             "-map", "0:v", "-map", "1:a", "-t:v", "4", "-t:a", "4.06",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(p)],
            check=True,
        )
        shots.append(p)
    assert wr.probe_duration(shots[0]) < 4.06  # 영상 트랙(오디오 4.06s 아님)
    final, report = wr.render_weekly_v2(_scenario(), shots, tmp_path / "out", dry_run=False, narrations=[])
    # 타임라인은 영상 트랙 합(-전환) 기준이며, 결과 영상이 잘리지 않아야 한다 (과거: 4.08s)
    assert report["total_sec"] > 15.5
    assert report["video_sec"] == pytest.approx(report["total_sec"], abs=0.12)
    assert report["audio_sec"] == pytest.approx(report["total_sec"], abs=0.12)


def test_render_length_mismatch_blocks_publish(tmp_path, monkeypatch):
    """사후 검증: ffmpeg 가 조용히 잘라도 길이 불일치면 예외 (발행 차단)."""
    shots = [tmp_path / f"s{i}.mp4" for i in range(4)]
    for p in shots:
        p.write_bytes(b"x")
    monkeypatch.setattr(wr, "probe_duration", lambda p: 4.08 if Path(p).name == "final_shorts.mp4" else 4.0)
    monkeypatch.setattr(wr, "has_audio", lambda p: True)
    monkeypatch.setattr(wr, "_ffprobe_value", lambda p, a: "15.7")
    monkeypatch.setattr(wr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stderr=""))
    with pytest.raises(wr.WeeklyRenderError, match="길이 불일치"):
        wr.render_weekly_v2(_scenario(), shots, tmp_path / "out", dry_run=False, narrations=[])


def test_failed_attempts_are_audited_not_billed(tmp_path, monkeypatch):
    veo = _FakeVeo(fail_first=2)
    ok = {"duration_sec": 4, "freeze_ratio": 0.0, "motion_score": 0.1, "frames": 96}
    _install_media_fakes(monkeypatch, veo, [ok] * 4)
    result = wm.generate_shots(_scenario(), tmp_path, _keyframed(tmp_path), dry_run=False)
    assert result.failed_attempts == 2
    assert result.manifest()["failed_attempts"] == 2
    assert result.veo_cost_usd == pytest.approx(1.6)


def test_narrative_failure_carries_spent_cost(monkeypatch):
    from tests.test_weekly_v2 import _ep, _gate, _install_fake_anthropic

    _install_fake_anthropic(monkeypatch, ["not json"] * 3)
    eps = [_ep("2026-09-15", "CHAR_HERO_005", "CHAR_VILLAIN_001"), _ep("2026-09-16", "CHAR_HERO_002", "CHAR_VILLAIN_001")]
    with pytest.raises(Exception) as info:
        v2.generate_v2_scenario(_gate(eps), dry_run=False)
    assert getattr(info.value, "cost_usd", 0) == pytest.approx(3 * (1000 * 3 + 500 * 15) / 1_000_000)
