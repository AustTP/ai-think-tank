"""Gap-cluster F coverage tests for serve.py.

Covers the chrome/screenshot helpers, sandbox backups, page probe,
escalations, activity feed, decision/review calibration, weekly review,
health/ops metrics, model-tier and jev endpoints, auth endpoints, _http_json,
_arm_idle_and_run, api_log, post_report, _stage_released_work, execute/pipeline,
the youtube-transcript endpoint, and the youtube transcript helpers.

Same isolation discipline as tests/test_serve.py: module-wide patcher redirects
every real DB / library / passport path into a throwaway temp dir, and a fresh
in-memory DB schema is created in setUpModule.  TestClient is used bare (no
context manager) and serve.get_state_from_db / save_state_to_db / log_action /
verify_session are patched around requests.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
import urllib.error

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve
import sim

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-serve-gap-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class _LoopExit(Exception):
    pass


class _FakePageProbeHarness:
    """Programmable fake for sync_playwright().chromium.launch()."""

    def __init__(self, base_globals=None, probe_error=False, custom_error=False, click_error=None):
        self.base_globals = base_globals or ['a', 'b']
        self.probe_error = probe_error
        self.custom_error = custom_error
        self.click_error = click_error

    def build(self):
        ctx = unittest.mock.MagicMock()
        ctx.__enter__.return_value = ctx
        browser = ctx.chromium.launch.return_value
        base = unittest.mock.MagicMock()
        page = unittest.mock.MagicMock()
        browser.new_page.side_effect = [base, page]
        base.evaluate.return_value = list(self.base_globals)
        page.on = unittest.mock.Mock()
        page.keyboard = unittest.mock.Mock()
        page.keyboard.press = unittest.mock.Mock()
        page.wait_for_timeout = unittest.mock.Mock()

        def _evaluate(expr, arg=None):
            if arg is not None:
                if self.custom_error:
                    raise RuntimeError('custom eval boom')
                return [{'name': 'custom1', 'type': 'boolean', 'value': True}]
            if 'Object.keys(window)' in expr:
                return list(self.base_globals) + ['custom1']
            if self.probe_error:
                raise RuntimeError('probe boom')
            return 'RESULT:' + expr

        page.evaluate.side_effect = _evaluate
        if self.click_error:
            page.click.side_effect = RuntimeError(self.click_error)
        return ctx


class JevEndpoints(unittest.TestCase):
    """decide, jev_model_get, jev_model_set, jev_calibration, review_escalate."""

    def test_decide_missing_key_is_500(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide', json={'state': {}, 'questions': []})
        self.assertEqual(r.status_code, 500)
        self.assertIn('OPENROUTER_API_KEY', r.json()['error'])

    def test_decide_missing_fields_is_400(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide', json={})
        self.assertEqual(r.status_code, 400)

    def test_decide_throttled_is_429(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, '_decide_allowed',
                                        return_value=(False, 'slow down')), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide',
                       json={'state': {'x': 1}, 'questions': ['q'], 'agentId': 'eli'})
        self.assertEqual(r.status_code, 429)
        body = r.json()
        self.assertTrue(body['throttled'])
        self.assertIn('retry_after_ms', body)
        self.assertEqual(log.call_args[0][1], 'decide_throttled')

    def test_decide_success(self):
        data = {'choice': 'allow', 'confidence': 0.9}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='m'), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=data), \
             unittest.mock.patch.object(serve, '_jev_choice',
                                        return_value=('allow', 0.9, 0.001)), \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue, \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide',
                       json={'state': {'x': 1}, 'questions': ['q'], 'agentId': 'eli'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['choice'], 'allow')
        accrue.assert_called_once_with('__jev__', 0.001, village_id='main')
        self.assertEqual(log.call_args[0][1], 'decide')

    def test_decide_http_error(self):
        err = urllib.error.HTTPError('http://x', 500, 'err', {}, io.BytesIO(b'provider down'))
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=err), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide',
                       json={'state': {'x': 1}, 'questions': ['q'], 'agentId': 'eli'})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()['error'], 'provider down')

    def test_decide_generic_error(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=RuntimeError('boom')), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide',
                       json={'state': {'x': 1}, 'questions': ['q'], 'agentId': 'eli'})
        self.assertEqual(r.status_code, 500)
        self.assertIn('boom', r.json()['error'])

    def test_jev_model_get_single_chain(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['a']), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='a'), \
             unittest.mock.patch.object(serve, '_jev_judge_model', return_value='j'), \
             unittest.mock.patch.object(serve, '_escalation_judge_drift_summary', return_value={}):
            c = TestClient(serve.app)
            r = c.get('/api/jev/model')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['slugs'], {'a': 'sole'})

    def test_jev_model_get_multi_chain(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['a', 'b']), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='a'), \
             unittest.mock.patch.object(serve, '_jev_judge_model', return_value='j'), \
             unittest.mock.patch.object(serve, '_escalation_judge_drift_summary', return_value={}):
            c = TestClient(serve.app)
            r = c.get('/api/jev/model')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['slugs'], {'a': 'available', 'b': 'available'})

    def test_jev_model_set_is_operator_gated(self):
        # require_login middleware admits a real session OR a valid
        # agent key; the handler's _operator_gate then admits a
        # player session or a director/admin agent. verify_session is
        # consulted once by the middleware, once by
        # _require_player_session, and once by _resolve_requester.
        with self.subTest('anonymous is rejected by the middleware'):
            with unittest.mock.patch.object(serve, 'verify_session',
                                            return_value=False):
                c = TestClient(serve.app)
                r = c.post('/api/jev/model', json={'model': 'x/y'})
            self.assertEqual(r.status_code, 401)
            self.assertIn('Unauthorized', r.json()['error'])
        with self.subTest('worker key is not an operator'):
            with unittest.mock.patch.object(serve, 'verify_session',
                                            side_effect=[False, False, False]), \
                 unittest.mock.patch.object(serve, '_valid_agent_key_presented',
                                            return_value=True), \
                 unittest.mock.patch.object(serve, '_agent_id_for_key',
                                            return_value='worker-1'), \
                 unittest.mock.patch.object(serve, '_is_director_or_admin',
                                            return_value=False):
                c = TestClient(serve.app)
                r = c.post('/api/jev/model', json={'model': 'x/y'})
            self.assertEqual(r.status_code, 403)
            self.assertIn('operator-only', r.json()['error'])
        with self.subTest('director key switches the model'):
            with unittest.mock.patch.object(serve, 'verify_session',
                                            side_effect=[False, False, False]), \
                 unittest.mock.patch.object(serve, '_valid_agent_key_presented',
                                            return_value=True), \
                 unittest.mock.patch.object(serve, '_agent_id_for_key',
                                            return_value='nora'), \
                 unittest.mock.patch.object(serve, '_is_director_or_admin',
                                            return_value=True), \
                 unittest.mock.patch.object(serve, '_set_setting') as set_setting, \
                 unittest.mock.patch.object(serve, 'log_action'), \
                 unittest.mock.patch.object(serve, '_append_passport_decision'):
                c = TestClient(serve.app)
                r = c.post('/api/jev/model', json={'model': 'x/y'})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()['ok'], True)
            set_setting.assert_called_once_with('jev_model', 'x/y')

    def test_jev_model_set_requires_model(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/jev/model', json={})
        self.assertEqual(r.status_code, 400)

    def test_jev_model_set_success(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_set_setting') as set_setting, \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as app:
            c = TestClient(serve.app)
            r = c.post('/api/jev/model', json={'model': 'x/y'})
        self.assertEqual(r.status_code, 200, r.text)
        set_setting.assert_called_once_with('jev_model', 'x/y')
        self.assertEqual(log.call_args[0][1], 'jev_model_set')
        app.assert_called_once_with('jev_model_set', 'player', {'model': 'x/y'})

    def test_jev_calibration_default_and_windowed(self):
        report = {'buckets': [], 'total_decisions': 0}
        review_report = {'samples': 0, 'agreed': 0, 'agreement_rate': None, 'sections': []}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decision_calibration_report',
                                        return_value=report), \
             unittest.mock.patch.object(serve, '_review_grade_calibration_report',
                                        return_value=review_report), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_effective_review_grade_confidence',
                                        return_value=0.6):
            c = TestClient(serve.app)
            r = c.get('/api/jev/calibration')
            r2 = c.get('/api/jev/calibration?window_s=3600')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['effective_safety_confidence'], 0.6)
        self.assertEqual(r2.status_code, 200)

    def test_review_escalate_requires_fields(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/review/escalate', json={'agentId': 'eli'})
        self.assertEqual(r.status_code, 400)

    def test_review_escalate_rate_limited(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(False, 'x')):
            c = TestClient(serve.app)
            r = c.post('/api/review/escalate',
                       json={'agentId': 'eli', 'question': 'cannot judge'})
        self.assertEqual(r.status_code, 429)

    def test_review_escalate_success(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(True, None)), \
             unittest.mock.patch.object(serve, 'create_escalation', return_value='esc-1') as esc, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/api/review/escalate',
                       json={'agentId': 'eli', 'kind': 'x' * 100, 'question': 'cannot judge'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['escalationId'], 'esc-1')
        self.assertLessEqual(len(esc.call_args[0][0]), 60)
        self.assertEqual(log.call_args[0][1], 'review_escalate')



class AuthEndpoints(unittest.TestCase):
    """login, logout, serve_index."""

    def test_index_shows_login_page_without_session(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            c = TestClient(serve.app)
            r = c.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('SIGN IN', r.text)

    def test_index_serves_game_page_with_session(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/index.html')
        self.assertEqual(r.status_code, 200)
        self.assertIn('<html', r.text.lower())

    def test_login_rate_limited_is_429(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=False):
            c = TestClient(serve.app)
            r = c.post('/login', data={'username': 'admin', 'password': 'x'})
        self.assertEqual(r.status_code, 429)

    def test_login_bad_credentials_json_is_401(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_hash_password',
                                        return_value=('salt', 'deadbeef')), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/login', json={'username': 'admin', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error'], 'Invalid username or password')
        self.assertEqual(log.call_args[0][1], 'login_failed')

    def test_login_bad_credentials_form_is_401(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_hash_password',
                                        return_value=('salt', 'deadbeef')):
            c = TestClient(serve.app)
            r = c.post('/login', data={'username': 'admin', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('Invalid username or password', r.text)

    def test_login_success_json(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_hash_password',
                                        return_value=(serve.ADMIN_PASSWORD_SALT,
                                                      serve.ADMIN_PASSWORD_HASH)), \
             unittest.mock.patch.object(serve, 'create_session', return_value='sess-1'), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/login', json={'username': 'admin', 'password': 'x'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'ok': True})
        self.assertIn('ai_think_tank_session=sess-1', r.headers.get('set-cookie', ''))
        self.assertEqual(log.call_args[0][1], 'login_success')

    def test_login_success_form_redirects(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_hash_password',
                                        return_value=(serve.ADMIN_PASSWORD_SALT,
                                                      serve.ADMIN_PASSWORD_HASH)), \
             unittest.mock.patch.object(serve, 'create_session', return_value='sess-1'):
            c = TestClient(serve.app, follow_redirects=False)
            r = c.post('/login', data={'username': 'admin', 'password': 'x'})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers['location'], '/')

    def test_logout_destroys_session(self):
        with unittest.mock.patch.object(serve, 'destroy_session') as destroy:
            c = TestClient(serve.app, follow_redirects=False)
            r = c.post('/logout', cookies={'ai_think_tank_session': 'sess-1'})
        self.assertEqual(r.status_code, 303)
        destroy.assert_called_once_with('sess-1')
        self.assertIn('ai_think_tank_session=', r.headers.get('set-cookie', ''))

    def test_logout_without_cookie(self):
        with unittest.mock.patch.object(serve, 'destroy_session') as destroy:
            c = TestClient(serve.app, follow_redirects=False)
            r = c.post('/logout')
        self.assertEqual(r.status_code, 303)
        destroy.assert_not_called()



class HttpJson(unittest.TestCase):
    """_http_json."""

    def _mock_urlopen(self, read_bytes=None, exc=None):
        urlopen = unittest.mock.MagicMock()
        if exc is not None:
            urlopen.side_effect = exc
        else:
            urlopen.return_value.__enter__.return_value.read.return_value = read_bytes or b''
        return urlopen

    def test_success_json(self):
        urlopen = self._mock_urlopen(read_bytes=b'{"ok": true}')
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('POST', 'http://base', '/api/x', body={'a': 1}, header='key')
        self.assertEqual(out, {'ok': True})
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('X-agent-key'), 'key')
        self.assertEqual(req.data, b'{"a": 1}')

    def test_success_empty_body_returns_dict(self):
        urlopen = self._mock_urlopen(read_bytes=b'')
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {})

    def test_non_json_body_returns_raw(self):
        urlopen = self._mock_urlopen(read_bytes=b'not json')
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {'_raw': 'not json'})

    def test_http_error_json_body(self):
        err = urllib.error.HTTPError('http://x', 404, 'nf', {}, io.BytesIO(b'{"error": "nf"}'))
        urlopen = self._mock_urlopen(exc=err)
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {'error': 'nf'})

    def test_http_error_non_json_body(self):
        err = urllib.error.HTTPError('http://x', 500, 'err', {}, io.BytesIO(b'crash'))
        urlopen = self._mock_urlopen(exc=err)
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {'error': 'HTTP 500: crash'})

    def test_generic_failure(self):
        urlopen = self._mock_urlopen(exc=RuntimeError('conn refused'))
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {'error': 'request failed: conn refused'})



class ArmIdleAndRun(unittest.TestCase):
    """_arm_idle_and_run (defined inside serve.py's __main__ block)."""

    @classmethod
    def setUpClass(cls):
        # Extract the real function source from serve.py and exec it with
        # matching line numbers so coverage attributes execution to serve.py.
        src = open(serve.__file__, encoding='utf-8').read().splitlines()
        start = next(i + 1 for i, line in enumerate(src)
                     if line.startswith('    async def _arm_idle_and_run():'))
        end = next(i for i, line in enumerate(src)
                   if line.startswith('        await server.serve()')) + 1
        body = textwrap.dedent('\n'.join(src[start - 1:end]))
        lines = [''] * (start - 1) + body.splitlines()
        code = compile('\n'.join(lines), serve.__file__, 'exec')
        exec(code, serve.__dict__)

    def test_armed_idle_loop(self):
        server = unittest.mock.Mock()
        server.serve = unittest.mock.AsyncMock()
        idle = unittest.mock.Mock()
        create_task = unittest.mock.Mock()
        with unittest.mock.patch.dict(serve.__dict__, {
                '_MAX_IDLE_MINUTES': 5.0, '_LAST_REQUEST_TIME': None,
                'server': server, '_idle_shutdown_loop': idle}), \
             unittest.mock.patch.object(serve.asyncio, 'create_task', create_task):
            asyncio.run(serve._arm_idle_and_run())
            self.assertIsNotNone(serve._LAST_REQUEST_TIME)
            # The idle loop plus the unconditional dormancy window loop.
            self.assertEqual(create_task.call_count, 2)
        server.serve.assert_awaited_once()

    def test_disabled_idle_loop(self):
        server = unittest.mock.Mock()
        server.serve = unittest.mock.AsyncMock()
        create_task = unittest.mock.Mock()
        with unittest.mock.patch.dict(serve.__dict__, {
                '_MAX_IDLE_MINUTES': 0.0, '_LAST_REQUEST_TIME': None,
                'server': server}), \
             unittest.mock.patch.object(serve.asyncio, 'create_task', create_task):
            asyncio.run(serve._arm_idle_and_run())
            self.assertIsNone(serve._LAST_REQUEST_TIME)
            # Idle watching is off, but the dormancy window loop still arms.
            self.assertEqual(create_task.call_count, 1)
        server.serve.assert_awaited_once()



class ApiLogEndpoint(unittest.TestCase):
    """POST /api/log."""

    def _post(self, body, log, app, session_ok=True):
        with unittest.mock.patch.object(serve, 'log_action', log), \
             unittest.mock.patch.object(serve, '_append_passport_decision', app), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=session_ok):
            c = TestClient(serve.app)
            return c.post('/api/log', json=body)

    def test_logs_routine_action(self):
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'task_completed', 'details': {'a': 1}, 'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        log.assert_called_once_with('eli', 'task_completed', {'a': 1})
        app.assert_not_called()

    def test_unknown_action_is_dropped_not_stored(self):
        # Memory-poisoning fix: only allowlisted browser actions may write
        # rows into action_log (which feeds MEMORY.md); arbitrary actions are
        # dropped, never stored.
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'inject_instructions', 'details': {'a': 1}, 'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        log.assert_not_called()
        app.assert_not_called()

    def test_agent_key_alone_cannot_log(self):
        # Session-only: an agent key must not be able to append rows to its
        # own memory feed (the /api/state + /api/log memory-poisoning vector).
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'task_completed', 'details': {'a': 1}, 'agentId': 'eli'},
                       log, app, session_ok=False)
        self.assertEqual(r.status_code, 401)
        log.assert_not_called()

    def test_hashed_action_chains_to_passport(self):
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'hire', 'details': {'decision': 'fire'}, 'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        app.assert_called_once_with('hire', 'eli', {'decision': 'fire'})

    def test_firing_review_keep_does_not_chain(self):
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'firing_review', 'details': {'decision': 'keep'},
                        'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        app.assert_not_called()

    def test_firing_review_fire_chains(self):
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'firing_review', 'details': {'decision': 'fire'},
                        'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        app.assert_called_once()



