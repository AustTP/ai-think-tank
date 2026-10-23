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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import content  # noqa: E402
import serve  # noqa: E402
import sim  # noqa: E402


def _completion(text):
    return {'choices': [{'message': {'content': text}}], 'usage': {'cost': 0.0}}


def _snapshot():
    return {
        'agentRoster': [{'id': 'cora', 'name': 'Cora'}],
        'agents': {'cora': {'name': 'Cora'}},
    }


class _Base(unittest.TestCase):
    def _patch(self, target, **kwargs):
        p = unittest.mock.patch(target, **kwargs)
        p.start()
        self.addCleanup(p.stop)
        return p

    def _patch_obj(self, obj, attr, **kwargs):
        p = unittest.mock.patch.object(obj, attr, **kwargs)
        p.start()
        self.addCleanup(p.stop)
        return p


class PolicyAndPerceptionTest(_Base):
    def test_team_agreement_skips_non_dict_teams(self):
        self.assertEqual(sim.team_agreement({'teams': ['bogus']}, 't1'), {})

    def test_team_policy_ceiling_type_error_clamps(self):
        with unittest.mock.patch.object(
                config, 'OPERATOR_POLICY', {'maxTeamMembers': None}):
            val = sim.team_policy({}, 't1', 'maxTeamMembers')
        self.assertIsNone(val)

    def test_perception_includes_never_block(self):
        state = {
            'agentRoster': [{'id': 'cora', 'name': 'Cora', 'role': 'Engineer'}],
            'agents': {'cora': {'id': 'cora', 'name': 'Cora',
                                'inRoom': 'pressoffice', 'offDuty': False}},
        }
        block = sim._build_agent_perception(
            state, 'cora',
            {'title': 'Do the thing', 'room': 'pressoffice',
             'never': 'no hacks'})
        self.assertIn('NEVER', block)
        self.assertIn('no hacks', block)

    def test_acquainted_falsy_and_self(self):
        state = {'agentRoster': [{'id': 'cora'}]}
        self.assertFalse(sim._are_acquainted(state, None, 'cora'))
        self.assertFalse(sim._are_acquainted(state, 'cora', None))
        self.assertTrue(sim._are_acquainted(state, 'cora', 'cora'))

    def test_describe_stranger_without_role(self):
        state = {'agentRoster': [{'id': 'bob', 'name': 'Bob'}]}
        self.assertEqual(sim._describe_agent(state, 'bob', 'cora'),
                         'a colleague you have not met')

    def test_reading_lane_busy_branches(self):
        item = {'lane': 'reading', 'room': 'library', 'id': 'i1'}
        state = {'tasks': {'x': 'not-a-dict'}}
        self.assertFalse(sim._reading_lane_busy(state, item))
        state = {'tasks': {'x': {'id': 'x', 'lane': 'build',
                                 'status': 'working', 'room': 'library'}}}
        self.assertFalse(sim._reading_lane_busy(state, item))
        state = {'tasks': {'x': {'id': 'i1', 'lane': 'reading',
                                 'status': 'working', 'room': 'library'}}}
        self.assertFalse(sim._reading_lane_busy(state, item))

    def test_resolve_memory_fields_bad_days(self):
        out = sim.resolve_memory_fields({}, {'reviewAfterDays': 'abc'}, 'cora')
        self.assertIn('review_after_ms', out)
        self.assertEqual(out['owner'], 'cora')


