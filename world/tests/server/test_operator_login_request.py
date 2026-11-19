"""Operator sign-in escalation (2026-11-19).

When an agent hits an auth wall only a human can pass (federated sign-in with
a device-prompt code, magic link, puzzle), it calls request_operator_login;
the server starts the operator login browser on the service's VAULT profile
and pages the player (Telegram + inbox) with the site, URL, and VNC
password. Hermetic: docker runs and Telegram sends are patched; no containers.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


def _session_cfg():
    return {'autoGrant': {
        'google': {
            'sessionProfile': 'google', 'session': True,
            'hosts': ['google.com', 'ideabrowser.com', 'supabase.co'],
            'methods': ['GET', 'POST'], 'ttlSec': 3600,
        },
    }}


class _EscalationCase(unittest.TestCase):
    def setUp(self):
        serve._rate_limit_calls.clear()
        self.tmp = tempfile.mkdtemp(prefix='think-tank-escalate-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        serve.save_state_to_db({'tasks': {}, 'playerInbox': []})
        self.client = TestClient(serve.app)
        self.client.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        self.client.headers.update({'X-Agent-Key': 'agent-key'})

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, agent_id='ben', service='google', **extra):
        body = {'agentId': agent_id, 'service': service, 'taskId': 'task-e-1',
                'reason': 'federated sign-in needs the human device prompt'}
        body.update(extra)
        with unittest.mock.patch.object(serve, '_actor_is_authorized',
                                        return_value=True), \
                unittest.mock.patch.object(serve, 'check_rate_limit',
                                           return_value=True), \
                unittest.mock.patch.object(serve, '_credential_access_cfg',
                                           return_value=_session_cfg()):
            return self.client.post('/api/browser-auth/request-login',
                                    json=body)

    def _inbox(self):
        with serve._db() as conn:
            pass
        return []


class TestOperatorLoginEscalation(_EscalationCase):
    def test_escalation_starts_browser_and_pages_player(self):
        with unittest.mock.patch.object(
                serve, '_start_operator_login_container',
                return_value=('http://100.88.51.79:6082/vnc.html?autoconnect=1',
                              'pw123456', 'btank-login-google')) as start, \
                unittest.mock.patch.object(serve, '_telegram_send_text_sync',
                                           return_value=1) as tg:
            r = self._post()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['vncUrl'],
                         'http://100.88.51.79:6082/vnc.html?autoconnect=1')
        self.assertEqual(body['expiresInSec'], 1800)
        start.assert_called_once()
        tg.assert_called_once()
        self.assertIn('needs your sign-in', tg.call_args[0][0])
        self.assertIn('6082', tg.call_args[0][0])
        state = serve.get_state_from_db() or {}
        inbox = [m for m in (state.get('playerInbox') or [])
                 if m.get('status') == 'awaiting_input']
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]['service'], 'google')
        self.assertIn('VNC password', inbox[0]['question'])

    def test_unknown_or_non_session_service_refused(self):
        r = self._post(service='not-a-service')
        self.assertEqual(r.status_code, 403)
        with unittest.mock.patch.object(serve, '_credential_access_cfg',
                                        return_value={'autoGrant': {
                                            'facebook': {
                                                'credential': 'facebook',
                                                'hosts': ['facebook.com'],
                                                'methods': ['POST'],
                                                'ttlSec': 900}}}), \
                unittest.mock.patch.object(serve,
                                           '_start_operator_login_container') as start:
            r = self._post(service='facebook')
        self.assertEqual(r.status_code, 403)  # credential-backed, not session-backed
        start.assert_not_called()

    def test_start_failure_surfaces_reason(self):
        with unittest.mock.patch.object(
                serve, '_start_operator_login_container',
                return_value=(None, None, 'could not start the login browser: boom')):
            r = self._post()
        self.assertEqual(r.status_code, 500)
        self.assertIn('boom', r.json()['error'])

    def test_spam_guard_two_recent_requests_then_refused(self):
        with unittest.mock.patch.object(
                serve, '_start_operator_login_container',
                return_value=('http://100.88.51.79:6083/vnc.html', 'pw',
                              'btank-login-google')):
            self._post()
            self._post()
            r = self._post()
        self.assertEqual(r.status_code, 429)
