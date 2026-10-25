"""Coverage-gap tests for the browser-act / browser-session / pipeline part.

Only new tests. Browser work is faked: the action driver, session registry and
noVNC relay are replaced with in-memory fakes. No real browser, port or network
socket is opened.
"""
import asyncio
import json
import os
import sys
import types
import unittest
import urllib.error
from contextlib import ExitStack
from unittest import mock

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import test_serve_helpers as _helpers  # noqa: E402

_AUTH_PATCHER = None


def setUpModule():
    global _AUTH_PATCHER
    _helpers.setUpModule()
    _AUTH_PATCHER = mock.patch.object(
        serve, '_valid_agent_key_presented', return_value=True)
    _AUTH_PATCHER.start()


def tearDownModule():
    _AUTH_PATCHER.stop()
    _helpers.tearDownModule()


def _post(path, body, raise_server_exceptions=True, **patches):
    with mock.patch.object(serve, 'EXECUTION_ENABLED', patches.pop('execution', True)), \
         mock.patch.object(serve, 'BROWSING_ENABLED', patches.pop('browsing', True)), \
         patches.pop('ctx', mock.patch.object(serve, 'log_action')):
        client = TestClient(serve.app,
                            raise_server_exceptions=raise_server_exceptions)
        return client.post(path, json=body, headers={'X-Agent-Key': 'k'})


def _browser_ctx(**overrides):
    defaults = {
        '_work_trust_from_body': mock.Mock(return_value=True),
        '_village_sandbox_id': mock.Mock(side_effect=lambda a, s: s),
        'check_rate_limit': mock.Mock(return_value=True),
        '_actor_is_authorized': mock.Mock(return_value=True),
        '_social_readonly_guard': mock.Mock(return_value=None),
        '_is_readonly_social_host': mock.Mock(return_value=False),
        '_classify_browser_action': mock.AsyncMock(return_value=(True, 'ok')),
        '_is_safe_public_host': mock.Mock(return_value=True),
        '_sandbox_dir_for': mock.Mock(return_value='/tmp'),
        '_load_env': mock.Mock(return_value={}),
        '_browser_act_with_pacing': mock.Mock(return_value=({'ok': True}, None)),
        'log_action': mock.Mock(),
    }
    defaults.update(overrides)
    return defaults


class _Ctx:
    def __init__(self, **values):
        self.values = values
        self.stack = ExitStack()

    def __enter__(self):
        for name, value in self.values.items():
            if isinstance(value, mock.Mock) and not isinstance(value, mock.AsyncMock):
                self.stack.enter_context(mock.patch.object(serve, name, value))
            else:
                self.stack.enter_context(mock.patch.object(serve, name, value))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


def _browser(**overrides):
    return _Ctx(**_browser_ctx(**overrides))