class RosterAndStateTest(_Base):
    def test_roster_view_malformed_teams(self):
        state = {'agentRoster': [{'id': 'cora', 'director': 't1'}],
                 'teams': ['bogus']}
        self.assertIsNotNone(sim.roster_view_from_state(state))

    def test_roster_view_non_dict_entry(self):
        state = {'agentRoster': ['bogus'], 'teams': []}
        self.assertIsNotNone(sim.roster_view_from_state(state))

    def test_roster_view_entry_missing_keys(self):
        state = {'agentRoster': [{'id': 'a'}, {'director': 't1'}],
                 'teams': []}
        self.assertIsNotNone(sim.roster_view_from_state(state))

    def test_is_negative_severity(self):
        for sev in ('SEVERE', 'serious', 'major'):
            self.assertTrue(sim._is_negative_severity(sev))
        self.assertFalse(sim._is_negative_severity(None))
        self.assertFalse(sim._is_negative_severity('minor'))

    def test_normalize_never_block_empty_lines(self):
        self.assertIsNone(sim.normalize_never_block('- \n* \n'))

    def test_overrun_factor_guard(self):
        with unittest.mock.patch('budget.overrun_factor',
                                 side_effect=RuntimeError('boom')):
            self.assertEqual(sim._overrun_factor_for({}, 'room', {}), 1.0)

    def test_apply_content_result_infra_tag(self):
        task = {'id': 't1'}
        state = {'tasks': {'t1': task}}
        sim._apply_content_result(state, task, {'infra': True})
        self.assertTrue(task.get('_infraFailure'))

    def test_append_mailbox_trim(self):
        agent = {'mailbox': [{'i': i} for i in range(200)]}
        sim._append_mailbox(agent, {'i': 999})
        self.assertEqual(len(agent['mailbox']), 200)
        self.assertEqual(agent['mailbox'][-1]['i'], 999)

    def test_queue_player_email_dedup(self):
        state = {}
        first = sim._queue_player_email(
            state, 'kind', 'S', 'B', now_ms=1_700_000_000_000)
        second = sim._queue_player_email(
            state, 'kind', 'S', 'B', now_ms=1_700_000_001_000)
        self.assertIsNotNone(first)
        self.assertIsNone(second)


class GuardsTest(_Base):
    def test_spawn_team_agent_fired(self):
        state = {'agents': {}, 'agentRoster': []}
        with unittest.mock.patch.object(sim, '_is_fired', return_value=True):
            self.assertIsNone(sim._spawn_team_agent(
                state, 'Dana', 'Engineer', 'dir', 1000, {}))

    def test_suspend_busy_guards(self):
        state = {
            'agentRoster': [{'id': f'a{i}'} for i in range(6)],
            'agents': {
                'a0': {'offDuty': True, 'busy': True, 'task': 't0'},
                'a1': {'busy': True, 'pairWith': 'x', 'task': 't1'},
                'a2': {'busy': True, 'task': 't2'},
                'a3': {'busy': True, 'task': 't3'},
                'a4': {'busy': True, 'task': 't4'},
                'a5': {'busy': True, 'task': 't5'},
            },
            'tasks': {
                't0': {'status': 'working'},
                't1': {'status': 'working'},
                't2': {'status': 'done'},
                't3': {'status': 'working', '_inSocial': True},
                't4': {'status': 'working', 'reviewOf': 'p'},
                't5': {'status': 'working'},
            },
        }
        with unittest.mock.patch.object(sim, '_task_is_time_critical',
                                        return_value=True):
            self.assertIsNone(sim._suspend_busy_agent_for_scheduled(
                state, None, None, 1000, skip_time_critical=True))

    def test_resume_suspended_guards(self):
        state = {'tasks': {}}
        self.assertFalse(sim._resume_suspended_task(state, 'a1', 'not-a-dict'))
        self.assertFalse(sim._resume_suspended_task(state, 'a1', {}))
        self.assertFalse(sim._resume_suspended_task(
            state, 'a1', {'_suspendedTask': {'task': 'missing'}}))

    def test_director_grant_guards(self):
        state = {'tasks': {}}
        self.assertEqual(
            sim._director_grant_budget_reopen(state, 'nope', 100)['error'],
            'unknown task')
        state = {'tasks': {'t1': {'status': 'failed',
                                  'budgetExhausted': True}}}
        self.assertIn('positive',
                      sim._director_grant_budget_reopen(state, 't1', 'x')['error'])
        self.assertIn('positive',
                      sim._director_grant_budget_reopen(state, 't1', 0)['error'])

    def test_notify_budget_factor_guard(self):
        state = {'agents': {'d1': {'id': 'd1'}},
                 'agentRoster': [{'id': 'd1', 'isAdmin': True}]}
        task = {'id': 't1', 'title': 'T', 'assignedTo': 'cora'}
        with unittest.mock.patch.object(sim, '_task_owner_director',
                                        return_value='d1'), \
             unittest.mock.patch('budget.overrun_factor',
                                 side_effect=RuntimeError('boom')), \
             unittest.mock.patch('serve.log_action'):
            sim._notify_task_budget_exhausted(
                state, task, spent=1.0, attempts=2,
                analysis={'reason': 'x'}, completed={'landed': True})
        self.assertTrue(task.get('_budgetExhaustedNotified'))

    def test_archive_distill_append_and_error(self):
        now_ms = sim.RULE_MINE_CADENCE_MS + 1_000_000
        state = {'lastArchiveDistillAt': 0}
        summary = {'total': 1, 'kinds': [
            {'kind': 'k', 'count': 1, 'ok': 1, 'cost': 0.0}],
            'failed': 0, 'cost': 0.0}
        with unittest.mock.patch('serve._decision_archive_summary',
                                 return_value=summary), \
             unittest.mock.patch('serve._wiki_page_body',
                                 return_value='## old\nbody'), \
             unittest.mock.patch('serve._write_wiki_server') as write:
            sim._archive_distill_step(state, now_ms)
        self.assertTrue(write.called)
        state = {'lastArchiveDistillAt': 0}
        with unittest.mock.patch('serve._decision_archive_summary',
                                 return_value=summary), \
             unittest.mock.patch('serve._wiki_page_body', return_value=''), \
             unittest.mock.patch('serve._write_wiki_server',
                                 side_effect=RuntimeError('boom')):
            sim._archive_distill_step(state, now_ms)

    def test_charter_alignment_empty_charter(self):
        with unittest.mock.patch.object(
                sim, 'get_charter', return_value={'goal': '', 'interests': []}):
            self.assertFalse(sim._charter_alignment({}, 'pressoffice'))

    def test_long_horizon_env_error(self):
        state = {'villages': [{'id': 'v1'}]}
        with unittest.mock.patch('serve._load_env',
                                 side_effect=RuntimeError('boom')):
            sim._ensure_long_horizon_topic(state, int(time.time() * 1000))

    def test_task_owner_director_product(self):
        state = {'agents': {}, 'agentRoster': []}
        with unittest.mock.patch.object(sim, '_product_director',
                                        return_value='d1'):
            self.assertEqual(
                sim._task_owner_director(state, {'productId': 'p1'}), 'd1')


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
             unittest.mock.patch('budget.analyze_overrun', return_value={}), \
             unittest.mock.patch('budget.evaluate_completed_work',
                                 return_value={}), \
             unittest.mock.patch('budget.record_overrun'), \
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


