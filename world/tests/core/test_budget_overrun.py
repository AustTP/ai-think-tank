"""Budget overrun intelligence (world/budget.py) and its wiring into
the sim's budgetExhausted branch: cause classification, completed-work
evaluation, the durable overrun history, the future-budget factor, the
enriched director notification, and the factor's effect on
budget_usd_for_task.

Hermetic: budget.py is pure; the sim notification test patches
sim._log_governance so no DB is ever touched.
"""

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.budget as budget  # noqa: E402
import sim  # noqa: E402


def _task(**over):
    task = {
        'id': 'task-1',
        'title': 'Build the thing',
        'room': 'pressoffice',
        'teamId': 'dev',
        'taskType': 'code',
        'assignedTo': 'ben',
        'budgetUsd': 0.50,
        'budgetBand': 'standard',
        'status': 'working',
    }
    task.update(over)
    return task


def _state():
    return {
        'agents': {'nora': {'id': 'nora', 'mailbox': []},
                   'ben': {'id': 'ben', 'mailbox': []}},
        'teams': [{'id': 'dev', 'directorId': 'nora'}],
        'tasks': {},
    }


class AnalyzeOverrun(unittest.TestCase):
    def test_attempts_heavy_overrun(self):
        out = budget.analyze_overrun(_task(), 0.50, 12)
        self.assertEqual(out['reason'], 'attempts')
        self.assertAlmostEqual(out['perCallUsd'], 0.50 / 12, places=5)
        self.assertAlmostEqual(out['overrunRatio'], 1.0)
        self.assertIn('retry/loop churn', out['detail'])

    def test_tier_heavy_overrun(self):
        out = budget.analyze_overrun(_task(), 0.50, 2)
        self.assertEqual(out['reason'], 'tier')
        self.assertAlmostEqual(out['perCallUsd'], 0.25)
        self.assertIn('expensive tier', out['detail'])

    def test_scope_overrun(self):
        out = budget.analyze_overrun(_task(), 0.50, 4)
        self.assertEqual(out['reason'], 'scope')
        self.assertIn('needed more than the ceiling', out['detail'])

    def test_missing_telemetry_is_scope(self):
        out = budget.analyze_overrun(_task(), None, None)
        self.assertEqual(out['reason'], 'scope')
        self.assertEqual(out['perCallUsd'], 0.0)
        self.assertIsNone(out['overrunRatio'])

    def test_missing_ceiling_is_scope_without_ratio(self):
        out = budget.analyze_overrun(_task(budgetUsd=None), 0.50, 2)
        self.assertEqual(out['reason'], 'scope')
        self.assertIsNone(out['overrunRatio'])

    def test_overrun_ratio_above_one(self):
        out = budget.analyze_overrun(_task(), 0.60, 20)
        self.assertAlmostEqual(out['overrunRatio'], 1.2)


class EvaluateCompletedWork(unittest.TestCase):
    def test_note_lands_as_partial(self):
        out = budget.evaluate_completed_work(_task(note='  shipped the fix  '))
        self.assertTrue(out['landed'])
        self.assertEqual(out['verdict'], 'partial')
        self.assertEqual(out['evidence'], 'shipped the fix')

    def test_empty_card_reports_empty(self):
        out = budget.evaluate_completed_work(_task())
        self.assertFalse(out['landed'])
        self.assertEqual(out['verdict'], 'empty')
        self.assertEqual(out['evidence'], '')

    def test_review_verdict_counts_as_landed(self):
        out = budget.evaluate_completed_work(_task(peerVerdict='approve'))
        self.assertTrue(out['landed'])
        self.assertEqual(out['verdict'], 'partial')


class RecordOverrun(unittest.TestCase):
    def test_appends_record_and_stamps_task(self):
        state = _state()
        task = _task()
        analysis = budget.analyze_overrun(task, 0.50, 12)
        completed = budget.evaluate_completed_work(task)
        record = budget.record_overrun(state, task, 0.50, 12,
                                       analysis=analysis, completed=completed)
        self.assertEqual(len(state['budgetOverruns']), 1)
        self.assertEqual(record['taskId'], 'task-1')
        self.assertEqual(record['taskType'], 'code')
        self.assertEqual(record['budgetUsd'], 0.50)
        self.assertEqual(record['spentUsd'], 0.50)
        self.assertEqual(record['attempts'], 12)
        self.assertEqual(record['reason'], 'attempts')
        self.assertEqual(record['room'], 'pressoffice')
        self.assertEqual(record['teamId'], 'dev')
        self.assertEqual(record['assignedTo'], 'ben')
        self.assertFalse(record['workLanded'])
        self.assertEqual(task['overrunReason'], 'attempts')
        self.assertEqual(task['overrunRatio'], 1.0)
        self.assertFalse(task['workLanded'])

    def test_history_is_capped(self):
        state = _state()
        state['budgetOverruns'] = [{'taskId': f'old-{i}'} for i in range(499)]
        for i in range(2):
            budget.record_overrun(state, _task(id=f'new-{i}'))
        self.assertEqual(len(state['budgetOverruns']),
                         budget.OVERRUN_HISTORY_MAX)
        self.assertEqual(state['budgetOverruns'][0]['taskId'], 'old-1')
        self.assertEqual(state['budgetOverruns'][-1]['taskId'], 'new-1')


