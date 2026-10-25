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


class StuckGateSweep(SimIsolation):
    @staticmethod
    def _gated_state():
        return {
            'agents': {'a': {'id': 'a', 'busy': False},
                       'r1': {'id': 'r1', 'busy': True, 'task': 'other'},
                       'r2': {'id': 'r2', 'busy': True, 'task': 'other'},
                       'r3': {'id': 'r3', 'busy': False},
                       'r4': {'id': 'r4', 'busy': False}},
            'agentRoster': [
                {'id': 'a', 'name': 'A'},
                {'id': 'r1', 'name': 'R1'},
                {'id': 'r2', 'name': 'R2'},
                {'id': 'r3', 'name': 'R3'},
                {'id': 'r4', 'name': 'R4'},
            ],
            'tasks': {'t1': {'id': 't1', 'status': 'needs_review', 'assignedTo': 'a',
                             'room': 'pressoffice', 'title': 'story',
                             '_peerGate': {'reviewerIds': ['r1', 'r2'], 'enteredMs': 0,
                                           'stuckRescueTs': 0, 'approvals': 0}}},
            'workQueue': [{'reviewOf': 't1', 'assignedTo': 'r3', 'title': 'Review: story'}],
        }

    def test_sweep_young_gate_skipped(self):
        state = self._gated_state()
        sim._sweep_stuck_gates(state, sim.STUCK_GATE_GRACE_MS // 2)
        self.assertEqual(state['tasks']['t1']['_peerGate']['reviewerIds'], ['r1', 'r2'])

    def test_sweep_rescued_recently_skipped(self):
        state = self._gated_state()
        sim._sweep_stuck_gates(state, sim.STUCK_GATE_GRACE_MS + 1)
        self.assertEqual(state['tasks']['t1']['_peerGate']['reviewerIds'], ['r1', 'r2'])

    def test_sweep_reachable_pair_left_alone(self):
        state = self._gated_state()
        for rid in ('r1', 'r2'):
            state['agents'][rid]['busy'] = False
            state['agents'][rid]['task'] = None
        now = sim.STUCK_GATE_GRACE_MS + sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1
        sim._sweep_stuck_gates(state, now)
        self.assertEqual(state['tasks']['t1']['_peerGate']['reviewerIds'], ['r1', 'r2'])

    def test_sweep_widens_unreachable_pair(self):
        state = self._gated_state()
        now = sim.STUCK_GATE_GRACE_MS + sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1
        with unittest.mock.patch('serve.log_action'):
            sim._sweep_stuck_gates(state, now)
        gate = state['tasks']['t1']['_peerGate']
        self.assertEqual(gate['reviewerIds'], ['r3', 'r4'])
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'r3')
        self.assertEqual(gate['stuckRescueTs'], now)

    def test_sweep_no_fresh_reviewer_noop(self):
        state = self._gated_state()
        state['agents'].pop('r3')
        state['agents'].pop('r4')
        state['agentRoster'] = [d for d in state['agentRoster']
                                if d['id'] not in ('r3', 'r4')]
        now = sim.STUCK_GATE_GRACE_MS + sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1
        sim._sweep_stuck_gates(state, now)
        self.assertEqual(state['tasks']['t1']['_peerGate']['reviewerIds'], ['r1', 'r2'])

    def test_sweep_reenters_lost_review(self):
        state = self._gated_state()
        state['workQueue'] = []
        state['agents']['r1']['busy'] = False
        state['agents']['r1']['task'] = None
        now = sim.STUCK_GATE_GRACE_MS + sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1
        with unittest.mock.patch('serve.log_action'):
            sim._sweep_stuck_gates(state, now)
        gate = state['tasks']['t1']['_peerGate']
        self.assertEqual(gate['stuckRescueTs'], now)
        self.assertTrue(any(it.get('reviewOf') == 't1' for it in state['workQueue']))


