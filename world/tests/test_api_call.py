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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


class CredentialResolution(unittest.TestCase):
    def test_env_credential(self):
        spec = {'credential': {'type': 'env', 'key': 'GITHUB_TOKEN'}}
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', 'tok'):
            token, token2, err = serve._api_credential_value(spec)
        self.assertEqual(token, 'tok')
        self.assertIsNone(token2)
        self.assertIsNone(err)

    def test_env_missing(self):
        spec = {'credential': {'type': 'env', 'key': 'GITHUB_TOKEN'}}
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', None):
            token, token2, err = serve._api_credential_value(spec)
        self.assertIsNone(token)
        self.assertIn('GITHUB_TOKEN', err)

    def test_two_part_env_credential(self):
        spec = {'credential': {'type': 'env', 'key': 'HIGGSFIELD_API_KEY_ID', 'key2': 'HIGGSFIELD_API_KEY_SECRET'}}
        with unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', 'id'), \
             unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec'):
            token, token2, err = serve._api_credential_value(spec)
        self.assertEqual((token, token2, err), ('id', 'sec', None))

    def test_vault_credential(self):
        spec = {'credential': {'type': 'vault', 'name': 'pixellab'}}
        with unittest.mock.patch.object(serve, '_credential_token', return_value='enc'), \
             unittest.mock.patch.object(serve, '_open_secret', return_value='raw-tok'):
            token, token2, err = serve._api_credential_value(spec)
        self.assertEqual(token, 'raw-tok')
        self.assertIsNone(err)

    def test_no_credential(self):
        spec = {'credential': {}}
        token, token2, err = serve._api_credential_value(spec)
        self.assertIsNone(token)
        self.assertIn('no credential', err)

    def test_none_credential_is_keyless(self):
        # A keyless/free public API (Open-Meteo): no token, no error, so the
        # caller never blocks on a missing credential and never injects auth.
        spec = {'credential': {'type': 'none'}}
        token, token2, err = serve._api_credential_value(spec)
        self.assertIsNone(token)
        self.assertIsNone(token2)
        self.assertIsNone(err)


class AuthInjection(unittest.TestCase):
    def test_header_bearer(self):
        url, headers, body = serve._api_apply_auth({'auth': {'type': 'header_bearer'}}, 'TOK', None, 'GET', 'u', {}, None)
        self.assertEqual(headers['Authorization'], 'Bearer TOK')

    def test_header_key_two_part(self):
        spec = {'auth': {'type': 'header_key', 'value_format': 'Key {key}:{key2}'}}
        url, headers, body = serve._api_apply_auth(spec, 'ID', 'SEC', 'POST', 'u', {}, None)
        self.assertEqual(headers['Authorization'], 'Key ID:SEC')

    def test_body_field(self):
        spec = {'auth': {'type': 'body_field', 'field': 'api_key'}}
        url, headers, body = serve._api_apply_auth(spec, 'K', None, 'POST', 'u', {}, {'query': 'x'})
        self.assertEqual(body, {'query': 'x', 'api_key': 'K'})

    def test_none_auth_is_a_noop(self):
        url, headers, body = serve._api_apply_auth({'auth': {'type': 'none'}}, None, None, 'GET', 'u', {}, None)
        self.assertEqual((url, headers, body), ('u', {}, None))

    def test_oauth2_client_credentials_mints_bearer(self):
        spec = {'auth': {'type': 'oauth2_client_credentials', 'token_url': 'https://auth.example.com/token'}}
        with unittest.mock.patch.object(serve, '_oauth2_access_token', return_value='MINTED'):
            url, headers, body = serve._api_apply_auth(spec, 'CLIENT_ID', 'CLIENT_SECRET', 'GET', 'u', {}, None)
        self.assertEqual(headers['Authorization'], 'Bearer MINTED')
        self.assertEqual((url, body), ('u', None))

    def test_oauth2_client_credentials_missing_token_url_raises(self):
        with self.assertRaises(ValueError):
            serve._api_apply_auth({'auth': {'type': 'oauth2_client_credentials'}}, 'a', 'b', 'GET', 'u', {}, None)


