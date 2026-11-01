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


def test_cron_is_weekday_1741_kst():
    assert '- cron: "41 8 * * 1-5"' in _text()


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