BUDGET = {'budgetExhausted': True, 'taskSpendUsd': 1.0,
          'taskSpendAttempts': 2}


def _http_router(responses):
    side = list(responses)

    def _fn(method, base, path, body=None, header=None, timeout=30):
        if side:
            return side.pop(0)
        return {}
    return _fn


class ContentBudgetTest(_Base):
    def test_record_source_evidence_except(self):
        with unittest.mock.patch('serve._http_json',
                                 side_effect=RuntimeError('boom')):
            out = content._record_source_evidence(
                'http://x', 'k', 'cora', 'u', 'ru', 'text', 0, 'tp', 't1')
        self.assertTrue(out.get('recorded'))

    def test_note_research_coverage_failure_swallowed(self):
        with unittest.mock.patch('serve._http_json',
                                 side_effect=RuntimeError('boom')):
            content._note_research_coverage('http://x', 'k', 'cora', 'ident',
                                            'artifact')
            content._note_research_failure('http://x', 'k', 'cora', 'ident',
                                           'why')

    def test_note_research_failure_records_when_server_responds(self):
        with unittest.mock.patch('serve._http_json',
                                 return_value={'ok': True}):
            content._note_research_failure('http://x', 'k', 'cora', 'ident',
                                           'why')

    def test_research_chat_budget(self):
        task = {'id': 't1', 'research': {'topicId': 'rt1', 'since': 0}}
        snap = {'researchTopics': [{'id': 'rt1', 'topic': 'T',
                                    'startUrl': 'https://example.com',
                                    'seenUrls': []}],
                'agents': {}, 'agentRoster': []}
        def _router(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return BUDGET
            if path == '/api/browse':
                return {'allowed': True, 'text': 'keyword text',
                        'links': []}
            if path == '/api/evidence':
                return {'recorded': True}
            if path == '/api/sandbox-save-page':
                return {'allowed': True, 'ok': True}
            return {}
        with unittest.mock.patch('serve._http_json',
                                 side_effect=_router), \
             unittest.mock.patch('serve._resolve_model_tier',
                                 return_value='mid'), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_research_content(snap, 'cora', task)
        self.assertTrue(store.called)
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def test_media_chat_budget(self):
        def _router(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return BUDGET
            if path == '/api/browse':
                return {'allowed': True, 'text': 'long text ' * 50}
            return {}
        with unittest.mock.patch('serve._http_json',
                                 side_effect=_router), \
             unittest.mock.patch('content._parse_feed_urls_after',
                                 return_value=['https://feed']), \
             unittest.mock.patch('serve._resolve_model_tier',
                                 return_value='mid'), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_media_content(_snapshot(), 'cora',
                                       {'id': 't1', 'room': 'media'})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def test_distill_chat_budget(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, 'a.md'), 'w') as f:
                f.write('# finding\n')
            with unittest.mock.patch.object(serve, 'LIBRARY_ARCHIVE_DIR', td), \
                 unittest.mock.patch.object(serve, 'LIBRARY_DIR',
                                            os.path.join(td, 'none')), \
                 unittest.mock.patch('serve._http_json',
                                     return_value=BUDGET), \
                 unittest.mock.patch('serve._resolve_model_tier',
                                     return_value='mid'), \
                 unittest.mock.patch('serve.get_or_create_agent_key',
                                     return_value='k'), \
                 unittest.mock.patch('sim._store_content_result') as store:
                content._run_distill_content(
                    _snapshot(), 'cora', {'id': 't1', 'distillSince': 0})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def test_dispatcher_training_capture(self):
        cap = unittest.mock.Mock()
        cap.enabled.return_value = True
        cap.metadata.return_value = {}
        cap.CURRENT.set.return_value = 'tok'
        with unittest.mock.patch.object(content, 'training_capture', cap), \
             unittest.mock.patch.object(content, '_run_observatory_content') as run:
            content._server_content_dispatcher(
                _snapshot(), 'cora', {'id': 't1', 'room': 'observatory'})
        self.assertTrue(run.called)
        self.assertTrue(cap.CURRENT.set.called)
        self.assertTrue(cap.CURRENT.reset.called)

    def test_research_project_chat_budget(self):
        with unittest.mock.patch('serve._resolve_model_tier',
                                 return_value='mid'), \
             unittest.mock.patch('serve._http_json', return_value=BUDGET), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_research_project_content(
                _snapshot(), 'cora',
                {'id': 't1', 'title': 'T', 'projectLabel': 'P'})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))


