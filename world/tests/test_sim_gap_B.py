"""Coverage-push tests for world/sim.py residual branches: the outskirts-wake /
room-door spawn cluster, the team-scoped auto-hire + borrow machinery, the
new-team spawn path, and the onboarding ceremony guards.

Uses the temp-DB isolation pattern from test_sim.py / test_sim_gap.py so the
real think_tank.db is never touched; serve network/DB seams are mocked at the
module attribute.
"""
import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-simgapB-')
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
        sim._content_results.clear()

    @staticmethod
    def _free_grid(cols=8, rows=8):
        return {'cols': cols, 'rows': rows, 'cell': 8,
                'grid': [[0] * cols for _ in range(rows)]}


class SpawnWakeCluster(SimIsolation):
    def test_appear_from_outskirts_missing_agent(self):
        sim.appear_from_outskirts({'agents': {}}, 'ghost')

    def test_appear_from_outskirts_geometry_load_failure(self):
        state = {'agents': {'ada': {'id': 'ada'}}}
        with unittest.mock.patch.object(
                sim, '_load_outdoor_geometry', side_effect=RuntimeError('boom')):
            sim.appear_from_outskirts(state, 'ada')
        self.assertFalse(state['agents']['ada']['offDuty'])
        self.assertTrue(state['agents']['ada']['visible'])

    def test_spawn_at_room_door_missing_agent(self):
        sim._spawn_at_room_door({'agents': {}}, 'ghost', 'observatory', {}, None, {})

    def test_spawn_at_room_door_no_door_falls_back(self):
        state = {'agents': {'ada': {'id': 'ada'}}}
        with unittest.mock.patch.object(sim, 'appear_from_outskirts') as fallback:
            sim._spawn_at_room_door(state, 'ada', 'ghostroom', {}, None, {})
        fallback.assert_called_once_with(state, 'ada', {})


