"""Team-definable activation doctrine (199): permanent/standing teams
(trading, bug bounty, on-call) declare their OWN levels -- the tank stores
them and routes notifications but imposes no severity vocabulary. Reusable
templates live in doctrine_templates.json; a team ADOPTS one (the levels
are copied onto the team's record), then edits its own copy freely. A team
can run SEVERAL process doctrines at once (bounty + trading books), each
with independent levels and history. Only the team's own people
(director/scrum master/members) or the operator may define or change
levels."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None

LEVELS = [
    {'id': 'd1', 'name': 'DEFCON 1', 'trigger': 'live exploit window',
     'staffing': 'all hands', 'autonomy': 'no external posts',
     'demote': 'window closed', 'notify': True},
    {'id': 'd3', 'name': 'DEFCON 3', 'trigger': 'weekly patrol',
     'autonomy': 'normal scope', 'notify': False},
]


def setUpModule():
    global _TMP_DIR, _PATCHER
    import tempfile
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-doctrine-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        check_rate_limit=lambda aid: True,
    )
    _PATCHER.start()
    serve.init_db()
    with serve._db() as conn:
        for agent, key in (('w1', 'key-w1'), ('outsider', 'key-out')):
            conn.execute('INSERT INTO agent_keys (agent_id, secret_key, '
                         'created_at) VALUES (?, ?, ?)', (agent, key, 0.0))


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


def _state():
    return {
        'teams': [
            {'id': 'bounty', 'name': 'Bounty Desk', 'directorId': 'w1',
             'members': ['w1', 'w2']},
            {'id': 'other', 'name': 'Other Desk', 'directorId': 'z9',
             'members': ['z9']},
        ],
    }


class DoctrineBase(unittest.TestCase):
    def setUp(self):
        self.st = _state()
        for name, val in (
                ('get_state_from_db', lambda *a, **k: self.st),
                ('save_state_to_db', lambda s: None),
                ('log_action', lambda *a, **k: None),
                ('_state_begin', lambda: self.st)):
            p = mock.patch.object(serve, name, val)
            p.start()
            self.addCleanup(p.stop)

    def doctrine_for(self, team_index, process):
        return serve._team_doctrine_for(self.st['teams'][team_index], process)


class TeamDoctrine(DoctrineBase):
    MEMBER = {'X-Agent-Key': 'key-w1'}
    OUTSIDER = {'X-Agent-Key': 'key-out'}

    def test_member_defines_levels_and_reads_them_back(self):
        r = client().put('/api/team-doctrine/bounty', json={'levels': LEVELS},
                         headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertEqual('default', r.json()['process'])
        self.assertEqual('d1', r.json()['doctrine']['currentLevel'])
        r = client().get('/api/team-doctrine/bounty', headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        doctrine = r.json()['processes']['default']
        levels = doctrine['levels']
        self.assertEqual(['d1', 'd3'], [l['id'] for l in levels])
        self.assertEqual('live exploit window', levels[0]['trigger'])

    def test_outsider_cannot_define_or_read(self):
        r = client().put('/api/team-doctrine/bounty', json={'levels': LEVELS},
                         headers=self.OUTSIDER)
        self.assertEqual(401, r.status_code)
        r = client().get('/api/team-doctrine/bounty', headers=self.OUTSIDER)
        self.assertEqual(401, r.status_code)
        r = client().get('/api/team-doctrine/other', headers=self.MEMBER)
        self.assertEqual(401, r.status_code)

    def test_operator_can_define(self):
        # The deny-by-default middleware wants a session or agent key before
        # the handler runs; the operator path here is the session. Bypass it
        # the same way every suite does (setUpModule-style module patch would
        # leak into the agent-key tests).
        with mock.patch.object(serve, '_valid_agent_key_presented',
                               return_value=True):
            with mock.patch.object(serve, '_operator_gate',
                                   return_value=(True, 'player')):
                r = client().put('/api/team-doctrine/bounty',
                                 json={'levels': LEVELS})
        self.assertEqual(200, r.status_code)

    def test_validation_rejects_bad_declarations(self):
        for bad in ({'levels': []}, {'levels': [{'id': 'x'}]},
                    {'levels': [{'id': 'x', 'name': 'A'}, {'id': 'x', 'name': 'B'}]},
                    {'levels': [{'id': str(i), 'name': 'L'} for i in range(9)]}):
            r = client().put('/api/team-doctrine/bounty', json=bad,
                             headers=self.MEMBER)
            self.assertEqual(400, r.status_code)

    def test_unknown_team_and_unknown_level(self):
        r = client().put('/api/team-doctrine/nope', json={'levels': LEVELS},
                         headers=self.MEMBER)
        self.assertEqual(404, r.status_code)
        client().put('/api/team-doctrine/bounty', json={'levels': LEVELS},
                     headers=self.MEMBER)
        r = client().post('/api/team-doctrine/bounty/level',
                          json={'level': 'DEFCON 7'}, headers=self.MEMBER)
        self.assertEqual(400, r.status_code)

    def test_level_change_by_name_notifies_and_logs_history(self):
        client().put('/api/team-doctrine/bounty', json={'levels': LEVELS},
                     headers=self.MEMBER)
        # d3 first (notify=false, silent), then DEFCON 1 (notify=true).
        client().post('/api/team-doctrine/bounty/level',
                      json={'level': 'd3', 'reason': 'patrol'},
                      headers=self.MEMBER)
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               return_value=True) as tg:
            r = client().post('/api/team-doctrine/bounty/level',
                              json={'level': 'DEFCON 1',
                                    'reason': 'live exploit window'},
                              headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertEqual('d1', r.json()['currentLevel'])
        self.assertEqual(1, tg.call_count)
        doctrine = self.doctrine_for(0, 'default')
        self.assertEqual(2, len(doctrine['history']))
        self.assertEqual('DEFCON 1', doctrine['history'][-1]['name'])
        self.assertEqual('live exploit window', doctrine['history'][-1]['reason'])

    def test_non_notifying_level_stays_silent(self):
        client().put('/api/team-doctrine/bounty', json={'levels': LEVELS},
                     headers=self.MEMBER)
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               return_value=True) as tg:
            r = client().post('/api/team-doctrine/bounty/level',
                              json={'level': 'd3', 'reason': 'patrol'},
                              headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertEqual(0, tg.call_count)

    def test_unchanged_level_is_a_noop(self):
        client().put('/api/team-doctrine/bounty', json={'levels': LEVELS},
                     headers=self.MEMBER)
        r = client().post('/api/team-doctrine/bounty/level',
                          json={'level': 'd1'}, headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.json()['unchanged'])
        self.assertEqual([], self.doctrine_for(0, 'default')['history'])

    def test_team_status_text_shows_activation(self):
        self.st['teams'][0]['doctrines'] = {
            'default': {'levels': LEVELS, 'currentLevel': 'd1',
                        'history': []}}
        text = serve._team_status_text()
        self.assertIn('ACTIVATION LEVELS', text)
        self.assertIn('Bounty Desk: DEFCON 1', text)
        self.assertIn('no external posts', text)


class DoctrineTemplates(DoctrineBase):
    MEMBER = {'X-Agent-Key': 'key-w1'}

    def test_templates_list_and_adopt(self):
        r = client().get('/api/doctrine-templates', headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        ids = [t['id'] for t in r.json()['templates']]
        self.assertIn('bug-bounty', ids)
        r = client().post('/api/team-doctrine/bounty/adopt',
                          json={'template': 'bug-bounty'}, headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        # The adopted process is named after the template by default.
        self.assertEqual('bug-bounty', r.json()['process'])
        doctrine = self.doctrine_for(0, 'bug-bounty')
        self.assertEqual('template:bug-bounty', doctrine['origin'])
        self.assertEqual('d5', doctrine['currentLevel'])  # template's first level
        self.assertEqual(5, len(doctrine['levels']))

    def test_adopted_copy_is_the_teams_own_and_editable(self):
        client().post('/api/team-doctrine/bounty/adopt',
                      json={'template': 'trading-desk'}, headers=self.MEMBER)
        # Team edits its copy: does not touch the template file.
        r = client().put('/api/team-doctrine/bounty',
                         json={'process': 'trading-desk',
                               'levels': [{'id': 'x1', 'name': 'OWN RULE',
                                           'notify': True}]},
                         headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        doctrine = self.doctrine_for(0, 'trading-desk')
        self.assertEqual(['x1'], [l['id'] for l in doctrine['levels']])
        with open(serve.DOCTRINE_TEMPLATES_PATH, encoding='utf-8') as fh:
            templates = json.load(fh)
        self.assertIn('s5', [l['id'] for l in
                             templates['templates']['trading-desk']['levels']])

    def test_same_template_twice_under_two_processes(self):
        r1 = client().post('/api/team-doctrine/bounty/adopt',
                           json={'template': 'trading-desk',
                                 'process': 'personal-book'},
                           headers=self.MEMBER)
        r2 = client().post('/api/team-doctrine/bounty/adopt',
                           json={'template': 'trading-desk',
                                 'process': 'fund-book'},
                           headers=self.MEMBER)
        self.assertEqual(200, r1.status_code)
        self.assertEqual(200, r2.status_code)
        r = client().post('/api/team-doctrine/bounty/level',
                          json={'process': 'fund-book', 'level': 's3',
                                'reason': 'drawdown 6%'},
                          headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertEqual('s5',
                         self.doctrine_for(0, 'personal-book')['currentLevel'])
        self.assertEqual('s3',
                         self.doctrine_for(0, 'fund-book')['currentLevel'])

    def test_get_without_process_returns_every_process(self):
        client().post('/api/team-doctrine/bounty/adopt',
                      json={'template': 'bug-bounty'}, headers=self.MEMBER)
        client().post('/api/team-doctrine/bounty/adopt',
                      json={'template': 'on-call'}, headers=self.MEMBER)
        r = client().get('/api/team-doctrine/bounty', headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertEqual({'bug-bounty', 'on-call'},
                         set(r.json()['processes'].keys()))
        r = client().get('/api/team-doctrine/bounty?process=on-call',
                         headers=self.MEMBER)
        self.assertEqual('template:on-call', r.json()['doctrine']['origin'])

    def test_level_processes_are_independent(self):
        client().post('/api/team-doctrine/bounty/adopt',
                      json={'template': 'bug-bounty'}, headers=self.MEMBER)
        client().post('/api/team-doctrine/bounty/adopt',
                      json={'template': 'on-call'}, headers=self.MEMBER)
        r = client().post('/api/team-doctrine/bounty/level',
                          json={'process': 'bug-bounty', 'level': 'd3',
                                'reason': 'finding confirmed'},
                          headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertEqual('c5',
                         self.doctrine_for(0, 'on-call')['currentLevel'])
        self.assertEqual('d3',
                         self.doctrine_for(0, 'bug-bounty')['currentLevel'])

    def test_delete_retires_one_process_or_all(self):
        client().post('/api/team-doctrine/bounty/adopt',
                      json={'template': 'bug-bounty'}, headers=self.MEMBER)
        client().post('/api/team-doctrine/bounty/adopt',
                      json={'template': 'on-call'}, headers=self.MEMBER)
        r = client().delete('/api/team-doctrine/bounty?process=on-call',
                            headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertEqual({'bug-bounty'},
                         set(serve._team_doctrine_all(
                             self.st['teams'][0]).keys()))
        r = client().delete('/api/team-doctrine/bounty', headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        self.assertEqual({}, serve._team_doctrine_all(self.st['teams'][0]))

    def test_process_cap(self):
        for i in range(serve._DOCTRINE_MAX_PROCESSES):
            r = client().put('/api/team-doctrine/bounty',
                             json={'process': f'p{i}',
                                   'levels': [{'id': 'a', 'name': 'A'}]},
                             headers=self.MEMBER)
            self.assertEqual(200, r.status_code)
        r = client().put('/api/team-doctrine/bounty',
                         json={'process': 'overflow',
                               'levels': [{'id': 'a', 'name': 'A'}]},
                         headers=self.MEMBER)
        self.assertEqual(409, r.status_code)

    def test_readopt_resets_to_template(self):
        client().post('/api/team-doctrine/bounty/adopt',
                      json={'template': 'on-call'}, headers=self.MEMBER)
        client().put('/api/team-doctrine/bounty',
                     json={'process': 'on-call',
                           'levels': [{'id': 'mine', 'name': 'Mine'}]},
                     headers=self.MEMBER)
        r = client().post('/api/team-doctrine/bounty/adopt',
                          json={'template': 'on-call'}, headers=self.MEMBER)
        self.assertEqual(200, r.status_code)
        doctrine = self.doctrine_for(0, 'on-call')
        self.assertEqual('c5', doctrine['currentLevel'])
        self.assertEqual([], doctrine['history'])

    def test_unknown_template_400_with_known_list(self):
        r = client().post('/api/team-doctrine/bounty/adopt',
                          json={'template': 'nope'}, headers=self.MEMBER)
        self.assertEqual(400, r.status_code)
        self.assertIn('available:', r.json()['error'])

    def test_outsider_cannot_adopt(self):
        r = client().post('/api/team-doctrine/bounty/adopt',
                          json={'template': 'bug-bounty'},
                          headers={'X-Agent-Key': 'key-out'})
        self.assertEqual(401, r.status_code)


class DoctrineNoAuth(DoctrineBase):
    def test_requires_any_key(self):
        r = client().put('/api/team-doctrine/bounty', json={'levels': LEVELS})
        self.assertEqual(401, r.status_code)