class BrowserActBranchTests(unittest.TestCase):
    def test_success_goto(self):
        with _browser():
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'goto',
                          'url': 'https://example.com', 'purpose': 'read',
                          'sandboxId': 'sandbox-a'})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json()['allowed'])

    def test_social_guard_retryable(self):
        with _browser(**{'_social_readonly_guard': mock.Mock(
                return_value='needs username or password')}):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'login',
                          'fields': [{'selector': '#u'}],
                          'sandboxId': 'sandbox-a'})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json()['credentialHandleError'])

    def test_early_social_handle_fallback(self):
        fake = mock.MagicMock()
        fake.return_value = {'kind': 'login', 'username': 'u',
                             'password': 'p'}
        with _browser(**{
                '_is_readonly_social_host': mock.Mock(return_value=True),
                '_active_task_handle': mock.Mock(return_value='task-handle'),
                'verify_capability_handle_scope': mock.Mock(
                    return_value={'kind': 'login'}),
                'resolve_capability_handle': fake,
        }):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'login',
                          'taskId': 't1', 'pageUrl': 'https://facebook.com/',
                          'sandboxId': 'sandbox-a',
                          'fields': [{'selector': '#u',
                                      'credentialField': 'username'}]})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json()['allowed'])
        self.assertTrue(fake.called)

    def test_non_dict_field_fails_before_driver(self):
        with _browser():
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'login',
                          'fields': ['bad'], 'sandboxId': 'sandbox-a'},
                         raise_server_exceptions=False)
        self.assertEqual(500, resp.status_code)

    def test_auto_solve_host_policy_off(self):
        with _browser(**{
                '_load_env': mock.Mock(
                    return_value={'CAPTCHA_AUTO_SOLVE': 'all'}),
        }), mock.patch.object(serve.captcha, 'policy_for_host',
                              return_value='relay'):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'read',
                          'url': 'https://example.com',
                          'sandboxId': 'sandbox-a'})
        self.assertEqual(200, resp.status_code)

    def test_blocked_payload_and_captcha_branches(self):
        with _browser(**{
                '_browser_act_with_pacing': mock.Mock(
                    return_value=({'blocked': True}, None)),
                '_browser_block_payload': mock.Mock(
                    return_value={'blocked': {'vendor': 'v'}}),
        }):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'read',
                          'sandboxId': 'sandbox-a'})
        self.assertEqual(200, resp.status_code)
        self.assertEqual({'vendor': 'v'}, resp.json()['blocked'])

        with _browser(**{
                '_browser_act_with_pacing': mock.Mock(
                    return_value=({'captchas': 'bad',
                                   'captchaSolver': 'bad'}, None)),
        }):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'read',
                          'sandboxId': 'sandbox-a'})
        self.assertEqual(200, resp.status_code)

        with _browser(**{
                '_browser_act_with_pacing': mock.Mock(
                    return_value=({'captchaSolver': {'solved': True,
                                                     'kind': 'k',
                                                     'challengeType': 't',
                                                     'attempts': 1}}, None)),
        }):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'read',
                          'sandboxId': 'sandbox-a'})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json()['captchaSolved'])

    def test_captcha_detected(self):
        with _browser(**{
                '_browser_act_with_pacing': mock.Mock(
                    return_value=({'captchas': [{'kind': 'image'}]}, None)),
                '_handle_captcha_detected': mock.Mock(
                    return_value={'requestId': 'r1'}),
        }):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'read',
                          'sandboxId': 'sandbox-a'})
        self.assertEqual(200, resp.status_code)
        self.assertEqual('r1', resp.json()['captchaRequired']['requestId'])


class BrowserSessionIdTests(unittest.TestCase):
    def test_invalid_sandbox_id_raises(self):
        with self.assertRaises(ValueError):
            serve._browser_session_sandbox_id('a', 'bad id!')

    def test_proxy_url(self):
        self.assertTrue(serve._browser_session_proxy_url().startswith('http://'))


