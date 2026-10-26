"""Gap-C coverage tests for serve.py: the Colab compute lifecycle, model
catalog / tier refresh, escalation-judge drift helpers, wake/dormancy and the
state save/merge surface. Everything that would touch the network, a real
process, or the real think_tank.db is patched or redirected into a temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  COVERAGE_FILE=/tmp/cov_gap_C.coverage python3 -m coverage run --source=serve tests/test_serve_gap_C.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
from contextlib import ExitStack

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-serve-gap-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class _StopLoop(Exception):
    pass


async def _stop_loop(_seconds):
    raise _StopLoop()


class TierWatchMoves(unittest.TestCase):
    """The 30-min price watch must fire on BOTH kinds of move: the selected
    model repricing/delisting, and a cheaper in-floor qualifier appearing (a
    value window). The second is the fix for missing a window that opens and
    closes between two every-other-hour sweeps."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_tiers')
            conn.execute('DELETE FROM model_benchmark_scores')

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

    def test_cheaper_in_floor_qualifier_triggers_move(self):
        serve.set_model_benchmark_score('low/pricey', 'intelligence_index', 70.0, 'u')
        serve.set_model_benchmark_score('low/cheap', 'intelligence_index', 68.0, 'u')
        self._select('low', 'low/pricey', 0.30)
        moves = self._watch([self._model('low/pricey', 0.30), self._model('low/cheap', 0.10)])
        self.assertEqual(moves, {'low'})

    def test_no_move_when_selected_is_the_cheapest_qualifier(self):
        serve.set_model_benchmark_score('low/a', 'intelligence_index', 70.0, 'u')
        serve.set_model_benchmark_score('low/b', 'intelligence_index', 68.0, 'u')
        self._select('low', 'low/a', 0.05)
        moves = self._watch([self._model('low/a', 0.05), self._model('low/b', 0.20)])
        self.assertEqual(moves, set())

    def test_cheaper_but_below_quality_floor_is_ignored(self):
        serve.set_model_benchmark_score('low/star', 'intelligence_index', 70.0, 'u')
        serve.set_model_benchmark_score('low/junk', 'intelligence_index', 30.0, 'u')
        self._select('low', 'low/star', 0.30)
        moves = self._watch([self._model('low/star', 0.30), self._model('low/junk', 0.02)])
        self.assertEqual(moves, set())

    def test_selected_repricing_triggers_move(self):
        serve.set_model_benchmark_score('low/a', 'intelligence_index', 70.0, 'u')
        self._select('low', 'low/a', 0.30)
        moves = self._watch([self._model('low/a', 0.45)])
        self.assertEqual(moves, {'low'})

    def test_delisted_selected_triggers_move(self):
        serve.set_model_benchmark_score('low/gone', 'intelligence_index', 70.0, 'u')
        self._select('low', 'low/gone', 0.30)
        moves = self._watch([self._model('low/other', 0.10)])
        self.assertEqual(moves, {'low'})

    def test_pinned_custom_endpoint_is_never_a_value_window(self):
        # A custom endpoint is an operator pin. It will not appear in the
        # OpenRouter catalog, so without the skip it would look delisted and
        # trigger a full re-pick every watch cycle.
        self._select('low', '@provider:low', 2.0)
        with unittest.mock.patch.object(serve, 'MODEL_PROVIDER_OVERRIDES', {
                'default': None,
                'tiers': {'low': {'base_url': 'http://127.0.0.1:9999/v1/chat/completions',
                                  'model': 'SKY-001', 'price_per_m': 2.0}}}):
            moves = self._watch([self._model('low/cheap', 0.01)])
        self.assertEqual(moves, set())