class BorrowAndAutoHire(SimIsolation):
    def _hire_state(self):
        return {
            'agentRoster': [
                {'id': 'admin', 'name': 'Admin', 'isAdmin': True},
                {'id': 'd1', 'name': 'Dana'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {
                'admin': {'id': 'admin', 'name': 'Admin'},
                'd1': {'id': 'd1', 'name': 'Dana', 'busy': False, 'offDuty': False},
                'm1': {'id': 'm1', 'name': 'Mia', 'busy': False, 'offDuty': False},
            },
        }

    def test_borrow_skips_loaned_agent(self):
        state = {'agents': {'ben': {'id': 'ben', 'offDuty': True}},
                 'agentRoster': [{'id': 'ben', 'name': 'Ben', 'director': 'd1',
                                  'loan': {'teamId': 'x'}}]}
        self.assertIsNone(sim._borrow_inactive_agent_for_team(state, 'd2'))

    def test_borrow_skips_agent_with_no_home_director(self):
        state = {'agents': {'ben': {'id': 'ben', 'offDuty': True}},
                 'agentRoster': [{'id': 'ben', 'name': 'Ben'}]}
        self.assertIsNone(sim._borrow_inactive_agent_for_team(state, 'd2'))

    def test_start_auto_hire_skips_director_with_no_live_members(self):
        state = {
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana'},
                {'id': 'ghost', 'name': 'Ghost', 'director': 'd1'},
            ],
            'agents': {'d1': {'id': 'd1', 'name': 'Dana',
                              'busy': False, 'offDuty': False}},
        }
        ok = sim._start_auto_hire(state, now_ms=1_000_000, grid=self._free_grid(),
                                  decider=lambda s, i, c: None)
        self.assertFalse(ok)
        self.assertEqual(state['lastHireAt'], 0)

    def test_start_auto_hire_no_pick_after_fallback(self):
        state = self._hire_state()

        def decider(s, instructions, candidates):
            s['agents'].pop('m1', None)
            return None

        ok = sim._start_auto_hire(state, now_ms=1_000_000, grid=self._free_grid(),
                                  decider=decider)
        self.assertFalse(ok)
        self.assertEqual(state['lastHireAt'], 0)

    def test_start_auto_hire_director_busy_at_pick(self):
        state = self._hire_state()

        def decider(s, instructions, candidates):
            s['agents']['d1']['busy'] = True
            return 'm1'

        ok = sim._start_auto_hire(state, now_ms=1_000_000, grid=self._free_grid(),
                                  decider=decider)
        self.assertFalse(ok)
        self.assertEqual(state['lastHireAt'], 0)

    def test_complete_auto_hire_no_name_available(self):
        state = {
            'agentRoster': [{'id': 'd1', 'name': 'Dana'}],
            'agents': {'d1': {'id': 'd1', 'name': 'Dana'}},
            '_usedNames': ['maya', 'leo', 'zara', 'owen', 'lyra', 'ida', 'vela'],
        }
        pending = {
            'adminId': 'd1', 'adminName': 'Dana', 'directorId': 'd1',
            'helpForId': 'm1', 'helpForName': 'Mia', 'at': 1_000_000,
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value=None):
            new_id = sim._complete_auto_hire(state, pending, grid=self._free_grid(),
                                             now_ms=1_000_000)
        self.assertIsNone(new_id)
        self.assertNotIn('_pendingHire', state)

    def test_complete_auto_hire_team_at_cap(self):
        state = {
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana'},
                *[{'id': f'm{i}', 'name': f'M{i}', 'director': 'd1'} for i in range(6)],
            ],
            'agents': {'d1': {'id': 'd1', 'name': 'Dana'}},
        }
        pending = {
            'adminId': 'd1', 'adminName': 'Dana', 'directorId': 'd1',
            'helpForId': 'm0', 'helpForName': 'M0', 'at': 1_000_000,
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value='maya'):
            new_id = sim._complete_auto_hire(state, pending, grid=self._free_grid(),
                                             now_ms=1_000_000)
        self.assertIsNone(new_id)
        self.assertNotIn('_pendingHire', state)

    def test_complete_auto_hire_appends_external_helpfor(self):
        state = {
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
                {'id': 'x1', 'name': 'Xia', 'director': 'd2'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'name': 'Dana'},
                'm1': {'id': 'm1', 'name': 'Mia'},
                'x1': {'id': 'x1', 'name': 'Xia'},
            },
        }
        pending = {
            'adminId': 'd1', 'adminName': 'Dana', 'directorId': 'd1',
            'helpForId': 'x1', 'helpForName': 'Xia', 'at': 1_000_000,
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value='maya'), \
             unittest.mock.patch.object(sim, '_free_outdoor_spot',
                                        return_value={'x': 10, 'y': 10}):
            new_id = sim._complete_auto_hire(state, pending, grid=self._free_grid(),
                                             now_ms=1_000_000)
        self.assertEqual(new_id, 'maya')
        self.assertIn('maya', state['agents'])
        self.assertIn('x1', state['_pendingOnboard']['coworkerIds'])

    def test_next_hire_name_pool_exhausted(self):
        state = {'_usedNames': ['maya', 'leo', 'zara', 'owen', 'lyra', 'ida', 'vela']}
        self.assertIsNone(sim._next_hire_name(state))

    def test_remember_name_whitespace_only(self):
        state = {}
        sim._remember_name(state, '   ')
        self.assertNotIn('_usedNames', state)


class NewTeamSpawn(SimIsolation):
    def test_spawn_team_at_cap(self):
        state = {'agentRoster': [{'id': f'a{i}', 'name': f'A{i}'}
                                 for i in range(sim.MAX_TOTAL_AGENTS)]}
        self.assertIsNone(sim.spawn_new_team_for_request(state, 'goal', now_ms=1_000_000))

    def test_spawn_team_no_admin(self):
        state = {'agentRoster': [{'id': 'd1', 'name': 'Dana'}]}
        self.assertIsNone(sim.spawn_new_team_for_request(state, 'goal', now_ms=1_000_000,
                                                         admin_id=None))

    def test_spawn_team_no_director_name(self):
        state = {
            'agentRoster': [{'id': 'admin', 'name': 'Admin', 'isAdmin': True}],
            '_usedNames': list(sim._NEW_TEAM_NAME_POOL),
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value=None):
            self.assertIsNone(sim.spawn_new_team_for_request(state, 'goal', now_ms=1_000_000))

    def test_spawn_team_employee_pool_exhausted(self):
        state = {
            'agentRoster': [{'id': 'admin', 'name': 'Admin', 'isAdmin': True}],
            '_usedNames': list(sim._NEW_TEAM_NAME_POOL),
        }

        def chooser(s, name, used, role):
            return 'zed' if role == 'Director' else None

        with unittest.mock.patch.object(sim, '_load_outdoor_geometry',
                                        return_value=(self._free_grid(48, 32), {})), \
             unittest.mock.patch('serve.log_action'):
            team = sim.spawn_new_team_for_request(state, 'goal', now_ms=1_000_000,
                                                  chooser=chooser)
        self.assertIsNotNone(team)
        self.assertEqual(team['members'], [])


