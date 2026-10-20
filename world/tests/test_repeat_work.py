import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import repeat_work


def fixture():
    parent = {'id': 'p', 'title': 'Story', 'status': 'working',
              'assignedTo': 'ada', 'originalAssignedTo': 'ada',
              'taskType': 'research', 'budgetUsd': 1.0}
    task = {'id': 'fix-1', 'reviewOf': 'p', 'taskType': 'code',
            'assignedTo': 'ada', 'status': 'working'}
    return {'tasks': {'p': parent, 'fix-1': task}, 'workQueue': []}


def failure(note='same failure\ntask-1: broken'):
    return {'ok': False, 'note': note}


class RepeatWork(unittest.TestCase):
    def test_same_failure_blocks_on_second_attempt(self):
        state = fixture()
        with patch.object(repeat_work, 'evidence_digest', return_value='same'):
            self.assertFalse(repeat_work.record(state, state['tasks']['fix-1'], failure(), 1))
            state['tasks']['fix-2'] = {'id': 'fix-2', 'reviewOf': 'p', 'taskType': 'code'}
            self.assertTrue(repeat_work.record(state, state['tasks']['fix-2'], failure(), 2))
        parent = state['tasks']['p']
        self.assertEqual(parent['status'], 'blocked')
        self.assertTrue(parent['manualAttention'])
        self.assertEqual(parent['blockedReason'], 'unchanged_failure')
        self.assertEqual(parent['repeatWorkStop']['attempts'], 2)

    def test_changed_failure_or_plan_is_a_new_attempt(self):
        state = fixture()
        with patch.object(repeat_work, 'evidence_digest', return_value='same'):
            repeat_work.record(state, state['tasks']['fix-1'], failure('failure one'), 1)
            state['tasks']['fix-2'] = {'id': 'fix-2', 'reviewOf': 'p', 'taskType': 'code'}
            self.assertFalse(repeat_work.record(state, state['tasks']['fix-2'], failure('failure two'), 2))
        self.assertNotEqual(state['tasks']['p']['status'], 'blocked')

    def test_authorization_resets_the_match_window(self):
        state = fixture()
        with patch.object(repeat_work, 'evidence_digest', return_value='same'):
            repeat_work.record(state, state['tasks']['fix-1'], failure(), 4)
            state['tasks']['p']['recoveryLedger'] = {'authorizations': [{'at': 5}]}
            state['tasks']['fix-2'] = {'id': 'fix-2', 'reviewOf': 'p', 'taskType': 'code'}
            self.assertFalse(repeat_work.record(state, state['tasks']['fix-2'], failure(), 6))
            state['tasks']['fix-3'] = {'id': 'fix-3', 'reviewOf': 'p', 'taskType': 'code'}
            self.assertTrue(repeat_work.record(state, state['tasks']['fix-3'], failure(), 7))

    def test_done_parent_is_not_modified(self):
        state = fixture()
        state['tasks']['p']['status'] = 'done'
        with patch.object(repeat_work, 'evidence_digest', return_value='same'):
            repeat_work.record(state, state['tasks']['fix-1'], failure(), 1)
        self.assertEqual(state['tasks']['p']['status'], 'done')
        self.assertNotIn('repeatWorkFailures', state['tasks']['p'])
