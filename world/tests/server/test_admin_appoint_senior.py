"""The senior-director appointment endpoint (193).

Pointing the other directors' roster `director` pointer at one director
makes her the chain's root -- which _senior_most_director then derives.
The regional VP and admins stand outside the recursion and are never
re-pointed.
"""
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-appoint-senior-test-')
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
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'villageChief': True, 'isDirector': True,
             'director': 'theo'},
            {'id': 'ben', 'isDirector': True, 'director': 'theo'},
            {'id': 'iris', 'isDirector': True, 'director': 'theo'},
            {'id': 'dev', 'isDirector': True, 'director': 'theo'},
        ],
        'agents': {'theo': {}, 'nora': {}, 'ben': {}, 'iris': {}, 'dev': {}},
        'teams': [],
        'tasks': {}, 'workQueue': [],
    }


AUTH = {'X-Agent-Key': 'key-theo'}


class AppointSenior(unittest.TestCase):
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
        r = client().post('/api/admin/appoint-senior-director',
                          json={'directorId': 'ben'})
        self.assertEqual(r.status_code, 401)

    def test_unknown_agent_404(self):
        r = client().post('/api/admin/appoint-senior-director', headers=AUTH,
                          json={'directorId': 'ghost'})
        self.assertEqual(r.status_code, 404)

    def test_refuses_admin_and_the_regional_vp(self):
        for bad in ('theo', 'nora'):
            r = client().post('/api/admin/appoint-senior-director',
                              headers=AUTH, json={'directorId': bad})
            self.assertEqual(r.status_code, 409, bad)

    def test_appointment_repoints_every_other_director(self):
        r = client().post('/api/admin/appoint-senior-director', headers=AUTH,
                          json={'directorId': 'ben'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(r.json()['reports']), ['dev', 'iris'])
        rows = {r['id']: r for r in self.st['agentRoster']}
        self.assertEqual(rows['iris']['director'], 'ben')
        self.assertEqual(rows['dev']['director'], 'ben')
        self.assertEqual(rows['ben']['director'], 'theo')   # ben stays root
        self.assertEqual(rows['nora']['director'], 'theo')  # VP untouched


class RevokeSenior(unittest.TestCase):
    def setUp(self):
        self.st = _state()
        # start from the appointed shape: everyone under ben
        for r in self.st['agentRoster']:
            if r.get('isDirector') and r['id'] not in ('ben', 'nora', 'theo'):
                r['director'] = 'ben'
        self.st['agentRoster'][2]['director'] = 'theo'  # ben root
        for name, val in (('get_state_from_db', lambda *a, **k: self.st),
                          ('save_state_to_db', lambda s: None),
                          ('log_action', lambda *a, **k: None),
                          ('_state_begin', lambda: self.st),
                          ('verify_session', lambda sess: False)):
            self._p = unittest.mock.patch.object(serve, name, val)
            self._p.start()
            self.addCleanup(self._p.stop)

    def test_revoke_flattens_to_equal_standing(self):
        r = client().post('/api/admin/appoint-senior-director', headers=AUTH,
                          json={'revoke': True})
        self.assertEqual(r.status_code, 200, r.text)
        rows = {r['id']: r for r in self.st['agentRoster']}
        for did in ('iris', 'dev'):
            self.assertEqual(rows[did]['director'], 'theo', did)
        self.assertEqual(rows['ben']['director'], 'theo')   # equal again
        self.assertEqual(rows['nora']['director'], 'theo')  # VP untouched

    def test_revoke_needs_no_director_id(self):
        r = client().post('/api/admin/appoint-senior-director', headers=AUTH,
                          json={'revoke': True})
        self.assertEqual(r.status_code, 200)
