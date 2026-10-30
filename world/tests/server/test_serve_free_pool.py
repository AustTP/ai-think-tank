"""Free-provider pool tests: config validation, quota-aware picking, the
team/global policy switches, and the call-layer fallback behavior. Everything
that would touch the network, the real operator config, or the real
think_tank.db is patched or redirected into a temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 -m unittest tests.server.test_serve_free_pool -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve


_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-free-pool-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        # This suite exercises the pool on purpose: keep its hermeticity gate
        # satisfied while every other path stays patched to the temp dir.
        _FREE_POOL_PROJECT_ROOT=_MODULE_TMP_DIR,
        # Vendor-key tests must not inherit the operator's real .env keys
        # (previously they passed only on machines whose .env had them).
        _load_env=lambda key=None: {
            'GEMINI_API_KEY': 'test-gemini-key',
            'GROQ_API_KEY': 'test-groq-key',
        },
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


CFG_GEMINI = 'https://gemini.example/v1beta/openai/chat/completions'
CFG_GROQ = 'https://groq.example/openai/v1/chat/completions'

_POOL_CONFIG = {
    'enabled': True,
    'bands': ['low'],
    'mode': 'free-first',
    'endpoints': [
        {'name': 'gemini-x', 'service': 'gemini', 'base_url': CFG_GEMINI,
         'model': 'gemini-x-model', 'api_key_env': 'GEMINI_API_KEY',
         'price_per_m': 0, 'rpm': 2, 'rpd': 5, 'tpm': 100000},
        {'name': 'groq-x', 'service': 'groq', 'base_url': CFG_GROQ,
         'model': 'groq-x-model', 'api_key_env': 'GROQ_API_KEY',
         'price_per_m': 0, 'pool_key': 'groq', 'rpm': 3, 'rpd': 4, 'tpm': 0},
        {'name': 'openrouter-free', 'service': 'openrouter',
         'openrouter_free': True, 'price_per_m': 0, 'rpm': 0, 'rpd': 0, 'tpm': 0},
    ],
}


def _write_pool_config(cfg):
    path = os.path.join(_MODULE_TMP_DIR, 'model_providers.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'default': None, 'tiers': {}, 'free_pool': cfg}, fh)
    patcher = unittest.mock.patch.object(serve, '_FREE_POOL_PROVIDERS_PATH', path)
    patcher.start()
    serve._reload_free_pool_config()
    return patcher


def _reset_pool_state():
    with serve._free_pool_lock:
        serve._free_pool_state['rpm'].clear()
        serve._free_pool_state['rpd'].clear()
        serve._free_pool_state['tpm'].clear()
        serve._free_pool_state['cooldown'].clear()
        serve._free_pool_state['or_free_banned'].clear()
        serve._free_pool_state['or_free_chosen'] = None
    # Model circuit state is module-global and survives between tests; the
    # breaker tests open circuits ON PURPOSE, so every suite must start with
    # closed circuits or leaked OPEN state poisons the next test's picks.
    with serve._model_circuit_lock:
        serve._model_circuit_state.clear()
    serve._FREE_POOL_ACTOR_TEAM.set(None)
    serve._invalidate_free_pool_policy_cache()


def _seed_db():
    import sqlite3
    with sqlite3.connect(serve.DB_PATH) as conn:
        now = time.time()
        conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                     "VALUES ('low', 'paid/low-model', 'Paid Low', 0.3, ?)", (now,))
        conn.execute("INSERT OR REPLACE INTO model_catalog (model_id, name, prompt_price, "
                     "completion_price, price_per_m, image_capable, scores, first_seen, last_seen) "
                     "VALUES ('goodmodel:free', 'Good Free', 0, 0, 0, 0, NULL, ?, ?)", (now, now))
        conn.execute("INSERT OR REPLACE INTO model_benchmark_scores (model_id, benchmark, score, "
                     "evaluator, checked_at) VALUES ('goodmodel:free', 'intelligence_index', 20, "
                     "'artificial_analysis', ?)", (now,))


class FreePoolConfigTests(unittest.TestCase):
    def setUp(self):
        _reset_pool_state()

    def test_valid_config_parses(self):
        _write_pool_config(_POOL_CONFIG)
        cfg = serve._load_free_pool_config()
        self.assertEqual(cfg['mode'], 'free-first')
        self.assertEqual([e['name'] for e in cfg['endpoints']],
                         ['gemini-x', 'groq-x', 'openrouter-free'])
        self.assertEqual(cfg['endpoints'][1]['pool_key'], 'groq')
        self.assertEqual(cfg['endpoints'][2]['service'], 'openrouter')

    def test_disabled_or_missing_returns_none(self):
        _write_pool_config({**_POOL_CONFIG, 'enabled': False})
        self.assertIsNone(serve._load_free_pool_config())
        _write_pool_config(None)
        self.assertIsNone(serve._load_free_pool_config())

    def test_bad_mode_rejected(self):
        _write_pool_config({**_POOL_CONFIG, 'mode': 'whenever'})
        self.assertIsNone(serve._load_free_pool_config())

    def test_bad_name_and_url_dropped(self):
        cfg = {**_POOL_CONFIG, 'endpoints': _POOL_CONFIG['endpoints'] + [
            {'name': 'bad name', 'base_url': CFG_GEMINI, 'model': 'm'},
            {'name': 'badurl', 'base_url': 'ftp://x', 'model': 'm'},
            {'name': 'noslug', 'service': 'gemini'},
        ]}
        _write_pool_config(cfg)
        names = [e['name'] for e in serve._load_free_pool_config()['endpoints']]
        self.assertNotIn('bad name', names)
        self.assertNotIn('badurl', names)
        self.assertNotIn('noslug', names)


class FreePoolPickerTests(unittest.TestCase):
    def setUp(self):
        _reset_pool_state()
        _write_pool_config(_POOL_CONFIG)
        # Config-independent OR-free readiness: picker tests stub the catalog
        # pick so 'openrouter-free' behaves like any other endpoint; the real
        # catalog pick has its own test in FreePoolProviderTests.
        or_free = unittest.mock.patch.object(serve, '_freepool_openrouter_free_model',
                                             return_value='goodmodel:free')
        or_free.start()
        self.addCleanup(or_free.stop)

    def test_order_and_key_requirement(self):
        with unittest.mock.patch.object(serve, '_load_env', return_value={}):
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:openrouter-free')
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'GEMINI_API_KEY': 'k',
                                                      'GROQ_API_KEY': 'g'}):
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:gemini-x')

    def test_allowed_kinds_scope_gates_the_picker(self):
        """2026-10-30: the operator's second Gemini key is scoped to the ask
        lane only. An endpoint with allowed_kinds serves ONLY those kinds;
        the ambient default kind is 'work', so scoped endpoints are invisible
        to any path that never opted in."""
        scoped = {**_POOL_CONFIG, 'endpoints': [
            {**_POOL_CONFIG['endpoints'][0], 'allowed_kinds': ['ask']},
            _POOL_CONFIG['endpoints'][1],
        ]}
        _write_pool_config(scoped)
        with unittest.mock.patch.object(
                serve, '_load_env',
                return_value={'GEMINI_API_KEY': 'k', 'GROQ_API_KEY': 'g'}):
            # ambient default (no kind set) = 'work' -> scoped endpoint skipped
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:groq-x')
            # explicitly work -> still skipped
            serve._set_free_pool_kind('work')
            try:
                self.assertEqual(serve._freepool_route_for_band('low'),
                                 '@freepool:low:groq-x')
                # ask -> scoped endpoint becomes first in line
                serve._set_free_pool_kind('ask')
                self.assertEqual(serve._freepool_route_for_band('low'),
                                 '@freepool:low:gemini-x')
            finally:
                serve._set_free_pool_kind(None)

    def test_unscoped_endpoints_serve_every_kind(self):
        unscoped = {**_POOL_CONFIG, 'endpoints': [
            {**_POOL_CONFIG['endpoints'][0], 'allowed_kinds': []},
        ]}
        _write_pool_config(unscoped)
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'GEMINI_API_KEY': 'k'}):
            for kind in (None, 'work', 'ask', 'future-kind'):
                serve._set_free_pool_kind(kind)
                try:
                    self.assertEqual(serve._freepool_route_for_band('low'),
                                     '@freepool:low:gemini-x')
                finally:
                    serve._set_free_pool_kind(None)

    def test_rpm_exhaustion_moves_to_next(self):
        with serve._free_pool_lock:
            serve._free_pool_state['rpm']['gemini-x'] = {
                'minute': serve._freepool_minute_bucket(),
                'stamps': [time.time()] * 2}
        self.assertEqual(serve._freepool_route_for_band('low'),
                         '@freepool:low:groq-x')

    def test_rpd_exhaustion_skips_endpoint_for_the_day(self):
        with serve._free_pool_lock:
            serve._free_pool_state['rpd']['gemini-x'] = {
                'date': serve._utc_today(), 'count': 5}
            serve._free_pool_state['rpd']['groq-x'] = {
                'date': serve._utc_today(), 'count': 4}
        self.assertEqual(serve._freepool_route_for_band('low'),
                         '@freepool:low:openrouter-free')

    def test_tpm_exhaustion_skips_pool(self):
        with serve._free_pool_lock:
            serve._free_pool_state['tpm']['gemini-x'] = {
                'minute': serve._freepool_minute_bucket(), 'tokens': 100000}
        self.assertEqual(serve._freepool_route_for_band('low'),
                         '@freepool:low:groq-x')

    def test_cooldown_skips_endpoint(self):
        with serve._free_pool_lock:
            serve._free_pool_state['cooldown']['gemini-x'] = time.time() + 120
        self.assertEqual(serve._freepool_route_for_band('low'),
                         '@freepool:low:groq-x')

    def test_all_exhausted_returns_none(self):
        with unittest.mock.patch.object(serve, '_load_env', return_value={}):
            with unittest.mock.patch.object(serve, '_freepool_openrouter_free_model',
                                            return_value=None):
                self.assertIsNone(serve._freepool_route_for_band('low'))
    def test_band_not_in_pool_returns_none(self):
        self.assertIsNone(serve._freepool_route_for_band('high'))

    def test_budget_only_mode_waits_for_pause(self):
        _write_pool_config({**_POOL_CONFIG, 'mode': 'budget-only'})
        self.assertIsNone(serve._freepool_route_for_band('low'))
        with unittest.mock.patch.object(serve, 'credits_pause_active',
                                        return_value=True):
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:gemini-x')

    def test_paid_first_mode_pays_but_force_assists(self):
        _write_pool_config({**_POOL_CONFIG, 'mode': 'paid-first'})
        self.assertIsNone(serve._freepool_route_for_band('low'))
        self.assertEqual(serve._freepool_route_for_band('low', force=True),
                         '@freepool:low:gemini-x')

    def test_openrouter_free_model_picked_from_catalog(self):
        _seed_db()
        self.assertEqual(serve._freepool_openrouter_free_model('low'),
                         'goodmodel:free')

class FreePoolPolicyTests(unittest.TestCase):
    def setUp(self):
        _reset_pool_state()
        _write_pool_config(_POOL_CONFIG)

    def _state(self, policy):
        return {'freePoolPolicy': policy}

    def test_global_switch_skips_service(self):
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._state({'globalDisabled': {'gemini': True},
                                          'teams': {}})):
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:groq-x')

    def test_team_switch_skips_service_for_that_team_only(self):
        # An actor with no resolvable team is governed by the global switches
        # only: a team switch must not block an unattributed pick.
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._state({'globalDisabled': {},
                                          'teams': {'t1': {'groq': True}}})):
            serve._set_free_pool_actor('ada')  # temp state has no roster
            self.assertIsNone(serve._FREE_POOL_ACTOR_TEAM.get())
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:gemini-x')
        # A stamped actor from team t1 sees the groq endpoints blocked while
        # every other service stays available.
        serve._FREE_POOL_ACTOR_TEAM.set('t1')
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._state({'globalDisabled': {},
                                          'teams': {'t1': {'groq': True}}})):
            groq = serve._free_pool_cfg()['endpoints'][1]
            self.assertEqual(serve._freepool_service_blocked(groq), 'team')
            self.assertFalse(serve._freepool_ready(groq, 'low'))
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:gemini-x')

    def test_global_overrides_team(self):
        serve._FREE_POOL_ACTOR_TEAM.set('t1')
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._state({'globalDisabled': {'groq': True},
                                          'teams': {'t1': {'groq': False}}})):
            self.assertFalse(serve._freepool_ready(
                serve._free_pool_cfg()['endpoints'][1], 'low'))

    def test_cache_invalidation_lands_write(self):
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._state({'globalDisabled': {}, 'teams': {}})):
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:gemini-x')
            serve._invalidate_free_pool_policy_cache()
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._state({'globalDisabled': {'gemini': True},
                                          'teams': {}})):
            serve._invalidate_free_pool_policy_cache()
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:groq-x')

    def test_ttc_recognizes_freepool_low(self):
        with unittest.mock.patch.multiple(
                serve, TTC_ENABLED=True,
                _high_tier_slug=lambda: 'paid/high-model',
                _coding_tier_slug=lambda: 'paid/coding-model',
                _reasoning_tier_slug=lambda: 'paid/reasoning-model',
                _low_tier_slug=lambda: '@freepool:low:gemini-x',
                _mid_tier_slug=lambda: 'paid/mid-model'):
            self.assertTrue(serve._ttc_should_deliberate('@freepool:low:gemini-x'))
            self.assertFalse(serve._ttc_should_deliberate('@freepool:high:gemini-x'))


class FreePoolProviderTests(unittest.TestCase):
    def setUp(self):
        _reset_pool_state()
        _write_pool_config(_POOL_CONFIG)

    def test_provider_shape(self):
        prov = serve._freepool_provider_for('@freepool:low:groq-x')
        self.assertEqual(prov['model'], 'groq-x-model')
        self.assertTrue(prov['is_custom'])
        self.assertTrue(prov['free'])
        self.assertEqual(prov['service'], 'groq')
        self.assertEqual(prov['pool_key'], 'groq')

    def test_unknown_slug_returns_none(self):
        self.assertIsNone(serve._freepool_provider_for('@freepool:low:nonexistent'))
        self.assertIsNone(serve._freepool_provider_for('@freepool:low'))
        self.assertIsNone(serve._freepool_provider_for('openai/gpt-x'))

    def test_openrouter_free_is_not_custom(self):
        _seed_db()
        prov = serve._freepool_provider_for('@freepool:low:openrouter-free')
        self.assertFalse(prov['is_custom'])
        self.assertEqual(prov['model'], 'goodmodel:free')
        self.assertTrue(prov['openrouter_free'])

    def test_slug_band_and_paid_fallback(self):
        _seed_db()
        self.assertEqual(serve._freepool_slug_band('@freepool:low:groq-x'), 'low')
        self.assertIsNone(serve._freepool_slug_band('paid/low-model'))
        self.assertEqual(serve._freepool_paid_fallback('@freepool:low:groq-x'),
                         'paid/low-model')

    def test_rejected_openrouter_free_model_is_banned(self):
        _seed_db()
        first = serve._freepool_openrouter_free_model('low')
        serve._freepool_note_rejected({'free': True, 'openrouter_free': True,
                                       'model': first})
        self.assertNotEqual(serve._freepool_openrouter_free_model('low'), first)


    def test_or_free_floor_drops_models_below_it(self):
        _seed_db()
        now = time.time()
        with serve._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO model_catalog (model_id, name, prompt_price, "
                "completion_price, price_per_m, image_capable, scores, first_seen, "
                "last_seen) VALUES ('weakmodel:free', 'Weak', 0, 0, 0, 0, NULL, ?, ?)",
                (now, now))
            conn.execute(
                "INSERT OR REPLACE INTO model_benchmark_scores (model_id, benchmark, "
                "score, evaluator, checked_at) VALUES ('weakmodel:free', "
                "'intelligence_index', 15, 'aa', ?)", (now,))
        # Default floor is 20: the 15-score model is skipped even though it is
        # the only other zero-price candidate.
        self.assertEqual(serve._freepool_openrouter_free_model('low'),
                         'goodmodel:free')

    def test_or_free_floor_config_override(self):
        _seed_db()
        _write_pool_config({**_POOL_CONFIG, 'or_free_min_score': 30})
        # goodmodel:free scores 20, below a raised floor: nothing qualifies.
        self.assertIsNone(serve._freepool_openrouter_free_model('low'))


class FreePoolQuotaTests(unittest.TestCase):
    def setUp(self):
        _reset_pool_state()
        _write_pool_config(_POOL_CONFIG)

    # The quota helpers observe PROVIDER specs (they carry free/name/pool_key);
    # the call layers build those from endpoints, so the tests do the same.
    _prov = {'free': True, 'name': 'gemini-x', 'service': 'gemini',
             'pool_key': 'gemini-x'}

    def test_call_fills_windows(self):
        serve._note_free_pool_call(self._prov, {'usage': {'prompt_tokens': 100,
                                                          'completion_tokens': 50}})
        with serve._free_pool_lock:
            self.assertEqual(len(serve._free_pool_state['rpm']['gemini-x']['stamps']), 1)
            self.assertEqual(serve._free_pool_state['tpm']['gemini-x']['tokens'], 150)
            self.assertEqual(serve._free_pool_state['rpd']['gemini-x']['count'], 1)

    def test_rate_limit_sets_cooldown(self):
        serve._note_free_pool_rate_limited(self._prov, 'rate limit exceeded', retry_after=90)
        with serve._free_pool_lock:
            until = serve._free_pool_state['cooldown']['gemini-x']
        self.assertGreater(until, time.time() + 60)

    def test_daily_limit_cools_until_utc_midnight(self):
        serve._note_free_pool_rate_limited(self._prov, 'requests per day limit reached')
        with serve._free_pool_lock:
            until = serve._free_pool_state['cooldown']['gemini-x']
        self.assertGreater(until, serve._next_utc_midnight() - 60)


_GOOD_RESULT = {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}],
                'usage': {'cost': 0.0, 'total_tokens': 10}}
_STARVED_RESULT = {'choices': [{'message': {'content': None}, 'finish_reason': 'length'}],
                   'usage': {'cost': 0.0}}


class _CallLayerBase(unittest.TestCase):
    def setUp(self):
        _reset_pool_state()
        _seed_db()
        _write_pool_config(_POOL_CONFIG)
        self.patches = [
            unittest.mock.patch.object(serve, 'credits_pause_active', return_value=False),
            unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded', return_value=False),
            unittest.mock.patch.object(serve, '_api_key_is_broken', return_value=False),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])


class CallLayerTests(_CallLayerBase):
    def test_free_route_allowed_during_credits_pause(self):
        with unittest.mock.patch.object(serve, 'credits_pause_active',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        return_value=_GOOD_RESULT):
            result = serve._call_openrouter_once_sync('@freepool:low:gemini-x',
                                                      [{'role': 'user', 'content': 'hi'}],
                                                      64)
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')

    def test_free_route_allowed_over_spend_cap(self):
        with unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        return_value=_GOOD_RESULT):
            result = serve._call_openrouter_once_sync('@freepool:low:groq-x',
                                                      [{'role': 'user', 'content': 'hi'}],
                                                      64)
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')

    def test_paid_slug_still_blocked_over_spend_cap(self):
        with unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=True):
            with self.assertRaises(RuntimeError):
                serve._call_openrouter_once_sync('paid/low-model',
                                                 [{'role': 'user', 'content': 'hi'}], 64)

    def test_free_429_falls_through_to_paid(self):
        import urllib.error
        err = urllib.error.HTTPError('http://x', 429, 'quota', {}, None)
        called = []

        def fake_post(body):
            called.append(body['model'])
            if body['model'].startswith('@freepool:'):
                raise err
            return _GOOD_RESULT

        with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=fake_post), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._call_openrouter_once_sync('@freepool:low:gemini-x',
                                                      [{'role': 'user', 'content': 'hi'}],
                                                      64)
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')
        # The fake sees the REWRITTEN body: the free slug became the concrete
        # free model, then the fallback ran the band's paid pick.
        # The patched _openrouter_chat_post sees the RAW body. The cascade
        # tries the other free endpoints before spending money: failed
        # gemini-x -> healthy groq-x -> or-free -> the band's paid pick.
        self.assertEqual(called, ['@freepool:low:gemini-x',
                                  '@freepool:low:groq-x',
                                  '@freepool:low:openrouter-free',
                                  'paid/low-model'])

    def test_free_starvation_falls_through_to_paid(self):
        def fake_post(body):
            if body['model'].startswith('@freepool:'):
                return dict(_STARVED_RESULT)
            return _GOOD_RESULT

        with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=fake_post), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._call_openrouter_once_sync('@freepool:low:gemini-x',
                                                      [{'role': 'user', 'content': 'hi'}],
                                                      64)
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')

    def test_paid_low_failure_assists_with_free(self):
        def fake_post(body):
            if body['model'] == 'paid/low-model':
                raise RuntimeError('paid endpoint down')
            return _GOOD_RESULT

        with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=fake_post), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._call_openrouter_once_sync('paid/low-model',
                                                      [{'role': 'user', 'content': 'hi'}],
                                                      64)
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')


class PaidSwitchTests(_CallLayerBase):
    """The 'paid' policy switch: blocks ONLY the low band's paid leg (the DB
    pick or a @provider:low pin), scoped global/team like the free switches,
    global overriding team."""

    def _policy_state(self, policy):
        return {'freePoolPolicy': policy}

    def test_global_paid_block_raises_for_low_leg_only(self):
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._policy_state({'globalDisabled': {'paid': True},
                                                 'teams': {}})):
            serve._invalidate_free_pool_policy_cache()
            with self.assertRaisesRegex(RuntimeError,
                                        'paid low tier is switched off by the global'):
                serve._call_openrouter_once_sync('paid/low-model',
                                                 [{'role': 'user', 'content': 'hi'}], 64)
            # The mid band is untouched by the low-scoped switch.
            with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                            return_value=_GOOD_RESULT):
                result = serve._call_openrouter_once_sync(
                    'paid/mid-model', [{'role': 'user', 'content': 'hi'}], 64)
            self.assertEqual(result['choices'][0]['message']['content'], 'ok')
            # And the free pool itself keeps serving.
            with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                            return_value=_GOOD_RESULT):
                result = serve._call_openrouter_once_sync(
                    '@freepool:low:gemini-x', [{'role': 'user', 'content': 'hi'}], 64)
            self.assertEqual(result['choices'][0]['message']['content'], 'ok')

    def test_team_paid_block_scoped_to_actor(self):
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._policy_state({'globalDisabled': {},
                                                 'teams': {'t1': {'paid': True}}})):
            serve._invalidate_free_pool_policy_cache()
            serve._FREE_POOL_ACTOR_TEAM.set('t1')
            with self.assertRaisesRegex(RuntimeError, 'team policy switch'):
                serve._call_openrouter_once_sync('paid/low-model',
                                                 [{'role': 'user', 'content': 'hi'}], 64)
            # Unattributed calls (player chat, JEV quorum) are governed by the
            # global switch only.
            serve._FREE_POOL_ACTOR_TEAM.set(None)
            with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                            return_value=_GOOD_RESULT):
                result = serve._call_openrouter_once_sync(
                    'paid/low-model', [{'role': 'user', 'content': 'hi'}], 64)
            self.assertEqual(result['choices'][0]['message']['content'], 'ok')

    def test_global_overrides_team_for_paid(self):
        serve._FREE_POOL_ACTOR_TEAM.set('t1')
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._policy_state({'globalDisabled': {'paid': True},
                                                 'teams': {'t1': {'paid': False}}})):
            serve._invalidate_free_pool_policy_cache()
            with self.assertRaisesRegex(RuntimeError, 'global policy switch'):
                serve._call_openrouter_once_sync('paid/low-model',
                                                 [{'role': 'user', 'content': 'hi'}], 64)

    def test_provider_pin_low_leg_is_blocked_too(self):
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._policy_state({'globalDisabled': {'paid': True},
                                                 'teams': {}})), \
             unittest.mock.patch.object(serve, 'MODEL_PROVIDER_OVERRIDES',
                                        {'tiers': {'low': {'provider': 'custom',
                                                           'base_url': 'https://x.example/v1',
                                                           'model': 'pinned-low',
                                                           'api_key_env': 'X',
                                                           'price_per_m': 0.5}}}), \
             unittest.mock.patch.object(serve, '_load_env', return_value={'X': 'k'}):
            serve._invalidate_free_pool_policy_cache()
            with self.assertRaisesRegex(RuntimeError, 'paid low tier is switched off'):
                serve._call_openrouter_once_sync('@provider:low',
                                                 [{'role': 'user', 'content': 'hi'}], 64)

    def test_freepool_fallback_skips_paid_when_blocked(self):
        import urllib.error
        err = urllib.error.HTTPError('http://x', 503, 'unavailable', {}, None)
        called = []

        def fake_post(body):
            called.append(body['model'])
            if body['model'].startswith('@freepool:'):
                raise err
            return _GOOD_RESULT

        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self._policy_state({'globalDisabled': {'paid': True},
                                                 'teams': {}})), \
             unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=fake_post), \
             unittest.mock.patch.object(serve, 'log_action'):
            serve._invalidate_free_pool_policy_cache()
            with self.assertRaises(urllib.error.HTTPError):
                serve._call_openrouter_once_sync('@freepool:low:gemini-x',
                                                 [{'role': 'user', 'content': 'hi'}], 64)
        self.assertNotIn('paid/low-model', called)


class PaidSwitchPickerTests(FreePoolPickerTests):
    """With the low paid leg switched off, the pool serves regardless of the
    configured mode -- there is no paid alternative to conserve."""

    def test_budget_only_serves_free_when_paid_blocked(self):
        _write_pool_config({**_POOL_CONFIG, 'mode': 'budget-only'})
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value={'freePoolPolicy': {'globalDisabled': {'paid': True},
                                                 'teams': {}}}):
            serve._invalidate_free_pool_policy_cache()
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:gemini-x')

    def test_paid_first_serves_free_when_paid_blocked(self):
        _write_pool_config({**_POOL_CONFIG, 'mode': 'paid-first'})
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value={'freePoolPolicy': {'globalDisabled': {'paid': True},
                                                 'teams': {}}}):
            serve._invalidate_free_pool_policy_cache()
            self.assertEqual(serve._freepool_route_for_band('low'),
                             '@freepool:low:gemini-x')


class FreePoolBreakerTests(_CallLayerBase):
    """Consultant finding 2026-10-27: free-pool breakers tripping on
    provider-side 429/503 storms re-probed after only 5 minutes and burned
    the probe, so free endpoints flap. Freepool slugs now open for 15 min;
    paid slugs keep the 5-minute window."""

    def test_freepool_breaker_cooldown_is_longer(self):
        import time as _time
        for slug in ('@freepool:low:gemini-x', 'paid/low-model'):
            for _ in range(serve.CIRCUIT_BREAKER_THRESHOLD):
                serve.record_model_result(slug, False)
        self.assertTrue(serve.is_model_circuit_broken('@freepool:low:gemini-x'))
        self.assertTrue(serve.is_model_circuit_broken('paid/low-model'))
        free_left = serve._model_circuit_state['@freepool:low:gemini-x']['open_until'] - _time.time()
        paid_left = serve._model_circuit_state['paid/low-model']['open_until'] - _time.time()
        self.assertAlmostEqual(free_left, serve.FREE_POOL_CIRCUIT_COOLDOWN_S, delta=5)
        self.assertAlmostEqual(paid_left, serve.CIRCUIT_BREAKER_COOLDOWN_S, delta=5)


if __name__ == '__main__':
    unittest.main()
