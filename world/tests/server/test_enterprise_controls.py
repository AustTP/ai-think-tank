"""Enterprise posture and audit export: auth, redaction, digest, page."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_TMP = None
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='tt-enterprise-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP, 'test.db'),
        THINK_TANK_DIR=_TMP,
        AGENTS_DIR=os.path.join(_TMP, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP, 'library'),
        PASSPORT_PATH=os.path.join(_TMP, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _state():
    return {
        'agents': {
            'theo': {'id': 'theo', 'name': 'Theo'},
            'nora': {'id': 'nora', 'name': 'Nora'},
            'ruth': {'id': 'ruth', 'name': 'Ruth'},
        },
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'name': 'Nora', 'isDirector': True},
            {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'},
        ],
        'teams': [{'id': 'nora', 'directorId': 'nora', 'members': ['ruth']}],
        'tasks': {}, 'workQueue': [], 'backlogRequests': [], 'sim': {},
    }


class EnterpriseControls(unittest.TestCase):
    def setUp(self):
        self.theo_key = serve.get_or_create_agent_key('theo')
        self.ruth_key = serve.get_or_create_agent_key('ruth')
        serve.save_state_to_db(_state())
        self.client = TestClient(serve.app)

    def _get(self, path, key=None):
        headers = {'X-Agent-Key': key} if key else {}
        return self.client.get(path, headers=headers)

    def test_posture_requires_operator(self):
        self.assertEqual(self._get('/api/admin/enterprise/posture').status_code, 401)
        self.assertEqual(self._get('/api/admin/enterprise/posture',
                                   self.ruth_key).status_code, 403)
        r = self._get('/api/admin/enterprise/posture', self.theo_key)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        ids = {c['id'] for c in body['checks']}
        self.assertIn('operator_access', ids)
        self.assertIn('agent_key_storage', ids)
        self.assertIn('audit_export', ids)
        self.assertNotIn(self.theo_key, r.text)

    def test_agent_key_storage_is_reported_honestly(self):
        body = self._get('/api/admin/enterprise/posture', self.theo_key).json()
        check = next(c for c in body['checks'] if c['id'] == 'agent_key_storage')
        self.assertEqual(check['status'], 'fail')
        self.assertIn('plaintext', check['detail'])

    def test_audit_export_redacts_and_hashes(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, authorized, ts) '
                'VALUES (?, ?, ?, ?, ?)',
                ('ruth', 'secret_probe',
                 json.dumps({'secret_key': 'sk-live-should-not-leak',
                             'token': 'tok-should-not-leak', 'note': 'ok'}),
                 1, time.time()))
        r = self._get('/api/admin/enterprise/audit?format=json&limit=50', self.theo_key)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreaterEqual(body['count'], 1)
        self.assertEqual(len(body['sha256']), 64)
        text = json.dumps(body)
        self.assertNotIn('sk-live-should-not-leak', text)
        self.assertNotIn('tok-should-not-leak', text)
        self.assertIn('[REDACTED]', text)

    def test_audit_export_jsonl_has_digest_headers(self):
        r = self._get('/api/admin/enterprise/audit?format=jsonl&limit=10', self.theo_key)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers['content-type'].split(';')[0], 'application/x-ndjson')
        self.assertEqual(len(r.headers['X-Audit-SHA256']), 64)
        self.assertEqual(int(r.headers['X-Audit-Count']), len([x for x in r.text.splitlines() if x]))

    def test_export_is_logged(self):
        self._get('/api/admin/enterprise/audit?format=json&limit=5', self.theo_key)
        with serve._db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE action='enterprise_audit_export'").fetchone()[0]
        self.assertGreaterEqual(count, 1)

    def test_operator_page_is_served(self):
        r = self.client.get('/admin/enterprise')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Enterprise controls', r.text)
        self.assertIn('Export audit JSONL', r.text)
