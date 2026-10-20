import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import completion_watchdog as watchdog


def seed(status='recovery_planned'):
    return {
        'tasks': {'p': {'id': 'p', 'title': 'Deliver report', 'status': status,
                        'assignedTo': 'ada', 'originalAssignedTo': 'ada',
                        'taskType': 'research', 'room': 'observatory',
                        'createdAt': 10, 'acceptanceCriteria': 'Save sourced findings',
                        'investigationReport': 'archive/diagnosis.md'}},
        'agents': {'ada': {}, 'iris': {}},
        'agentRoster': [{'id': 'ada'}, {'id': 'iris'}],
        'teams': [{'id': 'team', 'scrumMasterId': 'iris', 'members': ['ada']}],
        'completionControl': {'enabled': True, 'recoverySince': 1,
                              'maxConcurrentStories': 5, 'maxRecoveryAttempts': 3,
                              'retryBaseMs': 30000, 'maxRecoveriesPerTick': 2},
        'workQueue': [],
    }


class CompletionRecovery(unittest.TestCase):
    def test_recovery_planned_story_queues_one_repair(self):
        state = seed()
        watchdog.step(state, 100000, observe_only=False)
        self.assertEqual(state['tasks']['p']['status'], 'repairing')
        self.assertEqual(len(state['workQueue']), 1)
        item = state['workQueue'][0]
        self.assertEqual(item['reviewOf'], 'p')
        self.assertEqual(item['assignedTo'], 'ada')
        self.assertIn('diagnosis', item['instructions'].lower())

    def test_historical_failures_are_skipped_until_explicitly_enabled(self):
        state = seed('recovery_planned')
        state['tasks']['p']['createdAt'] = 1
        state['completionControl']['recoverySince'] = 1000
        watchdog.step(state, 100000, observe_only=False)
        self.assertEqual(state['workQueue'], [])
        state['completionControl']['recoverHistorical'] = True
        watchdog.step(state, 100000, observe_only=False)
        self.assertTrue(state['workQueue'])

    def test_missing_inputs_block_instead_of_spending_on_retry(self):
        state = seed('failed')
        state['tasks']['p']['reviewFailureKind'] = 'missing_inputs'
        state['tasks']['p']['failNote'] = 'credentials are not configured'
        watchdog.step(state, 100000, observe_only=False)
        self.assertEqual(state['tasks']['p']['status'], 'blocked')
        self.assertTrue(state['tasks']['p']['manualAttention'])
        self.assertEqual(state['workQueue'], [])

    def test_retry_limit_blocks(self):
        state = seed('recovery_planned')
        state['tasks']['p']['completionRecovery'] = {'attempts': 3, 'nextAttemptAt': 0}
        watchdog.step(state, 100000, observe_only=False)
        self.assertEqual(state['tasks']['p']['status'], 'blocked')
        self.assertEqual(state['workQueue'], [])
