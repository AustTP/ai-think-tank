"""Coverage-gap tests for the captcha-solve part and credential access helpers.

Only new tests. No network, browser, model call, or live DB write: helpers are
called directly with fakes, and endpoint functions are awaited with mock
Request objects.
"""
import asyncio
import json
import os
import sys
import unittest
import urllib.parse
from unittest import mock

from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402


_SENTINEL = object()


def _req(body=None, headers=None, json_error=None):
    req = mock.Mock()
    req.headers = headers or {}
    if json_error is not None:
        req.json = mock.AsyncMock(side_effect=json_error)
    else:
        req.json = mock.AsyncMock(return_value=body if body is not None else {})
    return req


class TaskNeedTextTests(unittest.TestCase):
    def test_list_value_branch(self):
        self.assertIn('alpha', serve._task_need_text(
            {'title': ['Alpha', 2], 'goal': '', 'note': None}))


class CredentialAccessHelperTests(unittest.TestCase):
    def test_cfg_falls_back_when_loader_raises(self):
        with mock.patch.object(serve, 'get_credential_access_config',
                               side_effect=RuntimeError('boom')):
            self.assertIs(serve._credential_access_cfg(),
                          serve.CREDENTIAL_ACCESS_CONFIG)

    def test_host_matches_edges(self):
        self.assertFalse(serve._host_matches('', ['x.com']))
        self.assertTrue(serve._host_matches('a.x.com', ['', 'x.com']))
        self.assertFalse(serve._host_matches('x.com', ['']))

    def test_social_login_url_edges(self):
        with mock.patch.object(serve.urllib.parse, 'urlparse',
                               side_effect=RuntimeError('bad url')):
            self.assertFalse(serve._is_social_login_url('http://x'))
        with mock.patch.object(serve, '_is_readonly_social_host',
                               return_value=False):
            self.assertFalse(serve._is_social_login_url('http://x/login'))

    def test_active_task_handle_edges(self):
        self.assertIsNone(serve._active_task_handle('', 't', 'POST', 'http://x'))
        self.assertIsNone(serve._active_task_handle('a', 't', 'POST', '/no-host'))

        with mock.patch.object(serve, '_db', side_effect=RuntimeError('db down')):
            self.assertIsNone(serve._active_task_handle(
                'a', 't', 'POST', 'https://x.com/login'))

        conn = mock.MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            ('bad', 'not-json', '[]', 1),
            ('h2', json.dumps(['other.com']), json.dumps(['POST']), 1),
            ('h1', json.dumps(['x.com']), json.dumps(['POST']), 1),
        ]
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=conn)
        ctx.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(serve, '_db', return_value=ctx):
            self.assertEqual('h1', serve._active_task_handle(
                'a', 't', 'POST', 'https://x.com/login'))

        conn.execute.return_value.fetchall.return_value = [
            ('h2', json.dumps(['other.com']), json.dumps(['POST']), 1),
        ]
        with mock.patch.object(serve, '_db', return_value=ctx):
            self.assertIsNone(serve._active_task_handle(
                'a', 't', 'POST', 'https://x.com/login'))

    def test_social_readonly_guard_edges(self):
        with mock.patch.object(serve.urllib.parse, 'urlparse',
                               side_effect=[RuntimeError('bad'), mock.Mock(
                                   hostname='x.com')]), \
             mock.patch.object(serve, '_is_readonly_social_host',
                               return_value=True):
            self.assertIsNone(serve._social_readonly_guard('goto', '', '', []))

        with mock.patch.object(serve, '_is_readonly_social_host',
                               return_value=True), \
             mock.patch.object(serve, '_is_social_login_url', return_value=True):
            self.assertIn('username and password', serve._social_readonly_guard(
                'login', 'https://x.com/login', '', [{'credentialHandle': 'h'}]))
            self.assertIn('username or password', serve._social_readonly_guard(
                'login', 'https://x.com/login', '',
                [{'credentialHandle': 'h', 'credentialField': 'totp'},
                 {'credentialHandle': 'h', 'credentialField': 'password'}]))

            err = serve._social_readonly_guard('read', '', 'https://x.com/page', [])
            self.assertIn('pass the current', err)


