"""Coverage-push tests for world/sim.py ceremony machinery: the refinement,
breakdown, and retrospective ceremony branches -- scrum-master/team lookups,
attendee guards, kick/cadence edge cases, decider outage fallbacks, and the
per-ceremony step convene/defer paths. Uses the temp-DB isolation pattern from
test_sim_gap.py so the real think_tank.db is never touched; serve network/DB
seams are mocked at the module attribute.
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
        self.tmp = tempfile.mkdtemp(prefix='think-tank-simgapE-')
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

    def _team_state(self):
        """A minimal two-worker team: director d1, scrum master sm1, worker m1."""
        return {
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': False, 'offDuty': False},
                'sm1': {'id': 'sm1', 'busy': False, 'offDuty': False},
                'm1': {'id': 'm1', 'busy': False, 'offDuty': False},
            },
            'tasks': {},
            'workQueue': [],
            'sprints': {},
        }


class RefinementCeremony(SimIsolation):
    def test_refinement_team_unknown_filer_returns_none(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}],
                 'agentRoster': [{'id': 'd1', 'name': 'D', 'director': None}]}
        self.assertIsNone(sim._refinement_team(state, {'filedBy': 'ghost'}))
        self.assertIsNone(sim._refinement_team(state, {'teamId': 'ghost', 'filedBy': 'ghost'}))

    def test_refinement_scrum_master_unknown_team(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}]}
        self.assertIsNone(sim._refinement_scrum_master_for_team(state, 'nope'))

    def test_refinement_attendees_busy_scrum_master(self):
        state = {'agents': {'sm1': {'id': 'sm1', 'busy': True}}}
        self.assertIsNone(sim._refinement_attendees(state, ['r1'], 'sm1'))

    def test_refinement_attendees_missing_scrum_master(self):
        state = {'agents': {}}
        self.assertIsNone(sim._refinement_attendees(state, ['r1'], 'sm1'))

    def test_start_refinement_defers_when_attendees_unhealthy(self):
        state = {'agents': {'sm1': {'id': 'sm1', 'busy': True}}}
        self.assertFalse(sim._start_refinement(state, 't1', ['r1'], 'sm1', now_ms=1_000_000))

    def test_restore_refinement_agent_missing(self):
        sim._restore_refinement_agent({'agents': {}}, 'ghost', {}, 1_000_000, 10_000)

    def test_reassign_rolled_over_skips_foreign_sprint(self):
        state = self._team_state()
        state['sprints'] = {'s1': {'id': 's1', 'status': 'closed', 'teamIds': ['t1']}}
        state['workQueue'] = [
            {'title': 'foreign', 'room': 'observatory', 'sprintId': 's9'},
            {'title': 'carried', 'room': 'observatory', 'sprintId': 's1'},
        ]
        count = sim._reassign_rolled_over_cards(state, 't1')
        self.assertEqual(count, 1)
        self.assertEqual(state['workQueue'][1]['_reassignedTo'], 'sm1')

    def test_reassign_rolled_over_skips_done_card(self):
        state = self._team_state()
        state['sprints'] = {'s1': {'id': 's1', 'status': 'closed', 'teamIds': ['t1']}}
        state['tasks'] = {'t1': {'id': 't1', 'title': 'carried', 'room': 'observatory',
                                 'status': 'done'}}
        state['workQueue'] = [{'title': 'carried', 'room': 'observatory', 'sprintId': 's1'}]
        self.assertEqual(sim._reassign_rolled_over_cards(state, 't1'), 0)

    def test_reassign_rolled_over_no_pickable_cards(self):
        state = self._team_state()
        state['sprints'] = {'s1': {'id': 's1', 'status': 'closed', 'teamIds': ['t1']}}
        state['workQueue'] = [{'title': 'foreign', 'room': 'observatory', 'sprintId': 's9'}]
        self.assertEqual(sim._reassign_rolled_over_cards(state, 't1'), 0)

    def test_reassign_rolled_over_no_free_member(self):
        state = self._team_state()
        state['sprints'] = {'s1': {'id': 's1', 'status': 'closed', 'teamIds': ['t1']}}
        state['workQueue'] = [{'title': 'carried', 'room': 'observatory', 'sprintId': 's1'}]
        state['agents']['sm1']['busy'] = True
        state['agents']['m1']['offDuty'] = True
        self.assertEqual(sim._reassign_rolled_over_cards(state, 't1'), 0)

    def test_resolve_refinement_skips_and_depends_and_rollover(self):
        pending = {'scrumMasterId': 'sm1', 'teamId': 't1',
                   'reqIds': ['r1', 'r2'], 'people': {}}
        state = {
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': False, 'offDuty': False},
                'sm1': {'id': 'sm1', 'name': 'Sam', 'busy': False, 'offDuty': False},
                'm1': {'id': 'm1', 'busy': False, 'offDuty': False},
            },
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'backlogRequests': [
                {'id': 'r1', 'status': 'pending', 'filedBy': 'm1', 'title': 'Story A',
                 'room': 'observatory', 'reason': 'gap', 'issueKey': 'ISS-1'},
                {'id': 'r2', 'status': 'accepted', 'filedBy': 'm1', 'title': 'Story B',
                 'room': 'observatory', 'reason': 'gap'},
            ],
            'issues': {'ISS-1': {'dependsOnTask': 'task-9'}},
            'sprints': {'s1': {'id': 's1', 'status': 'closed', 'teamIds': ['t1']}},
            'tasks': {},
            'workQueue': [{'title': 'carried', 'room': 'observatory', 'sprintId': 's1'}],
        }
        decider = lambda instructions, criteria: 'accept'
        sim._resolve_refinement(state, pending, 1_000_000, decider=decider)
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['status'], 'accepted')
        self.assertEqual(state['workQueue'][0]['_reassignedTo'], 'sm1')
        self.assertEqual(state['workQueue'][1]['dependsOn'], 'task-9')
        self.assertNotIn('t1', state['pendingRefinements'])

    def test_refinement_cadence_due_far_future_sentinel(self):
        now_ms = 1_000_000_000
        state = {'teamRefinementAt': {'t1': now_ms + 10 * 365 * 24 * 3600 * 1000 + 1000}}
        self.assertTrue(sim._refinement_cadence_due_for(state, 't1', now_ms))

    def test_kick_refinement_no_team(self):
        self.assertFalse(sim.kick_refinement_now({}, None, now_ms=1))
        self.assertFalse(sim.kick_refinement_now({}, '', now_ms=1))

    def test_kick_refinement_skips_inflight_ceremony(self):
        state = {'pendingRefinements': {'t1': {'embarked': True}}}
        self.assertFalse(sim.kick_refinement_now(state, 't1', now_ms=1_000_000))

    def test_kick_refinement_arms(self):
        state = {}
        self.assertTrue(sim.kick_refinement_now(state, 't1', now_ms=1_000_000))
        self.assertEqual(state['teamRefinementAt']['t1'], 0)

    def test_refinement_step_defers_without_facilitator(self):
        state = {
            'teams': [
                {'id': 't1', 'directorId': 'd1'},
                {'id': 't2', 'directorId': 'd2'},
            ],
            'agentRoster': [
                {'id': 'd1', 'name': 'D1', 'director': None},
                {'id': 'd2', 'name': 'D2', 'director': None},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
                {'id': 'm2', 'name': 'Mox', 'director': 'd2'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': True, 'offDuty': False},
                'd2': {'id': 'd2', 'busy': True, 'offDuty': False},
            },
            'backlogRequests': [
                {'id': 'r1', 'status': 'pending', 'filedBy': 'm1', 'title': 'A',
                 'room': 'observatory'},
                {'id': 'r2', 'status': 'pending', 'filedBy': 'm2', 'title': 'B',
                 'room': 'observatory'},
            ],
            'pendingRefinements': {'t1': {'embarked': False, 'reqIds': ['r1'],
                                          'teamId': 't1'}},
            'pendingSprintRetros': [],
            'sprints': {},
        }
        sim._refinement_step(state, now=1.0, now_ms=1_000_000)
        self.assertIn('t1', state['pendingRefinements'])
        self.assertNotIn('t2', state['pendingRefinements'])


class BreakdownCeremony(SimIsolation):
    def test_breakdown_attendees_busy_member(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'w1', 'name': 'Wren', 'director': 'd1'},
            ],
            'agents': {
                'sm1': {'id': 'sm1', 'busy': False},
                'w1': {'id': 'w1', 'busy': True},
            },
        }
        self.assertIsNone(sim._breakdown_attendees(state, ['r1'], 'sm1', 't1'))

    def test_breakdown_attendees_missing_member(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'w1', 'name': 'Wren', 'director': 'd1'},
            ],
            'agents': {'sm1': {'id': 'sm1', 'busy': False}},
        }
        self.assertIsNone(sim._breakdown_attendees(state, ['r1'], 'sm1', 't1'))

    def test_start_breakdown_defers_when_attendees_unhealthy(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
            ],
            'agents': {'sm1': {'id': 'sm1', 'busy': True}},
        }
        self.assertFalse(sim._start_breakdown(state, 't1', ['r1'], 'sm1', now_ms=1_000_000))

    def test_breakdown_decider_default_import_outage(self):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'serve':
                raise ImportError('nope')
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch('builtins.__import__', side_effect=fake_import):
            self.assertIsNone(sim._breakdown_decider_default({}, 'instructions', 'goal'))

    def test_resolve_breakdown_skips_foreign_and_resolved(self):
        pending = {'scrumMasterId': 'sm1', 'teamId': 't1',
                   'reqIds': ['r1', 'r2'], 'people': {}}
        state = {
            'agentRoster': [{'id': 'sm1', 'name': 'Sam'}],
            'agents': {'sm1': {'id': 'sm1'}},
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'backlogRequests': [
                {'id': 'r0', 'status': 'pending', 'filedBy': 'm1', 'title': 'X',
                 'goal': 'X'},
                {'id': 'r1', 'status': 'pending', 'filedBy': 'm1', 'title': 'Y',
                 'goal': 'Y'},
                {'id': 'r2', 'status': 'accepted', 'filedBy': 'm1', 'title': 'Z',
                 'goal': 'Z'},
            ],
            'workQueue': [],
        }
        decider = lambda s, instructions, goal: {'items': [{'title': 'Card A',
                                                            'type': 'story'}]}
        sim._resolve_breakdown(state, pending, 1_000_000, decider=decider)
        self.assertEqual(state['backlogRequests'][1]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['brokenDown'], True)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertNotIn('t1', state['pendingBreakdowns'])

    def test_breakdown_step_defers_unembarked_without_facilitator(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agentRoster': [{'id': 'd1', 'name': 'D', 'director': None}],
            'agents': {'d1': {'id': 'd1', 'busy': True, 'offDuty': False}},
            'backlogRequests': [],
            'pendingBreakdowns': {'t1': {'embarked': False, 'reqIds': ['r1'],
                                         'teamId': 't1'}},
        }
        sim._breakdown_step(state, now=1.0, now_ms=1_000_000)
        self.assertIn('t1', state['pendingBreakdowns'])
        self.assertFalse(state['pendingBreakdowns']['t1']['embarked'])

    def test_breakdown_step_embarks_unembarked_ceremony(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1', 'scrumMasterId': 'sm1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'w1', 'name': 'Wren', 'director': 'd1'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': False, 'offDuty': False},
                'sm1': {'id': 'sm1', 'busy': False, 'offDuty': False},
                'w1': {'id': 'w1', 'busy': False, 'offDuty': False},
            },
            'backlogRequests': [{'id': 'r1', 'status': 'pending', 'filedBy': 'w1',
                                 'title': 'Big', 'goal': 'Big', 'teamId': 't1'}],
            'pendingBreakdowns': {'t1': {'embarked': False, 'reqIds': ['r1'],
                                         'teamId': 't1'}},
        }
        sim._breakdown_step(state, now=1.0, now_ms=1_000_000)
        self.assertTrue(state['pendingBreakdowns']['t1']['embarked'])
        self.assertTrue(state['agents']['sm1']['busy'])
        self.assertEqual(state['agents']['sm1']['inRoom'], 'commandcenter')


class RetrospectiveCeremony(SimIsolation):
    def test_retro_scrum_master_team_without_director(self):
        state = {'teams': [{'id': 't1'}], 'agents': {}}
        self.assertIsNone(sim._retro_scrum_master(state, ['t1']))

    def test_retro_attendees_skips_unknown_team_and_busy_member(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'w1', 'name': 'Wren', 'director': 'd1'},
            ],
            'agents': {
                'sm1': {'id': 'sm1', 'busy': False},
                'w1': {'id': 'w1', 'busy': True},
            },
        }
        self.assertIsNone(sim._retro_attendees(state, ['ghost', 't1'], 'sm1'))

    def test_retro_attendees_missing_member(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'w1', 'name': 'Wren', 'director': 'd1'},
            ],
            'agents': {'sm1': {'id': 'sm1', 'busy': False}},
        }
        self.assertIsNone(sim._retro_attendees(state, ['t1'], 'sm1'))

    def test_start_retrospective_defers_when_attendees_unhealthy(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
            ],
            'agents': {'sm1': {'id': 'sm1', 'busy': True}},
        }
        self.assertFalse(sim._start_retrospective(state, 's1', ['t1'], 'sm1', now_ms=1_000_000))

    def test_retro_decider_default_import_outage(self):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'serve':
                raise ImportError('nope')
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch('builtins.__import__', side_effect=fake_import):
            self.assertEqual(sim._retro_decider_default({}, 'instructions', 's1', []), {})

    def test_retro_step_embarks_unembarked_retro(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1', 'scrumMasterId': 'sm1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'w1', 'name': 'Wren', 'director': 'd1'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': False, 'offDuty': False},
                'sm1': {'id': 'sm1', 'busy': False, 'offDuty': False},
                'w1': {'id': 'w1', 'busy': False, 'offDuty': False},
            },
            'pendingRetrospectives': {'s1': {'embarked': False, 'teamIds': ['t1'],
                                             'scrumMasterId': 'sm1'}},
            'pendingSprintRetros': [],
        }
        sim._retro_step(state, now=1.0, now_ms=1_000_000)
        self.assertTrue(state['pendingRetrospectives']['s1']['embarked'])
        self.assertTrue(state['agents']['sm1']['busy'])

    def test_retro_step_skips_already_inflight_retro(self):
        state = {
            'teams': [{'id': 't1', 'directorId': 'd1', 'scrumMasterId': 'sm1'}],
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'w1', 'name': 'Wren', 'director': 'd1'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': False, 'offDuty': False},
                'sm1': {'id': 'sm1', 'busy': True, 'offDuty': False},
                'w1': {'id': 'w1', 'busy': False, 'offDuty': False},
            },
            'sprints': {'s1': {'id': 's1', 'teamIds': ['t1']}},
            'pendingRetrospectives': {'s1': {'embarked': False, 'teamIds': ['t1'],
                                             'scrumMasterId': 'sm1'}},
            'pendingSprintRetros': ['s1'],
        }
        sim._retro_step(state, now=1.0, now_ms=1_000_000)
        self.assertIn('s1', state['pendingRetrospectives'])


if __name__ == '__main__':
    unittest.main()