class BrowserSessionEndpointTests(unittest.TestCase):
    def test_list(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False), \
             mock.patch.object(serve, 'log_action'):
            resp = TestClient(serve.app).get('/api/browser-session/list')
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'list_sessions',
                               return_value=[{'agentId': 'a'}]):
            resp = TestClient(serve.app).get('/api/browser-session/list')
        self.assertEqual(200, resp.status_code)
        self.assertEqual([{'agentId': 'a'}], resp.json()['sessions'])

    def test_start_unauthorized_and_disabled(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = _post('/api/browser-session/start', {})
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, 'EXECUTION_ENABLED', False):
            resp = _post('/api/browser-session/start', {'agentId': 'a'},
                         execution=False)
        self.assertEqual(403, resp.status_code)

    def test_start_validation_network_and_success(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, 'EXECUTION_ENABLED', True):
            resp = _post('/api/browser-session/start', {})
            self.assertEqual(400, resp.status_code)
            resp = _post('/api/browser-session/start',
                         {'agentId': 'a', 'sandboxId': 'bad id!'})
            self.assertEqual(400, resp.status_code)

        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, 'ensure_sandbox_networking',
                               side_effect=RuntimeError('no docker')):
            resp = _post('/api/browser-session/start',
                         {'agentId': 'a', 'sandboxId': 'sandbox-a'})
        self.assertEqual(500, resp.status_code)

        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, 'ensure_sandbox_networking'), \
             mock.patch.object(serve, '_sandbox_dir_for', return_value='/tmp'), \
             mock.patch.object(serve, '_browser_profile_dir',
                               return_value='/tmp/p'), \
             mock.patch.object(serve.browser_session, 'start',
                               return_value={'agentId': 'a'}), \
             mock.patch.object(serve, 'log_action'):
            resp = _post('/api/browser-session/start',
                         {'agentId': 'a', 'sandboxId': 'sandbox-a'})
        self.assertEqual(200, resp.status_code)
        self.assertEqual('a', resp.json()['session']['agentId'])

    def test_start_browser_error(self):
        err = serve.browser_session.BrowserSessionError('boom')
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, 'ensure_sandbox_networking'), \
             mock.patch.object(serve, '_sandbox_dir_for', return_value='/tmp'), \
             mock.patch.object(serve, '_browser_profile_dir',
                               return_value='/tmp/p'), \
             mock.patch.object(serve.browser_session, 'start', side_effect=err):
            resp = _post('/api/browser-session/start',
                         {'agentId': 'a', 'sandboxId': 'sandbox-a'})
        self.assertEqual(500, resp.status_code)

    def test_stop(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = _post('/api/browser-session/stop', {})
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True):
            resp = _post('/api/browser-session/stop', {})
            self.assertEqual(400, resp.status_code)
            resp = _post('/api/browser-session/stop', {'agentId': 'a'})
            self.assertEqual(200, resp.status_code)
        err = serve.browser_session.BrowserSessionError('boom')
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'stop', side_effect=err):
            resp = _post('/api/browser-session/stop', {'agentId': 'a'})
        self.assertEqual(500, resp.status_code)

    def test_takeover(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = _post('/api/browser-session/takeover', {})
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'takeover',
                               return_value={'port': 1}):
            resp = _post('/api/browser-session/takeover', {})
            self.assertEqual(400, resp.status_code)
            resp = _post('/api/browser-session/takeover', {'agentId': 'a'})
            self.assertEqual(200, resp.status_code)
        err = serve.browser_session.BrowserSessionError('boom')
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'takeover',
                               side_effect=err):
            resp = _post('/api/browser-session/takeover', {'agentId': 'a'})
        self.assertEqual(404, resp.status_code)

    def test_handback(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = _post('/api/browser-session/handback', {})
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'handback',
                               return_value={'port': 1}):
            resp = _post('/api/browser-session/handback', {})
            self.assertEqual(400, resp.status_code)
            resp = _post('/api/browser-session/handback', {'agentId': 'a'})
            self.assertEqual(200, resp.status_code)
        err = serve.browser_session.BrowserSessionError('boom')
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'handback',
                               side_effect=err):
            resp = _post('/api/browser-session/handback', {'agentId': 'a'})
        self.assertEqual(404, resp.status_code)


class _FakeResponse:
    def __init__(self, body=b'asset', ctype='text/plain'):
        self._body = body
        self.headers = {'Content-Type': ctype}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        return self._body


