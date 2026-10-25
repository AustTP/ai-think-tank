"""Coverage-gap tests for the self-edit schedule / allowlist / temp-access part.

Only new tests. Every endpoint is driven through TestClient with its stores,
auth gates and heavy helpers patched; the HTTP/SFTP/network seams are fakes.
No live DB, network, model call or browser is touched.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import core.config as config  # noqa: E402
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


def _client():
    return TestClient(serve.app)


def _fake_selfedit():
    fake = mock.MagicMock()
    fake.request_window.return_value = (True, {'id': 'w1', 'durationMin': 60,
                                               'ok': True})
    fake.cancel_window.return_value = (True, {'ok': True})
    fake.veto_window.return_value = (True, {'ok': True})
    fake.veto_window_by_link.return_value = (True, {'ok': True})
    fake.submit_remediation_fix.return_value = (True, {'id': 'f1', 'window': 'w1'})
    fake.run_remediation_checks.return_value = (True, {'tests': 'pass'})
    fake.validate_window.return_value = (True, {'status': 'approved'})
    fake.admin_approve_window.return_value = (True, {'status': 'approved'})
    fake.read_module.return_value = (True, {'path': 'world/x.py', 'text': 'x'})
    return fake


class SelfEditScheduleEndpointTests(unittest.TestCase):
    def _post(self, body, fake=None, authorized=True, allowed=True):
        fake = fake or _fake_selfedit()
        with mock.patch.object(serve, 'check_rate_limit', return_value=allowed), \
             mock.patch.object(serve, 'verify_agent_key', return_value=authorized), \
             mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action') as log:
            return _client().post('/api/selfedit/schedule', json=body,
                                  headers={'X-Agent-Key': 'k'}), fake, log

    def test_validation_requires_date_start_reason(self):
        resp, _, _ = self._post({'agentId': 'a'})
        self.assertEqual(400, resp.status_code)
        self.assertIn('date (YYYY-MM-DD)', resp.json()['error'])

    def test_rate_limited(self):
        resp, _, log = self._post({'agentId': 'a'}, allowed=False)
        self.assertEqual(429, resp.status_code)
        self.assertEqual('rate_limited', log.call_args[0][1])

    def test_success_and_refusal(self):
        body = {'agentId': 'a', 'date': '2026-10-30', 'start': '03:00',
                'reason': 'maintenance', 'durationMin': 30,
                'title': 't', 'description': 'd', 'changeType': 'normal',
                'riskLevel': 'low', 'serviceMode': 'idle_ok',
                'affectedServices': ['s'], 'fileManifest': ['f'],
                'rollbackPlan': 'r', 'rollbackOwner': 'o',
                'successCriteria': 'sc', 'abortCriteria': 'ac',
                'postApplyChecks': 'pc', 'safeStatePlan': 'sp',
                'restorePlan': 'rp', 'onCall': 'oc',
                'acknowledgements': ['a'], 'blackoutWindows': []}
        fake = _fake_selfedit()
        resp, fake, _ = self._post(body, fake=fake)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('w1', resp.json()['id'])
        self.assertEqual('a', fake.request_window.call_args[0][0])
        self.assertEqual('2026-10-30', fake.request_window.call_args[0][1])
        self.assertEqual('03:00', fake.request_window.call_args[0][2])
        self.assertTrue(fake.request_window.call_args[1]['notify_created'])

        fake.request_window.return_value = (False, {'error': 'slot taken'})
        resp, _, _ = self._post(body, fake=fake)
        self.assertEqual(400, resp.status_code)
        self.assertIn('slot taken', resp.json()['error'])

    def test_harness_crash(self):
        fake = _fake_selfedit()
        fake.request_window.side_effect = RuntimeError('quorum down')
        resp, _, log = self._post({'agentId': 'a', 'date': 'd',
                                   'start': 's', 'reason': 'r'}, fake=fake)
        self.assertEqual(500, resp.status_code)
        self.assertIn('the window request harness failed', resp.json()['error'])
        self.assertEqual('selfedit_crashed', log.call_args[0][1])


class SelfEditCancelVetoTests(unittest.TestCase):
    def _post(self, path, body, fake, session=False, agent=True, admin=False,
              allow=True):
        with mock.patch.object(serve, 'verify_session', return_value=session), \
             mock.patch.object(serve, 'verify_agent_key', return_value=agent), \
             mock.patch.object(serve, 'get_state_from_db', return_value={}), \
             mock.patch.object(serve, '_is_admin', return_value=admin), \
             mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action') as log:
            return _client().post(path, json=body), log

    def test_cancel_requires_window_id(self):
        resp, _ = self._post('/api/selfedit/window/cancel', {},
                             _fake_selfedit())
        self.assertEqual(400, resp.status_code)

    def test_cancel_unauthorized(self):
        resp, _ = self._post('/api/selfedit/window/cancel', {'windowId': 'w'},
                             _fake_selfedit(), session=False, agent=False)
        self.assertEqual(403, resp.status_code)

    def test_cancel_session_and_admin_paths(self):
        fake = _fake_selfedit()
        resp, _ = self._post('/api/selfedit/window/cancel', {'windowId': 'w1'},
                             fake, session=True)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('player', fake.cancel_window.call_args[0][1])

        resp, _ = self._post('/api/selfedit/window/cancel',
                             {'windowId': 'w2', 'agentId': 'chief'},
                             fake, session=False, agent=True, admin=True)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('chief', fake.cancel_window.call_args[0][1])

    def test_cancel_crash_and_refusal(self):
        fake = _fake_selfedit()
        fake.cancel_window.side_effect = RuntimeError('boom')
        resp, _ = self._post('/api/selfedit/window/cancel', {'windowId': 'w'},
                             fake, session=True)
        self.assertEqual(500, resp.status_code)
        self.assertIn('cancel failed', resp.json()['error'])

        fake.cancel_window.side_effect = None
        fake.cancel_window.return_value = (False, {'error': 'not pending'})
        resp, _ = self._post('/api/selfedit/window/cancel', {'windowId': 'w'},
                             fake, session=True)
        self.assertEqual(400, resp.status_code)

    def test_veto_validation_and_auth(self):
        fake = _fake_selfedit()
        resp, _ = self._post('/api/selfedit/window/veto', {}, fake)
        self.assertEqual(400, resp.status_code)
        resp, _ = self._post('/api/selfedit/window/veto', {'windowId': 'w'},
                             fake)
        self.assertEqual(400, resp.status_code)
        resp, _ = self._post('/api/selfedit/window/veto',
                             {'windowId': 'w', 'reason': 'r'}, fake,
                             session=False, agent=False)
        self.assertEqual(403, resp.status_code)

    def test_veto_session_and_admin_paths(self):
        fake = _fake_selfedit()
        resp, _ = self._post('/api/selfedit/window/veto',
                             {'windowId': 'w1', 'reason': 'no'}, fake,
                             session=True)
        self.assertEqual(200, resp.status_code)
        self.assertEqual(('w1', 'player', 'no'),
                         fake.veto_window.call_args[0])

        resp, _ = self._post('/api/selfedit/window/veto',
                             {'windowId': 'w2', 'reason': 'no',
                              'agentId': 'chief'}, fake, session=False,
                             agent=True, admin=True)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('chief', fake.veto_window.call_args[0][1])

    def test_veto_crash_and_refusal(self):
        fake = _fake_selfedit()
        fake.veto_window.side_effect = RuntimeError('boom')
        resp, _ = self._post('/api/selfedit/window/veto',
                             {'windowId': 'w', 'reason': 'r'}, fake,
                             session=True)
        self.assertEqual(500, resp.status_code)
        self.assertIn('veto harness failed', resp.json()['error'])

        fake.veto_window.side_effect = None
        fake.veto_window.return_value = (False, {'message': 'pushed'})
        resp, _ = self._post('/api/selfedit/window/veto',
                             {'windowId': 'w', 'reason': 'r'}, fake,
                             session=True)
        self.assertEqual(400, resp.status_code)

    def test_veto_link(self):
        fake = _fake_selfedit()
        with mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            resp = _client().get('/api/selfedit/window/veto-link')
            self.assertEqual(404, resp.status_code)
            resp = _client().get('/api/selfedit/window/veto-link',
                                 params={'windowId': 'w1', 'token': 't'})
            self.assertEqual(200, resp.status_code)
            self.assertIn('Vetoed change request', resp.text)

            fake.veto_window_by_link.return_value = (False, {'error': 'pushed'})
            resp = _client().get('/api/selfedit/window/veto-link',
                                 params={'windowId': 'w1', 'token': 't'})
            self.assertEqual(400, resp.status_code)
            self.assertIn('pushed', resp.text)

            fake.veto_window_by_link.side_effect = RuntimeError('boom')
            resp = _client().get('/api/selfedit/window/veto-link',
                                 params={'windowId': 'w1', 'token': 't'})
            self.assertEqual(500, resp.status_code)
            self.assertIn('Veto failed', resp.text)


class SelfEditFixValidateReadTests(unittest.TestCase):
    def _post(self, path, body, fake, key=True):
        with mock.patch.object(serve, 'verify_agent_key', return_value=key), \
             mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            return _client().post(path, json=body, headers={'X-Agent-Key': 'k'})

    def test_fix_edit(self):
        fake = _fake_selfedit()
        resp = self._post('/api/selfedit/fix/edit',
                          {'agentId': 'a', 'path': 'p', 'find': 'f',
                           'replace': 'r', 'summary': 's'}, fake)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('f1', resp.json()['id'])
        fake.submit_remediation_fix.return_value = (False, {'error': 'closed'})
        resp = self._post('/api/selfedit/fix/edit', {'agentId': 'a'}, fake)
        self.assertEqual(400, resp.status_code)
        fake.submit_remediation_fix.side_effect = RuntimeError('boom')
        resp = self._post('/api/selfedit/fix/edit', {'agentId': 'a'}, fake)
        self.assertEqual(500, resp.status_code)

    def test_fix_checks(self):
        fake = _fake_selfedit()
        resp = self._post('/api/selfedit/fix/checks', {'agentId': 'a'}, fake)
        self.assertEqual(200, resp.status_code)
        fake.run_remediation_checks.return_value = (False, {'error': 'red'})
        resp = self._post('/api/selfedit/fix/checks', {'agentId': 'a'}, fake)
        self.assertEqual(400, resp.status_code)
        fake.run_remediation_checks.side_effect = RuntimeError('boom')
        resp = self._post('/api/selfedit/fix/checks', {'agentId': 'a'}, fake)
        self.assertEqual(500, resp.status_code)

    def test_validate_admin_and_validator_paths(self):
        fake = _fake_selfedit()
        resp = self._post('/api/selfedit/validate',
                          {'agentId': 'a', 'windowId': 'w1',
                           'adminApproval': True, 'approved': True,
                           'note': 'ok'}, fake)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('w1', fake.admin_approve_window.call_args[0][1])

        resp = self._post('/api/selfedit/validate',
                          {'agentId': 'a', 'windowId': 'w2',
                           'approved': False, 'note': 5, 'issues': True}, fake)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('', fake.validate_window.call_args[0][3])
        self.assertTrue(fake.validate_window.call_args[0][4])

    def test_validate_crash_and_refusal(self):
        fake = _fake_selfedit()
        fake.validate_window.return_value = (False, {'error': 'hold'})
        resp = self._post('/api/selfedit/validate',
                          {'agentId': 'a', 'windowId': 'w'}, fake)
        self.assertEqual(400, resp.status_code)
        fake.validate_window.side_effect = RuntimeError('boom')
        resp = self._post('/api/selfedit/validate',
                          {'agentId': 'a', 'windowId': 'w'}, fake)
        self.assertEqual(500, resp.status_code)

    def test_read(self):
        fake = _fake_selfedit()
        resp = self._post('/api/selfedit/read',
                          {'agentId': 'a', 'path': 'world/x.py'}, fake)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('world/x.py', fake.read_module.call_args[0][0])
        fake.read_module.return_value = (False, {'error': 'denied'})
        resp = self._post('/api/selfedit/read', {'agentId': 'a'}, fake)
        self.assertEqual(400, resp.status_code)
        fake.read_module.side_effect = RuntimeError('boom')
        resp = self._post('/api/selfedit/read', {'agentId': 'a'}, fake)
        self.assertEqual(500, resp.status_code)


class PolicyEditTests(unittest.TestCase):
    def test_required_key_and_reason_length(self):
        ok, payload = serve._policy_edit_apply('a', '   ', 1, 'r')
        self.assertFalse(ok)
        self.assertIn('key is required', payload['error'])
        ok, payload = serve._policy_edit_apply(
            'a', 'memoryKeepCount', 7, 'x' * 401)
        self.assertFalse(ok)
        self.assertIn('400 characters', payload['error'])

    def test_write_failure_is_reported_and_tmp_removed(self):
        tmpdir = tempfile.mkdtemp(prefix='think-tank-policy-gap-')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path = os.path.join(tmpdir, 'operator_policy.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'memoryKeepCount': 20}, fh)
        fake_os = mock.MagicMock(wraps=os)
        fake_os.replace.side_effect = OSError('disk full')
        fake_os.unlink.side_effect = OSError('gone')
        with mock.patch.object(config, 'OPERATOR_POLICY_PATH', path), \
             mock.patch.object(config, 'OPERATOR_POLICY',
                               {'memoryKeepCount': 20}), \
             mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('approve', 0.9, 0.0)), \
             mock.patch.object(serve, 'os', fake_os), \
             mock.patch.object(serve, 'log_action'):
            ok, payload = serve._policy_edit_apply(
                'a', 'memoryKeepCount', 7, 'because')
        self.assertFalse(ok)
        self.assertIn('cannot write the policy file', payload['error'])
        self.assertTrue(fake_os.unlink.called)

    def test_policy_edit_endpoint(self):
        body = {'agentId': 'a', 'key': 'memoryKeepCount', 'value': 7,
                'reason': 'because'}
        with mock.patch.object(serve, 'verify_agent_key', return_value=False):
            resp = _client().post('/api/policy/agent_edit', json=body)
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             mock.patch.object(serve, '_policy_edit_apply',
                               return_value=(True, {'ok': True})), \
             mock.patch.object(serve, 'log_action'):
            resp = _client().post('/api/policy/agent_edit', json=body)
        self.assertEqual(200, resp.status_code)
        with mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             mock.patch.object(serve, '_policy_edit_apply',
                               return_value=(False, {'error': 'no'})), \
             mock.patch.object(serve, 'log_action'):
            resp = _client().post('/api/policy/agent_edit', json=body)
        self.assertEqual(400, resp.status_code)
        with mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             mock.patch.object(serve, '_policy_edit_apply',
                               side_effect=RuntimeError('boom')), \
             mock.patch.object(serve, 'log_action'):
            resp = _client().post('/api/policy/agent_edit', json=body)
        self.assertEqual(500, resp.status_code)


class AllowlistEndpointTests(unittest.TestCase):
    def test_normalize_target(self):
        self.assertEqual(('example.com', None),
                         serve._normalize_allowlist_target('https://Example.com/x'))
        self.assertEqual(('example.com', None),
                         serve._normalize_allowlist_target('example.com'))
        host, err = serve._normalize_allowlist_target('http://')
        self.assertIsNone(host)
        self.assertIn('hostname', err)

    def test_get(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            resp = _client().get('/api/allowlist')
        self.assertEqual(401, resp.status_code)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'a.com'}), \
             mock.patch.object(serve, '_runtime_allowlist_grants',
                               return_value={'b.com'}), \
             mock.patch.object(serve, '_effective_allowlist_domains',
                               return_value={'a.com', 'b.com'}):
            resp = _client().get('/api/allowlist')
        self.assertEqual(200, resp.status_code)
        self.assertEqual(['a.com'], resp.json()['envBaseline'])
        self.assertEqual(['b.com'], resp.json()['runtimeGrants'])

    def test_grant(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            resp = _client().post('/api/allowlist/grant', json={})
        self.assertEqual(401, resp.status_code)

        gate = mock.patch.object(serve, '_operator_gate',
                                 return_value=(True, 'player'))
        with gate:
            resp = _client().post('/api/allowlist/grant', content='not json',
                                  headers={'content-type': 'application/json'})
            self.assertEqual(400, resp.status_code)
            resp = _client().post('/api/allowlist/grant', json={})
            self.assertEqual(400, resp.status_code)
            resp = _client().post('/api/allowlist/grant',
                                  json={'host': 'http://'})
            self.assertEqual(400, resp.status_code)
        with gate, \
             mock.patch.object(serve, '_is_safe_public_host', return_value=False):
            resp = _client().post('/api/allowlist/grant',
                                  json={'host': 'internal.local'})
        self.assertEqual(400, resp.status_code)
        self.assertIn('private or internal', resp.json()['error'])
        with gate, \
             mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             mock.patch.object(serve, '_is_allowlisted_host', return_value=True):
            resp = _client().post('/api/allowlist/grant',
                                  json={'host': 'example.com'})
        self.assertEqual(200, resp.status_code)
        self.assertFalse(resp.json()['granted'])
        with gate, \
             mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             mock.patch.object(serve, '_grant_allowlist',
                               return_value='example.com') as grant:
            resp = _client().post('/api/allowlist/grant',
                                  json={'host': 'Example.com', 'purpose': 'p'})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json()['granted'])
        self.assertEqual('example.com', grant.call_args[0][0])

    def test_revoke(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            resp = _client().post('/api/allowlist/revoke', json={})
        self.assertEqual(401, resp.status_code)

        gate = mock.patch.object(serve, '_operator_gate',
                                 return_value=(True, 'player'))
        with gate:
            resp = _client().post('/api/allowlist/revoke', content='not json',
                                  headers={'content-type': 'application/json'})
            self.assertEqual(400, resp.status_code)
            resp = _client().post('/api/allowlist/revoke', json={})
            self.assertEqual(400, resp.status_code)
            resp = _client().post('/api/allowlist/revoke',
                                  json={'host': 'http://'})
            self.assertEqual(400, resp.status_code)
        with gate, \
             mock.patch.object(serve, '_revoke_allowlist', return_value=None):
            resp = _client().post('/api/allowlist/revoke',
                                  json={'host': 'example.com'})
        self.assertEqual(200, resp.status_code)
        self.assertFalse(resp.json()['revoked'])
        with gate, \
             mock.patch.object(serve, '_revoke_allowlist',
                               return_value='not_granted'):
            resp = _client().post('/api/allowlist/revoke',
                                  json={'host': 'example.com'})
        self.assertEqual(400, resp.status_code)
        self.assertIn('env baseline', resp.json()['error'])
        with gate, \
             mock.patch.object(serve, '_revoke_allowlist',
                               return_value='example.com'):
            resp = _client().post('/api/allowlist/revoke',
                                  json={'host': 'example.com', 'purpose': 'p'})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json()['revoked'])


class TempAccessAndWorkflowTests(unittest.TestCase):
    def test_revoke_task_access(self):
        serve.revoke_task_access('')
        serve.revoke_task_access('task-1')
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('no db')):
            serve.revoke_task_access('task-2')

    def test_has_active_and_grant_temp_access(self):
        serve._grant_temp_access('agent-x', 'curl', 'player', 'because')
        self.assertTrue(serve._has_active_temp_access('agent-x', 'curl'))
        self.assertFalse(serve._has_active_temp_access('agent-x', 'nope'))
        serve._grant_temp_access('agent-y', 'curl', 'player', 'story',
                                 task_id='task-9')
        self.assertTrue(serve._has_active_temp_access('agent-y', 'curl'))
        serve.revoke_task_access('task-9')
        self.assertFalse(serve._has_active_temp_access('agent-y', 'curl'))

    def test_api_request_sync_cache_hit(self):
        cached = {'expires': time.time() + 100, 'final_url': 'https://x/f',
                  'content_type': 'text/plain', 'body': 'cached',
                  'truncated': False}
        with mock.patch.object(serve, '_read_http_cache', return_value=cached), \
             mock.patch.object(serve, '_safe_urlopen') as opener:
            result = serve._api_request_sync('GET', 'https://x/', {}, None)
        self.assertEqual('cached', result['body'])
        self.assertFalse(opener.called)

    def _run_with_cached_headers(self, cached):
        class _Resp:
            status = 200
            headers = {'content-type': 'text/plain', 'ETag': 'new',
                       'Last-Modified': 'Wed, 21 Oct 2015 07:28:00 GMT'}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def geturl(self):
                return 'https://x/final'

            def read(self, n=-1):
                return b'body'

        seen = {}

        def _open(req, timeout=0):
            seen['req'] = req
            return _Resp()

        with mock.patch.object(serve, '_read_http_cache', return_value=cached), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=_open), \
             mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             mock.patch.object(serve, '_cache_ttl_from_headers', return_value=None), \
             mock.patch.object(serve, '_write_http_cache'):
            result = serve._api_request_sync('GET', 'https://x/', {}, None)
        return result, seen['req']

    def test_api_request_sync_etag_and_last_modified(self):
        result, req = self._run_with_cached_headers({'etag': 'old',
                                                     'expires': 0})
        self.assertEqual('body', result['body'])
        self.assertEqual('old', req.get_header('If-none-match'))
        result, req = self._run_with_cached_headers(
            {'last_modified_header': 'Wed, 21 Oct 2015 07:28:00 GMT',
             'expires': 0})
        self.assertEqual('Wed, 21 Oct 2015 07:28:00 GMT',
                         req.get_header('If-modified-since'))

    def test_api_request_sync_304_returns_cache(self):
        cached = {'final_url': 'https://x/f', 'content_type': 'text/plain',
                  'body': 'cached', 'truncated': True}
        err = urllib.error.HTTPError('https://x/', 304, 'not modified', {}, None)
        with mock.patch.object(serve, '_read_http_cache', return_value=cached), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            result = serve._api_request_sync('GET', 'https://x/', {}, None)
        self.assertEqual(200, result['status'])
        self.assertEqual('cached', result['body'])
        self.assertTrue(result['truncated'])

    def test_workflow_normalizes_v2_balance_path(self):
        balances = iter([10.0, 8.0])
        calls = []

        def _fake_request(method, url, headers, body):
            calls.append(url)
            if url.endswith('/balance'):
                return {'body': json.dumps({'balance': next(balances)})}
            if url.endswith('/submit'):
                return {'body': json.dumps({'id': 'job-1'})}
            return {'body': json.dumps({'status': 'completed'})}

        service = {'workflow': {
            'spend': {'kind': 'balance_delta', 'service': 'svc',
                      'balance_path': '/v2/balance'},
            'submit': {'path_template': '/submit', 'method': 'POST',
                       'id_field': 'id'},
            'poll': {'path_template': '/poll', 'method': 'GET',
                     'terminal': ['completed'], 'success': ['completed']},
        }}
        with mock.patch.object(serve, '_api_request_sync',
                               side_effect=_fake_request), \
             mock.patch.object(serve, '_accrue_spend') as accrue:
            result = serve._api_execute_workflow(
                service, {'kind': 'balance_delta'}, 'https://api.example/v2',
                'POST', {}, {'x': 1}, {})
        self.assertEqual('completed', result['status'])
        self.assertEqual('job-1', result['ids']['id'])
        self.assertEqual(2.0, accrue.call_args[0][1])
        self.assertIn('https://api.example/v2/balance', calls)


class PolicyEditBranchTests(unittest.TestCase):
    def _policy_file(self):
        tmpdir = tempfile.mkdtemp(prefix='think-tank-policy-branch-')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path = os.path.join(tmpdir, 'operator_policy.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'memoryKeepCount': 20}, fh)
        return path

    def test_unknown_and_operator_only_keys(self):
        ok, payload = serve._policy_edit_apply('a', 'not_a_real_key', 1, 'r')
        self.assertFalse(ok)
        self.assertIn('not an operator-policy key', payload['error'])
        ok, payload = serve._policy_edit_apply('a', 'maxTeamMembers', 3, 'r')
        self.assertFalse(ok)
        self.assertIn('operator-only', payload['error'])

    def test_out_of_range_value(self):
        ok, payload = serve._policy_edit_apply(
            'a', 'memoryKeepCount', 0, 'because')
        self.assertFalse(ok)
        self.assertIn('must be a int between 1 and 1000', payload['error'])

    def test_review_transport_failure(self):
        with mock.patch.object(serve, '_jev_quorum_choice_sync',
                               side_effect=RuntimeError('judge down')), \
             mock.patch.object(serve, 'log_action'):
            ok, payload = serve._policy_edit_apply(
                'a', 'memoryKeepCount', 7, 'because')
        self.assertFalse(ok)
        self.assertIn('review transport failed', payload['error'])

    def test_review_reject_and_escalate(self):
        for decision in ('reject', 'escalate'):
            with mock.patch.object(serve, '_jev_quorum_choice_sync',
                                   return_value=(decision, 0.7, 0.0)), \
                 mock.patch.object(serve, 'log_action'):
                ok, payload = serve._policy_edit_apply(
                    'a', 'memoryKeepCount', 7, 'because')
            self.assertFalse(ok)
            self.assertIn(decision, payload['error'])

    def test_policy_file_read_error(self):
        with mock.patch.object(config, 'OPERATOR_POLICY_PATH',
                               '/nonexistent/operator_policy.json'), \
             mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('approve', 0.9, 0.0)), \
             mock.patch.object(serve, 'log_action'):
            ok, payload = serve._policy_edit_apply(
                'a', 'memoryKeepCount', 7, 'because')
        self.assertFalse(ok)
        self.assertIn('cannot read the policy file', payload['error'])

    def test_policy_apply_success(self):
        path = self._policy_file()
        state = {'memoryKeepCount': 20}

        def _reload():
            with open(path, encoding='utf-8') as fh:
                state.update(json.load(fh))

        with mock.patch.object(config, 'OPERATOR_POLICY_PATH', path), \
             mock.patch.object(config, 'OPERATOR_POLICY', state), \
             mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('approve', 0.9, 0.0)), \
             mock.patch.object(config, 'reload_policy_config',
                               side_effect=_reload), \
             mock.patch.object(serve, 'log_action'):
            ok, payload = serve._policy_edit_apply(
                'a', 'memoryKeepCount', 7, 'because')
        self.assertTrue(ok)
        self.assertEqual(7, payload['value'])
        self.assertEqual(20, payload['previous'])
        self.assertEqual(7, state['memoryKeepCount'])
        with open(path, encoding='utf-8') as fh:
            self.assertEqual(7, json.load(fh)['memoryKeepCount'])

    def test_api_request_sync_reraises_non_304(self):
        err = urllib.error.HTTPError('https://x/', 500, 'boom', {}, None)
        with mock.patch.object(serve, '_read_http_cache',
                               return_value=None), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                serve._api_request_sync('GET', 'https://x/', {}, None)


if __name__ == '__main__':
    unittest.main()
