"""Review-only sandbox execution for a pending escalation."""
import base64
import time
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve


def _wrapped(code):
    blob = base64.b64encode(code.encode()).decode()
    return f"python -c 'import base64;exec(base64.b64decode(\"{blob}\"))'"


class ReviewExecute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tt-review-exec-')
        self.path = os.path.join(self.tmp, 'escalations.json')
        self.patcher = unittest.mock.patch.object(serve, 'ESCALATIONS_PATH', self.path)
        self.patcher.start()
        self.code = "import pathlib\nprint('hello')\n"
        self.question = 'Agent clara needs a decision on this command: ' + _wrapped(self.code)
        with unittest.mock.patch.object(serve, 'notify_human_of_escalation'):
            self.esc_id = serve.create_escalation('owner safety decision', self.question)
        self.client = TestClient(serve.app)

    def tearDown(self):
        self.patcher.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, body):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_operator_gate', return_value=(True, 'theo')):
            return self.client.post('/api/escalation/review-execute', json=body)

    def test_decoded_target_is_stored_for_review(self):
        esc = serve._load_escalations()[self.esc_id]
        self.assertTrue(esc.get('decodedForReview'))
        self.assertIn('import pathlib', esc.get('reviewQuestion'))

    def test_runs_the_escalations_own_command(self):
        with unittest.mock.patch.object(serve, '_run_in_sandbox_sync',
                                        return_value={'stdout': 'hello\n', 'stderr': '',
                                                      'exitCode': 0, 'timedOut': False}) as run:
            r = self._post({'escalationId': self.esc_id, 'command': self.code})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])
        run.assert_called_once()

    def test_refuses_a_command_that_is_not_part_of_the_escalation(self):
        with unittest.mock.patch.object(serve, '_run_in_sandbox_sync') as run:
            r = self._post({'escalationId': self.esc_id, 'command': 'rm -rf /'})
        self.assertEqual(r.status_code, 403)
        run.assert_not_called()

    def test_refuses_a_resolved_escalation(self):
        escs = serve._load_escalations()
        escs[self.esc_id]['status'] = 'approved'
        serve._save_escalations(escs)
        with unittest.mock.patch.object(serve, '_run_in_sandbox_sync') as run:
            r = self._post({'escalationId': self.esc_id, 'command': self.code})
        self.assertEqual(r.status_code, 400)
        run.assert_not_called()


class EscalationExpiryPushTests(unittest.TestCase):
    """Consultant finding 2026-10-27: 5 escalations expired in 24h with zero
    owner notification. Expiry must push a warning to the player channels."""

    def test_expiry_pushes_warning_alert(self):
        pushed = []
        esc = {'kind': 'owner safety decision', 'agentId': 'clara',
               'ts': time.time() - (serve.ESCALATION_STALE_TTL_S + 60)}
        with unittest.mock.patch.object(serve, '_push_new_health_alerts',
                                        side_effect=lambda alerts: pushed.extend(alerts)):
            self.assertTrue(serve._expire_stale_escalation('esc-test', esc, time.time()))
        self.assertEqual(esc['status'], 'denied')
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0]['severity'], 'warning')
        self.assertIn('esc-test', pushed[0]['message'])
        self.assertIn('clara', pushed[0]['message'])

    def test_non_stale_kind_does_not_push(self):
        esc = {'kind': 'some other kind', 'agentId': 'clara',
               'ts': time.time() - (serve.ESCALATION_STALE_TTL_S + 60)}
        with unittest.mock.patch.object(serve, '_push_new_health_alerts') as push:
            self.assertFalse(serve._expire_stale_escalation('esc-y', esc, time.time()))
        push.assert_not_called()


if __name__ == '__main__':
    unittest.main()
