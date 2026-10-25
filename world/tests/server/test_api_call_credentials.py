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
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', None), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}):
            token, token2, err = serve._api_credential_value(spec)
        self.assertIsNone(token)
        self.assertIn('GITHUB_TOKEN', err)

    def test_two_part_env_credential(self):
        spec = {'credential': {'type': 'env', 'key': 'HIGGSFIELD_API_KEY_ID', 'key2': 'HIGGSFIELD_API_KEY_SECRET'}}
        with unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', 'id'), \
             unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec'):
            token, token2, err = serve._api_credential_value(spec)
        self.assertEqual((token, token2, err), ('id', 'sec', None))

    def test_env_credential_falls_back_to_live_env_without_a_module_global(self):
        # A brand-new service key with no module global (e.g. a fresh .env key
        # an operator adds for a new registry row) must resolve from .env --
        # previously this required a code change to add the global.
        spec = {'credential': {'type': 'env', 'key': 'BRAND_NEW_API_KEY'}}
        self.assertFalse(hasattr(serve, 'BRAND_NEW_API_KEY'))
        with unittest.mock.patch.object(serve, '_load_env', return_value={'BRAND_NEW_API_KEY': 'secret'}):
            token, token2, err = serve._api_credential_value(spec)
        self.assertEqual((token, token2, err), ('secret', None, None))

    def test_module_global_wins_over_live_env(self):
        # Existing behavior preserved: a patched/rebound module global is
        # authoritative when both it and .env hold a value.
        spec = {'credential': {'type': 'env', 'key': 'GITHUB_TOKEN'}}
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', 'from-global'), \
             unittest.mock.patch.object(serve, '_load_env', return_value={'GITHUB_TOKEN': 'from-env'}):
            token, _, err = serve._api_credential_value(spec)
        self.assertEqual(token, 'from-global')
        self.assertIsNone(err)

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


