"""OAuth2 refresh-token grant (2026-11-19, option A).

A new registry auth type ('oauth2_refresh_token') lets a service use the
PLAYER's long-lived OAuth refresh token -- stored encrypted in the vault --
while only a short-lived minted access token ever rides on an API request.
The client id/secret are app credentials from .env. Hermetic: no live
network, no real DB writes (temp dir).
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-oauth-refresh-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _spec():
    return {
        'id': 'google_calendar',
        'name': 'Google Calendar API',
        'base_url': 'https://www.googleapis.com',
        'credential': {'type': 'vault', 'name': 'google_oauth_refresh'},
        'auth': {'type': 'oauth2_refresh_token'},
        'methods': ['GET'],
        'path_rules': [{'prefix': '/calendar/v3/', 'methods': ['GET']}],
        'spend': {'kind': 'none'},
    }


class SpecValidation(unittest.TestCase):
    def setUp(self):
        serve._OAUTH2_REFRESH_CACHE.clear()

    def test_spec_validates_and_defaults_token_url(self):
        sid, spec, err = serve._validate_api_service_spec(_spec())
        self.assertIsNone(err)
        self.assertEqual(spec['auth']['token_url'], 'https://oauth2.googleapis.com/token')
        self.assertEqual(spec['auth']['clientIdEnv'], 'GOOGLE_OAUTH_CLIENT_ID')
        self.assertEqual(spec['auth']['clientSecretEnv'], 'GOOGLE_OAUTH_CLIENT_SECRET')

    def test_explicit_token_url_and_env_keys_survive(self):
        spec = _spec()
        spec['auth'] = {'type': 'oauth2_refresh_token',
                        'token_url': 'https://oauth2.googleapis.com/token',
                        'clientIdEnv': 'MY_ID_KEY', 'clientSecretEnv': 'MY_SECRET_KEY'}
        _, norm, err = serve._validate_api_service_spec(spec)
        self.assertIsNone(err)
        self.assertEqual(norm['auth']['token_url'], 'https://oauth2.googleapis.com/token')
        self.assertEqual(norm['auth']['clientIdEnv'], 'MY_ID_KEY')

    def test_rejects_private_token_url(self):
        spec = _spec()
        spec['auth'] = {'type': 'oauth2_refresh_token', 'token_url': 'http://127.0.0.1/token'}
        _, _, err = serve._validate_api_service_spec(spec)
        self.assertIn('token_url', err)

    def test_rejects_non_vault_credential(self):
        spec = _spec()
        spec['credential'] = {'type': 'env', 'key': 'SOMETHING'}
        _, _, err = serve._validate_api_service_spec(spec)
        self.assertIn('vault credential', err)


class ApplyAuth(unittest.TestCase):
    def setUp(self):
        serve._OAUTH2_REFRESH_CACHE.clear()

    def test_injects_bearer_from_minted_token(self):
        with unittest.mock.patch.object(serve, '_oauth2_refresh_access_token',
                                        return_value='ya29.minted'):
            _, headers, _ = serve._api_apply_auth(
                {'auth': {'type': 'oauth2_refresh_token',
                          'token_url': 'https://oauth2.googleapis.com/token'}},
                '1//refresh-token-value', None, 'GET',
                'https://www.googleapis.com/calendar/v3/x', {}, None)
        self.assertEqual(headers['Authorization'], 'Bearer ya29.minted')

    def test_missing_env_keys_fail_cleanly(self):
        with unittest.mock.patch.dict(serve._load_env(), {}, clear=True), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}), \
             self.assertRaises(ValueError) as ctx:
            serve._api_apply_auth(
                {'auth': {'type': 'oauth2_refresh_token',
                          'token_url': 'https://oauth2.googleapis.com/token'}},
                '1//refresh', None, 'GET', 'https://www.googleapis.com/x', {}, None)
        self.assertIn('client id/secret', str(ctx.exception))


class Mint(unittest.TestCase):
    ENV = {'GOOGLE_OAUTH_CLIENT_ID': 'apps.googleusercontent.com', 'GOOGLE_OAUTH_CLIENT_SECRET': 'GOCSPX-secret'}

    def setUp(self):
        serve._OAUTH2_REFRESH_CACHE.clear()

    def _run_capture(self, status=200, body=None, refresh='1//0abc'):
        captured = {}

        class _Resp:
            def __init__(self):
                self.status = status

            def geturl(self):
                return 'https://oauth2.googleapis.com/token'

            def read(self, n):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            captured['url'] = req.full_url
            captured['body'] = req.data.decode()
            return _Resp()

        env = dict(self.ENV)
        with unittest.mock.patch.object(serve, '_load_env', return_value=env), \
             unittest.mock.patch.object(serve, '_safe_urlopen', side_effect=_fake_urlopen), \
             unittest.mock.patch.object(serve, 'API_CALL_MAX_BODY_BYTES', 1000000):
            out = serve._oauth2_refresh_access_token(
                'https://oauth2.googleapis.com/token', refresh,
                env['GOOGLE_OAUTH_CLIENT_ID'], env['GOOGLE_OAUTH_CLIENT_SECRET'])
        return out, captured

    def test_exchange_shape_and_cache(self):
        payload = b'{"access_token": "ya29.aaa", "expires_in": 3600}'
        token, cap = self._run_capture(body=payload)
        self.assertEqual(token, 'ya29.aaa')
        self.assertIn('grant_type=refresh_token', cap['body'])
        self.assertIn('refresh_token=1%2F%2F0abc', cap['body'])
        self.assertIn('client_id=apps.googleusercontent.com', cap['body'])
        self.assertIn('client_secret=GOCSPX-secret', cap['body'])
        # Second call inside the expiry window: cache hit, no second HTTP.
        with unittest.mock.patch.object(serve, '_safe_urlopen', side_effect=AssertionError('no second mint')):
            again = serve._oauth2_refresh_access_token(
                'https://oauth2.googleapis.com/token', '1//0abc',
                self.ENV['GOOGLE_OAUTH_CLIENT_ID'], self.ENV['GOOGLE_OAUTH_CLIENT_SECRET'])
        self.assertEqual(again, 'ya29.aaa')

    def test_rotated_refresh_token_mints_fresh(self):
        self._run_capture(body=b'{"access_token": "ya29.old", "expires_in": 3600}')
        # A different refresh token must not be served the cached token.
        token, _ = self._run_capture(body=b'{"access_token": "ya29.new", "expires_in": 3600}', refresh='1//rotated')
        self.assertEqual(token, 'ya29.new')

    def test_error_code_surfaced_without_secrets(self):
        import urllib.error
        err = urllib.error.HTTPError('https://oauth2.googleapis.com/token', 400,
                                     'Bad Request', {}, None)
        original_read = err.read

        def _read_body(*a):
            return b'{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}'
        err.read = _read_body
        with unittest.mock.patch.object(serve, '_load_env', return_value=dict(self.ENV)), \
             unittest.mock.patch.object(serve, '_safe_urlopen', side_effect=err), \
             self.assertRaises(ValueError) as ctx:
            serve._oauth2_refresh_access_token(
                'https://oauth2.googleapis.com/token', '1//0abc',
                self.ENV['GOOGLE_OAUTH_CLIENT_ID'], self.ENV['GOOGLE_OAUTH_CLIENT_SECRET'])
        msg = str(ctx.exception)
        self.assertIn('HTTP 400', msg)
        self.assertIn('invalid_grant', msg)
        self.assertNotIn('1//0abc', msg)

    def test_missing_refresh_token_raises_clearly(self):
        with self.assertRaises(ValueError) as ctx:
            serve._oauth2_refresh_token_request_sync(
                'https://oauth2.googleapis.com/token', '', 'id', 'secret')
        self.assertIn('refresh token is not configured', str(ctx.exception))

    def test_non_https_token_url_rejected(self):
        with self.assertRaises(ValueError):
            serve._oauth2_refresh_access_token(
                'http://oauth2.googleapis.com/token', '1//0abc', 'id', 'secret')


if __name__ == '__main__':
    unittest.main()
