"""Tests for Phase D: external-credential vault + capability handles.

Covers the confused-deputy core: an agent presents an opaque handle, the server
verifies scope (right agent, unexpired, host+method in scope), decrypts the
real credential in-process, and injects it into the outbound request -- never
returning or logging the raw secret. Runs against a hermetic temp DB + a temp
Fernet key dir so nothing touches the live vault or makes network calls.
"""
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from fastapi.testclient import TestClient


def setUpModule():
    # Hermetic: single-slug decision chains. The CLI standby (enabled via the
    # project .env) would append a provider, arming the shared-process breaker.
    serve.COLAB_STANDBY_ENABLED = False


def tearDownModule():
    serve.COLAB_STANDBY_ENABLED = str(
        serve._load_env().get('COLAB_STANDBY_ENABLED', '') or ''
    ).lower() in ('1', 'true', 'yes')


class CapabilityKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-keys-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            # Fernet EDEK master key must live in the temp dir, not the repo.
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_credential_roundtrip_and_never_listed(self):
        serve._store_credential('github-token', 'github', 'sk-very-secret')
        listed = serve._list_credentials()
        self.assertEqual(listed, [{'name': 'github-token', 'service': 'github',
                                   'kind': 'token', 'status': 'active', 'proposedBy': None}])
        # The raw value must never be readable back out of the vault.
        row = None
        with serve._db() as conn:
            row = conn.execute(
                'SELECT encrypted_value FROM external_credentials WHERE name = ?',
                ('github-token',)).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn('sk-very-secret', row[0])

    def test_handle_mint_requires_existing_credential(self):
        no_handle, reason = serve.mint_capability_handle(
            'agent-a', 'does-not-exist', 'deploy', '*', ['GET'], 'player', 3600)
        self.assertIsNone(no_handle)
        self.assertEqual(reason, 'unknown credential')

        serve._store_credential('gh', 'github', 'secret-1')
        h, reason = serve.mint_capability_handle(
            'agent-a', 'gh', 'deploy', '*', ['GET'], 'player', 3600)
        self.assertTrue(h)
        self.assertIsNone(reason)
        self.assertNotIn('secret-1', h)
        self.assertEqual(len(h), 64)

    def test_resolve_checks_scope(self):
        serve._store_credential('gh', 'github', 'secret-1')
        h, _ = serve.mint_capability_handle(
            'agent-a', 'gh', 'deploy', ['api.github.com'], ['GET'], 'player', 3600)

        # Right agent + right host + right method -> resolves, secret decrypted.
        grant = serve.resolve_capability_handle('agent-a', h, 'GET', 'https://api.github.com/repos/x')
        self.assertIsNotNone(grant)
        self.assertEqual(grant['secret'], 'secret-1')
        self.assertEqual(grant['service'], 'github')

        # Wrong agent.
        self.assertIsNone(serve.resolve_capability_handle('agent-b', h, 'GET', 'https://api.github.com/x'))
        # Wrong host (not in the allowed scape).
        self.assertIsNone(serve.resolve_capability_handle('agent-a', h, 'GET', 'https://evil.example.com'))
        # Wrong method.
        self.assertIsNone(serve.resolve_capability_handle('agent-a', h, 'POST', 'https://api.github.com/x'))
        # Wildcard host still lets any host through.
        h2, _ = serve.mint_capability_handle('agent-a', 'gh', 'anywhere', '*', ['GET'], 'player', 3600)
        self.assertIsNotNone(serve.resolve_capability_handle('agent-a', h2, 'GET', 'https://anything.example.com/x'))

    def test_resolve_rejects_expired_handle(self):
        serve._store_credential('gh', 'github', 'secret-1')
        # ttl 1s: mint with a short TTL, then time-travel past expiry.
        h, _ = serve.mint_capability_handle('agent-a', 'gh', 'deploy', '*', ['GET'], 'player', 0)
        with serve._db() as conn:
            conn.execute('UPDATE capability_handles SET expires_at = ? WHERE handle = ?',
                         (serve.time.time() - 10, h))
        self.assertIsNone(serve.resolve_capability_handle('agent-a', h, 'GET', 'https://api.github.com/x'))
        # And the stale row was swept out.
        with serve._db() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM capability_handles WHERE handle = ?', (h,)).fetchone())

    def test_expire_sweep_removes_stale_handles(self):
        serve._store_credential('gh', 'github', 'secret-1')
        live, _ = serve.mint_capability_handle('agent-a', 'gh', 'a', '*', ['GET'], 'player', 3600)
        stale, _ = serve.mint_capability_handle('agent-a', 'gh', 'b', '*', ['GET'], 'player', 3600)
        with serve._db() as conn:
            conn.execute('UPDATE capability_handles SET expires_at = ? WHERE handle = ?',
                         (serve.time.time() - 5, stale))
        serve._expire_handles()
        with serve._db() as conn:
            remains = [r[0] for r in conn.execute('SELECT handle FROM capability_handles')]
        self.assertIn(live, remains)
        self.assertNotIn(stale, remains)

    def test_revoke_all_clears_agents_handles(self):
        serve._store_credential('gh', 'github', 'secret-1')
        serve.mint_capability_handle('agent-a', 'gh', 'x', '*', ['GET'], 'player', 3600)
        serve.mint_capability_handle('agent-a', 'gh', 'y', '*', ['GET'], 'player', 3600)
        survive, _ = serve.mint_capability_handle('agent-b', 'gh', 'z', '*', ['GET'], 'player', 3600)
        serve.revoke_all_handles('agent-a')
        with serve._db() as conn:
            remains = [r[0] for r in conn.execute('SELECT handle FROM capability_handles')]
        self.assertEqual(remains, [survive])

    def test_delete_credential_voids_its_handles(self):
        serve._store_credential('gh', 'github', 'secret-1')
        h, _ = serve.mint_capability_handle('agent-a', 'gh', 'x', '*', ['GET'], 'player', 3600)
        self.assertIsNotNone(serve.resolve_capability_handle('agent-a', h, 'GET', 'https://x.y/z'))
        serve._delete_credential('gh')
        # Credential gone, and the handle that pointed at it can no longer resolve.
        self.assertIsNone(serve.resolve_capability_handle('agent-a', h, 'GET', 'https://x.y/z'))
        with serve._db() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM capability_handles WHERE handle = ?', (h,)).fetchone())

    def test_capability_auth_headers_are_per_service(self):
        # /api/curl injects whatever this returns into the outbound request.
        # DigitalOcean and PixelLab both take a standard Bearer token; Treg's
        # real API does not (X-Treg-Token instead) --
        # Real handle-authenticated action.
        self.assertEqual(serve._capability_auth_headers('digitalocean', 's3cret'),
                          {'Authorization': 'Bearer s3cret'})
        self.assertEqual(serve._capability_auth_headers('pixellab', 's3cret'),
                          {'Authorization': 'Bearer s3cret'})
        self.assertEqual(serve._capability_auth_headers('treg', 's3cret'),
                          {'X-Treg-Token': 's3cret'})
        # Unknown credential names default to Bearer, the common case.
        self.assertEqual(serve._capability_auth_headers('some-new-service', 's3cret'),
                          {'Authorization': 'Bearer s3cret'})

    def test_mint_refused_when_hard_cap_exceeded(self):
        # The hard circuit breaker: a credential in
        # _HARD_CAPPED_CREDENTIALS with a configured budgetCapUsd on its
        # product must refuse EVEN the mint step once real usage is at/over
        # cap -- the agent never even gets a handle to try. Runs with the
        # DigitalOcean master switch ON (SANDBOX_EXECUTION=digitalocean) so
        # the balance cap is what's being tested, not the switch gate.
        serve._store_credential('digitalocean', 'digitalocean', 'do-secret')
        state = {'products': {'digitalocean': {'budgetCapUsd': 25}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=True), \
             unittest.mock.patch.dict(serve._HARD_CAPPED_CREDENTIALS,
                                       {'digitalocean': lambda: 30.0}):
            handle, reason = serve.mint_capability_handle(
                'agent-a', 'digitalocean', 'check balance', '*', ['GET'], 'player', 3600)
        self.assertIsNone(handle)
        self.assertIn('25.00', reason)
        # And under cap, minting succeeds normally.
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=True), \
             unittest.mock.patch.dict(serve._HARD_CAPPED_CREDENTIALS,
                                       {'digitalocean': lambda: 5.0}):
            handle, reason = serve.mint_capability_handle(
                'agent-a', 'digitalocean', 'check balance', '*', ['GET'], 'player', 3600)
        self.assertIsNotNone(handle)
        self.assertIsNone(reason)

    def test_digitalocean_master_switch_blocks_mint_while_local(self):
        # The player's rule: DigitalOcean must be UNREACHABLE
        # unless SANDBOX_EXECUTION=digitalocean. While the switch reads
        # `local` (the default), even a provisioned DO credential and a
        # healthy balance must NOT yield a handle -- the switch is the sole
        # gate, checked BEFORE the balance circuit breaker.
        serve._store_credential('digitalocean', 'digitalocean', 'do-secret')
        state = {'products': {'digitalocean': {'budgetCapUsd': 25}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=False), \
             unittest.mock.patch.dict(serve._HARD_CAPPED_CREDENTIALS,
                                       {'digitalocean': lambda: 0.0}):
            handle, reason = serve.mint_capability_handle(
                'agent-a', 'digitalocean', 'check balance', '*', ['GET'], 'player', 3600)
        self.assertIsNone(handle)
        self.assertIn('SANDBOX_EXECUTION', reason)
        # Non-DO credentials are unaffected by the switch.
        serve._store_credential('gh', 'github', 'secret-1')
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=False):
            h, reason = serve.mint_capability_handle(
                'agent-a', 'gh', 'deploy', '*', ['GET'], 'player', 3600)
        self.assertTrue(h)
        self.assertIsNone(reason)

    def test_digitalocean_master_switch_blocks_stale_handle_resolution(self):
        # Defense in depth: a handle minted while the switch was ON must not
        # keep resolving after it's flipped back to `local`.
        serve._store_credential('digitalocean', 'digitalocean', 'do-secret')
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=True):
            h, _ = serve.mint_capability_handle(
                'agent-a', 'digitalocean', 'check balance', '*', ['GET'], 'player', 3600)
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=False):
            self.assertIsNone(serve.resolve_capability_handle(
                'agent-a', h, 'GET', 'https://api.digitalocean.com/v2/customers/my/balance'))
        # And with the switch back ON it resolves normally again.
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=True):
            grant = serve.resolve_capability_handle(
                'agent-a', h, 'GET', 'https://api.digitalocean.com/v2/customers/my/balance')
        self.assertIsNotNone(grant)
        self.assertEqual(grant['secret'], 'do-secret')

    def test_digitalocean_balance_check_fails_closed_while_local(self):
        # While the switch is `local` the balance check returns None WITHOUT
        # touching the network -- _credential_over_cap then treats an
        # unverifiable balance as over cap (fails closed), so DO cannot be
        # granted even through the account-usage path.
        serve._store_credential('digitalocean', 'digitalocean', 'do-secret')
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=False), \
             unittest.mock.patch.object(serve, 'urllib') as fake_urllib:
            self.assertIsNone(serve._digitalocean_account_balance())
        fake_urllib.request.Request.assert_not_called()

    def test_sandbox_execution_fails_closed_when_switch_set_to_digitalocean(self):
        # Flipping SANDBOX_EXECUTION=digitalocean without a provisioned remote
        # executor must NEVER silently run the command locally -- it fails
        # closed with an explicit error instead.
        with unittest.mock.patch.object(serve, 'SANDBOX_EXECUTION', 'digitalocean'):
            result = serve._run_in_sandbox_sync('/tmp/whatever', 'echo hi')
        self.assertIsNone(result['exitCode'])
        self.assertIn('has not been provisioned', result['stderr'])
        self.assertEqual(result['stdout'], '')
        # And the default `local` backend still runs locally (the switch does
        # not change behavior unless it explicitly says digitalocean).
        with unittest.mock.patch.object(serve, 'SANDBOX_EXECUTION', 'local'), \
             unittest.mock.patch.object(serve, 'subprocess') as fake_subprocess, \
             unittest.mock.patch.object(serve, 'SANDBOX_NETWORK', 'net'), \
             unittest.mock.patch.object(serve, 'PROXY_CONTAINER', 'proxy'), \
             unittest.mock.patch.object(serve, 'PROXY_PORT', 8080), \
             unittest.mock.patch.object(serve, 'SANDBOX_IMAGE', 'img'):
            fake_subprocess.run.return_value = unittest.mock.Mock(
                returncode=0, stdout='ok', stderr='')
            result = serve._run_in_sandbox_sync('/tmp/whatever', 'echo hi')
        self.assertEqual(result['exitCode'], 0)
        self.assertEqual(result['stdout'], 'ok')

    def test_revoke_agent_credentials_clears_keys_grants_and_handles(self):
        # The fire path: one call must wipe every standing credential for the
        # agent -- attribution secret, temp grants, and capability handles --
        # while leaving other agents' grants intact.
        serve._store_credential('gh', 'github', 'secret-1')
        key = serve.get_or_create_agent_key('agent-a')
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO temp_access_grants (agent_id, capability, granted_by, reason, granted_at, expires_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                ('agent-a', 'library_write', 'faye', 'review', 0, serve.time.time() + 3600))
        h, _ = serve.mint_capability_handle('agent-a', 'gh', 'x', '*', ['GET'], 'player', 3600)
        other_key = serve.get_or_create_agent_key('agent-b')
        other_handle, _ = serve.mint_capability_handle('agent-b', 'gh', 'y', '*', ['GET'], 'player', 3600)

        serve.revoke_agent_credentials('agent-a')

        # agent-a loses its key, grants, and handles.
        with serve._db() as conn:
            key_row = conn.execute('SELECT 1 FROM agent_keys WHERE agent_id = ?', ('agent-a',)).fetchone()
            grant_row = conn.execute('SELECT 1 FROM temp_access_grants WHERE agent_id = ?', ('agent-a',)).fetchone()
            handle_row = conn.execute('SELECT 1 FROM capability_handles WHERE agent_id = ?', ('agent-a',)).fetchone()
        self.assertIsNone(key_row)
        self.assertIsNone(grant_row)
        self.assertIsNone(handle_row)
        # agent-b is untouched.
        with serve._db() as conn:
            self.assertEqual(conn.execute('SELECT secret_key FROM agent_keys WHERE agent_id = ?', ('agent-b',)).fetchone()[0], other_key)
            self.assertIsNotNone(conn.execute('SELECT 1 FROM capability_handles WHERE handle = ?', (other_handle,)).fetchone())
        # And the old key no longer verifies for a freshly re-minted one (re-hire mints fresh).
        fresh = serve.get_or_create_agent_key('agent-a')
        self.assertNotEqual(fresh, key)
        self.assertIs(serve.verify_agent_key('agent-a', key), False)