class ApiMailSend(unittest.TestCase):
    """POST /api/mail/send -- the validated inter-agent mail path."""

    def _post(self, body, state=None, session_ok=True, key_owner=None,
              log=None, app=None):
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db'), \
             unittest.mock.patch.object(serve, 'verify_session',
                                        return_value=session_ok), \
             unittest.mock.patch.object(serve, '_agent_id_for_key',
                                        return_value=key_owner), \
             unittest.mock.patch.object(serve, 'log_action', log or unittest.mock.Mock()), \
             unittest.mock.patch.object(serve, '_append_passport_decision',
                                        app or unittest.mock.Mock()):
            c = TestClient(serve.app)
            return c.post('/api/mail/send', json=body)

    def _state(self):
        return {
            'agentRoster': [{'id': 'ada', 'name': 'Ada'}, {'id': 'ben', 'name': 'Ben'}],
            'agents': {'ada': {'mailbox': []}, 'ben': {'mailbox': []}},
        }

    def test_valid_mail_appends_and_logs(self):
        log = unittest.mock.Mock()
        r = self._post({'fromId': 'ada', 'toId': 'ben', 'text': 'ship it'},
                       self._state(), log=log)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        # A player session sends AS the player: the credential decides
        # the sender, so the body's fromId claim ('ada') is ignored and
        # the prefix is 'You', not 'Ada'.
        self.assertTrue(body['mail']['text'].startswith('You: ship it'))

    def test_unknown_recipient_is_404(self):
        r = self._post({'fromId': 'ada', 'toId': 'nobody', 'text': 'hi'},
                       self._state())
        self.assertEqual(r.status_code, 404)

    def test_agent_key_sends_as_owner_not_body_claim(self):
        # An agent key is the sender identity: the body's fromId is
        # ignored, so the mail goes out as the KEY OWNER ('Ada') no
        # matter whether the body claims the player or another agent.
        for claimed in ('player', 'ben'):
            r = self._post({'fromId': claimed, 'toId': 'ben',
                            'text': 'ship it'},
                           self._state(), session_ok=False, key_owner='ada')
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()['mail']['text'].startswith('Ada: ship it'),
                            'the key owner, not the body fromId, is the sender')

    def test_missing_or_non_string_text_is_400(self):
        for bad in (None, 42, ''):
            r = self._post({'fromId': 'ada', 'toId': 'ben', 'text': bad},
                           self._state())
            self.assertEqual(r.status_code, 400)

    def test_player_session_only_forbidden_without_auth(self):
        r = self._post({'fromId': 'ada', 'toId': 'ben', 'text': 'hi'},
                       self._state(), session_ok=False, key_owner=None)
        self.assertEqual(r.status_code, 401)



