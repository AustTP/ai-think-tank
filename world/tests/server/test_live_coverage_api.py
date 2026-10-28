"""HTTP wiring for live video coverage, without starting a real run."""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import unittest.mock

import server.live_coverage as live_coverage
import serve
from starlette.testclient import TestClient


def _client():
    client = TestClient(serve.app)
    client.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return client


def _run_snapshot():
    return {
        'id': 'run123',
        'label': 'test run',
        'stream': 'https://example.com/live.m3u8',
        'status': 'running',
        'frames': 1,
        'maxFrames': 12,
        'maxSeconds': 180,
        'maxUsd': 0.25,
        'spentUsd': 0.001,
        'stopReason': None,
        'lastError': None,
        'libraryPath': 'media/live-coverage/run123.md',
        'observations': [],
    }


def test_status_requires_auth():
    client = TestClient(serve.app)
    response = client.get('/api/live-coverage/status')
    assert response.status_code == 401


def test_start_and_status_use_the_module():
    client = _client()
    with unittest.mock.patch.object(
            live_coverage, 'start', return_value=_run_snapshot()) as start, \
            unittest.mock.patch.object(
                live_coverage, 'status',
                return_value={'run': _run_snapshot(), 'runs': []}) as status:
        started = client.post('/api/live-coverage/start', json={
            'url': 'https://example.com/live.m3u8',
            'label': 'test run',
        })
        assert started.status_code == 200
        assert started.json()['run']['id'] == 'run123'
        assert start.call_args.kwargs['actor'] == 'player'

        current = client.get('/api/live-coverage/status?limit=5')
        assert current.status_code == 200
        assert current.json()['run']['id'] == 'run123'
        assert status.call_args.kwargs['max_items'] == 5


def test_stop_uses_the_module():
    client = _client()
    with unittest.mock.patch.object(
            live_coverage, 'stop', return_value=_run_snapshot()) as stop:
        response = client.post('/api/live-coverage/stop',
                               json={'runId': 'run123'})
        assert response.status_code == 200
        assert response.json()['run']['id'] == 'run123'
        assert stop.call_args.args == ('run123',)


def test_unknown_run_is_a_404():
    client = _client()
    with unittest.mock.patch.object(
            live_coverage, 'status',
            return_value={'run': None, 'runs': []}):
        response = client.get('/api/live-coverage/status?run_id=missing')
        assert response.status_code == 404


def test_page_requires_a_session():
    client = TestClient(serve.app)
    response = client.get('/live-coverage', follow_redirects=False)
    assert response.status_code in (303, 401)


def test_agent_read_tool_includes_the_running_observations():
    executor = serve._make_web_tools_executor('ada', 'key')
    run = _run_snapshot()
    run['observations'] = [{
        'offsetS': 2.0,
        'frame': 1,
        'confidence': 0.9,
        'observation': 'A red test pattern.',
        'changes': 'first frame',
        'entities': ['test pattern'],
    }]
    with unittest.mock.patch.object(
            serve, '_http_json', return_value={'ok': True, 'run': run,
                                               'runs': []}):
        output = executor('read_live_coverage', {'max': 5})
    assert 'A red test pattern.' in output
    assert 'EXTERNAL_DATA' in output


def test_agent_key_can_read_the_status():
    client = TestClient(serve.app)
    with unittest.mock.patch.object(
            serve, '_resolve_requester', return_value='ada'), \
            unittest.mock.patch.object(
                serve, '_valid_agent_key_presented', return_value=True), \
            unittest.mock.patch.object(
                serve, 'verify_agent_key', return_value=True), \
            unittest.mock.patch.object(
                live_coverage, 'status',
                return_value={'run': _run_snapshot(), 'runs': []}):
        response = client.get('/api/live-coverage/status',
                              headers={'X-Agent-Key': 'key'})
    assert response.status_code == 200
    assert response.json()['run']['id'] == 'run123'