class CurlCredentialEcho(unittest.TestCase):
    """Real, narrow gap found in a security audit: a capability-
    handle-authenticated /api/curl response used to go back to the agent
    completely raw. If the target API ever echoed the injected credential
    back (some APIs do, in error/debug responses), that secret would land in
    the agent's visible tool output -- and from there could get written into
    the shared, PERSISTENT sandbox (workroom-shared/research-shared),
    readable by any later, unrelated task. Same shape as the real Pocket
    OS/Railway incident (a leftover credential found in an unrelated file).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-curl-echo-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        serve._store_credential('some-api', 'Some API', 'sk-live-real-secret-value')
        self.agent_key = serve.get_or_create_agent_key('eli')
        self.handle, refusal = serve.mint_capability_handle(
            'eli', 'some-api', 'test', '*', ['GET'], 'player', 3600)
        self.assertIsNone(refusal)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_echoed_credential_is_redacted_from_body_and_headers(self):
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        fake_response = {
            'status': 200, 'finalUrl': 'https://api.example.com/whoami',
            'headers': {'X-Echoed-Auth': 'Bearer sk-live-real-secret-value'},
            'body': 'You sent: Authorization: Bearer sk-live-real-secret-value',
            'truncated': False,
        }
        with unittest.mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'fake'), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync'), \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)), \
             unittest.mock.patch.object(serve, '_curl_request_sync', return_value=fake_response):
            r = c.post('/api/curl', json={
                'agentId': 'eli', 'url': 'https://api.example.com/whoami', 'method': 'GET',
                'purpose': 'test', 'capabilityHandle': self.handle,
            }, headers={'X-Agent-Key': self.agent_key})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        raw = str(body)
        self.assertNotIn('sk-live-real-secret-value', raw)
        self.assertIn('[REDACTED]', body['body'])
        self.assertIn('[REDACTED]', body['headers']['X-Echoed-Auth'])

    def test_no_handle_used_leaves_unrelated_content_untouched(self):
        # No capability handle presented -> no secret to redact by exact
        # value; ordinary response content must pass through unchanged.
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        fake_response = {
            'status': 200, 'finalUrl': 'https://api.example.com/public',
            'headers': {'Content-Type': 'application/json'},
            'body': '{"ok": true}', 'truncated': False,
        }
        with unittest.mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'fake'), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync'), \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)), \
             unittest.mock.patch.object(serve, '_curl_request_sync', return_value=fake_response):
            r = c.post('/api/curl', json={
                'agentId': 'eli', 'url': 'https://api.example.com/public', 'method': 'GET',
                'purpose': 'test',
            }, headers={'X-Agent-Key': self.agent_key})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['body'], '{"ok": true}')


class HandlesEndpointAuth(unittest.TestCase):
    """HTTP-level test of POST/DELETE /api/keys/handles' 'player-only' gate.

    Real handle-authenticated
    action, that the gate used _resolve_requester(request) -- which reads
    "player" from the ABSENCE of a self-declared `requesterId` query param,
    not from a verified player session. An agent's own HTTP client already
    needs a valid X-Agent-Key just to clear the global auth middleware for
    this path (it's in AUTH_PROTECTED_PREFIXES); it could then reach this
    handler and mint itself a handle -- to DigitalOcean, with DELETE
    allowed, if it chose to ask for that -- simply by not sending
    `requesterId`. Fixed by requiring an actual verified player session
    cookie instead. No test caught this because no HTTP-level test of this
    endpoint existed before now -- only the pure mint_capability_handle /
    resolve_capability_handle functions were covered above."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-handles-auth-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        serve._store_credential('gh', 'github', 'secret-1')
        self.agent_key = serve.get_or_create_agent_key('agent-a')

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mint_body(self):
        return {'agentId': 'agent-a', 'credentialName': 'gh', 'purpose': 'x',
                'allowedHosts': '*', 'allowedMethods': ['GET']}

    def test_agent_key_alone_cannot_self_mint_even_without_requesterId(self):
        # The exact bypass: a valid agent key, no player session cookie, and
        # no `requesterId` query param -- this must now be refused, not
        # silently treated as the player.
        c = TestClient(serve.app)
        r = c.post('/api/keys/handles', json=self._mint_body(),
                   headers={'X-Agent-Key': self.agent_key})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn('handle minting', r.json().get('error', ''))
        with serve._db() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM capability_handles').fetchone())

    def test_agent_key_with_requesterId_is_also_refused(self):
        c = TestClient(serve.app)
        r = c.post('/api/keys/handles?requesterId=agent-a', json=self._mint_body(),
                   headers={'X-Agent-Key': self.agent_key})
        self.assertEqual(r.status_code, 403, r.text)

    def test_no_auth_at_all_is_401_from_the_middleware(self):
        c = TestClient(serve.app)
        r = c.post('/api/keys/handles', json=self._mint_body())
        self.assertEqual(r.status_code, 401, r.text)

    def test_real_player_session_can_mint(self):
        session_id = serve.create_session()
        c = TestClient(serve.app, cookies={serve.SESSION_COOKIE_NAME: session_id})
        r = c.post('/api/keys/handles', json=self._mint_body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get('handle'))

    def test_real_player_session_can_delete(self):
        session_id = serve.create_session()
        h, _ = serve.mint_capability_handle('agent-a', 'gh', 'x', '*', ['GET'], 'player', 3600)
        c = TestClient(serve.app, cookies={serve.SESSION_COOKIE_NAME: session_id})
        r = c.delete(f'/api/keys/handles/{h}')
        self.assertEqual(r.status_code, 200, r.text)

    def test_agent_key_alone_cannot_delete(self):
        h, _ = serve.mint_capability_handle('agent-a', 'gh', 'x', '*', ['GET'], 'player', 3600)
        c = TestClient(serve.app)
        r = c.delete(f'/api/keys/handles/{h}', headers={'X-Agent-Key': self.agent_key})
        self.assertEqual(r.status_code, 403, r.text)
        with serve._db() as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM capability_handles WHERE handle = ?', (h,)).fetchone())


class PerStoryCapabilityGrant(unittest.TestCase):
    """Todo 14: a temporary capability grant is tied to the SPECIFIC story it
    was granted for (task_id) and dies with it -- revoke_task_access fires when
    the story ships, so capability access never outlives the work that
    justified it (and the access-request endpoint only accepts the agent's own
    live task)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-storygrant-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _grant(self, task_id='task-9'):
        return serve._grant_temp_access('agent-a', 'curl', 'faye', 'need it for the weather crawl', task_id=task_id)

    def test_grant_records_its_task_id(self):
        self._grant()
        with serve._db() as conn:
            row = conn.execute('SELECT task_id FROM temp_access_grants WHERE agent_id = ?', ('agent-a',)).fetchone()
        self.assertEqual(row[0], 'task-9')

    def test_revoking_a_task_clears_only_that_storys_grants(self):
        self._grant(task_id='task-9')
        serve._grant_temp_access('agent-b', 'curl', 'faye', 'other story', task_id='task-10')
        serve.revoke_task_access('task-9')
        with serve._db() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM temp_access_grants WHERE agent_id = ?', ('agent-a',)).fetchone())
            self.assertIsNotNone(conn.execute('SELECT 1 FROM temp_access_grants WHERE agent_id = ?', ('agent-b',)).fetchone(),
                                 'an unrelated story\'s grant survives')

    def test_revoke_task_access_is_a_noop_without_a_task(self):
        self._grant()
        serve.revoke_task_access(None)
        serve.revoke_task_access('')
        with serve._db() as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM temp_access_grants WHERE agent_id = ?', ('agent-a',)).fetchone())

    def test_story_scoped_grant_rides_no_timer(self):
        expires_at = self._grant(task_id='task-9')
        self.assertIsNone(expires_at, 'a story-scoped grant has no clock -- it dies with the story')
        self.assertTrue(serve._has_active_temp_access('agent-a', 'curl'),
                        'no-timer grant is active while its story is live')
        with serve._db() as conn:
            row = conn.execute('SELECT expires_at FROM temp_access_grants WHERE agent_id = ?', ('agent-a',)).fetchone()
        self.assertIsNone(row[0])

    def test_grant_without_a_story_keeps_a_finite_window(self):
        expires_at = serve._grant_temp_access('agent-a', 'curl', 'faye', 'standing helper access')
        self.assertIsNotNone(expires_at, 'a grant with no story still rides the finite timer')
        self.assertGreater(expires_at, time.time())
        self.assertTrue(serve._has_active_temp_access('agent-a', 'curl'))


if __name__ == '__main__':
    unittest.main()