class CaptchaSolveEndpointTests(unittest.TestCase):
    def _call(self, request_id='r1', agent='a1', rec=None, body=None,
              wait_result=_SENTINEL, json_error=None, **patches):
        defaults = {
            '_agent_id_for_key': mock.Mock(return_value=agent),
            'captcha': mock.Mock(),
            'check_rate_limit': mock.Mock(return_value=True),
            'EXECUTION_ENABLED': True,
            'BROWSING_ENABLED': True,
            '_is_safe_public_host': mock.Mock(return_value=True),
            '_sandbox_dir_for': mock.Mock(return_value='/tmp'),
            '_run_browser_act_sync': mock.Mock(return_value=({'ok': True}, None)),
            'log_action': mock.Mock(),
        }
        defaults.update(patches)
        started = []
        for name, value in defaults.items():
            p = mock.patch.object(serve, name, value)
            p.start()
            started.append(p)
            self.addCleanup(p.stop)
        serve.captcha.get.return_value = rec
        serve.captcha.wait_for_answer.return_value = (
            rec if wait_result is _SENTINEL else wait_result)
        return asyncio.run(serve.captcha_solve(
            request_id, _req(body, json_error=json_error)))

    def _rec(self, **kw):
        base = {'agentId': 'a1', 'status': 'pending',
                'pageUrl': 'https://example.com/login',
                'sandboxId': 'sandbox-a', 'inputSelector': '#code',
                'answer': '1234'}
        base.update(kw)
        return base

    def test_agent_and_lookup_guards(self):
        self.assertEqual(401, self._call(agent=None).status_code)
        self.assertEqual(404, self._call(rec=None).status_code)

    def test_record_state_guards(self):
        self.assertEqual(403, self._call(rec=self._rec(agentId='other')).status_code)
        self.assertEqual(200, self._call(rec=self._rec(status='solved')).status_code)
        self.assertEqual(410, self._call(rec=self._rec(status='expired')).status_code)

    def test_execution_and_rate_guards(self):
        self.assertEqual(403, self._call(rec=self._rec(), EXECUTION_ENABLED=False).status_code)
        self.assertEqual(429, self._call(rec=self._rec(), check_rate_limit=mock.Mock(return_value=False)).status_code)
        self.assertEqual(403, self._call(
            rec=self._rec(pageUrl='http://127.0.0.1/x'),
            _is_safe_public_host=mock.Mock(return_value=False)).status_code)

    def test_body_and_wait_edges(self):
        rec = self._rec(status='expired')
        self.assertEqual(410, self._call(rec=rec, body=None).status_code)
        resp = self._call(rec=self._rec(status='pending'), body=['not-dict'])
        self.assertEqual(202, resp.status_code)
        resp = self._call(rec=self._rec(status='pending'), body={'waitSec': 'bad'})
        self.assertEqual(202, resp.status_code)
        resp = self._call(rec=self._rec(status='pending'),
                          body=None, json_error=ValueError('no json'))
        self.assertEqual(202, resp.status_code)
        self.assertEqual(410, self._call(
            rec=self._rec(status='ready'),
            wait_result=self._rec(status='expired')).status_code)
        # wait_for_answer returns None after the wait
        rec = self._rec()
        self.assertEqual(404, self._call(rec=rec, wait_result=None).status_code)

    def test_post_wait_guards(self):
        self.assertEqual(410, self._call(rec=self._rec(status='expired')).status_code)
        self.assertEqual(400, self._call(rec=self._rec(status='ready', inputSelector='')).status_code)
        self.assertEqual(400, self._call(
            rec=self._rec(status='ready'),
            wait_result=self._rec(status='ready', pageUrl='')).status_code)

    def test_browser_result_edges(self):
        self.assertEqual(502, self._call(
            rec=self._rec(status='ready'), _run_browser_act_sync=mock.Mock(return_value=(None, 'boom'))).status_code)
        resp = self._call(rec=self._rec(status='ready'), _run_browser_act_sync=mock.Mock(
            return_value=({'captchas': [{'x': 1}]}, None)))
        self.assertEqual(409, resp.status_code)

    def test_success(self):
        resp = self._call(rec=self._rec(status='ready'))
        self.assertEqual(200, resp.status_code)


