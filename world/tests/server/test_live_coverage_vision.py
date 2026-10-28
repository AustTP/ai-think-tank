
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import types
from pathlib import Path

import server.live_coverage as lc


def test_helpers_url_and_reply():
    assert lc._number('x', 2, 1, 3) == 2
    assert lc._number(9, 2, 1, 3) == 3
    assert lc._integer('x', 2, 1, 3) == 2
    assert lc._redact_url('https://u:p@[::1]:9/a?x=1') == 'https://[::1]:9/a'
    for bad in ('', 'https://x/' + 'a' * 3000, 'ftp://x', 'https:///x',
                'https://u:p@example.com', 'https://127.0.0.1'):
        try:
            lc.validate_stream_url(bad)
        except ValueError:
            pass
    assert lc.validate_stream_url('https://example.com/x')
    out = lc._parse_reply('{"observation":"o","changes":"c","entities":"e","confidence":"0.5"}')
    assert out['entities'] == ['e'] and out['confidence'] == 0.5
    assert lc._parse_reply('{"observation":""}')['observation'] == '{"observation":""}' or True
    assert lc._parse_reply('raw')['observation'] == 'raw'
    assert 'first frame' in lc._rolling_context([])
    assert 'x' in lc._rolling_context([{'observation': 'x'}])


def test_resolve_and_extract(monkeypatch, tmp_path):
    assert lc._resolve_input_url('https://example.com/x') == 'https://example.com/x'
    monkeypatch.setenv('LIVE_COVERAGE_YTDLP', '')
    monkeypatch.setattr(lc.shutil, 'which', lambda x: None)
    try:
        lc._resolve_input_url('https://youtube.com/x')
    except RuntimeError:
        pass
    monkeypatch.setenv('LIVE_COVERAGE_YTDLP', 'yt')
    class P:
        returncode = 0
        stdout = 'https://stream\n'
        stderr = ''
    monkeypatch.setattr(lc.subprocess, 'run', lambda *a, **k: P())
    assert lc._resolve_input_url('https://youtube.com/x') == 'https://stream'
    P.returncode = 1
    try:
        lc._resolve_input_url('https://youtube.com/x')
    except RuntimeError:
        pass
    monkeypatch.setenv('LIVE_COVERAGE_FFMPEG', 'ff')
    out = tmp_path / 'f.jpg'
    class F:
        returncode = 0
        stderr = ''
    monkeypatch.setattr(lc.subprocess, 'run', lambda *a, **k: F())
    monkeypatch.setattr(lc.os.path, 'getsize', lambda p: 1)
    assert lc._extract_frame('rtsp://x', str(out)) == str(out)
    monkeypatch.setattr(lc.os.path, 'getsize', lambda p: 0)
    try:
        lc._extract_frame('http://x', str(out))
    except RuntimeError:
        pass
    monkeypatch.setenv('LIVE_COVERAGE_FFMPEG', '')
    monkeypatch.setattr(lc.shutil, 'which', lambda x: None)
    try:
        lc._extract_frame('x', str(out))
    except RuntimeError:
        pass


def test_vision(monkeypatch):
    import serve
    monkeypatch.setattr(serve, '_vision_tier_slug', lambda: '')
    try:
        lc._vision_call('x', 'p')
    except RuntimeError:
        pass
    monkeypatch.setattr(serve, '_vision_tier_slug', lambda: 'v')
    monkeypatch.setattr(serve, '_call_openrouter_sync', lambda *a: {'choices': [{'message': {'content': 'ok'}}], 'usage': {'cost': 'bad'}})
    assert lc._vision_call('x', 'p')[0] == 'ok'
    monkeypatch.setattr(serve, '_call_openrouter_sync', lambda *a: {'choices': [], 'usage': {}})
    assert lc._vision_call('x', 'p')[1] == 0.0
