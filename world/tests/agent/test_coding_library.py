import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import agents.coding_library as coding_library
import content


@pytest.fixture
def storage(tmp_path):
    library = tmp_path / 'library'
    sandboxes = tmp_path / 'sandboxes'
    library.mkdir()
    (sandboxes / 'story-a').mkdir(parents=True)
    (sandboxes / 'story-b').mkdir()
    (library / 'data.csv').write_bytes(b'name,value\nreal,7\n')
    return SimpleNamespace(LIBRARY_DIR=library, SANDBOXES_DIR=sandboxes,
                           _library_search_matches=Mock(return_value=[
                               {'path': 'data.csv', 'snippet': 'real,7', 'size': 18}]),
                           record_library_read=Mock(), log_action=Mock())


def run(storage, **request):
    return json.loads(coding_library.execute(request, storage, 'ada', 'story-a'))


def test_search_read_and_import_preserve_source_and_isolation(storage):
    assert run(storage, action='search', query='real')['matches'][0]['path'] == 'data.csv'
    assert 'real,7' in run(storage, action='read', path='data.csv')['content']
    imported = run(storage, action='copy', path='data.csv')
    destination = storage.SANDBOXES_DIR / 'story-a' / imported['relativePath']
    assert destination.read_bytes() == (storage.LIBRARY_DIR / 'data.csv').read_bytes()
    assert not list((storage.SANDBOXES_DIR / 'story-b').iterdir())
    assert run(storage, action='copy', path='data.csv') == imported
    destination.write_bytes(b'local edit')
    assert 'error' in run(storage, action='copy', path='data.csv')
    assert destination.read_bytes() == b'local edit'
    assert b'real,7' in (storage.LIBRARY_DIR / 'data.csv').read_bytes()


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', 'C:\\secret', '//server/share', 'missing.csv'])
def test_invalid_sources_rejected(storage, path):
    assert 'error' in run(storage, action='read', path=path)
    assert 'error' in run(storage, action='copy', path=path)


def test_pagination_and_copy_limit(storage):
    (storage.LIBRARY_DIR / 'long.txt').write_text('a' * 12000 + 'tail')
    first = run(storage, action='read', path='long.txt')
    assert len(first['content']) == 12000
    assert run(storage, action='read', path='long.txt', offset=first['nextOffset'])['content'] == 'tail'
    with patch.object(coding_library, 'MAX_COPY_BYTES', 5):
        assert 'error' in run(storage, action='copy', path='data.csv')


def test_links_cannot_escape_storage(storage, tmp_path):
    link = storage.SANDBOXES_DIR / 'story-a' / '.library-artifacts'
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Symlinks unavailable on this host')
    assert 'error' in run(storage, action='copy', path='data.csv')
    assert not list(outside.iterdir())


def test_coding_loop_services_library_requests_before_execution(storage):
    responses = iter([
        {'reply': json.dumps({'libraryRequest': {'action': 'search', 'query': 'real'}})},
        {'reply': json.dumps({'libraryRequest': {'action': 'copy', 'path': 'data.csv'}})},
        {'error': 'test ends before implementation'},
        {'error': 'test ends before implementation'},
    ])
    results, prompts = [], []
    def chat(method, base, endpoint, payload, *args, **kwargs):
        assert endpoint == '/api/chat', 'Library requests must never execute as shell commands'
        prompts.append(list(payload['messages']))
        return next(responses)
    with ExitStack() as stack:
        for name in ('LIBRARY_DIR', 'SANDBOXES_DIR', '_library_search_matches', 'record_library_read', 'log_action'):
            stack.enter_context(patch.object(content._serve, name, getattr(storage, name)))
        stack.enter_context(patch.object(content._serve, 'get_or_create_agent_key', return_value='test'))
        stack.enter_context(patch.object(content._serve, '_coding_tier_slug', return_value='test'))
        stack.enter_context(patch.object(content._serve, '_http_json', side_effect=chat))
        assert not content._run_coding_content_inner({}, 'ada', {'id': 't', 'title': 'Build'},
            sandbox_override='story-a', result_sink=lambda tid, result: results.append(result))
    assert len(prompts) >= 3
    assert 'data.csv' in prompts[1][-1]['content']
    assert '/workspace/.library-artifacts/' in prompts[2][-1]['content']
    assert list((storage.SANDBOXES_DIR / 'story-a/.library-artifacts').rglob('data.csv'))
    assert not results[-1]['ok']