class OAuth2Token(unittest.TestCase):
    """The client-credentials mint + in-process cache that backs OpenSky auth."""

    def setUp(self):
        serve._OAUTH2_TOKEN_CACHE.clear()
        p = unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(serve._OAUTH2_TOKEN_CACHE.clear)

    def _token_response(self, body, **kw):
        kw.setdefault('final_url', 'https://auth.example.com/token')
        return unittest.mock.patch.object(serve, '_safe_urlopen',
                                          return_value=_FakeResponse(body, **kw))

    def test_mint_parses_and_caches(self):
        with self._token_response('{"access_token": "abc123", "expires_in": 1800}'):
            tok = serve._oauth2_access_token('https://auth.example.com/token', 'cid', 'csec')
        self.assertEqual(tok, 'abc123')
        key = ('https://auth.example.com/token', 'cid')
        self.assertIn(key, serve._OAUTH2_TOKEN_CACHE)
        self.assertEqual(serve._OAUTH2_TOKEN_CACHE[key]['token'], 'abc123')

    def test_cache_hit_skips_mint(self):
        serve._OAUTH2_TOKEN_CACHE[('https://auth.example.com/token', 'cid')] = {
            'token': 'cached', 'expires_at': time.time() + 1000}
        with unittest.mock.patch.object(serve, '_oauth2_token_request_sync') as req:
            tok = serve._oauth2_access_token('https://auth.example.com/token', 'cid', 'csec')
        self.assertEqual(tok, 'cached')
        req.assert_not_called()

    def test_refreshes_near_expiry(self):
        serve._OAUTH2_TOKEN_CACHE[('https://auth.example.com/token', 'cid')] = {
            'token': 'stale', 'expires_at': time.time() + 10}
        with unittest.mock.patch.object(serve, '_oauth2_token_request_sync',
                                        return_value='{"access_token": "fresh", "expires_in": 1800}'):
            tok = serve._oauth2_access_token('https://auth.example.com/token', 'cid', 'csec')
        self.assertEqual(tok, 'fresh')

    def test_rejects_non_https_token_url(self):
        with self.assertRaises(ValueError):
            serve._oauth2_access_token('http://auth.example.com/token', 'cid', 'csec')

    def test_missing_credentials_raises(self):
        with self.assertRaises(ValueError):
            serve._oauth2_access_token('https://auth.example.com/token', '', '')

    def test_non_json_response_raises(self):
        with unittest.mock.patch.object(serve, '_oauth2_token_request_sync', return_value='not-json'):
            with self.assertRaises(ValueError):
                serve._oauth2_access_token('https://auth.example.com/token', 'cid', 'csec')

    def test_missing_access_token_raises(self):
        with unittest.mock.patch.object(serve, '_oauth2_token_request_sync', return_value='{"expires_in": 60}'):
            with self.assertRaises(ValueError):
                serve._oauth2_access_token('https://auth.example.com/token', 'cid', 'csec')

    def test_request_sync_rejects_non_2xx(self):
        with self._token_response('denied', status=401):
            with self.assertRaises(ValueError):
                serve._oauth2_token_request_sync('https://auth.example.com/token', 'cid', 'csec')

    def test_request_sync_rejects_internal_redirect(self):
        with unittest.mock.patch.object(serve, '_is_safe_public_host',
                                        side_effect=lambda h: h != '127.0.0.1'), \
             self._token_response('{}', final_url='http://127.0.0.1/token'):
            with self.assertRaises(ValueError):
                serve._oauth2_token_request_sync('https://auth.example.com/token', 'cid', 'csec')


class DailyCallBudget(unittest.TestCase):
    """The per-service daily call cap (spend.kind = 'daily_calls') that guards
    daily-quota APIs like OpenSky, keyed by UTC date."""

    def setUp(self):
        serve._api_daily_calls_ledger_write({})  # creates the kv_apicalls table + clears it

    def tearDown(self):
        serve._api_daily_calls_ledger_write({})

    def _spec(self, **over):
        spend = {'kind': 'daily_calls', 'cap': 5, 'note': 'n'}
        spend.update(over)
        return {'id': 'opensky', 'spend': spend}

    def test_day_format_is_utc_date(self):
        self.assertRegex(serve._api_daily_calls_day(0), r'^\d{4}-\d{2}-\d{2}$')

    def test_used_starts_at_zero(self):
        self.assertEqual(serve._api_daily_calls_used({'id': 'opensky'}), 0)

    def test_accrue_then_exceeded(self):
        self.assertFalse(serve._api_daily_calls_exceeded(self._spec()))
        for _ in range(5):
            serve._api_accrue_daily_call({'id': 'opensky'})
        self.assertEqual(serve._api_daily_calls_used({'id': 'opensky'}), 5)
        self.assertTrue(serve._api_daily_calls_exceeded(self._spec()))

    def test_exceeded_false_for_non_daily_kind(self):
        self.assertFalse(serve._api_daily_calls_exceeded({'id': 'x', 'spend': {'kind': 'none'}}))

    def test_exceeded_false_when_cap_missing(self):
        self.assertFalse(serve._api_daily_calls_exceeded({'id': 'x', 'spend': {'kind': 'daily_calls'}}))

    def test_accrue_with_no_sid_is_noop(self):
        serve._api_accrue_daily_call({'spend': {'kind': 'daily_calls'}})
        self.assertEqual(serve._api_daily_calls_used({'id': 'opensky'}), 0)

    def test_ledger_read_isolated_from_db_failure(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('no db')):
            self.assertEqual(serve._api_daily_calls_ledger_read(), {})
            serve._api_accrue_daily_call({'id': 'opensky'})  # must not raise


class DailyCallBudgetLedger(unittest.TestCase):
    def test_ledger_write_read_round_trip(self):
        serve._api_daily_calls_ledger_write({'2026-10-08': {'opensky': 3}})
        self.assertEqual(serve._api_daily_calls_ledger_read(), {'2026-10-08': {'opensky': 3}})