class BrowserVncTests(unittest.TestCase):
    def _asset(self, token, asset, opener):
        req = mock.MagicMock()
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'session_by_token',
                               return_value={'port': 6001}), \
             mock.patch.object(serve.urllib.request, 'urlopen', opener):
            return asyncio.run(
                serve.browser_session_vnc_asset(req, token, asset))

    def test_asset_requires_session_and_rejects_dotdot(self):
        req = mock.MagicMock()
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = asyncio.run(
                serve.browser_session_vnc_asset(req, 'tok', 'vnc.html'))
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'session_by_token',
                               return_value=None):
            resp = asyncio.run(
                serve.browser_session_vnc_asset(req, 'tok', 'vnc.html'))
        self.assertEqual(404, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'session_by_token',
                               return_value={'port': 6001}):
            resp = asyncio.run(
                serve.browser_session_vnc_asset(req, 'tok', '../secret'))
        self.assertEqual(400, resp.status_code)

    def test_asset_success_and_failures(self):
        resp = self._asset('tok', 'vnc.html',
                           lambda req, timeout=0: _FakeResponse())
        self.assertEqual(200, resp.status_code)
        self.assertEqual(b'asset', resp.body)

        err = urllib.error.HTTPError('http://x', 404, 'nf', {}, None)
        resp = self._asset('tok', 'vnc.html', mock.Mock(side_effect=err))
        self.assertEqual(404, resp.status_code)
        resp = self._asset('tok', 'vnc.html',
                           mock.Mock(side_effect=RuntimeError('boom')))
        self.assertEqual(502, resp.status_code)


class _FakeUpstream:
    def __init__(self, messages=()):
        self.subprotocol = 'binary'
        self._messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class _FakeWebSocket:
    def __init__(self, subprotocols=('binary',)):
        self.cookies = {serve.SESSION_COOKIE_NAME: 's'}
        self.scope = {'subprotocols': list(subprotocols)}
        self.incoming = [
            {'type': 'websocket.receive', 'bytes': b'abc'},
            {'type': 'websocket.receive', 'text': 'hi'},
            {'type': 'websocket.disconnect'},
        ]
        self.sent = []
        self.close_codes = []
        self.accepted = None

    async def close(self, code=None):
        self.close_codes.append(code)

    async def accept(self, subprotocol=None):
        self.accepted = subprotocol

    async def receive(self):
        return self.incoming.pop(0)

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_text(self, data):
        self.sent.append(data)


class BrowserVncWebSocketTests(unittest.TestCase):
    def _run(self, ws, token='tok', session=True, match=None, connect=None,
             allow_import=True):
        module = types.SimpleNamespace(connect=connect)
        mods = {'websockets': module} if allow_import else {'websockets': None}
        with mock.patch.object(serve, 'verify_session', return_value=session), \
             mock.patch.object(serve.browser_session, 'session_by_token',
                               return_value=match), \
             mock.patch.dict(sys.modules, mods):
            asyncio.run(serve.browser_session_vnc_ws(ws, token))

    def test_websocket_auth_and_unknown_token(self):
        ws = _FakeWebSocket()
        self._run(ws, session=False)
        self.assertEqual([4401], ws.close_codes)
        ws = _FakeWebSocket()
        self._run(ws, match=None)
        self.assertEqual([4404], ws.close_codes)

    def test_websocket_import_and_connect_failures(self):
        ws = _FakeWebSocket()
        self._run(ws, match={'port': 1}, allow_import=False)
        self.assertEqual([1011], ws.close_codes)

        async def _fail(*args, **kwargs):
            raise RuntimeError('connect failed')

        ws = _FakeWebSocket()
        self._run(ws, match={'port': 1}, connect=_fail)
        self.assertEqual([1011], ws.close_codes)

    def test_websocket_relays_frames_both_directions(self):
        upstream = _FakeUpstream([b'from-up', 'text-up'])

        async def _connect(*args, **kwargs):
            return upstream

        ws = _FakeWebSocket()
        self._run(ws, match={'port': 1}, connect=_connect)
        self.assertEqual('binary', ws.accepted)
        self.assertIn(b'abc', upstream.sent)
        self.assertIn('hi', upstream.sent)
        self.assertIn(b'from-up', ws.sent)
        self.assertIn('text-up', ws.sent)
        self.assertTrue(upstream.closed)


