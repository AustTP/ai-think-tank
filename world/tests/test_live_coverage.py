"""Bounded live video coverage: sampling loop, caps, sharing, and safety."""
from __future__ import annotations

import json
import time

import pytest

import live_coverage as lc


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(lc, 'MIN_INTERVAL_S', 0.01)
    yield
    for run in list(lc._RUNS.values()):
        if run.get('status') in lc.ACTIVE_STATUSES:
            try:
                lc.stop(run['id'])
            except KeyError:
                pass
            lc.wait(run['id'], 5)
    with lc._LOCK:
        lc._RUNS.clear()
        lc._CONTROL.clear()


def _fake_extract(_url, out_path, _timeout):
    with open(out_path, 'wb') as handle:
        handle.write(b'fake-jpeg-frame')


def _fake_vision(replies, prompts=None, costs=None):
    state = {'n': 0}

    def call(_image_b64, prompt, _max_tokens):
        index = state['n']
        state['n'] += 1
        if prompts is not None:
            prompts.append(prompt)
        reply = replies[min(index, len(replies) - 1)]
        cost = (costs[min(index, len(costs) - 1)]
                if costs else 0.001)
        return reply, cost

    return call


def _valid_reply(observation='A test pattern.', changes='first frame'):
    return json.dumps({
        'observation': observation,
        'changes': changes,
        'entities': ['test pattern'],
        'confidence': 0.9,
    })


def _start(tmp_path, **overrides):
    params = {
        'url': 'http://127.0.0.1/live.m3u8',
        'label': 'unit test',
        'interval_s': 0.01,
        'max_seconds': 5,
        'max_frames': 3,
        'max_usd': 1.0,
        'actor': 'player',
        'extract_frame': _fake_extract,
        'vision_call': _fake_vision([_valid_reply()]),
        'log_action': lambda *args, **kwargs: None,
        'accrue_spend': lambda *args, **kwargs: None,
        'library_dir': str(tmp_path),
        'allow_private': True,
    }
    params.update(overrides)
    return lc.start(**params)


def test_run_samples_frames_and_shares_observations(tmp_path):
    prompts = []
    vision = _fake_vision(
        [_valid_reply(changes='first frame'),
         _valid_reply(changes='nothing moved'),
         _valid_reply(changes='nothing moved')],
        prompts=prompts,
    )
    run = _start(tmp_path, vision_call=vision, max_frames=3)
    result = lc.wait(run['id'], 5)
    assert result['status'] == 'finished'
    assert result['stopReason'] == 'max_frames'
    assert result['frames'] == 3
    assert result['observationCount'] == 3
    assert result['observations'][0]['observation'] == 'A test pattern.'
    assert result['observations'][1]['changes'] == 'nothing moved'
    assert 'A test pattern.' in prompts[1]
    path = tmp_path / 'media' / 'live-coverage' / f"{run['id']}.md"
    text = path.read_text()
    assert 'Live coverage: unit test' in text
    assert 'A test pattern.' in text
    assert 'Run finished' in text


def test_per_run_budget_stops_the_loop(tmp_path):
    run = _start(
        tmp_path,
        vision_call=_fake_vision([_valid_reply()], costs=[0.1]),
        max_frames=20,
        max_usd=0.15,
    )
    result = lc.wait(run['id'], 5)
    assert result['status'] == 'finished'
    assert result['stopReason'] == 'max_usd'
    assert result['frames'] == 2
    assert result['spentUsd'] == pytest.approx(0.2)


def test_stop_ends_an_active_run(tmp_path):
    run = _start(
        tmp_path,
        vision_call=_fake_vision([_valid_reply()], costs=[0.0]),
        interval_s=0.2,
        max_frames=50,
    )
    deadline = time.time() + 3
    while time.time() < deadline:
        current = lc.status(run['id'])['run']
        if current and current['frames'] >= 1:
            break
        time.sleep(0.02)
    lc.stop(run['id'])
    result = lc.wait(run['id'], 5)
    assert result['status'] == 'stopped'
    assert result['stopReason'] == 'stopped'
    assert result['frames'] < 50


def test_one_run_at_a_time(tmp_path):
    first = _start(tmp_path, interval_s=0.2, max_frames=50)
    with pytest.raises(RuntimeError):
        _start(tmp_path, interval_s=0.2, max_frames=50)
    lc.stop(first['id'])
    lc.wait(first['id'], 5)


def test_status_unknown_run_is_empty(tmp_path):
    data = lc.status('does-not-exist')
    assert data['run'] is None


def test_stream_url_guard_rejects_unsafe_shapes():
    with pytest.raises(ValueError):
        lc.validate_stream_url('')
    with pytest.raises(ValueError):
        lc.validate_stream_url('file:///etc/passwd')
    with pytest.raises(ValueError):
        lc.validate_stream_url('http://127.0.0.1/live.m3u8')
    with pytest.raises(ValueError):
        lc.validate_stream_url('rtsp://user:password@example.com/live')
    with pytest.raises(ValueError):
        lc.validate_stream_url('https://example.com/' + ('a' * lc.MAX_URL_CHARS))
    assert lc.validate_stream_url('http://127.0.0.1/live.m3u8',
                                  allow_private=True).startswith('http://')


def test_url_redaction_drops_query_and_fragment():
    redacted = lc._redact_url(
        'https://example.com/live.m3u8?token=secret#frag')
    assert redacted == 'https://example.com/live.m3u8'
    assert 'secret' not in redacted


def test_unparsed_vision_reply_still_becomes_an_observation(tmp_path):
    run = _start(
        tmp_path,
        vision_call=_fake_vision(['a plain-language description']),
        max_frames=1,
    )
    result = lc.wait(run['id'], 5)
    assert result['status'] == 'finished'
    assert result['observations'][0]['observation'] == 'a plain-language description'
    assert result['observations'][0]['confidence'] == 0.0