class ApiRequestSync(unittest.TestCase):
    def test_parses_response(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=_FakeResponse(
                '{"a": 1}', content_type='application/json')):
            result = serve._api_request_sync('GET', 'https://api.github.com/x', {}, None)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['body'], '{"a": 1}')
        self.assertFalse(result['truncated'])
        self.assertIn('application/json', result['contentType'])

    def test_truncates_over_cap(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=_FakeResponse('x' * (serve.API_CALL_MAX_BODY_BYTES + 10))):
            result = serve._api_request_sync('GET', 'https://example.com/', {}, None)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['body']), serve.API_CALL_MAX_BODY_BYTES)

    def test_redirect_to_internal_host_rejected(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=_FakeResponse(
                '{}', final_url='http://127.0.0.1/internal')):
            with self.assertRaises(ValueError):
                serve._api_request_sync('GET', 'https://example.com/', {}, None)


class ApiCallEndpoint(unittest.TestCase):
    """Gate order for /api/api-call through the real app, with the network,
    Jev, and agent-key checks mocked."""

    def _client(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _gates(self, jev_allow=True):
        async def _allow(instructions, criteria):
            return ('allow' if jev_allow else 'block', 0.99 if jev_allow else 0.1, 0.0, 'trace-1')
        return [
            unittest.mock.patch.object(serve, 'API_CALL_ENABLED', True),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, '_jev_quorum_decision', _allow),
            unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=jev_allow),
            unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False),
            unittest.mock.patch.object(serve, 'log_action'),
        ]

    def _start(self, patchers):
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_write_to_unregistered_host_blocked(self):
        self._start(self._gates())
        r = self._client().post('/api/api-call', json={
            'agentId': 'ben', 'url': 'https://api.not-registered.com/v1/x', 'method': 'POST', 'purpose': 'p'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertIn('registered-services list', r.json()['reason'])

    def test_registered_path_outside_rules_blocked(self):
        self._start(self._gates())
        r = self._client().post('/api/api-call', json={
            'agentId': 'ben', 'url': 'https://api.tavily.com/delete-everything', 'method': 'POST', 'purpose': 'p'})
        self.assertFalse(r.json()['allowed'])
        self.assertIn('not in the registered service', r.json()['reason'])

    def test_registered_credential_missing_blocked(self):
        self._start(self._gates())
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', None):
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://api.github.com/repos/o/r', 'method': 'GET', 'purpose': 'p'})
        self.assertFalse(r.json()['allowed'])
        self.assertIn('credential unavailable', r.json()['reason'])

    def test_jev_denied_blocked(self):
        # Unregistered host => Jev runs; a block is honored.
        self._start(self._gates(jev_allow=False))
        r = self._client().post('/api/api-call', json={
            'agentId': 'ben', 'url': 'https://example.com/data', 'method': 'GET', 'purpose': 'p'})
        self.assertFalse(r.json()['allowed'])

    def test_registered_get_injects_credential_and_wraps(self):
        self._start(self._gates())
        fake_result = {'status': 200, 'finalUrl': 'https://api.github.com/repos/o/r',
                       'contentType': 'application/json', 'body': '{"full_name": "o/r"}', 'truncated': False}
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result) as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://api.github.com/repos/o/r', 'method': 'GET', 'purpose': 'read repo'})
        out = r.json()
        self.assertTrue(out['allowed'])
        self.assertIn('<<<EXTERNAL_DATA', out['textForModel'])
        self.assertIn('o/r', out['textForModel'])
        self.assertIn('GitHub', out['modelInstruction'])
        sent_url, sent_headers = fetch.call_args.args[1], fetch.call_args.args[2]
        self.assertEqual(sent_headers.get('Authorization'), 'Bearer tok')
        self.assertEqual(sent_url, 'https://api.github.com/repos/o/r')

    def test_unregistered_get_is_read_only_with_no_auth_headers(self):
        self._start(self._gates())
        fake_result = {'status': 200, 'finalUrl': 'https://example.com/',
                       'contentType': 'text/plain', 'body': 'ok', 'truncated': False}
        with unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result) as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://example.com/data', 'method': 'GET', 'purpose': 'check status',
                'headers': {'Authorization': 'Bearer steal', 'Accept': 'application/json'}})
        self.assertTrue(r.json()['allowed'])
        sent_headers = fetch.call_args.args[2]
        self.assertNotIn('Authorization', sent_headers)
        self.assertEqual(sent_headers.get('Accept'), 'application/json')

    def test_apify_spend_accrued_from_run_object(self):
        self._start(self._gates())
        run_body = '{"defaultDatasetId": "d1", "usageTotalUsd": 0.42}'
        fake_result = {'status': 201, 'finalUrl': 'https://api.apify.com/v2/actors/a/runs',
                       'contentType': 'application/json', 'body': run_body, 'truncated': False}
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'tok'), \
             unittest.mock.patch.object(serve, '_apify_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result), \
             unittest.mock.patch.object(serve, '_accrue_apify_spend') as accrue:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://api.apify.com/v2/actors/apify/x/runs', 'method': 'POST',
                'purpose': 'run scraper', 'body': {'startUrls': []}})
        self.assertTrue(r.json()['allowed'])
        accrue.assert_called_once_with(0.42)

    def test_apify_budget_exceeded_blocks_before_call(self):
        self._start(self._gates())
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'tok'), \
             unittest.mock.patch.object(serve, '_apify_budget_exceeded', return_value=True), \
             unittest.mock.patch.object(serve, '_api_request_sync') as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://api.apify.com/v2/actors/apify/x/runs', 'method': 'POST',
                'purpose': 'run scraper', 'body': {}})
        self.assertFalse(r.json()['allowed'])
        self.assertIn('budget', r.json()['reason'])
        fetch.assert_not_called()

    def test_tavily_accrues_page_request(self):
        self._start(self._gates())
        fake_result = {'status': 200, 'finalUrl': 'https://api.tavily.com/search',
                       'contentType': 'application/json', 'body': '{"answer": "hi", "results": []}', 'truncated': False}
        with unittest.mock.patch.object(serve, 'TAVILY_API_KEY', 'tok'), \
             unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result), \
             unittest.mock.patch.object(serve, '_accrue_page_request') as accrue:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://api.tavily.com/search', 'method': 'POST',
                'purpose': 'search the web', 'body': {'query': 'x'}})
        self.assertTrue(r.json()['allowed'])
        accrue.assert_called_once()

    def test_opensky_mints_and_injects_bearer(self):
        # Registered OAuth2 service: the registry's client id/secret pair is
        # minted into a short-lived access token server-side and injected as a
        # Bearer header -- the agent never supplies a credential.
        self._start(self._gates())
        fake_result = {'status': 200, 'finalUrl': 'https://opensky-network.org/api/states/all',
                       'contentType': 'application/json', 'body': '{"time": 1, "states": []}', 'truncated': False}
        with unittest.mock.patch.object(serve, 'OPEN_SKY_CLIENT_ID', 'cid'), \
             unittest.mock.patch.object(serve, 'OPEN_SKY_CLIENT_SECRET', 'csec'), \
             unittest.mock.patch.object(serve, '_oauth2_access_token', return_value='MINTED'), \
             unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result) as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://opensky-network.org/api/states/all', 'method': 'GET',
                'purpose': 'check flights overhead'})
        self.assertTrue(r.json()['allowed'])
        sent_headers = fetch.call_args.args[2]
        self.assertEqual(sent_headers.get('Authorization'), 'Bearer MINTED')

    def test_daily_calls_budget_exceeded_blocks(self):
        # A registered daily-quota service (OpenSky) must refuse once its daily
        # call cap is consumed, before any network or auth work.
        self._start(self._gates())
        with unittest.mock.patch.object(serve, 'OPEN_SKY_CLIENT_ID', 'cid'), \
             unittest.mock.patch.object(serve, 'OPEN_SKY_CLIENT_SECRET', 'csec'), \
             unittest.mock.patch.object(serve, '_api_daily_calls_used', return_value=200), \
             unittest.mock.patch.object(serve, '_api_request_sync') as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://opensky-network.org/api/states/all', 'method': 'GET',
                'purpose': 'check flights overhead'})
        self.assertFalse(r.json()['allowed'])
        self.assertIn('daily call budget', r.json()['reason'])
        fetch.assert_not_called()

    def test_oauth2_mint_failure_fails_clean(self):
        # If the OAuth2 token mint refuses, the request must NOT go out
        # unauthenticated -- it fails cleanly as an allowed-but-failed call.
        self._start(self._gates())
        with unittest.mock.patch.object(serve, 'OPEN_SKY_CLIENT_ID', 'cid'), \
             unittest.mock.patch.object(serve, 'OPEN_SKY_CLIENT_SECRET', 'csec'), \
             unittest.mock.patch.object(serve, '_oauth2_access_token',
                                        side_effect=ValueError('token endpoint 401')), \
             unittest.mock.patch.object(serve, '_api_request_sync') as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://opensky-network.org/api/states/all', 'method': 'GET',
                'purpose': 'check flights overhead'})
        out = r.json()
        self.assertTrue(out['allowed'])
        self.assertIn('auth could not be applied', out['error'])
        fetch.assert_not_called()

    def test_daily_calls_accrued_on_success(self):
        self._start(self._gates())
        fake_result = {'status': 200, 'finalUrl': 'https://opensky-network.org/api/states/all',
                       'contentType': 'application/json', 'body': '{"time": 1, "states": []}', 'truncated': False}
        with unittest.mock.patch.object(serve, 'OPEN_SKY_CLIENT_ID', 'cid'), \
             unittest.mock.patch.object(serve, 'OPEN_SKY_CLIENT_SECRET', 'csec'), \
             unittest.mock.patch.object(serve, '_oauth2_access_token', return_value='MINTED'), \
             unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result), \
             unittest.mock.patch.object(serve, '_api_accrue_daily_call') as accrue:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://opensky-network.org/api/states/all', 'method': 'GET',
                'purpose': 'check flights overhead'})
        self.assertTrue(r.json()['allowed'])
        accrue.assert_called_once()
        self.assertEqual(accrue.call_args.args[0]['id'], 'opensky')


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


