import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import completion_watchdog as watchdog


def seed(status='working'):
    return {
        'tasks': {
            'p': {
                'id': 'p', 'title': 'Deliver report', 'status': status,
                'assignedTo': 'ada', 'originalAssignedTo': 'ada',
                'taskType': 'research', 'room': 'observatory',
                'acceptanceCriteria': 'Save sourced findings',
            }
        },
        'agents': {'ada': {}, 'iris': {}},
        'agentRoster': [{'id': 'ada'}, {'id': 'iris'}],
        'teams': [{'id': 'team', 'scrumMasterId': 'iris', 'members': ['ada']}],
        'workQueue': [],
    }


class ObserveOnly(unittest.TestCase):
    def test_failed_task_is_not_blocked_or_requeued(self):
        state = seed('failed')
        watchdog.observe(state, 100000)
        self.assertEqual(state['tasks']['p']['status'], 'failed')
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(state['tasks']['p']['deliveryState'], 'stalled')
        self.assertIn('Recover', state['tasks']['p']['nextAction'])

    def test_recovery_planned_task_is_not_queued(self):
        state = seed('recovery_planned')
        watchdog.observe(state, 100000)
        self.assertEqual(state['tasks']['p']['status'], 'recovery_planned')
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(state['completionControl']['enabled'], False)
        self.assertEqual(state['tasks']['p']['deliveryState'], 'stalled')

    def test_manual_attention_and_budget_are_untouched(self):
        state = seed('repairing')
        state['tasks']['p'].update(manualAttention=True, budgetExhausted=True)
        watchdog.observe(state, 100000)
        self.assertTrue(state['tasks']['p']['manualAttention'])
        self.assertTrue(state['tasks']['p']['budgetExhausted'])
        self.assertEqual(state['tasks']['p']['deliveryState'], 'blocked')
        self.assertEqual(state['workQueue'], [])

    def test_observe_sets_last_progress_and_signature(self):
        state = seed('working')
        watchdog.observe(state, 100000)
        self.assertIn('lastProgressAt', state['tasks']['p'])
        self.assertIn('_completionSignature', state['tasks']['p'])
        self.assertEqual(state['tasks']['p']['deliveryState'], 'actively_working')

    def test_needs_review_waits_for_reviewer(self):
        state = seed('needs_review')
        watchdog.observe(state, 100000)
        self.assertEqual(state['tasks']['p']['deliveryState'], 'awaiting_review')
        self.assertIn('reviewer', state['tasks']['p']['nextAction'])
        self.assertEqual(state['workQueue'], [])

    def test_idle_state_is_unchanged(self):
        state = {'tasks': {}, 'workQueue': []}
        watchdog.observe(state, 100000)
        self.assertEqual(state, {'tasks': {}, 'workQueue': []})
