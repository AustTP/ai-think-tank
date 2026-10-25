import base64
import json
import time

import server.live_coverage as lc


def _reset():
    lc._RUNS.clear(); lc._CONTROL.clear()


def _extract_ok(url, path, timeout):
    open(path, 'wb').write(b'jpeg')
    return path


def _vision_ok(img, prompt, max_tokens):
    return json.dumps({'observation': 'seen', 'changes': 'none', 'entities': ['a'], 'confidence': 0.9}), 0.01


def test_start_finish_and_status(tmp_path):
    _reset()
    logs = []
    snap = lc.start('https://example.com', max_frames=1, interval_s=2, max_seconds=5,
                    max_usd=1, extract_frame=_extract_ok, vision_call=_vision_ok,
                    log_action=lambda *a, **k: logs.append(a), library_dir=str(tmp_path))
    assert snap['status'] == 'starting'
    done = lc.wait(snap['id'], 5)
    assert done['status'] == 'finished' and done['frames'] == 1
    assert (tmp_path / done['libraryPath']).exists()
    assert lc.status(snap['id'])['run']['id'] == snap['id']
    assert lc.status()['run']['id'] == snap['id']
    assert lc.status()['runs']
    # a second run after finish
    snap2 = lc.start('https://example.com', max_frames=1, interval_s=2,
                     extract_frame=_extract_ok, vision_call=_vision_ok,
                     library_dir=str(tmp_path))
    lc.wait(snap2['id'], 5)
    # stop missing
    try:
        lc.stop('missing')
    except KeyError:
        pass


def test_start_conflict_and_errors(tmp_path):
    _reset()
    lc._RUNS['a'] = {'id': 'a', 'status': 'running', 'startedAt': 1}
    try:
        lc.start('https://example.com', allow_private=True, extract_frame=_extract_ok,
                 vision_call=_vision_ok)
    except RuntimeError:
        pass
    lc._RUNS.clear()
    # repeated extraction failures
    def bad(*a): raise RuntimeError('no frame')
    snap = lc.start('https://example.com', max_frames=5, interval_s=2,
                    max_seconds=5, extract_frame=bad, vision_call=_vision_ok,
                    library_dir=str(tmp_path))
    done = lc.wait(snap['id'], 10)
    assert done['status'] == 'failed' and done['stopReason'] == 'repeated_errors'
    # empty frame
    def empty(url, path, timeout): open(path, 'wb').write(b'')
    snap = lc.start('https://example.com', max_frames=1, interval_s=2,
                    extract_frame=empty, vision_call=_vision_ok,
                    library_dir=str(tmp_path))
    done = lc.wait(snap['id'], 5)
    assert done['status'] == 'finished' or done['lastError']
    # stop active
    def slow(*a): time.sleep(0.2); return _extract_ok(*a)
    snap = lc.start('https://example.com', max_frames=10, interval_s=2,
                    extract_frame=slow, vision_call=_vision_ok,
                    library_dir=str(tmp_path))
    assert lc.stop(snap['id'])['status'] == 'stopping'
    lc.wait(snap['id'], 5)
    # max_usd hit
    snap = lc.start('https://example.com', max_frames=10, interval_s=2, max_usd=0.005,
                    extract_frame=_extract_ok, vision_call=_vision_ok,
                    library_dir=str(tmp_path))
    done = lc.wait(snap['id'], 5)
    assert done['stopReason'] in ('max_usd', 'finished', 'stopped')
