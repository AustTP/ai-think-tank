"""Generic api_call: the registry-gated /api/api-call path for external
services (config, not code). Covers the registry loader and host/path lookup,
credential resolution and auth injection, the raw request helper (including
the redirect re-check), and the endpoint's gate order -- read-only for
unregistered hosts, path/method enforcement and credential injection for
registered hosts, Jev gating, and page/spend accrual.

Hermetic: the endpoint's only IO is log_action (DB) and the model gate, so
this suite patches those along with the network fetch. No real outbound call
or DB write happens in these tests.
"""
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-api-call-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        API_CALL_ENABLED=True,
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class _FakeResponse:
    def __init__(self, body, status=200, content_type='application/json', final_url=None):
        self._body = body
        self.status = status
        self.headers = {'content-type': content_type}
        self._final_url = final_url or 'https://example.com/x'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._final_url

    def read(self, n=-1):
        data = self._body if isinstance(self._body, bytes) else self._body.encode()
        return data[:n] if n != -1 else data
_VALID_SPEC = {
    'id': 'my_service',
    'name': 'My Service',
    'base_url': 'https://api.myservice.example.com',
    'credential': {'type': 'env', 'key': 'MY_SERVICE_TOKEN'},
    'auth': {'type': 'header_bearer'},
    'methods': ['GET', 'POST'],
    'path_rules': [{'prefix': '/', 'methods': ['GET', 'POST']}],
    'spend': {'kind': 'none'},
}



