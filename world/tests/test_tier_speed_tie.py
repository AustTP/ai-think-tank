"""BP-63 speed tie-break tests: observed-latency recording, the per-model
median gate, the parity-band reorder used by _best_value_pick, and the
price-watch mirror that keeps a speed pick from churning every watch cycle.
The network, the real model calls and the live think_tank.db are patched or
redirected into a temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 tests/test_tier_speed_tie.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402

_TMP = None
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='think-tank-speed-tie-')
    _PATCHER = unittest.mock.patch.object(serve, 'DB_PATH', os.path.join(_TMP, 'test.db'))
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _clear():
    with serve._db() as conn:
        conn.execute('DELETE FROM model_latency_samples')


def _refresh_clear():
    _clear()
    with serve._db() as conn:
        for table in ('model_catalog', 'model_benchmark_scores', 'model_tiers',
                      'tier_daily_log', 'catalog_price_daily'):
            conn.execute(f'DELETE FROM {table}')


def _seed(model, duration_ms, n=serve.MODEL_LATENCY_MIN_SAMPLES, base_ts=None):
    base = base_ts if base_ts is not None else serve.time.time() - 60
    with serve._db() as conn:
        for i in range(n):
            conn.execute(
                'INSERT INTO model_latency_samples (model, ts, duration_ms) VALUES (?, ?, ?)',
                (model, base - i, duration_ms))


def _cands(*specs):
    return [{'id': mid, 'price': price} for mid, price in specs]


class RecordModelLatency(unittest.TestCase):
    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def test_inserts_a_duration_row_per_call(self):
        for ms in (120.0, 340.0):
            serve._record_model_latency('vendor/m', ms)
        with serve._db() as conn:
            rows = conn.execute(
                'SELECT duration_ms FROM model_latency_samples ORDER BY ts').fetchall()
        self.assertEqual([float(r[0]) for r in rows], [120.0, 340.0])

    def test_ignores_empty_or_nonpositive_durations(self):
        serve._record_model_latency('', 100)
        serve._record_model_latency(None, 100)
        serve._record_model_latency('vendor/m', 0)
        serve._record_model_latency('vendor/m', -5)
        with serve._db() as conn:
            count = conn.execute('SELECT COUNT(*) FROM model_latency_samples').fetchone()[0]
        self.assertEqual(count, 0)

    def test_prunes_samples_older_than_retention(self):
        serve._LAST_LATENCY_PRUNE = 0.0  # force the once-per-hour prune on this write
        old_ts = serve.time.time() - serve.MODEL_LATENCY_RETENTION_S - 10
        _seed('vendor/m', 100, n=1, base_ts=old_ts)
        serve._record_model_latency('vendor/m', 250)  # fresh row via the recorder
        with serve._db() as conn:
            rows = conn.execute(
                'SELECT duration_ms FROM model_latency_samples ORDER BY ts').fetchall()
        self.assertEqual([float(r[0]) for r in rows], [250.0])


class MedianLatencyGate(unittest.TestCase):
    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def test_below_min_samples_returns_none(self):
        _seed('vendor/m', 100, n=serve.MODEL_LATENCY_MIN_SAMPLES - 1)
        self.assertIsNone(serve._model_median_latency_ms('vendor/m'))

    def test_at_min_samples_returns_median(self):
        _seed('vendor/m', 100)
        _seed('vendor/m', 200)
        for value in (300, 400, 500):
            _seed('vendor/m', value)
        self.assertEqual(serve._model_median_latency_ms('vendor/m'), 300.0)
        _clear()
        _seed('vendor/m', 100)
        _seed('vendor/m', 200)
        _seed('vendor/m', 300)
        _seed('vendor/m', 400)
        _seed('vendor/m', 500)
        _seed('vendor/m', 600)  # even count: median is the middle-two average
        self.assertEqual(serve._model_median_latency_ms('vendor/m'), 350.0)

    def test_out_of_window_samples_do_not_count(self):
        stale_ts = serve.time.time() - serve.MODEL_LATENCY_WINDOW_S - 10
        _seed('vendor/m', 900, n=3, base_ts=stale_ts)
        _seed('vendor/m', 100)
        _seed('vendor/m', 200)
        _seed('vendor/m', 300)
        _seed('vendor/m', 400)
        _seed('vendor/m', 500)
        self.assertEqual(serve._model_median_latency_ms('vendor/m'), 300.0)

    def test_unknown_model_and_empty_id_return_none(self):
        self.assertIsNone(serve._model_median_latency_ms('vendor/never-called'))
        self.assertIsNone(serve._model_median_latency_ms(''))


class SpeedTieOrder(unittest.TestCase):
    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def test_short_list_passes_through(self):
        quals = _cands(('a', 1.0))
        self.assertEqual(serve._speed_tie_order(quals), quals)

    def test_no_latency_data_keeps_price_order(self):
        quals = _cands(('a', 1.0), ('b', 1.1), ('c', 3.0))
        self.assertEqual(serve._speed_tie_order(quals), quals)

    def test_faster_slightly_pricier_model_leads_within_band(self):
        quals = _cands(('slow', 1.0), ('fast', 1.1))
        _seed('slow', 900)
        _seed('fast', 100)
        self.assertEqual(
            [m['id'] for m in serve._speed_tie_order(quals)], ['fast', 'slow'])

    def test_latency_ties_break_by_price(self):
        quals = _cands(('a', 1.1), ('b', 1.0))
        _seed('a', 200)
        _seed('b', 200)
        self.assertEqual(
            [m['id'] for m in serve._speed_tie_order(quals)], ['b', 'a'])

    def test_untimed_band_members_rank_behind_timed_ones(self):
        quals = _cands(('cheap', 1.0), ('mid', 1.05), ('late', 1.08))
        _seed('cheap', 500)
        _seed('mid', 100)
        self.assertEqual(
            [m['id'] for m in serve._speed_tie_order(quals)], ['mid', 'cheap', 'late'])

    def test_boundary_model_exactly_at_parity_stays_in_band(self):
        quals = _cands(('a', 1.0), ('edge', 1.0 * (1 + serve.TIER_SPEED_TIE_PARITY)))
        _seed('a', 900)
        _seed('edge', 100)
        self.assertEqual(
            [m['id'] for m in serve._speed_tie_order(quals)], ['edge', 'a'])

    def test_beyond_band_never_reorders_even_when_fast(self):
        quals = _cands(('a', 1.0), ('far', 2.0))
        _seed('a', 900)
        _seed('far', 100)  # a real outlier speed; still out of the parity band
        self.assertEqual(
            [m['id'] for m in serve._speed_tie_order(quals)], ['a', 'far'])

    def test_membership_and_scores_untouched(self):
        quals = _cands(('a', 1.0), ('b', 1.1), ('c', 1.2), ('d', 5.0))
        _seed('a', 800)
        _seed('c', 100)
        out = serve._speed_tie_order(quals)
        self.assertEqual(len(out), len(quals))
        self.assertEqual({m['id'] for m in out}, {m['id'] for m in quals})
        self.assertEqual(
            sorted([m['price'] for m in out]), sorted([m['price'] for m in quals]))

    def test_only_untimed_models_pass_through(self):
        quals = _cands(('a', 1.0), ('b', 1.1))
        _seed('vendor/other', 100)
        self.assertEqual(serve._speed_tie_order(quals), quals)


class BestValuePickSpeed(unittest.TestCase):
    def _pick(self, candidates, scores):
        with unittest.mock.patch.object(serve, '_verify_model_works_sync', return_value=True):
            return asyncio.run(serve._best_value_pick(candidates, scores))['id']

    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def test_faster_within_parity_pick_wins(self):
        _seed('low/slow-cheap', 900)
        _seed('low/fast-pricier', 100)
        picked = self._pick(
            _cands(('low/slow-cheap', 1.0), ('low/fast-pricier', 1.1)),
            {'low/slow-cheap': 70.0, 'low/fast-pricier': 69.0})
        self.assertEqual(picked, 'low/fast-pricier')

    def test_without_latency_data_cheapest_still_wins(self):
        picked = self._pick(
            _cands(('low/slow-cheap', 1.0), ('low/fast-pricier', 1.1)),
            {'low/slow-cheap': 70.0, 'low/fast-pricier': 69.0})
        self.assertEqual(picked, 'low/slow-cheap')

    def test_quality_floor_still_guards_the_pick(self):
        _seed('low/best-score', 1)
        _seed('low/worse', 100)
        # 'worse' is far below the floor gap, so it may not win no matter
        # how fast its observed calls are.
        picked = self._pick(
            _cands(('low/best-score', 3.0), ('low/worse', 0.1)),
            {'low/best-score': 70.0, 'low/worse': 20.0})
        self.assertEqual(picked, 'low/best-score')


class _TierWatchFixture(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_latency_samples')
            conn.execute('DELETE FROM model_tiers')
            conn.execute('DELETE FROM model_benchmark_scores')

    def tearDown(self):
        self.setUp()

    def _model(self, id_, price):
        # blended (prompt+completion)*1e6 == price; split evenly
        half = str(price / 2_000_000)
        return {'id': id_, 'name': id_,
                'pricing': {'prompt': half, 'completion': half},
                'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']}}

    def _select(self, band, slug, price):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, chosen_at) VALUES (?,?,?,?,?) '
                'ON CONFLICT(band) DO UPDATE SET slug=excluded.slug, price_per_m=excluded.price_per_m',
                (band, slug, slug, price, 0.0))

    def _watch(self, models):
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync', return_value=models):
            return serve._tier_watch_moves_sync()


class WatchMirror(_TierWatchFixture):
    """Without the mirror, a speed pick (a slightly pricier, faster
    qualifier winning on BP-63) would look like a perpetually available
    cheaper qualifier: the watch would re-probe and re-run the pick on every
    cycle and never converge. The watch must compare against the pick order
    itself."""

    def test_speed_pick_does_not_churn_the_watch(self):
        serve.set_model_benchmark_score('low/fast', 'intelligence_index', 69.0, 'u')
        serve.set_model_benchmark_score('low/cheap', 'intelligence_index', 70.0, 'u')
        self._select('low', 'low/fast', 0.11)
        _seed('low/fast', 100)
        _seed('low/cheap', 900)
        moves = self._watch([self._model('low/fast', 0.11), self._model('low/cheap', 0.10)])
        self.assertEqual(moves, set())

    def test_watch_still_fires_when_the_pick_order_candidate_is_cheaper(self):
        serve.set_model_benchmark_score('low/fast', 'intelligence_index', 69.0, 'u')
        serve.set_model_benchmark_score('low/cheap', 'intelligence_index', 70.0, 'u')
        self._select('low', 'low/pricey-old', 0.30)
        _seed('low/fast', 100)
        _seed('low/cheap', 900)
        moves = self._watch([
            self._model('low/pricey-old', 0.30),
            self._model('low/fast', 0.11),
            self._model('low/cheap', 0.10)])
        self.assertEqual(moves, {'low'})

    def test_watch_untouched_when_no_latency_data_exists(self):
        serve.set_model_benchmark_score('low/pricey', 'intelligence_index', 70.0, 'u')
        serve.set_model_benchmark_score('low/cheap', 'intelligence_index', 68.0, 'u')
        self._select('low', 'low/pricey', 0.30)
        moves = self._watch([self._model('low/pricey', 0.30), self._model('low/cheap', 0.10)])
        self.assertEqual(moves, {'low'})  # plain cheapest-first behavior preserved


class RefreshLatencyAudit(unittest.TestCase):
    """tier_daily_log audit columns: when the refreshed pick is latency-decided
    (a pricier qualifier inside the parity band won on observed speed), the
    day's row carries the median ms and the sample count that grounded it plus
    speed_picked=1; a cheapest-first pick or classifier fallback writes
    latency NULLs and speed_picked=0, so absence stays honest."""

    def setUp(self):
        _refresh_clear()

    def _model(self, id_, prompt, completion, input_mods=('text',), output_mods=('text',)):
        return {
            'id': id_, 'name': id_,
            'pricing': {'prompt': prompt, 'completion': completion},
            'architecture': {
                'input_modalities': list(input_mods),
                'output_modalities': list(output_mods),
            },
        }

    def _run_refresh(self, models):
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog', return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.refresh_model_tiers())

    def _row(self, band='low'):
        with serve._db() as conn:
            return conn.execute(
                'SELECT slug, median_latency_ms, latency_samples, speed_picked '
                'FROM tier_daily_log WHERE band = ?', (band,)).fetchone()

    def test_speed_pick_row_records_latency_grounds(self):
        models = [self._model('low/cheap', '0.00000005', '0.00000005'),
                  self._model('low/fast', '0.000000055', '0.000000055')]
        serve.set_model_benchmark_score('low/cheap', 'intelligence_index', 70.0, 'u')
        serve.set_model_benchmark_score('low/fast', 'intelligence_index', 69.0, 'u')
        _seed('low/cheap', 900.0)
        _seed('low/fast', 100.0)
        chosen = self._run_refresh(models)
        self.assertEqual(chosen['low']['id'], 'low/fast')
        self.assertEqual(self._row(), ('low/fast', 100.0, 5, 1))

    def test_cheapest_pick_row_stays_null(self):
        models = [self._model('low/cheap', '0.00000005', '0.00000005')]
        serve.set_model_benchmark_score('low/cheap', 'intelligence_index', 70.0, 'u')
        chosen = self._run_refresh(models)
        self.assertEqual(chosen['low']['id'], 'low/cheap')
        self.assertEqual(self._row(), ('low/cheap', None, None, 0))


if __name__ == '__main__':
    unittest.main()