class ContentHelperTest(_Base):
    def test_review_screenshot_budget(self):
        with unittest.mock.patch('serve._http_json',
                                 side_effect=[{'imageBase64': 'x'},
                                              BUDGET]), \
             unittest.mock.patch('serve._vision_tier_slug',
                                 return_value='v'):
            out = content._review_screenshot(
                'b', 'k', 'cora', 'sb', 'index.html', 'q')
        self.assertTrue(out.get('budgetExhausted'))

    def test_quality_snippet_bandit_cap(self):
        stdout = '\n'.join(
            f'>> Issue: [B{i}] thing\n  detail\nLocation: ./f.py:{i}'
            for i in range(7))
        out = content._quality_pipeline_failure_snippet(
            'bandit', stdout, '')
        self.assertIn('Location', out)

    def test_quality_evidence(self):
        out = content._quality_pipeline_evidence({'results': [
            {'name': 'flake8', 'stdout': 'a\nb\nc', 'stderr': 'e'},
            {'name': 'empty', 'stdout': '', 'stderr': ''}]})
        self.assertIn('[flake8]', out)
        self.assertNotIn('[empty]', out)

    def test_list_handles(self):
        handles = [{'credentialName': 'c', 'purpose': 'p',
                    'allowedHosts': ['a.com', 'b.com'],
                    'allowedMethods': ['GET'], 'expiresInSec': 60,
                    'handle': 'h1'}]
        with unittest.mock.patch('serve._http_json',
                                 return_value={'handles': handles}):
            ex = content._make_list_handles_executor('cora', 'k')
            out = ex('list_capability_handles', {})
        self.assertIn('a.com, b.com', out)
        with unittest.mock.patch('serve._http_json',
                                 return_value='not-a-dict'):
            self.assertIn('unexpected', ex('list_capability_handles', {}))
        with unittest.mock.patch('serve._http_json',
                                 return_value={'error': 'boom'}):
            self.assertIn('boom', ex('list_capability_handles', {}))

    def test_browser_act_branches(self):
        ex = content._make_browser_act_executor('cora', 'k', 'sb')
        with unittest.mock.patch('serve._http_json',
                                 return_value='not-a-dict'):
            self.assertIn('unexpected', ex('browser_act', {}))
        with unittest.mock.patch('serve._http_json',
                                 return_value={'allowed': True,
                                               'error': 'boom'}):
            self.assertIn('failed', ex('browser_act', {}))
        with unittest.mock.patch('serve._http_json',
                                 return_value={'allowed': True, 'ok': False}):
            self.assertIn('sandbox', ex('browser_act', {}))
        with unittest.mock.patch('serve._http_json',
                                 return_value={
                                     'allowed': True, 'ok': True, 'url': 'u',
                                     'fields': [{'tag': 'input'}],
                                     'links': [{'text': 't', 'href': 'h'}],
                                     'screenshotBase64': 'x'}), \
             unittest.mock.patch('serve.wrap_external_content',
                                 return_value=('w', 'n', 't', 'instr')):
            out = ex('browser_act', {'op': 'click', 'selector': '#a',
                                     'credentialHandle': 'hh',
                                     'credentialField': 'pw',
                                     'pageUrl': 'u'})
        self.assertIsInstance(out, str)

    def test_library_tools(self):
        ex = content._make_library_tools_executor('cora')
        with unittest.mock.patch('serve._requester_villages',
                                 return_value=None), \
             unittest.mock.patch('serve._library_search_matches',
                                 return_value=[{'path': 'p', 'size': 1,
                                                'snippet': 's'}]), \
             unittest.mock.patch('serve.log_action'):
            out = ex('search_library', {'query': 'q'})
        self.assertIn('p', out)
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve._safe_library_path',
                                 return_value=None):
            out = ex('read_library_file',
                     {'path': 'p', 'offset': 'abc', 'limit': 'xyz'})
        self.assertIn('Not found', out)

    def test_skill_publisher_http_error(self):
        ex = content._make_skill_publisher('cora', 'k')
        args = {'slug': 'my-skill', 'title': 'T', 'sourceTask': 'st',
                'interface': 'i',
                'body': ('## Purpose\np\n## Key facts\nf\n'
                         '## Sources\ns\n## Lessons learned\nl')}
        with unittest.mock.patch('serve._http_json',
                                 side_effect=RuntimeError('boom')), \
             unittest.mock.patch('serve.log_action'):
            out = ex('publish_skill', args)
        self.assertIn('failed', out)

    def test_skill_publisher_unknown_tool_raises(self):
        ex = content._make_skill_publisher('cora', 'k')
        with self.assertRaises(ValueError):
            ex('some_other_tool', {})

    def test_google_tools(self):
        ex = content._make_google_tools_executor()
        with unittest.mock.patch('serve.wrap_external_content',
                                 return_value=('w', 'n', 't', 'instr')):
            with unittest.mock.patch(
                    'serve._google_call',
                    side_effect=[({'messages': [{'id': 'm1'}]}, None),
                                 ({}, 'err')]):
                out = ex('search_gmail_messages', {'query': 'q'})
            self.assertIsInstance(out, str)
            with unittest.mock.patch('serve._google_call',
                                     return_value=({}, 'err')):
                self.assertIn('Could not read the message',
                              ex('read_gmail_message',
                                 {'message_id': 'm1'}))
            with unittest.mock.patch(
                    'serve._google_call',
                    return_value=({'payload': {'parts': [
                        {'mimeType': 'text/plain',
                         'body': {'data': 'aGk='}}]}}, None)):
                self.assertIsInstance(ex('read_gmail_message',
                                         {'message_id': 'm1'}), str)
            with unittest.mock.patch('serve._google_call',
                                     return_value=({}, 'err')):
                self.assertIn('Could not create the draft',
                              ex('create_gmail_draft',
                                 {'to': 'a', 'subject': 's'}))
                self.assertIn('Could not read the doc',
                              ex('read_google_doc',
                                 {'document_id': 'd'}))
                self.assertIn('Could not create the doc',
                              ex('create_google_doc', {'title': 't'}))

    def test_gmail_decode_bad(self):
        self.assertEqual(content._gmail_base64url_decode('abc'), '')

    def test_spike_budget_gate(self):
        with unittest.mock.patch('serve._task_budget_exhausted',
                                 return_value=True), \
             unittest.mock.patch('serve._task_budget_spent',
                                 return_value=(1.0, 2)), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_spike_content(
                _snapshot(), 'cora', {'id': 't1', 'title': 'T'})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))


