"""Regression evidence for recent-context ordering, artifact auth and cap tuning."""
import io
import json
from urllib.request import Request

import pytest

import run_reply
from reply_engine import filter as filters
from reply_engine import generator, store
from scripts.publish_summary import ArtifactRedirectHandler, artifact_zip_bytes, build_markdown
from tests.test_reply_pipeline import _base_env, _install_x, _MemStore, _quiet


def test_signed_storage_redirect_strips_github_bearer():
    request = Request('https://api.github.com/repos/o/r/actions/artifacts/1/zip',
                      headers={'Authorization': 'Bearer TEST_ONLY', 'Accept': 'application/json'})
    redirect = ArtifactRedirectHandler().redirect_request(
        request, None, 302, 'Found', {}, 'https://storage.example/file?signature=test')
    assert redirect.get_header('Authorization') is None
    assert request.get_header('Authorization') == 'Bearer TEST_ONLY'
    assert redirect.full_url.endswith('?signature=test')


def test_artifact_selects_exact_run_and_downloads_with_safe_opener(monkeypatch):
    data = {'artifacts': [
        {'name': 'reply-report-other', 'archive_download_url': 'wrong'},
        {'name': 'reply-report-12', 'expired': True, 'archive_download_url': 'expired'},
        {'name': 'reply-report-12', 'archive_download_url': 'https://api.github.com/valid'},
    ]}
    monkeypatch.setattr('urllib.request.urlopen',
                        lambda *_a, **_kw: io.BytesIO(json.dumps(data).encode()))
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://api.github.com/valid'
            assert request.get_header('Authorization') == 'Bearer TEST_ONLY'
            return io.BytesIO(b'zip')
    def build(handler):
        assert isinstance(handler, ArtifactRedirectHandler)
        return Opener()
    monkeypatch.setattr('urllib.request.build_opener', build)
    assert artifact_zip_bytes('12', 'TEST_ONLY') == b'zip'


def test_failed_summary_does_not_invent_zero_candidates():
    text = build_markdown('12', 'success', 'schedule', '', {})
    assert '후보: 미확인 / 분류 통과: 미확인' in text
    assert '수집 포화: 미확인' in text


def test_generator_receives_latest_ten_and_specific_subject_instruction(monkeypatch):
    _base_env(monkeypatch, 'shadow')
    _quiet(monkeypatch)
    _MemStore().install(monkeypatch)
    _install_x(monkeypatch, [])
    # Store contract: newest first. The old [-10:] wrongly used oldest 10.
    newest_first = [f'previous-{i}' for i in range(30)]
    monkeypatch.setattr(store, 'get_recent_response_texts', lambda _n: newest_first)
    captured = []
    def generate(**kwargs):
        captured.append(kwargs['prompt'])
        return {'success': True, 'data': [{'id': '100', 'reply': '말씀 고맙습니다'}]}
    monkeypatch.setattr(generator, 'gemini_call', generate)
    run_reply.main()
    row = next(json.loads(line) for line in captured[0].splitlines() if line.startswith('{"id"'))
    assert row['recent_response_texts'] == list(reversed(newest_first[:10]))
    assert newest_first[0] == 'previous-0'  # do not mutate the store result
    assert '구체적인 지표·대상' in captured[0]


@pytest.mark.parametrize('cap,expected', [(1, 1), (2, 2)])
def test_author_cap_tuning_is_bounded_and_preserves_duplicate_block(monkeypatch, cap, expected):
    monkeypatch.setattr(filters, 'REPLY_AUTHOR_DAILY_CAP', cap)
    class Repo:
        history_exists = staticmethod(lambda tid: tid == 'published')
        count_author_responded_today = staticmethod(lambda _: 0)
        count_conversation_responded_today = staticmethod(lambda _: 0)
    ctx = filters.CapContext()
    admitted = [filters.check_and_admit(
        {'id': str(i), 'author_id': 'same', 'conversation_id': 'thread'}, ctx, repo=Repo())[0]
        for i in range(3)]
    assert sum(admitted) == expected
    assert filters.check_and_admit(
        {'id': 'published', 'author_id': 'same', 'conversation_id': 'thread'},
        ctx, repo=Repo()) == (False, 'DUP')
