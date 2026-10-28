import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import agents.transport_recovery as transport_recovery


def fixture():
    parent = {'id': 'p', 'status': 'repairing', 'taskType': 'code',
              'assignedTo': 'leo', 'budgetUsd': 1.0}
    task = {'id': 'fix', 'reviewOf': 'p', 'title': 'Fix: story',
            'taskType': 'code', 'assignedTo': 'leo', 'status': 'working'}
    return {'tasks': {'p': parent, 'fix': task},
            'completionControl': {'transportRecoveryAttempts': 3},
            'workQueue': []}, task


def failure(note=None):
    return {'ok': False, 'infra': True,
            'note': note or 'the model call failed or returned nothing (infrastructure error, retried once)'}


class TransportRecovery(unittest.TestCase):
    def test_first_retry_is_immediate_and_queued(self):
        state, task = fixture()
        self.assertEqual(transport_recovery.queue(state, task, failure(), 100000), 'queued')
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['notBefore'], 100000)
        self.assertEqual(state['tasks']['p']['status'], 'repairing')
        self.assertEqual(state['tasks']['p']['transportRecovery']['attempts'], 1)

    def test_retry_backoff_grows_and_duplicate_is_not_queued(self):
        state, task = fixture()
        self.assertEqual(transport_recovery.queue(state, task, failure(), 100000), 'queued')
        self.assertFalse(transport_recovery.queue(state, task, failure(), 100001))
        state['workQueue'] = []
        self.assertEqual(transport_recovery.queue(state, task, failure(), 200000), 'queued')
        self.assertGreater(state['workQueue'][0]['notBefore'], 200000)
        self.assertLessEqual(state['workQueue'][0]['notBefore'], 200000 + 900000)

    def test_retry_limit_blocks_instead_of_looping(self):
        state, task = fixture()
        for now in (100000, 200000, 400000):
            state['workQueue'] = []
            self.assertEqual(transport_recovery.queue(state, task, failure(), now), 'queued')
        state['workQueue'] = []
        self.assertEqual(transport_recovery.queue(state, task, failure(), 800000), 'blocked')
        self.assertEqual(state['tasks']['p']['status'], 'blocked')
        self.assertTrue(state['tasks']['p']['manualAttention'])
        self.assertEqual(state['workQueue'], [])

    def test_real_code_failure_is_not_transport_retry(self):
        state, task = fixture()
        self.assertFalse(transport_recovery.queue(
            state, task, {'ok': False, 'note': 'pytest failed: assertion error'}, 100000))

    def test_budget_exhaustion_is_not_transport_retry(self):
        state, task = fixture()
        self.assertFalse(transport_recovery.queue(
            state, task, {'ok': False, 'infra': True, 'budgetExhausted': True}, 100000))

    def test_non_coding_infra_failure_is_retried_with_backoff(self):
        # A non-coding card whose model call failed on transport must be
        # re-queued on the SAME story (reviewOf), not auto-closed into churn.
        state, task = fixture()
        state['tasks']['p']['taskType'] = 'spike'
        task['taskType'] = 'spike'
        self.assertEqual(transport_recovery.queue(state, task, failure(), 100000), 'queued')
        queued = state['workQueue']
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]['reviewOf'], 'p')
        self.assertEqual(queued[0]['title'], task.get('title'))
        self.assertEqual(state['tasks']['p']['status'], 'repairing')
        self.assertFalse(state['tasks']['p']['manualAttention'])
        # Retries follow the non-coding schedule: first is immediate, second waits.
        self.assertEqual(queued[0]['notBefore'], 100000)
        state['workQueue'] = []
        self.assertEqual(transport_recovery.queue(state, task, failure(), 200000), 'queued')
        self.assertGreaterEqual(state['workQueue'][0]['notBefore'] - 200000, 60000)

    def test_non_coding_non_infra_failure_is_not_transport_retry(self):
        state, task = fixture()
        state['tasks']['p']['taskType'] = 'spike'
        task['taskType'] = 'spike'
        self.assertFalse(transport_recovery.queue(
            state, task, {'ok': False, 'note': 'the pipeline did not pass'}, 100000))

    def test_non_coding_retry_limit_blocks(self):
        state, task = fixture()
        state['tasks']['p']['taskType'] = 'spike'
        task['taskType'] = 'spike'
        for now in (100000, 200000, 400000):
            state['workQueue'] = []
            self.assertEqual(transport_recovery.queue(state, task, failure(), now), 'queued')
        state['workQueue'] = []
        self.assertEqual(transport_recovery.queue(state, task, failure(), 800000), 'blocked')
        self.assertEqual(state['tasks']['p']['status'], 'blocked')
        self.assertTrue(state['tasks']['p']['manualAttention'])