class ApiServiceRegistry(unittest.TestCase):
    """DB-backed registry: validation, upsert/revoke round-trip, and the
    proposal/approval flow (the floor-1.0 escalation whose approval writes an
    active row)."""

    def setUp(self):
        # _is_safe_public_host does a real DNS check; the example domains
        # don't resolve, so treat any public-looking host as safe.
        p = unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _proposal_spec(self, **over):
        spec = json.loads(json.dumps(_VALID_SPEC))
        spec.update(over)
        return spec

    def test_validation_accepts_a_wellformed_spec(self):
        sid, norm, error = serve._validate_api_service_spec(self._proposal_spec())
        self.assertIsNone(error)
        self.assertEqual(sid, 'my_service')
        self.assertEqual(norm['base_url'], 'https://api.myservice.example.com')

    def test_validation_rejects_bad_base_url(self):
        for bad in ('http://api.myservice.example.com', 'file:///etc/passwd', '', 'not-a-url'):
            _sid, _norm, error = serve._validate_api_service_spec(self._proposal_spec(base_url=bad))
            self.assertIsNotNone(error, bad)

    def test_validation_rejects_bad_auth_and_methods(self):
        _sid, _norm, error = serve._validate_api_service_spec(
            self._proposal_spec(auth={'type': 'magic_header'}))
        self.assertIn('auth.type', error)
        _sid, _norm, error = serve._validate_api_service_spec(self._proposal_spec(methods=[]))
        self.assertIn('methods', error)

    def test_validation_accepts_keyless_spec(self):
        # A free public API needs no credential and no auth -- the config-not-
        # code path that lets agents reach keyless services (Open-Meteo) with
        # zero setup and no vault/env requirement.
        sid, norm, error = serve._validate_api_service_spec(self._proposal_spec(
            credential={'type': 'none'}, auth={'type': 'none'}))
        self.assertIsNone(error)
        self.assertEqual(sid, 'my_service')
        self.assertEqual(norm['credential']['type'], 'none')

    def _oauth2_spec(self, **over):
        spec = self._proposal_spec()
        spec['credential'] = {'type': 'env', 'key': 'OPEN_SKY_CLIENT_ID', 'key2': 'OPEN_SKY_CLIENT_SECRET'}
        spec['auth'] = {'type': 'oauth2_client_credentials', 'token_url': 'https://auth.example.com/token'}
        spec.update(over)
        return spec

    def test_validation_accepts_oauth2_spec(self):
        sid, norm, error = serve._validate_api_service_spec(self._oauth2_spec())
        self.assertIsNone(error)
        self.assertEqual(norm['auth']['type'], 'oauth2_client_credentials')
        self.assertEqual(norm['auth']['token_url'], 'https://auth.example.com/token')

    def test_validation_rejects_oauth2_without_token_url(self):
        _sid, _norm, error = serve._validate_api_service_spec(
            self._oauth2_spec(auth={'type': 'oauth2_client_credentials'}))
        self.assertIn('token_url', error)

    def test_validation_rejects_oauth2_bad_token_url(self):
        for bad in ('http://auth.example.com/token', 'not-a-url', 'ftp://x'):
            _sid, _norm, error = serve._validate_api_service_spec(
                self._oauth2_spec(auth={'type': 'oauth2_client_credentials', 'token_url': bad}))
            self.assertIsNotNone(error, bad)

    def test_validation_rejects_oauth2_without_two_part_credential(self):
        _sid, _norm, error = serve._validate_api_service_spec(self._oauth2_spec(
            credential={'type': 'none'}))
        self.assertIn('two-part env', error)

    def test_validation_rejects_daily_calls_without_cap(self):
        _sid, _norm, error = serve._validate_api_service_spec(self._proposal_spec(
            spend={'kind': 'daily_calls'}))
        self.assertIn('cap', error)

    def test_validation_accepts_daily_calls_with_cap(self):
        sid, norm, error = serve._validate_api_service_spec(self._proposal_spec(
            spend={'kind': 'daily_calls', 'cap': 200}))
        self.assertIsNone(error)
        self.assertEqual(norm['spend']['cap'], 200)

    def test_upsert_persists_and_loads_as_active(self):
        sid, norm, error = serve._validate_api_service_spec(self._proposal_spec())
        serve._api_service_upsert(norm, status='active', proposed_by='player')
        self.addCleanup(lambda: serve._api_service_upsert(
            {'id': sid, 'base_url': 'https://api.myservice.example.com'}, status='denied'))
        spec = serve._api_service_for_host('api.myservice.example.com')
        self.assertIsNotNone(spec)
        self.assertEqual(spec.get('name'), 'My Service')
        self.assertEqual(spec.get('auth', {}).get('type'), 'header_bearer')

    def test_revoke_removes_from_in_memory_registry(self):
        sid, norm, error = serve._validate_api_service_spec(self._proposal_spec())
        serve._api_service_upsert(norm, status='active')
        self.assertIsNotNone(serve._api_service_for_host('api.myservice.example.com'))
        serve._api_service_upsert({'id': sid, 'base_url': 'https://api.myservice.example.com'},
                                  status='denied')
        self.assertIsNone(serve._api_service_for_host('api.myservice.example.com'))

    def test_upsert_roundtrips_oauth2_and_daily_cap(self):
        # The two new registry fields must survive a DB round trip: the OAuth2
        # token endpoint and the daily-calls spend cap. Losing either would
        # silently disable the minted-bearer auth or the quota guard.
        spec = self._proposal_spec(
            credential={'type': 'env', 'key': 'OPEN_SKY_CLIENT_ID', 'key2': 'OPEN_SKY_CLIENT_SECRET'},
            auth={'type': 'oauth2_client_credentials', 'token_url': 'https://auth.example.com/token'},
            spend={'kind': 'daily_calls', 'cap': 200})
        sid, norm, error = serve._validate_api_service_spec(spec)
        self.assertIsNone(error)
        serve._api_service_upsert(norm, status='active')
        self.addCleanup(lambda: serve._api_service_upsert(
            {'id': sid, 'base_url': 'https://api.myservice.example.com'}, status='denied'))
        loaded = serve._api_service_for_host('api.myservice.example.com')
        self.assertEqual(loaded['auth']['type'], 'oauth2_client_credentials')
        self.assertEqual(loaded['auth']['token_url'], 'https://auth.example.com/token')
        self.assertEqual(loaded['spend']['kind'], 'daily_calls')
        self.assertEqual(loaded['spend']['cap'], 200)

    def test_request_endpoint_files_an_escalation(self):
        _start = unittest.mock.patch.multiple(
            serve, API_CALL_ENABLED=True, check_rate_limit=lambda a: True,
            verify_agent_key=lambda a, k: True, log_action=lambda *a, **k: None)
        _start.start()
        self.addCleanup(_start.stop)
        captured = {}
        with unittest.mock.patch.object(serve, 'create_escalation',
                                        side_effect=lambda kind, question, on_approve_note='': (
                                            captured.update(kind=kind, note=on_approve_note) or 'esc-1')):
            from starlette.testclient import TestClient
            c = TestClient(serve.app)
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
            r = c.post('/api/api-service/request', json={
                'agentId': 'ben', 'service': self._proposal_spec(), 'purpose': 'need it'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['requested'])
        self.assertEqual(r.json()['serviceId'], 'my_service')
        self.assertEqual(captured['kind'], 'api service proposal')
        self.assertEqual(json.loads(captured['note'])['id'], 'my_service')

    def test_request_endpoint_rejects_invalid_spec(self):
        _start = unittest.mock.patch.multiple(
            serve, API_CALL_ENABLED=True, check_rate_limit=lambda a: True,
            verify_agent_key=lambda a, k: True)
        _start.start()
        self.addCleanup(_start.stop)
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        r = c.post('/api/api-service/request', json={
            'agentId': 'ben', 'service': self._proposal_spec(base_url='http://x.com')})
        self.assertEqual(r.status_code, 400)
        self.assertIn('https', r.json()['error'])

    def test_approval_branch_upserts_active(self):
        # The escalation-resolve approve side-effect: note carries the spec,
        # and on approve it is validated again and written as active.
        sid, norm, error = serve._validate_api_service_spec(self._proposal_spec())
        with unittest.mock.patch.object(serve, '_api_service_upsert') as upsert:
            esc = {'kind': 'api service proposal', 'question': 'q',
                   'note': json.dumps(norm, sort_keys=True), 'status': 'pending', 'resolvedBy': 'player'}
            ok, msg = serve._approve_api_service_proposal(esc)
        self.assertTrue(ok)
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.args[0]['id'], sid)
        self.assertEqual(upsert.call_args.kwargs['status'], 'active')

    def test_approval_rejects_a_tampered_spec(self):
        # A proposal whose note was edited to a hostile value must not register.
        with unittest.mock.patch.object(serve, '_api_service_upsert') as upsert:
            ok, msg = serve._approve_api_service_proposal(
                {'note': json.dumps(self._proposal_spec(base_url='http://evil.example.com'))})
        self.assertFalse(ok)
        upsert.assert_not_called()


