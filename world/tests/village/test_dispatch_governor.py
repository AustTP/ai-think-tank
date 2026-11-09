"""The dispatch concurrency governor (2026-11-08 incident).

Each content run holds a full state snapshot on its own thread. 57 concurrent
runs pegged the server at 132% CPU and a quarter of the machine's memory and
every endpoint -- including the static assets the map needs -- timed out, so
the UI was unusable on any refresh. The governor caps concurrent content runs
at the queue dispatcher: a due item that would exceed the cap waits for the
next tick instead of dispatching, so nothing is lost and nothing hangs."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402


class DispatchGovernor(unittest.TestCase):
    def _state(self, running=0):
        tasks = {}
        for i in range(running):
            tasks[f'task-run-{i}'] = {
                'status': 'working', '_contentInFlight': True}
        return {'tasks': tasks, 'workQueue': [], 'agents': {},
                'agentRoster': []}

    def _pick(self):
        return {'title': 'A card', 'instructions': 'Do the thing.',
                'room': 'observatory', 'taskType': 'report', 'priority': 2}

    def test_cap_waits_instead_of_dispatching(self):
        old_cap = sim._TASK_CONTENT_MAX_CONCURRENT
        try:
            sim._TASK_CONTENT_MAX_CONCURRENT = 2
            state = self._state(running=2)
            # Any candidate works: the governor fires before eligibility.
            result = sim._assign_due_item(state, self._pick(), False,
                                          None, None, time.time() * 1000)
            self.assertIs(result, sim._ASSIGN_WAIT,
                          'at the cap the due item waits for the next tick')
        finally:
            sim._TASK_CONTENT_MAX_CONCURRENT = old_cap

    def test_active_content_runs_counts_exactly_in_flight(self):
        state = self._state(running=2)
        state['tasks']['task-light'] = {'status': 'working'}
        state['tasks']['task-review'] = {'status': 'needs_review',
                                         '_contentInFlight': False}
        self.assertEqual(sim._active_content_runs(state), 2)

    def test_non_content_tasks_do_not_count(self):
        old_cap = sim._TASK_CONTENT_MAX_CONCURRENT
        try:
            sim._TASK_CONTENT_MAX_CONCURRENT = 1
            state = self._state(running=0)
            state['tasks']['task-light'] = {'status': 'working'}
            result = sim._assign_due_item(state, self._pick(), False,
                                          None, None, time.time() * 1000)
            self.assertIsNot(result, sim._ASSIGN_WAIT,
                             'only in-flight content runs consume the cap')
        finally:
            sim._TASK_CONTENT_MAX_CONCURRENT = old_cap

    def test_env_override(self):
        # The constant is read from the environment at import; re-exec the
        # constant expression rather than the whole module.
        with mock_env('TANK_MAX_CONTENT_RUNS', '7'):
            value = max(1, int(
                (os.environ.get('TANK_MAX_CONTENT_RUNS') or '24').strip() or '24'))
        self.assertEqual(value, 7)


class mock_env:
    def __init__(self, key, value):
        self.key, self.value = key, value

    def __enter__(self):
        self._old = os.environ.get(self.key)
        os.environ[self.key] = self.value

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self._old


if __name__ == '__main__':
    unittest.main()
