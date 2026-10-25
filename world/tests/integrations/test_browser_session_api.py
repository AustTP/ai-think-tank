"""Endpoint tests for the player browser-session surface.

Uses FastAPI's TestClient with the player gate patched open and the session
module stubbed, so no Docker runs and no ports open.

Run: python3 tests/test_browser_session_api.py
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import agents.browser_session as browser_session  # noqa: E402
import serve  # noqa: E402
from fastapi.testclient import TestClient


SESSION = {
    'agentId': 'agent-a',
    'status': 'running',
    'takeover': False,
    'port': 43210,
    'startedAt': 1,
    'lastUsed': 1,
    'viewerUrl': 'http://127.0.0.1:43210/vnc.html',
    'token': 'tok-123',
    'cdpUrl': 'http://127.0.0.1:9222',
}


class BrowserSessionApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-session-api-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patcher = unittest.mock.patch.multiple(
            serve,
            verify_session=lambda *_: True,
            _require_player_session=lambda *_: True,
            EXECUTION_ENABLED=True,
            ensure_sandbox_networking=lambda: None,
            _sandbox_dir_for=lambda sid: os.path.join(self.tmp, sid),
            _browser_profile_dir=lambda agent: os.path.join(self.tmp, 'profile'),
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = TestClient(serve.app)

    def test_list_returns_sessions(self):
        with unittest.mock.patch.object(browser_session, 'list_sessions',
                                        return_value=[SESSION]):
            r = self.client.get('/api/browser-session/list')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['sessions'][0]['agentId'], 'agent-a')

    def test_list_requires_player(self):
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False):
            r = self.client.get('/api/browser-session/list')
        self.assertEqual(r.status_code, 401)

    def test_start_requires_agent(self):
        r = self.client.post('/api/browser-session/start', json={})
        self.assertEqual(r.status_code, 400)

    def test_start_calls_session_manager(self):
        with unittest.mock.patch.object(browser_session, 'start',
                                        return_value=SESSION) as start:
            r = self.client.post('/api/browser-session/start',
                                 json={'agentId': 'agent-a'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['session']['agentId'], 'agent-a')
        self.assertEqual(start.call_args.kwargs['profile_dir'],
                         os.path.join(self.tmp, 'profile'))

    def test_start_failure_is_reported(self):
        with unittest.mock.patch.object(
                browser_session, 'start',
                side_effect=browser_session.BrowserSessionError('no docker')):
            r = self.client.post('/api/browser-session/start',
                                 json={'agentId': 'agent-a'})
        self.assertEqual(r.status_code, 500)
        self.assertIn('no docker', r.json()['error'])

    def test_stop(self):
        with unittest.mock.patch.object(browser_session, 'stop', return_value=True):
            r = self.client.post('/api/browser-session/stop',
                                 json={'agentId': 'agent-a'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['stopped'])

    def test_takeover_missing_session_is_404(self):
        with unittest.mock.patch.object(
                browser_session, 'takeover',
                side_effect=browser_session.BrowserSessionError('none')):
            r = self.client.post('/api/browser-session/takeover',
                                 json={'agentId': 'ghost'})
        self.assertEqual(r.status_code, 404)

    def test_handback(self):
        with unittest.mock.patch.object(browser_session, 'handback',
                                        return_value=dict(SESSION, takeover=False)):
            r = self.client.post('/api/browser-session/handback',
                                 json={'agentId': 'agent-a'})
        self.assertEqual(r.status_code, 200)

    def test_view_rejects_unknown_token(self):
        with unittest.mock.patch.object(browser_session, 'session_by_token',
                                        return_value=None):
            r = self.client.get('/api/browser-session/view?token=nope')
        self.assertEqual(r.status_code, 404)

    def test_view_embeds_same_origin_proxy(self):
        match = {'agentId': 'agent-a', 'port': 43210, 'password': 'pw',
                 'status': 'running', 'takeover': False, 'token': 'tok-123'}
        with unittest.mock.patch.object(browser_session, 'session_by_token',
                                        return_value=match):
            r = self.client.get('/api/browser-session/view?token=tok-123')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('/api/browser-session/vnc/tok-123/vnc.html', r.text)
        self.assertIn('path=/api/browser-session/vnc/tok-123/websockify', r.text)
        self.assertIn('/api/browser-session/handback', r.text)
        self.assertNotIn('127.0.0.1:43210', r.text)

    def test_asset_proxy_serves_upstream_bytes(self):
        class _Resp:
            headers = {'Content-Type': 'application/javascript'}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=None):
                return b'console.log(1)'
        match = {'agentId': 'agent-a', 'port': 43210, 'password': 'pw',
                 'status': 'running', 'takeover': False, 'token': 'tok-123'}
        with unittest.mock.patch.object(browser_session, 'session_by_token',
                                        return_value=match), \
                unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                           return_value=_Resp()):
            r = self.client.get('/api/browser-session/vnc/tok-123/app/ui.js')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, b'console.log(1)')
        self.assertIn('javascript', r.headers.get('content-type', ''))

    def test_asset_proxy_requires_token(self):
        with unittest.mock.patch.object(browser_session, 'session_by_token',
                                        return_value=None):
            r = self.client.get('/api/browser-session/vnc/nope/vnc.html')
        self.assertEqual(r.status_code, 404)

    def test_websocket_requires_session_cookie(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            with self.assertRaises(Exception):
                with self.client.websocket_connect(
                        '/api/browser-session/vnc/tok-123/websockify') as ws:
                    ws.receive_text()

    def test_asset_proxy_requires_player(self):
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False):
            r = self.client.get('/api/browser-session/vnc/tok-123/vnc.html')
        self.assertEqual(r.status_code, 401)

    def test_view_requires_player(self):
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False):
            r = self.client.get('/api/browser-session/view?token=tok-123')
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()