class RequestCredentialAccessTests(unittest.TestCase):
    def _call(self, body, **patches):
        defaults = {
            'verify_agent_key': mock.Mock(return_value=True),
            'check_rate_limit': mock.Mock(return_value=True),
            'get_state_from_db': mock.Mock(return_value={'tasks': {}}),
            '_credential_access_cfg': mock.Mock(return_value={}),
            'log_action': mock.Mock(),
            '_append_passport_decision': mock.Mock(),
            'mint_capability_handle': mock.Mock(return_value=('h', None)),
        }
        defaults.update(patches)
        started = []
        for name, value in defaults.items():
            p = mock.patch.object(serve, name, value)
            p.start()
            started.append(p)
            self.addCleanup(p.stop)
        return asyncio.run(serve.request_credential_access(_req(body)))

    def _body(self, **kw):
        body = {'agentId': 'a1', 'service': 'svc',
                'purpose': 'a real purpose here', 'taskId': 't1'}
        body.update(kw)
        return body

    def test_auth_rate_and_fields(self):
        self.assertEqual(401, self._call(self._body(), verify_agent_key=mock.Mock(return_value=False)).status_code)
        self.assertEqual(429, self._call(self._body(), check_rate_limit=mock.Mock(return_value=False)).status_code)
        self.assertEqual(400, self._call(self._body(service='')).status_code)
        self.assertEqual(400, self._call(self._body(purpose='short')).status_code)

    def test_task_and_credential_guards(self):
        spec = {'autoGrant': {'svc': {'hosts': ['x.com'], 'credential': 'cred'}}}
        self.assertEqual(404, self._call(
            self._body(), _credential_access_cfg=mock.Mock(return_value=spec),
            get_state_from_db=mock.Mock(return_value={'tasks': {'t1': None}})).status_code)

        task = {'assignedTo': 'a1', 'status': 'working', 'title': 'svc work'}
        state = {'tasks': {'t1': task}}
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=conn)
        ctx.__exit__ = mock.Mock(return_value=False)
        self.assertEqual(404, self._call(
            self._body(), _credential_access_cfg=mock.Mock(return_value=spec),
            get_state_from_db=mock.Mock(return_value=state),
            _db=mock.Mock(return_value=ctx)).status_code)
        login_conn = mock.MagicMock()
        login_conn.execute.return_value.fetchone.side_effect = [('login',), None]
        login_ctx = mock.MagicMock()
        login_ctx.__enter__ = mock.Mock(return_value=login_conn)
        login_ctx.__exit__ = mock.Mock(return_value=False)
        self.assertEqual(403, self._call(
            self._body(), _credential_access_cfg=mock.Mock(return_value=spec),
            get_state_from_db=mock.Mock(return_value=state),
            _db=mock.Mock(return_value=login_ctx),
            mint_capability_handle=mock.Mock(return_value=(None, 'no'))).status_code)


class ExecuteEndpointGuardTests(unittest.TestCase):
    def _call(self, **patches):
        defaults = {
            'EXECUTION_ENABLED': True,
            'check_rate_limit': mock.Mock(return_value=True),
            '_actor_is_authorized': mock.Mock(return_value=True),
            '_confirmation_gate_or_response': mock.Mock(return_value=(None, 'cid')),
            '_village_sandbox_id': mock.Mock(return_value='sandbox-a'),
            '_SANDBOX_ID_RE': mock.Mock(match=mock.Mock(return_value=True)),
            '_classify_command': mock.AsyncMock(return_value=(True, 'ok')),
            '_snapshot_sandbox': mock.Mock(),
            '_run_in_sandbox_sync': mock.Mock(return_value={'exitCode': 0, 'timedOut': False}),
            'log_action': mock.Mock(),
            '_finish_confirmation': mock.Mock(),
            'sync_prototypes': mock.Mock(),
        }
        defaults.update(patches)
        for name, value in defaults.items():
            p = mock.patch.object(serve, name, value)
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.execute(_req({'agentId': 'a1', 'command': 'ls'})))

    def test_unauthorized_and_confirmation_error(self):
        self.assertEqual(401, self._call(_actor_is_authorized=mock.Mock(return_value=False)).status_code)
        err = JSONResponse({'error': 'needs confirmation'}, status_code=409)
        self.assertIs(err, self._call(
            _confirmation_gate_or_response=mock.Mock(return_value=(err, None))))


if __name__ == '__main__':
    unittest.main()
