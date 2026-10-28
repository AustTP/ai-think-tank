"""Integrity primitives (015): the kv_state checksum pair, action-log parity
rows, and the escalation-store checksum. All against the temp DB via the
shared helper -- the live think_tank.db is never touched."""
import json
import os
import unittest

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import os
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402


class StateChecksumTests(unittest.TestCase):
    def test_seed_then_verify_then_detect_tamper(self):
        serve.save_state_to_db({'tasks': {}, 'agents': {}})
        self.assertEqual(serve._verify_state_checksum_at_boot(), {'ok': True})
        # A direct write under the writer (the exact torn-save / out-of-band
        # edit class the checksum exists to catch).
        tampered = json.dumps({'tasks': {}, 'agents': {}, 'tampered': 1})
        with serve._db() as conn:
            conn.execute('UPDATE kv_state SET blob = ? WHERE id = 1', (tampered,))
        result = serve._verify_state_checksum_at_boot()
        self.assertFalse(result['ok'])
        self.assertTrue(result['stashedTo'])
        self.assertTrue(os.path.exists(result['stashedTo']))
        os.unlink(result['stashedTo'])

    def test_missing_checksum_row_seeds_quietly(self):
        serve.save_state_to_db({'tasks': {}, 'agents': {}})
        with serve._db() as conn:
            conn.execute('DELETE FROM state_checksum')
        self.assertEqual(serve._verify_state_checksum_at_boot(), {'seeded': True})
        self.assertEqual(serve._verify_state_checksum_at_boot(), {'ok': True})


class ActionLogParityTests(unittest.TestCase):
    def test_parity_row_written_and_verified(self):
        for i in range(serve.ACTION_LOG_PARITY_WINDOW + 2):
            serve.log_action(None, 'parity_probe', {'i': i})
        with serve._db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE action = 'log_parity'").fetchone()[0]
        self.assertGreaterEqual(n, 1)
        ok, problems = serve._verify_action_log_parity()
        self.assertTrue(ok, problems)

    def test_tamper_detects(self):
        serve.log_action(None, 'parity_probe_marker', {'mark': 1})
        for i in range(serve.ACTION_LOG_PARITY_WINDOW + 2):
            serve.log_action(None, 'parity_probe2', {'i': i})
        with serve._db() as conn:
            row = conn.execute(
                "SELECT MIN(id) FROM action_log WHERE action = 'parity_probe2'").fetchone()[0]
            conn.execute('UPDATE action_log SET details = ? WHERE id = ?',
                         (json.dumps({'i': 'tampered'}), row))
        ok, problems = serve._verify_action_log_parity()
        self.assertFalse(ok)
        self.assertTrue(problems)


class EscalationChecksumTests(unittest.TestCase):
    def test_save_writes_checksum_and_load_detects_tamper(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix='tt-esc-chk-')
        path = os.path.join(tmp, 'escalations.json')
        try:
            with unittest.mock.patch.object(serve, 'ESCALATIONS_PATH', path), \
                 unittest.mock.patch.object(serve, 'notify_human_of_escalation'):
                serve.create_escalation('owner safety decision', 'parity question')
            self.assertTrue(os.path.exists(path + '.sha256'))
            data = json.load(open(path))
            first = next(iter(data))
            data[first]['status'] = 'approved'
            with open(path, 'w') as f:
                json.dump(data, f)
            with unittest.mock.patch.object(serve, 'ESCALATIONS_PATH', path), \
                 unittest.mock.patch.object(serve, 'log_action') as la, \
                 unittest.mock.patch.object(serve, '_push_new_health_alerts'):
                loaded = serve._load_escalations()
            self.assertIn(first, loaded)
            actions = [c[0][1] for c in la.call_args_list]
            self.assertIn('escalation_checksum_mismatch', actions)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_checksum_seeds_on_load(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix='tt-esc-chk2-')
        path = os.path.join(tmp, 'escalations.json')
        try:
            with unittest.mock.patch.object(serve, 'ESCALATIONS_PATH', path), \
                 unittest.mock.patch.object(serve, 'notify_human_of_escalation'):
                serve.create_escalation('owner safety decision', 'seed question')
            os.unlink(path + '.sha256')
            with unittest.mock.patch.object(serve, 'ESCALATIONS_PATH', path):
                loaded = serve._load_escalations()
            self.assertTrue(loaded)
            self.assertTrue(os.path.exists(path + '.sha256'))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
