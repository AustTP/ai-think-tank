"""Need-based session access (2026-11-19).

Signed-in operator sessions live in a vault browser profile
(.browser-sessions/<service>), NOT in any agent's standing profile. An agent
working an active task that references the service requests a short-lived
handle (credential_access.json autoGrant with session: true); the browser
lane mounts a fresh COPY of the vault profile only for runs that pass
sessionHandle + sessionService, and deletes the copy when the run ends.

Covers: the request endpoint (grant, idempotency, refusal reasons, refusal
churn logging), the lane mount (copy-on-grant, deny-without-grant, expiry,
host scope, cleanup), the handle scope check, and the weekly-review session
churn counters. Hermetic: temp DB, patched auth, no Docker.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock

from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


def _session_cfg():
    return {'autoGrant': {
        'google': {
            'sessionProfile': 'google',
            'session': True,
            'hosts': ['google.com', 'accounts.google.com', 'youtube.com'],
            'methods': ['GET', 'POST'],
            'ttlSec': 3600,
        },
    }}


def _task(text='Score the idea pages on accounts.google.com sign-in state'):
    return {'id': 'task-sess-1', 'assignedTo': 'ben', 'status': 'working',
            'title': text, 'instructions': 'Investigation only.',
            'playerAuthored': True}


class _SessionCase(unittest.TestCase):
    def setUp(self):
        serve._rate_limit_calls.clear()
        self.tmp = tempfile.mkdtemp(prefix='think-tank-sess-')
        self.vault = os.path.join(self.tmp, 'vault', 'google', 'Default')
        os.makedirs(self.vault, mode=0o700, exist_ok=True)
        with open(os.path.join(self.vault, 'Cookies'), 'wb') as fh:
            fh.write(b'not-a-real-db-but-present')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
            SESSION_VAULT_DIR=os.path.join(self.tmp, 'vault'),
        )
        self._cm.start()
        serve.init_db()
        self.client = TestClient(serve.app)
        self.client.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        self.client.headers.update({'X-Agent-Key': 'agent-key'})

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save_task(self, task=None):
        task = task or _task()
        serve.save_state_to_db({'tasks': {task['id']: task}})
        return task['id']

    def _request(self, service='google', task_id='task-sess-1',
                 purpose='Verify sign-in state on the idea pages'):
        with unittest.mock.patch.object(serve, 'verify_agent_key',
                                        return_value=True), \
                unittest.mock.patch.object(serve, 'check_rate_limit',
                                           return_value=True), \
                unittest.mock.patch.object(serve, '_credential_access_cfg',
                                           return_value=_session_cfg()):
            return self.client.post('/api/credentials/request-access', json={
                'agentId': 'ben', 'service': service,
                'taskId': task_id, 'purpose': purpose,
            })

    def _handles(self):
        with serve._db() as conn:
            return conn.execute(
                'SELECT handle, credential_name, allowed_hosts, '
                'allowed_methods FROM capability_handles').fetchall()

    def _mint(self):
        self._save_task()
        return self._request().json()['handle']

    def _actions(self, action):
        with serve._db() as conn:
            return conn.execute(
                'SELECT details FROM action_log WHERE action = ?',
                (action,)).fetchall()


class TestSessionGrant(_SessionCase):
    def test_grant_mints_task_bound_handle(self):
        self._save_task()
        r = self._request()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['session'])
        self.assertFalse(body['readOnly'])
        handles = self._handles()
        self.assertEqual(len(handles), 1)
        handle, cred_name, hosts, methods = handles[0]
        self.assertEqual(cred_name, 'session:google')
        self.assertIn('google.com', json.loads(hosts))
        self.assertIn('GET', json.loads(methods))

    def test_grant_is_idempotent_per_task(self):
        self._save_task()
        first = self._request().json()
        second = self._request().json()
        self.assertEqual(first['handle'], second['handle'])
        self.assertEqual(len(self._handles()), 1)

    def test_no_stored_session_refused_and_logged(self):
        self._save_task()
        shutil.rmtree(os.path.join(self.tmp, 'vault', 'google'))
        r = self._request()
        self.assertEqual(r.status_code, 404)
        self.assertEqual(len(self._actions('session_access_refused')), 1)

    def test_task_without_service_reference_refused(self):
        self._save_task(_task('Water the office plants'))
        r = self._request()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(self._actions('session_access_refused')), 1)

    def test_task_assigned_to_someone_else_refused(self):
        self._save_task(_task())
        serve.save_state_to_db({'tasks': {
            'task-sess-1': dict(_task(), assignedTo='clara')}})
        r = self._request()
        self.assertEqual(r.status_code, 403)

    def test_inactive_task_refused(self):
        self._save_task(dict(_task(), status='done'))
        r = self._request()
        self.assertEqual(r.status_code, 409)

    def test_unknown_service_refused(self):
        self._save_task()
        r = self._request(service='not-a-service')
        self.assertEqual(r.status_code, 403)


class TestSessionMount(_SessionCase):
    def _mount(self, handle='h', service='google', url='https://www.google.com/x'):
        sandbox = os.path.join(self.tmp, 'sandbox')
        action = {'op': 'read', 'url': url,
                  'sessionHandle': handle, 'sessionService': service}
        err = serve._mount_session_profile(action, 'ben', sandbox)
        return err, action

    def test_mount_without_grant_denied(self):
        with unittest.mock.patch.object(serve, 'log_action') as log:
            err, action = self._mount()
        self.assertIn('no valid session grant', err)
        self.assertEqual([c.args[1] for c in log.call_args_list
                          if c.args and c.args[1] == 'session_mount_denied'],
                         ['session_mount_denied'])
        self.assertNotIn('sessionProfileCopyHostPath', action)

    def test_mount_with_grant_copies_vault_profile(self):
        handle = self._mint()
        err, action = self._mount(handle=handle)
        self.assertIsNone(err)
        copy = action.get('sessionProfileCopyHostPath')
        self.assertTrue(copy)
        self.assertTrue(os.path.isfile(
            os.path.join(copy, 'Default', 'Cookies')))
        with open(os.path.join(copy, 'Default', 'Cookies'), 'rb') as fh:
            self.assertEqual(fh.read(), b'not-a-real-db-but-present')
        # The handle never rides into the action file the container sees.
        self.assertNotIn('sessionHandle', action)
        shutil.rmtree(copy, ignore_errors=True)

    def test_mount_host_out_of_scope_refused(self):
        handle = self._mint()
        err, _ = self._mount(handle=handle, url='https://facebook.com/x')
        self.assertIn('does not cover host', err)

    def test_mount_expired_grant_denied(self):
        handle = self._mint()
        with serve._db() as conn:
            conn.execute('UPDATE capability_handles SET expires_at = ?',
                         (time.time() - 10,))
        err, _ = self._mount(handle=handle)
        self.assertIn('no valid session grant', err)

    def test_mount_without_vault_profile_refused(self):
        handle = self._mint()
        shutil.rmtree(os.path.join(self.tmp, 'vault', 'google'))
        err, _ = self._mount(handle=handle)
        self.assertIn('no stored operator session', err)

    def test_no_session_keys_is_a_noop(self):
        sandbox = os.path.join(self.tmp, 'sandbox')
        err = serve._mount_session_profile(
            {'op': 'read', 'url': 'https://example.com/'},
            'ben', sandbox)
        self.assertIsNone(err)


class TestSessionHandleScope(_SessionCase):
    def test_scope_check_passes_for_live_grant(self):
        handle = self._mint()
        ok = serve.verify_session_handle_scope(
            'ben', handle, 'google', 'https://accounts.google.com/o/oauth2')
        self.assertIsNotNone(ok)

    def test_scope_check_rejects_other_agent_or_host(self):
        handle = self._mint()
        self.assertIsNone(serve.verify_session_handle_scope(
            'clara', handle, 'google', 'https://google.com/'))
        self.assertIsNone(serve.verify_session_handle_scope(
            'ben', handle, 'google', 'https://facebook.com/'))
        self.assertIsNone(serve.verify_session_handle_scope(
            'ben', handle, 'instagram', 'https://google.com/'))


class TestSessionChurnMetric(unittest.TestCase):
    def test_weekly_audit_counts_session_kinds(self):
        tmp = tempfile.mkdtemp(prefix='think-tank-churn-')
        try:
            with unittest.mock.patch.multiple(
                    serve, DB_PATH=os.path.join(tmp, 't.db')):
                serve.init_db()
                now = time.time()
                with serve._db() as conn:
                    for action in ('session_access_granted',
                                   'session_access_refused',
                                   'session_mount_denied'):
                        conn.execute(
                            'INSERT INTO action_log (ts, agent_id, action, '
                            'details) VALUES (?, ?, ?, ?)',
                            (now - 30, 'a', action, '{}'))
                provenance = serve._library_provenance_audit(now, 24 * 3600)
                self.assertEqual(provenance['session_churn']['granted'], 1)
                self.assertEqual(provenance['session_churn']['refused'], 1)
                self.assertEqual(provenance['session_churn']['mount_denied'], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