class Workflow(unittest.TestCase):
    """The config-driven submit/poll/finalize engine, with spend accrued only
    on a successful terminal."""

    def _hf_spec(self):
        return {
            'id': 'higgsfield', 'name': 'Higgsfield', 'base_url': 'https://api.higgsfield.ai',
            'workflow': {
                'submit': {'path_template': '/{endpoint}', 'method': 'POST', 'id_field': 'request_id'},
                'estimate': {'path_template': '/estimate/{endpoint}', 'method': 'POST', 'usd_field': 'usd'},
                'poll': {'path_template': '/requests/{request_id}/status', 'method': 'GET',
                         'interval_s': 0.01, 'timeout_s': 5,
                         'terminal': ['completed', 'failed'], 'success': ['completed']},
                'spend': {'kind': 'estimate_first', 'service': 'higgsfield'},
            },
            'spend': {'kind': 'none'},
        }

    def _run(self, poll_status, usd=0.094, with_usd=True):
        responses = [
            {'body': json.dumps({'usd': usd}), 'contentType': 'application/json', 'status': 200},
            {'body': json.dumps({'request_id': 'r1'}), 'contentType': 'application/json', 'status': 200},
            {'body': json.dumps({'status': poll_status, 'request_id': 'r1',
                                 'images': [{'url': 'https://cdn/u.jpg'}]}),
             'contentType': 'application/json', 'status': 200},
        ]
        calls = []
        def fake_req(method, url, headers, body):
            calls.append(url)
            return responses[len(calls) - 1]
        spec = self._hf_spec()
        with unittest.mock.patch.object(serve, '_api_request_sync', side_effect=fake_req), \
             unittest.mock.patch.object(serve, '_accrue_spend') as acc:
            out = serve._api_execute_workflow(spec, spec['spend'], 'https://api.higgsfield.ai',
                                              'POST', {}, {'prompt': 'x'}, {'endpoint': 'higgsfield-ai/soul/v2/standard'})
        return out, calls, acc

    def test_estimate_accrued_on_success(self):
        out, calls, acc = self._run('completed')
        self.assertEqual(out['status'], 'completed')
        self.assertEqual(out['usd'], 0.094)
        self.assertEqual(out['ids'], {'endpoint': 'higgsfield-ai/soul/v2/standard', 'request_id': 'r1'})
        acc.assert_called_once_with('higgsfield', 0.094, agent_id=None)

    def test_no_charge_on_failed_terminal(self):
        out, calls, acc = self._run('failed')
        self.assertEqual(out['status'], 'failed')
        acc.assert_not_called()

    def test_workflow_submits_estimate_then_polls(self):
        out, calls, acc = self._run('completed')
        self.assertIn('https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard', calls)
        self.assertIn('https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard', calls)
        self.assertIn('https://api.higgsfield.ai/requests/r1/status', calls)


