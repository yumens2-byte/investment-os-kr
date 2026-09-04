"""reply_engine — P-1(스코프)/P-2(에코·질문) 회귀 테스트 (2026-08-18 shadow 실측 사고 기반).

실측 사고: 마스터가 타인 글에 단 축하 댓글의 대댓글("@tiger18272 축하해주셔서 감사합니다♡♡")이
스코프를 통과했고, 생성 답글("축하해주셔서 진심으로 감사합니다.♡")이 댓글을 미러링해
역할이 반전됨. → P-1(루트 검증) + P-2(GATE_ECHO/GATE_QUESTION/프롬프트 v2)로 해소.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import run_reply
from reply_engine import gate, generator, x_client
from tests.test_reply_pipeline import _base_env, _MemStore, _quiet

_INCIDENT_COMMENT = "@tiger18272 축하해주셔서 감사합니다♡♡"
_INCIDENT_REPLY = "축하해주셔서 진심으로 감사합니다.♡"


# ---------------------------------------------------------------------------
# P-2: GATE_ECHO / GATE_QUESTION (단위)
# ---------------------------------------------------------------------------

def test_p2_incident_echo_blocked():
    """실측 사고 문구 그대로 — 반드시 GATE_ECHO 탈락."""
    ok, reason = gate.check_reply(_INCIDENT_REPLY, [], comment_text=_INCIDENT_COMMENT)
    assert not ok and reason == "GATE_ECHO"


def test_p2_normal_thanks_for_thanks_passes():
    """감사 댓글에 '저야말로' 방향 답글은 통과해야 한다 (오탈락 방지)."""
    ok, reason = gate.check_reply(
        "저야말로 감사합니다 🙂", [], comment_text="@tiger18272 감사합니다 잘 봤어요"
    )
    assert ok, reason


def test_p2_echo_skipped_without_comment():
    """comment_text 미전달(하위호환) 시 에코 검증 생략."""
    ok, _ = gate.check_reply(_INCIDENT_REPLY, [])
    assert ok  # 에코 축이 없으면 다른 게이트는 통과하는 문구


def test_p2_question_mark_blocked():
    """실측 사례: '정말요? 저도 놀랐어요.' — 물음표 탈락."""
    assert gate.check_reply("정말요? 저도 놀랐어요.", [])[1] == "GATE_QUESTION"
    assert gate.check_reply("그런가요？", [])[1] == "GATE_QUESTION"  # 전각 물음표


def test_p2_pool_passes_new_gates():
    """내장 풀 전 문구가 신규 게이트(에코 포함)를 통과하는지 — 일반 댓글 대비."""
    for text in generator._POOL_POSITIVE + generator._POOL_SUPPORTIVE:
        ok, reason = gate.check_reply(text, [], comment_text="오늘 브리핑 잘 봤습니다 감사합니다")
        assert ok, (text, reason)


def test_p2_prompt_v2_contains_new_rules(monkeypatch):
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {"success": False, "data": None, "error": "capture"}

    monkeypatch.setattr(generator, "gemini_call", _capture)
    generator.generate_batch([{"id": "a", "text": _INCIDENT_COMMENT, "label": "POSITIVE"}])

    prompt = captured["prompt"]
    for required in ("저야말로 감사합니다", "상황어", "아는 척", "과장 수식 금지", "역할이 뒤집힘"):
        assert required in prompt, required


# ---------------------------------------------------------------------------
# P-1: 대화 루트 검증 (단위 + E2E)
# ---------------------------------------------------------------------------

def test_p1_fetch_conversation_roots_parsing():
    t1 = SimpleNamespace(id=100, author_id=111)
    t2 = SimpleNamespace(id=200, author_id=999)
    resp = SimpleNamespace(data=[t1, t2])

    class _Client:
        def get_tweets(self, ids, **_kwargs):
            return resp

    roots = x_client.fetch_conversation_roots(_Client(), ["100", "200", "100"])
    assert roots == {"100": "111", "200": "999"}


def test_p1_fetch_conversation_roots_failure_returns_none():
    class _Client:
        def get_tweets(self, ids, **_kwargs):
            raise RuntimeError("429")

    assert x_client.fetch_conversation_roots(_Client(), ["100"]) is None
    assert x_client.fetch_conversation_roots(_Client(), []) == {}  # 빈 입력은 호출 없이 {}


def _mixed_tweets():
    now = datetime.now(UTC)
    mk = lambda tid, conv: {  # noqa: E731
        "id": tid, "text": "@tiger18272 축하해주셔서 감사합니다♡♡", "author_id": f"u{tid}",
        "conversation_id": conv, "in_reply_to_user_id": "111", "created_at": now,
    }
    return [mk("500", "my_conv"), mk("501", "other_conv"), mk("502", "deleted_conv")]


def _install_mixed(monkeypatch, roots_result):
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": _mixed_tweets(), "users": {},
                            "newest_id": "502", "error": None},
    )
    monkeypatch.setattr(
        x_client, "fetch_conversation_roots", lambda _c, ids: roots_result
    )


def test_p1_e2e_scope_verdicts(monkeypatch):
    """내 글만 통과, 타인 글=OUT_OF_SCOPE_THREAD, 루트 삭제=THREAD_UNVERIFIED."""
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_mixed(monkeypatch, {"my_conv": "111", "other_conv": "999"})  # deleted_conv 누락

    result = run_reply.main()
    assert result["candidates"] == 1                       # my_conv 1건만
    assert result["skip_reasons"]["OUT_OF_SCOPE_THREAD"] == 1
    assert result["skip_reasons"]["THREAD_UNVERIFIED"] == 1
    # 통과한 1건도 에코 게이트 대상 — 미러링 답글이면 발행 0 (프롬프트 실패 가정 풀 fallback은 통과)
    assert result["budget"]["read_calls"] == 2             # mentions 1 + 루트 1


def test_p1_e2e_root_lookup_total_failure(monkeypatch):
    """루트 조회 API 실패 시 전량 보수적 스킵 (응답 0건)."""
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_mixed(monkeypatch, None)  # fetch_conversation_roots → None

    result = run_reply.main()
    assert result["candidates"] == 0
    assert result["skip_reasons"]["THREAD_UNVERIFIED"] == 3
    assert result["published"] == 0


def test_p1_e2e_incident_full_regression(monkeypatch):
    """실측 사고 완전 재현: 타인 글 스레드의 축하 대댓글 + 미러링 생성 →
    P-1에서 이미 차단되어 생성 단계 자체에 도달하지 않아야 한다."""
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    now = datetime.now(UTC)
    incident_tweet = [{
        "id": "2089493697151009214", "text": _INCIDENT_COMMENT, "author_id": "u_conv",
        "conversation_id": "2089280657486913650", "in_reply_to_user_id": "111",
        "created_at": now,
    }]
    monkeypatch.setattr(x_client, "get_x_client", lambda: object())
    monkeypatch.setattr(
        x_client, "fetch_mentions",
        lambda _c, _u, _s: {"success": True, "tweets": incident_tweet, "users": {},
                            "newest_id": "2089493697151009214", "error": None},
    )
    # 루트(2089280657486913650)는 타인 글
    monkeypatch.setattr(
        x_client, "fetch_conversation_roots",
        lambda _c, ids: {"2089280657486913650": "someone_else"},
    )

    result = run_reply.main()
    assert result["published"] == 0
    assert result["skip_reasons"]["OUT_OF_SCOPE_THREAD"] == 1
    assert mem.history == {}  # 범위 밖 건은 이력 미기록 (R-4 정책과 동일)


def test_versions_bumped_p_series():
    """P-1/P-2 반영 버전 확인 (지침 5)."""
    from reply_engine import config

    assert run_reply.VERSION == "1.5.0"
    assert x_client.VERSION == "1.4.0"
    assert gate.VERSION == "1.1.1"
    assert generator.VERSION == "1.4.0"
    assert config.VERSION == "1.4.0"
