"""Coverage-gap tests for world/sim.py and world/content.py.

Targets the residual uncovered lines reported by the per-file coverage run.
Every test is hermetic: serve network/DB seams are monkeypatched, no live
model call or fetch happens, and no real state DB is touched.
"""
import asyncio
import importlib
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.config as config  # noqa: E402
import content  # noqa: E402
import serve  # noqa: E402
import sim  # noqa: E402

from _content_sim_helpers import _Base, _completion, _snapshot


class DeepSimTest(_Base):
    def test_prune_state_blob_caps(self):
        state = {
            'completedDeliverables': [{'i': i} for i in range(501)],
            'backlogRequests': [{'id': 'p', 'status': 'pending'}] + [
                {'id': f'r{i}', 'status': 'resolved'} for i in range(201)],
            'playerInbox': [{'id': 'u'}] + [
                {'id': f'a{i}', 'answered': True} for i in range(101)],
            'issues': {
                f'k{i}': {'key': f'k{i}',
                          'status': 'open' if i == 0 else 'closed'}
                for i in range(102)},
            'growthPlans': [{'id': 'p', 'status': 'pending'}] + [
                {'id': f'g{i}', 'status': 'applied'} for i in range(51)],
        }
        sim._prune_state_blob(state, 1000)
        self.assertEqual(len(state['completedDeliverables']), 500)
        self.assertEqual(len(state['backlogRequests']), 201)
        self.assertEqual(len(state['playerInbox']), 101)
        self.assertEqual(len(state['issues']), 101)
        self.assertEqual(len(state['growthPlans']), 51)

    def test_resolve_meeting_attendees_pair_fallback(self):
        class FakeTeam:
            def __contains__(self, item):
                return False

            def __iter__(self):
                return iter(['fac'])

            def __bool__(self):
                return True

            def __getitem__(self, idx):
                return ['fa', 'fb'][idx]

        with unittest.mock.patch.object(sim, '_meeting_team_members',
                                        return_value=FakeTeam()):
            out = sim._resolve_meeting_attendees(
                {'agents': {}, 'agentRoster': []}, 'pair', 't1',
                facilitator_id='fac')
        self.assertEqual(out, ['fa', 'fb'])

    def test_spawn_new_team_request_constraints(self):
        state = {'agentRoster': [{'id': 'admin', 'name': 'Admin',
                                  'isAdmin': True}],
                 'agents': {}, 'teams': [], 'backlogRequests': []}
        names = iter(['Dana', 'Eve'])

        def chooser(st, admin_name, used, role):
            return next(names)

        with unittest.mock.patch.object(
                sim, '_load_outdoor_geometry', return_value=({}, {})), \
             unittest.mock.patch.object(sim, '_spawn_team_agent',
                                        return_value='dana'), \
             unittest.mock.patch.object(sim, '_remember_name'), \
             unittest.mock.patch.object(sim, '_estimate_employees_for_request',
                                        return_value=1), \
             unittest.mock.patch.object(sim, 'kick_refinement_now'), \
             unittest.mock.patch.object(sim, '_log_governance'):
            team = sim.spawn_new_team_for_request(
                state, 'Build a thing', chooser=chooser,
                constraints={'budgetUsd': 5, 'constraints': ['no x'],
                             'roles': ['dev'], 'order': ['a'],
                             'mustInclude': ['b']})
        self.assertIsNotNone(team)
        req = state['backlogRequests'][0]
        self.assertEqual(req['playerConstraints']['budgetUsd'], 5.0)
        self.assertEqual(req['playerConstraints']['roles'], ['dev'])

    def test_send_back_infra_never_instructions(self):
        task = {'id': 't1', 'assignedTo': 'cora', 'title': 'T',
                'note': 'boom', 'never': 'no hacks',
                '_infraFailure': True}
        state = {'agents': {'cora': {'mailbox': []}}, 'workQueue': []}
        queued = {}
        with unittest.mock.patch.object(sim, '_sim_notify_author_failed'), \
             unittest.mock.patch.object(sim, 'team_policy', return_value=8), \
             unittest.mock.patch.object(
                 sim, 'queue_work',
                 lambda st, items: queued.setdefault('items', items)), \
             unittest.mock.patch('serve.log_action'):
            sim._send_back_after_failure(state, task)
        self.assertIn('NEVER', queued['items'][0]['instructions'])
        self.assertIn('no hacks', queued['items'][0]['instructions'])

    def test_free_spike_bad_maps(self):
        state = {'agents': {'cora': {'id': 'cora'}},
                 'freeSpikeUsed': 'not-a-dict',
                 'freeSpikeGlobal': 'not-a-dict',
                 'freeSpikeRooms': 'not-a-dict'}
        with unittest.mock.patch.object(sim, 'queue_spike'), \
             unittest.mock.patch.object(sim, '_log_governance'):
            n = sim._file_free_spike(
                state, 'cora', {'room': 'pressoffice', 'title': 'T'},
                now_ms=1_700_000_000_000)
        self.assertEqual(n, 1)
        self.assertEqual(sim._file_free_spike(
            {'agents': {'cora': {'id': 'cora'}}}, 'cora',
            {'room': 'nowhere', 'title': 'T'}, now_ms=1_700_000_000_000), 0)

    def test_free_spike_week_map_skips_bad_keys(self):
        self.assertEqual(sim._free_spike_week_map({'bad': 1}), {})

    def test_watchdog_event_int_guard_and_log_error(self):
        state = {'agents': {'d1': {'id': 'd1'}},
                 'agentRoster': [{'id': 'd1'},
                                 {'id': 'admin', 'isAdmin': True}]}
        with unittest.mock.patch.object(sim, '_senior_director',
                                        return_value={'id': 'd1'}), \
             unittest.mock.patch.object(sim, '_deliver_mail'), \
             unittest.mock.patch.object(sim, '_queue_mail_work'), \
             unittest.mock.patch('serve.log_action'):
            sim._deliver_watchdog_event(
                state, {'kind': 'k', 'detail': 'd', 'restarts': 'bad'})
        with unittest.mock.patch.object(sim, '_senior_director',
                                        return_value={'id': 'd1'}), \
             unittest.mock.patch.object(sim, '_deliver_mail'), \
             unittest.mock.patch.object(sim, '_queue_mail_work'), \
             unittest.mock.patch('serve.log_action',
                                 side_effect=RuntimeError('boom')):
            sim._deliver_watchdog_event(
                state, {'kind': 'k', 'detail': 'd', 'restarts': 2})

    def test_assign_player_task_title_and_depends(self):
        state = {'tasks': {}, 'playerInbox': [], 'emailOutbox': []}
        with unittest.mock.patch('serve.log_action'):
            self.assertIsNone(sim._assign_player_task(
                state, {'title': '  '}, 1000, [1]))
            task = sim._assign_player_task(
                state, {'title': 'Do', 'dependsOn': 't0',
                        'instructions': 'i'}, 1000, [2])
        self.assertEqual(task['status'], 'needs_player')
        self.assertEqual(state['tasks'][task['id']]['dependsOn'], 't0')


