"""The admin bulk-close endpoint (191) with strict auth and pattern scope.

Runaway card families (the 2026-11-05 disk-full/weatherstation re-card
pump, 167 open cards) need an executive instrument: the operator supplies
explicit title patterns, the endpoint cancels only non-terminal rows with
no saved artifact, and every close is logged with the matched pattern."""
import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    import tempfile
    import shutil
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-admin-close-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        check_rate_limit=lambda aid: True,
    )
    _PATCHER.start()
    serve.init_db()
    with serve._db() as conn:
        conn.execute('INSERT INTO agent_keys (agent_id, secret_key, '
                     'created_at) VALUES (?, ?, ?)',
                     ('theo', 'key-theo', 0.0))
        conn.execute('INSERT INTO roster_agent (id, seq, blob, updated_at) '
                     'VALUES (?, ?, ?, ?)',
                     ('theo', 0, json.dumps(
                         {'id': 'theo', 'director': None, 'role': 'chief',
                          'isAdmin': True}), 0.0))


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


def _state():
    return {
        'agentRoster': [{'id': 'theo', 'isAdmin': True}],
        'tasks': {
            't1': {'id': 't1', 'title': 'Fix: [SPIKE] Strip leaked weatherstation '
                                       'text from task-6320', 'status': 'repairing'},
            't2': {'id': 't2', 'title': 'Fix disk-full failure on task-12270',
                   'status': 'blocked'},
            't3': {'id': 't3', 'title': 'Fix disk-full with the real '
                                        'patched module',
                   'status': 'working', 'libraryPath': 'library/x.md'},
            't4': {'id': 't4', 'title': 'Fix: disk-full second variant',
                   'status': 'done'},
        },
        'workQueue': [
            {'id': 'q1', 'title': '[STORY] Park task-12353 and stop the CI '
                                  'investigation re-card loop'},
            {'id': 'q2', 'title': 'Real pending work'},
        ],
    }


AUTH = {'X-Agent-Key': 'key-theo'}


class AdminCloseStaleCards(unittest.TestCase):
    def setUp(self):
        self.st = _state()
        for name, val in (('get_state_from_db', lambda *a, **k: self.st),
                          ('save_state_to_db', lambda s: None),
                          ('log_action', lambda *a, **k: None),
                          ('_state_begin', lambda: self.st),
                          ('verify_session', lambda sess: False)):
            self._p = unittest.mock.patch.object(serve, name, val)
            self._p.start()
            self.addCleanup(self._p.stop)

    def test_requires_admin_or_player(self):
        r = client().post('/api/admin/close-stale-cards',
                          json={'patterns': ['disk-full']})
        self.assertEqual(r.status_code, 401)

    def test_requires_explicit_patterns(self):
        r = client().post('/api/admin/close-stale-cards', headers=AUTH,
                          json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn('patterns', r.json()['error'])

    def test_bad_regex_rejected(self):
        r = client().post('/api/admin/close-stale-cards', headers=AUTH,
                          json={'patterns': ['([']})
        self.assertEqual(r.status_code, 400)

    def test_closes_matched_nonterminal_without_artifact(self):
        r = client().post('/api/admin/close-stale-cards', headers=AUTH,
                          json={'patterns': ['disk-full|weatherstation|park task-12'],
                                'reason': 'underlying work already done'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # t1 (weatherstation, repairing) and t2 (disk-full, blocked)
        # close; t3 matches the pattern but carries a real artifact, so it
        # is SKIPPED; t4 is terminal and untouched; q1 closes, q2 stays.
        self.assertEqual(body['closedTasks'], 2)
        self.assertEqual(body['closedQueue'], 1)
        self.assertIn('t3', body['skippedWithArtifact'])
        self.assertEqual(self.st['tasks']['t1']['status'], 'cancelled')
        self.assertEqual(self.st['tasks']['t1']['statusReason'],
                         'admin_bulk_close')
        self.assertNotIn('_peerRepairPending', self.st['tasks']['t1'])
        self.assertEqual(self.st['tasks']['t3']['status'], 'working')
        self.assertEqual(self.st['tasks']['t4']['status'], 'done')
        self.assertEqual([q['id'] for q in self.st['workQueue']], ['q2'])
        self.assertEqual(self.st['workQueue'][0]['title'], 'Real pending work')

    def test_each_close_is_logged(self):
        logged = []
        with unittest.mock.patch.object(serve, 'log_action',
                                        lambda aid, action, details=None,
                                        **k: logged.append(
                                            (aid, action, details)) or None):
            r = client().post('/api/admin/close-stale-cards', headers=AUTH,
                              json={'patterns': ['weatherstation'],
                                    'reason': 'test close'})
        self.assertEqual(r.status_code, 200, r.text)
        close_rows = [l for l in logged if l[1] == 'admin_bulk_closed_card']
        self.assertEqual(len(close_rows), 1)
        self.assertEqual(close_rows[0][0], 'theo')
        self.assertEqual(close_rows[0][2]['pattern'], 'weatherstation')
        done = [l for l in logged if l[1] == 'admin_bulk_close_complete']
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][2]['closedTasks'], 1)


if __name__ == '__main__':
    unittest.main()
