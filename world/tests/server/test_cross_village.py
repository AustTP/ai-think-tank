"""The cross-village API and team migration (195 + 196).

The parent is the post office between village DBs: escalate, relay
briefs/cards, an org->village outbox, team-placement registration, and
the admin move-team surgery (village inserts first, parent removes
second, both in their own processes).
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-cross-village-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


TOKEN = {'X-Parent-Token': 'tok-1'}
_AUTH = {'X-Agent-Key': 'key-theo'}


def _register_village(vid='north'):
    with serve._db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO villages_registry (id, name, api_base,'
            ' created_at) VALUES (?, ?, ?, ?)',
            (vid, vid, 'http://x:1', 0.0))


class CrossVillageApi(unittest.TestCase):
    def setUp(self):
        os.environ['THINK_TANK_PARENT_TOKEN'] = 'tok-1'
        self.addCleanup(os.environ.pop, 'THINK_TANK_PARENT_TOKEN', None)
        self._ps = []
        for name, val in (('log_action', lambda *a, **k: None),
                          ('save_state_to_db', lambda s: None),
                          ('_state_begin', lambda: self.st),
                          ('check_rate_limit', lambda aid: True)):
            self._ps.append(unittest.mock.patch.object(serve, name, val))
            self._ps[-1].start()
            self.addCleanup(self._ps[-1].stop)

    def _state(self, **extra):
        st = {'agents': {'theo': {'mailbox': []},
                         'ben': {'mailbox': []}},
              'agentRoster': [
                  {'id': 'theo', 'isAdmin': True},
                  {'id': 'ben', 'isDirector': True}],
              'teams': [], 'tasks': {}, 'workQueue': [], 'sprints': {}}
        st.update(extra)
        self.st = st
        return st

    def test_escalation_lands_in_the_chief_of_staff_mailbox(self):
        self._state()
        _register_village()
        r = client().post('/api/villages/north/escalate', headers=TOKEN,
                          json={'items': [{'from': 'ora', 'kind': 'ask',
                                           'text': 'need a data source',
                                           'taskId': 't9'}]})
        self.assertEqual(r.status_code, 200, r.text)
        box = self.st['agents']['theo']['mailbox']
        self.assertEqual(len(box), 1)
        self.assertIn('[village north] ora (ask) (task t9)', box[0]['text'])

    def test_relayed_brief_reaches_the_director_homed_here(self):
        self._state()
        _register_village()
        r = client().post('/api/villages/north/relay-brief', headers=TOKEN,
                          json={'directorId': 'ben', 'text': 'team report',
                                'teamName': "Ben's North Team"})
        self.assertEqual(r.status_code, 200, r.text)
        box = self.st['agents']['ben']['mailbox']
        self.assertEqual(box[0]['kind'], 'supervisor_brief')
        self.assertIn('village north', box[0]['title'])

    def test_relayed_brief_404s_for_a_director_not_homed_here(self):
        self._state()
        _register_village()
        r = client().post('/api/villages/north/relay-brief', headers=TOKEN,
                          json={'directorId': 'ghost', 'text': 'x'})
        self.assertEqual(r.status_code, 404)

    def test_relayed_card_is_queued_with_village_attribution(self):
        self._state()
        _register_village()
        r = client().post('/api/villages/north/relay-card', headers=TOKEN,
                          json={'cards': [{'title': '[SUPERVISE] Cross-team',
                                           'assignedTo': 'ben'}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.st['workQueue'][0]['villageId'], 'north')

    def test_inbox_is_consumed_at_most_once(self):
        _register_village()
        from serve import _village_deliver_to_org
        _village_deliver_to_org('north', [
            {'to': 'ora', 'text': 'focus on the migration'},
            {'to': 'ven', 'text': 'hiring approved'}])
        r1 = client().get('/api/villages/north/inbox', headers=TOKEN)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(len(r1.json()['items']), 2)
        r2 = client().get('/api/villages/north/inbox', headers=TOKEN)
        self.assertEqual(r2.json()['items'], [])

    def test_bad_token_is_401_everywhere(self):
        _register_village()
        posts = ('/api/villages/north/escalate',
                 '/api/villages/north/relay-brief',
                 '/api/villages/north/relay-card',
                 '/api/villages/north/teams')
        for path in posts:
            r = client().post(path, json={'items': []},
                              headers={'X-Parent-Token': 'nope'})
            self.assertEqual(r.status_code, 401, path)
        r = client().get('/api/villages/north/inbox',
                         headers={'X-Parent-Token': 'nope'})
        self.assertEqual(r.status_code, 401)


class MoveTeam(unittest.TestCase):
    def setUp(self):
        os.environ['THINK_TANK_PARENT_TOKEN'] = 'tok-1'
        self.addCleanup(os.environ.pop, 'THINK_TANK_PARENT_TOKEN', None)
        for name, val in (('log_action', lambda *a, **k: None),
                          ('save_state_to_db', lambda s: None),
                          ('_state_begin', lambda: self.st),
                          ('check_rate_limit', lambda aid: True)):
            self._p = unittest.mock.patch.object(serve, name, val)
            self._p.start()
            self.addCleanup(self._p.stop)
        self._village_posts = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({'ok': True}).encode()

        def _fake_post(req, timeout=30, **kw):
            self._village_posts.append(json.loads(req.data))
            return _FakeResp()
        self._p2 = unittest.mock.patch.object(
            serve.urllib.request, 'urlopen', _fake_post)
        self._p2.start()
        self.addCleanup(self._p2.stop)

    def _state(self, **extra):
        st = {
            'agents': {'theo': {'mailbox': []}, 'eli': {'mailbox': []},
                       'callie': {'mailbox': []}},
            'agentRoster': [
                {'id': 'theo', 'isAdmin': True},
                {'id': 'eli', 'isDirector': True},
                {'id': 'callie', 'director': 'eli'}],
            'teams': [{'id': 'eli', 'name': "Eli's Team",
                       'directorId': 'eli', 'members': ['callie']}],
            'sprints': {},
            'tasks': {'t1': {'id': 't1', 'teamId': 'eli', 'status': 'working',
                             'assignedTo': 'callie', 'title': 'w'}},
            'workQueue': [],
        }
        st.update(extra)
        self.st = st
        return st

    def test_dry_run_inspects_without_moving(self):
        self._state()
        _register_village()
        r = client().post('/api/admin/move-team-to-village', headers=TOKEN,
                          json={'teamId': 'eli', 'villageId': 'north',
                                'dryRun': True})
        self.assertEqual(r.status_code, 200, r.text)
        plan = r.json()
        self.assertEqual(plan['members'], ['callie'])
        self.assertEqual(plan['taskCount'], 1)
        self.assertEqual(len(self.st['teams']), 1)  # untouched
        self.assertEqual(self._village_posts, [])

    def test_move_posts_to_the_village_then_removes_locally(self):
        self._state()
        _register_village()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO roster_agent (id, seq, blob, updated_at)'
                ' VALUES (?, ?, ?, ?)',
                ('callie', 0, json.dumps(
                    {'id': 'callie', 'director': 'eli'}), 0.0))
            conn.execute(
                'INSERT INTO agent_keys (agent_id, secret_key, created_at)'
                ' VALUES (?, ?, ?)', ('callie', 'key-c', 0.0))
        r = client().post('/api/admin/move-team-to-village', headers=TOKEN,
                          json={'teamId': 'eli', 'villageId': 'north'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self._village_posts), 1)
        sent = self._village_posts[0]
        self.assertEqual(sent['team']['id'], 'eli')
        self.assertEqual(sent['members'], ['callie'])
        self.assertIn('t1', sent['tasks'])
        self.assertEqual(self.st['teams'], [])          # team removed
        self.assertNotIn('t1', self.st['tasks'])        # card moved
        with serve._db() as conn:
            self.assertEqual(conn.execute(
                'SELECT count(*) FROM roster_agent WHERE id="callie"'
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                'SELECT count(*) FROM agent_keys WHERE agent_id="callie"'
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                'SELECT village_id FROM villages_teams WHERE team_id="eli"'
            ).fetchone()[0], 'north')

    def test_active_sprint_refuses_the_move(self):
        self._state(sprints={'s1': {'id': 's1', 'status': 'active',
                                    'teamIds': ['eli']}})
        _register_village()
        r = client().post('/api/admin/move-team-to-village', headers=TOKEN,
                          json={'teamId': 'eli', 'villageId': 'north'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._village_posts, [])
        self.assertEqual(len(self.st['teams']), 1)      # untouched

    def test_unknown_village_refuses(self):
        self._state()
        r = client().post('/api/admin/move-team-to-village', headers=TOKEN,
                          json={'teamId': 'eli', 'villageId': 'ghost'})
        self.assertEqual(r.status_code, 404)


class MoveSemantics(unittest.TestCase):
    """The 2026-11-06 eli-move lesson: assignee-based collection dragged
    five parent-team cards into a village. The move takes ONLY the team's
    own cards; foreign cards stay home with their assignee cleared."""

    def setUp(self):
        os.environ['THINK_TANK_PARENT_TOKEN'] = 'tok-1'
        self.addCleanup(os.environ.pop, 'THINK_TANK_PARENT_TOKEN', None)
        for name, val in (('log_action', lambda *a, **k: None),
                          ('save_state_to_db', lambda s: None),
                          ('_state_begin', lambda: self.st),
                          ('check_rate_limit', lambda aid: True)):
            self._p = unittest.mock.patch.object(serve, name, val)
            self._p.start()
            self.addCleanup(self._p.stop)

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({'ok': True}).encode()
        self.sent = []

        def _fake_post(req, timeout=30, **kw):
            self.sent.append(json.loads(req.data))
            return _FakeResp()
        self._p2 = unittest.mock.patch.object(
            serve.urllib.request, 'urlopen', _fake_post)
        self._p2.start()
        self.addCleanup(self._p2.stop)

    def _state(self):
        self.st = {
            'agents': {'theo': {'mailbox': []}, 'eli': {'mailbox': []},
                       'callie': {'mailbox': []}},
            'agentRoster': [
                {'id': 'theo', 'isAdmin': True},
                {'id': 'eli', 'isDirector': True},
                {'id': 'callie', 'director': 'eli'}],
            'teams': [{'id': 'eli', 'name': "Eli's Team",
                       'directorId': 'eli', 'members': ['callie']}],
            'sprints': {},
            'tasks': {
                'own': {'id': 'own', 'teamId': 'eli', 'status': 'working',
                        'assignedTo': 'callie'},
                'foreign': {'id': 'foreign', 'teamId': 'ben',
                            'status': 'working', 'assignedTo': 'callie'},
                'loose': {'id': 'loose', 'teamId': None, 'status': 'walking',
                          'assignedTo': 'callie'},
            },
            'workQueue': [],
        }
        return self.st

    def test_move_takes_only_the_teams_own_cards(self):
        self._state()
        _register_village()
        r = client().post('/api/admin/move-team-to-village', headers=TOKEN,
                          json={'teamId': 'eli', 'villageId': 'north'})
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.sent[0]
        self.assertIn('own', sent['tasks'])
        self.assertNotIn('foreign', sent['tasks'])
        self.assertEqual(sent['reassignedElsewhere'], ['foreign'])
        # the loose card follows its worker (worker-owned, teamless)
        self.assertIn('loose', sent['tasks'])
        # foreign card stays home, assignee cleared for re-staffing
        self.assertIn('foreign', self.st['tasks'])
        self.assertIsNone(self.st['tasks']['foreign']['assignedTo'])

    def test_return_village_cards_rehomes_them(self):
        self._state()
        _register_village()

        released = {}

        class _RelResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {'ok': True,
                     'released': {'foreign': dict(
                         self.st['tasks']['foreign'])}}).encode()
        state_holder = self

        def _fake_post(req, timeout=30, **kw):
            released.update(json.loads(req.data))
            st = state_holder.st
            card = st['tasks'].get('foreign')
            st['tasks'].pop('foreign', None)
            payload = json.dumps({'ok': True,
                                  'released': {'foreign': card}}).encode()

            class _R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return payload
            return _R()
        self._p2.set_secs = None
        self._p2.stop()
        self._p2 = unittest.mock.patch.object(
            serve.urllib.request, 'urlopen', _fake_post)
        self._p2.start()
        self.addCleanup(self._p2.stop)
        self.st['tasks']['foreign']['assignedTo'] = 'callie'
        r = client().post('/api/admin/return-village-cards', headers=TOKEN,
                          json={'villageId': 'north',
                                'taskIds': ['foreign']})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['restored'], ['foreign'])
        card = self.st['tasks']['foreign']
        self.assertIsNone(card['assignedTo'])
        self.assertIn('returned from village north', card['note'])