class SimLoopTest(_Base):
    def test_sim_loop_pass_exception_branches(self):
        state = {'sim': {}, 'agents': {}}
        with unittest.mock.patch('serve._expire_handles'), \
             unittest.mock.patch('serve.expire_sublow_grants'), \
             unittest.mock.patch('serve.expire_jev_grants'), \
             unittest.mock.patch('serve._dormant', return_value=False), \
             unittest.mock.patch('serve._state_begin', return_value=state), \
             unittest.mock.patch('serve._apply_pending_ask_results'), \
             unittest.mock.patch('serve._peer_review_tick'), \
             unittest.mock.patch('serve._adversarial_village_tick',
                                 side_effect=RuntimeError('boom')), \
             unittest.mock.patch.object(sim, '_deliver_watchdog_event',
                                        side_effect=RuntimeError('boom')), \
             unittest.mock.patch.object(sim, '_SIM_WATCHDOG_EVENT',
                                        {'kind': 'k'}), \
             unittest.mock.patch('serve.save_state_to_db'), \
             unittest.mock.patch('serve._state_abort'), \
             unittest.mock.patch.object(sim, '_engine') as engine:
            engine.tick.return_value = state
            out = sim._sim_loop_pass()
        self.assertIs(out, state)

    def test_sim_loop_error_branches(self):
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if calls['n'] == 1:
                return
            if calls['n'] == 2:
                raise RuntimeError('sleep boom')
            raise asyncio.CancelledError()

        async def fake_to_thread(fn, *args, **kwargs):
            raise RuntimeError('thread boom')

        fake_async = unittest.mock.Mock()
        fake_async.sleep = fake_sleep
        fake_async.to_thread = fake_to_thread
        with unittest.mock.patch.object(sim, 'asyncio', fake_async):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sim._sim_loop())
        self.assertGreaterEqual(calls['n'], 3)