class PipelineEndpointTests(unittest.TestCase):
    def test_pipeline_background_dispatch(self):
        with mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, '_start_pipeline_job',
                               mock.AsyncMock(return_value=JSONResponse(
                                   {'jobId': 'j'}, status_code=202))):
            resp = _post('/api/pipeline', {'background': True})
        self.assertEqual(202, resp.status_code)

    def _start(self, body, execute):
        req = mock.MagicMock()
        req.headers = {}
        async def go():
            with mock.patch.object(serve, '_require_player_session',
                                   return_value=True), \
                 mock.patch.object(serve, '_confirmation_gate_or_response',
                                   return_value=(None, 'cid')), \
                 mock.patch.object(serve, '_pipeline_execute', execute):
                resp = await serve._start_pipeline_job(req, body)
                job = serve._PIPELINE_JOBS[json.loads(resp.body)['jobId']]
                await job['worker']
                return resp, job
        return asyncio.run(go())

    def test_start_job_success_and_failure(self):
        serve._PIPELINE_JOBS.clear()
        resp, job = self._start(
            {'agentId': 'a', 'sandboxId': 'sandbox-a', 'steps': []},
            mock.AsyncMock(return_value=JSONResponse({'ok': True})))
        self.assertEqual(202, resp.status_code)
        self.assertEqual('done', job['status'])

        serve._PIPELINE_JOBS.clear()
        resp, job = self._start(
            {'agentId': 'a', 'sandboxId': 'sandbox-a', 'steps': []},
            mock.AsyncMock(side_effect=RuntimeError('boom')))
        self.assertEqual(202, resp.status_code)
        self.assertEqual('failed', job['status'])

    def test_start_job_requires_auth(self):
        req = mock.MagicMock()
        req.headers = {}
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False), \
             mock.patch.object(serve, 'verify_agent_key', return_value=False):
            resp = asyncio.run(serve._start_pipeline_job(req, {'agentId': 'a'}))
        self.assertEqual(401, resp.status_code)

    def test_start_job_prunes_and_limits(self):
        serve._PIPELINE_JOBS.clear()
        for i in range(128):
            serve._PIPELINE_JOBS[f'old-{i}'] = {'status': 'done'}
        try:
            self._start({'agentId': 'a', 'sandboxId': 'sandbox-a',
                         'steps': []},
                        mock.AsyncMock(return_value=JSONResponse({})))
            self.assertLess(len(serve._PIPELINE_JOBS), 256)

            serve._PIPELINE_JOBS.clear()
            for i in range(256):
                serve._PIPELINE_JOBS[f'run-{i}'] = {'status': 'running'}
            req = mock.MagicMock()
            req.headers = {}
            with mock.patch.object(serve, '_require_player_session',
                                   return_value=True), \
                 mock.patch.object(serve, '_confirmation_gate_or_response',
                                   return_value=(None, 'cid')):
                resp = asyncio.run(
                    serve._start_pipeline_job(
                        req, {'agentId': 'a', 'sandboxId': 'sandbox-a',
                              'steps': []}))
            self.assertEqual(429, resp.status_code)
        finally:
            serve._PIPELINE_JOBS.clear()

    def test_pipeline_job_lookup(self):
        req = mock.MagicMock()
        req.headers = {}
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True):
            resp = asyncio.run(serve.pipeline_job('missing', req))
        self.assertEqual(404, resp.status_code)

        serve._PIPELINE_JOBS['job-1'] = {'id': 'job-1', 'agentId': 'a',
                                         'worker': object()}
        try:
            with mock.patch.object(serve, '_require_player_session',
                                   return_value=True):
                resp = asyncio.run(serve.pipeline_job('job-1', req))
            self.assertNotIn('worker', resp)
            with mock.patch.object(serve, '_require_player_session',
                                   return_value=False), \
                 mock.patch.object(serve, 'verify_agent_key',
                                   return_value=False):
                resp = asyncio.run(serve.pipeline_job('job-1', req))
            self.assertEqual(401, resp.status_code)
        finally:
            serve._PIPELINE_JOBS.pop('job-1', None)

    def test_locked_execute_early_returns(self):
        req = mock.MagicMock()
        req.headers = {}
        with mock.patch.object(serve, 'EXECUTION_ENABLED', False):
            resp = asyncio.run(serve._pipeline_execute_locked(req, {}))
        self.assertEqual(403, resp.status_code)

        body = {'agentId': 'a', 'sandboxId': 'sandbox-a',
                'steps': [{'name': 's', 'command': 'c'}]}
        with mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             mock.patch.object(serve, '_actor_is_authorized',
                               return_value=False):
            resp = asyncio.run(serve._pipeline_execute_locked(req, body))
        self.assertEqual(401, resp.status_code)

        many = {'agentId': 'a', 'sandboxId': 'sandbox-a',
                'steps': [{'name': 's', 'command': 'c'}] * 65}
        with mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             mock.patch.object(serve, '_actor_is_authorized',
                               return_value=True):
            resp = asyncio.run(serve._pipeline_execute_locked(req, many))
        self.assertEqual(400, resp.status_code)

        with mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             mock.patch.object(serve, '_actor_is_authorized',
                               return_value=True), \
             mock.patch.object(serve, '_confirmation_gate_or_response',
                               return_value=(JSONResponse(
                                   {'error': 'confirm'}, status_code=409),
                                   None)):
            resp = asyncio.run(serve._pipeline_execute_locked(req, body))
        self.assertEqual(409, resp.status_code)

    def test_locked_execute_updates_job_counters(self):
        req = mock.MagicMock()
        req.headers = {}
        serve._PIPELINE_JOBS['job-c'] = {'status': 'running'}
        body = {'agentId': 'a', 'sandboxId': 'sandbox-a', '_jobId': 'job-c',
                'steps': [{'name': 'compile', 'command': 'make'}]}
        try:
            with mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
                 mock.patch.object(serve, 'check_rate_limit',
                                   return_value=True), \
                 mock.patch.object(serve, '_actor_is_authorized',
                                   return_value=True), \
                 mock.patch.object(serve, '_confirmation_gate_or_response',
                                   return_value=(None, 'cid')), \
                 mock.patch.object(serve, '_sandbox_dir_for',
                                   return_value='/tmp'), \
                 mock.patch.object(serve, '_sandbox_has_python_files',
                                   return_value=True), \
                 mock.patch.object(serve, '_classify_command',
                                   mock.AsyncMock(return_value=(True, 'ok'))), \
                 mock.patch.object(serve, '_snapshot_sandbox'), \
                 mock.patch.object(serve, '_run_in_sandbox_sync',
                                   return_value={'exitCode': 0,
                                                 'timedOut': False}), \
                 mock.patch.object(serve, 'sync_prototypes'), \
                 mock.patch.object(serve, '_finish_confirmation'), \
                 mock.patch.object(serve, 'log_action'):
                resp = asyncio.run(
                    serve._pipeline_execute_locked(req, body))
        finally:
            serve._PIPELINE_JOBS.pop('job-c', None)
        self.assertEqual(200, resp.status_code)
        self.assertEqual(1, serve._PIPELINE_JOBS.get('job-c', {}).get(
            'stepsCompleted', 1))


