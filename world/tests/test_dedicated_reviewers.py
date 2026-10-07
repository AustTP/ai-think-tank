import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dedicated_reviewers
import sim


def fixture():
    state = {'agentRoster': [{'id': 'author', 'director': 'delivery'},
                             {'id': 'worker', 'director': 'delivery'}],
             'agents': {'author': {'id': 'author'}, 'worker': {'id': 'worker'}},
             'tasks': {}, 'workQueue': [], 'teams': []}
    dedicated_reviewers.create_reviewers(state)
    return state


class DedicatedReviewers(unittest.TestCase):
    def test_creation_is_persistent_and_idempotent(self):
        state = fixture()
        original = copy.deepcopy(state)
        self.assertEqual(dedicated_reviewers.create_reviewers(state), [])
        self.assertEqual(state, original)
        self.assertEqual(len([d for d in state['agentRoster'] if d.get('dedicatedReviewer')]), 4)

    def test_new_reviews_use_distinct_independent_reviewers(self):
        state = fixture()
        pair = sim._pick_reviewer_ids(state, 'author', 'observatory')
        self.assertEqual(len(set(pair)), 2)
        self.assertTrue(all(cid.startswith('reviewer-') for cid in pair))

    def test_busy_and_queued_reviewers_yield_to_free_reviewers(self):
        state = fixture()
        state['agents']['reviewer-quinn']['busy'] = True
        state['workQueue'].append({'taskType': 'review', 'assignedTo': 'reviewer-rhea'})
        self.assertEqual(set(sim._pick_reviewer_ids(state, 'author')), {'reviewer-soren', 'reviewer-tessa'})

    def test_repair_is_reverified_by_original_pair(self):
        state = fixture()
        pair = ['worker', 'reviewer-quinn']
        self.assertEqual(set(sim._pick_reviewer_ids(state, 'author', preferred=pair)), set(pair))

    def test_reviewers_are_reserved_from_general_and_pair_work(self):
        state = fixture()
        self.assertEqual(sim._eligible_candidates(state, True), ['author', 'worker'])
        self.assertEqual(len(sim._eligible_candidates(state, True, include_reviewers=True)), 6)

    def test_explicit_nonreview_pin_cannot_take_reviewer(self):
        state = fixture()
        with patch.object(sim, 'assign_task') as assign:
            result = sim._assign_due_item(state, {'title': 'Repair', 'room': 'observatory',
                         'taskType': 'research', 'reviewOf': 'parent',
                         'assignedTo': 'reviewer-quinn'}, True, {}, {}, 0)
        self.assertIsNone(result)
        assign.assert_not_called()

    def test_sprint_cannot_include_reviewers(self):
        state = fixture()
        state['sprints'] = {'spr': {'teamIds': ['delivery'], 'workerCount': 6}}
        for definition in state['agentRoster']:
            if definition.get('dedicatedReviewer'):
                definition['director'] = 'delivery'
        self.assertEqual(sim._sprint_worker_pool(state, 'spr'), ['author', 'worker'])


if __name__ == '__main__':
    unittest.main()
