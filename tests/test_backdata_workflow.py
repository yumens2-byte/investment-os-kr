"""DN2026_0090 backdata_card.yml 계약 테스트 (PyYAML 미의존 — 문자열 검사)."""

from __future__ import annotations

from pathlib import Path

YML = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "backdata_card.yml"
MODE_EXPR = (
    "${{ github.event_name == 'workflow_dispatch' && inputs.mode "
    "|| vars.BACKDATA_MODE || 'dry_run' }}"
)


def _text() -> str:
    return YML.read_text(encoding="utf-8")


def test_crons_and_job_conditions_match():
    text = _text()
    assert '- cron: "41 8 * * 1-5"' in text and '- cron: "7 1,9,17 * * *"' in text
    assert "github.event.schedule == '7 1,9,17 * * *'" in text      # metrics job
    assert "github.event.schedule == '41 8 * * 1-5'" in text        # post job
    assert "inputs.step == 'metrics'" in text and "inputs.step != 'metrics'" in text


def test_mode_uses_standard_expression_everywhere():
    text = _text()
    assert text.count(f"BACKDATA_MODE: {MODE_EXPR}") == 2


def test_paid_gemini_key_not_injected():
    assert "GEMINI_API_SUB_PAY_KEY" not in _text()


def test_inputs_pass_through_env():
    text = _text()
    assert 'run_backdata.py --step "$STEP"' in text
    assert "${{ inputs.step }}" not in text.split("run:")[-1]


def test_python_311_and_concurrency():
    text = _text()
    assert text.count('python-version: "3.11"') == 3
    assert "group: backdata-card" in text and "cancel-in-progress: false" in text
