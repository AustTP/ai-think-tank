"""Generic api_call: the registry-gated /api/api-call path for external
services (config, not code). Covers the registry loader and host/path lookup,
credential resolution and auth injection, the raw request helper (including
the redirect re-check), and the endpoint's gate order -- read-only for
unregistered hosts, path/method enforcement and credential injection for
registered hosts, Jev gating, and page/spend accrual.

Hermetic: the endpoint's only IO is log_action (DB) and the model gate, so
this suite patches those along with the network fetch. No real outbound call
or DB write happens in these tests.
"""
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-api-call-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        API_CALL_ENABLED=True,
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class _FakeResponse:
    def __init__(self, body, status=200, content_type='application/json', final_url=None):
        self._body = body
        self.status = status
        self.headers = {'content-type': content_type}
        self._final_url = final_url or 'https://example.com/x'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._final_url

    def read(self, n=-1):
        data = self._body if isinstance(self._body, bytes) else self._body.encode()
        return data[:n] if n != -1 else data
_VALID_SPEC = {
    'id': 'my_service',
    'name': 'My Service',
    'base_url': 'https://api.myservice.example.com',
    'credential': {'type': 'env', 'key': 'MY_SERVICE_TOKEN'},
    'auth': {'type': 'header_bearer'},
    'methods': ['GET', 'POST'],
    'path_rules': [{'prefix': '/', 'methods': ['GET', 'POST']}],
    'spend': {'kind': 'none'},
}



class RegistryLoader(unittest.TestCase):
    """Loader + lookups against the real world/api_services.json."""

    def setUp(self):
        # Each test starts from the real registry; the bad-file test leaves
        # the module registry empty, so reload before every lookup.
        serve._load_api_services()

    def test_loads_without_error(self):
        serve._load_api_services()
        self.assertIsNone(serve._API_SERVICES_LOAD_ERROR)
        self.assertIn('github', serve._API_SERVICES)
        self.assertIn('apify', serve._API_SERVICES)
        self.assertIn('tavily', serve._API_SERVICES)
        self.assertIn('pixellab', serve._API_SERVICES)
        self.assertIn('higgsfield', serve._API_SERVICES)
        self.assertIn('open-meteo', serve._API_SERVICES)
        self.assertIn('open-meteo-geocoding', serve._API_SERVICES)
        self.assertIn('opensky', serve._API_SERVICES)
        self.assertIn('adsb-lol', serve._API_SERVICES)
        self.assertIn('celestrak', serve._API_SERVICES)

    def test_host_lookup_exact_and_subdomain(self):
        self.assertIsNotNone(serve._api_service_for_host('api.github.com'))
        self.assertIsNotNone(serve._api_service_for_host('api.apify.com'))
        self.assertEqual(serve._api_service_for_host('api.github.com').get('name'), 'GitHub')

    def test_new_services_lookup_and_auth(self):
        opensky = serve._api_service_for_host('opensky-network.org')
        self.assertIsNotNone(opensky)
        self.assertEqual(opensky.get('auth', {}).get('type'), 'oauth2_client_credentials')
        self.assertTrue(opensky.get('auth', {}).get('token_url').startswith('https://auth.opensky-network.org'))
        self.assertTrue(serve._api_service_path_allowed(opensky, 'GET', '/api/states/all'))
        self.assertFalse(serve._api_service_path_allowed(opensky, 'POST', '/api/states/all'))
        adsb = serve._api_service_for_host('api.adsb.lol')
        self.assertIsNotNone(adsb)
        self.assertEqual(adsb.get('credential', {}).get('type'), 'none')
        self.assertEqual(adsb.get('auth', {}).get('type'), 'none')
        self.assertTrue(serve._api_service_path_allowed(adsb, 'GET', '/v2/callsign/UAL101'))
        celestrak = serve._api_service_for_host('celestrak.org')
        self.assertIsNotNone(celestrak)
        self.assertTrue(serve._api_service_path_allowed(celestrak, 'GET', '/NORAD/elements/gp.php'))

    def test_unknown_host_is_none(self):
        self.assertIsNone(serve._api_service_for_host('api.not-registered.com'))

    def test_open_meteo_keyless_lookup_and_paths(self):
        om = serve._api_service_for_host('api.open-meteo.com')
        self.assertIsNotNone(om)
        self.assertEqual(om.get('credential', {}).get('type'), 'none')
        self.assertEqual(om.get('auth', {}).get('type'), 'none')
        self.assertTrue(serve._api_service_path_allowed(om, 'GET', '/v1/forecast'))
        self.assertFalse(serve._api_service_path_allowed(om, 'POST', '/v1/forecast'))
        geo = serve._api_service_for_host('geocoding-api.open-meteo.com')
        self.assertIsNotNone(geo)
        self.assertTrue(serve._api_service_path_allowed(geo, 'GET', '/v1/search'))

    def test_path_rules_enforced(self):
        apify = serve._api_service_for_host('api.apify.com')
        self.assertTrue(serve._api_service_path_allowed(apify, 'POST', '/actors/apify/website-content-crawler/runs'))
        self.assertTrue(serve._api_service_path_allowed(apify, 'GET', '/users/me'))
        self.assertFalse(serve._api_service_path_allowed(apify, 'DELETE', '/actors/foo/runs'))
        self.assertFalse(serve._api_service_path_allowed(apify, 'GET', '/actors/foo'))
        tavily = serve._api_service_for_host('api.tavily.com')
        self.assertTrue(serve._api_service_path_allowed(tavily, 'POST', '/search'))
        self.assertFalse(serve._api_service_path_allowed(tavily, 'GET', '/search'))

    def test_fail_closed_when_db_unavailable(self):
        # DB-backed loader: if the DB read fails, fall back to the JSON seed
        # so the runtime still has a working (read-only) registry.
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('no db')):
            serve._load_api_services()
        self.assertIn('github', serve._API_SERVICES)
        self.assertIn('apify', serve._API_SERVICES)