class OnboardCeremonyB(SimIsolation):
    def test_onboard_coworker_defs_skips_missing(self):
        state = {
            'agents': {'m1': {'id': 'm1', 'name': 'Mia'}},
            'agentRoster': [{'id': 'm1', 'name': 'Mia'}, {'id': 'ghost', 'name': 'Ghost'}],
        }
        onboard = {'coworkerIds': ['ghost', 'm1']}
        out = sim._onboard_coworker_defs(state, onboard)
        self.assertEqual([c['id'] for c in out], ['m1'])

    def test_start_onboard_meeting_director_unavailable(self):
        state = {'agents': {'d1': {'id': 'd1', 'busy': True, 'offDuty': False}},
                 'agentRoster': []}
        onboard = {'directorId': 'd1', 'agentId': 'maya', 'coworkerIds': []}
        self.assertFalse(sim._start_onboard_meeting(state, onboard, now_ms=1_000_000))

        vanished = {'agents': {}, 'agentRoster': []}
        ghost = {'directorId': 'ghost', 'agentId': 'maya', 'coworkerIds': []}
        self.assertFalse(sim._start_onboard_meeting(vanished, ghost, now_ms=1_000_000))

    def test_onboard_hold_claimed_task_no_agent(self):
        state = {'agents': {}, 'tasks': {}}
        onboard = {'agentId': 'maya'}
        self.assertFalse(sim._onboard_hold_claimed_task(state, onboard))

    def test_onboard_hold_claimed_task_handoff(self):
        state = {
            'agents': {'maya': {'id': 'maya', 'task': 't1', 'handoff': {'to': 'm1'}}},
            'tasks': {'t1': {'id': 't1', 'status': 'walking'}},
        }
        onboard = {'agentId': 'maya'}
        self.assertFalse(sim._onboard_hold_claimed_task(state, onboard))

    def test_work_agreement_text_non_dict(self):
        self.assertIsNone(sim._work_agreement_text({}, 'not-a-dict'))

    def test_work_agreement_text_no_parts(self):
        agent = {'id': 'maya', 'role': '', 'profile': {}, 'accessGrant': ''}
        self.assertIsNone(sim._work_agreement_text({}, agent))

    def test_empower_work_agreement_no_agent(self):
        state = {'agents': {}}
        onboard = {'agentId': 'maya'}
        self.assertEqual(sim._empower_work_agreement(state, onboard, 1_000_000), (None, None))

    def test_empower_work_agreement_no_text(self):
        state = {'agents': {'maya': {'id': 'maya', 'role': '', 'profile': {},
                                     'accessGrant': ''}}}
        onboard = {'agentId': 'maya'}
        self.assertEqual(sim._empower_work_agreement(state, onboard, 1_000_000), (None, None))

    def test_resolve_onboard_meeting_vanished_hire(self):
        state = {
            'agents': {'d1': {'id': 'd1', 'busy': True, 'visible': False,
                              'inRoom': 'commandcenter'}},
            'agentRoster': [{'id': 'd1', 'name': 'Dana'}],
            '_pendingOnboard': {'agentId': 'maya', 'directorId': 'd1', 'coworkerIds': []},
        }
        onboard = state['_pendingOnboard']
        sim._resolve_onboard_meeting(state, onboard, now_ms=1_000_000)
        self.assertNotIn('_pendingOnboard', state)


if __name__ == '__main__':
    unittest.main()