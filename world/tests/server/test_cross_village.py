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


class VisitsAndTalk(unittest.TestCase):
    """Walking between villages: the parent's cross-village directory and
    post office let an agent talk to someone in another village and read
    a visitor snapshot, without either village talking to the other."""

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

    def _state(self):
        self.st = {'agents': {'theo': {'mailbox': []}},
                   'agentRoster': [{'id': 'theo', 'isAdmin': True}],
                   'teams': [], 'tasks': {}, 'workQueue': []}
        return self.st

    def test_heartbeat_builds_the_cross_village_directory(self):
        self._state()
        _register_village()
        r = client().post('/api/villages/north/heartbeat', headers=TOKEN,
                          json={'ok': True, 'openTasks': 3, 'queued': 0,
                                'alerts24h': 0,
                                'agents': [{'id': 'ven', 'name': 'Venus',
                                            'role': 'Director',
                                            'room': 'warroom'}]})
        self.assertEqual(r.status_code, 200, r.text)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT name, role, room FROM villages_agents'
                ' WHERE village_id = ? AND agent_id = ?',
                ('north', 'ven')).fetchone()
        self.assertEqual(row, ('Venus', 'Director', 'warroom'))

    def test_post_routes_to_a_parent_homed_recipient(self):
        self._state()
        _register_village()
        r = client().post('/api/villages/north/post', headers=TOKEN,
                          json={'to': 'theo', 'from': 'ora',
                                'text': 'saw the migration, looks right'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['routed'], 'parent')
        box = self.st['agents']['theo']['mailbox']
        self.assertIn('ora (village north)', box[-1]['text'])

    def test_post_routes_to_another_villages_agent(self):
        self._state()
        _register_village()
        _register_village('south')
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO villages_agents (village_id, agent_id, name,'
                ' role, room, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                ('south', 'kai', 'Kai', 'Worker', 'lab', 0.0))
        r = client().post('/api/villages/north/post', headers=TOKEN,
                          json={'to': 'kai', 'from': 'ora',
                                'text': 'how is the build going?'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['routed'], 'village:south')
        with serve._db() as conn:
            out = conn.execute(
                'SELECT to_agent, text FROM villages_outbox'
                ' WHERE village_id = ?', ('south',)).fetchone()
        self.assertEqual(out[0], 'kai')
        self.assertIn('ora (village north)', out[1])

    def test_post_refuses_unknown_recipients(self):
        self._state()
        _register_village()
        r = client().post('/api/villages/north/post', headers=TOKEN,
                          json={'to': 'ghost', 'text': 'hi'})
        self.assertEqual(r.status_code, 404)

    def test_village_visit_snapshot(self):
        self.st = {'agents': {'ven': {'inRoom': 'warroom', 'busy': False},
                              'pax': {'inRoom': 'lab', 'busy': True}},
                   'agentRoster': [
                       {'id': 'ven', 'name': 'Venus', 'role': 'Director'},
                       {'id': 'pax', 'name': 'Pax', 'role': 'Builder'}],
                   'teams': [{'id': 'ven', 'name': "Venus's Team",
                              'directorId': 'ven', 'members': ['pax']}],
                   'tasks': {'t1': {'status': 'working', 'teamId': 'ven'}},
                   'workQueue': []}
        _register_village()
        self._vm = unittest.mock.patch.object(serve, 'VILLAGE_MODE', True)
        self._vm.start()
        self.addCleanup(self._vm.stop)
        r = client().get('/api/villages-self/visit', headers=TOKEN)
        self.assertEqual(r.status_code, 200, r.text)
        view = r.json()
        self.assertEqual(view['rooms']['warroom'][0]['id'], 'ven')
        self.assertEqual(view['openByTeam'], {'ven': 1})
        self.assertEqual(view['teams'][0]['name'], "Venus's Team")

    def test_visit_refuses_without_village_mode(self):
        r = client().get('/api/villages-self/visit', headers=TOKEN)
        self.assertEqual(r.status_code, 404)


class Visits(unittest.TestCase):
    """True mobility: an agent temporarily rehomes into another village
    and comes back. The DB surgery itself runs against a live postgres
    (proven live on 2026-11-06); these tests pin the VALIDATION and the
    record-keeping."""

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
        serve._ensure_visits_table()
        serve._ensure_registry_columns()
        with serve._db() as conn:
            conn.execute('DELETE FROM villages_visits')
            conn.execute(
                "INSERT INTO villages_registry (id, name, api_base,"
                " created_at, launchd_label, db_url) VALUES"
                " ('north', 'north', 'http://x:1', 0.0, 'lbl-1',"
                " 'postgresql://x/north')"
                " ON CONFLICT(id) DO UPDATE SET launchd_label = 'lbl-1',"
                " db_url = 'postgresql://x/north'")

    def _state(self, **extra):
        st = {'agents': {'theo': {'mailbox': []}, 'warren': {'mailbox': []}},
              'agentRoster': [{'id': 'theo', 'isAdmin': True},
                              {'id': 'warren'}],
              'teams': [], 'tasks': {}, 'workQueue': []}
        st.update(extra)
        self.st = st
        return st

    def test_send_refuses_an_agent_with_an_open_task(self):
        self._state(tasks={'t1': {'assignedTo': 'warren',
                                  'status': 'working'}})
        r = client().post('/api/admin/send-visitor', headers=TOKEN,
                          json={'agentId': 'warren', 'villageId': 'north',
                                'hours': 1})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn('open task', r.json()['error'])

    def test_send_refuses_an_unknown_village_or_agent(self):
        self._state()
        r = client().post('/api/admin/send-visitor', headers=TOKEN,
                          json={'agentId': 'warren', 'villageId': 'ghost'})
        self.assertEqual(r.status_code, 409)
        r = client().post('/api/admin/send-visitor', headers=TOKEN,
                          json={'agentId': 'ghost', 'villageId': 'north'})
        self.assertEqual(r.status_code, 404)

    def test_send_records_the_visit(self):
        self._state()
        calls = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{}'

        def _fake_urlopen(req, timeout=0, **kw):
            calls.append(req)
            return _Resp()
        # patch every piece of the surgery the test cannot run: launchctl,
        # port wait, and the village DB writes
        for name, val in (
                ('_bootout', lambda label: None),
                ('_bootstrap', lambda label: None),
                ('_wait_port', lambda *a, **k: True),
                ('_village_port', lambda label: 8938),
                ('_village_insert_agent',
                 lambda dsn, aid, rows, blob: calls.append(
                     ('insert', aid, json.loads(json.dumps(blob)))))):
            p = unittest.mock.patch.object(serve, name, val)
            p.start()
            self.addCleanup(p.stop)
        # the parent-side footprint rows (idempotent: a prior test may
        # have left them)
        with serve._db() as conn:
            for sql, args in (
                    ('INSERT INTO roster_agent (id, seq, blob, updated_at)'
                     ' VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING',
                     ('warren', 0, json.dumps({'id': 'warren'}), 0.0)),
                    ('INSERT INTO live_agent (id, seq, blob, updated_at)'
                     ' VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING',
                     ('warren', 0, json.dumps({'mailbox': []}), 0.0))):
                conn.execute(sql, args)
        r = client().post('/api/admin/send-visitor', headers=TOKEN,
                          json={'agentId': 'warren', 'villageId': 'north',
                                'hours': 2, 'reason': 'learn the layout'})
        self.assertEqual(r.status_code, 200, r.text)
        inserts = [c for c in calls if isinstance(c, tuple)]
        self.assertEqual(len(inserts), 1)
        blob = inserts[0][2]
        self.assertEqual(blob['_visitFrom'], 'main')
        self.assertGreater(blob['_visitReturnAt'], 0)
        self.assertTrue(any('VISITING' in str(m.get('text'))
                            for m in blob.get('mailbox', [])))
        with serve._db() as conn:
            row = conn.execute(
                'SELECT to_village, returned_at FROM villages_visits'
                ' WHERE agent_id = ?', ('warren',)).fetchone()
        self.assertEqual(row[0], 'north')
        self.assertIsNone(row[1])
        self.assertNotIn('warren', self.st['agents'])  # gone from home

    def test_return_refuses_when_not_due_and_closes_when_forced(self):
        self._state()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO villages_visits (agent_id, from_village,'
                ' to_village, started_at, return_at) VALUES'
                " ('warren', 'main', 'north', 0, ?)",
                (serve.time.time() + 3600,))
        r = client().post('/api/villages/north/return-visitor', headers=TOKEN,
                          json={'agentId': 'warren'})
        self.assertEqual(r.status_code, 409, r.text)
        # forced early return: the surgery runs for real against a
        # throwaway postgres village DB (the actual SQL, not a stub)
        import psycopg2
        admin = psycopg2.connect('postgresql://poole86@/postgres')
        admin.autocommit = True
        acur = admin.cursor()
        acur.execute('DROP DATABASE IF EXISTS tcv_village_tmp')
        acur.execute('CREATE DATABASE tcv_village_tmp')
        admin.commit()
        acur.close()
        admin.close()

        # no DROP on teardown: a lingering pooled session can hold the
        # tmp DB busy; the next run recreates it from scratch anyway
        vdb = psycopg2.connect('postgresql://poole86@/tcv_village_tmp')
        vcur = vdb.cursor()
        vcur.execute('CREATE TABLE roster_agent (id TEXT PRIMARY KEY,'
                     ' seq INTEGER, blob TEXT, updated_at REAL)')
        vcur.execute('CREATE TABLE live_agent (id TEXT PRIMARY KEY,'
                     ' seq INTEGER, blob TEXT, updated_at REAL)')
        vcur.execute('CREATE TABLE agent_keys (agent_id TEXT PRIMARY KEY,'
                     ' secret_key TEXT, created_at REAL)')
        vcur.execute('INSERT INTO roster_agent VALUES (%s, %s, %s, %s)',
                     ('warren', 0, json.dumps({'id': 'warren'}), 0.0))
        vcur.execute('INSERT INTO live_agent VALUES (%s, %s, %s, %s)',
                     ('warren', 0, json.dumps(
                         {'mailbox': [], '_visitReturnAt': 1}), 0.0))
        vcur.execute('INSERT INTO agent_keys VALUES (%s, %s, %s)',
                     ('warren', 'key-warren', 0.0))
        vdb.commit()
        vcur.close()
        vdb.close()
        with serve._db() as conn:
            conn.execute('UPDATE villages_registry SET db_url = ?'
                         " WHERE id = 'north'",
                         ('postgresql://poole86@/tcv_village_tmp',))
        for name, val in (
                ('_bootout', lambda label: None),
                ('_bootstrap', lambda label: None),
                ('_wait_port', lambda *a, **k: True),
                ('_village_port', lambda label: 8938)):
            p = unittest.mock.patch.object(serve, name, val)
            p.start()
            self.addCleanup(p.stop)
        r = client().post('/api/villages/north/return-visitor', headers=TOKEN,
                          json={'agentId': 'warren', 'force': True})
        self.assertEqual(r.status_code, 200, r.text)
        with serve._db() as conn:
            returned = conn.execute(
                'SELECT returned_at FROM villages_visits'
                ' WHERE agent_id = ?', ('warren',)).fetchone()[0]
        self.assertIsNotNone(returned)
        self.assertIn('warren', self.st['agents'])  # back home
        # the HOME mail rode in her live blob (the real parent insert)
        with serve._db() as conn:
            blob = json.loads(conn.execute(
                "SELECT blob FROM live_agent WHERE id = 'warren'"
            ).fetchone()[0])
        self.assertTrue(any('HOME' in str(m.get('text'))
                            for m in blob.get('mailbox', [])))
        import psycopg2
        vdb = psycopg2.connect('postgresql://poole86@/tcv_village_tmp')
        vcur = vdb.cursor()
        vcur.execute('SELECT count(*) FROM roster_agent')
        self.assertEqual(vcur.fetchone()[0], 0)  # really gone from there
        vcur.close()
        vdb.close()


class AgentInitiatedVisits(unittest.TestCase):
    """An agent asks to visit with her own key; the guardrails are the
    policy, so they must hold: no open task, cooldown, org concurrency,
    a real reason. She can also walk herself home early."""

    def setUp(self):
        os.environ['THINK_TANK_PARENT_TOKEN'] = 'tok-1'
        self.addCleanup(os.environ.pop, 'THINK_TANK_PARENT_TOKEN', None)
        for name, val in (('log_action', lambda *a, **k: None),
                          ('save_state_to_db', lambda s: None),
                          ('_state_begin', lambda: self.st),
                          ('check_rate_limit', lambda aid: True),
                          # the surgery primitives: launchctl, port
                          # probes, and the village DB writes
                          ('_bootout', lambda label: None),
                          ('_bootstrap', lambda label: None),
                          ('_wait_port', lambda *a, **k: True),
                          ('_village_port', lambda label: 8938),
                          ('_village_insert_agent',
                           lambda dsn, aid, rows, blob: None)):
            p = unittest.mock.patch.object(serve, name, val)
            p.start()
            self.addCleanup(p.stop)
        serve._ensure_visits_table()
        with serve._db() as conn:
            conn.execute('DELETE FROM villages_visits')
            conn.execute(
                "INSERT INTO villages_registry (id, name, api_base,"
                " created_at, launchd_label, db_url) VALUES"
                " ('north', 'north', 'http://x:1', 0.0, 'lbl-1',"
                " 'postgresql://x/north')"
                " ON CONFLICT(id) DO UPDATE SET launchd_label = 'lbl-1',"
                " db_url = 'postgresql://x/north'")

    def _state(self, **extra):
        st = {'agents': {'theo': {'mailbox': []}, 'ben': {}},
              'agentRoster': [{'id': 'theo', 'isAdmin': True},
                              {'id': 'ben'}],
              'teams': [], 'tasks': {}, 'workQueue': []}
        st.update(extra)
        self.st = st
        return st

    def _key(self, agent_id):
        with serve._db() as conn:
            conn.execute('INSERT INTO agent_keys (agent_id, secret_key,'
                         ' created_at) VALUES (?, ?, ?) ON CONFLICT'
                         ' (agent_id) DO UPDATE SET secret_key ='
                         ' EXCLUDED.secret_key',
                         (agent_id, f'key-{agent_id}', 0.0))

    def _post(self, path, agent_id=None, payload=None, headers=None):
        h = dict(headers or {})
        if agent_id:
            h['X-Agent-Key'] = f'key-{agent_id}'
        return client().post(path, headers=h, json=payload or {})

    def test_requires_an_agent_key(self):
        self._state()
        r = self._post('/api/visit-requests', payload={
            'villageId': 'north', 'reason': 'learn the review flow there'})
        self.assertEqual(r.status_code, 401, r.text)

    def test_refuses_a_thin_reason(self):
        self._state()
        self._key('ben')
        r = self._post('/api/visit-requests', 'ben',
                       {'villageId': 'north', 'reason': 'because'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('real reason', r.json()['error'])

    def test_refuses_while_holding_an_open_task(self):
        self._state(tasks={'t1': {'assignedTo': 'ben',
                                  'status': 'working'}})
        self._key('ben')
        r = self._post('/api/visit-requests', 'ben', {
            'villageId': 'north',
            'reason': 'learn the review flow used by the moved team'})
        self.assertEqual(r.status_code, 409, r.text)

    def test_enforces_the_twelve_hour_cooldown(self):
        self._state()
        self._key('ben')
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO villages_visits (agent_id, from_village,'
                ' to_village, started_at, return_at, returned_at) VALUES'
                " ('ben', 'main', 'north', ?, ?, ?)",
                (serve.time.time() - 3600, serve.time.time() - 1800,
                 serve.time.time() - 1800))
        r = self._post('/api/visit-requests', 'ben', {
            'villageId': 'north',
            'reason': 'learn the review flow used by the moved team'})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn('12h', r.json()['error'])

    def test_enforces_the_org_wide_visitor_cap(self):
        self._state()
        self._key('ben')
        with serve._db() as conn:
            for aid in ('v1', 'v2', 'v3'):
                conn.execute(
                    'INSERT INTO villages_visits (agent_id, from_village,'
                    ' to_village, started_at, return_at) VALUES'
                    " (?, 'main', 'north', 0, ?)",
                    (aid, serve.time.time() + 3600))
        r = self._post('/api/visit-requests', 'ben', {
            'villageId': 'north',
            'reason': 'learn the review flow used by the moved team'})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn('visiting', r.json()['error'])

    def test_grants_and_tells_theo(self):
        self._state()
        self._key('ben')
        with serve._db() as conn:
            for sql, args in (
                    ('INSERT INTO roster_agent (id, seq, blob, updated_at)'
                     ' VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING',
                     ('ben', 0, json.dumps({'id': 'ben'}), 0.0)),
                    ('INSERT INTO live_agent (id, seq, blob, updated_at)'
                     ' VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING',
                     ('ben', 0, json.dumps({'mailbox': []}), 0.0))):
                conn.execute(sql, args)
        r = self._post('/api/visit-requests', 'ben', {
            'villageId': 'north',
            'reason': 'learn the review flow used by the moved team'})
        self.assertEqual(r.status_code, 200, r.text)
        box = self.st['agents']['theo']['mailbox']
        self.assertIn('ben asked to visit village north', box[-1]['text'])
        with serve._db() as conn:
            visit = conn.execute(
                'SELECT to_village, returned_at FROM villages_visits'
                ' WHERE agent_id = ?', ('ben',)).fetchone()
        self.assertEqual(visit[0], 'north')
        self.assertIsNone(visit[1])

    def test_an_agent_returns_only_herself(self):
        self._state()
        self._key('ben')
        self._key('ava')
        r = self._post('/api/visit-requests/return', 'ava')
        # ava has no visit: the refusal names HER, not ben
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn('ava', r.json()['error'])