class Channels(unittest.TestCase):
    """Read-only channels: host->ordered-backend-chain lookup from the JSON
    seed, fallthrough behavior (first backend that answers wins), the
    player-only probe, and the endpoint wiring that routes an unregistered
    channel host through the chain instead of a plain direct GET."""

    def setUp(self):
        serve._load_api_services()

    def test_channel_host_lookup_exact_and_subdomain(self):
        self.assertEqual(serve._api_channel_for_host('reddit.com')['id'], 'reddit')
        self.assertEqual(serve._api_channel_for_host('www.reddit.com')['id'], 'reddit')
        self.assertEqual(serve._api_channel_for_host('x.com')['id'], 'twitter')
        self.assertEqual(serve._api_channel_for_host('sub.x.com')['id'], 'twitter')
        self.assertIsNone(serve._api_channel_for_host('example.com'))

    def test_channel_backend_url_rewrites(self):
        backend = {'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'}
        self.assertEqual(
            serve._api_backend_url(backend, 'https://reddit.com/r/programming'),
            'https://r.jina.ai/https://reddit.com/r/programming')

    def test_channel_backend_requires_registered_service(self):
        # A channel whose backend is NOT a registered service fails closed.
        chan = {'name': 'fake', 'backends': [{'service': 'does-not-exist', 'url_template': '{url}'}]}
        out = serve._api_channel_execute('ben', True, chan, 'https://x.com/s', 'GET', {}, None, 'p', None)
        self.assertTrue(out['allowed'])
        self.assertFalse(out['ok'])
        self.assertIn('not a registered service', out['error'])

    def test_channel_routes_through_first_working_backend(self):
        # Ordered chain: first backend that answers wins.
        calls = []
        fake_result = {'status': 200, 'finalUrl': 'https://r.jina.ai/https://reddit.com/r/x',
                       'contentType': 'text/markdown', 'body': '# Hello', 'truncated': False}
        def fake_req(method, url, headers, body):
            calls.append(url)
            return fake_result
        chan = {'name': 'Reddit', 'hosts': ['reddit.com'],
                'backends': [{'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'}]}
        with unittest.mock.patch.object(serve, '_api_request_sync', side_effect=fake_req):
            out = serve._api_channel_execute('ben', True, chan, 'https://reddit.com/r/x', 'GET', {}, None, 'p', None)
        self.assertTrue(out['ok'])
        self.assertEqual(calls, ['https://r.jina.ai/https://reddit.com/r/x'])
        self.assertEqual(out['channel'], 'Reddit')
        self.assertEqual(out['backend'], 'jina-reader')
        self.assertEqual(out['finalUrl'], 'https://reddit.com/r/x')

    def test_channel_falls_through_on_backend_failure(self):
        # First backend raises/errors, second answers -> chain falls through.
        calls = []
        fake_ok = {'status': 200, 'finalUrl': 'https://r.jina.ai/https://reddit.com/r/x',
                   'contentType': 'text/markdown', 'body': 'ok', 'truncated': False}
        def fake_req(method, url, headers, body):
            calls.append(url)
            if url.startswith('https://first.example'):
                raise RuntimeError('backend down')
            return fake_ok
        chan = {'name': 'Reddit', 'hosts': ['reddit.com'],
                'backends': [
                    {'service': 'jina-reader', 'url_template': 'https://first.example/{url}'},
                    {'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'},
                ]}
        with unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_api_request_sync', side_effect=fake_req):
            out = serve._api_channel_execute('ben', True, chan, 'https://reddit.com/r/x', 'GET', {}, None, 'p', None)
        self.assertTrue(out['ok'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], 'https://r.jina.ai/https://reddit.com/r/x')

    def test_channel_all_backends_fail_reports_error(self):
        def fake_req(method, url, headers, body):
            raise RuntimeError('all down')
        chan = {'name': 'Reddit', 'hosts': ['reddit.com'],
                'backends': [{'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'}]}
        with unittest.mock.patch.object(serve, '_api_request_sync', side_effect=fake_req):
            out = serve._api_channel_execute('ben', True, chan, 'https://reddit.com/r/x', 'GET', {}, None, 'p', None)
        self.assertTrue(out['allowed'])
        self.assertFalse(out['ok'])
        self.assertIn('All backends', out['error'])

    def test_channel_is_read_only(self):
        chan = {'name': 'Reddit', 'hosts': ['reddit.com'],
                'backends': [{'service': 'jina-reader', 'url_template': 'https://r.jina.ai/{url}'}]}
        out = serve._api_channel_execute('ben', True, chan, 'https://reddit.com/r/x', 'POST', {}, 'b', 'p', None)
        self.assertFalse(out['allowed'])
        self.assertIn('read-only', out['reason'])

    def test_endpoint_routes_unregistered_channel_host(self):
        # An unregistered host owned by a channel (reddit.com) goes through the
        # channel chain via /api/api-call: Jev runs (unregistered), then the
        # backend is fetched rather than a direct GET to reddit.com.
        fake_result = {'status': 200, 'finalUrl': 'https://r.jina.ai/https://reddit.com/r/x',
                       'contentType': 'text/markdown', 'body': '# Thread', 'truncated': False}
        self._start(self._gates())
        with unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result) as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://reddit.com/r/programming', 'method': 'GET', 'purpose': 'read thread'})
        out = r.json()
        self.assertTrue(out['allowed'])
        self.assertIn('Thread', out['textForModel'])
        sent_url = fetch.call_args.args[1]
        self.assertTrue(sent_url.startswith('https://r.jina.ai/'))
        self.assertIn('reddit.com', sent_url)

    def _client(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _gates(self, jev_allow=True):
        async def _allow(instructions, criteria):
            return ('allow' if jev_allow else 'block', 0.99 if jev_allow else 0.1, 0.0, 'trace-1')
        return [
            unittest.mock.patch.object(serve, 'API_CALL_ENABLED', True),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, '_jev_quorum_decision', _allow),
            unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=jev_allow),
            unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False),
            unittest.mock.patch.object(serve, 'log_action'),
        ]

    def _start(self, patchers):
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_channel_probe_requires_player_session(self):
        # Agent key alone must not run the probe -- it makes real outbound calls.
        from starlette.testclient import TestClient
        with unittest.mock.patch.multiple(
                serve, check_rate_limit=lambda a: True, verify_agent_key=lambda a, k: True,
                _valid_agent_key_presented=lambda k: True, log_action=lambda *a, **k: None):
            c = TestClient(serve.app)
            r = c.get('/api/channel-probe', headers={'X-Agent-Key': 'somekey'})
        self.assertEqual(r.status_code, 401)

    def test_channel_probe_reports_current_backend(self):
        # Player session: probe runs real (mocked) GETs per backend and reports
        # which backend currently answers, mirroring agent-reach doctor.
        from starlette.testclient import TestClient
        class _Resp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self, n=-1):
                return b''
        with unittest.mock.patch.multiple(
                serve, check_rate_limit=lambda a: True, log_action=lambda *a, **k: None,
                _is_safe_public_host=lambda h: True), \
             unittest.mock.patch.object(serve, '_safe_urlopen', return_value=_Resp()):
            c = TestClient(serve.app)
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
            r = c.get('/api/channel-probe')
        self.assertEqual(r.status_code, 200)
        channels = r.json()['channels']
        self.assertIn('reddit', channels)
        self.assertTrue(channels['reddit']['ok'])
        self.assertEqual(channels['reddit']['current'], 'jina-reader')
        self.assertIn('twitter', channels)
        self.assertIn('linkedin', channels)


if __name__ == '__main__':
    unittest.main()