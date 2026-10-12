import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import recovery_control
import request_budget
import staged_work
import serve
import content
import sim


def seed():
    return {'tasks': {'p': {'id': 'p', 'requestId': 'r'},
                     'fix': {'id': 'fix', 'reviewOf': 'p'},
                     'review': {'id': 'review', 'reviewOf': 'p'}},
            'backlogRequests': [{'id': 'r', 'requestBudgetUsd': 1}], 'workQueue': []}


class StagedChanges(unittest.TestCase):
    def test_failed_stage_cannot_change_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'project');path.mkdir()
            (path / 'app.py').write_text('x = 1')
            stage = staged_work.Stage(directory, 'project')
            (stage.directory / 'app.py').write_text('def incomplete')
            self.assertEqual((path / 'app.py').read_text(), 'x = 1')

    def test_publish_keeps_previous_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'project');path.mkdir()
            (path / 'app.py').write_text('x = 1')
            stage = staged_work.Stage(directory, 'project')
            (stage.directory / 'app.py').write_text('x = 2')
            previous = stage.publish()
            self.assertEqual((path / 'app.py').read_text(), 'x = 2')
            self.assertEqual(Path(previous, 'app.py').read_text(), 'x = 1')

    def test_concurrent_change_blocks_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'project');path.mkdir()
            (path / 'app.py').write_text('x = 1')
            stage = staged_work.Stage(directory, 'project')
            (path / 'app.py').write_text('x = 3')
            with self.assertRaises(ValueError):stage.publish()
            self.assertEqual((path / 'app.py').read_text(), 'x = 3')

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):staged_work.Stage(directory, '../outside')

    def test_executor_does_not_publish_failed_quality_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'project');path.mkdir()
            (path / 'app.py').write_text('x = 1')
            def generate(*args, **kwargs):
                Path(directory, kwargs['sandbox_override'], 'app.py').write_text('x = 2')
                kwargs['result_sink']('t', {'ok': False, 'pipelineOk': False, 'note': 'mypy failed'})
                return False
            with patch('code_workspaces.prepare', return_value='project'), \
                 patch.object(serve, 'SANDBOXES_DIR', directory), \
                 patch.object(content, '_run_coding_content_inner', side_effect=generate), \
                 patch.object(sim, '_store_content_result') as result:
                self.assertFalse(content._run_coding_content({}, 'agent', {'id': 't'}))
                self.assertEqual((path / 'app.py').read_text(), 'x = 1')
                self.assertIn('preserved', result.call_args.args[1]['note'])

    def test_executor_publishes_before_reporting_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'project');path.mkdir()
            (path / 'app.py').write_text('x = 1')
            def generate(*args, **kwargs):
                Path(directory, kwargs['sandbox_override'], 'app.py').write_text('x = 2')
                kwargs['result_sink']('t', {'ok': True, 'pipelineOk': True, 'note': 'all checks pass'})
                return True
            def record(task_id, result):
                self.assertEqual((path / 'app.py').read_text(), 'x = 2')
                self.assertTrue(result['ok'])
            with patch('code_workspaces.prepare', return_value='project'), \
                 patch.object(serve, 'SANDBOXES_DIR', directory), \
                 patch.object(content, '_run_coding_content_inner', side_effect=generate), \
                 patch.object(sim, '_store_content_result', side_effect=record):
                self.assertTrue(content._run_coding_content({}, 'agent', {'id': 't'}))


class Recovery(unittest.TestCase):
    def test_gate_reset_does_not_reset_story_retries(self):
        state = seed()
        self.assertFalse(recovery_control.record(state, state['tasks']['fix'], 'wrong return'))
        state['tasks']['p']['_peerGate'] = {}
        self.assertTrue(recovery_control.record(state, state['tasks']['review'], 'different wrong return'))
        self.assertEqual(state['tasks']['p']['recoveryLedger']['attempts'], 2)
        self.assertTrue(state['tasks']['p']['manualAttention'])

    def test_policy_denial_pauses_instead_of_code_repair(self):
        state = seed()
        self.assertTrue(recovery_control.record(state, state['tasks']['fix'], 'blocked: unsure'))
        self.assertEqual(state['tasks']['p']['failureCategory'], 'policy')

    def test_human_decision_preserves_history_and_grants_one_attempt(self):
        state = seed();parent = state['tasks']['p']
        recovery_control.record(state, parent, 'wrong result')
        recovery_control.record(state, parent, 'wrong result')
        recovery_control.authorize(parent, 'Fix this specific issue')
        self.assertEqual(parent['recoveryLedger']['attempts'], 2)
        self.assertTrue(recovery_control.record(state, parent, 'another wrong result'))
        self.assertEqual(len(parent['recoveryLedger']['history']), 3)


class RequestBudgets(unittest.IsolatedAsyncioTestCase):
    async def test_children_of_one_request_are_serialized(self):
        state = seed();active = 0;maximum = 0
        async def call(task_id):
            nonlocal active, maximum
            async with request_budget.guard(state, task_id):
                active += 1;maximum = max(maximum, active)
                await asyncio.sleep(.01)
                active -= 1
        await asyncio.gather(call('fix'), call('review'))
        self.assertEqual(maximum, 1)

    def test_request_total_limits_next_response(self):
        state = seed()
        ledger = {'__task__/p': {'used': .2}, '__task__/fix': {'used': .3}, '__task__/review': {'used': .1}}
        limit = request_budget.allowance(state, ledger, 'review', [], 1000, (0, .001))
        self.assertLessEqual(limit, 400)

    def test_blank_budget_does_not_limit_response(self):
        state = seed();state['backlogRequests'][0]['requestBudgetUsd'] = None
        self.assertEqual(request_budget.allowance(state, {}, 'fix', [], 1000, None), 1000)

    def test_unknown_prices_cannot_spend_a_bounded_request(self):
        with self.assertRaises(ValueError):request_budget.allowance(seed(), {}, 'fix', [], 1000, None)
