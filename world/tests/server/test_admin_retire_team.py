"""The admin retire-team endpoint (192): dissolve a team transactionally.

Org rule (2026-11-06): the regional VP must not also direct a team. The
endpoint moves every member's roster `director` pointer to receiving teams
(a team IS its director), retasks the retired team's open cards to follow
their assignee's new team, and removes the team row -- refusing an active
sprint or the last team.
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-retire-team-test-')
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
            {'id': 'nora', 'villageChief': True, 'isDirector': True},
            {'id': 'ben', 'isDirector': True},
            {'id': 'w1', 'director': 'nora'},
            {'id': 'w2', 'director': 'nora'},
            {'id': 'w3', 'director': 'nora'},
        ],
        'teams': [
            {'id': 'nora', 'name': "Supporting Director's Team",
             'directorId': 'nora', 'members': ['w1', 'w2', 'w3']},
            {'id': 'ben', 'name': "Ben's Team", 'directorId': 'ben',
             'members': ['b1']},
        ],
        'sprints': {},
        'tasks': {
            't1': {'id': 't1', 'title': 'work', 'status': 'working',
                   'teamId': 'nora', 'assignedTo': 'w1'},
            't2': {'id': 't2', 'title': 'work', 'status': 'queued',
                   'teamId': 'nora', 'assignedTo': 'w2'},
            't3': {'id': 't3', 'title': 'work', 'status': 'done',
                   'teamId': 'nora', 'assignedTo': 'w3'},
            't4': {'id': 't4', 'title': 'work', 'status': 'working',
                   'teamId': 'nora', 'assignedTo': None},
        },
        'workQueue': [{'id': 'q1', 'title': 'x', 'teamId': 'nora'}],
        'backlogRequests': [{'id': 'r1', 'title': 'x', 'teamId': 'nora'}],
        'agents': {'theo': {}, 'nora': {}, 'ben': {},
                   'w1': {}, 'w2': {}, 'w3': {}},
    }


AUTH = {'X-Agent-Key': 'key-theo'}


class RetireTeam(unittest.TestCase):
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
        r = client().post('/api/admin/retire-team', json={'teamId': 'nora'})
        self.assertEqual(r.status_code, 401)

    def test_unknown_team_404(self):
        r = client().post('/api/admin/retire-team', headers=AUTH,
                          json={'teamId': 'ghost'})
        self.assertEqual(r.status_code, 404)

    def test_active_sprint_refused(self):
        self.st['sprints'] = {'s1': {'status': 'active',
                                     'teamIds': ['nora']}}
        r = client().post('/api/admin/retire-team', headers=AUTH,
                          json={'teamId': 'nora'})
        self.assertEqual(r.status_code, 409)
        self.assertIn('sprint', r.json()['error'])

    def test_members_follow_new_directors_and_tasks_retask(self):
        r = client().post('/api/admin/retire-team', headers=AUTH,
                          json={'teamId': 'nora', 'reason': 'VP cannot direct'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['team'], 'nora')
        self.assertEqual(sorted(body['membersMoved']), ['w1', 'w2', 'w3'])
        # the team row is gone
        self.assertEqual([t['id'] for t in self.st['teams']], ['ben'])
        # every member's roster pointer left nora
        for wid in ('w1', 'w2', 'w3'):
            row = next(r for r in self.st['agentRoster'] if r['id'] == wid)
            self.assertEqual(row['director'], 'ben', wid)
        # ben's team absorbed them
        ben = self.st['teams'][0]
        self.assertEqual(sorted(ben['members']), ['b1', 'w1', 'w2', 'w3'])
        # open cards followed their assignee's new team; terminal untouched
        self.assertEqual(self.st['tasks']['t1']['teamId'], 'ben')
        self.assertEqual(self.st['tasks']['t2']['teamId'], 'ben')
        self.assertEqual(self.st['tasks']['t3']['teamId'], 'nora')  # done: left
        # unassigned open card drops its teamId (owner chain falls back to
        # the assignee's roster pointer)
        self.assertIsNone(self.st['tasks']['t4']['teamId'])
        self.assertIsNone(self.st['workQueue'][0]['teamId'])
        self.assertIsNone(self.st['backlogRequests'][0]['teamId'])

    def test_village_chief_role_survives(self):
        client().post('/api/admin/retire-team', headers=AUTH,
                      json={'teamId': 'nora'})
        nora = next(r for r in self.st['agentRoster'] if r['id'] == 'nora')
        self.assertTrue(nora.get('villageChief'))