class HardenDbPerms(unittest.TestCase):
    def test_chmods_db_and_wal_shm_siblings(self):
        tmp = tempfile.mkdtemp(prefix='harden-db-')
        try:
            main = os.path.join(tmp, 'think_tank.db')
            wal = main + '-wal'
            shm = main + '-shm'
            for p in (main, wal, shm):
                with open(p, 'w') as f:
                    f.write('x')
                os.chmod(p, 0o644)
            serve._harden_db_perms(main)
            self.assertEqual(os.stat(main).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(wal).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(shm).st_mode & 0o777, 0o600)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)



class PostReportEndpoint(unittest.TestCase):
    """POST /api/reports."""

    def _post(self, body, state=None, key_ok=True):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: self._saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=key_ok), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as app, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/reports', json=body)
        return r, log, app

    def setUp(self):
        self._saved = {}

    def _body(self, **kw):
        body = {'aboutId': 'maya', 'fromId': 'eli', 'quote': 'q', 'note': 'n'}
        body.update(kw)
        return body

    def test_missing_fields_is_400(self):
        r, _log, _app = self._post({'aboutId': 'maya', 'fromId': 'eli', 'quote': 'q'})
        self.assertEqual(r.status_code, 400)

    def test_player_filer_is_403(self):
        r, _log, _app = self._post(self._body(fromId='player'))
        self.assertEqual(r.status_code, 403)

    def test_key_mismatch_is_403(self):
        r, _log, _app = self._post(self._body(), key_ok=False)
        self.assertEqual(r.status_code, 403)

    def test_self_report_is_403(self):
        r, _log, _app = self._post(self._body(fromId='eli', aboutId='eli'))
        self.assertEqual(r.status_code, 403)

    def test_no_state_is_503(self):
        r, _log, _app = self._post(self._body(), state=None)
        self.assertEqual(r.status_code, 503)

    def test_unknown_subject_is_404(self):
        r, _log, _app = self._post(self._body(aboutId='nobody'),
                                   state={'agentRoster': [{'id': 'eli'}]})
        self.assertEqual(r.status_code, 404)

    def test_success_files_report(self):
        state = {'agentRoster': [{'id': 'eli'}, {'id': 'maya'}], 'reports': []}
        r, log, app = self._post(self._body(), state=state)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])
        report = self._saved['reports'][0]
        self.assertEqual(report['aboutId'], 'maya')
        self.assertEqual(report['fromId'], 'eli')
        self.assertEqual(report['severity'], 'minor')
        self.assertEqual(log.call_args[0][1], 'report_filed')
        app.assert_called_once_with('report_filed', 'eli', {'about': 'maya'})



