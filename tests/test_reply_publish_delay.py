"""reply_engine — 발행 랜덤 딜레이(안티봇) + V-1 예산 즉시 저장 검증 (2026-08-17).

  D-1: live 첫 발행 직전 1회 랜덤 딜레이 0~600초 (env REPLY_PUBLISH_DELAY_MAX_SEC)
       - 발행 확정 건이 있을 때만 대기 (전량 스킵 실행은 대기 없음)
       - dry_run/shadow는 대기 없음
  V-1: live 발행마다 예산 즉시 upsert (timeout 킬 시 write_calls 집계 유실 방지)
"""

from __future__ import annotations

from datetime import UTC, datetime

import run_reply
from reply_engine import config, x_client
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet


def _capture_delay(monkeypatch):
    """random.randint 호출 인자 캡처 (딜레이 발동 여부·범위 검증용)."""
    calls: list[tuple] = []

    def _randint(a, b):
        calls.append((a, b))
        return 0

    monkeypatch.setattr(run_reply.random, "randint", _randint)
    return calls


def test_d1_config_default_and_env(monkeypatch):
    assert config.PUBLISH_START_DELAY_MAX_SEC == 600  # 기본 10분


def test_d1_delay_once_before_first_publish(monkeypatch):
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    calls = _capture_delay(monkeypatch)   # _quiet의 randint 패치를 캡처판으로 교체
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    _install_x(monkeypatch, published)

    result = run_reply.main()
    assert result["published"] == 1
    # (0, 600) 딜레이 호출이 정확히 1회 포함되어야 한다
    delay_calls = [c for c in calls if c == (0, 600)]
    assert len(delay_calls) == 1


def test_d1_no_delay_in_shadow_and_dry_run(monkeypatch):
    for mode in ("dry_run", "shadow"):
        _base_env(monkeypatch, mode)
        _quiet(monkeypatch)
        calls = _capture_delay(monkeypatch)
        mem = _MemStore()
        mem.install(monkeypatch)
        _install_x(monkeypatch, [])

        result = run_reply.main()
        assert result["published"] == 1, mode
        assert [c for c in calls if c == (0, 600)] == [], mode


def test_d1_no_delay_when_nothing_publishable(monkeypatch):
    """발행 확정 건이 없으면(게이트 전량 탈락) 딜레이도 없어야 한다."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    calls = _capture_delay(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    from reply_engine import generator

    banned = [{"id": "100", "reply": "지금 매수하세요"}]  # 금지어 → 전량 탈락
    monkeypatch.setattr(
        generator, "gemini_call", lambda **_k: {"success": True, "data": banned}
    )

    result = run_reply.main()
    assert result["published"] == 0
    assert [c for c in calls if c == (0, 600)] == []


def test_v1_budget_saved_per_write(monkeypatch):
    """live 2건 발행 시 예산 저장 = 발행마다 2회 + 종료 1회 = 3회."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published: list = []
    now = datetime.now(UTC)
    two_pass = [
        {"id": "400", "text": "@edt 감사합니다!", "author_id": "222",
         "conversation_id": "c1", "in_reply_to_user_id": "111", "created_at": now},
        {"id": "401", "text": "@edt 오늘도 감사해요", "author_id": "333",
         "conversation_id": "c2", "in_reply_to_user_id": "111", "created_at": now},
    ]
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_conversation_roots", lambda _c, ids: {i: "111" for i in ids}
    )
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": two_pass, "users": {},
                            "newest_id": "401", "error": None},
    )
    monkeypatch.setattr(
        x_client, "post_reply",
        lambda _c, text, tid: published.append((tid, text)) or f"resp-{tid}",
    )
    from reply_engine import generator

    monkeypatch.setattr(
        generator, "gemini_call",
        lambda **_k: {"success": True, "data": [
            {"id": "400", "reply": "감사합니다, 큰 힘이 돼요"},
            {"id": "401", "reply": "따뜻한 말씀 감사드립니다"},
        ]},
    )

    result = run_reply.main()
    assert result["published"] == 2
    assert len(mem.budget_saved) == 3
    # 마지막 저장분의 write_calls가 2인지 (집계 정확성)
    assert mem.budget_saved[-1]["write_calls"] == 2


def test_versions_bumped_d_series():
    """D-1/V-1 반영 버전 확인 (지침 5)."""
    assert run_reply.VERSION == "1.4.0"
    assert config.VERSION == "1.2.0"
