"""reply_engine — R-10(user_id 무결성) / B안(타인 스레드 응답) 검증.

R-10: X_MY_USER_ID 오등록 시 남의 멘션을 조회해 전건 OUT_OF_SCOPE로 걸러지며
      '에러 없이 0건'이 되는 경로를 차단한다. 커서 정체는 관측 지표로 남긴다.
B안 : in_reply_to_user_id == 나를 이미 통과한 대댓글(원 게시글만 타인 것)을
      회당 저상한 내에서 응답 대상에 포함한다 (마스터 승인, 2026-08-30).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import run_reply
from reply_engine import config
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet
from tests.test_reply_r_patch import _override_mentions, _tweet


def _install_roots(monkeypatch, mapping: dict[str, str]) -> None:
    """conversation_id -> root author 매핑 주입."""
    monkeypatch.setattr(
        run_reply.x_client, "fetch_conversation_roots",
        lambda _c, ids: {cid: mapping.get(cid) for cid in ids},
    )


# ---------------------------------------------------------------------------
# R-10 — user_id 무결성
# ---------------------------------------------------------------------------

def test_user_id_mismatch_warns_but_proceeds(monkeypatch):
    """불일치는 경고로 노출하되 중단하지 않는다 (B-1 규약: 변수 > 커서).

    계정 교체 시 불일치는 정상 시나리오이므로 중단시키면 교체가 불가능해진다.
    """
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "999999")   # 커서 캐시(111)와 불일치
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])

    result = run_reply.main()

    assert result["user_id_mismatch"] is True
    assert result["exit_reason"] != "EXIT_USER_ID_MISMATCH"


def test_matching_user_id_proceeds(monkeypatch):
    """일치하면 정상 진행한다 (가드가 정상 경로를 막지 않아야 한다)."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [])

    result = run_reply.main()

    assert result["user_id_mismatch"] is False


def test_no_cached_user_id_skips_guard(monkeypatch):
    """최초 실행(커서 캐시 없음)은 가드를 통과해야 한다."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "222")
    mem = _MemStore()
    mem.cursor = {"account": "kr_main", "since_id": "1"}   # my_user_id 없음
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [])

    result = run_reply.main()

    assert result["user_id_mismatch"] is False


def test_cursor_stale_hours_computed():
    """커서 정체 시간이 계산되고, 값이 없거나 깨져도 예외를 내지 않는다."""
    old = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    assert run_reply._cursor_stale_hours({"updated_at": old}) >= 29
    assert run_reply._cursor_stale_hours({"updated_at": "not-a-date"}) is None
    assert run_reply._cursor_stale_hours({}) is None
    assert run_reply._cursor_stale_hours(None) is None


def test_cursor_stale_hours_handles_z_suffix():
    """Supabase가 Z 접미 ISO를 반환해도 파싱된다."""
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    assert run_reply._cursor_stale_hours({"updated_at": old}) >= 4


# ---------------------------------------------------------------------------
# B안 — 타인 스레드 응답
# ---------------------------------------------------------------------------

def test_foreign_thread_admitted_within_run_cap(monkeypatch):
    """[opt-in] 타인 스레드 2건 중 회당 상한(1)만큼만 통과, 나머지 FOREIGN_THREAD_CAP."""
    _base_env(monkeypatch, "live")
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_ENABLED", True)
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [
        _tweet("f1", "A1", "cf1", "감사합니다"),
        _tweet("f2", "A2", "cf2", "고맙습니다"),
    ])
    _install_roots(monkeypatch, {"cf1": "OTHER", "cf2": "OTHER"})

    result = run_reply.main()

    assert result["foreign_thread_replies"] == 1
    assert result["skip_reasons"].get("FOREIGN_THREAD_CAP") == 1
    assert result["skip_reasons"].get("OUT_OF_SCOPE_THREAD") is None


def test_own_thread_not_counted_against_foreign_cap(monkeypatch):
    """[opt-in] 내 스레드 건은 타인 스레드 상한과 무관하게 전부 통과한다."""
    _base_env(monkeypatch, "live")
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_ENABLED", True)
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [
        _tweet("o1", "A1", "co1", "감사합니다"),
        _tweet("o2", "A2", "co2", "고맙습니다"),
        _tweet("f1", "A3", "cf1", "좋네요"),
    ])
    _install_roots(monkeypatch, {"co1": "111", "co2": "111", "cf1": "OTHER"})

    result = run_reply.main()

    assert result["candidates"] == 3
    assert result["foreign_thread_replies"] == 1


def test_foreign_thread_disabled_is_default(monkeypatch):
    """기본값(비활성)에서는 기존 P-1 동작(OUT_OF_SCOPE_THREAD)을 유지한다."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [_tweet("f1", "A1", "cf1", "감사합니다")])
    _install_roots(monkeypatch, {"cf1": "OTHER"})

    result = run_reply.main()

    assert result["skip_reasons"].get("OUT_OF_SCOPE_THREAD") == 1
    assert result["foreign_thread_replies"] == 0


def test_unverified_root_still_blocked(monkeypatch):
    """루트 조회 실패는 B안과 무관하게 보수적 스킵을 유지한다."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [_tweet("u1", "A1", "cu1", "감사합니다")])
    _install_roots(monkeypatch, {})   # 매핑 없음 → root_author None

    result = run_reply.main()

    assert result["skip_reasons"].get("THREAD_UNVERIFIED") == 1
    assert result["foreign_thread_replies"] == 0


def test_foreign_thread_flag_in_review(monkeypatch):
    """[opt-in] 리포트에서 타인 스레드 응답을 식별할 수 있어야 한다 (검수용)."""
    _base_env(monkeypatch, "live")
    monkeypatch.setattr(run_reply, "REPLY_FOREIGN_THREAD_ENABLED", True)
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [_tweet("f1", "A1", "cf1", "감사합니다")])
    _install_roots(monkeypatch, {"cf1": "OTHER"})

    result = run_reply.main()

    assert result["review"][0]["foreign_thread"] is True


def test_env_bool_parsing():
    """관대한 파싱 금지 — 'true'/'1'/'yes'만 참."""
    import os

    for raw, expected in [("true", True), ("TRUE", True), ("1", True),
                          ("yes", True), ("false", False), ("no", False),
                          ("dry_run", False)]:
        os.environ["_TEST_BOOL"] = raw
        assert config.env_bool("_TEST_BOOL", False) is expected, raw
    os.environ.pop("_TEST_BOOL", None)
    assert config.env_bool("_TEST_BOOL", True) is True     # 미설정 → 기본값