class OverrunFactor(unittest.TestCase):
    def test_empty_history_is_one(self):
        self.assertEqual(budget.overrun_factor(_state(), room='pressoffice'), 1.0)

    def test_room_scoped_history_weights_attempts(self):
        state = _state()
        state['budgetOverruns'] = [{
            'room': 'pressoffice', 'teamId': 'dev', 'taskType': 'code',
            'overrunRatio': 1.0, 'reason': 'attempts'}]
        self.assertAlmostEqual(
            budget.overrun_factor(state, room='pressoffice',
                                  team_id='dev', task_type='code'), 1.25)

    def test_widens_from_room_to_team(self):
        state = _state()
        state['budgetOverruns'] = [{
            'room': 'observatory', 'teamId': 'dev', 'taskType': 'code',
            'overrunRatio': 1.0, 'reason': 'attempts'}]
        self.assertAlmostEqual(
            budget.overrun_factor(state, room='pressoffice',
                                  team_id='dev', task_type='code'), 1.25)

    def test_filters_by_task_type(self):
        state = _state()
        state['budgetOverruns'] = [
            {'room': 'pressoffice', 'taskType': 'code',
             'overrunRatio': 1.0, 'reason': 'attempts'},
            {'room': 'pressoffice', 'taskType': 'research',
             'overrunRatio': 1.0, 'reason': 'attempts'}]
        self.assertAlmostEqual(
            budget.overrun_factor(state, room='pressoffice', task_type='code'),
            1.25)
        self.assertEqual(
            budget.overrun_factor(state, room='pressoffice', task_type='qa'),
            1.0)

    def test_clamped_to_max(self):
        state = _state()
        state['budgetOverruns'] = [{
            'room': 'pressoffice', 'taskType': 'code',
            'overrunRatio': 5.0, 'reason': 'scope'}]
        self.assertEqual(
            budget.overrun_factor(state, room='pressoffice', task_type='code'),
            budget.OVERRUN_FACTOR_MAX)

    def test_floored_at_one(self):
        state = _state()
        state['budgetOverruns'] = [{
            'room': 'pressoffice', 'taskType': 'code',
            'overrunRatio': 0.9, 'reason': 'scope'}]
        self.assertEqual(
            budget.overrun_factor(state, room='pressoffice', task_type='code'),
            1.0)

    def test_median_of_even_history(self):
        state = _state()
        state['budgetOverruns'] = [
            {'room': 'pressoffice', 'taskType': 'code',
             'overrunRatio': 1.0, 'reason': 'scope'},
            {'room': 'pressoffice', 'taskType': 'code',
             'overrunRatio': 2.0, 'reason': 'scope'}]
        self.assertAlmostEqual(
            budget.overrun_factor(state, room='pressoffice', task_type='code'),
            1.5)


class OverrunFactorScoping(unittest.TestCase):
    def test_product_scope_scales_similar_work(self):
        state = _state()
        state['budgetOverruns'] = [{
            'room': 'observatory', 'productId': 'prod-1',
            'taskType': 'code', 'overrunRatio': 1.5, 'reason': 'scope'}]
        self.assertAlmostEqual(
            budget.overrun_factor(state, room='pressoffice',
                                  product_id='prod-1'), 1.5)

    def test_non_numeric_ratio_counts_as_one(self):
        state = _state()
        state['budgetOverruns'] = [
            {'room': 'pressoffice', 'taskType': 'code',
             'overrunRatio': 'heavy', 'reason': 'scope'},
            {'room': 'pressoffice', 'taskType': 'code',
             'overrunRatio': 2.0, 'reason': 'scope'}]
        self.assertAlmostEqual(
            budget.overrun_factor(state, room='pressoffice',
                                  task_type='code'), 1.5)


class BudgetUsdForTask(unittest.TestCase):
    def test_explicit_budget_ignores_the_factor(self):
        self.assertEqual(
            sim.budget_usd_for_task('code', 0.90, 'standard', 2.0), 0.90)

    def test_factor_scales_the_type_tier(self):
        self.assertEqual(
            sim.budget_usd_for_task('code', None, 'standard', 2.0), 1.0)
        self.assertEqual(
            sim.budget_usd_for_task('code', None, 'generous', 1.5), 1.5)

    def test_invalid_factor_is_ignored(self):
        for bad in (None, 0, -1.0, 'big'):
            self.assertEqual(
                sim.budget_usd_for_task('code', None, 'standard', bad), 0.50)

    def test_overrun_factor_for_guard(self):
        state = _state()
        state['budgetOverruns'] = [{
            'room': 'pressoffice', 'teamId': 'dev', 'taskType': 'code',
            'overrunRatio': 1.0, 'reason': 'attempts'}]
        self.assertAlmostEqual(
            sim._overrun_factor_for(
                state, 'pressoffice', {'teamId': 'dev', 'taskType': 'code'}),
            1.25)
        self.assertEqual(sim._overrun_factor_for(None, 'pressoffice'), 1.0)