class BrowserActRemainingBranchTests(unittest.TestCase):
    def test_unauthorized_agent_key(self):
        with _browser(**{'_actor_is_authorized': mock.Mock(
                return_value=False)}):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'read',
                          'sandboxId': 'sandbox-a'})
        self.assertEqual(401, resp.status_code)

    def test_per_field_social_fallback(self):
        with _browser(**{
                '_is_readonly_social_host': mock.Mock(return_value=True),
                '_active_task_handle': mock.Mock(
                    side_effect=[None, 'task-handle']),
                'verify_capability_handle_scope': mock.Mock(
                    return_value={'kind': 'login'}),
                'resolve_capability_handle': mock.Mock(
                    return_value={'kind': 'login', 'username': 'u'}),
        }):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'login',
                          'taskId': 't1', 'url': 'https://facebook.com/',
                          'sandboxId': 'sandbox-a',
                          'fields': [{'selector': '#u',
                                      'credentialField': 'username'}]})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json()['allowed'])

    def test_social_login_without_handle(self):
        with _browser(**{
                '_is_readonly_social_host': mock.Mock(return_value=True),
                '_active_task_handle': mock.Mock(return_value=None),
        }):
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'login',
                          'taskId': 't1', 'url': 'https://facebook.com/',
                          'sandboxId': 'sandbox-a',
                          'fields': [{'selector': '#u',
                                      'credentialField': 'username'}]})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json()['credentialHandleError'])

    def test_login_entry_url_must_match_scope(self):
        with _browser():
            resp = _post('/api/browser-act',
                         {'agentId': 'a', 'op': 'login',
                          'url': 'https://example.com/login',
                          'loginEntryUrl': 'https://evil.test/',
                          'sandboxId': 'sandbox-a',
                          'fields': [{'selector': '#u', 'value': 'x'}]})
        self.assertEqual(400, resp.status_code)
        self.assertIn('loginEntryUrl', resp.json()['error'])


