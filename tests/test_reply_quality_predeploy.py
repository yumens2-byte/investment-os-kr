"""Incident replay and adversarial regressions; all external calls are mocked."""
from datetime import UTC, datetime

import pytest

import run_reply
from reply_engine import classifier, gate, generator, policy, telemetry, x_client
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet


@pytest.mark.parametrize('reply', [
    '저녁 맛있게 드셨군요', '맛있게 먹었다니 다행이에요',
    '주말 즐겁게 보내셨네요', '푹 쉬셨다니 다행이에요',
    '좋은 꿈 꾸고 잘 잤다니 다행이에요',
])
def test_wish_does_not_assert_completed_experience(reply):
    assert gate.check_reply(reply, [], '맛저하시고 푹 쉬세요') == (
        False, 'GATE_ROLE_REVERSAL')


@pytest.mark.parametrize('comment,reply', [
    ('저녁 맛있게 드세용', '인사 남겨주셔서 감사해요 😊'),
    ('저녁 맛있게 먹었어요', '맛있게 드셨군요'),
    ('좋은 주말 보내세요', '고마워요, 오늘도 무탈한 하루 되세요'),
])
def test_valid_wish_and_experience_are_distinct(comment, reply):
    assert gate.check_reply(reply, [], comment) == (True, None)


@pytest.mark.parametrize('comment', [
    '좋은 하루 보내세요', '투자 너무 어렵네요 ㅠ',
    '저는 B할래요',
    '금리와 환율, 유가가 함께 움직여서 지표별로 시간대를 나눠 살펴보면 좋을 것 같다는 의견입니다',
])
def test_generic_reply_cannot_replace_explicit_intent(comment):
    assert gate.check_reply('댓글 잘 봤어요 🙂', [], comment) == (False, 'GATE_INTENT')
    item = {'id': '1', 'text': comment, 'label': 'SUPPORTIVE_NEUTRAL'}
    chosen, source, reason = gate.choose_reply(
        ['댓글 잘 봤어요 🙂'], generator.contextual_fallbacks(item), [], comment)
    assert chosen is not None and source == 'TEMPLATE_FALLBACK'
    assert reason == 'GATE_INTENT'
    assert gate.check_reply(chosen, [], comment) == (True, None)


def test_worry_pool_does_not_invent_market_conditions():
    comment = '육아가 힘들어요 ㅠ'
    for reply in policy.SAFE_POOLS['WORRY']:
        assert not any(word in reply for word in ('시장', '변동', '숫자', '장이라'))
        assert gate.check_reply(reply, [], comment) == (True, None)


def test_pool_exhaustion_still_abstains():
    item = {'id': '1', 'text': '푹 쉬세요', 'label': 'POSITIVE'}
    pool = generator.contextual_fallbacks(item)
    chosen, source, reason = gate.choose_reply(
        ['댓글 잘 봤어요 🙂'], pool, list(pool), item['text'])
    assert (chosen, source, reason) == (None, 'NONE', 'GATE_INTENT')


def test_quality_metrics_separate_modes_and_non_evaluated_rows():
    base = {'reply_tweet_id': '1', 'quality_evaluated': True,
            'source': 'TEMPLATE_FALLBACK', 'result': 'SIMULATED',
            'draft_gate_reason': 'GATE_INTENT'}
    rows = [base, dict(base), {'reply_tweet_id': '2', 'result': 'RUN_CAP'}]
    metrics = telemetry.quality_metrics(rows, 'shadow')
    assert metrics['evaluated'] == metrics['selected'] == 1
    assert metrics['actual_sources'] == {}
    assert metrics['simulated_sources'] == {'TEMPLATE_FALLBACK': 1}
    assert metrics['template_selection_rate'] == 1.0
    assert not metrics['semantic_quality_verified']
    assert telemetry.quality_metrics([], 'live')['template_selection_rate'] is None
    failed = {**base, 'result': 'PUBLISH_UNKNOWN'}
    metrics = telemetry.quality_metrics([failed], 'live')
    assert metrics['selected'] == 0 and metrics['actual_sources'] == {}


