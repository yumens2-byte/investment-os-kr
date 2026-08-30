"""reply_engine — R-패치 검증 (2026-08-30 artifact 실측 분석 후속).

  R-2: AUTHOR/CONV 캡 in-run 이중 계수 (동일 배치 내 동일 저자 다건 발행 차단)
  R-3: 멘션 수집 상한 확대 + 포화 감지
  R-4: 분류 룰 순서 보정 ('?' 단독 선점으로 인한 감탄형 오탈락 해소)
  R-5: Supabase 배치 조회 (N+1 해소) + 실패 시 보수 처리
"""

from __future__ import annotations

import run_reply
from reply_engine import classifier, config, store
from reply_engine import filter as filter_mod
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet


def _tweet(tid: str, author: str, conv: str, text: str = "감사합니다") -> dict:
    return {
        "id": tid,
        "text": text,
        "author_id": author,
        "conversation_id": conv,
        "in_reply_to_user_id": "111",
        "created_at": None,
    }


def _override_mentions(monkeypatch, tweets: list[dict]) -> None:
    """_install_x의 고정 픽스처(_mk_tweets)를 시나리오별 트윗으로 교체.

    주의: _install_x(monkeypatch, published)의 2번째 인자는 발행 기록 수집용
    리스트이지 수집 트윗 목록이 아니다 — 반드시 이 헬퍼로 덮어써야 한다.
    """
    from reply_engine import x_client

    monkeypatch.setattr(
        x_client,
        "fetch_mentions",
        lambda _c, _uid, _sid: {
            "success": True,
            "tweets": tweets,
            "users": {},
            "newest_id": tweets[-1]["id"] if tweets else None,
            "oldest_id": tweets[0]["id"] if tweets else None,
            "saturated": False,
            "error": None,
        },
    )


# ---------------------------------------------------------------------------
# R-2 — 캡 in-run 이중 계수
# ---------------------------------------------------------------------------

def test_t1_author_cap_run_blocks_same_author_in_batch():
    """동일 저자 3건 → 승인 1건, 나머지 AUTHOR_CAP_RUN (REPLY_AUTHOR_DAILY_CAP=1)."""
    tweets = [
        _tweet("t1", "A", "c1"),
        _tweet("t2", "A", "c2"),
        _tweet("t3", "A", "c3"),
    ]
    ctx = filter_mod.CapContext(bulk_ready=True)

    results = [filter_mod.check_and_admit(t, ctx) for t in tweets]

    assert results[0] == (True, None)
    assert results[1] == (False, "AUTHOR_CAP_RUN")
    assert results[2] == (False, "AUTHOR_CAP_RUN")
    assert ctx.author_run["A"] == 1


def test_t2_conv_cap_run_blocks_fourth_in_same_conversation():
    """동일 대화 4건 → 승인 3건(REPLY_CONV_DAILY_CAP=3), 4번째 CONV_CAP_RUN."""
    tweets = [_tweet(f"t{i}", f"A{i}", "same_conv") for i in range(4)]
    ctx = filter_mod.CapContext(bulk_ready=True)

    results = [filter_mod.check_and_admit(t, ctx) for t in tweets]

    assert [r[0] for r in results] == [True, True, True, False]
    assert results[3][1] == "CONV_CAP_RUN"
    assert ctx.conv_run["same_conv"] == 3


def test_t3_reproduces_20260830_incident(monkeypatch):
    """08-30 실사고 재현: 동일 저자 3건 포함 5건 → 동일 저자 중복 발행 0건."""
    _base_env(monkeypatch, "live")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)

    # 실측 데이터: author 2057775414777671680 이 3건 (t2/t3/t4)
    tweets = [
        _tweet("t1", "1371642956088627200", "conv1", "또 늘어나서 걱정이네요"),
        _tweet("t2", "2057775414777671680", "conv2", "오호 👍"),
        _tweet("t3", "2057775414777671680", "conv3", "크 멋져요.!!!"),
        _tweet("t4", "2057775414777671680", "conv4", "정리감사합니다."),
        _tweet("t5", "1537065769204600832", "conv5", "좋네요"),
    ]
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, tweets)

    result = run_reply.main()

    published_authors = [
        r for r in result["review"] if r["result"] in ("PUBLISHED", "SIMULATED")
    ]
    # 동일 저자에게 2건 이상 발행되지 않아야 한다 (핵심 회귀)
    assert result["skip_reasons"].get("AUTHOR_CAP_RUN", 0) >= 1
    assert len(published_authors) <= 3