class RegistryFreshness(unittest.TestCase):
    """Config-only service registration: an operator's row edit goes live
    without a restart. The freshness check reloads _API_SERVICES when the
    active-row fingerprint (count + max updated_at) changes."""

    def setUp(self):
        serve._API_SERVICES_FINGERPRINT = None
        serve._API_SERVICES_FRESH_CHECK = 0.0
        serve._load_api_services()

    def tearDown(self):
        # Remove only the rows this class inserted; init_db's seed of the
        # built-in services must survive so later tests load the full registry.
        with serve._db() as conn:
            conn.execute("DELETE FROM api_services WHERE id='brand-new'")
        serve._API_SERVICES_FINGERPRINT = None
        serve._API_SERVICES_FRESH_CHECK = 0.0
        serve._load_api_services()

    def _insert_service(self, sid='brand-new', status='active'):
        spec = {'id': sid, 'name': sid, 'base_url': 'https://api.example.com',
                'credential': {'type': 'none'}, 'auth': {'type': 'none'},
                'methods': ['GET'], 'path_rules': [{'prefix': '/', 'methods': ['GET']}],
                'spend': {'kind': 'none'}, 'workflow': None}
        row = serve._api_service_spec_to_row(sid, spec, status=status, proposed_by='ops')
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO api_services (' + ','.join(serve._API_SERVICE_COLUMNS) + ') '
                'VALUES (' + ','.join('?' * len(serve._API_SERVICE_COLUMNS)) + ')',
                [row.get(c) for c in serve._API_SERVICE_COLUMNS])

    def test_new_db_row_is_picked_up_without_restart(self):
        self._insert_service('brand-new')
        self.assertNotIn('brand-new', serve._API_SERVICES)
        serve._ensure_api_services_fresh()
        self.assertIn('brand-new', serve._API_SERVICES)

    def test_revoked_row_is_dropped_without_restart(self):
        self._insert_service('brand-new')
        serve._ensure_api_services_fresh()
        self.assertIn('brand-new', serve._API_SERVICES)
        with serve._db() as conn:
            conn.execute("UPDATE api_services SET status='denied', updated_at=? WHERE id='brand-new'",
                         (time.time(),))
        # The TTL gate only skips re-checks inside the window; force a re-check.
        serve._API_SERVICES_FRESH_CHECK = 0.0
        serve._ensure_api_services_fresh()
        self.assertNotIn('brand-new', serve._API_SERVICES)

    def test_unchanged_table_does_not_reload(self):
        self._insert_service('brand-new')
        serve._ensure_api_services_fresh()
        with unittest.mock.patch.object(serve, '_load_api_services') as loader:
            serve._ensure_api_services_fresh()  # inside the TTL window -> no reload
        self.assertFalse(loader.called)


