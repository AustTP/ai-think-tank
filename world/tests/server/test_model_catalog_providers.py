"""Multi-provider catalog sync tests (provider-agnostic model registry).

The catalog layer must not know OpenRouter's name outside the call
transport: each provider's raw slice lands in model_provider_catalog, the
canonical model_catalog row carries the CHEAPEST listing's price plus a
`providers` provenance list, purge only hits a model gone from EVERY
provider (its scores go with it), and a provider's own router product
(jev-router) never lands anywhere. The network, the real fetches and the
live think_tank.db are patched or redirected into a temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 tests/server/test_model_catalog_providers.py
"""
import asyncio
import json
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
    _TMP = tempfile.mkdtemp(prefix='think-tank-provider-catalog-')
    _PATCHER = unittest.mock.patch.object(serve, 'DB_PATH', os.path.join(_TMP, 'test.db'))
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _clear():
    with serve._db() as conn:
        for table in ('model_provider_catalog', 'model_catalog',
                      'model_benchmark_scores', 'catalog_price_daily'):
            conn.execute(f'DELETE FROM {table}')


def _model(mid, prompt, completion, image=False, ctx=None, name=None):
    arch = {'input_modalities': ['image', 'text'] if image else ['text'],
            'output_modalities': ['text']}
    m = {'id': mid, 'name': name or mid,
         'pricing': {'prompt': str(prompt), 'completion': str(completion)},
         'architecture': arch}
    if ctx:
        m['context_length'] = ctx
    return m


def _stale():
    return serve.time.time() - serve.MODEL_TIER_REFRESH_INTERVAL_S - 10


class ProviderSliceRows(unittest.TestCase):
    """model_provider_catalog: one row per (provider, model_id), with each
    provider's grace purge scoped strictly to its own slice."""

    def setUp(self):
        _clear()

    def test_upsert_and_provider_scoped_grace_purge(self):
        stale = _stale()
        with serve._db() as conn:
            for provider in ('straitly', 'openrouter'):
                conn.execute(
                    'INSERT INTO model_provider_catalog (provider, model_id, name, context_length, prompt_price, completion_price, price_per_m, image_capable, last_seen) '
                    "VALUES (?, 'gone/old', 'Old', NULL, 1, 1, 2, 0, ?)",
                    (provider, stale))
        purged = serve._sync_provider_catalog_rows(
            'straitly', [_model('fresh/a', 1, 1, ctx=4096)])
        self.assertEqual(purged, 1)
        with serve._db() as conn:
            rows = conn.execute(
                'SELECT provider, model_id FROM model_provider_catalog '
                'ORDER BY provider, model_id').fetchall()
        # Only straitly's stale row died; openrouter's slice is untouched,
        # and the fresh model landed with its context length.
        self.assertEqual(rows, [('openrouter', 'gone/old'), ('straitly', 'fresh/a')])
        with serve._db() as conn:
            ctx = conn.execute(
                'SELECT context_length FROM model_provider_catalog '
                "WHERE provider = 'straitly' AND model_id = 'fresh/a'").fetchone()[0]
        self.assertEqual(ctx, 4096)

    def test_router_model_never_lands(self):
        purged = serve._sync_provider_catalog_rows(
            'straitly', [_model('jev-router', 1, 1)])
        with serve._db() as conn:
            rows = conn.execute('SELECT model_id FROM model_provider_catalog').fetchall()
        self.assertEqual(rows, [])
        self.assertEqual(purged, 0)


