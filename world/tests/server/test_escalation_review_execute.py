"""Review-only sandbox execution for a pending escalation."""
import base64
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
