"""reply_engine — G-1(프롬프트 v3: 도메인 맥락 + 의도 방향 분기) 검증 (2026-08-20).

실측 오독 사례:
  "가자 돈복사!!!" (시장 환호) → "응원 감사해요" (나를 응원한 것으로 오독)
  "슈드 잘 가네요 ㅋ" (종목 시황 관찰) → "언급 감사해요" (하지 않은 행동에 감사)
"""

from __future__ import annotations

from reply_engine import generator


def _capture_prompt(monkeypatch) -> dict:
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {"success": False, "data": None, "error": "capture"}

    monkeypatch.setattr(generator, "gemini_call", _capture)
    return captured


def test_g1_prompt_contains_domain_context(monkeypatch):
    """도메인 컨텍스트(투자 은어)와 실측 오독 2건이 few-shot으로 고정되어야 한다."""
    captured = _capture_prompt(monkeypatch)
    generator.generate_batch([{"id": "a", "text": "가자 돈복사!!!", "label": "POSITIVE"}])
    prompt = captured["prompt"]

    for required in (
        "돈복사", "슈드", "종목 애칭",          # 도메인 컨텍스트
        "시장·종목에 대한 관찰/환호",            # 방향 분기 [B]
        "같은 마음입니다",                       # 공감 인사 방향
        "의도 오독",                             # 실측 사례 1 (돈복사)
        "하지 않은 행동에 감사",                 # 실측 사례 2 (슈드) + 라벨 규칙
        "불확실하면 라벨 없이",                  # 의도 라벨 접두 제한
    ):
        assert required in prompt, required


def test_g1_prompt_keeps_prior_rules(monkeypatch):
    """v2에서 확정된 기존 규칙(C-1/P-2)이 v3에서도 유지되어야 한다 (회귀 방지)."""
    captured = _capture_prompt(monkeypatch)
    generator.generate_batch([{"id": "a", "text": "감사합니다", "label": "POSITIVE"}])
    prompt = captured["prompt"]

    for required in (
        "절대 금지", "행동 안내", "질문이어도 답하지 말고",   # C-1
        "저야말로 감사합니다", "상황어", "아는 척",           # P-2
        "과장 수식 금지", "역할이 뒤집힘",                    # P-2 few-shot
        "서로 다른 단어로 시작",                              # F-2
    ):
        assert required in prompt, required


def test_g1_market_cheer_replies_pass_gates():
    """[B] 방향 공감 인사 문구들이 게이트(지시형/에코 포함)를 통과하는지."""
    from reply_engine import gate

    for reply, comment in (
        ("같은 마음입니다 🙂", "가자 돈복사!!!"),
        ("함께 지켜보시죠 🙂", "슈드 잘 가네요 ㅋ"),
    ):
        ok, reason = gate.check_reply(reply, [], comment_text=comment)
        assert ok, (reply, reason)


def test_g1_version_bumped():
    """G-1 반영 버전 확인 (지침 5)."""
    assert generator.VERSION == "1.1.2"
