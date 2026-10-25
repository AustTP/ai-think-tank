import io
import json
import runpy
from contextlib import redirect_stdout

import integrations.x_ingest as x_ingest


def test_helpers():
    assert x_ingest.status_id('https://x.com/a/status/123?s=1') == '123'
    assert x_ingest.status_id('nope') is None
    assert x_ingest._clean('  a\t b  ') == 'a b'
    blocks = [
        {'type': 'header-two', 'text': 'Head'},
        {'type': 'unordered-list-item', 'text': 'One'},
        {'type': 'ordered-list-item', 'text': 'Two'},
        {'type': 'code-block', 'text': 'x=1'},
        {'type': 'unstyled', 'text': 'Plain'},
        {'type': 'unstyled', 'text': '  '},
    ]
    out = x_ingest._article_blocks({'content': {'blocks': blocks}})
    assert '## Head' in out and '- One' in out and '```' in out and 'Plain' in out
    assert x_ingest._article_blocks(None) == ''
    assert x_ingest._media({'media_entities': [{'media_info': {'original_img_url': 'u'}}, {}]}) == ['u']
    assert x_ingest._media(None) == []
    assert x_ingest._category('AI agents', 'llm prompt') == 'AI & Agents'
    assert x_ingest._category('nothing', 'here') == 'Other'


def test_fetch(monkeypatch):
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"tweet":{"text":"hi"}}'
    monkeypatch.setattr(x_ingest.urllib.request, 'urlopen', lambda *a, **k: Resp())
    assert x_ingest._fetch('1')['tweet']['text'] == 'hi'


def test_render_article_and_post():
    tab = {'url': 'https://x.com/a/status/1', 'title': 'Tab title'}
    payload = {'tweet': {'author': {'name': 'A'}, 'article': {
        'title': 'T', 'id': 'art', 'preview_text': 'P',
        'content': {'blocks': [{'text': 'Body'}]},
        'media_entities': [{'media_info': {'original_img_url': 'img'}}],
        'created_at': 'now'}}}
    title, preview, md = x_ingest._render(tab, payload, 'AI & Agents')
    assert title == 'T' and preview == 'P'
    assert 'X article id: art' in md and '- Media: img' in md
    post = {'tweet': {'raw_text': {'text': 'raw'}}}
    title, preview, md = x_ingest._render(tab, post, 'Other')
    assert title == 'Tab title' and 'Kind: X post' in md


def test_ingest_writes_and_collects_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(x_ingest, 'OUT_DIR', str(tmp_path))
    monkeypatch.setattr(x_ingest.time, 'sleep', lambda *_: None)
    tabs = [
        {'url': 'https://x.com/a/status/1', 'title': 'One'},
        {'url': 'https://x.com/a/status/1', 'title': 'Dup'},
        {'url': 'https://x.com/a/not-a-status', 'title': 'No id'},
        {'url': 'https://x.com/a/status/2', 'title': 'Two'},
        {'url': 'https://x.com/a/status/3', 'title': 'Three'},
    ]
    monkeypatch.setattr(x_ingest.safari_tabs, 'x_tabs', lambda _='iPhone': tabs)

    def fetch(sid):
        if sid == '2':
            return {'message': 'gone'}
        if sid == '3':
            raise RuntimeError('network')
        return {'tweet': {'text': 'hello', 'author': {'name': 'A'}}}
    monkeypatch.setattr(x_ingest, '_fetch', fetch)
    result = x_ingest.ingest()
    assert result['tabs'] == 5 and result['written'] == 1
    assert len(result['errors']) == 3
    assert (tmp_path / 'x-1.md').exists()
    index = json.loads((tmp_path / 'index.json').read_text())
    assert index['entries'][0]['id'] == '1'
    assert (tmp_path / 'INDEX.md').exists()