class TaskCycleTest(_Base):
    def _cycle_patches(self):
        for name in (
                '_check_schedules', '_governance_pass', '_sweep_stuck_gates',
                '_prune_state_blob', '_reclaim_orphaned_walking_tasks',
                '_stale_work_step', '_coaching_loop_step', '_refinement_step',
                '_breakdown_step', '_retro_step', '_roadmap_step',
                '_consensus_relay_step', '_rule_mine_step',
                '_archive_distill_step', '_success_mine_step',
                '_escalation_step', '_block_step', '_mail_action_step',
                '_park_idle_wanderers', '_auto_close_completed_sprints',
                '_sprint_staffing_step'):
            self._patch_obj(sim, name)

    def _state(self, task, agent=None):
        return {
            'sim': {'owner': 'server'},
            'agents': {'cora': agent or {
                'id': 'cora', 'task': task['id'], 'busy': True,
                'inRoom': 'pressoffice', 'visible': True}},
            'tasks': {task['id']: task},
            'workQueue': [],
            'agentRoster': [{'id': 'cora'}],
        }

    def test_task_cycle_budget_exhausted(self):
        now = 1_700_000_000.0
        task = {'id': 't1', 'title': 'T', 'status': 'working',
                'workUntil': now + 100, 'assignedTo': 'cora',
                '_contentInFlight': True}
        state = self._state(task)
        sim._store_content_result('t1', {
            'budgetExhausted': True, 'note': 'spent',
            'taskSpendUsd': 1.0, 'taskSpendAttempts': 2})
        self._cycle_patches()
        with unittest.mock.patch.object(sim, '_notify_task_budget_exhausted'), \
             unittest.mock.patch.object(sim, 'send_agent_off_duty'), \
             unittest.mock.patch('core.budget.analyze_overrun', return_value={}), \
             unittest.mock.patch('core.budget.evaluate_completed_work',
                                 return_value={}), \
             unittest.mock.patch('core.budget.record_overrun'), \
             unittest.mock.patch('serve.tier_value_ratios',
                                 side_effect=RuntimeError('boom')):
            sim._task_cycle(state, now=now, grid={}, doors={})
        self.assertEqual(state['tasks']['t1']['status'], 'failed')
        self.assertFalse(state['agents']['cora']['busy'])

    def test_task_cycle_grace_log_except(self):
        now = 1_700_000_000.0
        task = {'id': 't1', 'title': 'T', 'status': 'working',
                'workUntil': now - 1, 'assignedTo': 'cora',
                '_contentInFlight': True, '_contentHeartbeat': now - 5,
                '_contentGraceUsed': 0}
        state = self._state(task)
        self._cycle_patches()
        with unittest.mock.patch.object(sim, 'send_agent_off_duty'), \
             unittest.mock.patch('serve.log_action',
                                 side_effect=RuntimeError('boom')), \
             unittest.mock.patch('serve.tier_value_ratios', return_value={}):
            sim._task_cycle(state, now=now, grid={}, doors={})
        self.assertGreater(task.get('_contentGraceUsed') or 0, 0)

    def test_task_cycle_abandoned_log_except(self):
        now = 1_700_000_000.0
        task = {'id': 't1', 'title': 'T', 'status': 'working',
                'workUntil': now - 1, 'assignedTo': 'cora',
                '_contentInFlight': True,
                '_contentHeartbeat': now - 1_000_000,
                '_contentGraceUsed': 0}
        state = self._state(task)
        self._cycle_patches()
        with unittest.mock.patch.object(sim, 'send_agent_off_duty'), \
             unittest.mock.patch.object(sim, 'finish_task'), \
             unittest.mock.patch.object(sim, '_peer_gated_lane',
                                        return_value=False), \
             unittest.mock.patch('serve.log_action',
                                 side_effect=RuntimeError('boom')), \
             unittest.mock.patch('serve.tier_value_ratios', return_value={}):
            sim._task_cycle(state, now=now, grid={}, doors={})
        self.assertEqual(task['status'], 'working')

    def test_task_cycle_resume_after_failed_assignment(self):
        now = 1_700_000_000.0
        now_ms = int(now * 1000)
        tool = {'id': 't0', 'title': 'Busy', 'status': 'working',
                'workUntil': now + 100}
        pick = {'id': 'q1', 'title': 'Due', 'room': 'pressoffice',
                'notBefore': now_ms - 1000}
        state = {
            'sim': {'owner': 'server'},
            'agents': {'cora': {'id': 'cora', 'task': 't0', 'busy': True,
                                '_suspendedTask': {'task': 't0'}}},
            'tasks': {'t0': tool},
            'workQueue': [pick],
            'agentRoster': [{'id': 'cora'}],
        }
        resumed = {}
        self._cycle_patches()
        with unittest.mock.patch.object(sim, '_awake_idle_count',
                                        return_value=0), \
             unittest.mock.patch.object(sim, 'can_activate_another',
                                        return_value=False), \
             unittest.mock.patch.object(sim, 'pick_next_due_index',
                                        return_value=0), \
             unittest.mock.patch.object(sim, '_suspend_busy_agent_for_scheduled',
                                        return_value='cora'), \
             unittest.mock.patch.object(sim, '_assign_due_item',
                                        return_value=None), \
             unittest.mock.patch.object(
                 sim, '_resume_suspended_task',
                 side_effect=lambda *a: resumed.setdefault('called', True)), \
             unittest.mock.patch.object(sim, 'team_policy', return_value=3), \
             unittest.mock.patch.object(sim, '_task_owner_director',
                                        return_value=None), \
             unittest.mock.patch('serve.tier_value_ratios', return_value={}):
            sim._task_cycle(state, now=now, grid={}, doors={})
        self.assertTrue(resumed.get('called'))
        self.assertEqual(state['workQueue'][0]['attempts'], 1)