@pytest.mark.parametrize('mode', ['shadow', 'live'])
def test_incident_pipeline_uses_safe_alternate_without_publishing_bad_draft(monkeypatch, mode):
    _base_env(monkeypatch, mode)
    _quiet(monkeypatch)
    mem = _MemStore()
    mem.install(monkeypatch)
    published = []
    _install_x(monkeypatch, published)
    monkeypatch.setattr(x_client, 'fetch_mentions', lambda *_a, **_k: {
        'success': True, 'tweets': [{
            'id': '100', 'text': '류님 저녁 맛있게 드세용 😄😄',
            'author_id': '222', 'conversation_id': '300',
            'in_reply_to_user_id': '111', 'created_at': datetime.now(UTC),
        }], 'users': {}, 'newest_id': '100', 'error': None,
    })
    monkeypatch.setattr(classifier, 'gemini_call', lambda **_k: {
        'success': True, 'data': [{'id': '100', 'label': 'POSITIVE'}],
    })
    monkeypatch.setattr(generator, 'gemini_call', lambda **_k: {
        'success': True, 'data': [{'id': '100', 'replies': [
            '맛있는 저녁 되셨다니 다행이에요! 😊',
            '댓글 잘 봤어요 🙂', '인사 남겨주셔서 감사해요 😊',
        ]}],
    })
    result = run_reply.main()
    entry = next(r for r in result['review'] if r['reply_tweet_id'] == '100')
    assert entry['reply_text'] == '인사 남겨주셔서 감사해요 😊'
    assert entry['source'] == 'AI_ALT'
    assert entry['draft_gate_reason'] == 'GATE_ROLE_REVERSAL'
    assert entry['quality_evaluated'] is True and entry['intent'] == 'GREETING'
    assert published == ([('100', entry['reply_text'])] if mode == 'live' else [])
    metrics = result['quality_metrics']
    assert metrics['actual_sources'] == ({'AI_ALT': 1} if mode == 'live' else {})
    assert metrics['simulated_sources'] == ({'AI_ALT': 1} if mode == 'shadow' else {})


@pytest.mark.parametrize('mode,expected', [
    ('live', '실제 발행: 1 / 모의: 0'),
    ('shadow', '실제 발행: 0 / 모의: 1'),
    ('dry_run', '실제 발행: 0 / 모의: 1'),
    ('unknown', '실제 발행: 미확인 / 모의: 미확인'),
])
def test_persistent_summary_never_labels_simulation_as_publication(mode, expected):
    from scripts.publish_summary import build_markdown
    markdown = build_markdown('1', 'success', 'schedule', '', {
        'mode': mode, 'published': 1, 'skip_reasons': {'PUBLISH_UNKNOWN': 2},
    })
    assert expected in markdown
    assert '발행 결과 불명: 2' in markdown


def test_missing_artifact_does_not_report_zero_publications():
    from scripts.publish_summary import build_markdown
    text = build_markdown('1', 'success', 'schedule', '', {})
    assert '실제 발행: 미확인' in text and '발행 결과 불명: 미확인' in text


@pytest.mark.parametrize('comment', ['좋은 하루 보내세요', '저는 B할래요',
    '금리와 환율, 유가가 함께 움직여서 지표별로 시간대를 나눠 살펴보면 좋을 것 같다는 의견입니다'])
@pytest.mark.parametrize('reply', ['ㅎㅎ 반가워요', 'ㅎㅎ 반갑습니다'])
def test_generic_laughter_normalization_does_not_bypass_intent(comment, reply):
    assert gate.check_reply(reply, [], comment) == (False, 'GATE_INTENT')


def test_greeting_allows_our_own_emotion():
    assert gate.check_reply('인사 덕분에 기분 좋았어요', [], '좋은 하루 보내세요') == (True, None)


@pytest.mark.parametrize('reasons', [None, [], 'invalid'])
def test_missing_or_invalid_unknown_counts_are_unconfirmed(reasons):
    from scripts.publish_summary import build_markdown
    text = build_markdown('1', 'success', 'schedule', '', {
        'mode': 'live', 'skip_reasons': reasons, 'actual_published': None,
    })
    assert '실제 발행: 미확인' in text and '발행 결과 불명: 미확인' in text


@pytest.mark.parametrize('value', [None, -1, True, '2'])
def test_invalid_publication_count_is_unconfirmed(value):
    from scripts.publish_summary import build_markdown
    text = build_markdown('1', 'success', 'schedule', '', {
        'mode': 'live', 'actual_published': value, 'skip_reasons': {'PUBLISH_UNKNOWN': value},
    })
    assert '실제 발행: 미확인' in text and '발행 결과 불명: 미확인' in text


def test_summary_trigger_matches_engine_workflow_name():
    import re
    from pathlib import Path
    engine = Path('.github/workflows/reply_engine.yml').read_text()
    summary = Path('.github/workflows/reply_summary.yml').read_text()
    name = re.search(r'^name: (.+)$', engine, re.MULTILINE).group(1)
    assert f'workflows: ["{name}"]' in summary