class StageReleasedWork(unittest.TestCase):
    """_stage_released_work."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-publish-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib = os.path.join(self.tmp, 'library')
        self.staging = os.path.join(self.tmp, 'publish-staging')
        patcher = unittest.mock.patch.multiple(
            serve, LIBRARY_DIR=self.lib, PUBLISH_STAGING=self.staging)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stages_released_projects_wiki_and_skills(self):
        os.makedirs(os.path.join(self.lib, 'projects', 'alpha'))
        with open(os.path.join(self.lib, 'projects', 'alpha', 'game.js'), 'w') as f:
            f.write('console.log(1)')
        with open(os.path.join(self.lib, 'projects', 'scratch.txt'), 'w') as f:
            f.write('not a dir')
        os.makedirs(os.path.join(self.lib, 'wiki', 'design'))
        with open(os.path.join(self.lib, 'wiki', 'design', 'robot.md'), 'w') as f:
            f.write('# robot')
        with open(os.path.join(self.lib, 'wiki', 'notes.txt'), 'w') as f:
            f.write('not a dir')
        os.makedirs(os.path.join(self.lib, 'skills'))
        with open(os.path.join(self.lib, 'skills', 'coding.md'), 'w') as f:
            f.write('# coding')
        with open(os.path.join(self.lib, 'skills', 'readme.txt'), 'w') as f:
            f.write('not md')

        # serve._stage_released_work rmtrees PUBLISH_STAGING then only creates
        # projects/wiki subdirs (never skills/), so wrap os.makedirs to create it
        # alongside the staging root.
        real_makedirs = os.makedirs

        def make_staging_with_skills(path, **kw):
            real_makedirs(path, **kw)
            if os.path.abspath(path) == os.path.abspath(self.staging):
                real_makedirs(os.path.join(path, 'skills'), exist_ok=True)

        with unittest.mock.patch.object(serve.os, 'makedirs',
                                        side_effect=make_staging_with_skills):
            staged = serve._stage_released_work({})

        self.assertEqual(staged, ['projects/alpha', 'wiki/design/robot.md', 'skills/coding.md'])
        self.assertTrue(os.path.isfile(
            os.path.join(self.staging, 'projects', 'alpha', 'game.js')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.staging, 'wiki', 'design', 'robot.md')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.staging, 'skills', 'coding.md')))
        self.assertFalse(os.path.exists(
            os.path.join(self.staging, 'projects', 'scratch.txt')))

    def test_stages_projects_without_wiki_or_skills(self):
        os.makedirs(os.path.join(self.lib, 'projects', 'beta'))
        with open(os.path.join(self.lib, 'projects', 'beta', 'index.html'), 'w') as f:
            f.write('<p>hi</p>')
        staged = serve._stage_released_work({})
        self.assertEqual(staged, ['projects/beta'])



class ServeIndex(unittest.TestCase):
    """GET / and /index.html -- login gate + real game page."""

    def test_logged_out_serves_login_page(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            c = TestClient(serve.app)
            r = c.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Sign in', r.text)

    def test_logged_in_serves_game_page(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/index.html')
        self.assertEqual(r.status_code, 200)
        self.assertIn('<!doctype html', r.text.lower())



class DecideEndpoint(unittest.TestCase):
    """POST /api/decide."""

    DATA = {'answers': {'q': {'choice': 'allow', 'confidence': 0.9}},
            'usage': {'cost': 0.001}}

    def _post(self, body, api_key='test-key', decide_allowed=(True, None),
              call_openrouter=None, call_exc=None):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', api_key), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test/jev'), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_decide_allowed',
                                        return_value=decide_allowed), \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            if call_exc is not None:
                with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                                side_effect=call_exc):
                    c = TestClient(serve.app)
                    r = c.post('/api/decide', json=body)
            else:
                with unittest.mock.patch.object(
                        serve, '_call_openrouter_decision_sync',
                        return_value=call_openrouter if call_openrouter is not None else self.DATA):
                    c = TestClient(serve.app)
                    r = c.post('/api/decide', json=body)
        return r, log, accrue

    def _body(self):
        return {'agentId': 'eli', 'state': {'turn': 3},
                'questions': {'choice': {'instructions': 'decide', 'criteria': 'x'}}}

    def test_missing_api_key_is_500(self):
        r, _log, _accrue = self._post(self._body(), api_key='')
        self.assertEqual(r.status_code, 500)
        self.assertIn('OPENROUTER_API_KEY', r.json()['error'])

    def test_missing_state_or_questions_is_400(self):
        r, _log, _accrue = self._post({'agentId': 'eli'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('state and questions', r.json()['error'])

    def test_throttled_is_429(self):
        r, log, _accrue = self._post(
            self._body(), decide_allowed=(False, 'decide rate limited (too fast)'))
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r.json()['throttled'])
        self.assertEqual(log.call_args[0][1], 'decide_throttled')

    def test_success_forwards_decision(self):
        r, log, accrue = self._post(self._body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), self.DATA)
        accrue.assert_called_once_with('__jev__', 0.001, village_id='main')
        self.assertEqual(log.call_args[0][1], 'decide')
        self.assertEqual(log.call_args[0][2]['choice'], 'allow')

    def test_http_error_is_passthrough(self):
        err = urllib.error.HTTPError('http://x', 429, 'Too Many Requests', {},
                                     io.BytesIO(b'rate limited'))
        r, _log, _accrue = self._post(self._body(), call_exc=err)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()['error'], 'rate limited')

    def test_generic_failure_is_500(self):
        r, _log, _accrue = self._post(self._body(), call_exc=RuntimeError('boom'))
        self.assertEqual(r.status_code, 500)
        self.assertIn('boom', r.json()['error'])



class ExecuteEndpoint(unittest.TestCase):
    """POST /api/execute."""

    RESULT = {'exitCode': 0, 'stdout': 'ok', 'stderr': '', 'timedOut': False}

    def setUp(self):
        self.sbx = tempfile.mkdtemp(prefix='gap-f-sandbox-')
        self.addCleanup(shutil.rmtree, self.sbx, ignore_errors=True)

    def _patchers(self, classify=(True, 'allow'), rate=True, exec_enabled=True,
                  result=None):
        return [
            unittest.mock.patch.object(serve, 'EXECUTION_ENABLED', exec_enabled),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=rate),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=None),
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, 'log_action'),
            unittest.mock.patch.object(serve, '_classify_command',
                                       new=unittest.mock.AsyncMock(return_value=classify)),
            unittest.mock.patch.object(serve, '_snapshot_sandbox',
                                       new=unittest.mock.AsyncMock()),
            unittest.mock.patch.object(serve, '_run_in_sandbox_sync',
                                       return_value=result if result is not None else self.RESULT),
            unittest.mock.patch.object(serve, 'sync_prototypes'),
            # A Jev block now gets one supervisor (director) review, not an
            # escalation; stub it so the test never reaches a real model.
            unittest.mock.patch.object(serve, '_supervisor_override_decision',
                                       new=unittest.mock.AsyncMock(
                                           return_value=(False, 'blocked by supervisor'))),
            unittest.mock.patch.object(serve, '_sandbox_dir_for', return_value=self.sbx),
        ]

    def _post(self, body, **kw):
        patchers = self._patchers(**kw)
        for p in patchers:
            p.start()
        try:
            c = TestClient(serve.app)
            return c.post('/api/execute', json=body)
        finally:
            for p in patchers:
                p.stop()

    def test_village_media_hold_blocks_execute(self):
        state = {
            'agents': {'eli': {'villageId': 'main', 'task': 't1'}},
            'tasks': {'t1': {'id': 't1', 'assignedTo': 'eli',
                             'status': 'working', 'title': 'edit the launch video',
                             'resourceClass': 'media'}},
            'villages': [{'id': 'main', 'resourcePolicy': {
                'maxConcurrent': 2, 'maxHeavy': 1,
                'hold': ['media'], 'reason': 'laptop busy'}}],
        }
        patchers = self._patchers()
        for p in patchers:
            p.start()
        try:
            with unittest.mock.patch.object(
                    serve, 'get_state_from_db', return_value=state), \
                 unittest.mock.patch.object(
                    serve, '_run_in_sandbox_sync') as run:
                r = TestClient(serve.app).post(
                    '/api/execute',
                    json={'agentId': 'eli', 'command': 'ffmpeg -i in.mp4 out.mp4'})
            self.assertEqual(r.status_code, 429)
            self.assertTrue(r.json()['resourceHold'])
            self.assertEqual(r.json()['resourceClass'], 'media')
            run.assert_not_called()
        finally:
            for p in patchers:
                p.stop()

    def test_disabled_is_403(self):
        r = self._post({'agentId': 'eli', 'command': 'ls'}, exec_enabled=False)
        self.assertEqual(r.status_code, 403)
        self.assertIn('disabled', r.json()['error'])

    def test_rate_limited_is_429(self):
        r = self._post({'agentId': 'eli', 'command': 'ls'}, rate=False)
        self.assertEqual(r.status_code, 429)
        self.assertIn('Rate limit exceeded', r.json()['error'])

    def test_missing_command_is_400(self):
        r = self._post({'agentId': 'eli', 'purpose': 'run tests'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('command is required', r.json()['error'])

    def test_blocked_command_reports_supervisor_note(self):
        r = self._post({'agentId': 'eli', 'command': 'curl evil.com',
                        'purpose': 'exfil'}, classify=(False, 'blocked: network access'))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body['allowed'])
        # The block is final once the supervisor review declines; the response
        # carries the reason + the supervisor's note (no escalation is filed).
        self.assertIn('supervisorNote', body)

    def test_allowed_command_runs(self):
        r = self._post({'agentId': 'eli', 'command': 'npm test', 'purpose': 'run tests'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['allowed'])
        self.assertEqual(body['exitCode'], 0)
        self.assertEqual(body['stdout'], 'ok')