class DirectorNotification(unittest.TestCase):
    def _notify(self, state, task, analysis=None, completed=None):
        with unittest.mock.patch.object(sim, '_log_governance') as log:
            sim._notify_task_budget_exhausted(
                state, task, spent=0.50, attempts=12,
                analysis=analysis, completed=completed)
        return log

    def test_note_carries_cause_work_and_future_factor(self):
        state = _state()
        state['budgetOverruns'] = [{
            'room': 'pressoffice', 'teamId': 'dev', 'taskType': 'code',
            'overrunRatio': 1.0, 'reason': 'attempts'}]
        task = _task(note='partial work shipped')
        analysis = budget.analyze_overrun(task, 0.50, 12)
        completed = budget.evaluate_completed_work(task)
        log = self._notify(state, task, analysis, completed)
        director = state['agents']['nora']
        self.assertEqual(len(director['mailbox']), 1)
        note = director['mailbox'][0]
        self.assertEqual(note['kind'], 'task_budget_exhausted')
        self.assertEqual(note['reason'], 'attempts')
        self.assertAlmostEqual(note['overrunRatio'], 1.0)
        self.assertTrue(note['workLanded'])
        self.assertAlmostEqual(note['futureFactor'], 1.25)
        self.assertIn('Cause: 12 model calls averaging', note['text'])
        self.assertIn('Work landed before the stop: partial', note['text'])
        self.assertIn('Similar future cards now budget 1.25x', note['text'])
        self.assertIn('grant it more budget', note['text'])
        args = log.call_args[0]
        self.assertEqual(args[2], 'task_budget_exhausted')
        details = args[3]
        self.assertEqual(details['reason'], 'attempts')
        self.assertAlmostEqual(details['futureFactor'], 1.25)
        self.assertTrue(details['workLanded'])

    def test_empty_work_reports_nothing_landed(self):
        state = _state()
        task = _task()
        completed = budget.evaluate_completed_work(task)
        self._notify(state, task, completed=completed)
        text = state['agents']['nora']['mailbox'][0]['text']
        self.assertIn('No work landed before the stop', text)
        self.assertNotIn('Similar future cards', text)

    def test_budget_exhaustion_queues_a_director_decision_card(self):
        state = _state()
        task = _task(status='failed', budgetExhausted=True,
                     failNote='task model-spend budget exhausted')
        analysis = budget.analyze_overrun(task, 0.75, 12)
        completed = budget.evaluate_completed_work(task)
        self._notify(state, task, analysis, completed)
        queued = [w for w in state.get('workQueue', [])
                  if str(w.get('title', '')).startswith('[BUDGET]')]
        self.assertEqual(len(queued), 1)
        card = queued[0]
        self.assertEqual(card['taskType'], 'supervise')
        self.assertEqual(card['assignedTo'], 'nora')
        self.assertEqual(card['teamId'], 'dev')
        self.assertEqual(card['priority'], 2)
        self.assertIn('supervisor_grant_budget', card['instructions'])
        self.assertIn('Default to granting more budget', card['instructions'])
        self.assertIn('$1.00', card['instructions'])

    def test_budget_decision_card_is_not_duplicated(self):
        state = _state()
        task = _task(status='failed', budgetExhausted=True)
        self._notify(state, task)
        self._notify(state, task)
        self.assertEqual(
            len([w for w in state.get('workQueue', [])
                 if str(w.get('title', '')).startswith('[BUDGET]')]), 1)

    def test_fires_once_per_task(self):
        state = _state()
        task = _task()
        self._notify(state, task)
        self._notify(state, task)
        self.assertEqual(len(state['agents']['nora']['mailbox']), 1)

    def test_branch_composition_raises_future_budgets(self):
        state = _state()
        task = _task()
        analysis = budget.analyze_overrun(task, 0.50, 12)
        completed = budget.evaluate_completed_work(task)
        budget.record_overrun(state, task, 0.50, 12,
                              analysis=analysis, completed=completed)
        with unittest.mock.patch.object(sim, '_log_governance'):
            sim._notify_task_budget_exhausted(
                state, task, spent=0.50, attempts=12,
                analysis=analysis, completed=completed)
        self.assertEqual(len(state['budgetOverruns']), 1)
        self.assertEqual(state['agents']['nora']['mailbox'][0]['reason'],
                         'attempts')
        factor = sim._overrun_factor_for(
            state, 'pressoffice', {'teamId': 'dev', 'taskType': 'code'})
        self.assertAlmostEqual(factor, 1.25)
        self.assertAlmostEqual(
            sim.budget_usd_for_task('code', None, 'standard', factor), 0.625)


if __name__ == '__main__':
    unittest.main()
