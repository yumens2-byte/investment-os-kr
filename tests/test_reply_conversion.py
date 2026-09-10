"""자연스러운 답글과 운영 퍼널 개선 회귀 테스트."""

import json

import run_reply
from reply_engine import classifier, generator


def test_short_unambiguous_reactions_do_not_depend_on_ai():
    for text in ("ㅋㅋㅋ", "ㅎㅎ", "ㅇㅈ", "맞아요!", "그러게요"):
        assert classifier.classify_by_rule(text) == "SUPPORTIVE_NEUTRAL"


def test_questions_still_take_priority_over_supportive_markers():
    assert classifier.classify_by_rule("맞아요?") == "QUESTION"
    assert classifier.classify_by_rule("왜 맞아요") == "QUESTION"


def test_fallback_candidates_are_complete_and_deterministic():
    first = generator.fallback_candidates("POSITIVE", "tweet-1")
    second = generator.fallback_candidates("POSITIVE", "tweet-1")
    assert first == second
    assert first[0] == generator.pick_fallback("POSITIVE", "tweet-1")
    assert set(first) == set(generator._POOL_POSITIVE)


def test_report_exposes_conversion_funnel(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    summary = {
        "collected": 10,
        "candidates": 5,
        "classified_pass": 4,
        "published": 3,
    }
    run_reply._write_report(summary)

    report_path = next((tmp_path / "logs").glob("reply_report_*.json"))
    report = json.loads(report_path.read_text())
    assert report["funnel"] == {
        "candidate_rate": 0.5,
        "classification_pass_rate": 0.8,
        "publish_rate_of_collected": 0.3,
        "publish_rate_of_pass": 0.75,
    }
    assert report["finished_at"]