class CodingAndReviewTest(_Base):
    def _coding(self, task, responses, **extra):
        with unittest.mock.patch('serve._http_json',
                                 side_effect=_http_router(responses)), \
             unittest.mock.patch.object(content, '_review_sandbox_id',
                                        return_value='sb'), \
             unittest.mock.patch('serve._coding_tier_slug',
                                 return_value='code'), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_coding_content(_snapshot(), 'cora', task)
        return store

    def test_coding_exit_nonzero_detail(self):
        cmd = "cat > index.html << 'EOF'\n<html></html>\nEOF"
        store = self._coding(
            {'id': 't1', 'title': 'T', 'projectLabel': 'P'},
            [{'reply': cmd},
             {'allowed': True, 'exitCode': 1, 'timedOut': False,
              'stderr': 'err line\n', 'stdout': 'out line\n'}])
        result = store.call_args[0][1]
        self.assertFalse(result['ok'])
        self.assertIn('stderr:', result['note'])
        self.assertIn('stdout:', result['note'])

    def test_coding_autolink_budget(self):
        cmd = "cat > app.js << 'EOF'\nconsole.log(1)\nEOF"
        with unittest.mock.patch('serve._http_json',
                                 side_effect=_http_router(
                                     [{'reply': cmd},
                                      {'allowed': True, 'exitCode': 0}])), \
             unittest.mock.patch.object(content, '_review_sandbox_id',
                                        return_value='sb'), \
             unittest.mock.patch('serve._coding_tier_slug',
                                 return_value='code'), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch.object(content, '_extract_written_js_files',
                                        return_value=['app.js']), \
             unittest.mock.patch.object(content,
                                        '_extract_written_python_files',
                                        return_value=[]), \
             unittest.mock.patch.object(content, '_find_unlinked_js_files',
                                        return_value=['app.js']), \
             unittest.mock.patch.object(
                 content, '_auto_link_js_files',
                 return_value={'ok': False, 'budgetExhausted': True,
                               'taskSpendUsd': 1.0,
                               'taskSpendAttempts': 2,
                               'note': 'paused'}), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_coding_content(
                _snapshot(), 'cora',
                {'id': 't1', 'title': 'T', 'projectLabel': 'P'})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def _review(self, responses, task=None, screenshot=None):
        task = task or {'id': 't1', 'title': 'T'}
        patches = [
            unittest.mock.patch('serve._http_json',
                                side_effect=_http_router(responses)),
            unittest.mock.patch.object(content, '_review_sandbox_id',
                                       return_value='sb'),
            unittest.mock.patch.object(content, '_gather_unified_context',
                                       return_value='ctx'),
            unittest.mock.patch.object(content, '_agent_name',
                                       return_value='Cora'),
            unittest.mock.patch('serve._coding_tier_slug',
                                return_value='tier'),
            unittest.mock.patch('serve._vision_tier_slug',
                                return_value='v'),
            unittest.mock.patch('serve.get_or_create_agent_key',
                                return_value='k'),
            unittest.mock.patch('serve._jev_model', return_value='m'),
            unittest.mock.patch('serve._call_openrouter_decision_sync',
                                return_value={'choice': {'id': 'clean'}}),
            unittest.mock.patch('serve._jev_choice',
                                return_value=('clean', None, None)),
        ]
        if screenshot is not None:
            patches.append(unittest.mock.patch.object(
                content, '_review_screenshot', return_value=screenshot))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        content._run_review_content(_snapshot(), 'cora', task)

    def test_review_structured_sendback(self):
        verdict = ('Bad. {"verdict": "send_back", "summary": "s", '
                   '"checks": ["c"], "risks": ["r"]}')
        store = sim._store_content_result
        with unittest.mock.patch('sim._store_content_result') as store:
            self._review([{'reply': verdict}, {'imageBase64': 'x'},
                          {'reply': 'visual'}, {}])
        result = store.call_args[0][1]
        self.assertEqual(result['peerSummary'], 's')
        self.assertEqual(result['peerChecks'], ['c'])
        self.assertEqual(result['peerRisks'], ['r'])
        self.assertIn('queued a fix', result['note'])

    def test_review_chat_budget_stores_budget_result(self):
        with unittest.mock.patch('sim._store_content_result') as store:
            self._review([{'budgetExhausted': True, 'taskSpendUsd': 1.0,
                           'taskSpendAttempts': 2}])
        self.assertTrue(store.called)
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def test_review_visual_budget(self):
        with unittest.mock.patch('sim._store_content_result') as store:
            self._review(
                [{'reply': 'Looks ok.'}, {}],
                screenshot={'ok': False, 'budgetExhausted': True,
                            'taskSpendUsd': 1.0, 'taskSpendAttempts': 2,
                            'note': 'b'})
        result = store.call_args[0][1]
        self.assertTrue(result.get('budgetExhausted'))