class RefreshModelTiers(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_catalog')
            conn.execute('DELETE FROM model_benchmark_scores')
            conn.execute('DELETE FROM model_tiers')

    def _model(self, id_, prompt, completion, input_mods=('text',), output_mods=('text',)):
        return {
            'id': id_, 'name': id_,
            'pricing': {'prompt': prompt, 'completion': completion},
            'architecture': {
                'input_modalities': list(input_mods),
                'output_modalities': list(output_mods),
            },
        }

    def test_picks_every_band_and_vision_from_benchmarks(self):
        models = [
            self._model('low/m1', '0.0000001', '0.0000001'),
            self._model('mid/m1', '0.000002', '0.000002'),
            self._model('mid/v1', '0.0000005', '0.0000005',
                        input_mods=('image', 'text')),
            self._model('mid/c1', '0.0000015', '0.0000015'),
            self._model('high/h1', '0.00001', '0.00001'),
            self._model('vendor/batch:batch', '0.000001', '0.000001'),
            dict(self._model('vendor/reason', '0.000001', '0.000001',
                             input_mods=('image', 'text')),
                 reasoning={'default_enabled': True}),
            self._model('vendor/badprice', 'x', 'x',
                        input_mods=('image', 'text')),
            self._model('vendor/free', '0', '0',
                        input_mods=('image', 'text')),
        ]
        serve.set_model_benchmark_score('low/m1', 'intelligence_index', 0.7, 'u')
        serve.set_model_benchmark_score('mid/m1', 'intelligence_index', 0.75, 'u')
        serve.set_model_benchmark_score('mid/c1', 'coding_index', 0.65, 'u')
        serve.set_model_benchmark_score('high/h1', 'agentic_index', 0.55, 'u')
        serve.set_model_benchmark_score('mid/v1', 'MMMU', 0.6, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['low']['id'], 'low/m1')
        self.assertEqual(chosen['mid']['id'], 'mid/m1')
        self.assertEqual(chosen['coding']['id'], 'mid/c1')
        self.assertEqual(chosen['high']['id'], 'high/h1')
        self.assertEqual(chosen['vision']['id'], 'mid/v1')
        log.assert_called_once()
        with serve._db() as conn:
            row = conn.execute(
                "SELECT slug FROM model_tiers WHERE band = 'low'").fetchone()
        self.assertEqual(row[0], 'low/m1')

    def test_sublow_picks_cheapest_scored_model_under_ceiling(self):
        # Sublow is the hard-ceilinged band for short, bounded test scenarios
        # at scale: among intelligence-scored models at or below the $0.09/M
        # cap, best-score-then-cheapest (same policy as every band) -- and a
        # brighter model over the ceiling must NOT win on score. The low band
        # pools independently, so its pick is untouched by the sublow cap.
        models = [
            self._model('sub/cheap', '0.00000002', '0.00000003'),   # $0.05/M blended
            self._model('sub/mid', '0.00000003', '0.00000005'),     # $0.08/M blended
            self._model('sub/star', '0.0000001', '0.0000002'),      # $0.30/M blended (over ceiling)
        ]
        serve.set_model_benchmark_score('sub/cheap', 'intelligence_index', 24.0, 'u')
        serve.set_model_benchmark_score('sub/mid', 'intelligence_index', 20.0, 'u')
        serve.set_model_benchmark_score('sub/star', 'intelligence_index', 50.0, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'SUBLOW_TIER_MAX_PRICE_USD', 0.09), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['sublow']['id'], 'sub/cheap')
        self.assertAlmostEqual(chosen['sublow']['price'], 0.05, places=6)
        # Low band still sees the whole cheap pool and its own floor picks
        # the brightest model there -- the sublow ceiling is not a filter on low.
        self.assertEqual(chosen['low']['id'], 'sub/star')
        with serve._db() as conn:
            row = conn.execute(
                "SELECT slug FROM model_tiers WHERE band = 'sublow'").fetchone()
        self.assertEqual(row[0], 'sub/cheap')

    def test_sublow_skips_when_nothing_qualifies_under_ceiling(self):
        # Every candidate is over the $0.09 cap: sublow must stay absent from
        # the result (callers then refuse / 503 -- _sublow_tier_slug never
        # falls back to the low tier), never silently pick an over-cap model.
        models = [self._model('sub/over', '0.0000001', '0.0000002')]  # $0.30/M blended
        serve.set_model_benchmark_score('sub/over', 'intelligence_index', 50.0, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'SUBLOW_TIER_MAX_PRICE_USD', 0.09), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertNotIn('sublow', chosen)

    def test_vision_classifier_fallback_picks_when_no_mmmu(self):
        # No MMMU scores on file (the catalog carries no image-comprehension
        # metric and the player hasn't entered any): the vision band must
        # still resolve through the same classifier fallback as the cost
        # bands and land a working vision-capable model, not stay stale.
        models = [
            self._model('vendor/text', '0.0000001', '0.0000001'),
            self._model('vendor/vision', '0.0000005', '0.0000005',
                        input_mods=('image', 'text')),
        ]
        decision = {'answers': {'q': {'choice': 'vendor/vision'}}, 'usage': {'cost': 0.01}}
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=decision), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['vision']['id'], 'vendor/vision')

    def test_vision_skips_when_pool_empty(self):
        # No vision-capable models in the catalog: the vision band is simply
        # absent from the result (and the fallback is guarded off an empty
        # shortlist -- no pointless classifier call on empty criteria).
        models = [self._model('vendor/text', '0.0000001', '0.0000001')]
        decision = {'answers': {'q': {'choice': 'vendor/text'}}, 'usage': {'cost': 0.01}}
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=decision), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertNotIn('vision', chosen)

    def test_chain_prefers_primary_metric_pool(self):
        # BAND_BENCHMARK is an ordered metric CHAIN. As long as ANY candidate in
        # the pool is scored on the primary metric, the fallback metric does NOT
        # let a fallback-only model compete on a different scale -- a modest
        # intelligence_index model wins over a fallback-only MMLU-Pro star.
        models = [
            self._model('low/primary', '0.0000001', '0.0000001'),
            self._model('low/fallback', '0.0000001', '0.0000001'),
        ]
        serve.set_model_benchmark_score('low/primary', 'intelligence_index', 0.7, 'u')
        serve.set_model_benchmark_score('low/fallback', 'MMLU-Pro', 0.95, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['low']['id'], 'low/primary')

    def test_chain_uses_fallback_when_primary_pool_empty(self):
        # When NO candidate in the pool has the primary metric, the band falls
        # through to the fallback (HF Open LLM MMLU-Pro) instead of giving up
        # and trusting Jev's classifier -- that is the point of the fallback.
        models = [
            self._model('low/a', '0.0000001', '0.0000001'),
            self._model('low/b', '0.00000005', '0.00000005'),
        ]
        serve.set_model_benchmark_score('low/a', 'MMLU-Pro', 0.7, 'u')
        serve.set_model_benchmark_score('low/b', 'MMLU-Pro', 0.95, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['low']['id'], 'low/b')

    def test_empty_band_skips_and_classifier_fallback_picks(self):
        models = [self._model('mid/only', '0.000002', '0.000002')]
        decision = {'answers': {'q': {'choice': 'mid/only'}}, 'usage': {'cost': 0.01}}
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=decision), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['mid']['id'], 'mid/only')
        self.assertNotIn('vision', chosen)

    def test_default_enabled_reasoning_model_is_score_eligible(self):
        # default_enabled reasoning no longer bars a model from the pools:
        # the call layer's explicit `reasoning` override (disabled for
        # non-thinking tiers, capped for thinking tiers) is the safeguard
        # against hidden token spend, so a default_enabled model that scores
        # on the band benchmark competes on score like any other.
        models = [
            self._model('low/plain', '0.00000005', '0.00000005'),
            dict(self._model('low/reason', '0.00000001', '0.00000001'),
                 reasoning={'default_enabled': True}),
        ]
        serve.set_model_benchmark_score('low/plain', 'intelligence_index', 0.4, 'u')
        serve.set_model_benchmark_score('low/reason', 'intelligence_index', 0.7, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['low']['id'], 'low/reason')

    def test_mandatory_reasoning_model_is_admissible(self):
        # A mandatory-reasoning model is admissible now: the call layer
        # recovers its reject-disabled 400 with a capped-reasoning retry
        # (_openrouter_chat_post), and the live verification probe is the
        # real gate. z-ai/glm-5.3-flash (mandatory reasoning, best value in
        # its class) was invisible to every pool under the old exclusion.
        models = [
            self._model('low/plain', '0.00000005', '0.00000005'),
            dict(self._model('low/mand', '0.00000004', '0.00000004'),
                 reasoning={'mandatory': True}),
        ]
        serve.set_model_benchmark_score('low/plain', 'intelligence_index', 0.4, 'u')
        serve.set_model_benchmark_score('low/mand', 'intelligence_index', 0.7, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        # low/mand scores higher AND is cheaper-or-equal, so it wins on the
        # normal best-score-then-cheapest rule.
        self.assertEqual(chosen['low']['id'], 'low/mand')

    def test_no_working_candidate_is_logged_and_skipped(self):
        models = [self._model('mid/only', '0.000002', '0.000002')]
        decision = {'answers': {'q': {'choice': 'mid/only'}}, 'usage': {'cost': 0.01}}
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=decision), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch('builtins.print') as pr:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen, {})
        self.assertTrue(any('no working candidate' in str(a) for a in pr.call_args_list))

    def test_classifier_error_falls_back_to_verify(self):
        models = [self._model('mid/only', '0.000002', '0.000002')]
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(
                 serve, '_call_openrouter_decision_sync',
                 side_effect=RuntimeError('jev down')), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['mid']['id'], 'mid/only')

    def _refresh(self, models):
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.refresh_model_tiers())

    def _logged_at(self, band):
        with serve._db() as conn:
            return conn.execute(
                'SELECT logged_at FROM tier_daily_log WHERE band = ?', (band,)).fetchone()[0]

    def test_same_slug_rerun_preserves_logged_at(self):
        # logged_at marks when the CURRENT pick first appeared, not the last
        # refresh -- a same-slug re-run must NOT move it, or the "value window
        # age" it feeds would always read as ~0.
        serve.set_model_benchmark_score('low/m1', 'intelligence_index', 70.0, 'u')
        self._refresh([self._model('low/m1', '0.0000001', '0.0000001')])
        first = self._logged_at('low')
        time.sleep(0.01)
        self._refresh([self._model('low/m1', '0.0000001', '0.0000001')])
        self.assertEqual(self._logged_at('low'), first)

    def test_slug_change_resets_logged_at(self):
        serve.set_model_benchmark_score('low/m1', 'intelligence_index', 70.0, 'u')
        serve.set_model_benchmark_score('low/m2', 'intelligence_index', 69.0, 'u')
        self._refresh([self._model('low/m1', '0.0000001', '0.0000001')])
        first = self._logged_at('low')
        time.sleep(0.01)
        # m1 delisted; m2 (in floor) takes over -> new pick, new window start.
        self._refresh([self._model('low/m2', '0.0000002', '0.0000002')])
        self.assertNotEqual(self._logged_at('low'), first)



    def test_pinned_custom_endpoint_band_is_not_repicked(self):
        models = [self._model('low/auto', '0.000002', '0.000002')]
        serve.set_model_benchmark_score('low/auto', 'intelligence_index', 50.0, 'u')
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, chosen_at) VALUES (?,?,?,?,?)',
                ('low', 'operator/old', 'Operator endpoint', 2.0, 0.0))
        with unittest.mock.patch.object(serve, 'MODEL_PROVIDER_OVERRIDES', {
                'default': None,
                'tiers': {'low': {'base_url': 'http://127.0.0.1:9999/v1/chat/completions',
                                  'model': 'SKY-001', 'price_per_m': 2.0}}}), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog', return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertNotIn('low', chosen, 'a pinned band is not re-picked')
        with serve._db() as conn:
            row = conn.execute("SELECT slug FROM model_tiers WHERE band = 'low'").fetchone()
        self.assertEqual(row[0], 'operator/old', 'the pinned row is left untouched')

