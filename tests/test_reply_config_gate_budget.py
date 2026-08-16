"""reply_engine — config / gate / budget 단위 테스트."""

from __future__ import annotations

from reply_engine import config
from reply_engine.budget import BudgetGuard
from reply_engine.gate import check_reply, jaccard_similarity

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_is_enabled_requires_exact_true(monkeypatch):
    monkeypatch.setenv("REPLY_ENABLED", "true")
    assert config.is_enabled() is True
    monkeypatch.setenv("REPLY_ENABLED", "false")
    assert config.is_enabled() is False
    monkeypatch.setenv("REPLY_ENABLED", "")
    assert config.is_enabled() is False
    monkeypatch.delenv("REPLY_ENABLED", raising=False)
    assert config.is_enabled() is False


def test_get_mode_fail_safe(monkeypatch):
    monkeypatch.setenv("REPLY_MODE", "live")
    assert config.get_mode() == "live"
    monkeypatch.setenv("REPLY_MODE", "SHADOW")
    assert config.get_mode() == "shadow"
    monkeypatch.setenv("REPLY_MODE", "prod")  # 인식 불가 → dry_run 강등
    assert config.get_mode() == "dry_run"
    monkeypatch.delenv("REPLY_MODE", raising=False)
    assert config.get_mode() == "dry_run"


def test_get_cost_per_call_fallback(monkeypatch):
    monkeypatch.delenv("X_READ_COST_KRW", raising=False)
    monkeypatch.delenv("X_WRITE_COST_KRW", raising=False)
    assert config.get_cost_per_call() == (None, None)

    monkeypatch.setenv("X_READ_COST_KRW", "12.5")
    monkeypatch.setenv("X_WRITE_COST_KRW", "abc")  # 파싱 불가
    read_cost, write_cost = config.get_cost_per_call()
    assert read_cost == 12.5
    assert write_cost is None

    monkeypatch.setenv("X_WRITE_COST_KRW", "-1")  # 음수 거부
    assert config.get_cost_per_call()[1] is None


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def test_gate_length_and_empty():
    assert check_reply("", []) == (False, "GATE_EMPTY")
    assert check_reply("가" * 41, []) == (False, "GATE_LENGTH")
    ok, reason = check_reply("감사합니다, 좋은 하루 되세요", [])
    assert ok and reason is None


def test_gate_non_kr_and_banned_and_format():
    assert check_reply("ありがとう", [])[1] == "GATE_NON_KR"
    assert check_reply("谢谢", [])[1] == "GATE_NON_KR"
    assert check_reply("지금 매수 타이밍입니다", [])[1] == "GATE_BANNED_WORD"
    assert check_reply("감사합니다 #국장", [])[1] == "GATE_FORMAT"
    assert check_reply("감사합니다 https://a.b", [])[1] == "GATE_FORMAT"
    assert check_reply("@user 감사합니다", [])[1] == "GATE_FORMAT"


def test_gate_similarity_blocks_duplicates():
    prev = ["좋게 봐주셔서 감사합니다"]
    assert check_reply("좋게 봐주셔서 감사합니다", prev)[1] == "GATE_SIMILARITY"
    assert check_reply("좋게 봐주셔서 감사합니다!", prev)[1] == "GATE_SIMILARITY"
    ok, _ = check_reply("의견 나눠주셔서 감사해요", prev)
    assert ok


def test_jaccard_bounds():
    assert jaccard_similarity("", "") == 0.0
    assert jaccard_similarity("감사합니다", "감사합니다") == 1.0
    assert 0.0 <= jaccard_similarity("감사합니다", "동의합니다") < 1.0


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------

def _row(read=0, write=0, gemini=0, cost=0.0):
    return {
        "budget_date": "2026-08-17",
        "read_calls": read,
        "write_calls": write,
        "gemini_calls": gemini,
        "est_cost_krw": cost,
    }


def test_budget_cost_mode_blocks_over_limit(monkeypatch):
    monkeypatch.setenv("DAILY_BUDGET_KRW", "1000")
    monkeypatch.setenv("X_READ_COST_KRW", "100")
    monkeypatch.setenv("X_WRITE_COST_KRW", "300")

    guard = BudgetGuard(_row(cost=950.0))
    assert guard.cost_mode is True
    assert guard.can_read() is False    # 950 + 100 > 1000
    assert guard.can_write() is False   # 950 + 300 > 1000

    guard2 = BudgetGuard(_row(cost=600.0))
    assert guard2.can_read() is True
    guard2.record_read()
    assert guard2.row["read_calls"] == 1
    assert guard2.row["est_cost_krw"] == 700.0
    assert guard2.can_write() is True   # 700 + 300 = 1000 (경계 허용)
    guard2.record_write()
    assert guard2.can_write() is False  # 1000 + 300 > 1000


def test_budget_count_mode_fallback(monkeypatch):
    monkeypatch.delenv("X_READ_COST_KRW", raising=False)
    monkeypatch.delenv("X_WRITE_COST_KRW", raising=False)

    guard = BudgetGuard(_row(read=7, write=4))
    assert guard.cost_mode is False
    assert guard.can_read() is True
    guard.record_read()
    assert guard.can_read() is False    # 8 도달
    assert guard.can_write() is True
    guard.record_write()
    assert guard.can_write() is False   # 5 도달
