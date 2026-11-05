"""Tests for the ops failure surface (186_ops_failures).

Hermetic: temp DB seeded with synthetic action_log rows and health alerts;
no network. Pins: auth required, window clamped, block patterns grouped
with classifier-unavailable broken out, and the endpoint read-only (no
state writes).
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None
_KEY = 'ops-test-key'


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-ops-failures-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        verify_session=lambda sess: False,
        check_rate_limit=lambda aid: True,
    )
    _PATCHER.start()
    serve.init_db()
    with serve._db() as conn:
        conn.execute('INSERT INTO agent_keys (agent_id, secret_key, '
                     'created_at) VALUES (?, ?, ?)',
                     ('nora', _KEY, 0.0))
        conn.execute('INSERT INTO roster_agent (id, seq, blob, updated_at) '
                     'VALUES (?, ?, ?, ?)',
                     ('nora', 0, '{"id": "nora", "role": "regional_vp"}',
                      0.0))
        now = time.time()
        for details, n in (
            ('{"decision": "blocked", "reason": "jev: classifier unavailable, '
             'failed closed", "target": "python -m flake8 ."}', 3),
            ('{"decision": "blocked", "reason": "policy: exec", '
             '"target": "rm -rf /"}', 2),
            ('{"decision": "allowed", "reason": "ok", "target": "ls"}', 9),
        ):
            for _ in range(n):
                conn.execute(
                    'INSERT INTO action_log (agent_id, action, details, ts) '
                    'VALUES (?, ?, ?, ?)',
                    ('marcus', 'execute_classify', details, now - 60))
        conn.execute(
            'INSERT INTO action_log (agent_id, action, details, ts) '
            'VALUES (?, ?, ?, ?)',
            ('ruth', 'browse_failed', '{"error": "boom"}', now - 120))
        conn.execute(
            'INSERT INTO health_alerts (category, severity, message, ts) '
            'VALUES (?, ?, ?, ?)',
            ('coverage', 'high', 'coverage dipped below floor', now - 300))


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


class OpsFailures(unittest.TestCase):
    def test_requires_auth(self):
        r = client().get('/api/ops/failures')
        self.assertEqual(r.status_code, 401)

    def test_regular_agent_is_403(self):
        with unittest.mock.patch.object(
                serve, '_agent_id_for_key', lambda k: 'marcus' if k else None), \
                unittest.mock.patch.object(
                    serve, 'verify_agent_key', lambda a, k: k == _KEY), \
                unittest.mock.patch.object(
                    serve, '_db') as db_ctx:
            conn = db_ctx.return_value.__enter__.return_value
            conn.execute.return_value.fetchone.return_value = (
                '{"id": "marcus", "role": "worker"}',)
            r = client().get('/api/ops/failures',
                             headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 403)
        self.assertIn('regional VP', r.json()['error'])

    def test_groups_blocks_and_breaks_out_outage(self):
        r = client().get('/api/ops/failures', params={'hours': '12'},
                         headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['windowHours'], 12)
        self.assertEqual(body['classifierUnavailable'], 3)
        patterns = {p['pattern']: p['count'] for p in body['executeBlocks']}
        self.assertTrue(any('rm -rf' in k for k in patterns), patterns)
        self.assertFalse(any('classifier unavailable' in k
                             for k in patterns), patterns)
        self.assertGreaterEqual(body['failedActions'], 1)
        self.assertEqual(len(body['healthAlerts']), 1)
        self.assertEqual(body['healthAlerts'][0]['severity'], 'high')

    def test_window_clamped(self):
        r = client().get('/api/ops/failures', params={'hours': '99999'},
                         headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['windowHours'], serve._OPS_MAX_HOURS)

    def test_read_only_no_writes(self):
        before = serve._ops_failures(24)
        client().get('/api/ops/failures', headers={'X-Agent-Key': _KEY})
        after = serve._ops_failures(24)
        self.assertEqual(before['failedActions'], after['failedActions'])


if __name__ == '__main__':
    unittest.main()
