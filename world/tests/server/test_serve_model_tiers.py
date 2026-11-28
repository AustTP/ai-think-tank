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
            # Mid band is [0.5, 3) $/M blended since the 2026-11-25
            # retune; $2.4/M lands both inside it.
            self._model('mid/m1', '0.0000012', '0.0000012'),
            self._model('mid/v1', '0.0000005', '0.0000005',
                        input_mods=('image', 'text')),
            self._model('mid/c1', '0.0000012', '0.0000012'),
            # Priced exactly at the HIGH_TIER_MAX_PRICE_USD cap (default
            # $5/M blended, loaded from the operator's .env, inclusive
            # check) and on the high band's lo-inclusive edge
            # ([5, 50) $/M) so the band pool includes the fixture
            # instead of falling through to the intelligence chain.
            self._model('high/h1', '0.0000025', '0.0000025'),
            self._model('vendor/batch:batch', '0.000001', '0.000001'),
            dict(self._model('vendor/reason', '0.000001', '0.000001',
                             input_mods=('image', 'text')),
                 reasoning={'default_enabled': True}),
            self._model('vendor/badprice', 'x', 'x',
                        input_mods=('image', 'text')),
            self._model('vendor/free', '0', '0',
                        input_mods=('image', 'text')),
        ]
        # intelligence_index is a 0-100 scale in the real catalog (AA);
        # the band floor (BAND_MIN_SCORE, default low=30) is read on that
        # scale, so fixtures use it.
        serve.set_model_benchmark_score('low/m1', 'intelligence_index', 70.0, 'u')
        serve.set_model_benchmark_score('mid/m1', 'intelligence_index', 75.0, 'u')
        serve.set_model_benchmark_score('mid/c1', 'coding_index', 0.65, 'u')
        serve.set_model_benchmark_score('high/h1', 'agentic_index', 0.55, 'u')
        serve.set_model_benchmark_score('mid/v1', 'MMMU', 0.6, 'u')
        # The *_TIER_MAX_PRICE_USD caps load from the operator's real .env at
        # import (before setUpModule repoints THINK_TANK_DIR), so a fixture
        # priced near a bucket edge must pin the cap it assumes. Both band
        # comments below say $5/M; 5.0 is inclusive on both the high bucket's
        # lo edge and the ceilings.
        with unittest.mock.patch.object(serve, 'CODING_TIER_MAX_PRICE_USD', 5.0), \
             unittest.mock.patch.object(serve, 'HIGH_TIER_MAX_PRICE_USD', 5.0), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
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
            # Coding picks FIRST now; give it its own anchor so it does not
            # consume the model this low/sublow scenario is asserting on.
            self._model('coding/anchor', '0.000001', '0.000001'),
        ]
        serve.set_model_benchmark_score('coding/anchor', 'coding_index', 90.0, 'u')
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
            self._model('coding/anchor', '0.000001', '0.000001'),
        ]
        serve.set_model_benchmark_score('coding/anchor', 'coding_index', 90.0, 'u')
        serve.set_model_benchmark_score('low/primary', 'intelligence_index', 70.0, 'u')
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

    def test_band_min_score_excludes_below_floor_and_unscored(self):
        # The absolute floor (the operator's "the low tier holds the
        # evaluation score threshold" rule): a scored candidate below the
        # floor is not eligible, and an unscored candidate is never eligible
        # either -- the failure mode that once landed an unscored
        # cheapest-candidate pick in the low band.
        models = [
            self._model('low/unscored', '0.00000005', '0.00000005'),
            self._model('low/weak', '0.00000006', '0.00000006'),
            self._model('low/ok', '0.00000008', '0.00000008'),
        ]
        serve.set_model_benchmark_score('low/weak', 'intelligence_index', 20.0, 'u')
        serve.set_model_benchmark_score('low/ok', 'intelligence_index', 40.0, 'u')
        scores = {r['model_id']: r['score'] for r in serve.get_model_benchmark_scores()}
        pool = serve._band_candidate_pools(models)['low']
        quals = serve._best_value_qualifiers_sync(
            pool, scores, serve.BAND_QUALITY_FLOOR_GAP.get('low'),
            min_score=serve.BAND_MIN_SCORE.get('low'))
        self.assertEqual([m['id'] for m in quals], ['low/ok'])

    def test_floored_band_widens_to_the_cheapest_floor_holder(self):
        # The low band's own price bucket holds no candidate at the floor:
        # the refresh widens across every price bucket (under the band's
        # price ceiling) and picks the cheapest scored model that DOES hold
        # the floor, instead of trusting the unscored classifier fallback.
        models = [
            self._model('low/unscored', '0.00000005', '0.00000005'),
            self._model('mid/holder', '0.0000005', '0.0000005'),
            self._model('coding/anchor', '0.000001', '0.000001'),
        ]
        serve.set_model_benchmark_score('coding/anchor', 'coding_index', 90.0, 'u')
        serve.set_model_benchmark_score('mid/holder', 'intelligence_index', 70.0, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['low']['id'], 'mid/holder')

    def test_floored_band_fails_closed_without_any_scores(self):
        # No evaluation score on file for any metric in the band's chain: a
        # floored band cannot claim to hold the threshold, so the refresh
        # keeps the previous pick instead of handing the tier to the
        # unscored classifier fallback.
        models = [self._model('low/unscored', '0.00000005', '0.00000005')]
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch('builtins.print') as pr:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertNotIn('low', chosen)
        self.assertTrue(any('no evaluation score on file' in str(c)
                            for c in pr.call_args_list))

    def test_corrupt_out_of_scale_score_is_never_a_qualifier(self):
        # BENCHMARK_SCALE_MAX pick-time guard: a stored score above the AA
        # 0-100 ceiling (e.g. the 2026-11-24 Time-per-Task 63.8 read as 638)
        # is corrupt data, never a model that good. It must not set the
        # best-score anchor nor win a pick, and it must not empty the band
        # when honest candidates exist alongside it.
        models = [
            self._model('low/sane', '0.00000008', '0.00000008'),
            self._model('low/corrupt', '0.00000001', '0.00000001'),
            self._model('low/slow', '0.00000009', '0.00000009'),
        ]
        serve.set_model_benchmark_score('low/sane', 'intelligence_index', 40.0, 'u')
        serve.set_model_benchmark_score('low/corrupt', 'intelligence_index', 638.0, 'u')
        serve.set_model_benchmark_score('low/slow', 'intelligence_index', 38.0, 'u')
        scores = {r['model_id']: r['score'] for r in serve.get_model_benchmark_scores()}
        pool = serve._band_candidate_pools(models)['low']
        quals = serve._best_value_qualifiers_sync(
            pool, scores, serve.BAND_QUALITY_FLOOR_GAP.get('low'),
            min_score=serve.BAND_MIN_SCORE.get('low'))
        # slow (38) sits inside the 6-point gap of sane (40), so it stays a
        # legitimate qualifier; the corrupt 638 must be the thing that's gone.
        self.assertEqual([m['id'] for m in quals], ['low/sane', 'low/slow'])

    def test_floored_band_keeps_previous_pick_when_floor_unreachable(self):
        # The floor exists nowhere in the catalog (no scored candidate holds
        # it): fail closed -- keep the previous pick, never an unscored one.
        models = [
            self._model('low/weak', '0.00000005', '0.00000005'),
            self._model('mid/weak', '0.0000005', '0.0000005'),
        ]
        serve.set_model_benchmark_score('low/weak', 'intelligence_index', 10.0, 'u')
        serve.set_model_benchmark_score('mid/weak', 'intelligence_index', 12.0, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch('builtins.print') as pr:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertNotIn('low', chosen)
        self.assertTrue(any('keeping the previous pick' in str(c)
                            for c in pr.call_args_list))

    def test_empty_band_fails_closed_and_skips(self):
        # 2026-11-25 rewrite: no Jev classifier fallback for an unscored
        # pool. The band keeps its previous pick; nothing is written.
        # Mid band is [0.5, 3) $/M blended; $2.4/M keeps the unscored
        # fixture inside the mid bucket so the band reaches the
        # fail-closed path under test.
        models = [self._model('mid/only', '0.0000012', '0.0000012')]
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=RuntimeError('unused')), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch('builtins.print') as pr:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen, {})
        self.assertTrue(any('keeping the previous pick' in str(c)
                            for c in pr.call_args_list))

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
            self._model('coding/anchor', '0.000001', '0.000001'),
        ]
        serve.set_model_benchmark_score('coding/anchor', 'coding_index', 90.0, 'u')
        serve.set_model_benchmark_score('low/plain', 'intelligence_index', 40.0, 'u')
        serve.set_model_benchmark_score('low/reason', 'intelligence_index', 70.0, 'u')
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
            self._model('coding/anchor', '0.000001', '0.000001'),
        ]
        serve.set_model_benchmark_score('coding/anchor', 'coding_index', 90.0, 'u')
        serve.set_model_benchmark_score('low/plain', 'intelligence_index', 40.0, 'u')
        serve.set_model_benchmark_score('low/mand', 'intelligence_index', 70.0, 'u')
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
        # Vision: a scored pool candidate that fails live-verify (and no
        # MMMU fallback path reaches the shortlist either under the rewrite)
        # leaves nothing chosen for the vision band.
        models = [self._model('mid/v1', '0.0000005', '0.0000005',
                              input_mods=('image', 'text'))]
        serve.set_model_benchmark_score('mid/v1', 'MMMU', 60.0, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=False), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch('builtins.print') as pr:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen, {})
        self.assertTrue(any('no working' in str(a) for a in pr.call_args_list))

    def test_no_qualified_scored_candidate_keeps_previous_pick(self):
        # Fail-closed (2026-11-25): the Jev classifier fallback is gone --
        # a classifier pick carries no evaluation score, and trusting one is
        # exactly how a corrupt score row hijacked a band overnight. When no
        # scored candidate qualifies, the band keeps its previous pick.
        # Mid band is [0.5, 3) $/M blended; $2.4/M keeps the unscored
        # fixture inside the mid bucket so the band reaches the
        # fail-closed path under test.
        models = [self._model('mid/only', '0.0000012', '0.0000012')]
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(
                 serve, '_call_openrouter_decision_sync',
                 side_effect=RuntimeError('jev down')), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch('builtins.print') as pr:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen, {})
        self.assertTrue(any('keeping the previous pick' in str(a)
                            for a in pr.call_args_list))

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
        serve.set_model_benchmark_score('coding/anchor', 'coding_index', 90.0, 'u')
        anchor = self._model('coding/anchor', '0.000001', '0.000001')
        self._refresh([self._model('low/m1', '0.0000001', '0.0000001'), anchor])
        first = self._logged_at('low')
        time.sleep(0.01)
        # m1 delisted; m2 (in floor) takes over -> new pick, new window start.
        self._refresh([self._model('low/m2', '0.0000002', '0.0000002'), anchor])
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
        self.assertEqual(tiers['low'], {'slug': 'low/m1', 'name': 'Low1', 'price': 0.2,
                                        'provider': None})



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


