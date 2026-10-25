"""Coverage-push tests for world/sim.py: the social ceremony pipeline, the
Jev-backed decider defaults, the movement stuck/replan/respawn/cancel branches,
and the small never-called helpers. Uses the temp-DB isolation pattern from
test_sim.py so the real think_tank.db is never touched; serve network helpers
are mocked at the module attribute (deciders late-import serve)."""
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve
import sim

from _sim_social_helpers import SimIsolation


class SocialCeremony(SimIsolation):
    @staticmethod
    def _social_state():
        return {
            'agents': {
                'ada': {'id': 'ada', 'x': 10, 'y': 20, 'dir': 'south',
                        'busy': True, 'task': 't1', 'inRoom': 'office',
                        'offDuty': False, 'visible': True, 'weekApprovals': 3,
                        'name': 'Ada'},
                'ben': {'id': 'ben', 'x': 30, 'y': 40, 'dir': 'north',
                        'busy': False, 'task': None, 'inRoom': None,
                        'offDuty': True, 'visible': False, 'weekApprovals': 2,
                        'name': 'Ben'},
                'cat': {'id': 'cat', 'x': 50, 'y': 60, 'dir': 'south',
                        'busy': False, 'task': None, 'inRoom': None,
                        'offDuty': False, 'visible': True, 'weekApprovals': 0,
                        'name': 'Cat'},
                'dan': {'id': 'dan', 'x': 70, 'y': 80, 'dir': 'south',
                        'busy': False, 'task': None, 'inRoom': None,
                        'offDuty': False, 'visible': True, 'weekApprovals': 5,
                        'handoff': {'with': 'ada'}, 'name': 'Dan'},
            },
            'tasks': {'t1': {'status': 'walking', 'workUntil': 1000}},
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada'}, {'id': 'ben', 'name': 'Ben'},
                {'id': 'cat', 'name': 'Cat'}, {'id': 'dan', 'name': 'Dan'},
            ],
        }

    def test_attendees_filters_by_work_handoff_pair(self):
        state = self._social_state()
        state['agents']['eve'] = {'id': 'eve', 'weekApprovals': 4,
                                  'pairWith': 'ada', 'offDuty': False}
        out = sim._social_attendees(state, {'embarked': False})
        self.assertEqual(out, {'ada': True, 'ben': True})
        self.assertNotIn('cat', out)   # weekApprovals <= 0
        self.assertNotIn('dan', out)   # mid-handoff
        self.assertNotIn('eve', out)   # mid-pair

    def test_convene_social_snapshots_workers_and_off_duty(self):
        state = self._social_state()
        pending = {'embarked': False, 'at': 0, 'people': {}}
        with unittest.mock.patch('serve.log_action'):
            sim._convene_social(state, pending, 1234)
        self.assertTrue(pending['embarked'])
        self.assertEqual(pending['at'], 1234 + sim.SOCIAL_MEET_MS)
        self.assertEqual(set(pending['people']), {'ada', 'ben'})
        ada = state['agents']['ada']
        self.assertEqual(ada['inRoom'], 'hangout')
        self.assertEqual(ada['task'], None)
        self.assertTrue(ada['busy'])
        self.assertTrue(ada['visible'])
        self.assertFalse(ada['offDuty'])
        self.assertEqual(state['tasks']['t1']['_inSocial'], 1234)
        ben = state['agents']['ben']
        self.assertTrue(ben['visible'])      # brought into the Hangout
        self.assertFalse(ben['offDuty'])
        self.assertEqual(pending['people']['ben']['offDuty'], True)

    def test_convene_social_records_co_location_acquaintances(self):
        # Item 8: co-location at the social ceremony is the marker that two agents
        # met -- every pair pulled into the Hangout becomes acquainted, so the
        # perception present-list can name them afterwards.
        state = self._social_state()
        pending = {'embarked': False, 'at': 0, 'people': {}}
        with unittest.mock.patch('serve.log_action'):
            sim._convene_social(state, pending, 1234)
        self.assertTrue(sim._are_acquainted(state, 'ada', 'ben'))
        self.assertTrue(sim._are_acquainted(state, 'ben', 'ada'))
        self.assertEqual(sim._describe_agent(state, 'ben', 'ada'), 'Ben')
        self.assertIn('Ben', sim._build_agent_perception(state, 'ada'))

    def test_restore_worker_extends_workuntil(self):
        state = self._social_state()
        snapshot = {'offDuty': False, 'visible': True, 'x': 10, 'y': 20,
                    'dir': 'south', 'task': 't1', 'busy': True,
                    'inRoom': 'office', 'workUntil': 1000}
        sim._restore_social_agent(state, 'ada', snapshot, 1234, 60000)
        a = state['agents']['ada']
        self.assertEqual(a['task'], 't1')
        self.assertTrue(a['busy'])
        self.assertEqual(a['inRoom'], 'office')
        self.assertTrue(a['visible'])
        self.assertNotIn('_inSocial', state['tasks']['t1'])
        self.assertEqual(state['tasks']['t1']['workUntil'], 1060)

    def test_restore_off_duty_and_idle_and_vanished(self):
        state = self._social_state()
        sim._restore_social_agent(state, 'ben',
                                  {'offDuty': True, 'task': None, 'visible': False},
                                  1234, 60000)
        self.assertTrue(state['agents']['ben']['offDuty'])
        self.assertFalse(state['agents']['ben']['visible'])
        sim._restore_social_agent(state, 'cat',
                                  {'offDuty': False, 'task': None, 'visible': True},
                                  1234, 60000)
        self.assertFalse(state['agents']['cat']['busy'])
        self.assertTrue(state['agents']['cat']['visible'])
        sim._restore_social_agent(state, 'ghost', {'offDuty': False, 'task': None},
                                  1234, 60000)  # vanished -> no-op

    def test_resolve_social_writes_tape_and_growth_plans(self):
        state = self._social_state()
        state['agents']['ben']['offDuty'] = False
        pending = {
            'people': {
                'ada': {'offDuty': False, 'visible': True, 'x': 10, 'y': 20,
                        'dir': 'south', 'task': 't1', 'busy': True,
                        'inRoom': 'office', 'workUntil': 1000},
                'ben': {'offDuty': False, 'visible': False, 'x': 30, 'y': 40,
                        'dir': 'north', 'task': None, 'busy': False,
                        'inRoom': None},
            },
        }
        choices = iter([('adopt', 0.9), ('note', 0.5)])
        decider = lambda instructions, criteria: next(choices)
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_social_digest') as digest:
            sim._resolve_social(state, pending, 5000, decider=decider)
        self.assertNotIn('_pendingSocial', state)
        self.assertEqual(state['agents']['ada']['weekApprovals'], 0)
        self.assertEqual(state['agents']['ben']['weekApprovals'], 0)
        self.assertEqual(len(state.get('_socialDecisions') or []), 2)
        plans = (state.get('growthPlans') or {}).get('ada') or []
        self.assertEqual(len(plans), 1)          # adopt + high confidence
        self.assertEqual(plans[0]['kind'], 'social_adopt')
        self.assertTrue(plans[0]['applied'] is False)
        self.assertEqual((state.get('growthPlans') or {}).get('ben'), None)
        digest.assert_called_once()

    def test_resolve_social_survives_digest_outage(self):
        state = self._social_state()
        pending = {'people': {}}
        with unittest.mock.patch('serve.log_action'):
            with unittest.mock.patch('serve.log_social_digest',
                                     side_effect=RuntimeError('boom')):
                sim._resolve_social(state, pending, 5000, decider=lambda *a: ('skip', 0.1))
        self.assertNotIn('_pendingSocial', state)

    def test_social_step_schedules_then_convenes_then_resolves(self):
        state = self._social_state()
        state['agents']['ben']['offDuty'] = False
        now_ms = sim.SOCIAL_CADENCE_MS + 1000
        with unittest.mock.patch('serve.log_action'):
            sim._social_step(state, now_ms / 1000, now_ms)
            pending = state['_pendingSocial']
            self.assertFalse(pending['embarked'])
            self.assertEqual(state['lastSocialAt'], now_ms)
            sim._social_step(state, now_ms / 1000, now_ms)
            self.assertTrue(state['_pendingSocial']['embarked'])
            self.assertEqual(set(state['_pendingSocial']['people']), {'ada', 'ben'})
            sim._social_step(state, now_ms / 1000, now_ms + sim.SOCIAL_MEET_MS + 1,
                             decider=lambda *a: ('skip', 0.1))
        self.assertNotIn('_pendingSocial', state)

    def test_social_step_in_flight_before_cadence_is_noop(self):
        state = {'agents': {}, 'lastSocialAt': sim.SOCIAL_CADENCE_MS}
        sim._social_step(state, 0, sim.SOCIAL_CADENCE_MS - 1)
        self.assertNotIn('_pendingSocial', state)

    def test_social_decider_default_returns_choice(self):
        data = {'answers': {'q': {'choice': 'adopt', 'confidence': 0.9}},
                'usage': {'cost': 0.01}}
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value=data), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            choice, conf = sim._social_decider_default(
                'instr', [{'id': 'adopt', 'description': 'x'},
                          {'id': 'skip', 'description': 'y'}])
        self.assertEqual((choice, conf), ('adopt', 0.9))

    def test_social_decider_default_rejects_unknown_and_outage(self):
        data = {'answers': {'q': {'choice': 'bogus'}}}
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value=data), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            choice, conf = sim._social_decider_default('instr', [])
        self.assertEqual(choice, None)
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 side_effect=RuntimeError('offline')):
            choice, conf = sim._social_decider_default('instr', [])
        self.assertEqual((choice, conf), (None, 1.0))