class MergeCatalogs(unittest.TestCase):
    """_merge_provider_catalogs: union by id, cheapest price wins, providers
    provenance, router models filtered, unparseable pricing excluded."""

    def setUp(self):
        _clear()

    def test_shared_id_takes_cheapest_price_and_records_providers(self):
        per = {
            'openrouter': [_model('z-ai/glm-5.3', 2, 2)],
            'straitly': [_model('z-ai/glm-5.3', 1, 1)],
        }
        union, keep = serve._merge_provider_catalogs(per)
        self.assertEqual(keep, {'z-ai/glm-5.3'})
        self.assertEqual(len(union), 1)
        entry = union[0]
        self.assertEqual(entry['providers'], ['openrouter', 'straitly'])
        self.assertEqual(float(entry['pricing']['prompt']), 1)

    def test_more_expensive_challenger_does_not_touch_pricing(self):
        per = {
            'openrouter': [_model('or/cheap', 1, 1)],
            'straitly': [_model('or/cheap', 9, 9)],
        }
        union, _keep = serve._merge_provider_catalogs(per)
        self.assertEqual(float(union[0]['pricing']['prompt']), 1)
        self.assertEqual(union[0]['providers'], ['openrouter', 'straitly'])

    def test_router_model_filtered_from_union(self):
        union, keep = serve._merge_provider_catalogs(
            {'straitly': [_model('jev-router', 1, 1)]})
        self.assertEqual((union, keep), ([], set()))

    def test_unparseable_pricing_excluded_from_keep(self):
        union, keep = serve._merge_provider_catalogs(
            {'straitly': [{'id': 'x/y', 'pricing': {}}]})
        self.assertEqual((union, keep), ([], set()))

    def test_provider_outside_the_registry_is_not_dropped(self):
        # A registry trim must never silently drop a provider's slice.
        union, keep = serve._merge_provider_catalogs(
            {'newvendor': [_model('nv/m', 1, 1)]})
        self.assertEqual(keep, {'nv/m'})
        self.assertEqual(union[0]['providers'], ['newvendor'])


class CanonicalSync(unittest.TestCase):
    """model_catalog: cheapest canonical price + providers column, purge
    scoped to models gone from EVERY provider."""

    def setUp(self):
        _clear()

    def test_providers_column_and_cheapest_canonical_price(self):
        union = [_model('shared/m', 1, 1, name='Shared M')]
        union[0]['providers'] = ['openrouter', 'straitly']
        purged = serve._sync_model_catalog(union, keep_ids={'shared/m'})
        self.assertEqual(purged, 0)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT prompt_price, price_per_m, providers FROM model_catalog '
                'WHERE model_id = ?', ('shared/m',)).fetchone()
        self.assertEqual(row[0], 1.0)
        self.assertEqual(row[1], 2_000_000.0)
        self.assertEqual(row[2], 'openrouter,straitly')

    def test_legacy_call_without_providers_keeps_row_provenance(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_catalog (model_id, name, prompt_price, completion_price, price_per_m, image_capable, providers, first_seen, last_seen) '
                "VALUES ('or/kept', 'Kept', 5, 5, 10, 0, 'openrouter', 1, 1)")
        # Single-catalog legacy shape: no providers metadata on the entry.
        serve._sync_model_catalog([_model('or/kept', 6, 6)])
        with serve._db() as conn:
            row = conn.execute(
                'SELECT prompt_price, providers FROM model_catalog '
                "WHERE model_id = 'or/kept'").fetchone()
        self.assertEqual(row, (6.0, 'openrouter'))

    def test_purge_only_when_gone_from_every_provider(self):
        stale = _stale()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_catalog (model_id, name, prompt_price, completion_price, price_per_m, image_capable, providers, first_seen, last_seen) '
                "VALUES ('or/only', 'OR Only', 1, 1, 2, 0, 'openrouter', ?, ?)",
                (stale, stale))
            conn.execute(
                'INSERT INTO model_benchmark_scores (model_id, benchmark, score, source_url, evaluator, checked_at) '
                "VALUES ('or/only', 'intelligence_index', 50.0, 'u', 'aa', ?)",
                (stale,))
        # Only straitly still answers: or/only is gone from every provider.
        union, keep = serve._merge_provider_catalogs(
            {'straitly': [_model('straitly/only', 1, 1)]})
        purged = serve._sync_model_catalog(union, keep_ids=keep)
        with serve._db() as conn:
            left = {r[0] for r in conn.execute('SELECT model_id FROM model_catalog')}
            scores = conn.execute(
                'SELECT COUNT(*) FROM model_benchmark_scores').fetchone()[0]
        self.assertEqual(purged, 1)
        self.assertEqual(left, {'straitly/only'})
        self.assertEqual(scores, 0)

    def test_straitly_only_survivor_keeps_scores_and_provenance(self):
        union, keep = serve._merge_provider_catalogs(
            {'straitly': [_model('st/only', 1, 1)]})
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_benchmark_scores (model_id, benchmark, score, source_url, evaluator, checked_at) '
                "VALUES ('st/only', 'intelligence_index', 61.3, 'u', 'aa', ?)",
                (_stale(),))
        serve._sync_model_catalog(union, keep_ids=keep)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT score FROM model_benchmark_scores WHERE model_id = ?',
                ('st/only',)).fetchone()
            cat = conn.execute(
                'SELECT providers FROM model_catalog WHERE model_id = ?',
                ('st/only',)).fetchone()
        self.assertEqual(row[0], 61.3)
        self.assertEqual(cat[0], 'straitly')


