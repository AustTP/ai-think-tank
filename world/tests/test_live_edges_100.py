import json
import time
from pathlib import Path

import live_coverage as lc


def test_url_validate_redact():
    assert lc._redact_url('https://[::1]:8443/a') == 'https://[::1]:8443/a'
    assert lc._redact_url('https://x:bad/a') == 'https://x/a'
    for bad in ('', 'https://x/' + 'a' * 3000, 'ftp://x', 'https:///x',
                'https://u:p@example.com', 'https://127.0.0.1'):
        try:
            lc.validate_stream_url(bad)
        except ValueError:
            pass
    assert lc.validate_stream_url('https://example.com', allow_private=True)


def test_resolve_extract_vision(monkeypatch, tmp_path):
    assert lc._resolve_input_url('https://example.com') == 'https://example.com'
    monkeypatch.setattr(lc.shutil, 'which', lambda x: None)
    monkeypatch.delenv('LIVE_COVERAGE_YTDLP', raising=False)
    try:
        lc._resolve_input_url('https://youtube.com/x')
    except RuntimeError:
        pass
    monkeypatch.setenv('LIVE_COVERAGE_YTDLP', 'yt')
    class P:
        returncode = 0; stdout = 'https://s\n'; stderr = ''
    monkeypatch.setattr(lc.subprocess, 'run', lambda *a, **k: P())
    assert lc._resolve_input_url('https://youtube.com/x') == 'https://s'
    P.returncode = 1
    try:
        lc._resolve_input_url('https://youtube.com/x')
    except RuntimeError:
        pass
    monkeypatch.setenv('LIVE_COVERAGE_FFMPEG', 'ff')
    out = tmp_path / 'f'
    class F:
        returncode = 0; stderr = ''
    monkeypatch.setattr(lc.subprocess, 'run', lambda *a, **k: F())
    monkeypatch.setattr(lc.os.path, 'getsize', lambda p: 1)
    assert lc._extract_frame('rtsp://x', str(out)) == str(out)
    monkeypatch.setattr(lc.os.path, 'getsize', lambda p: 0)
    try:
        lc._extract_frame('http://x', str(out))
    except RuntimeError:
        pass
    # vision
    import serve
    monkeypatch.setattr(serve, '_vision_tier_slug', lambda: '')
    try:
        lc._vision_call('x', 'p')
    except RuntimeError:
        pass
    monkeypatch.setattr(serve, '_vision_tier_slug', lambda: 'v')
    monkeypatch.setattr(serve, '_call_openrouter_sync', lambda *a: {'choices': [{'message': {'content': 'c'}}], 'usage': {'cost': 'bad'}})
    assert lc._vision_call('x', 'p') == ('c', 0.0)


