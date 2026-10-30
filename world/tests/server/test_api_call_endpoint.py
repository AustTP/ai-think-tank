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
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', None), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}):
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

    def test_standing_safe_host_get_skips_jev(self):
        # STANDING_SAFE_DOMAINS read tier: a GET to a curated public-reference
        # host skips the Jev classify entirely, with no allowlist grant
        # involved (_is_allowlisted_host stays mocked False in _gates).
        async def _should_not_run(instructions, criteria):
            raise AssertionError('Jev must not run for a standing-host GET')
        self._start([
            unittest.mock.patch.object(serve, 'API_CALL_ENABLED', True),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, '_jev_quorum_decision', _should_not_run),
            unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False),
            unittest.mock.patch.object(serve, 'log_action'),
        ])
        fake_result = {'status': 200, 'finalUrl': 'https://www.nasa.gov/data',
                       'contentType': 'application/json', 'body': '{"ok": true}', 'truncated': False}
        with unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result):
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://www.nasa.gov/data', 'method': 'GET',
                'purpose': 'read a public dataset index'})
        self.assertTrue(r.json()['allowed'])

    def test_standing_safe_host_write_still_gates(self):
        # The standing tier is READ-ONLY: a POST to the same host goes
        # through Jev like any unregistered host (a block is honored).
        async def _block(instructions, criteria):
            return ('block', 0.99, 0.0, 'trace-1')
        self._start([
            unittest.mock.patch.object(serve, 'API_CALL_ENABLED', True),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, '_jev_quorum_decision', _block),
            unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False),
            unittest.mock.patch.object(serve, 'log_action'),
        ])
        r = self._client().post('/api/api-call', json={
            'agentId': 'ben', 'url': 'https://www.nasa.gov/submit', 'method': 'POST',
            'purpose': 'submit something'})
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

    def test_registered_post_body_sends_json_content_type(self):
        # A dict body is sent as JSON, so the request MUST carry
        # Content-Type: application/json -- Tavily 422s a JSON body sent
        # without the header ("Input should be a valid dictionary or object
        # to extract fields from"), which broke search_web end-to-end.
        self._start(self._gates())
        fake_result = {'status': 200, 'finalUrl': 'https://api.tavily.com/search',
                       'contentType': 'application/json', 'body': '{"results": []}', 'truncated': False}
        with unittest.mock.patch.object(serve, 'TAVILY_API_KEY', 'tok'), \
             unittest.mock.patch.object(serve, '_api_request_sync', return_value=fake_result) as fetch:
            r = self._client().post('/api/api-call', json={
                'agentId': 'ben', 'url': 'https://api.tavily.com/search', 'method': 'POST',
                'purpose': 'web search', 'body': {'query': 'test'}})
        self.assertTrue(r.json()['allowed'])
        sent_headers = fetch.call_args.args[2]
        self.assertEqual(sent_headers.get('Content-Type'), 'application/json')
        self.assertIn('api_key', fetch.call_args.args[3])

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




class TestApiCallFailureMarker(unittest.TestCase):
    """2026-10-30 Tier-1: a failed api_call left 'Approved, but the request
    failed: ...' in the model-facing text and the ask model then presented
    INVENTED numbers (173 cafes, 0 incidents) from calls that never
    succeeded. The failure text must be an unmissable no-data marker."""

    def test_failed_request_carries_no_data_marker(self):
        import urllib.error
        with unittest.mock.patch.object(
                serve, '_api_request_sync',
                side_effect=TimeoutError('The read operation timed out')), \
             unittest.mock.patch.object(serve, '_page_budget_exhausted',
                                        return_value=False):
            out = serve._api_execute('agent', None,
                                     'https://overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D',
                                     'GET', purpose='probe')
        self.assertFalse(out.get('ok'))
        self.assertIn('API CALL FAILED - NO DATA RETURNED', out.get('error', ''))
        self.assertIn('must not state any count', out.get('error', ''))
