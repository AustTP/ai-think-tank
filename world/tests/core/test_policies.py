import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.policies as policies
import serve
import sim


class RouteTests(unittest.TestCase):
    def test_code_routes_to_coding(self):
        route = policies.choose_route('write a parser', task_type='code')
        self.assertEqual(route['band'], 'coding')
        self.assertEqual(route['best_of'], 1)

    def test_latency_routes_low(self):
        route = policies.choose_route('pulse triage on the live monitor')
        self.assertEqual(route['band'], 'low')
        self.assertEqual(route['best_of'], 1)

    def test_best_effort_keeps_samples_but_leaves_band_to_jev(self):
        route = policies.choose_route('hard synthesis', allow_high=True,
                                      best_effort=True)
        self.assertEqual(route['band'], 'mid')
        self.assertEqual(route['best_of'], 3)

    def test_routine_routes_low(self):
        route = policies.choose_route('answer a routine question')
        self.assertEqual(route['band'], 'low')


class AssignmentTests(unittest.TestCase):
    def test_room_experience_wins(self):
        state = {'agents': {
            'ada': {'id': 'ada', 'busy': False, 'task': None,
                    'completedRooms': ['pressoffice']},
            'ben': {'id': 'ben', 'busy': False, 'task': None,
                    'completedRooms': []},
        }}
        ranked = policies.rank_assignees(
            state, {'room': 'pressoffice', 'taskType': 'code'}, ['ben', 'ada'])
        self.assertEqual(ranked[0]['agentId'], 'ada')
        self.assertIn('room_experience', ranked[0]['reasons'])

    def test_off_duty_is_penalized(self):
        state = {'agents': {
            'ada': {'id': 'ada', 'busy': False, 'task': None, 'offDuty': True},
            'ben': {'id': 'ben', 'busy': False, 'task': None, 'offDuty': False},
        }}
        ranked = policies.rank_assignees(
            state, {'room': 'pressoffice', 'taskType': 'code'}, ['ada', 'ben'])
        self.assertEqual(ranked[0]['agentId'], 'ben')


class RetryTests(unittest.TestCase):
    def test_first_failure_keeps_author(self):
        decision = policies.decide_retry(
            {'assignedTo': 'ben'}, {'cycleCount': 0}, 'bad output',
            alternatives=['ada'])
        self.assertEqual(decision['action'], 'queue_fix')
        self.assertEqual(decision['target'], 'ben')

    def test_repeated_failure_reassigns(self):
        decision = policies.decide_retry(
            {'assignedTo': 'ben'}, {'cycleCount': 2}, 'bad output',
            alternatives=['ada'])
        self.assertEqual(decision['action'], 'reassign')
        self.assertEqual(decision['target'], 'ada')

    def test_infra_failure_keeps_author(self):
        decision = policies.decide_retry(
            {'assignedTo': 'ben'}, {'cycleCount': 5}, 'timeout',
            is_infra=True, alternatives=['ada'])
        self.assertEqual(decision['action'], 'queue_fix')
        self.assertEqual(decision['target'], 'ben')