class TierDistinctRule(unittest.TestCase):
    """Operator rule (2026-11-24): coding may equal high but must never equal
    sublow/low/mid -- mid peer-reviews the coding band's output, and one
    model grading its own work is the failure mode. Sublow/low/mid/high are
    distinct rungs except the explicitly waived low==mid."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_catalog')
            conn.execute('DELETE FROM model_benchmark_scores')
            conn.execute('DELETE FROM model_tiers')
        # A sibling test class pins tier_provider mid-module; the pin leaks
        # into this class and suppresses every other provider's model
        # listings (pin = pick from that provider's slice only). The rule
        # tests must run on the default registry-order picker.
        no_pin = unittest.mock.patch.object(serve, '_get_setting',
                                            return_value='')
        no_pin.start()
        self.addCleanup(no_pin.stop)

    def _model(self, id_, prompt, completion):
        return {
            'id': id_, 'name': id_,
            'pricing': {'prompt': prompt, 'completion': completion},
            'architecture': {'input_modalities': ['text'],
                             'output_modalities': ['text']},
        }

    def test_coding_is_picked_before_every_other_band(self):
        # Operator directive 2026-11-28: coding resolves first so its score
        # floor is set by the whole catalog before a cheaper rung can claim
        # the model it needs.
        seen = []

        async def record(pool, scores, gap=None, band=None, **kwargs):
            seen.append(band)
            return None

        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=[]), \
             unittest.mock.patch.object(serve, '_best_value_pick', new=record), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(seen[0], 'coding')
        self.assertLess(seen.index('coding'), seen.index('low'))
        self.assertLess(seen.index('coding'), seen.index('sublow'))
        self.assertLess(seen.index('coding'), seen.index('mid'))
        self.assertLess(seen.index('coding'), seen.index('high'))
        self.assertLess(seen.index('coding'), seen.index('vision'))

    def test_coding_never_collapses_onto_mid(self):
        # Coding picks FIRST now, so it takes its own best value model and
        # mid must move off it: one model grading its own work is the failure
        # mode the tier-distinct rule exists to prevent. m2 still sits inside
        # mid's 6-point quality floor gap, so it is an eligible alternative.
        models = [
            # Mid band is [0.5, 3) $/M blended since the 2026-11-25
            # retune; $2.4/M keeps m1 in mid's bucket and $2.5/M keeps
            # m2 there too, preserving the price ranking and the
            # 6-point qualifier gap.
            self._model('mid/m1', '0.0000012', '0.0000012'),  # $2.4/M blended
            self._model('mid/m2', '0.00000125', '0.00000125'),  # $2.5/M blended
        ]
        serve.set_model_benchmark_score('mid/m1', 'intelligence_index', 75.0, 'u')
        serve.set_model_benchmark_score('mid/m2', 'intelligence_index', 74.0, 'u')
        serve.set_model_benchmark_score('mid/m1', 'coding_index', 90.0, 'u')
        serve.set_model_benchmark_score('mid/m2', 'coding_index', 86.0, 'u')
        # The coding/high ceilings load from the operator's real .env at
        # import (CODING=2.00 here), before setUpModule repoints
        # THINK_TANK_DIR; pin the $5/M caps the rule test's comments assume.
        with unittest.mock.patch.object(serve, 'CODING_TIER_MAX_PRICE_USD', 5.0), \
             unittest.mock.patch.object(serve, 'HIGH_TIER_MAX_PRICE_USD', 5.0), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        # m1 is mid's pick only because mid's bucket holds nothing else at a
        # better value; m2 is the coding band's allowed alternative. Both
        # prices sit under the coding/high ceilings pinned to $5/M in the
        # patch below, sublow's $0.09 cap excludes both, and low has no
        # landing bucket.
        self.assertEqual(chosen['coding']['id'], 'mid/m1')
        self.assertNotEqual(chosen['mid']['id'], chosen['coding']['id'])
        self.assertEqual(chosen['mid']['id'], 'mid/m2')

    def test_coding_may_equal_high(self):
        # The waiver: both capability bands resolving onto the SAME strong
        # model is allowed. m_mid holds mid's intelligence score so mid never
        # claims h1; h1 carries the agentic and coding scores so coding's
        # best qualifier and high's best qualifier are the same model -- and
        # that is permitted, because mid peer-reviews but high does not.
        models = [
            self._model('mid/m_mid', '0.0000004', '0.0000004'),  # $0.8/M blended
            # $5.0/M blended: on the high bucket's lo-inclusive edge and
            # exactly at the HIGH_TIER_MAX_PRICE_USD cap (inclusive check).
            self._model('high/h1', '0.0000025', '0.0000025'),
        ]
        serve.set_model_benchmark_score('mid/m_mid', 'intelligence_index', 70.0, 'u')
        serve.set_model_benchmark_score('high/h1', 'agentic_index', 60.0, 'u')
        serve.set_model_benchmark_score('high/h1', 'coding_index', 90.0, 'u')
        # Same import-time .env leak: pin the ceilings this waiver scenario
        # assumes ($5/M, inclusive on both bands' caps).
        with unittest.mock.patch.object(serve, 'CODING_TIER_MAX_PRICE_USD', 5.0), \
             unittest.mock.patch.object(serve, 'HIGH_TIER_MAX_PRICE_USD', 5.0), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['mid']['id'], 'mid/m_mid')
        self.assertEqual(chosen['coding']['id'], 'high/h1')
        self.assertEqual(chosen['high']['id'], 'high/h1')

    def test_band_pick_conflict_matches_family_not_raw_id(self):
        # One model sold under two catalog ids (cheaperinference's
        # 'hy4-flash' vs OpenRouter's vendor-prefixed 'tencent/hy4-flash')
        # is the SAME weights: the tier-distinct rule must see through the
        # vendor prefix, or a band dodges it by hopping provider under a
        # second id (coding paid 2.2x doing exactly that, 2026-11-28).
        self.assertEqual(
            serve._band_pick_conflict('coding', 'tencent/hy4-flash',
                                      {'low': {'id': 'hy4-flash'}}), 'low')
        self.assertEqual(
            serve._band_pick_conflict('low', 'hy4-flash',
                                      {'coding': {'id': 'tencent/hy4-flash'}}),
            'coding')
        # Unrelated families stay allowed.
        self.assertIsNone(
            serve._band_pick_conflict('coding', 'ling-3.0-flash',
                                      {'low': {'id': 'hy4-flash'}}))
        # An unnormalizable slug (empty family) keeps the exact-id rule.
        self.assertIsNone(
            serve._band_pick_conflict('coding', 'vendor/one',
                                      {'low': {'id': 'vendor/two'}}))
        self.assertEqual(
            serve._band_pick_conflict('coding', 'vendor/same',
                                      {'low': {'id': 'vendor/same'}}), 'low')

    def test_same_family_second_id_is_not_the_conflict_escape_hatch(self):
        # The 2026-11-28 morning state inverted by the coding-first order:
        # coding now claims cheaperinference's 'hy4-flash' ($0.079/M) first,
        # and low must avoid BOTH ids of that family (family-aware identity)
        # and walk to the next cheapest different-family qualifier
        # (deepseek) -- genuinely distinct weights at $0.15/M.
        cheaperinference = [self._model('hy4-flash', '0.0000000395', '0.0000000395')]
        openrouter = [
            self._model('tencent/hy4-flash', '0.000000088', '0.000000088'),
            self._model('deepseek/deepseek-v4.1-flash-1017',
                        '0.000000075', '0.000000075'),
        ]
        serve.set_model_benchmark_score('hy4-flash', 'intelligence_index', 35.0, 'u')
        serve.set_model_benchmark_score('hy4-flash', 'coding_index', 31.22, 'u')
        serve.set_model_benchmark_score('tencent/hy4-flash', 'coding_index', 31.22, 'u')
        serve.set_model_benchmark_score('deepseek/deepseek-v4.1-flash-1017',
                                        'intelligence_index', 30.0, 'u')
        serve.set_model_benchmark_score('deepseek/deepseek-v4.1-flash-1017',
                                        'coding_index', 28.24, 'u')
        # conftest trims PROVIDER_CATALOGS to OpenRouter by default; this
        # scenario NEEDS the second provider's listing in the union.
        registry = {'openrouter': '_fetch_openrouter_catalog_sync',
                    'cheaperinference': '_fetch_cheaperinference_catalog_sync'}
        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', registry), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=openrouter), \
             unittest.mock.patch.object(serve, '_fetch_cheaperinference_catalog_sync',
                                        return_value=cheaperinference), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        # Coding keeps the cheapest listing of the family; low is FORCED off
        # both ids of it onto the next qualifier.
        self.assertEqual(chosen['coding']['id'], 'hy4-flash')
        self.assertEqual(chosen['coding'].get('provider'), 'cheaperinference')
        self.assertEqual(chosen['low']['id'],
                         'deepseek/deepseek-v4.1-flash-1017')
        self.assertEqual(chosen['low'].get('provider'), 'openrouter')
        # Both hy4 ids cost more than deepseek's 31.22-2.98 gap under the
        # 6-point floor, and every other band has no landing bucket here.
        self.assertNotIn('sublow', chosen)
        self.assertNotIn('mid', chosen)
        # high may share coding's model: the waiver says both deep-work
        # bands can resolve onto the same weights.
        self.assertEqual(chosen['high']['id'], chosen['coding']['id'])
        with serve._db() as conn:
            row = conn.execute(
                "SELECT slug, provider FROM model_tiers WHERE band = 'coding'"
            ).fetchone()
        self.assertEqual(row[0], 'hy4-flash')
        self.assertEqual(row[1], 'cheaperinference')