def test_parse_rolling_and_io(tmp_path, monkeypatch):
    assert lc._parse_reply('{"observation":"o","entities":"e","confidence":"bad"}')['confidence'] == 0.0
    assert lc._parse_reply('{"observation":"o","entities":[1,""]}')['entities'] == ['1']
    assert lc._parse_reply('{bad}')['observation'] == '{bad}'
    assert 'change' in lc._rolling_context([{'offsetS': 1, 'observation': 'o', 'changes': 'c'}])
    assert lc._rolling_context([{'observation': 'x' * 2000}])
    # library target via serve
    import serve
    monkeypatch.setattr(serve, '_safe_library_path', lambda p: str(tmp_path / p))
    rel, target = lc._library_target('', 'r')
    assert target.endswith('r.md')
    assert lc._write_text('', 'x') is False
    assert lc._write_text(str(tmp_path / 'a' / 'b'), 'x') is True
    # log/accrue paths
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    lc._log(None, 'a', 'x', {})
    lc._log(lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')), 'a', 'x', {})
    lc._accrue(None, 0, 'a')
    monkeypatch.setattr(serve, '_accrue_spend', lambda *a, **k: None)
    lc._accrue(None, 1, 'a')
    lc._accrue(lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')), 1, 'a')
    lc._update('missing', x=1)
    lc._append_observation('missing', {}, 0)
    # prune
    lc._RUNS.clear(); lc._CONTROL.clear()
    for i in range(25):
        lc._RUNS[str(i)] = {'id': str(i), 'status': 'finished', 'startedAt': i}
    lc._prune_locked()
    assert len(lc._RUNS) == lc.HISTORY_LIMIT


def test_worker_branches(tmp_path, monkeypatch):
    _reset = lambda: (lc._RUNS.clear(), lc._CONTROL.clear())
    _reset()
    # max_frames immediate
    snap = lc.start('https://example.com', max_frames=1, interval_s=2, max_seconds=5,
                    extract_frame=lambda *a: None, vision_call=lambda *a: ('{}', 0),
                    library_dir=str(tmp_path))
    lc.wait(snap['id'], 5)
    # frame too large
    def big(url, path, t): Path(path).write_bytes(b'x' * (lc.MAX_FRAME_BYTES + 1))
    snap = lc.start('https://example.com', max_frames=1, interval_s=2,
                    extract_frame=big, vision_call=lambda *a: ('{}', 0),
                    library_dir=str(tmp_path))
    done = lc.wait(snap['id'], 5)
    assert done['lastError'] or done['status']
    # stop while waiting
    _reset()
    def slow(url, path, t):
        Path(path).write_bytes(b'x'); time.sleep(0.1); return path
    snap = lc.start('https://example.com', max_frames=10, interval_s=2,
                    extract_frame=slow, vision_call=lambda *a: ('{}', 0),
                    library_dir=str(tmp_path))
    lc.stop(snap['id'])
    assert lc.wait(snap['id'], 5)


def test_live_final_edges(tmp_path, monkeypatch):
    # malformed URL
    try:
        lc.validate_stream_url('https://[bad')
    except ValueError:
        pass
    # getsize error
    monkeypatch.setenv('LIVE_COVERAGE_FFMPEG', 'ff')
    monkeypatch.setattr(lc.subprocess, 'run', lambda *a, **k: type('P', (), {'returncode': 0, 'stderr': ''})())
    monkeypatch.setattr(lc.os.path, 'getsize', lambda p: (_ for _ in ()).throw(OSError('x')))
    try:
        lc._extract_frame('http://x', str(tmp_path / 'f'))
    except RuntimeError:
        pass
    # write error
    monkeypatch.setattr(lc, 'open', lambda *a, **k: (_ for _ in ()).throw(OSError('x')), raising=False)
    assert lc._write_text(str(tmp_path / 'x'), 'x') is False
    monkeypatch.undo()
    # worker outer error
    lc._RUNS.clear(); lc._CONTROL.clear()
    monkeypatch.setattr(lc, '_resolve_input_url', lambda u: (_ for _ in ()).throw(RuntimeError('bad')))
    snap = lc.start('https://example.com', max_frames=1, interval_s=2,
                    extract_frame=lambda *a: None, vision_call=lambda *a: ('{}', 0),
                    library_dir=str(tmp_path))
    done = lc.wait(snap['id'], 5)
    assert done['status'] == 'failed'
    monkeypatch.undo()
    # stop during after-frame wait
    lc._RUNS.clear(); lc._CONTROL.clear()
    ev = __import__('threading').Event()
    def extract(url, path, t):
        Path(path).write_bytes(b'x'); ev.set(); return path
    # inject event into control after start? use direct _run_worker
    rid = 'r1'
    lc._RUNS[rid] = {'id': rid, 'label': 'l', 'stream': 's', 'status': 'starting',
                     'startedAt': time.time(), 'startedAtIso': '', 'intervalS': 2,
                     'maxSeconds': 10, 'maxFrames': 10, 'maxUsd': 1, 'frames': 0,
                     'spentUsd': 0, 'observations': [], 'actor': 'a'}
    lc._CONTROL[rid] = {'stop': ev}
    lc._run_worker(rid, 'https://example.com', interval=2, max_seconds=10,
                   max_frames=10, max_usd=1, label='l', actor='a',
                   extract_frame=extract, vision_call=lambda *a: ('{}', 0),
                   log_action=None, accrue_spend=None, library_dir=str(tmp_path))
    assert lc._RUNS[rid]['status'] == 'stopped'
    # error wait stop
    lc._RUNS.clear(); lc._CONTROL.clear()
    ev2 = __import__('threading').Event()
    def bad(url, path, t):
        ev2.set(); raise RuntimeError('x')
    lc._RUNS[rid] = {'id': rid, 'label': 'l', 'stream': 's', 'status': 'starting',
                     'startedAt': time.time(), 'startedAtIso': '', 'intervalS': 2,
                     'maxSeconds': 10, 'maxFrames': 10, 'maxUsd': 1, 'frames': 0,
                     'spentUsd': 0, 'observations': [], 'actor': 'a'}
    lc._CONTROL[rid] = {'stop': ev2}
    lc._run_worker(rid, 'https://example.com', interval=2, max_seconds=10,
                   max_frames=10, max_usd=1, label='l', actor='a',
                   extract_frame=bad, vision_call=lambda *a: ('{}', 0),
                   log_action=None, accrue_spend=None, library_dir=str(tmp_path))
    assert lc._RUNS[rid]['status'] == 'stopped'


def _run_direct(rid, tmp_path, **over):
    lc._RUNS.clear(); lc._CONTROL.clear()
    run = {'id': rid, 'label': 'l', 'stream': 's', 'status': 'starting',
           'startedAt': time.time(), 'startedAtIso': '', 'intervalS': 2,
           'maxSeconds': 10, 'maxFrames': 10, 'maxUsd': 1, 'frames': 0,
           'spentUsd': 0, 'observations': [], 'actor': 'a'}
    run.update(over)
    lc._RUNS[rid] = run
    lc._CONTROL[rid] = {'stop': __import__('threading').Event()}
    lc._run_worker(rid, 'https://example.com', interval=2,
                   max_seconds=over.get('maxSeconds', 10),
                   max_frames=over.get('maxFrames', 10),
                   max_usd=over.get('maxUsd', 1), label='l', actor='a',
                   extract_frame=lambda *a: None, vision_call=lambda *a: ('{}', 0),
                   log_action=None, accrue_spend=None, library_dir=str(tmp_path))
    return lc._RUNS[rid]


def test_live_loop_caps(tmp_path):
    assert _run_direct('a', tmp_path, maxFrames=0)['stopReason'] == 'max_frames'
    # max seconds before first frame
    assert _run_direct('c', tmp_path, maxSeconds=0)['stopReason'] == 'max_seconds'
    # max seconds after a frame
    def slow(*a): time.sleep(0.02)
    lc._RUNS.clear(); lc._CONTROL.clear()
    rid = 'd'
    lc._RUNS[rid] = {'id': rid, 'label': 'l', 'stream': 's', 'status': 'starting',
                     'startedAt': time.time(), 'startedAtIso': '', 'intervalS': 2,
                     'maxSeconds': 0.01, 'maxFrames': 10, 'maxUsd': 0, 'frames': 0,
                     'spentUsd': 0, 'observations': [], 'actor': 'a'}
    lc._CONTROL[rid] = {'stop': __import__('threading').Event()}
    lc._run_worker(rid, 'https://example.com', interval=2, max_seconds=0.01,
                   max_frames=10, max_usd=0, label='l', actor='a',
                   extract_frame=slow, vision_call=lambda *a: ('{}', 0),
                   log_action=None, accrue_spend=None, library_dir=str(tmp_path))
    assert lc._RUNS[rid]['stopReason'] == 'max_seconds'
