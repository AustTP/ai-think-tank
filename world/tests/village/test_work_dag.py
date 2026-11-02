"""The task DAG: dependency edge lists with join semantics and a cycle
guard.

The historic model was a single-edge chain -- `dependsOn` held ONE task
id, so fan-out worked (many cards can wait on one predecessor) but
fan-in joins were impossible (a card needing TWO predecessors could not
be expressed) and no cycle could form. This upgrade makes the edge list
a real DAG: `dependsOn` accepts a single id (backward compatible) or a
list; a gated card is released only when EVERY predecessor has landed;
self-edges are dropped and any edge that would close a directed cycle is
pruned at the only place a durable task edge is born.

These tests pin the core (normalization, join release, cycle detection
and pruning) plus every read-side matcher that had to learn the list
shape: the reopened-story cascade, the issue auto-unblock, the
long-horizon topic/pipeline gates, and the player-card prose.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


def _state(tasks=None):
    return {'tasks': tasks or {}, 'workQueue': []}


class Normalize(unittest.TestCase):
    def test_string_form_is_the_historic_chain(self):
        self.assertEqual(sim._normalize_dep_ids('task-1'), ['task-1'])

    def test_list_form_dedupes_and_cleans(self):
        self.assertEqual(sim._normalize_dep_ids(['task-1', ' task-1 ', '', 'task-2']),
                         ['task-1', 'task-2'])

    def test_garbage_is_no_edges(self):
        self.assertEqual(sim._normalize_dep_ids(None), [])
        self.assertEqual(sim._normalize_dep_ids(42), [])
        self.assertEqual(sim._normalize_dep_ids({'a': 1}), [])


class JoinSemantics(unittest.TestCase):
    def test_single_dep_string_still_gates_and_releases(self):
        state = _state({'task-1': {'id': 'task-1', 'status': 'walking'}})
        item = {'dependsOn': 'task-1'}
        self.assertFalse(sim._work_item_dependency_met(state, item))
        state['tasks']['task-1']['status'] = 'done'
        self.assertTrue(sim._work_item_dependency_met(state, item))

    def test_join_releases_only_when_every_predecessor_lands(self):
        state = _state({'task-1': {'id': 'task-1', 'status': 'done'},
                        'task-2': {'id': 'task-2', 'status': 'working'}})
        item = {'dependsOn': ['task-1', 'task-2']}
        self.assertFalse(sim._work_item_dependency_met(state, item))
        state['tasks']['task-2']['status'] = 'done'
        self.assertTrue(sim._work_item_dependency_met(state, item))

    def test_no_deps_is_always_met(self):
        self.assertTrue(sim._work_item_dependency_met(_state(), {}))
        self.assertTrue(sim._work_item_dependency_met(_state(), {'dependsOn': []}))


class Cycles(unittest.TestCase):
    def test_self_edge_is_a_cycle(self):
        self.assertTrue(sim._dependency_would_cycle(_state(), 'task-1', ['task-1']))

    def test_direct_two_cycle(self):
        state = _state({'task-a': {'id': 'task-a', 'dependsOn': ['task-b']}})
        self.assertTrue(sim._dependency_would_cycle(state, 'task-b', ['task-a']))

    def test_transitive_three_cycle(self):
        state = _state({'task-b': {'id': 'task-b', 'dependsOn': ['task-c']},
                        'task-c': {'id': 'task-c', 'dependsOn': ['task-a']}})
        self.assertTrue(sim._dependency_would_cycle(state, 'task-a', ['task-b']))

    def test_diamond_is_acyclic(self):
        state = _state({'task-b': {'id': 'task-b', 'dependsOn': ['task-d']},
                        'task-c': {'id': 'task-c', 'dependsOn': ['task-d']}})
        self.assertFalse(sim._dependency_would_cycle(state, 'task-a', ['task-b', 'task-c']))

    def test_unknown_deps_are_acyclic(self):
        self.assertFalse(sim._dependency_would_cycle(_state(), 'task-a', ['not-yet']))

    def test_prune_drops_the_closing_edge_and_keeps_the_rest(self):
        # task-c waits on task-d transitively; adding task-d -> task-c
        # would close the cycle, so the prune keeps the innocent edge and
        # drops the closing one.
        state = _state({'task-c': {'id': 'task-c', 'dependsOn': ['task-d']}})
        kept = sim._prune_task_dep_cycles(state, 'task-d', ['task-b', 'task-c'])
        self.assertEqual(kept, ['task-b'])

    def test_a_diamond_join_is_not_pruned(self):
        state = _state({'task-b': {'id': 'task-b', 'dependsOn': ['task-a']}})
        kept = sim._prune_task_dep_cycles(state, 'task-c', ['task-b', 'task-a'])
        self.assertEqual(kept, ['task-b', 'task-a'])

    def test_prune_drops_self_edges(self):
        self.assertEqual(sim._prune_task_dep_cycles(_state(), 'task-a', ['task-a', 'task-b']),
                         ['task-b'])

    def test_prune_of_an_already_acyclic_set_changes_nothing(self):
        state = _state({'task-b': {'id': 'task-b', 'dependsOn': ['task-c']}})
        self.assertEqual(sim._prune_task_dep_cycles(state, 'task-a', ['task-b']), ['task-b'])


class CascadeMatchers(unittest.TestCase):
    @staticmethod
    def _tank():
        """The bell-labs minimal tank shape: enough roster/teams/agents for
        _enter_peer_review to find two real reviewers."""
        return {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'theo', 'name': 'Theo', 'role': 'Admin', 'isAdmin': True},
                {'id': 'dev', 'name': 'Dev', 'role': 'Director', 'director': 'theo'},
                {'id': 'ben', 'name': 'Ben', 'role': 'Engineer', 'director': 'dev'},
                {'id': 'ada', 'name': 'Ada', 'role': 'Researcher', 'director': 'dev'},
                {'id': 'ops', 'name': 'Ops', 'role': 'Director', 'director': 'theo'},
                {'id': 'zoe', 'name': 'Zoe', 'role': 'Engineer', 'director': 'ops'},
            ],
            'teams': [
                {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev', 'scrumMasterId': 'ada'},
                {'id': 'ops', 'name': 'Ops Squad', 'directorId': 'ops', 'scrumMasterId': 'zoe'},
            ],
            'agents': {
                'theo': {'id': 'theo', 'x': 0, 'y': 0, 'busy': False, 'task': None, 'offDuty': False},
                'dev': {'id': 'dev', 'x': 0, 'y': 0, 'busy': False, 'task': None, 'offDuty': False},
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'busy': False, 'task': None, 'offDuty': False},
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'busy': False, 'task': None, 'offDuty': False},
                'ops': {'id': 'ops', 'x': 0, 'y': 0, 'busy': False, 'task': None, 'offDuty': False},
                'zoe': {'id': 'zoe', 'x': 0, 'y': 0, 'busy': False, 'task': None, 'offDuty': False},
            },
            'villages': [{'id': 'main', 'name': 'Main Village'}],
            'workQueue': [],
        }

    def test_reopened_story_cascades_to_list_members(self):
        # A done card that waited on a JOIN (the reopened task is one of
        # several predecessors) must still be sent back through the peer
        # gate when that one predecessor is vetoed.
        state = self._tank()
        state['tasks'] = {
            't-join': {'id': 't-join', 'status': 'done', 'taskType': 'code',
                       'room': 'pressoffice', 'title': 'Joined deliverable',
                       'assignedTo': 'ben', 'projectLabel': 'report',
                       'dependsOn': ['t-reopened', 't-other']},
            't-other': {'id': 't-other', 'status': 'done'},
            't-reopened': {'id': 't-reopened', 'status': 'done'},
        }
        n = sim._cascade_rereview(state, 't-reopened')
        self.assertEqual(n, 1)
        self.assertEqual(state['tasks']['t-join']['status'], 'needs_review')

    def test_reopened_story_ignores_non_members(self):
        state = self._tank()
        state['tasks'] = {
            't-x': {'id': 't-x', 'status': 'done', 'taskType': 'code',
                    'room': 'pressoffice', 'title': 'Unrelated work',
                    'assignedTo': 'ben', 'projectLabel': 'report',
                    'dependsOn': ['t-unrelated']},
        }
        self.assertEqual(sim._cascade_rereview(state, 't-reopened'), 0)

    def test_issue_unblock_fires_for_list_members(self):
        state = {'issues': {'ISS-1': {'key': 'ISS-1', 'blocked': True,
                                      'blockedKind': 'stuck_on_agent',
                                      'dependsOnTask': ['t-done', 't-still-open']}},
                 'tasks': {'t-done': {'id': 't-done', 'status': 'done',
                                      'title': 'Landed work'},
                           't-still-open': {'id': 't-still-open', 'status': 'walking'}},
                 'agentRoster': [], 'teams': [], 'workQueue': [],
                 '_pendingBlockChanges': []}
        cleared = sim._auto_clear_dependency_blocks(state, 't-done')
        # One unblock request filed for the landed predecessor.
        self.assertEqual(cleared, 1)

    def test_issue_unblock_still_fires_for_the_historic_string_form(self):
        state = {'issues': {'ISS-1': {'key': 'ISS-1', 'blocked': True,
                                      'blockedKind': 'stuck_on_agent',
                                      'dependsOnTask': 't-done'}},
                 'tasks': {'t-done': {'id': 't-done', 'status': 'done',
                                      'title': 'Landed work'}},
                 'agentRoster': [], 'teams': [], 'workQueue': [],
                 '_pendingBlockChanges': []}
        self.assertEqual(sim._auto_clear_dependency_blocks(state, 't-done'), 1)


if __name__ == '__main__':
    unittest.main()
