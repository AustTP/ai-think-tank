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
                                        side_effect=lambda kind, question, on_approve_note='', **kwargs: (
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


class ApiServiceRevokeEndpoint(unittest.TestCase):
    """POST /api/api-service/revoke -- the ENDPOINT, operator-gated (player
    session OR director/admin agent). The grant path (proposal approval) is
    director-approvable via the escalation loop, so a director must be able to
    PULL a service when something changes (same unwind class as allowlist
    revoke); an ordinary agent key must not."""

    def setUp(self):
        # The registry stores the example base_url; no host lookups happen on
        # the revoke path, but keep the DNS-free convention of this file.
        p = unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True)
        p.start()
        self.addCleanup(p.stop)
        p = unittest.mock.patch.object(serve, 'log_action')
        self._log = p.start()
        self.addCleanup(p.stop)

    def _seed_active(self):
        sid, norm, error = serve._validate_api_service_spec(json.loads(json.dumps(_VALID_SPEC)))
        self.assertIsNone(error)
        serve._api_service_upsert(norm, status='active')
        self.addCleanup(lambda: serve._api_service_upsert(
            {'id': sid, 'base_url': norm['base_url']}, status='denied'))
        return sid

    def _client(self, session=True, agent_id=None):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        if session:
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        if agent_id:
            c.headers['X-Agent-Key'] = serve.get_or_create_agent_key(agent_id)
        return c

    def test_unauthenticated_is_rejected(self):
        from starlette.testclient import TestClient
        r = TestClient(serve.app).post('/api/api-service/revoke',
                                       json={'serviceId': 'my_service'})
        self.assertEqual(r.status_code, 401)

    def test_ordinary_agent_key_is_refused(self):
        # A valid agent key clears the login middleware, but the service
        # registry is not a worker's to mutate: the operator gate refuses,
        # and the service stays live.
        self._seed_active()
        r = self._client(session=False, agent_id='worker').post(
            '/api/api-service/revoke', json={'serviceId': 'my_service'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('my_service', serve._API_SERVICES)
        self._log.assert_not_called()

    def test_player_session_revokes_and_attributes_player(self):
        sid = self._seed_active()
        r = self._client().post('/api/api-service/revoke', json={'serviceId': sid.upper()})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['revoked'])
        self.assertEqual(body['serviceId'], sid)  # normalized to lowercase
        self.assertNotIn(sid, serve._API_SERVICES)
        self._log.assert_called_with('player', 'api_service_revoke', {'serviceId': sid})

    def test_director_agent_key_revokes_with_director_attribution(self):
        # The whole reason the gate moved: a director can pull a service.
        # Role check is mocked (the module temp DB has no seeded roster --
        # same approach as the feed-registry and allowlist tests); the
        # requester is the REAL key owner resolved from the header.
        sid = self._seed_active()
        with unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=True):
            r = self._client(session=False, agent_id='theo').post(
                '/api/api-service/revoke', json={'serviceId': sid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['revoked'])
        self.assertNotIn(sid, serve._API_SERVICES)
        self._log.assert_called_with('theo', 'api_service_revoke', {'serviceId': sid})

    def test_missing_and_unknown_service_id(self):
        r = self._client().post('/api/api-service/revoke', json={})
        self.assertEqual(r.status_code, 400)
        r = self._client().post('/api/api-service/revoke', json={'serviceId': 'ghost'})
        self.assertEqual(r.status_code, 404)


