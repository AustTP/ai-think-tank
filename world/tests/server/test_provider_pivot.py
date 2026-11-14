"""Budget-aware provider pivot tests (operator, 2026-11-14): paid tier calls
move to Straitly when OpenRouter's live balance is at/below the pivot floor;
sublow never spends; the JEV decisions lane is exempt. Everything network,
.env, and DB shaped is patched or redirected into a temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 -m pytest tests/server/test_provider_pivot.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve


_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-pivot-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        _load_env=lambda key=None: {
            'OPENROUTER_API_KEY': 'test-or-key',
            'STRAITLY_API_KEY': 'test-straitly-key',
        },
        # The dial sites use the module constant resolved at import time (it
        # holds the operator's real key when serve imports with the real
        # .env); the tests must never see it in a captured request.
        OPENROUTER_API_KEY='test-or-key',
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _set_tier(band, slug, price=0.1):
    with serve._db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO model_tiers (band, slug, name, '
            'price_per_m, chosen_at, provider) VALUES (?, ?, ?, ?, ?, ?)',
            (band, slug, slug, price, 0.0, 'openrouter'))


def _clear_tier(band):
    with serve._db() as conn:
        conn.execute('DELETE FROM model_tiers WHERE band = ?', (band,))


class BudgetGateTests(unittest.TestCase):
    def setUp(self):
        serve._PROVIDER_BUDGET_CACHE.update({
            'openrouter': {'at': 9e9, 'remaining': None},
            'straitly': {'at': 9e9, 'remaining': None}})

    def test_below_floor_when_balance_read(self):
        with unittest.mock.patch.object(serve, '_provider_remaining_usd',
                                        return_value=4.99):
            self.assertTrue(serve._openrouter_budget_below_pivot_floor())
        with unittest.mock.patch.object(serve, '_provider_remaining_usd',
                                        return_value=5.01):
            self.assertFalse(serve._openrouter_budget_below_pivot_floor())

    def test_unreadable_balance_falls_back_to_pause_latch(self):
        # An unreadable balance alone never blocks or pivots; only the
        # already-latched credits pause does.
        with unittest.mock.patch.object(serve, '_provider_remaining_usd',
                                        return_value=None), \
                unittest.mock.patch.object(serve, 'credits_pause_active',
                                           return_value=False):
            self.assertFalse(serve._openrouter_budget_below_pivot_floor())
        with unittest.mock.patch.object(serve, '_provider_remaining_usd',
                                        return_value=None), \
                unittest.mock.patch.object(serve, 'credits_pause_active',
                                           return_value=True):
            self.assertTrue(serve._openrouter_budget_below_pivot_floor())

    def test_straitly_budget_respects_local_caps_and_balance(self):
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=True):
            self.assertFalse(serve._straitly_has_budget())
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=False), \
                unittest.mock.patch.object(serve, '_model_pool_cap_exceeded',
                                           return_value=True):
            self.assertFalse(serve._straitly_has_budget())
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=False), \
                unittest.mock.patch.object(serve, '_model_pool_cap_exceeded',
                                           return_value=False), \
                unittest.mock.patch.object(serve, '_provider_remaining_usd',
                                           side_effect=lambda n: 88.0
                                           if n == 'straitly' else 2.0):
            self.assertTrue(serve._straitly_has_budget())
        # Unreadable Straitly balance: the caps are the gate, not the balance.
        with unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=False), \
                unittest.mock.patch.object(serve, '_model_pool_cap_exceeded',
                                           return_value=False), \
                unittest.mock.patch.object(serve, '_provider_remaining_usd',
                                           return_value=None):
            self.assertTrue(serve._straitly_has_budget())


class StraitlyPickTests(unittest.TestCase):
    def _install_catalog(self, rows):
        serve._STRAITLY_CATALOG_CACHE['at'] = 9e9
        serve._STRAITLY_CATALOG_CACHE['rows'] = rows

    def tearDown(self):
        serve._STRAITLY_CATALOG_CACHE.update({'at': 0.0, 'rows': None})

    def test_exact_slug_wins(self):
        self._install_catalog([('inclusionai/ling-3.1-flash', 0.18),
                               ('z-ai/glm-5.3-flash', 0.1658)])
        self.assertEqual(serve._straitly_pick_for('inclusionai/ling-3.1-flash', 0.4),
                         'inclusionai/ling-3.1-flash')

    def test_family_match_beats_scored_fallback(self):
        # The exact slug is NOT carried; the family normalizer (publisher
        # relay + quantization/censorship suffixes are noise, size tokens
        # kept) matches the carried glm flash relay even though another
        # model would score higher on the scored fallback.
        self._install_catalog([('z-ai/glm-5.3-flash', 0.1658),
                               ('deepseek/deepseek-v4-flash', 0.1374)])
        self.assertEqual(
            serve._straitly_pick_for('dealignai/glm-5.3-flash-uncensored-fp8',
                                     0.5),
            'z-ai/glm-5.3-flash')

    def test_scored_fallback_respects_price_ceiling(self):
        self._install_catalog([('qwen/qwen3.7-flash', 0.0828),
                               ('deepseek/deepseek-v4-flash', 0.1374),
                               ('openai/gpt-oss-120b', 0.1242)])
        scored = [('qwen/qwen3.7-flash', '{"intelligence_index": {"score": 20}}'),
                  ('deepseek/deepseek-v4-flash', '{"intelligence_index": {"score": 40}}'),
                  ('openai/gpt-oss-120b', '{"intelligence_index": {"score": 35}}')]
        with unittest.mock.patch.object(
                serve, '_db') as db:
            conn = unittest.mock.MagicMock()
            conn.execute.return_value.fetchall.return_value = scored
            db.return_value.__enter__.return_value = conn
            pick = serve._straitly_pick_for('somevendor/unheard-of-model', 0.168)
        self.assertEqual(pick, 'deepseek/deepseek-v4-flash')

    def test_scored_fallback_ceiling_excludes_pricey_models(self):
        self._install_catalog([('qwen/qwen3.7-flash', 0.0828),
                               ('deepseek/deepseek-v4-flash', 0.1374)])
        scored = [('qwen/qwen3.7-flash', '{"intelligence_index": {"score": 20}}'),
                  ('deepseek/deepseek-v4-flash', '{"intelligence_index": {"score": 40}}')]
        with unittest.mock.patch.object(serve, '_db') as db:
            conn = unittest.mock.MagicMock()
            conn.execute.return_value.fetchall.return_value = scored
            db.return_value.__enter__.return_value = conn
            # ceiling 0.084 (tier price 0.042 x 2): the deepseek pick is
            # priced out, qwen stays.
            pick = serve._straitly_pick_for('somevendor/unheard-of-model', 0.084)
        self.assertEqual(pick, 'qwen/qwen3.7-flash')


class PivotProviderTests(unittest.TestCase):
    def tearDown(self):
        serve._STRAITLY_CATALOG_CACHE.update({'at': 0.0, 'rows': None})
        _clear_tier('low')
        _clear_tier('sublow')

    def test_pivot_builds_straitly_spec(self):
        _set_tier('low', 'inclusionai/ling-3.1-flash', 0.18)
        serve._STRAITLY_CATALOG_CACHE.update(
            {'at': 9e9,
             'rows': [('inclusionai/ling-3.1-flash', 0.18)]})
        with unittest.mock.patch.object(serve, '_straitly_has_budget',
                                        return_value=True):
            spec = serve._straitly_pivot_provider('inclusionai/ling-3.1-flash')
        self.assertIsNotNone(spec)
        self.assertEqual(spec['provider'], 'straitly')
        self.assertEqual(spec['model'], 'inclusionai/ling-3.1-flash')
        self.assertEqual(spec['base_url'],
                         'https://api.straitly.ai/v1/chat/completions')
        self.assertEqual(spec['api_key_env'], 'STRAITLY_API_KEY')
        self.assertTrue(spec['is_custom'])

    def test_no_pivot_when_straitly_dry(self):
        _set_tier('low', 'inclusionai/ling-3.1-flash', 0.18)
        serve._STRAITLY_CATALOG_CACHE.update(
            {'at': 9e9, 'rows': [('inclusionai/ling-3.1-flash', 0.18)]})
        with unittest.mock.patch.object(serve, '_straitly_has_budget',
                                        return_value=False):
            self.assertIsNone(
                serve._straitly_pivot_provider('inclusionai/ling-3.1-flash'))

    def test_sublow_never_pivots(self):
        _set_tier('sublow', 'inclusionai/ling-3.0-flash', 0.084)
        serve._STRAITLY_CATALOG_CACHE.update(
            {'at': 9e9, 'rows': [('inclusionai/ling-3.0-flash', 0.084)]})
        with unittest.mock.patch.object(serve, '_straitly_has_budget',
                                        return_value=True):
            self.assertIsNone(
                serve._straitly_pivot_provider('inclusionai/ling-3.0-flash'))

    def test_no_pivot_without_a_carried_model(self):
        _set_tier('high', 'z-ai/glm-5.3', 3.684)
        serve._STRAITLY_CATALOG_CACHE.update({'at': 9e9, 'rows': []})
        with unittest.mock.patch.object(serve, '_straitly_has_budget',
                                        return_value=True):
            self.assertIsNone(serve._straitly_pivot_provider('z-ai/glm-5.3'))


class CallSitePivotTests(unittest.TestCase):
    def setUp(self):
        serve._PROVIDER_BUDGET_CACHE.update({
            'openrouter': {'at': 9e9, 'remaining': 2.27},
            'straitly': {'at': 9e9, 'remaining': 88.0}})
        _set_tier('low', 'inclusionai/ling-3.1-flash', 0.18)
        serve._STRAITLY_CATALOG_CACHE.update(
            {'at': 9e9, 'rows': [('inclusionai/ling-3.1-flash', 0.18)]})

    def tearDown(self):
        serve._STRAITLY_CATALOG_CACHE.update({'at': 0.0, 'rows': None})
        _clear_tier('low')

    def _fake_post(self, capture, status=200):
        # _urlopen_with_resilience returns the raw response bytes; the POST
        # site wraps them in json.loads directly.
        def fake(req, timeout=30, **kwargs):
            capture['url'] = req.full_url
            capture['body'] = json.loads(req.data.decode('utf-8'))
            capture['auth'] = (req.headers.get('Authorization') or '')
            return json.dumps({
                'choices': [{'message': {'content': 'ok'}}],
                'usage': {'total_tokens': 1}}).encode('utf-8')
        return fake

    def test_call_pivots_to_straitly_below_floor(self):
        capture = {}
        with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        side_effect=self._fake_post(capture)):
            result = serve._call_openrouter_once_sync(
                'inclusionai/ling-3.1-flash',
                [{'role': 'user', 'content': 'hi'}], 10)
        self.assertEqual(capture['url'],
                         'https://api.straitly.ai/v1/chat/completions')
        self.assertEqual(capture['body']['model'],
                         'inclusionai/ling-3.1-flash')
        self.assertIn('test-straitly-key', capture['auth'])
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')

    def test_call_keeps_openrouter_when_pivot_disallowed(self):
        capture = {}
        with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        side_effect=self._fake_post(capture)):
            serve._call_openrouter_once_sync(
                'inclusionai/ling-3.1-flash',
                [{'role': 'user', 'content': 'hi'}], 10,
                allow_provider_pivot=False)
        self.assertIn('openrouter.ai', capture['url'])
        self.assertIn('test-or-key', capture['auth'])

    def test_call_degrades_to_free_pool_when_no_pivot_model(self):
        # Straitly carries nothing for the slug: the pre-existing free-pool
        # degrade (paused account, no free route on the paid leg) still gets
        # its turn instead of the pivot.
        serve._STRAITLY_CATALOG_CACHE.update({'at': 9e9, 'rows': []})
        capture = {}
        with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        side_effect=self._fake_post(capture)), \
                unittest.mock.patch.object(
                    serve, 'credits_pause_active', return_value=True), \
                unittest.mock.patch.object(
                    serve, '_freepool_route_for_band',
                    return_value='@freepool:low:gemini-free') as pool, \
                unittest.mock.patch.object(
                    serve, '_freepool_provider_for',
                    return_value={'free': True, 'is_custom': True,
                                  'provider': 'gemini',
                                  'model': 'gemini/gemini-2.0-flash:free',
                                  'base_url': 'https://gemini.example/v1',
                                  'api_key_env': 'GEMINI_API_KEY',
                                  'price_per_m': 0, 'send_reasoning': False}):
            serve._call_openrouter_once_sync(
                'inclusionai/ling-3.1-flash',
                [{'role': 'user', 'content': 'hi'}], 10)
        self.assertIn('gemini.example', capture['url'])
        pool.assert_called()


class SublowFreeOnlyTests(unittest.TestCase):
    def tearDown(self):
        _clear_tier('sublow')

    def test_sublow_returns_none_without_free_route(self):
        # Operator directive 2026-11-14: sublow never spends. With no free
        # pool route the band fails closed instead of riding the paid leg.
        _set_tier('sublow', 'inclusionai/ling-3.0-flash', 0.084)
        with unittest.mock.patch.object(serve, '_freepool_route_for_band',
                                        return_value=None):
            self.assertIsNone(serve._sublow_tier_slug())

    def test_sublow_returns_free_route_when_pool_serves(self):
        with unittest.mock.patch.object(serve, '_freepool_route_for_band',
                                        return_value='@freepool:sublow:or-free'):
            self.assertEqual(serve._sublow_tier_slug(),
                             '@freepool:sublow:or-free')
