import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import story_tracking


class StoryTracking(unittest.TestCase):
    def test_budget_rejects_zero_negative_and_non_finite(self):
        self.assertEqual(story_tracking.budget('2.5'), 2.5)
        self.assertIsNone(story_tracking.budget(''))
        for value in (0, -1, 'nan', 'inf', True):
            with self.assertRaises(ValueError):
                story_tracking.budget(value)

    def test_root_walks_review_and_investigation_links(self):
        state = {'tasks': {
            'p': {'id': 'p'},
            'r': {'id': 'r', 'reviewOf': 'p'},
            'i': {'id': 'i', 'investigationOf': 'r'},
        }}
        self.assertEqual(story_tracking.root(state, state['tasks']['i'])['id'], 'p')

    def test_refresh_rolls_child_spend_into_the_root_story(self):
        state = {
            'tasks': {
                'p': {'id': 'p', 'assignedTo': 'ada', 'createdAt': 1},
                'c': {'id': 'c', 'reviewOf': 'p', 'assignedTo': 'iris', 'createdAt': 2},
            },
            'agentRoster': [{'id': 'ada'}, {'id': 'iris'}],
            'teams': [{'id': 'team', 'scrumMasterId': 'iris'}],
            'workQueue': [],
        }
        view = story_tracking.refresh(state, {
            '__task__/p': {'used': 1.5, 'calls': 2},
            '__task__/c': {'used': 0.5, 'calls': 1},
        })
        self.assertEqual(state['tasks']['c']['rootStoryId'], 'p')
        self.assertEqual(state['tasks']['p']['storySpendUsd'], 2.0)
        self.assertEqual(state['tasks']['c']['storySpendUsd'], 2.0)
        self.assertEqual(view['stories']['p']['calls'], 3)

    def test_budget_status_and_closed_flags(self):
        state = {
            'tasks': {'p': {'id': 'p', 'status': 'working', 'budgetUsd': 5}},
            'backlogRequests': [],
            'workQueue': [],
        }
        status = story_tracking.budget_status(state, {'__task__/p': {'used': 2, 'calls': 1}}, 'p')
        self.assertEqual(status['scope'], 'story')
        self.assertEqual(status['budgetUsd'], 5)
        self.assertEqual(status['used'], 2)
        self.assertFalse(story_tracking.closed(state, 'p'))
        state['tasks']['p']['status'] = 'cancelled'
        self.assertTrue(story_tracking.closed(state, 'p'))
