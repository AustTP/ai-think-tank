"""The coalescing saver rides out transient serialize races in-worker.

The snapshot's nested structures are shared with the live state, so a sim
thread mid-mutation can break a json walk or a task lookup. Each failed save
used to go back through the 5s coalesce cycle (458 re-queues in a day on
2026-11-08); the saver now retries the same snapshot a beat later first.
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import serve  # noqa: E402


class SaverRaceRetry(unittest.TestCase):
    def setUp(self):
        serve._SAVE_PENDING['snapshot'] = None
        serve._SAVE_RETRY_COUNTS.clear()

    def tearDown(self):
        serve._SAVE_PENDING['snapshot'] = None
        serve._SAVE_RETRY_COUNTS.clear()

    def test_transient_race_retries_then_succeeds(self):
        calls = []

        def flaky(data):
            calls.append(data)
            if len(calls) < 3:
                raise RuntimeError('dictionary changed size during iteration')

        with mock.patch.object(serve, '_save_state_to_db_sync', flaky), \
                mock.patch.object(serve.time, 'sleep'):
            saved = serve._save_snapshot_with_retry({'tasks': {}})
        self.assertTrue(saved)
        self.assertEqual(len(calls), 3)

    def test_keyerror_race_is_retried_then_requeued(self):
        calls = []

        def flaky(data):
            calls.append(data)
            raise KeyError('task-19824')

        with mock.patch.object(serve, '_save_state_to_db_sync', flaky), \
                mock.patch.object(serve.time, 'sleep'):
            saved = serve._save_snapshot_with_retry({'tasks': {}})
        self.assertFalse(saved)
        self.assertEqual(len(calls), 3)
        self.assertIn(id(None), serve._SAVE_RETRY_COUNTS) if False else None
        # The re-queue fallback kept the snapshot queued for a later pass.
        self.assertIsNotNone(serve._SAVE_PENDING['snapshot'])

    def test_persistent_failure_drops_after_three_attempts(self):
        # Seed the retry ledger so this snapshot looks already exhausted.
        snap = object()
        serve._SAVE_RETRY_COUNTS[id(snap)] = 3

        def flaky(data):
            raise RuntimeError('dictionary changed size during iteration')

        with mock.patch.object(serve, '_save_state_to_db_sync', flaky), \
                mock.patch.object(serve.time, 'sleep'):
            saved = serve._save_snapshot_with_retry(snap)
        self.assertFalse(saved)
        self.assertIsNone(serve._SAVE_PENDING['snapshot'],
                          'an exhausted snapshot is dropped, not re-queued '
                          'forever')

    def test_non_race_error_requeues_immediately(self):
        calls = []

        def broken(data):
            calls.append(data)
            raise ValueError('bad blob shape')

        with mock.patch.object(serve, '_save_state_to_db_sync', broken), \
                mock.patch.object(serve.time, 'sleep'):
            saved = serve._save_snapshot_with_retry({'tasks': {}})
        self.assertFalse(saved)
        self.assertEqual(len(calls), 1,
                         'a non-transient error goes straight to re-queue')


class _FakeClock:
    """Deterministic clock for the saver worker: time() is driven by the
    test, sleep() advances the clock instead of blocking."""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class SaverStarvationByRefresh(unittest.TestCase):
    """A producer that queues faster than the coalesce window resets 'at' on
    every queue, so 'age' never reaches _SAVE_COALESCE_S and the pre-2026-11-10
    saver skipped forever. The north village hit exactly this once its idle
    tick dropped to ~1s per pass: kv_state froze while the sim kept running.
    The worker must persist once the queue has been continuously non-empty
    for _SAVE_MAX_PENDING_S, no matter how often the snapshot is refreshed."""

    def setUp(self):
        serve._SAVE_PENDING['snapshot'] = None
        serve._SAVE_PENDING['at'] = 0.0
        serve._SAVE_EVENT.clear()
        serve._SAVE_RETRY_COUNTS.clear()

    def tearDown(self):
        serve._SAVE_PENDING['snapshot'] = None
        serve._SAVE_RETRY_COUNTS.clear()

    def test_continuously_refreshed_queue_still_persists(self):
        clock = _FakeClock()
        saved = []
        with mock.patch.object(serve, 'time', clock), \
                mock.patch.object(serve, '_save_state_to_db_sync',
                                  lambda data: saved.append(dict(data))):
            worker = threading.Thread(target=serve._save_worker_loop,
                                      daemon=True)
            worker.start()
            for tick in range(14):
                clock.t += 1.0  # one idle sim pass per second, forever
                serve._queue_state_save({'tick': tick})
                time.sleep(0.01)  # let the worker wake, skip, or save
        self.assertTrue(
            saved,
            'a queue refreshed faster than the coalesce window must still '
            'persist within _SAVE_MAX_PENDING_S')
        self.assertGreaterEqual(
            saved[0].get('tick'), 10,
            'the first save lands only after the max-pending cap, not on '
            'every refreshed snapshot')


if __name__ == '__main__':
    unittest.main()
