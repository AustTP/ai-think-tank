# Team-roster endpoint tests (operator, 2026-11-23): identity comes from
# state['agents'] (name/role/color/profile.mission), team via teams[].members,
# avatar availability from agents/<id>/avatars on disk. Hermetic: no DB, no
# network, agent dirs under a temp AGENTS_DIR.
import os
import sys
import unittest
import unittest.mock as mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import serve  # noqa: E402


def _mk_agent(aid, name, role, color, mission, team=None):
    a = {'id': aid, 'name': name, 'role': role, 'color': color,
         'busy': False, 'profile': {'mission': mission}}
    return a


STATE = {
    'agents': {
        'ada': _mk_agent('ada', 'Ada', 'Scrum Master', '#a4c2f4',
                         'Run the ada team board.'),
        'no_team': _mk_agent('no_team', 'Nemo', 'Float', '#d5a6bd', 'Drift.'),
        'bad': 'not-a-dict',
    },
    'teams': [
        {'id': 'ada', 'members': ['ada', {'id': 'ghost', 'name': 'ghosty'}]},
    ],
}


class TeamRoster(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__('tempfile').mkdtemp(prefix='roster-test-')
        self.patcher = mock.patch.multiple(
            serve,
            get_state_from_db=lambda *a, **k: STATE,
            AGENTS_DIR=self.tmp,
            verify_session=lambda sid: True,
        )
        self.patcher.start()
        self.client = TestClient(serve.app)

    def tearDown(self):
        self.patcher.stop()

    def test_roster_rows_identity_team_and_bad_entry_skipped(self):
        r = self.client.get('/api/team-roster')
        self.assertEqual(r.status_code, 200)
        rows = r.json()['agents']
        by_id = {x['id']: x for x in rows}
        self.assertEqual(set(by_id), {'ada', 'no_team'})
        ada = by_id['ada']
        self.assertEqual(ada['name'], 'Ada')
        self.assertEqual(ada['teamId'], 'ada')
        self.assertEqual(ada['color'], '#a4c2f4')
        self.assertIn('ada team', ada['mission'])
        self.assertEqual(by_id['no_team']['teamId'], '')
        self.assertFalse(any(x['hasAvatar'] for x in rows))

    def test_roster_sorted_by_team_then_name(self):
        rows = self.client.get('/api/team-roster').json()['agents']
        ids = [x['id'] for x in rows]
        self.assertEqual(ids, ['ada', 'no_team'])

    def test_has_avatar_reflects_disk(self):
        os.makedirs(os.path.join(self.tmp, 'ada', 'avatars'))
        rows = self.client.get('/api/team-roster').json()['agents']
        by_id = {x['id']: x for x in rows}
        self.assertTrue(by_id['ada']['hasAvatar'])
        self.assertEqual(by_id['ada']['avatarUrl'], '/api/avatar/ada/front.png')

    def test_agent_summary_known_and_unknown(self):
        r = self.client.get('/api/agent-summary/ada')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['name'], 'Ada')
        self.assertEqual(r.json()['avatarUrl'], '/api/avatar/ada/front.png')
        self.assertEqual(self.client.get('/api/agent-summary/ghost').status_code, 404)

    def test_roster_requires_player_session(self):
        with mock.patch.object(serve, '_require_player_session',
                               lambda request: False):
            self.assertEqual(self.client.get('/api/team-roster').status_code, 401)
            self.assertEqual(
                self.client.get('/api/agent-summary/ada').status_code, 401)


if __name__ == '__main__':
    unittest.main()
