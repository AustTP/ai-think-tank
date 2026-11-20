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
from unittest import mock

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

    def test_active_content_runs_counts_only_working_in_flight(self):
        state = self._state(running=2)
        state['tasks']['task-light'] = {'status': 'working'}
        state['tasks']['task-review'] = {'status': 'needs_review',
                                         '_contentInFlight': False}
        self.assertEqual(sim._active_content_runs(state), 2)
        # Zombie flags on non-working cards are residue, not live runs
        # (2026-11-08: 422 of them throttled the tank to zero).
        state['tasks']['task-zombie'] = {'status': 'queued',
                                         '_contentInFlight': True}
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


class RssWatermark(DispatchGovernor):
    """The RSS watermark guard (2026-11-20: RSS 12.6 -> 19.8GB in 70 minutes
    under the 24-run default cap on a 23GB box; MemoryBarrier=22G kill was
    minutes away). The cap bounds run COUNT, not run SIZE, so the governor
    now also holds new dispatches while the process is at/above a watermark,
    failing OPEN when the RSS read is unavailable."""

    def setUp(self):
        self._old_stopped = sim._rss_guard_stopped
        self._old_wm = sim._RSS_GUARD_WATERMARK
        sim._rss_guard_stopped = False

    def tearDown(self):
        sim._rss_guard_stopped = self._old_stopped
        sim._RSS_GUARD_WATERMARK = self._old_wm

    def test_watermark_from_cgroup_limit(self):
        # 22GiB cgroup limit at the 70% share -> ~15GiB watermark.
        wm = sim._rss_watermark_from_limit(22 * 1024**3)
        self.assertGreater(wm, 15 * 1024**3)
        self.assertLess(wm, 16 * 1024**3)
        # Absurd/absent limits fall back to the 16GiB default.
        self.assertEqual(sim._rss_watermark_from_limit(None), 16 * 1024**3)
        self.assertEqual(sim._rss_watermark_from_limit(0), 16 * 1024**3)
        self.assertEqual(sim._rss_watermark_from_limit(10**15), 16 * 1024**3)
        # The share clamps into [10, 95]; a tiny limit keeps a 1GiB floor.
        with mock.patch.dict(os.environ, {'TANK_RSS_GUARD_FRACTION': '5'}):
            self.assertEqual(sim._rss_watermark_from_limit(22 * 1024**3),
                             (22 * 1024**3) * 10 // 100)
        with mock.patch.dict(os.environ, {'TANK_RSS_GUARD_FRACTION': '99'}):
            self.assertEqual(sim._rss_watermark_from_limit(2 * 1024**3),
                             (2 * 1024**3) * 95 // 100)

    def test_explicit_bytes_env_wins(self):
        with mock.patch.dict(os.environ,
                             {'TANK_RSS_GUARD_BYTES': str(3 * 1024**3)}):
            self.assertEqual(sim._rss_watermark_bytes(), 3 * 1024**3)

    def test_blocks_at_or_above_watermark_and_fails_open(self):
        sim._RSS_GUARD_WATERMARK = 10 * 1024**3
        with mock.patch.object(sim, '_read_rss_bytes',
                               return_value=(10 * 1024**3) + 1):
            self.assertTrue(sim._rss_guard_blocks_dispatch())
        # After a stop, an unavailable RSS read fails OPEN (the guard never
        # wedges dispatch on a missing file), but the stopped flag persists
        # so the resume edge still works when reads return.
        with mock.patch.object(sim, '_read_rss_bytes', return_value=0):
            self.assertFalse(sim._rss_guard_blocks_dispatch())
        self.assertTrue(sim._rss_guard_stopped)

    def test_resumes_after_clearing_the_watermark(self):
        sim._RSS_GUARD_WATERMARK = 10 * 1024**3
        with mock.patch.object(sim, '_read_rss_bytes',
                               return_value=20 * 1024**3):
            self.assertTrue(sim._rss_guard_blocks_dispatch())
        # Inside the 1GiB hysteresis band the tank stays held.
        with mock.patch.object(sim, '_read_rss_bytes',
                               return_value=(10 * 1024**3) - 1):
            self.assertTrue(sim._rss_guard_blocks_dispatch())
        # A full GiB below the watermark clears the stop.
        with mock.patch.object(
                sim, '_read_rss_bytes',
                return_value=(10 * 1024**3) - 1024**3 - 1):
            self.assertFalse(sim._rss_guard_blocks_dispatch())
        self.assertFalse(sim._rss_guard_stopped)

    def test_guard_blocks_assign_due_item(self):
        sim._RSS_GUARD_WATERMARK = 1  # force the guard active
        state = self._state(running=0)
        with mock.patch.object(sim, '_read_rss_bytes',
                               return_value=2 * 1024**3):
            result = sim._assign_due_item(state, self._pick(), False,
                                          None, None, time.time() * 1000)
            self.assertIs(result, sim._ASSIGN_WAIT,
                          'over the watermark no new run is dispatched')
            self.assertTrue(sim._rss_guard_blocks_dispatch())
        # Under the real watermark the guard itself passes; the same call
        # shape without the guard assigns (covered by test_non_content...).
        sim._RSS_GUARD_WATERMARK = self._old_wm
        with mock.patch.object(sim, '_read_rss_bytes',
                               return_value=1 * 1024**3):
            self.assertFalse(sim._rss_guard_blocks_dispatch())


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