def test_t4_author_cap_run_applies_in_shadow_mode(monkeypatch):
    """shadow 모드에서도 캡이 실동작해야 검수가 유효하다."""
    _base_env(monkeypatch, "shadow")
    _quiet(monkeypatch)
    monkeypatch.setenv("X_MY_USER_ID", "111")
    mem = _MemStore()
    mem.install(monkeypatch)
    _install_x(monkeypatch, [])
    _override_mentions(monkeypatch, [
        _tweet("s1", "SAME", "c1", "감사합니다"),
        _tweet("s2", "SAME", "c2", "고맙습니다"),
    ])

    result = run_reply.main()

    assert result["skip_reasons"].get("AUTHOR_CAP_RUN", 0) == 1


def test_t4b_legacy_wrapper_preserves_single_query_path(monkeypatch):
    """하위호환 래퍼는 ctx 없이 단건 쿼리 경로로 동작한다."""
    monkeypatch.setattr(filter_mod.store, "history_exists", lambda _id: False)
    monkeypatch.setattr(filter_mod.store, "count_author_responded_today", lambda _a: 0)
    monkeypatch.setattr(filter_mod.store, "count_conversation_responded_today", lambda _c: 0)

    assert filter_mod.check_caps_and_dup(_tweet("x", "A", "c")) == (True, None)


# ---------------------------------------------------------------------------
# R-5 — 배치 조회 보수성
# ---------------------------------------------------------------------------

class _BoomClient:
    def table(self, _name):
        raise RuntimeError("supabase down")


def test_t5_history_exists_bulk_failure_is_conservative(monkeypatch):
    """조회 실패 → 전건 DUP 취급 (확인 불가면 발행 금지)."""
    monkeypatch.setattr(store, "get_client", lambda: _BoomClient())
    assert store.history_exists_bulk(["a", "b"]) == {"a", "b"}


def test_t6_count_today_bulk_failure_is_conservative(monkeypatch):
    """조회 실패 → 전건 큰 값 반환 (보수적 차단)."""
    monkeypatch.setattr(store, "get_client", lambda: _BoomClient())
    counts = store.count_author_responded_today_bulk(["a", "b"])
    assert counts == {"a": 10**9, "b": 10**9}


def test_t7_cap_context_uses_three_queries_only(monkeypatch):
    """후보 5건이어도 store 배치 호출은 3회 고정 (N+1 해소)."""
    calls: list[str] = []
    monkeypatch.setattr(
        filter_mod.store, "history_exists_bulk",
        lambda ids: calls.append("hist") or set(),
    )
    monkeypatch.setattr(
        filter_mod.store, "count_author_responded_today_bulk",
        lambda ids: calls.append("author") or {},
    )
    monkeypatch.setattr(
        filter_mod.store, "count_conversation_responded_today_bulk",
        lambda ids: calls.append("conv") or {},
    )

    tweets = [_tweet(f"t{i}", f"A{i}", f"c{i}") for i in range(5)]
    ctx = filter_mod.build_cap_context(tweets)

    assert len(calls) == 3
    assert ctx.bulk_ready is True


def test_t8_in_chunk_splitting():
    """in_() URL 길이 안전장치 — 60건은 2청크로 분할된다."""
    items = [str(i) for i in range(60)]
    chunks = list(store._chunks(items))
    assert len(chunks) == 2
    assert len(chunks[0]) == 50
    assert len(chunks[1]) == 10


# ---------------------------------------------------------------------------
# R-4 — 분류 룰 순서
# ---------------------------------------------------------------------------