class ModeTests(unittest.TestCase):
    def tearDown(self):
        policies.clear_shadow()

    def test_default_mode_is_shadow(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(policies.policy_mode(), 'shadow')
            self.assertFalse(policies.apply_enabled())

    def test_apply_mode(self):
        with mock.patch.dict(os.environ, {'OPTIMIZATION_POLICY_MODE': 'apply'}):
            self.assertTrue(policies.apply_enabled())

    def test_shadow_snapshot_records(self):
        policies.clear_shadow()
        policies.record_shadow('routing', {'band': 'low'}, {'purpose': 'x'})
        snap = policies.snapshot()
        self.assertEqual(snap['counts'].get('routing'), 1)
        self.assertEqual(snap['recent'][0]['decision']['band'], 'low')


class _Req:
    cookies = {}


class EndpointTests(unittest.TestCase):
    def test_requires_player_session(self):
        with mock.patch.object(serve, '_require_player_session', return_value=None):
            resp = asyncio.run(serve.optimization_policies(_Req()))
        self.assertEqual(resp.status_code, 401)

    def test_returns_policy_snapshot(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value={'id': 's'}):
            resp = asyncio.run(serve.optimization_policies(_Req()))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertIn('mode', data)
        self.assertIn('definitions', data)


class IntegrationTests(unittest.TestCase):
    def tearDown(self):
        policies.clear_shadow()

    def test_routing_apply_uses_policy_band(self):
        with mock.patch.dict(os.environ, {'OPTIMIZATION_POLICY_MODE': 'apply'}), \
                mock.patch.object(serve, '_coding_tier_slug',
                                  return_value='coding-model'):
            self.assertEqual(
                serve._resolve_model_tier('write code', task_type='code'),
                'coding-model')

    def test_routing_shadow_records_without_changing_result(self):
        policies.clear_shadow()
        with mock.patch.dict(os.environ, {'OPTIMIZATION_POLICY_MODE': 'shadow'}), \
                mock.patch.object(serve, '_low_tier_slug',
                                  return_value='low-model'):
            model = serve._resolve_model_tier(
                'routine question', decider=lambda *_: ('low', 1.0))
        self.assertEqual(model, 'low-model')
        self.assertGreaterEqual(policies.snapshot()['counts'].get('routing', 0), 1)

    def _assign_state(self):
        return {
            'sim': {'rr': {}},
            'agents': {
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'busy': False,
                        'task': None, 'offDuty': False},
                'ada': {'id': 'ada', 'x': 20, 'y': 0, 'busy': False,
                        'task': None, 'offDuty': False},
            },
            'workQueue': [],
        }

    def test_assignment_apply_uses_policy_pick(self):
        state = self._assign_state()
        pick = {'title': 'x', 'room': 'pressoffice', 'taskType': 'code',
                'instructions': 'do it', 'projectLabel': 'p'}
        with mock.patch.object(sim, '_eligible_candidates',
                               return_value=['ben', 'ada']), \
                mock.patch.object(sim.governance, 'may_work_code',
                                  return_value=True), \
                mock.patch.object(sim, '_sprint_worker_pool',
                                  return_value=None), \
                mock.patch.object(sim, '_one_off_routing_applies',
                                  return_value=False), \
                mock.patch.object(sim, '_agent_failure_score',
                                  return_value=0), \
                mock.patch.object(sim, '_feature_affinity',
                                  return_value=0), \
                mock.patch.object(sim, '_description_affinity',
                                  return_value=0), \
                mock.patch.object(sim, '_augment_task_instructions',
                                  return_value='do it'), \
                mock.patch.object(sim.policies, 'rank_assignees',
                                  return_value=[{'agentId': 'ada', 'score': 9,
                                                 'reasons': ['test']}]), \
                mock.patch.object(sim.policies, 'apply_enabled',
                                  return_value=True), \
                mock.patch.object(sim.policies, 'record_shadow'), \
                mock.patch.object(sim, 'assign_task',
                                  return_value={'id': 'task-1'}) as assign:
            task = sim._assign_due_item(
                state, pick, False, {'pressoffice': {'x': 0, 'y': 0, 'w': 1, 'h': 1}},
                {'cells': []}, 1000, task_id_holder=[1])
        self.assertEqual(task['id'], 'task-1')
        self.assertEqual(assign.call_args.args[1], 'ada')

    def test_assignment_shadow_keeps_round_robin(self):
        state = self._assign_state()
        pick = {'title': 'x', 'room': 'pressoffice', 'taskType': 'code',
                'instructions': 'do it', 'projectLabel': 'p'}
        with mock.patch.object(sim, '_eligible_candidates',
                               return_value=['ben', 'ada']), \
                mock.patch.object(sim.governance, 'may_work_code',
                                  return_value=True), \
                mock.patch.object(sim, '_sprint_worker_pool',
                                  return_value=None), \
                mock.patch.object(sim, '_one_off_routing_applies',
                                  return_value=False), \
                mock.patch.object(sim, '_agent_failure_score',
                                  return_value=0), \
                mock.patch.object(sim, '_feature_affinity',
                                  return_value=0), \
                mock.patch.object(sim, '_description_affinity',
                                  return_value=0), \
                mock.patch.object(sim, '_augment_task_instructions',
                                  return_value='do it'), \
                mock.patch.object(sim.policies, 'rank_assignees',
                                  return_value=[{'agentId': 'ada', 'score': 9,
                                                 'reasons': ['test']}]), \
                mock.patch.object(sim.policies, 'apply_enabled',
                                  return_value=False), \
                mock.patch.object(sim.policies, 'record_shadow'), \
                mock.patch.object(sim, 'assign_task',
                                  return_value={'id': 'task-1'}) as assign:
            sim._assign_due_item(
                state, pick, False, {'pressoffice': {'x': 0, 'y': 0, 'w': 1, 'h': 1}},
                {'cells': []}, 1000, task_id_holder=[1])
        self.assertEqual(assign.call_args.args[1], 'ben')

    def test_retry_apply_reassigns_after_repeated_cycles(self):
        state = {
            'agents': {
                'ben': {'id': 'ben', 'busy': False, 'task': None,
                        'pairWith': None, 'offDuty': False},
                'ada': {'id': 'ada', 'busy': False, 'task': None,
                        'pairWith': None, 'offDuty': False},
            },
            'workQueue': [],
        }
        task = {'id': 'task-1', 'title': 'Build', 'room': 'pressoffice',
                'taskType': 'code', 'assignedTo': 'ben', 'teamId': 'dev',
                'status': 'working',
                '_peerGate': {'cycleCount': 2, 'approvals': 0}}
        with mock.patch.object(sim, '_sim_direct_reports',
                               return_value=['ben', 'ada']), \
                mock.patch.object(sim.governance, 'may_work_code',
                                  return_value=True), \
                mock.patch.object(sim, '_sim_notify_author_failed'), \
                mock.patch.object(sim, '_write_growth_plan'), \
                mock.patch.object(sim, '_maybe_escalate_stuck_gate',
                                  return_value=False), \
                mock.patch.object(sim, '_log_quality_gate_reject'), \
                mock.patch.object(sim, '_log_governance'), \
                mock.patch.object(sim, '_open_fix_exists',
                                  return_value=False), \
                mock.patch.object(sim.policies, 'apply_enabled',
                                  return_value=True), \
                mock.patch.object(sim.policies, 'decide_retry',
                                  return_value={'action': 'reassign',
                                                'target': 'ada',
                                                'reason': 'test',
                                                'confidence': 'high'}), \
                mock.patch.object(sim.policies, 'record_shadow'):
            sim._send_back_after_failure(state, task, fail_note='bad output')
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'ada')
        self.assertEqual(state['workQueue'][0]['reviewAuthorId'], 'ada')

    def test_retry_shadow_keeps_original_author(self):
        state = {
            'agents': {
                'ben': {'id': 'ben', 'busy': False, 'task': None,
                        'pairWith': None, 'offDuty': False},
                'ada': {'id': 'ada', 'busy': False, 'task': None,
                        'pairWith': None, 'offDuty': False},
            },
            'workQueue': [],
        }
        task = {'id': 'task-1', 'title': 'Build', 'room': 'pressoffice',
                'taskType': 'code', 'assignedTo': 'ben', 'teamId': 'dev',
                'status': 'working',
                '_peerGate': {'cycleCount': 2, 'approvals': 0}}
        with mock.patch.object(sim, '_sim_direct_reports',
                               return_value=['ben', 'ada']), \
                mock.patch.object(sim.governance, 'may_work_code',
                                  return_value=True), \
                mock.patch.object(sim, '_sim_notify_author_failed'), \
                mock.patch.object(sim, '_write_growth_plan'), \
                mock.patch.object(sim, '_maybe_escalate_stuck_gate',
                                  return_value=False), \
                mock.patch.object(sim, '_log_quality_gate_reject'), \
                mock.patch.object(sim, '_log_governance'), \
                mock.patch.object(sim, '_open_fix_exists',
                                  return_value=False), \
                mock.patch.object(sim.policies, 'apply_enabled',
                                  return_value=False), \
                mock.patch.object(sim.policies, 'decide_retry',
                                  return_value={'action': 'reassign',
                                                'target': 'ada',
                                                'reason': 'test',
                                                'confidence': 'high'}), \
                mock.patch.object(sim.policies, 'record_shadow'):
            sim._send_back_after_failure(state, task, fail_note='bad output')
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'ben')


if __name__ == '__main__':
    unittest.main()
