"""Tests for Phase A: the teams data model + team-scoped hiring/promote.

These exercise serve.py's team helpers and the /api/teams endpoints against a
hermetic DB (temp dir, patched serve.DB_PATH) so they never touch the live
think_tank.db and make no network calls.
"""
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


class TeamsModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-teams-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state_with_chains(self):
        """A roster mirroring the real backfill map: faye leads the top team,
        dev (under faye) is a mid-director leading a support team, sam (under
        faye) leads a research team, and the rest report up the chain."""
        return {
            'agentRoster': [
                {'id': 'faye', 'name': 'Faye', 'role': 'Control Room', 'isAdmin': True},
                {'id': 'nora', 'name': 'Nora', 'role': 'Personnel', 'isDirector': True},
                {'id': 'dev', 'name': 'Dev', 'role': 'Studio', 'director': 'faye'},
                {'id': 'sam', 'name': 'Sam', 'role': 'Research', 'director': 'faye'},
                {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'director': 'sam'},
                {'id': 'maya', 'name': 'Maya', 'role': 'Research', 'director': 'sam'},
                {'id': 'nadya', 'name': 'Nadia', 'role': 'Support', 'director': 'dev'},
            ],
        }

    def test_backfill_spawns_teams_for_directors(self):
        state = self._state_with_chains()
        serve.save_state_to_db(state)
        serve._backfill_teams_in_db()
        teams = serve.get_state_from_db().get('teams') or []
        by_id = {t['id']: t for t in teams}
        # sam + dev are directors with reports -> teams exist.
        self.assertIn('sam', by_id)
        self.assertIn('dev', by_id)
        # sam's team = his direct reports (ada, maya), NOT dev's support line.
        self.assertEqual(sorted(by_id['sam']['members']), ['ada', 'maya'])
        self.assertEqual(sorted(by_id['dev']['members']), ['nadya'])
        # dev (a director under faye) is NOT a member of faye's team; faye's
        # team holds her non-director reports only.
        self.assertNotIn('dev', by_id['faye']['members'])
        # A flagged director with no current reports (nora) still keeps a team
        # (so a mid-restructure senior director doesn't lose theirs).
        self.assertIn('nora', by_id)
        self.assertEqual(by_id['nora']['members'], [])

    def test_backfill_is_idempotent(self):
        state = self._state_with_chains()
        serve.save_state_to_db(state)
        serve._backfill_teams_in_db()
        first = serve.get_state_from_db().get('teams')
        serve._backfill_teams_in_db()
        second = serve.get_state_from_db().get('teams')
        self.assertEqual(first, second, 'backfill must be idempotent (no clobber/re-shuffle)')

    def test_promote_spawns_new_team_and_leaves_old(self):
        state = self._state_with_chains()
        serve.save_state_to_db(state)
        serve._backfill_teams_in_db()
        # Operate on the DB state that backfill produced (the same object the
        # promote endpoint would load from /api).
        state = serve.get_state_from_db()
        # sam promotes maya -> maya becomes director of her own team.
        new_team = serve._promote_to_director(state, 'maya', 'sam')
        self.assertIsNotNone(new_team)
        self.assertEqual(new_team['directorId'], 'maya')
        teams = state.get('teams') or []
        by_id = {t['id']: t for t in teams}
        self.assertIn('maya', by_id, 'a promoted employee gets a fresh team record')
        # maya is now a director -> she is DERIVED out of sam's team (she leads
        # her own team instead), while staying under sam's reporting chain.
        self.assertEqual(serve._derive_team_members(state, 'sam'), ['ada'])
        self.assertEqual(next(d for d in state['agentRoster'] if d['id'] == 'maya')['director'], 'sam')
        # And she's the director of her own (currently empty) team.
        self.assertEqual(serve._derive_team_members(state, 'maya'), [])

    def test_can_write_team_acls(self):
        state = self._state_with_chains()
        serve.save_state_to_db(state)
        serve._backfill_teams_in_db()
        state = serve.get_state_from_db()
        # sam's team members may write sam's shared space.
        self.assertTrue(serve._can_write_team(state, 'ada', 'sam'))
        self.assertTrue(serve._can_write_team(state, 'sam', 'sam'))  # director
        # A different team's member (dev's support) may NOT write sam's space.
        self.assertFalse(serve._can_write_team(state, 'nadya', 'sam'))
        # The admin (faye) may write anything.
        self.assertTrue(serve._can_write_team(state, 'faye', 'dev'))
        # A chain-above director (faye over sam) may write.
        self.assertTrue(serve._can_write_team(state, 'faye', 'dev'))


if __name__ == '__main__':
    unittest.main()