import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import agents.completion_watchdog as watchdog


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


class StaleRepairFlagSweep(unittest.TestCase):
    def test_stale_flag_is_popped_when_no_repair_is_live(self):
        # A dead failed card kept _peerRepairPending forever: nothing ever
        # cleared it, and the archive split refuses to drop a recovering
        # parent, so the card sat in the live blob indefinitely (F-C).
        state = seed('failed')
        state['tasks']['p']['_peerRepairPending'] = True
        watchdog.step(state, 100000, observe_only=False)
        self.assertNotIn('_peerRepairPending', state['tasks']['p'])

    def test_flag_survives_while_a_repair_child_is_live(self):
        state = seed('failed')
        state['tasks']['p']['_peerRepairPending'] = True
        state['tasks']['r1'] = {'id': 'r1', 'status': 'working',
                                'reviewOf': 'p', 'taskType': 'code'}
        watchdog.step(state, 100000, observe_only=False)
        self.assertTrue(state['tasks']['p'].get('_peerRepairPending'))

    def test_flag_survives_while_a_repair_is_queued(self):
        state = seed('failed')
        state['tasks']['p']['_peerRepairPending'] = True
        state['workQueue'].append({'reviewOf': 'p', 'taskType': 'code'})
        watchdog.step(state, 100000, observe_only=False)
        self.assertTrue(state['tasks']['p'].get('_peerRepairPending'))

    def test_observe_pass_never_sweeps(self):
        state = seed('failed')
        state['tasks']['p']['_peerRepairPending'] = True
        watchdog.observe(state, 100000)
        self.assertTrue(state['tasks']['p'].get('_peerRepairPending'))


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