class FiringReview(SimIsolation):
    @staticmethod
    def _state(candidate=None, severe=True):
        candidate = candidate or {
            'id': 'c', 'name': 'Cara', 'role': 'Dev',
            'approvedCount': 1, 'droppedCount': 5, 'hiredAt': 1000,
            'busy': False, 'task': None, 'pairWith': None, 'handoff': None,
        }
        reports = []
        if severe:
            reports = [{'aboutId': 'c', 'fromId': 'r', 'severity': 'severe',
                        'quote': 'bad', 'note': 'worse'}]
        return {
            'agents': {
                'c': candidate,
                'r1': {'id': 'r1', 'name': 'R1', 'busy': False, 'visible': True},
                'r2': {'id': 'r2', 'name': 'R2', 'busy': False, 'visible': True},
            },
            'agentRoster': [
                {'id': 'r1', 'name': 'R1'}, {'id': 'r2', 'name': 'R2'},
                {'id': 'c', 'name': 'Cara'},
            ],
            'reports': reports,
        }

    def _pending(self):
        return {'reviewer1Id': 'r1', 'reviewer2Id': 'r2', 'candidateId': 'c',
                'at': 1000}

    def test_resolve_firing_review_candidate_vanished(self):
        state = self._state()
        state['agents'].pop('c')
        state['_pendingFiringReview'] = self._pending()
        sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                   decider=lambda *a: 'fire')
        self.assertNotIn('_pendingFiringReview', state)

    def test_resolve_firing_review_candidate_def_missing(self):
        state = self._state()
        state['agentRoster'] = [d for d in state['agentRoster'] if d['id'] != 'c']
        state['_pendingFiringReview'] = self._pending()
        sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                   decider=lambda *a: 'fire')
        self.assertNotIn('_pendingFiringReview', state)

    def test_resolve_firing_review_deferred_for_consultation(self):
        # No severe report -> consultation blocks the fire.
        state = self._state(severe=False)
        state['_pendingFiringReview'] = self._pending()
        with unittest.mock.patch('serve.log_action'):
            sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                       decider=lambda *a: 'fire')
        self.assertIn('c', state['agents'])
        self.assertNotIn('_pendingFiringReview', state)
        self.assertIsNone(state['agents']['c']['lastFiringReview'])

    def test_resolve_firing_review_busy_candidate_deferred(self):
        state = self._state()
        state['agents']['c']['busy'] = True
        state['agents']['c']['task'] = 't9'
        state['_pendingFiringReview'] = self._pending()
        with unittest.mock.patch('serve.log_action'):
            sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                       decider=lambda *a: 'fire')
        self.assertIn('c', state['agents'])
        self.assertNotIn('_pendingFiringReview', state)

    def test_resolve_firing_review_fires(self):
        state = self._state()
        state['_pendingFiringReview'] = self._pending()
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.revoke_agent_credentials'):
            sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                       decider=lambda *a: 'fire')
        self.assertNotIn('c', state['agents'])
        self.assertEqual([d['id'] for d in state['agentRoster']], ['r1', 'r2'])
        self.assertIn('cara', state['_usedNames'])
        self.assertNotIn('_pendingFiringReview', state)

    def test_resolve_firing_review_keeps(self):
        state = self._state()
        state['_pendingFiringReview'] = self._pending()
        with unittest.mock.patch('serve.log_action'):
            sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                       decider=lambda *a: 'keep')
        self.assertIn('c', state['agents'])
        self.assertEqual(state['agents']['c']['lastFiringReview']['verdict'], 'keep')
        self.assertNotIn('_pendingFiringReview', state)

    def test_fire_decision_outage_fallback(self):
        state = self._state()
        info = {'reporters': [{'id': 'r', 'severity': 'severe'}], 'coworkers': []}
        with unittest.mock.patch('sim._firing_consultation', return_value=info):
            decision, _ = sim._fire_decision(state, [{'name': 'R1'}, {'name': 'R2'}],
                                             {'id': 'c'}, 2000, decider=lambda *a: None)
        self.assertEqual(decision, 'fire')  # severe report + dropped > approved*0.3