class SpikeContentTest(_Base):
    def _run_spike(self, task_title='what should we explore next?',
                   extra_patches=None, http=None):
        task = {'id': 't1', 'title': task_title}
        captured = {}

        def fake_loop(model, messages, tools, execute_tool, **kwargs):
            captured['execute_tool'] = execute_tool
            captured['force_first_tool'] = kwargs.get('force_first_tool')
            transcript = list(messages) + [
                {'role': 'assistant',
                 'tool_calls': [{'id': 'c1', 'function': {
                     'name': 'browse_page', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'c1',
                 'content': 'PAGE TEXT'},
            ]
            return ('done', transcript)

        patches = [
            unittest.mock.patch.object(content, '_spike_file_issue_wish',
                                       return_value=None),
            unittest.mock.patch('serve.get_or_create_agent_key',
                                return_value='k'),
            unittest.mock.patch('serve._resolve_model_tier',
                                return_value='mid'),
            unittest.mock.patch('serve._task_budget_exhausted',
                                return_value=False),
            unittest.mock.patch('serve._call_agent_tool_loop',
                                side_effect=fake_loop),
            unittest.mock.patch('serve._http_json',
                                side_effect=http or (lambda *a, **k: {})),
            unittest.mock.patch('serve.wrap_external_content',
                                return_value=('w', 'n', 't', 'instr')),
            unittest.mock.patch('serve.log_action'),
            unittest.mock.patch('serve._local_file_tool',
                                return_value='ok'),
            unittest.mock.patch('serve._read_store_tool', return_value='ok'),
            unittest.mock.patch('serve._app_script_tool', return_value='ok'),
            unittest.mock.patch.object(sim, 'village_of_agent',
                                       return_value='main'),
            unittest.mock.patch('serve._call_openrouter_sync',
                                return_value=_completion('final')),
            unittest.mock.patch('sim._store_content_result'),
        ]
        patches.extend(extra_patches or [])
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        content._run_spike_content(_snapshot(), 'cora', task)
        return captured

    def test_execute_tool_dispatch_arms(self):
        captured = self._run_spike()
        ex = captured['execute_tool']
        ex('browser_act', {'op': 'navigate', 'url': 'u'})
        ex('list_capability_handles', {})
        ex('publish_skill', {})
        ex('write_spike_report', {'summary': 's'})
        ex('local_file', {'path': 'x'})
        ex('read_store', {'path': 'y'})
        ex('app_script', {'code': 'z'})

    def test_first_tool_ais_and_market(self):
        names = ('_spike_wants_internal_review', '_spike_wants_x_trending',
                 '_spike_wants_linkedin_search', '_spike_wants_github',
                 '_spike_wants_ais_feed', '_spike_wants_rss',
                 '_spike_wants_market')
        for target in ('_spike_wants_ais_feed', '_spike_wants_market'):
            patches = [unittest.mock.patch.object(
                content, n, return_value=(n == target)) for n in names]
            self._run_spike(extra_patches=patches)

    def test_epilogue_probe_error(self):
        self._run_spike(extra_patches=[
            unittest.mock.patch('serve._safe_library_path',
                                side_effect=RuntimeError('boom'))])

    def test_workroom_context_paths(self):
        task = {'id': 't1', 'title': 'T', 'projectLabel': 'P',
                'instructions': None}
        with unittest.mock.patch('serve._http_json',
                                 return_value={'allowed': True,
                                               'stdout': 'files'}), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch.object(content, '_review_sandbox_id',
                                        return_value='sb'), \
             unittest.mock.patch.object(content, '_design_context_for_task',
                                        return_value='design'), \
             unittest.mock.patch.object(content, '_run_coding_content') as run:
            content._run_workroom_content(
                _snapshot(), 'cora', task, base_ctx='ctx')
        self.assertTrue(run.called)
        self.assertTrue(run.call_args.kwargs['base_ctx'].startswith('design'))


if __name__ == "__main__":
    unittest.main()
