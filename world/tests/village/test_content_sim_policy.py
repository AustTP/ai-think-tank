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
        with unittest.mock.patch('core.budget.overrun_factor',
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
             unittest.mock.patch('core.budget.overrun_factor',
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