class AssignDueItemEdges(SimIsolation):
    @staticmethod
    def _state():
        return {
            'agents': {
                'ada': {'id': 'ada', 'busy': False, 'task': None, 'offDuty': False,
                        'x': 600, 'y': 340},
                'ben': {'id': 'ben', 'busy': False, 'task': None, 'offDuty': False,
                        'x': 640, 'y': 340},
                'faye': {'id': 'faye', 'busy': False, 'task': None, 'offDuty': False,
                         'isAdmin': True, 'x': 680, 'y': 340},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'director': 't1'},
                {'id': 'ben', 'name': 'Ben', 'director': 't2'},
                {'id': 'faye', 'name': 'Faye', 'isAdmin': True, 'director': 'admin'},
            ],
            'sim': {'rr': {'task': 0}},
            'workQueue': [],
        }

    def test_assign_due_item_resolves_room_for_spike(self):
        state = self._state()
        pick = {'title': 'Investigate', 'taskType': 'spike', 'notBefore': None,
                'pair': False}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(pick['room'], 'observatory')
        self.assertEqual(task['taskType'], 'spike')
        self.assertEqual(state['workQueue'], [])

    def test_assign_due_item_rescues_a_room_with_no_door(self):
        # Regression: a producer named a room that exists nowhere in
        # door_triggers.json ('townsquare'), so assign_task returned None on
        # every cycle -- the item retried 21k+ times over 38 hours and no agent
        # could ever claim it. A room that does not resolve to a real door is
        # routed through the normal resolver instead of wedging forever.
        state = self._state()
        pick = {'title': 'Act on mail (harold)', 'room': 'townsquare',
                '_mailResume': True, 'assignedTo': 'ada', 'notBefore': 1000}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task, 'a phantom room must not make the item unassignable')
        self.assertIn(pick['room'], doors)
        self.assertNotEqual(pick['room'], 'townsquare')

    def test_assign_due_item_drops_a_review_of_an_agent_id(self):
        # help_signal mail stamped an AGENT id into reviewOf; that both lies to
        # the review lifecycle and claimed the permanent-retry exemption.
        state = self._state()
        pick = {'title': 'Act on mail (harold)', 'room': None,
                'reviewOf': 'ada', '_mailResume': True, 'assignedTo': 'ben',
                'notBefore': 1000, 'attempts': sim.WORK_ITEM_STALL_CEILING + 1}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertIsNone(pick.get('reviewOf'))

    def test_assign_due_item_empty_sprint_pool_returns_none(self):
        state = self._state()
        state['sprints'] = {'s1': {'status': 'active', 'workerCount': 2}}
        pick = {'title': 'Sprint card', 'room': 'pressoffice', 'sprintId': 's1'}
        grid, doors = sim._load_outdoor_geometry()
        self.assertIsNone(sim._assign_due_item(state, pick, True, grid, doors, 1000))

    def test_assign_due_item_pinned_reviewer_wins(self):
        state = self._state()
        state['agents']['ben']['offDuty'] = True
        pick = {'title': 'Review: story', 'room': 'pressoffice', 'reviewOf': 't1',
                'assignedTo': 'ben', 'taskType': 'review'}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['assignedTo'], 'ben')
        self.assertFalse(state['agents']['ben']['offDuty'])

    def test_assign_due_item_no_candidates_returns_none(self):
        state = self._state()
        for a in state['agents'].values():
            a['busy'] = True
        pick = {'title': 'Story', 'room': 'pressoffice'}
        grid, doors = sim._load_outdoor_geometry()
        self.assertIsNone(sim._assign_due_item(state, pick, True, grid, doors, 1000))

    def test_assign_due_item_team_preference_narrows_pick(self):
        state = self._state()
        pick = {'title': 'Team story', 'room': 'pressoffice', 'teamId': 't1'}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertEqual(task['assignedTo'], 'ada')


