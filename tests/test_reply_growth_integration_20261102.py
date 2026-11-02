"""Growth profile through the real pipeline; external writes use in-memory doubles."""
from datetime import UTC, datetime

import pytest

import run_reply
from reply_engine import classifier, generator, store, x_client
from reply_engine import filter as filters
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet


@pytest.mark.parametrize('mode', ['live', 'shadow'])
def test_author_two_profile_keeps_third_deferred_and_never_reposts(monkeypatch, mode):
    _base_env(monkeypatch, mode)
    _quiet(monkeypatch)
    monkeypatch.setenv('X_MY_USER_ID', '111')
    monkeypatch.setattr(run_reply, 'REPLY_RUN_CAP', 3)
    monkeypatch.setattr(run_reply, 'REPLY_AUTHOR_DAILY_CAP', 2)
    monkeypatch.setattr(filters, 'REPLY_AUTHOR_DAILY_CAP', 2)
    mem = _MemStore()
    mem.install(monkeypatch)
    sent = []
    _install_x(monkeypatch, sent)
    tweets = [{
        'id': str(100 + i), 'author_id': '222', 'conversation_id': f'c{i}',
        'text': text, 'in_reply_to_user_id': '111', 'created_at': datetime.now(UTC),
    } for i, text in enumerate(['자료 정리 감사합니다', '좋은 하루 보내세요', '축하합니다'])]
    monkeypatch.setattr(x_client, 'fetch_mentions', lambda *_a, **_k: {
        'success': True, 'tweets': tweets, 'users': {}, 'newest_id': '102', 'error': None,
    })
    monkeypatch.setattr(classifier, 'classify_batch', lambda items: {
        item['id']: 'POSITIVE' for item in items
    })
    monkeypatch.setattr(generator, 'generate_batch', lambda items: {
        item['id']: {'100': '읽어주셔서 고맙습니다',
                     '101': '따뜻한 인사 고마워요, 편안한 시간 되세요 🙂',
                     '102': '축하해 주셔서 감사합니다 😊'}[item['id']] for item in items
    })
    result = run_reply.main()
    assert result['published'] == 2
    assert result['skip_reasons']['AUTHOR_CAP_RUN'] == 1
    assert len(sent) == (2 if mode == 'live' else 0)
    if mode == 'live':
        first_ids = [tid for tid, _ in sent]
        assert len(set(first_ids)) == 2
        # A later run uses persisted successful rows and the daily count.
        monkeypatch.setattr(store, 'count_author_responded_today_bulk', lambda _ids: {'222': 2})
        run_reply.main()
        assert [tid for tid, _ in sent] == first_ids
