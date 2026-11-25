"""_score_ingest_loop retry semantics (operator follow-up, 2026-11-25):
a failed or empty midnight ingest must retry hourly
(SCORE_INGEST_RETRY_INTERVAL_S) with a LOUD log, and return to the midnight
cadence only after rows land (added > 0 or rows already on file). Sleeps are
folded so the test runs in milliseconds."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402

_TMP = None
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='think-tank-score-ingest-retry-')
    _PATCHER = unittest.mock.patch.object(serve, 'DB_PATH', os.path.join(_TMP, 'test.db'))
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _insert_score(mid='t/model'):
    with serve._db() as conn:
        conn.execute(
            'INSERT INTO model_benchmark_scores (model_id, benchmark, score, evaluator, checked_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (mid, 'intelligence_index', 40.0, 'artificial_analysis', serve.time.time()))


class Driver:
    """Runs _score_ingest_loop with a folded sleep so both the midnight wait
    and the hourly retries are observable without wall-clock delays.
    ingest(attempt, orig_fn) returns the added-row count for that attempt."""

    def __init__(self, ingest):
        self.ingest = ingest
        self.sleeps = []
        self.attempts = 0
        self._real_sleep = asyncio.sleep
        original = serve._ingest_benchmark_scores

        def fake(models):
            self.attempts += 1
            return self.ingest(self.attempts, original)

        self._fake = fake

    async def folded_sleep(self, s):
        self.sleeps.append(s)
        await self._real_sleep(0)  # yield or the loop task starves

    def retry_sleeps(self):
        return [s for s in self.sleeps
                if s == serve.SCORE_INGEST_RETRY_INTERVAL_S]

    def other_sleeps(self):
        return [s for s in self.sleeps
                if s != serve.SCORE_INGEST_RETRY_INTERVAL_S]

    async def run_until(self, predicate, budget=10.0):
        with unittest.mock.patch.object(serve, '_ingest_benchmark_scores', self._fake), \
             unittest.mock.patch.object(serve, '_catalog_union_models_for_scores', lambda: []):
            serve.asyncio.sleep = self.folded_sleep
            task = asyncio.create_task(serve._score_ingest_loop())
            try:
                deadline = asyncio.get_running_loop().time() + budget
                while asyncio.get_running_loop().time() < deadline:
                    if predicate(self):
                        return True
                    await self._real_sleep(0)
                return False
            finally:
                serve.asyncio.sleep = asyncio.sleep
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class ScoreIngestRetry(unittest.TestCase):
    """2026-11-25 incident: one failed midnight ingest left bands scoreless
    until the NEXT midnight. The loop must retry hourly until rows land."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_benchmark_scores')

    def test_failure_retries_hourly_then_succeeds(self):
        def ingest(attempt, _orig):
            if attempt < 3:
                raise OSError('AA fetch boom')
            return 5
        d = Driver(ingest)
        done = asyncio.run(d.run_until(
            lambda dr: dr.attempts >= 3 and len(d.retry_sleeps()) >= 2))
        self.assertTrue(done, 'loop never reached two retries + success')
        # Two failures -> two hourly retry sleeps, then the third ingest
        # added 5 rows and the cycle broke back to the midnight cadence.
        self.assertEqual(d.retry_sleeps(),
                         [serve.SCORE_INGEST_RETRY_INTERVAL_S] * 2)

    def test_empty_table_retries_without_added_rows(self):
        # Fail-open sources returning empty slices (added=0, no exception)
        # must also retry while the table is EMPTY: bands have no scores on
        # file, exactly the 2026-11-25 zero-row drain.
        d = Driver(lambda attempt, _orig: 0)
        done = asyncio.run(d.run_until(lambda dr: len(dr.retry_sleeps()) >= 1))
        self.assertTrue(done, 'empty ingest with an empty table never retried')
        self.assertEqual(d.attempts, 1)

    def test_rows_on_file_ends_retry_cycle(self):
        # added=0 with an existing score row: a normal nothing-new day. The
        # cycle ends and the loop returns to the midnight wait (no retry).
        _insert_score()
        d = Driver(lambda attempt, _orig: 0)
        done = asyncio.run(d.run_until(lambda dr: len(d.other_sleeps()) >= 1))
        self.assertTrue(done, 'loop did not return to midnight cadence')
        self.assertEqual(d.retry_sleeps(), [])


if __name__ == '__main__':
    unittest.main()