class ReclaimAndEscalation(SimIsolation):
    def test_reclaim_orphaned_walking_tasks_requeues(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'x',
                                  'room': 'pressoffice', 'taskType': 'code',
                                  'assignedTo': 'ghost'}},
                 'agents': {}, 'workQueue': []}
        count = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(count, 1)
        self.assertNotIn('t1', state['tasks'])
        self.assertEqual(state['workQueue'][0]['title'], 'x')

    def test_reclaim_orphaned_walking_tasks_skips_in_social(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'x',
                                  'room': 'pressoffice', 'assignedTo': 'ghost',
                                  '_inSocial': 1234}},
                 'agents': {}, 'workQueue': []}
        self.assertEqual(sim._reclaim_orphaned_walking_tasks(state), 0)
        self.assertIn('t1', state['tasks'])

    def test_reclaim_orphaned_walking_tasks_held_task_skipped(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'x',
                                  'room': 'pressoffice', 'assignedTo': 'ada'}},
                 'agents': {'ada': {'id': 'ada', 'task': 't1', 'busy': False}},
                 'workQueue': []}
        self.assertEqual(sim._reclaim_orphaned_walking_tasks(state), 0)
        self.assertIn('t1', state['tasks'])

    def test_reclaim_orphaned_walking_tasks_abandons_over_cap(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'x',
                                  'room': 'pressoffice', 'taskType': 'code',
                                  'attempts': sim.WORK_ITEM_MAX_ATTEMPTS - 1,
                                  'assignedTo': 'ghost'}},
                 'agents': {}, 'workQueue': []}
        with unittest.mock.patch('serve.log_action'):
            count = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(count, 1)
        self.assertNotIn('t1', state['tasks'])
        self.assertEqual(state['workQueue'], [])

    def test_reclaim_orphaned_walking_tasks_work_queue_not_list(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'working', 'title': 'x',
                                  'room': 'pressoffice', 'assignedTo': 'ghost'}},
                 'agents': {}, 'workQueue': None}
        sim._reclaim_orphaned_walking_tasks(state)
        self.assertIsInstance(state['workQueue'], list)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['title'], 'x')

    def test_start_escalation_dedups_by_product(self):
        state = {'_escalatedProducts': {'p1': {'resolved': False}},
                 'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}]}
        self.assertIsNone(sim._start_escalation(state, 'p1', 'd1', 't', 'src', 1000))

    def test_start_escalation_no_scrum_master(self):
        state = {'teams': [{'directorId': 'd1'}]}
        self.assertIsNone(sim._start_escalation(state, 'p1', 'd1', 't', 'src', 1000))

    def test_start_escalation_caps_concurrent(self):
        state = {'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}]}
        state['_escalatedProducts'] = {
            f'p{i}': {'directorId': 'd1', 'resolved': False}
            for i in range(sim.ESCALATION_MAX_OPEN)}
        self.assertIsNone(sim._start_escalation(state, 'p-new', 'd1', 't', 'src', 1000))

    def test_start_escalation_opens(self):
        state = {'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}]}
        sm = sim._start_escalation(state, 'p1', 'd1', 'Broken thing', 'unrestored', 1000)
        self.assertEqual(sm, 'sm')
        self.assertEqual(state['_pendingEscalation']['productId'], 'p1')
        self.assertFalse(state['_pendingEscalation']['embarked'])
        self.assertIn('p1', state['_escalatedProducts'])

    def test_resolve_escalation_story_and_spike_and_outage(self):
        state = {'agents': {'sm': {'id': 'sm', 'name': 'SM', 'busy': False,
                                   'visible': True}},
                 'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                 '_escalatedProducts': {'p1': {'resolved': False}}}
        pending = {'scrumMasterId': 'sm', 'productId': 'p1', 'title': 'Broken',
                   'source': 'unrestored', 'people': {}}
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_escalation_digest'):
            sim._resolve_escalation(state, pending, 1000, decider=lambda *a: 'story')
        self.assertEqual(state['backlogRequests'][0]['origin'], 'oncall_escalation')
        self.assertEqual(state['_escalatedProducts']['p1']['outcome'], 'story')
        self.assertIsNone(state['_pendingEscalation'])
        # Spike path.
        state2 = {'agents': {'sm': {'id': 'sm', 'name': 'SM', 'busy': False,
                                    'visible': True}},
                  'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                  '_escalatedProducts': {'p2': {'resolved': False}}}
        pending2 = {'scrumMasterId': 'sm', 'productId': 'p2', 'title': 'Broken',
                    'source': 'unrestored', 'people': {}}
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_escalation_digest'):
            sim._resolve_escalation(state2, pending2, 1000, decider=lambda *a: 'spike')
        self.assertEqual(state2['workQueue'][0]['taskType'], 'spike')
        # Outage -> spike fallback.
        state3 = {'agents': {'sm': {'id': 'sm', 'name': 'SM', 'busy': False,
                                    'visible': True}},
                  'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                  '_escalatedProducts': {'p3': {'resolved': False}}}
        pending3 = {'scrumMasterId': 'sm', 'productId': 'p3', 'title': 'Broken',
                    'source': 'unrestored', 'people': {}}
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_escalation_digest'):
            sim._resolve_escalation(state3, pending3, 1000, decider=lambda *a: None)
        self.assertEqual(state3['workQueue'][0]['taskType'], 'spike')

    def test_escalation_step_advances_and_sweeps(self):
        state = {'agents': {'sm': {'id': 'sm', 'name': 'SM', 'busy': False,
                                   'visible': True, 'offDuty': False}},
                 'agentRoster': [{'id': 'sm', 'name': 'SM'}]}
        state['_pendingEscalation'] = {'scrumMasterId': 'sm', 'embarked': False,
                                       'productId': 'p1', 'title': 'B',
                                       'source': 'unrestored', 'at': 0, 'people': {}}
        with unittest.mock.patch('serve.log_action'):
            sim._escalation_step(state, 1.0, 1000)
        self.assertTrue(state['_pendingEscalation']['embarked'])
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_escalation_digest'):
            sim._escalation_step(state, 1.0, 1000 + sim.ESCALATION_MEET_MS + 1,
                                 decider=lambda *a: 'spike')
        self.assertIsNone(state['_pendingEscalation'])

    def test_escalation_step_sweeps_unrestored_bug(self):
        state = {'agents': {'sm': {'id': 'sm', 'busy': False, 'offDuty': False}},
                 'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}],
                 'products': {'p1': {'teamId': 'd1'}},
                 'tasks': {'b1': {'id': 'b1', 'taskType': 'bug', 'status': 'walking',
                                  'openedAt': 1, 'productId': 'p1',
                                  'title': 'Broken'}}}
        with unittest.mock.patch('serve.log_action'):
            sim._escalation_step(state, 1.0, sim.RESTORE_TIMEOUT_MS + 1)
        self.assertIn('p1', state['_escalatedProducts'])
        self.assertEqual(state['_pendingEscalation']['scrumMasterId'], 'sm')

    def test_on_work_item_abandoned_incident_routes(self):
        state = {'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}],
                 'products': {'p1': {'teamId': 'd1'}}}
        with unittest.mock.patch('serve.log_action'):
            sim._on_work_item_abandoned(
                state, {'taskType': 'bug', 'productId': 'p1', 'title': 'B'},
                int(1000))
        self.assertIn('p1', state['_escalatedProducts'])
        # Non-bug, no product, no director -> no-op.
        state2 = {}
        sim._on_work_item_abandoned(state2, {'taskType': 'code', 'productId': 'p1'}, 1)
        sim._on_work_item_abandoned(state2, {'taskType': 'bug'}, 1)
        sim._on_work_item_abandoned(state2, {'taskType': 'bug', 'productId': 'nope'}, 1)
        self.assertNotIn('_escalatedProducts', state2)
        self.assertEqual(len(state2['_abandonedCards']), 3)
