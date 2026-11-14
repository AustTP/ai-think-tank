"""Per-provider spend caps + provider-agnostic dialing (operator, 2026-11-13).

Each dialing vendor stops at its own monthly cap (<NAME>_SPEND_CAP_USD);
when the default transport's cap trips, the tier pick re-selects from a
provider with spend left at the same score/price policy; the JEV decisions
lane carries its OWN smaller allowance and its failover chain prefers the
Colab/Laya standby over the free pool. The network is patched throughout;
the ledger, tier rows and decision chain run against a temp DB.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 tests/core/test_provider_caps.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# Isolated sqlite DB (never Postgres, never live) before serve imports.
os.environ.pop('THINK_TANK_DB_URL', None)
os.environ['THINK_TANK_DB'] = os.path.join(tempfile.mkdtemp(prefix='think-tank-provider-caps-env-'),
                                           'test.db')
import serve  # noqa: E402

_TMP = None
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='think-tank-provider-caps-')
    _PATCHER = unittest.mock.patch.object(serve, 'DB_PATH', os.path.join(_TMP, 'test.db'))
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _month():
    return serve._spend_cap_period()


def _day():
    return _month() + '-13'


class ProviderCapMath(unittest.TestCase):
    """The general cap measures the DEFAULT transport's provider: tracked
    lanes minus the decisions lane minus every OTHER provider's rollup.
    Every other provider reads its own rollup -- no subtraction chain."""

    def setUp(self):
        serve._ALLOWANCE_NOTIFIED.clear()

    def _ledger(self, jev=0.0, general=0.0, straitly=0.0, treg=0.0):
        m, d = _month(), _day()
        ledger = {}
        if general:
            ledger['__general__'] = {'byDay': {d: general}}
        if jev:
            ledger['__jev__'] = {'byMonth': {m: jev}}
        if straitly:
            ledger['__provider__/straitly'] = {'byMonth': {m: straitly}}
        if treg:
            ledger['treg'] = {'byDay': {d: treg}}
        return ledger

    def test_general_excludes_jev_other_provider_rollups_and_non_vendors(self):
        ledger = self._ledger(jev=1.0, general=3.0, straitly=0.4, treg=9.0)
        # tracked lanes (3.0 general + 1.0 jev) - jev - straitly rollup;
        # treg is a non-OpenRouter vendor lane and never counts.
        self.assertEqual(serve._provider_month_used_for_cap(ledger, 'openrouter'), 2.6)
        self.assertEqual(serve._provider_month_used_for_cap(ledger, 'straitly'), 0.4)

    def test_jev_spend_never_counts_against_the_general_cap(self):
        # JEV is the ONLY spend: the general cap stays untripped.
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        return_value=self._ledger(jev=4.9)), \
             unittest.mock.patch.object(serve, 'OPENROUTER_SPEND_CAP_USD', 5.0):
            self.assertFalse(serve._provider_cap_exceeded('openrouter'))

    def test_openrouter_cap_trips_and_fires_one_allowance_alert(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        return_value=self._ledger(general=6.0)), \
             unittest.mock.patch.object(serve, 'OPENROUTER_SPEND_CAP_USD', 5.0):
            self.assertTrue(serve._provider_cap_exceeded('openrouter'))
            self.assertTrue(serve._ALLOWANCE_NOTIFIED.get('openrouterSpend'))
            # Second check: the latch holds (one alert per trip).
            self.assertTrue(serve._provider_cap_exceeded('openrouter'))

    def test_openrouter_cap_does_not_trip_on_jev_spend(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        return_value=self._ledger(jev=4.9)), \
             unittest.mock.patch.object(serve, 'OPENROUTER_SPEND_CAP_USD', 5.0):
            self.assertFalse(serve._provider_cap_exceeded('openrouter'))

    def test_straitly_cap_reads_its_own_rollup(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        return_value=self._ledger(straitly=0.6)), \
             unittest.mock.patch.object(serve, 'STRAITLY_SPEND_CAP_USD', 0.5):
            self.assertTrue(serve._provider_cap_exceeded('straitly'))

    def test_jev_lane_cap(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        return_value=self._ledger(jev=0.6)), \
             unittest.mock.patch.object(serve, 'JEV_SPEND_CAP_USD', 0.5):
            self.assertTrue(serve._lane_cap_exceeded('__jev__'))
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        return_value=self._ledger(jev=0.1)), \
             unittest.mock.patch.object(serve, 'JEV_SPEND_CAP_USD', 0.5):
            self.assertFalse(serve._lane_cap_exceeded('__jev__'))

    def test_provider_rollup_accrual(self):
        serve._accrue_provider_spend('straitly', 0.01)
        ledger = serve._spend_ledger_read()
        self.assertEqual(serve._provider_month_used(ledger, 'straitly'), 0.01)

    def test_disabled_cap_never_blocks(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_SPEND_CAP_USD', 0):
            self.assertFalse(serve._provider_cap_exceeded('openrouter'))


class ProviderRollupExcludedFromSharedCap(unittest.TestCase):
    """__provider__/* is an attribution dimension: never double-counted in
    the shared monthly cap or the Bank's service view."""

    def test_rollup_prefix_is_a_rollup(self):
        self.assertTrue(any(p == '__provider__/' for p in serve._ROLLUP_BUCKET_PREFIXES))
        self.assertFalse(serve._is_tracked_service_bucket(
            '__provider__/straitly', {'used': 1.0}))


class DecisionChainOrder(unittest.TestCase):
    """Operator directive 2026-11-13: JEV -> Laya -> free pool. The free
    tier is the LAST resort -- decisions are the most frequent calls, and
    burning its minute/daily allowance on them starves low-band work."""

    def _chain(self, credits=None, paused=False, jev_capped=False):
        laya = serve.COLAB_STANDBY_URL.rstrip('/') + serve.COLAB_STANDBY_DECISION_PATH
        with unittest.mock.patch.object(serve, '_get_setting', return_value=''), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}), \
             unittest.mock.patch.object(serve, '_freepool_route_for_band',
                                        return_value='@freepool:low:gemini-free'), \
             unittest.mock.patch.object(serve, '_colab_standby_enabled',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_colab_standby_forward_alive',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_credits_remaining_cached',
                                        return_value=credits), \
             unittest.mock.patch.object(serve, 'credits_pause_active',
                                        return_value=paused), \
             unittest.mock.patch.object(serve, '_lane_cap_exceeded',
                                        return_value=jev_capped):
            return serve._decision_model_chain(), laya

    def test_healthy_order_paid_laya_free(self):
        chain, laya = self._chain()
        self.assertEqual(chain, [serve.JEV_MODEL, laya, '@freepool:low:gemini-free'])

    def test_jev_cap_drops_paid_laya_first_then_free(self):
        chain, laya = self._chain(jev_capped=True)
        self.assertEqual(chain, [laya, '@freepool:low:gemini-free'])

    def test_credits_pause_keeps_laya_before_free(self):
        chain, laya = self._chain(paused=True)
        self.assertEqual(chain, [laya, '@freepool:low:gemini-free'])

    def test_below_free_floor_laya_only(self):
        chain, laya = self._chain(credits=0.01)
        self.assertEqual(chain, [laya])

    def test_laya_down_free_still_serves(self):
        with unittest.mock.patch.object(serve, '_get_setting', return_value=''), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}), \
             unittest.mock.patch.object(serve, '_freepool_route_for_band',
                                        return_value='@freepool:low:gemini-free'), \
             unittest.mock.patch.object(serve, '_colab_standby_enabled',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_credits_remaining_cached',
                                        return_value=None), \
             unittest.mock.patch.object(serve, 'credits_pause_active',
                                        return_value=False):
            self.assertEqual(serve._decision_model_chain(),
                             [serve.JEV_MODEL, '@freepool:low:gemini-free'])


class TierProviderDialing(unittest.TestCase):
    """A tier row naming a non-default provider resolves the plain slug to
    that provider's transport: base_url, key env, price, is_custom."""

    def setUp(self):
        with serve._db() as conn:
            for table in ('model_tiers', 'model_tier_fallbacks',
                          'model_provider_catalog'):
                conn.execute(f'DELETE FROM {table}')

    def _seed_tier(self, slug='st/m', provider='straitly', price=2.0):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, provider, chosen_at) '
                "VALUES ('low', ?, 'M', ?, ?, ?)", (slug, price, provider, 1.0))

    def test_tier_row_resolves_straitly_spec(self):
        self._seed_tier()
        spec = serve._model_provider_for('st/m')
        self.assertIsNotNone(spec)
        self.assertTrue(spec['is_custom'])
        self.assertEqual(spec['provider'], 'straitly')
        self.assertEqual(spec['base_url'], 'https://api.straitly.ai/v1/chat/completions')
        self.assertEqual(spec['api_key_env'], 'STRAITLY_API_KEY')
        self.assertEqual(spec['price_per_m'], 2.0)

    def test_openrouter_provider_row_rides_default_transport(self):
        self._seed_tier(slug='or/m', provider='openrouter')
        self.assertIsNone(serve._model_provider_for('or/m'))

    def test_null_provider_row_rides_default_transport(self):
        self._seed_tier(slug='leg/m', provider=None)
        self.assertIsNone(serve._model_provider_for('leg/m'))

    def test_fallback_row_resolves_too(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_tier_fallbacks (band, rank, slug, name, price_per_m, provider, chosen_at) '
                "VALUES ('low', 1, 'st/fb', 'FB', 1.5, 'straitly', 1.0)")
        spec = serve._model_provider_for('st/fb')
        self.assertIsNotNone(spec)
        self.assertEqual(spec['price_per_m'], 1.5)

    def test_explicit_spec_for_the_verify_path(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_provider_catalog (provider, model_id, name, context_length, prompt_price, completion_price, price_per_m, image_capable, last_seen) '
                "VALUES ('straitly', 'st/cand', 'Cand', NULL, 0.5, 1.5, 2.0, 0, 1)")
        spec = serve._provider_dialing_spec('st/cand', 'straitly')
        self.assertEqual(spec['model'], 'st/cand')
        self.assertEqual(spec['price_per_m'], 2.0)
        self.assertIsNone(serve._provider_dialing_spec('x', 'openrouter'))
        self.assertIsNone(serve._provider_dialing_spec('x', 'unknown-vendor'))


class PickProviderSelection(unittest.TestCase):
    """refresh_model_tiers picks from a provider with spend left: capped
    default transport -> the next dialing provider in registry order."""

    _CATALOGS = {'openrouter': '_fetch_openrouter_catalog_sync',
                 'straitly': '_fetch_straitly_catalog_sync'}

    def _m(self, mid):
        return {'id': mid, 'name': mid,
                'pricing': {'prompt': '1', 'completion': '1'},
                'architecture': {'input_modalities': ['text'],
                                 'output_modalities': ['text']}}

    def test_uncapped_default_wins(self):
        per = {'openrouter': [self._m('or/a')], 'straitly': [self._m('st/a')]}
        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', self._CATALOGS), \
             unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=False):
            self.assertEqual(serve._pick_provider(per), 'openrouter')

    def test_capped_default_moves_picks_to_straitly(self):
        per = {'openrouter': [self._m('or/a')], 'straitly': [self._m('st/a')]}

        def capped(provider):
            return provider == 'openrouter'
        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', self._CATALOGS), \
             unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        side_effect=capped):
            self.assertEqual(serve._pick_provider(per), 'straitly')

    def test_every_provider_capped_still_returns_a_listing(self):
        per = {'openrouter': [self._m('or/a')]}
        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', self._CATALOGS), \
             unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=True):
            self.assertEqual(serve._pick_provider(per), 'openrouter')


class StraitlyTransport(unittest.TestCase):
    """A straitly-picked slug POSTs to Straitly's endpoint with Straitly's
    key, bills at the tier row's price, accrues the provider rollup, and
    answers to Straitly's own cap."""

    def setUp(self):
        with serve._db() as conn:
            for table in ('model_tiers', 'model_provider_catalog'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, provider, chosen_at) '
                "VALUES ('low', 'st/m', 'M', 2.0, 'straitly', 1.0)")
        serve._ALLOWANCE_NOTIFIED.clear()

    def _resp(self, cost=0.01):
        return json.dumps({'choices': [{'message': {'content': 'hi'}}],
                           'usage': {'total_tokens': 5000, 'cost': cost}}).encode()

    def _post(self, **overrides):
        captured = {}

        def fake_open(req, timeout=None):
            captured['url'] = req.full_url
            captured['auth'] = (req.headers or {}).get('Authorization')
            return self._resp(**overrides)

        with unittest.mock.patch.object(serve, '_urlopen_with_resilience', fake_open), \
             unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'STRAITLY_API_KEY': 'sk-test'}), \
             unittest.mock.patch.object(serve, 'credits_pause_active', return_value=False), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_api_key_is_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_low_paid_blocked', return_value=None), \
             unittest.mock.patch.object(serve, '_provider_cap_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_free_pool_fallback_slugs', return_value=[]), \
             unittest.mock.patch.object(serve, '_try_tier_fallbacks', return_value=None):
            result = serve._call_openrouter_sync('st/m',
                                                 [{'role': 'user', 'content': 'hi'}],
                                                 64)
        return captured, result

    def test_post_dials_straitly_with_its_key(self):
        captured, result = self._post()
        self.assertEqual(captured['url'], 'https://api.straitly.ai/v1/chat/completions')
        self.assertEqual(captured['auth'], 'Bearer sk-test')
        self.assertEqual(result['choices'][0]['message']['content'], 'hi')

    def test_priced_call_accrues_the_provider_rollup(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        return_value={}), \
             unittest.mock.patch.object(serve, '_spend_ledger_write') as write:
            self._post(cost=0.02)
            blobs = [c.args[0] for c in write.call_args_list
                     if c.args and isinstance(c.args[0], dict)
                     and '__provider__/straitly' in c.args[0]]
            self.assertTrue(blobs)
            self.assertEqual(blobs[-1]['__provider__/straitly']['used'], 0.02)

    def test_straitly_cap_blocks_the_dial(self):
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=True), \
             unittest.mock.patch.object(serve, 'credits_pause_active', return_value=False), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_api_key_is_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_low_paid_blocked', return_value=None), \
             unittest.mock.patch.object(serve, '_free_pool_fallback_slugs', return_value=[]), \
             unittest.mock.patch.object(serve, '_try_tier_fallbacks', return_value=None):
            with self.assertRaises(RuntimeError):
                serve._call_openrouter_sync('st/m',
                                            [{'role': 'user', 'content': 'hi'}], 64)


class RefreshMovesPicksToAvailableProvider(unittest.TestCase):
    """With OpenRouter capped and Straitly listed, the refresh selects the
    band picks from Straitly and stamps them provider='straitly' so every
    later call dials Straitly."""

    def setUp(self):
        with serve._db() as conn:
            for table in ('model_tiers', 'model_tier_fallbacks',
                          'tier_daily_log', 'model_provider_catalog',
                          'model_catalog', 'model_benchmark_scores'):
                conn.execute(f'DELETE FROM {table}')

    def _m(self, mid):
        return {'id': mid, 'name': mid,
                'pricing': {'prompt': '0.0000005', 'completion': '0.0000005'},
                'architecture': {'input_modalities': ['text'],
                                 'output_modalities': ['text']}}

    def test_cap_trip_selects_straitly_and_stamps_provider(self):
        catalogs = {'openrouter': '_fetch_openrouter_catalog_sync',
                    'straitly': '_fetch_straitly_catalog_sync'}
        backfilled = {}
        serve.set_model_benchmark_score('st/only', 'intelligence_index', 41.0,
                                        source_url='test://provider-caps')

        def fake_aa(models):
            backfilled['aa'] = [m['id'] for m in models]
            return 0

        def capped(provider):
            return provider == 'openrouter'

        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', catalogs), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=[]), \
             unittest.mock.patch.object(serve, '_fetch_straitly_catalog_sync',
                                        return_value=[self._m('st/only')]), \
             unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        side_effect=capped), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=RuntimeError('no decision')), \
             unittest.mock.patch.object(serve, '_ingest_catalog_benchmarks',
                                        return_value=0), \
             unittest.mock.patch.object(serve, '_ingest_aa_leaderboard', fake_aa), \
             unittest.mock.patch.object(serve, '_ingest_mmmu_leaderboard',
                                        return_value=0), \
             unittest.mock.patch.object(serve, '_ingest_hf_open_llm_fallback',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            serve.asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(backfilled['aa'], ['st/only'])
        # The single candidate's blended price buckets it into 'mid'.
        with serve._db() as conn:
            tier = conn.execute(
                'SELECT slug, provider FROM model_tiers WHERE band = ?',
                ('mid',)).fetchone()
        self.assertEqual(tier, ('st/only', 'straitly'))
        with serve._db() as conn:
            logged = conn.execute(
                'SELECT provider FROM tier_daily_log WHERE band = ?',
                ('mid',)).fetchone()
        self.assertEqual(logged, ('straitly',))


class CapCrossingWatch(unittest.TestCase):
    """A provider spend crossing its monthly cap is a re-pick EVENT within
    one watch poll (operator, 2026-11-13), in EITHER direction (a month
    reset migrates the picks back). The JEV decisions lane is its own spend
    lane with its own failover -- crossing it never re-picks the bands."""

    _CATALOGS = {'openrouter': '_fetch_openrouter_catalog_sync',
                 'straitly': '_fetch_straitly_catalog_sync'}

    def setUp(self):
        serve._PROVIDER_CAP_STATE.clear()
        _reg = unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', self._CATALOGS)
        _reg.start()
        self.addCleanup(_reg.stop)

    def tearDown(self):
        serve._PROVIDER_CAP_STATE.clear()

    def test_first_poll_seeds_without_firing(self):
        # Booting into an already-capped world must not fire a spurious
        # re-pick: the picks were migrated before the restart.
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=True):
            self.assertEqual(serve._provider_cap_transitions_sync(), [])
        self.assertEqual(serve._PROVIDER_CAP_STATE,
                         {'openrouter': True, 'straitly': True})

    def test_crossing_fires_once_then_quiets(self):
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=False):
            self.assertEqual(serve._provider_cap_transitions_sync(), [])
        states = {'openrouter': True, 'straitly': False}

        def capped(provider):
            return states[provider]

        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        side_effect=capped):
            flipped = serve._provider_cap_transitions_sync()
        self.assertEqual(flipped, [('openrouter', True)])
        # Same state on the next poll: no new crossing, no second re-pick.
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        side_effect=capped):
            self.assertEqual(serve._provider_cap_transitions_sync(), [])

    def test_month_reset_crosses_back(self):
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=True):
            serve._provider_cap_transitions_sync()
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=False):
            self.assertEqual(serve._provider_cap_transitions_sync(),
                             [('openrouter', False), ('straitly', False)])

    def test_failed_cap_read_never_fabricates_a_crossing(self):
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=False):
            serve._provider_cap_transitions_sync()

        def boom(provider):
            raise RuntimeError('rollup read failed')

        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        side_effect=boom):
            self.assertEqual(serve._provider_cap_transitions_sync(), [])
        # The failed provider keeps its last known state.
        self.assertEqual(serve._PROVIDER_CAP_STATE,
                         {'openrouter': False, 'straitly': False})


class WatchReadsTheServingProviderCatalog(unittest.TestCase):
    """The price watch must read the SAME catalog the next pick draws from:
    after a cap crossing migrated the picks to Straitly, reading OpenRouter
    alone made every migrated slug look delisted and re-fired a full re-pick
    on every watch cycle (fixed 2026-11-13)."""

    _CATALOGS = {'openrouter': '_fetch_openrouter_catalog_sync',
                 'straitly': '_fetch_straitly_catalog_sync'}

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_tiers')
            conn.execute('DELETE FROM model_benchmark_scores')

    def tearDown(self):
        self.setUp()

    def _m(self, mid, price):
        half = str(price / 2_000_000)
        return {'id': mid, 'name': mid,
                'pricing': {'prompt': half, 'completion': half},
                'architecture': {'input_modalities': ['text'],
                                 'output_modalities': ['text']}}

    def _select(self, band, slug, price, provider):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, provider, chosen_at) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(band) DO UPDATE SET '
                'slug=excluded.slug, price_per_m=excluded.price_per_m, provider=excluded.provider',
                (band, slug, slug, price, provider, 0.0))

    def test_straitly_picks_do_not_look_delisted_on_openrouter(self):
        self._select('mid', 'st/pick', 0.5, 'straitly')
        fetches = []

        def fake_openrouter():
            fetches.append('openrouter')
            return []

        def fake_straitly():
            fetches.append('straitly')
            return [self._m('st/pick', 0.5)]

        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', self._CATALOGS), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        side_effect=fake_openrouter), \
             unittest.mock.patch.object(serve, '_fetch_straitly_catalog_sync',
                                        side_effect=fake_straitly):
            self.assertEqual(serve._tier_watch_moves_sync(), set())
        self.assertEqual(fetches, ['straitly'])

    def test_unknown_provider_stamp_fails_the_cycle(self):
        self._select('mid', 'x/pick', 0.5, 'ghost-vendor')
        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', self._CATALOGS):
            with self.assertRaises(RuntimeError):
                serve._tier_watch_moves_sync()

    def test_openrouter_outage_never_reads_as_mass_delisting(self):
        self._select('mid', 'or/pick', 0.5, None)

        def down():
            raise RuntimeError('catalog unreachable')

        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', self._CATALOGS), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        side_effect=down):
            with self.assertRaises(RuntimeError):
                serve._tier_watch_moves_sync()


class BestValuePickProbesItsOwnProvider(unittest.TestCase):
    """A candidate from a non-default provider must be VERIFY-PROBED through
    that provider's transport: verifying a Straitly slug against OpenRouter
    (which does not list it) fails every band and falls through to the
    classifier fallback (fixed 2026-11-13)."""

    def test_provider_rides_the_verify_probe(self):
        cands = [{'id': 'st/only', 'name': 'st/only', 'price': 0.5}]
        scores = {'st/only': 41.0}
        seen = []

        def probe(model_id, provider=None):
            seen.append((model_id, provider))
            return True

        with unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        side_effect=probe):
            picked = serve.asyncio.run(serve._best_value_pick(
                cands, scores, provider='straitly'))
        self.assertEqual(picked['id'], 'st/only')
        self.assertEqual(seen, [('st/only', 'straitly')])

    def test_default_probe_keeps_the_legacy_signature(self):
        cands = [{'id': 'or/a', 'name': 'or/a', 'price': 0.5}]
        scores = {'or/a': 41.0}
        seen = []

        def probe(model_id, provider=None):
            seen.append((model_id, provider))
            return True

        with unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        side_effect=probe):
            picked = serve.asyncio.run(serve._best_value_pick(cands, scores))
        self.assertEqual(picked['id'], 'or/a')
        self.assertEqual(seen, [('or/a', None)])


class ModelRouterPool(unittest.TestCase):
    """The shared model-router pool (operator, 2026-11-13): OpenRouter and
    Straitly draw from ONE monthly budget -- no hard half per router. Either
    router may spend the whole pool; the POOL gate at the transport is the
    binding constraint and the per-provider caps are disaster brakes."""

    def setUp(self):
        serve._ALLOWANCE_NOTIFIED.clear()

    def tearDown(self):
        serve._ALLOWANCE_NOTIFIED.clear()

    def test_pool_used_sums_provider_rollups(self):
        with unittest.mock.patch.object(serve, 'DIALING_PROVIDERS',
                                        ('a', 'b')):
            before = serve._model_pool_month_used(serve._spend_ledger_read())
            serve._accrue_provider_spend('a', 30.0)
            serve._accrue_provider_spend('b', 0.5)
            used = serve._model_pool_month_used(serve._spend_ledger_read())
        self.assertAlmostEqual(used - before, 30.5)

    def test_pool_trips_at_cap_with_one_alert(self):
        with unittest.mock.patch.object(serve, 'DIALING_PROVIDERS', ('a',)):
            serve._accrue_provider_spend('a', 3.0)
            with unittest.mock.patch.object(serve, 'MODEL_ROUTER_POOL_USD', 2.0):
                self.assertTrue(serve._model_pool_cap_exceeded())
                # Same state on the next poll: still exceeded, still one alert.
                self.assertTrue(serve._model_pool_cap_exceeded())
            with unittest.mock.patch.object(serve, 'MODEL_ROUTER_POOL_USD', 0):
                self.assertFalse(serve._model_pool_cap_exceeded())

    def test_transport_refuses_when_pool_trips_even_if_provider_cap_does_not(self):
        spec = {'is_custom': True, 'free': False, 'provider': 'straitly',
                'model': 'st/m',
                'base_url': 'https://api.straitly.ai/v1/chat/completions',
                'api_key_env': 'STRAITLY_API_KEY', 'price_per_m': 2.0}
        with unittest.mock.patch.object(serve, '_model_pool_cap_exceeded',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience') as post:
            with self.assertRaisesRegex(RuntimeError, 'model router pool'):
                serve._openrouter_chat_post({'model': 'st/m'}, provider=spec)
        post.assert_not_called()

    def test_free_dial_skips_the_pool_gate(self):
        spec = {'is_custom': True, 'free': True, 'provider': 'freepool',
                'model': '@freepool:low:x', 'base_url': 'http://127.0.0.1:9/v1'}

        def fake_open(req, timeout=None):
            return json.dumps({'choices': [{'message': {'content': 'hi'}}],
                               'usage': {'cost': 0.0}}).encode()

        with unittest.mock.patch.object(serve, '_model_pool_cap_exceeded',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        fake_open), \
             unittest.mock.patch.object(serve, 'training_capture'):
            result = serve._openrouter_chat_post({'model': '@freepool:low:x'},
                                                 provider=spec)
        self.assertEqual(result['choices'][0]['message']['content'], 'hi')

    def test_mpp_settlements_draw_the_pool(self):
        with unittest.mock.patch.object(serve, 'DIALING_PROVIDERS',
                                        ('mppvendor',)):
            serve._accrue_provider_spend('mppvendor', 1.0)
            used_before = serve._model_pool_month_used(
                serve._spend_ledger_read())
            serve._accrue_provider_spend('mppvendor', 0.25)
            used_after = serve._model_pool_month_used(
                serve._spend_ledger_read())
        # A machine payment lands in the vendor rollup, so it draws the
        # shared pool like any other model spend.
        self.assertAlmostEqual(used_after - used_before, 0.25)


if __name__ == '__main__':
    unittest.main()