class TierValueWindows(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM tier_daily_log')

    def _row(self, log_date, band, slug, price, score, logged_at):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO tier_daily_log (log_date, band, slug, name, price_per_m, benchmark, score, logged_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (log_date, band, slug, slug, price, 'coding_index', score, logged_at))

    def test_reports_ratio_and_pick_age(self):
        today = time.strftime('%Y-%m-%d', time.gmtime())
        for i in range(1, 6):
            self._row(f'2000-01-0{i}', 'coding', 'old', 1.0, 50.0, 0.0)
        # today: twice the value (same score, half the price)
        self._row(today, 'coding', 'cheap', 0.5, 50.0, 111.0)
        out = serve.get_tier_value_windows()
        self.assertAlmostEqual(out['ratios']['coding'], 2.0, places=6)
        w = out['windows']['coding']
        self.assertEqual(w['slug'], 'cheap')
        self.assertEqual(w['sinceTs'], 111.0)
        self.assertGreaterEqual(w['sinceAgeS'], 0.0)
        self.assertAlmostEqual(w['valueRatio'], 2.0, places=6)



class GetCachedModelTiers(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_tiers')

    def test_reads_rows_from_db(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, chosen_at) '
                'VALUES (?, ?, ?, ?, ?)',
                ('low', 'low/m1', 'Low1', 0.2, time.time()),
            )
        tiers = serve.get_cached_model_tiers()
        self.assertEqual(tiers['low'], {'slug': 'low/m1', 'name': 'Low1', 'price': 0.2})



class ShouldWake(unittest.TestCase):
    def _req(self, method, path):
        return types.SimpleNamespace(method=method, url=types.SimpleNamespace(path=path))

    def test_non_get_wakes(self):
        self.assertTrue(serve._should_wake(self._req('POST', '/api/anything')))

    def test_non_waking_read_does_not_wake(self):
        self.assertFalse(serve._should_wake(self._req('GET', '/api/state')))

    def test_escalation_get_except_resolve_does_not_wake(self):
        self.assertFalse(serve._should_wake(self._req('GET', '/api/escalation/abc')))

    def test_resolve_wakes(self):
        self.assertTrue(serve._should_wake(self._req('GET', '/api/escalation/resolve')))

    def test_unlisted_get_wakes(self):
        self.assertTrue(serve._should_wake(self._req('GET', '/api/other')))



class NoStoreMiddleware(unittest.TestCase):
    def test_wakes_dormant_server_and_marks_no_store(self):
        with unittest.mock.patch.object(serve, '_dormant', return_value=True), \
             unittest.mock.patch.object(serve, '_should_wake', return_value=True), \
             unittest.mock.patch.object(serve, '_set_dormant') as setd:
            c = TestClient(serve.app)
            r = c.get('/index.html')
        self.assertEqual(r.status_code, 200)
        setd.assert_called_once_with(False)
        self.assertEqual(r.headers.get('cache-control'), 'no-store')



class SaveCollision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-gap-save-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_saves_valid_grid(self):
        payload = {'grid': [['wall', 'room']], 'cols': 2, 'rows': 1}
        with unittest.mock.patch.object(serve, 'ROOT', self.tmp), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/save', json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, 'saved')
        with open(os.path.join(self.tmp, 'collision_grid.json')) as f:
            self.assertEqual(json.load(f), payload)
        log.assert_called_once_with(None, 'save_collision_grid')

    def test_rejects_invalid_grid(self):
        with unittest.mock.patch.object(serve, 'ROOT', self.tmp), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/save', json={'cols': 2})
        self.assertEqual(r.status_code, 400)



class SaveDoors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-gap-doors-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_saves_valid_doors(self):
        payload = {'pressoffice': {'x': 1, 'y': 2, 'w': 3, 'h': 4}}
        with unittest.mock.patch.object(serve, 'ROOT', self.tmp), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/save-doors', json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, 'saved')
        with open(os.path.join(self.tmp, 'door_triggers.json')) as f:
            self.assertEqual(json.load(f), payload)
        log.assert_called_once_with(None, 'save_door_triggers')

    def test_rejects_invalid_doors(self):
        with unittest.mock.patch.object(serve, 'ROOT', self.tmp), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/save-doors', json={'pressoffice': {'x': 1}})
        self.assertEqual(r.status_code, 400)



class GetState(unittest.TestCase):
    def test_empty_db_seeds_default_roster_then_returns_null(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=None), \
             unittest.mock.patch.object(serve, '_seed_default_roster') as seed, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/state')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json())
        seed.assert_called_once()

    def test_heals_identity_and_adds_agent_keys(self):
        state = {'agentRoster': [{'id': 'ada'}], 'agents': {'ada': {}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save, \
             unittest.mock.patch.object(serve, 'get_or_create_agent_key',
                                        return_value='key-1'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/state')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['agentKeys'], {'ada': 'key-1'})
        save.assert_called_once()

    def test_agent_key_caller_gets_no_agent_keys(self):
        # Identity fix: an agent-key-authenticated caller (a content-executor
        # loopback) must not be handed every other agent's key -- those keys
        # gate key-authenticated endpoints.
        state = {'agentRoster': [{'id': 'ada'}], 'agents': {'ada': {}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented',
                                        return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/state')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('agentKeys', r.json())



class PostState(unittest.TestCase):
    def test_strips_agent_keys_and_merges_server_owned(self):
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value={'templates': {'T': {'mission': 'm'}},
                              'sim': {'owner': 'client'}}), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/state', json={
                'agents': {'ada': {'busy': True}},
                'agentKeys': {'ada': 'secret'},
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, 'saved')
        saved = save.call_args[0][0]
        self.assertNotIn('agentKeys', saved)
        self.assertEqual(saved['templates']['T']['mission'], 'm')
        self.assertEqual(saved['agents']['ada']['busy'], True)

    def test_agent_key_alone_cannot_overwrite_state(self):
        # State mutation is the player's domain: an agent key must not be able
        # to overwrite village state and bypass every escalation/oversight gate.
        with unittest.mock.patch.object(
                serve, 'get_state_from_db', return_value={}), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented',
                                        return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/state', json={'agents': {'ada': {'busy': True}}})
        self.assertEqual(r.status_code, 401)
        save.assert_not_called()



class MergeServerOwnedExtra(unittest.TestCase):
    def test_server_owned_room_teams_sprints_products_wiki_win(self):
        existing = {
            'sim': {'owner': 'server'},
            'agents': {'ada': {'x': 1}},
            'roomDefinitions': {'r': 'rd'},
            'teams': ['team'],
            'sprints': ['sprint'],
            'products': ['product'],
            'wiki': {'page': 1},
        }
        incoming = {'agents': {'ada': {'x': 99, 'busy': True}}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['roomDefinitions'], {'r': 'rd'})
        self.assertEqual(merged['teams'], ['team'])
        self.assertEqual(merged['sprints'], ['sprint'])
        self.assertEqual(merged['products'], ['product'])
        self.assertEqual(merged['wiki'], {'page': 1})
        self.assertEqual(merged['agents']['ada']['x'], 1)
        self.assertEqual(merged['agents']['ada']['busy'], True)



class MergeServerOwnedAgent(unittest.TestCase):
    def test_returns_server_agent_when_client_missing(self):
        self.assertEqual(
            serve._merge_server_owned_agent({'x': 1}, None), {'x': 1})

    def test_returns_client_agent_when_server_missing(self):
        self.assertEqual(
            serve._merge_server_owned_agent(None, {'x': 1}), {'x': 1})

    def test_returns_client_agent_on_non_dict_types(self):
        self.assertEqual(serve._merge_server_owned_agent({'x': 1}, 'not-a-dict'),
                         'not-a-dict')
        self.assertEqual(serve._merge_server_owned_agent('not-a-dict', {'x': 1}),
                         {'x': 1})

    def test_server_spatial_fields_win_over_client(self):
        merged = serve._merge_server_owned_agent(
            {'x': 5, 'y': 6, 'busy': False}, {'x': 1, 'y': 2, 'busy': True})
        self.assertEqual(merged['x'], 5)
        self.assertEqual(merged['y'], 6)
        self.assertEqual(merged['busy'], True)