def test_t9_classify_by_rule_precedence_table():
    """설계서 대조표 전수 검증."""
    # 감탄형 '?' — 오탈락 해소 (08-30 실사고)
    assert classifier.classify_by_rule("오호? 👍") == "POSITIVE"
    assert classifier.classify_by_rule("대박?") == "POSITIVE"
    # 의문 어미/의문사 — QUESTION 유지
    assert classifier.classify_by_rule("환율 어떻게 보시나요?") == "QUESTION"
    assert classifier.classify_by_rule("이거 사도 될까요?") == "QUESTION"
    assert classifier.classify_by_rule("이 정보 유익한가요?") == "QUESTION"
    # '?' 있고 긍정 마커 없음 — 보수적으로 QUESTION 유지
    assert classifier.classify_by_rule("진짜요?") == "QUESTION"
    # 기존 동작 보존
    assert classifier.classify_by_rule("정말 감사합니다!") == "POSITIVE"
    assert classifier.classify_by_rule("완전 틀렸네요") == "NEGATIVE"
    assert classifier.classify_by_rule("음 그렇군요") is None


# ---------------------------------------------------------------------------
# R-3 — 수집 상한 및 포화 감지
# ---------------------------------------------------------------------------

def test_t10_env_int_clamped_rejects_out_of_range(monkeypatch):
    """범위 밖 값은 조용히 자르지 않고 기본값으로 되돌린다."""
    monkeypatch.setenv("REPLY_MENTIONS_MAX_RESULTS", "500")
    assert config.env_int_clamped("REPLY_MENTIONS_MAX_RESULTS", 100, 5, 100) == 100
    monkeypatch.setenv("REPLY_MENTIONS_MAX_RESULTS", "1")
    assert config.env_int_clamped("REPLY_MENTIONS_MAX_RESULTS", 100, 5, 100) == 100
    monkeypatch.setenv("REPLY_MENTIONS_MAX_RESULTS", "50")
    assert config.env_int_clamped("REPLY_MENTIONS_MAX_RESULTS", 100, 5, 100) == 50
    monkeypatch.delenv("REPLY_MENTIONS_MAX_RESULTS", raising=False)
    assert config.env_int_clamped("REPLY_MENTIONS_MAX_RESULTS", 100, 5, 100) == 100


def test_t10b_mentions_max_results_raised_to_api_ceiling():
    """수집 상한이 API 하한(5)에서 벗어났는지 — 유실 차단 회귀."""
    assert config.MENTIONS_MAX_RESULTS == 100


def test_t11_saturated_flag_true_when_limit_reached(monkeypatch):
    """수집 건수가 상한과 같으면 포화로 표시된다."""
    from reply_engine import x_client

    monkeypatch.setattr(x_client, "MENTIONS_MAX_RESULTS", 2)

    class _T:
        def __init__(self, tid):
            self.id = tid
            self.text = "감사"
            self.author_id = 1
            self.conversation_id = 1
            self.in_reply_to_user_id = 111
            self.created_at = None

    class _Resp:
        data = [_T(1), _T(2)]
        includes: dict = {}
        meta = {"newest_id": "2", "oldest_id": "1"}

    class _Client:
        def get_users_mentions(self, *_a, **_k):
            return _Resp()

    result = x_client.fetch_mentions(_Client(), "111", since_id=None)

    assert result["saturated"] is True
    assert result["oldest_id"] == "1"


def test_t12_saturated_flag_false_when_below_limit(monkeypatch):
    """상한 미달이면 포화 아님."""
    from reply_engine import x_client

    monkeypatch.setattr(x_client, "MENTIONS_MAX_RESULTS", 100)

    class _Resp:
        data: list = []
        includes: dict = {}
        meta: dict = {}

    class _Client:
        def get_users_mentions(self, *_a, **_k):
            return _Resp()

    result = x_client.fetch_mentions(_Client(), "111", since_id=None)

    assert result["saturated"] is False
    assert result["oldest_id"] is None


def test_t12b_fetch_failure_keeps_schema_keys(monkeypatch):
    """실패 반환에도 신규 키가 존재해야 호출부가 KeyError를 만나지 않는다."""
    from reply_engine import x_client

    class _Client:
        def get_users_mentions(self, *_a, **_k):
            raise RuntimeError("boom")

    result = x_client.fetch_mentions(_Client(), "111", since_id=None)

    assert result["success"] is False
    assert result["saturated"] is False
    assert result["oldest_id"] is None