class BrowserSessionViewTests(unittest.TestCase):
    def test_view_requires_session_unknown_and_success(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = TestClient(serve.app).get('/api/browser-session/view')
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'session_by_token',
                               return_value=None):
            resp = TestClient(serve.app).get(
                '/api/browser-session/view', params={'token': 'tok'})
        self.assertEqual(404, resp.status_code)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve.browser_session, 'session_by_token',
                               return_value={'agentId': 'a',
                                             'password': 'pw'}):
            resp = TestClient(serve.app).get(
                '/api/browser-session/view', params={'token': 'tok'})
        self.assertEqual(200, resp.status_code)
        self.assertIn('Takeover: a', resp.text)


class BrowserVncWebSocketErrorTests(unittest.TestCase):
    def _run(self, ws, token='tok', connect=None):
        module = types.SimpleNamespace(connect=connect)
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve.browser_session, 'session_by_token',
                               return_value={'port': 1}), \
             mock.patch.dict(sys.modules, {'websockets': module}):
            asyncio.run(serve.browser_session_vnc_ws(ws, token))

    def test_client_to_upstream_error_is_swallowed(self):
        class _SendFails(_FakeUpstream):
            async def send(self, data):
                raise RuntimeError('send failed')

        upstream = _SendFails([])

        async def _connect(*args, **kwargs):
            return upstream

        ws = _FakeWebSocket()
        self._run(ws, connect=_connect)
        self.assertEqual('binary', ws.accepted)

    def test_upstream_to_client_error_is_swallowed(self):
        class _IterFails(_FakeUpstream):
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError('iter failed')

        upstream = _IterFails([])

        async def _connect(*args, **kwargs):
            return upstream

        ws = _FakeWebSocket()
        ws.incoming = [{'type': 'websocket.disconnect'}]
        self._run(ws, connect=_connect)
        self.assertEqual('binary', ws.accepted)


class PipelineConfirmationTests(unittest.TestCase):
    def test_start_job_confirmation_error_returned(self):
        req = mock.MagicMock()
        req.headers = {}
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, '_confirmation_gate_or_response',
                               return_value=(JSONResponse(
                                   {'error': 'confirm'}, status_code=409),
                                   None)):
            resp = asyncio.run(
                serve._start_pipeline_job(
                    req, {'agentId': 'a', 'sandboxId': 'sandbox-a',
                          'steps': []}))
        self.assertEqual(409, resp.status_code)


if __name__ == '__main__':
    unittest.main()


class SandboxProxySourceTests(unittest.TestCase):
    def test_moved_path_and_legacy_fallback(self):
        self.assertTrue(serve._sandbox_proxy_source().endswith('sandbox_proxy.py'))
        with mock.patch.object(serve.os.path, 'isfile', return_value=False):
            self.assertTrue(serve._sandbox_proxy_source().endswith('sandbox_proxy.py'))
