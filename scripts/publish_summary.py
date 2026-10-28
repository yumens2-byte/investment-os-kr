#!/usr/bin/env python3
"""publish_summary.py — P3 배치: Reply Engine 실행 리포트(artifact)를 요약 MD로 영속화·커밋.

입력: 환경변수 RUN_ID / RUN_CONCLUSION / RUN_EVENT / RUN_CREATED + GH_TOKEN
동작: 1) reply-report-<run_id> artifact 검색·다운로드(같은 repo, GITHUB_TOKEN)
      2) reply_report_*.json 최신 1건 파싱 — published / skip_reasons / funnel / exit_reason
      3) docs/reply_summary/YYYY-MM/run_<run_id>.md 생성 (집계만 — 댓글 원문·개인정보 미포함)
      4) git add/commit/push (변경 없으면 스킵)
안전: 커밋 대상은 요약 MD 뿐 — 로그 원문·댓글 텍스트는 절대 커밋하지 않는다(격리 원칙).
실패 정책: artifact 부재·파싱 실패 시 이유를 남기고 정상 종료한다.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = "yumens2-byte/investment-os-kr"
OUT_DIR = Path("docs/reply_summary")
MAX_SKIP_ROWS = 12


def sh(*cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} 실패: {r.stderr.strip()[:200]}")
    return r.stdout


def artifact_zip_bytes(run_id: str, token: str) -> bytes:
    """run의 artifact 목록에서 reply-report-* 를 찾아 zip 바이트를 내려받는다."""
    import urllib.request
    hdr = {"Authorization": f"Bearer {token}",
           "Accept": "application/vnd.github+json"}
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/artifacts", headers=hdr)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    art = next((a for a in data.get("artifacts", [])
                if str(a.get("name", "")).startswith("reply-report-")), None)
    if art is None:
        raise FileNotFoundError("reply-report artifact 없음")
    req = urllib.request.Request(art["archive_download_url"], headers=hdr)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def parse_summary(zbytes: bytes) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    names = sorted(n for n in zf.namelist()
                   if "reply_report_" in n and n.endswith(".json"))
    if not names:
        raise FileNotFoundError("reply_report_*.json 없음")
    return json.loads(zf.read(names[-1]).decode("utf-8"))


def build_markdown(run_id: str, conclusion: str, event: str,
                   created: str, s: dict, note: str = "") -> str:
    finished = s.get("finished_at", "")
    mode = s.get("mode", "unknown")
    # bool, null, strings and negative counts are not publication evidence.
    def count(value):
        return value if type(value) is int and value >= 0 else "미확인"

    actual = count(s.get("actual_published", s.get("published"))) if mode == "live" else (
        0 if mode in {"shadow", "dry_run"} else "미확인"
    )
    simulated = count(s.get("simulated", s.get("published"))) if mode in {
        "shadow", "dry_run"
    } else (0 if mode == "live" else "미확인")
    raw_reasons = s.get("skip_reasons")
    reasons = raw_reasons if isinstance(raw_reasons, dict) else {}
    unknown = count(reasons.get("PUBLISH_UNKNOWN", 0)) if isinstance(
        raw_reasons, dict
    ) else "미확인"
    lines = [
        f"# run {run_id} — 요약",
        "",
        f"- 실행: {created or '?'} / 종료: {finished or '?'}",
        f"- 결론: {conclusion} · 트리거: {event} · exit_reason: {s.get('exit_reason', '-')}",
        f"- 모드: {mode} · 실행 코드: {s.get('commit_sha', '미확인')}",
        f"- **실제 발행: {actual} / 모의: {simulated} / 발행 결과 불명: {unknown}**",
        f"- 초안: {s.get('candidates', 0)} / 분류 통과: {s.get('classified_pass', 0)}",
        f"- 수집 포화: {s.get('collection_saturated', False)} "
        f"· oldest_id: {s.get('oldest_id', '-')}",
        "",
        "## skip 사유 분포",
        "",
    ]
    valid_reasons = {k: v for k, v in reasons.items() if type(v) is int and v >= 0}
    for k, v in sorted(valid_reasons.items(), key=lambda kv: -kv[1])[:MAX_SKIP_ROWS]:
        lines.append(f"- {k}: {v}")
    if not valid_reasons:
        lines.append("- (기록 없음)")
    funnel = s.get("funnel") or {}
    if funnel:
        lines += ["", "## funnel", ""]
        lines += [f"- {k}: {v}" for k, v in funnel.items()]
    if note:
        lines += ["", f"> {note}"]
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    run_id = os.environ.get("RUN_ID", "")
    conclusion = os.environ.get("RUN_CONCLUSION", "unknown")
    event = os.environ.get("RUN_EVENT", "unknown")
    created = os.environ.get("RUN_CREATED", "")
    token = os.environ.get("GH_TOKEN", "")
    note = ""

    try:
        if not run_id or not token:
            raise RuntimeError("RUN_ID 또는 GH_TOKEN 미설정")
        summary = parse_summary(artifact_zip_bytes(run_id, token))
    except Exception as e:                     # fail-open — 워크플로 실패로 만들지 않음
        summary, note = {}, f"요약 생성 부분 실패: {type(e).__name__}: {e}"

    day = (created or datetime.now(timezone.utc).isoformat())[:7]
    out = OUT_DIR / day
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"run_{run_id or 'unknown'}.md"
    path.write_text(build_markdown(run_id, conclusion, event, created, summary, note),
                    encoding="utf-8")

    sh("git", "config", "user.name", "reply-summary-bot")
    sh("git", "config", "user.email", "actions@users.noreply.github.com")
    sh("git", "add", "docs/reply_summary", check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        sh("git", "commit", "-m", f"reply summary: run {run_id} ({conclusion})")
        sh("git", "push")
        print(f"PUSHED: {path}")
    else:
        print("NO_CHANGES: 커밋 생략")


if __name__ == "__main__":
    main()