class CeremonyBranches(SimIsolation):
    @staticmethod
    def _team_state():
        return {
            'agents': {
                'sm': {'id': 'sm', 'busy': True, 'offDuty': False, 'visible': True},
                'dir': {'id': 'dir', 'busy': False, 'offDuty': False, 'visible': True},
                'dir2': {'id': 'dir2', 'busy': False, 'offDuty': False, 'visible': True},
                'worker': {'id': 'worker', 'busy': False, 'offDuty': False, 'visible': True},
            },
            'teams': [
                {'id': 't1', 'directorId': 'dir', 'scrumMasterId': 'sm'},
                {'id': 't2', 'directorId': 'dir2', 'scrumMasterId': 'sm'},
            ],
            'agentRoster': [
                {'id': 'sm', 'name': 'SM', 'director': 'dir'},
                {'id': 'dir', 'name': 'Dir', 'director': 'admin'},
                {'id': 'dir2', 'name': 'Dir2', 'director': 'admin'},
                {'id': 'worker', 'name': 'Worker', 'director': 'dir'},
            ],
        }

    def test_ceremony_facilitator_director_fallback(self):
        state = self._team_state()
        self.assertEqual(sim._ceremony_facilitator(state, 't1'), 'dir')

    def test_ceremony_facilitator_borrows_other_director(self):
        state = self._team_state()
        state['agents']['dir']['busy'] = True
        self.assertEqual(sim._ceremony_facilitator(state, 't1'), 'dir2')

    def test_ceremony_facilitator_returns_none_when_all_busy(self):
        state = self._team_state()
        state['agents']['dir']['busy'] = True
        state['agents']['dir2']['busy'] = True
        state['agents']['sm']['busy'] = True
        self.assertIsNone(sim._ceremony_facilitator(state, 't1'))

    def test_ceremony_facilitator_uses_free_scrum_master(self):
        state = self._team_state()
        state['agents']['sm']['busy'] = False
        self.assertEqual(sim._ceremony_facilitator(state, 't1'), 'sm')

    def test_ceremony_facilitator_wakes_when_everyone_is_off_duty(self):
        state = self._team_state()
        for a in state['agents'].values():
            a['offDuty'] = True
        self.assertIn(sim._ceremony_facilitator(state, 't1'), ('sm', 'dir'))

    def test_refinement_scrum_master_small_team_director_standin(self):
        state = self._team_state()
        state['teams'][0]['scrumMasterId'] = None
        # 1 direct report (< SCRUM_MASTER_MIN_TEAM_SIZE=4) -> director stands in.
        self.assertEqual(sim._refinement_scrum_master_for_team(state, 't1'), 'dir')

    def test_refinement_scrum_master_big_team_without_sm(self):
        state = self._team_state()
        state['teams'][0]['scrumMasterId'] = None
        # A large team (>= 4 members) with no designated SM -> None.
        state['agentRoster'].extend([
            {'id': 'w2', 'name': 'W2', 'director': 'dir'},
            {'id': 'w3', 'name': 'W3', 'director': 'dir'},
            {'id': 'w4', 'name': 'W4', 'director': 'dir'},
        ])
        for wid in ('w2', 'w3', 'w4'):
            state['agents'][wid] = {'id': wid, 'busy': False, 'offDuty': False}
        self.assertIsNone(sim._refinement_scrum_master_for_team(state, 't1'))

    def test_restore_escalation_agent_off_duty(self):
        state = {'agents': {'sm': {'id': 'sm', 'offDuty': False, 'visible': True,
                                   'task': 't1', 'busy': True, 'inRoom': 'commandcenter'}}}
        sim._restore_escalation_agent(state, 'sm', {'offDuty': True, 'task': 't1'}, 1000)
        a = state['agents']['sm']
        self.assertTrue(a['offDuty'])
        self.assertFalse(a['visible'])
        self.assertIsNone(a['task'])
        self.assertFalse(a['busy'])

    def test_restore_escalation_agent_mid_task_extends_budget(self):
        state = {'agents': {'sm': {'id': 'sm', 'offDuty': False, 'visible': True,
                                   'task': 't1', 'busy': True, 'inRoom': 'office'}},
                 'tasks': {'t1': {'status': 'working', 'workUntil': 1000}}}
        sim._restore_escalation_agent(
            state, 'sm',
            {'offDuty': False, 'visible': True, 'task': 't1', 'inRoom': 'office'},
            1000)
        a = state['agents']['sm']
        self.assertEqual(a['task'], 't1')
        self.assertTrue(a['busy'])
        self.assertEqual(state['tasks']['t1']['workUntil'],
                         1000 + sim.ESCALATION_MEET_MS // 1000)

    def test_restore_escalation_agent_idle(self):
        state = {'agents': {'sm': {'id': 'sm', 'task': 't1', 'busy': True,
                                   'inRoom': 'commandcenter', 'visible': True}}}
        sim._restore_escalation_agent(state, 'sm',
                                      {'offDuty': False, 'visible': True}, 1000)
        a = state['agents']['sm']
        self.assertIsNone(a['task'])
        self.assertFalse(a['busy'])
        self.assertTrue(a['visible'])

    def test_restore_escalation_agent_vanished_is_noop(self):
        sim._restore_escalation_agent({'agents': {}}, 'ghost',
                                      {'offDuty': False, 'task': None}, 1000)

    def test_resolve_refinement_accept_and_reject(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'name': 'SM'},
                       'worker': {'id': 'worker', 'name': 'Worker', 'role': 'Dev'}},
            'agentRoster': [{'id': 'sm', 'name': 'SM'}],
            'backlogRequests': [
                {'id': 'r1', 'filedBy': 'worker', 'room': 'pressoffice',
                 'title': 'Ship docs', 'reason': 'gap', 'status': 'pending'},
                {'id': 'r2', 'filedBy': 'worker', 'room': 'pressoffice',
                 'title': 'Trivial task', 'reason': 'dupe', 'status': 'pending'},
            ],
            'pendingRefinements': {'t1': {'scrumMasterId': 'sm', 'reqIds': ['r1', 'r2'],
                                          'teamId': 't1', 'people': {}}},
        }
        choices = iter(['accept', 'reject'])
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest'):
            sim._resolve_refinement(state, state['pendingRefinements']['t1'], 1000,
                                    decider=lambda i, c: next(choices))
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['status'], 'rejected')
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['teamRefinementAt']['t1'], 1000)
        self.assertNotIn('t1', state['pendingRefinements'])

    def test_resolve_refinement_outage_fallback_accepts_valued_room(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'name': 'SM'},
                       'worker': {'id': 'worker', 'name': 'Worker'}},
            'agentRoster': [{'id': 'sm', 'name': 'SM'}],
            'backlogRequests': [
                {'id': 'r1', 'filedBy': 'worker', 'room': 'pressoffice',
                 'title': 'Ship docs', 'reason': 'gap', 'status': 'pending'},
                {'id': 'r2', 'filedBy': 'worker', 'room': 'notaroom',
                 'title': 'Junk', 'reason': 'x', 'status': 'pending'},
            ],
            '_pendingRefinement': {'scrumMasterId': 'sm', 'reqIds': ['r1', 'r2'],
                                   'people': {}},
        }
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest'):
            sim._resolve_refinement(state, state['_pendingRefinement'], 1000,
                                    decider=lambda i, c: None)
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['status'], 'rejected')
        self.assertEqual(state['lastBacklogRefinementAt'], 1000)
        self.assertNotIn('_pendingRefinement', state)

    def test_resolve_refinement_survives_digest_outage(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'name': 'SM'}},
            'agentRoster': [{'id': 'sm', 'name': 'SM'}],
            'backlogRequests': [],
            '_pendingRefinement': {'scrumMasterId': 'sm', 'reqIds': [],
                                   'people': {}},
        }
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest',
                                 side_effect=RuntimeError('boom')):
            sim._resolve_refinement(state, state['_pendingRefinement'], 1000,
                                    decider=lambda i, c: 'reject')
        self.assertNotIn('_pendingRefinement', state)

    def test_breakdown_step_in_flight_resolve_and_convene(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'busy': False, 'offDuty': False, 'visible': True},
                       'dir': {'id': 'dir', 'busy': False, 'offDuty': False, 'visible': True},
                       'worker': {'id': 'worker', 'busy': False, 'offDuty': False, 'visible': True}},
            'teams': [{'id': 't1', 'directorId': 'dir', 'scrumMasterId': 'sm'}],
            'agentRoster': [
                {'id': 'sm', 'name': 'SM', 'director': 'dir'},
                {'id': 'dir', 'name': 'Dir', 'director': 'admin'},
                {'id': 'worker', 'name': 'Worker', 'director': 'dir'},
            ],
            'backlogRequests': [
                {'id': 'br1', 'filedBy': 'worker', 'status': 'pending', 'breakdown': True,
                 'teamId': 't1', 'title': 'Build the platform', 'goal': 'Build the platform',
                 'room': 'pressoffice'},
            ],
        }
        with unittest.mock.patch('serve.log_action'):
            sim._breakdown_step(state, 1.0, 1000,
                                decider=lambda s, i, g: {'items': [
                                    {'title': 'story one', 'type': 'story'},
                                    {'title': 'spike one', 'type': 'spike'}]})
        # Convened a ceremony (in-flight).
        self.assertIn('t1', state['pendingBreakdowns'])
        pend = state['pendingBreakdowns']['t1']
        self.assertTrue(pend['embarked'])
        # Advance it on the next pass -> resolves into queued stories.
        sim._breakdown_step(state, 1.0, 1000 + sim.BREAKDOWN_MEET_MS + 1,
                            decider=lambda s, i, g: {'items': [
                                {'title': 'story one', 'type': 'story'},
                                {'title': 'spike one', 'type': 'spike'}]})
        self.assertNotIn('t1', state['pendingBreakdowns'])
        titles = [it['title'] for it in state['workQueue']]
        self.assertEqual(titles, ['story one', 'spike one'])
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertTrue(state['backlogRequests'][0]['brokenDown'])

    def test_breakdown_step_defers_no_facilitator_and_active_sprint(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'busy': True, 'offDuty': False},
                       'dir': {'id': 'dir', 'busy': True, 'offDuty': False}},
            'teams': [{'id': 't1', 'directorId': 'dir', 'scrumMasterId': 'sm'}],
            'agentRoster': [
                {'id': 'sm', 'name': 'SM', 'director': 'dir'},
                {'id': 'dir', 'name': 'Dir', 'director': 'admin'},
            ],
            'backlogRequests': [
                {'id': 'br1', 'filedBy': 'sm', 'status': 'pending', 'breakdown': True,
                 'teamId': 't1', 'title': 'Big ask', 'goal': 'Big ask', 'room': 'pressoffice'},
            ],
        }
        sim._breakdown_step(state, 1.0, 1000)
        self.assertNotIn('t1', state['pendingBreakdowns'])  # no facilitator
        # With a free facilitator but an active sprint, still defer.
        state['agents']['sm']['busy'] = False
        state['agents']['dir']['busy'] = False
        state['sprints'] = {'s1': {'status': 'active', 'teamIds': ['t1']}}
        sim._breakdown_step(state, 1.0, 1000)
        self.assertNotIn('t1', state['pendingBreakdowns'])