class StraitlyFetcher(unittest.TestCase):
    """_fetch_straitly_catalog_sync: no key -> empty (fail-open per
    provider), key -> Bearer GET of the OpenRouter-shaped catalog, transport
    errors propagate (refresh catches them per provider)."""

    def test_missing_key_is_empty_not_error(self):
        with unittest.mock.patch.object(serve, '_load_env', return_value={}):
            self.assertEqual(serve._fetch_straitly_catalog_sync(), [])

    def test_keyed_fetch_reads_the_catalog(self):
        payload = json.dumps({'data': [_model('claude-opus-5-5', 5, 25)]}).encode()

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['url'] = req.full_url
            captured['auth'] = req.headers.get('Authorization')
            return Resp()

        with unittest.mock.patch.object(
                serve, '_load_env',
                return_value={'STRAITLY_API_KEY': 'k'}), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        fake_urlopen):
            data = serve._fetch_straitly_catalog_sync()
        self.assertEqual([m['id'] for m in data], ['claude-opus-5-5'])
        self.assertEqual(captured['url'], 'https://api.straitly.ai/v1/models')
        self.assertEqual(captured['auth'], 'Bearer k')

    def test_fetch_failure_propagates_and_refresh_catches_it(self):
        with unittest.mock.patch.object(
                serve, '_load_env',
                return_value={'STRAITLY_API_KEY': 'k'}), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                serve._fetch_straitly_catalog_sync()


class RefreshIntegratesProviders(unittest.TestCase):
    """refresh_model_tiers: every registered provider is fetched, the union
    drives the canonical sync and the score backfills, the pools stay on the
    OpenRouter listing alone."""

    def setUp(self):
        _clear()

    def test_union_sync_backfill_scope_and_provider_slices(self):
        or_models = [_model('or/m', 2, 2), _model('shared/x', 2, 2)]
        st_models = [_model('shared/x', 1, 1), _model('st/vision', 1, 2, image=True)]
        catalogs = {'openrouter': '_fetch_openrouter_catalog_sync',
                    'straitly': '_fetch_straitly_catalog_sync'}
        backfilled = {}

        def fake_aa(models):
            backfilled['aa'] = sorted(m['id'] for m in models)
            return 0

        with unittest.mock.patch.object(serve, 'PROVIDER_CATALOGS', catalogs), \
             unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=or_models), \
             unittest.mock.patch.object(serve, '_fetch_straitly_catalog_sync',
                                        return_value=st_models), \
             unittest.mock.patch.object(serve, 'MODEL_BAND_PURPOSE', {}), \
             unittest.mock.patch.object(serve, '_ingest_catalog_benchmarks',
                                        return_value=0), \
             unittest.mock.patch.object(serve, '_ingest_aa_leaderboard', fake_aa), \
             unittest.mock.patch.object(serve, '_ingest_mmmu_leaderboard',
                                        return_value=0), \
             unittest.mock.patch.object(serve, '_ingest_hf_open_llm_fallback',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            asyncio.run(serve.refresh_model_tiers())
        # The UNION (not the OpenRouter list) is what the score backfills see.
        self.assertEqual(backfilled['aa'],
                         ['or/m', 'shared/x', 'st/vision'])
        with serve._db() as conn:
            canon = {r[0]: (r[1], r[2]) for r in conn.execute(
                'SELECT model_id, price_per_m, providers FROM model_catalog')}
            prov = {(r[0], r[1]) for r in conn.execute(
                'SELECT provider, model_id FROM model_provider_catalog')}
        self.assertEqual(set(canon), {'or/m', 'shared/x', 'st/vision'})
        # shared id: canonical price is the CHEAPEST listing's, provenance
        # names both providers; st/vision is straitly-only.
        self.assertEqual(canon['shared/x'], (2_000_000.0, 'openrouter,straitly'))
        self.assertEqual(canon['st/vision'], (3_000_000.0, 'straitly'))
        self.assertEqual(prov, {('openrouter', 'or/m'), ('openrouter', 'shared/x'),
                                ('straitly', 'shared/x'), ('straitly', 'st/vision')})


if __name__ == '__main__':
    unittest.main()