class Channels(unittest.TestCase):
    """Read-only channels: host->ordered-backend-chain lookup from the JSON
    seed, fallthrough behavior (first backend that answers wins), the
    player-only probe, and the endpoint wiring that routes an unregistered
    channel host through the chain instead of a plain direct GET."""

    def setUp(self):
        serve._load_api_services()

    def test_channel_host_lookup_exact_and_subdomain(self):
        self.assertEqual(serve._api_channel_for_host('reddit.com')['id'], 'reddit')
        self.assertEqual(serve._api_channel_for_host('www.reddit.com')['id'], 'reddit')
        self.assertEqual(serve._api_channel_for_host('x.com')['id'], 'twitter')
        self.assertEqual(serve._api_channel_for_host('sub.x.com')['id'], 'twitter')
        self.assertIsNone(serve._api_channel_for_host('example.com'))

    def test_channel_backend_url_rewrites(self):
        backend = {'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'}
        self.assertEqual(
            serve._api_backend_url(backend, 'https://reddit.com/r/programming'),
            'https://r.jina.ai/https://reddit.com/r/programming')

    def test_channel_backend_requires_registered_service(self):
        # A channel whose backend is NOT a registered service fails closed.
        chan = {'name': 'fake', 'backends': [{'service': 'does-not-exist', 'url_template': '{url}'}]}
        out = serve._api_channel_execute('ben', True, chan, 'https://x.com/s', 'GET', {}, None, 'p', None)
        self.assertTrue(out['allowed'])
        self.assertFalse(out['ok'])
        self.assertIn('not a registered service', out['error'])

    def test_channel_routes_through_first_working_backend(self):
        # Ordered chain: first backend that answers wins.
        calls = []
        fake_result = {'status': 200, 'finalUrl': 'https://r.jina.ai/https://reddit.com/r/x',
                       'contentType': 'text/markdown', 'body': '# Hello', 'truncated': False}
        def fake_req(method, url, headers, body):
            calls.append(url)
            return fake_result
        chan = {'name': 'Reddit', 'hosts': ['reddit.com'],
                'backends': [{'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'}]}
        with unittest.mock.patch.object(serve, '_api_request_sync', side_effect=fake_req):
            out = serve._api_channel_execute('ben', True, chan, 'https://reddit.com/r/x', 'GET', {}, None, 'p', None)
        self.assertTrue(out['ok'])
        self.assertEqual(calls, ['https://r.jina.ai/https://reddit.com/r/x'])
        self.assertEqual(out['channel'], 'Reddit')
        self.assertEqual(out['backend'], 'jina-reader')
        self.assertEqual(out['finalUrl'], 'https://reddit.com/r/x')

    def test_channel_falls_through_on_backend_failure(self):
        # First backend raises/errors, second answers -> chain falls through.
        calls = []
        fake_ok = {'status': 200, 'finalUrl': 'https://r.jina.ai/https://reddit.com/r/x',
                   'contentType': 'text/markdown', 'body': 'ok', 'truncated': False}
        def fake_req(method, url, headers, body):
            calls.append(url)
            if url.startswith('https://first.example'):
                raise RuntimeError('backend down')
            return fake_ok
        chan = {'name': 'Reddit', 'hosts': ['reddit.com'],
                'backends': [
                    {'service': 'jina-reader', 'url_template': 'https://first.example/{url}'},
                    {'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'},
                ]}
        with unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_api_request_sync', side_effect=fake_req):
            out = serve._api_channel_execute('ben', True, chan, 'https://reddit.com/r/x', 'GET', {}, None, 'p', None)
        self.assertTrue(out['ok'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], 'https://r.jina.ai/https://reddit.com/r/x')

    def test_channel_all_backends_fail_reports_error(self):
        def fake_req(method, url, headers, body):
            raise RuntimeError('all down')
        chan = {'name': 'Reddit', 'hosts': ['reddit.com'],
                'backends': [{'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'}]}
        with unittest.mock.patch.object(serve, '_api_request_sync', side_effect=fake_req):
            out = serve._api_channel_execute('ben', True, chan, 'https://reddit.com/r/x', 'GET', {}, None, 'p', None)
        self.assertTrue(out['allowed'])
        self.assertFalse(out['ok'])
        self.assertIn('All backends', out['error'])

    def test_channel_is_read_only(self):
        chan = {'name': 'Reddit', 'hosts': ['reddit.com'],
                'backends': [{'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'}]}
        out = serve._api_channel_execute('ben', True, chan, 'https://reddit.com/r/x', 'POST', {}, 'b', 'p', None)
        self.assertFalse(out['allowed'])
        self.assertIn('read-only', out['reason'])

    def test_endpoint_routes_unregistered_channel_host(self):
        # An unregistered host owned by a channel (reddit.com) goes through the
        # channel chain via /api/api-call: Jev runs (unregistered), then the
        # backend is fetched rather than a direct GET to reddit.com.
        fake_result = {'status': 200, 'finalUrl': 'https://r.jina.ai/https://reddit.com/r/x',
                       'contentType': 'text/markdown', 'body': '# Thread', 'truncated': False}
        self._start(self._gates())
        with unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result) as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://reddit.com/r/programming', 'method': 'GET', 'purpose': 'read thread'})
        out = r.json()
        self.assertTrue(out['allowed'])
        self.assertIn('Thread', out['textForModel'])
        sent_url = fetch.call_args.args[1]
        self.assertTrue(sent_url.startswith('https://r.jina.ai/'))
        self.assertIn('reddit.com', sent_url)

    def _client(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _gates(self, jev_allow=True):
        async def _allow(instructions, criteria):
            return ('allow' if jev_allow else 'block', 0.99 if jev_allow else 0.1, 0.0, 'trace-1')
        return [
            unittest.mock.patch.object(serve, 'API_CALL_ENABLED', True),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, '_jev_quorum_decision', _allow),
            unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=jev_allow),
            unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False),
            unittest.mock.patch.object(serve, 'log_action'),
        ]

    def _start(self, patchers):
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_channel_probe_requires_player_session(self):
        # Agent key alone must not run the probe -- it makes real outbound calls.
        from starlette.testclient import TestClient
        with unittest.mock.patch.multiple(
                serve, check_rate_limit=lambda a: True, verify_agent_key=lambda a, k: True,
                _valid_agent_key_presented=lambda k: True, log_action=lambda *a, **k: None):
            c = TestClient(serve.app)
            r = c.get('/api/channel-probe', headers={'X-Agent-Key': 'somekey'})
        self.assertEqual(r.status_code, 401)

    def test_channel_probe_reports_current_backend(self):
        # Player session: probe runs real (mocked) GETs per backend and reports
        # which backend currently answers, mirroring agent-reach doctor.
        from starlette.testclient import TestClient
        class _Resp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self, n=-1):
                return b''
        with unittest.mock.patch.multiple(
                serve, check_rate_limit=lambda a: True, log_action=lambda *a, **k: None,
                _is_safe_public_host=lambda h: True), \
             unittest.mock.patch.object(serve, '_safe_urlopen', return_value=_Resp()):
            c = TestClient(serve.app)
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
            r = c.get('/api/channel-probe')
        self.assertEqual(r.status_code, 200)
        channels = r.json()['channels']
        self.assertIn('reddit', channels)
        self.assertTrue(channels['reddit']['ok'])
        self.assertEqual(channels['reddit']['current'], 'jina-reader')
        self.assertIn('twitter', channels)
        self.assertIn('linkedin', channels)