class ReloadHookTest(_Base):
    def test_sim_reload_hook_guard(self):
        hooks = unittest.mock.Mock()
        hooks.__contains__ = unittest.mock.Mock(
            side_effect=RuntimeError('boom'))
        with unittest.mock.patch.object(config, 'ON_RELOAD_HOOKS', hooks):
            mod = importlib.reload(sim)
        self.assertIsNotNone(mod)

    def test_content_reload_hook_guard(self):
        hooks = unittest.mock.Mock()
        hooks.__contains__ = unittest.mock.Mock(
            side_effect=RuntimeError('boom'))
        with unittest.mock.patch.object(config, 'ON_RELOAD_HOOKS', hooks):
            mod = importlib.reload(content)
        self.assertIsNotNone(mod)


class SimResidualTest(_Base):
    def test_sim_loop_pass_cancel_reraise(self):
        async def fake_sleep(_):
            return

        async def fake_to_thread(fn, *args, **kwargs):
            raise asyncio.CancelledError()

        fake_async = unittest.mock.Mock()
        fake_async.sleep = fake_sleep
        fake_async.to_thread = fake_to_thread
        with unittest.mock.patch.object(sim, 'asyncio', fake_async):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sim._sim_loop())

    def test_sim_loop_drain_cancel_reraise(self):
        calls = {'n': 0}

        async def fake_sleep(_):
            return

        async def fake_to_thread(fn, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return None
            raise asyncio.CancelledError()

        fake_async = unittest.mock.Mock()
        fake_async.sleep = fake_sleep
        fake_async.to_thread = fake_to_thread
        with unittest.mock.patch.object(sim, 'asyncio', fake_async):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sim._sim_loop())

    def test_standing_task_sweep_missing_result(self):
        state = {'standingTasks': [{'taskId': 'st1', 'name': 'ST'}],
                 'workQueue': []}
        fake = unittest.mock.Mock()
        fake.tick_all.return_value = {}
        with unittest.mock.patch.object(sim, '_STANDING_SCHEDULER', fake), \
             unittest.mock.patch.object(sim, 'queue_work'):
            sim._standing_task_sweep(state, time.time(),
                                     int(time.time() * 1000))

    def test_dormant_workers_ceiling_blocks(self):
        state = {'agentRoster': [{'id': 'cora'}],
                 'agents': {'cora': {'offDuty': True}}, 'workQueue': []}
        with unittest.mock.patch.object(sim, 'can_activate_another',
                                        return_value=False):
            self.assertFalse(sim._dormant_workers_will_be_woken(state, 1000))

    def test_dormant_workers_skips_parked(self):
        state = {'agentRoster': [{'id': 'cora'}],
                 'agents': {'cora': {'offDuty': True}},
                 'workQueue': [{'id': 'q', 'notBefore': 0}]}
        with unittest.mock.patch.object(sim, 'can_activate_another',
                                        return_value=True), \
             unittest.mock.patch.object(sim, '_is_parked', return_value=True):
            self.assertFalse(sim._dormant_workers_will_be_woken(state, 1000))

    def test_dormant_workers_wakes_without_idle(self):
        state = {'agentRoster': [{'id': 'cora'}],
                 'agents': {'cora': {'offDuty': True}},
                 'workQueue': [{'id': 'q', 'notBefore': 0}]}
        with unittest.mock.patch.object(sim, 'can_activate_another',
                                        return_value=True), \
             unittest.mock.patch.object(sim, '_is_parked', return_value=False), \
             unittest.mock.patch.object(sim, '_work_item_dependency_met',
                                        return_value=True), \
             unittest.mock.patch.object(sim, '_awake_idle_count',
                                        return_value=0):
            self.assertTrue(sim._dormant_workers_will_be_woken(state, 1000))

    def test_queue_watchdog_alert_requires_list(self):
        self.assertFalse(sim._queue_watchdog_alert(
            {'workQueue': 'nope'}, 'a', {}, 0))

    def test_resolve_refinement_player_pins(self):
        req = {'id': 'r1', 'status': 'pending', 'filedBy': 'cora',
               'room': 'pressoffice', 'title': 'Do X', 'reason': 'why',
               'playerConstraints': {'budgetUsd': 5.0,
                                     'constraints': ['no x'],
                                     'roles': ['dev'],
                                     'mustInclude': ['a']}}
        state = {'agents': {'sm': {'name': 'SM'},
                            'cora': {'name': 'Cora', 'role': 'worker'}},
                 'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                 'backlogRequests': [req], 'tasks': {}, 'workQueue': []}
        pending = {'scrumMasterId': 'sm', 'reqIds': ['r1'], 'teamId': 't1'}
        with unittest.mock.patch.object(sim.comprehension,
                                        'comprehend_batch',
                                        return_value={}), \
             unittest.mock.patch.object(sim, '_overrun_factor_for',
                                        return_value=1.0), \
             unittest.mock.patch.object(sim, 'budget_usd_for_task',
                                        return_value=1.0), \
             unittest.mock.patch.object(sim, '_refinement_context_for_room',
                                        return_value=''), \
             unittest.mock.patch.object(sim, '_reassign_rolled_over_cards'), \
             unittest.mock.patch.object(sim, '_restore_refinement_agent'), \
             unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest'):
            sim._resolve_refinement(
                state, pending, 1000,
                decider=lambda instructions, criteria: 'reject',
                reader=lambda r: None)
        self.assertEqual(req['status'], 'rejected')

    def test_resolve_refinement_appeal_rides_instructions(self):
        req = {'id': 'r1', 'status': 'pending', 'filedBy': 'cora',
               'room': 'pressoffice', 'title': 'Do X', 'reason': 'why',
               'appeal': 'it also breaks on Safari'}
        state = {'agents': {'sm': {'name': 'SM'},
                            'cora': {'name': 'Cora', 'role': 'worker'}},
                 'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                 'backlogRequests': [req], 'tasks': {}, 'workQueue': []}
        pending = {'scrumMasterId': 'sm', 'reqIds': ['r1'], 'teamId': 't1'}
        seen = {}
        with unittest.mock.patch.object(sim.comprehension,
                                        'comprehend_batch',
                                        return_value={}), \
             unittest.mock.patch.object(sim, '_overrun_factor_for',
                                        return_value=1.0), \
             unittest.mock.patch.object(sim, 'budget_usd_for_task',
                                        return_value=1.0), \
             unittest.mock.patch.object(sim, '_refinement_context_for_room',
                                        return_value=''), \
             unittest.mock.patch.object(sim, '_reassign_rolled_over_cards'), \
             unittest.mock.patch.object(sim, '_restore_refinement_agent'), \
             unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest'):
            sim._resolve_refinement(
                state, pending, 1000,
                decider=lambda instructions, criteria: seen.setdefault(
                    'instructions', instructions) or 'reject',
                reader=lambda r: None)
        self.assertIn('APPEAL', seen['instructions'])
        self.assertIn('Safari', seen['instructions'])
        self.assertEqual(req['status'], 'rejected')

    def test_resolve_refinement_pressoffice_spike_is_not_a_room_reject(self):
        req = {'id': 'r1', 'status': 'pending', 'filedBy': 'player',
               'room': 'pressoffice', 'title': '[SPIKE] Review a video transcript',
               'issueType': 'spike', 'reason': 'JIRA issue DEV-99',
               'approvedTools': ['self_edit', 'schedule_dormant_apply']}
        state = {'agents': {'sm': {'name': 'SM'},
                            'player': {'name': 'Player', 'role': 'operator'}},
                 'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                 'backlogRequests': [req], 'tasks': {}, 'workQueue': []}
        pending = {'scrumMasterId': 'sm', 'reqIds': ['r1'], 'teamId': 't1'}
        seen = {}

        def decider(instructions, criteria):
            seen['instructions'] = instructions
            seen['criteria'] = criteria
            return 'accept'
        with unittest.mock.patch.object(sim.comprehension,
                                        'comprehend_batch',
                                        return_value={}), \
             unittest.mock.patch.object(sim, '_overrun_factor_for',
                                        return_value=1.0), \
             unittest.mock.patch.object(sim, 'budget_usd_for_task',
                                        return_value=1.0), \
             unittest.mock.patch.object(sim, '_refinement_context_for_room',
                                        return_value=''), \
             unittest.mock.patch.object(sim, '_reassign_rolled_over_cards'), \
             unittest.mock.patch.object(sim, '_restore_refinement_agent'), \
             unittest.mock.patch.object(sim, '_mail_card_update'), \
             unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest'):
            sim._resolve_refinement(state, pending, 1000, decider=decider,
                                    reader=lambda r: None)
        self.assertIn('Card type: spike', seen['instructions'])
        self.assertIn('room is where the work happens, not a scope gate',
                      seen['instructions'])
        self.assertIn('player explicitly asked', seen['instructions'])
        accept = next(c['description'] for c in seen['criteria'] if c['id'] == 'accept')
        self.assertIn('spike card', accept)
        self.assertEqual(req['status'], 'accepted')
        self.assertEqual(state['workQueue'][-1]['room'], 'pressoffice')
        self.assertEqual(state['workQueue'][-1]['taskType'], 'spike',
                         'the issue type must survive into the queue lane')
        self.assertEqual(state['workQueue'][-1]['approvedTools'],
                         ['self_edit', 'schedule_dormant_apply'])

    def test_resolve_breakdown_never_text(self):
        req = {'id': 'r1', 'status': 'pending', 'filedBy': 'cora',
               'goal': 'G', 'title': 'T', 'room': 'pressoffice'}
        state = {'agents': {'sm': {'name': 'SM'}},
                 'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                 'backlogRequests': [req], 'tasks': {}, 'workQueue': []}
        pending = {'scrumMasterId': 'sm', 'reqIds': ['r1'], 'teamId': 't1'}
        queued = {}
        with unittest.mock.patch.object(
                sim, 'queue_work',
                lambda st, items: queued.setdefault('items', items)), \
             unittest.mock.patch.object(sim, '_overrun_factor_for',
                                        return_value=1.0), \
             unittest.mock.patch.object(sim, 'budget_usd_for_task',
                                        return_value=1.0), \
             unittest.mock.patch.object(sim, '_restore_refinement_agent'), \
             unittest.mock.patch.object(sim, '_log_governance'):
            sim._resolve_breakdown(
                state, pending, 1000,
                decider=lambda st, instructions, goal: {
                    'items': [{'title': 'Card',
                               'never': ['no hacks'],
                               'type': 'story'}]})
        self.assertTrue(queued.get('items'))
