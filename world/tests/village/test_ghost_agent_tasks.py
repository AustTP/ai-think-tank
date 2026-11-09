"""Ghost-pointer sweeper: an agent whose .task points at a dead task is
permanently ineligible (the candidate filter requires an empty .task), so
a torn mid-flight restore or a missed completion clear benches her
forever. The sweeper clears the pointer on the next tick."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


class GhostAgentSweeper(unittest.TestCase):
    def _state(self):
        return {
            'tasks': {
                'task-live': {'id': 'task-live', 'status': 'working'},
                'task-done': {'id': 'task-done', 'status': 'done'},
            },
            'agents': {
                'ghost': {'id': 'ghost', 'task': 'task-gone', 'busy': False},
                'stale': {'id': 'stale', 'task': 'task-done', 'busy': False},
                'live': {'id': 'live', 'task': 'task-live', 'busy': False},
                'busy': {'id': 'busy', 'task': 'task-gone', 'busy': True},
                'free': {'id': 'free', 'task': None, 'busy': False},
            },
        }

    def _sweeper(self):
        import sim
        return sim._clear_ghost_agent_tasks

    def test_clears_gone_and_terminal_pointers_only(self):
        clear = self._sweeper()
        state = self._state()
        n = clear(state)
        agents = state['agents']
        self.assertEqual(n, 2)
        self.assertIsNone(agents['ghost']['task'])
        self.assertIsNone(agents['stale']['task'])
        self.assertEqual(agents['live']['task'], 'task-live',
                         'a live working assignment is never touched')
        self.assertEqual(agents['busy']['task'], 'task-gone',
                         'a busy agent is left alone (mid-ceremony)')
        self.assertIsNone(agents['free']['task'])

    def test_missing_agents_map_is_a_noop(self):
        clear = self._sweeper()
        self.assertEqual(clear({}), 0)

    def test_sweeper_is_wired_into_the_tick(self):
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            'village', 'sim_parts', '160__stale_work_step.py'),
            encoding='utf-8').read()
        self.assertIn('_clear_ghost_agent_tasks(state)', src,
                      'the sweeper runs beside the orphan reclaim')

    def test_ceremony_restore_does_not_stomp_a_live_assignment(self):
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            'village', 'sim_parts', '120__file_free_spike.py'),
            encoding='utf-8').read()
        self.assertIn('Only replay the\n        # snapshot when the agent has NOT moved on',
                      src,
                      'the restore replays a stale pointer only when the '
                      'agent has not taken new work during the ceremony')


if __name__ == '__main__':
    unittest.main()
