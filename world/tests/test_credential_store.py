"""Generic runtime-administered credential vault.

Any agent can PROPOSE a new credential (API token or username + password) at
runtime -- it is encrypted at rest the moment it lands, status='proposed', and
unusable until a director/admin or the player approves it. Use is scoped by
capability handles ("limited by the ask"): a handle binds one grantee to a
purpose, host scope, method scope, and TTL, and the server resolves the secret
server-side -- the agent never holds it. Legacy raw-token rows stay
compatible.

Hermetic: no live network, temp DB + Fernet key dir, gates patched.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class _CredentialCase(unittest.TestCase):
    def setUp(self):
        serve._rate_limit_calls.clear()  # full-suite isolation; see test_feed_registries setUpModule
        self.tmp = tempfile.mkdtemp(prefix='think-tank-credential-test-')
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

    def _store_encrypted(self, name, kind, **fields):
        payload = {'kind': kind, **fields}
        serve._store_credential(name, name, payload, status='active', proposed_by='player')
        return payload


class PayloadRoundtrip(_CredentialCase):
    def test_login_payload_roundtrips(self):
        serve._store_credential('myacct', 'ExampleCo',
                                {'kind': 'login', 'username': 'alice', 'password': 'hunter2'},
                                status='active', proposed_by='player')
        self.assertEqual(serve._credential_payload('myacct'),
                         {'kind': 'login', 'username': 'alice', 'password': 'hunter2'})
        # Encrypted at rest -- the plaintext password never appears in the DB.
        with serve._db() as conn:
            row = conn.execute('SELECT encrypted_value FROM external_credentials WHERE name = ?',
                               ('myacct',)).fetchone()
        self.assertNotIn('hunter2', row[0])

    def test_legacy_raw_token_stays_compatible(self):
        serve._store_credential('github', 'github', 'sk-very-secret')
        self.assertEqual(serve._credential_payload('github'),
                         {'kind': 'token', 'token': 'sk-very-secret'})
        # And the stored kind column is 'token' for old rows.
        with serve._db() as conn:
            kind = conn.execute('SELECT kind FROM external_credentials WHERE name = ?',
                                ('github',)).fetchone()[0]
        self.assertEqual(kind, 'token')


class Validator(_CredentialCase):
    def test_login_requires_username_and_password(self):
        n, s, p, err = serve._validate_credential_body(
            {'name': 'acct', 'kind': 'login', 'username': 'alice'})
        self.assertIsNone(n)
        self.assertIn('username and a password', err)
        n, s, p, err = serve._validate_credential_body(
            {'name': 'acct', 'kind': 'login', 'password': 'x'})
        self.assertIsNone(n)
        self.assertIn('username and a password', err)

    def test_token_requires_value(self):
        n, s, p, err = serve._validate_credential_body({'name': 'acct', 'kind': 'token'})
        self.assertIsNone(n)
        self.assertIn('value', err)

    def test_login_normalizes(self):
        n, s, p, err = serve._validate_credential_body(
            {'name': 'acct-1', 'service': 'Example', 'kind': 'login',
             'username': ' alice ', 'password': 'hunter2'})
        self.assertIsNone(err)
        self.assertEqual(n, 'acct-1')
        self.assertEqual(p['kind'], 'login')
        self.assertEqual(p['username'], 'alice')

    def test_rejects_bad_slug_and_kind(self):
        _, _, _, err = serve._validate_credential_body(
            {'name': 'bad name!', 'kind': 'token', 'value': 'x'})
        self.assertIsNotNone(err)
        _, _, _, err = serve._validate_credential_body(
            {'name': 'acct', 'kind': 'api_key', 'value': 'x'})
        self.assertIsNotNone(err)


class Masking(_CredentialCase):
    def test_mask_never_leaks_the_secret(self):
        self.assertEqual(serve._mask_secret('abcdefgh'), 'ab****gh')
        self.assertEqual(serve._mask_secret('ab'), '**')
        preview = serve._mask_credential_preview({'kind': 'login', 'username': 'alice', 'password': 'hunter2'})
        self.assertEqual(preview['username'], 'alice')
        self.assertNotIn('hunter2', preview['password'])
        self.assertNotIn('hunter2', json.dumps(preview))


class RegistryFlow(_CredentialCase):
    def setUp(self):
        super().setUp()
        self.key = serve.get_or_create_agent_key('ben')
        self.headers = {'X-Agent-Key': self.key}

    def test_propose_approve_deny_remove_lifecycle(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/credentials/propose', json={
            'agentId': 'ben', 'name': 'myacct', 'service': 'ExampleCo', 'kind': 'login',
            'username': 'alice', 'password': 'hunter2'}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        with serve._db() as conn:
            status = conn.execute('SELECT status FROM external_credentials WHERE name = ?',
                                  ('myacct',)).fetchone()[0]
            enc = conn.execute('SELECT encrypted_value FROM external_credentials WHERE name = ?',
                               ('myacct',)).fetchone()[0]
        self.assertEqual(status, 'proposed')
        self.assertNotIn('hunter2', enc)
        # Proposed credentials are not mintable yet.
        h, reason = serve.mint_capability_handle('ben', 'myacct', 'login to example', '*', ['GET'], 'player', 3600)
        self.assertIsNone(h)
        # A non-director agent cannot approve.
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='ben'), \
             unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=False):
            r = c.post('/api/credentials/approve', json={'name': 'myacct'}, headers=self.headers)
        self.assertEqual(r.status_code, 403)
        # A director approves; the response preview is masked, never the password.
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='director1'), \
             unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=True):
            r = c.post('/api/credentials/approve', json={'name': 'myacct'}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['credential']['kind'], 'login')
        self.assertIn('hunter2', serve._credential_payload('myacct')['password'])
        self.assertNotIn('hunter2', json.dumps(body))
        # Now mintable, and resolves to the structured login payload.
        h, reason = serve.mint_capability_handle('ben', 'myacct', 'login to example', 'example.com', ['GET'], 'director1', 600)
        self.assertTrue(h)
        grant = serve.resolve_capability_handle('ben', h, 'GET', 'https://example.com/login')
        self.assertIsNotNone(grant)
        self.assertEqual(grant['kind'], 'login')
        self.assertEqual(grant['username'], 'alice')
        self.assertEqual(grant['password'], 'hunter2')
        self.assertIsNone(grant['secret'])
        # Remove voids the handles and makes it un-mintable.
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=True):
            r = c.post('/api/credentials/remove', json={'name': 'myacct'})
        self.assertEqual(r.status_code, 200, r.text)
        with serve._db() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM capability_handles WHERE credential_name = ?',
                                           ('myacct',)).fetchone())
        self.assertIsNone(serve.resolve_capability_handle('ben', h, 'GET', 'https://example.com/login'))
        h2, _ = serve.mint_capability_handle('ben', 'myacct', 'again', '*', ['GET'], 'director1', 600)
        self.assertIsNone(h2)

    def test_list_is_operator_only_and_masked(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        serve._store_credential('token-acct', 'GitHub',
                                {'kind': 'token', 'token': 'sk-super-secret'}, status='active')
        # An agent key alone must not enumerate the vault.
        r = c.get('/api/credentials', headers=self.headers)
        self.assertEqual(r.status_code, 401)
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=True):
            r = c.get('/api/credentials')
        self.assertEqual(r.status_code, 200, r.text)
        entries = {e['name']: e for e in r.json()['credentials']}
        self.assertEqual(entries['token-acct']['kind'], 'token')
        self.assertIn('sk-super-secret', serve._credential_payload('token-acct')['token'])
        self.assertNotIn('sk-super-secret', json.dumps(r.json()))

    def test_duplicate_proposal_rejected(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        payload = {'agentId': 'ben', 'name': 'dup', 'service': 'X', 'kind': 'token', 'value': 'v1'}
        self.assertEqual(c.post('/api/credentials/propose', json=payload, headers=self.headers).status_code, 200)
        self.assertEqual(c.post('/api/credentials/propose', json={**payload, 'value': 'v2'},
                                headers=self.headers).status_code, 409)


class TokenHandleBackwardCompat(_CredentialCase):
    def test_token_kind_resolves_legacy_secret(self):
        serve._store_credential('gh', 'github', 'secret-1')
        h, _ = serve.mint_capability_handle('agent-a', 'gh', 'deploy', ['api.github.com'], ['GET'], 'player', 3600)
        grant = serve.resolve_capability_handle('agent-a', h, 'GET', 'https://api.github.com/repos/x')
        self.assertEqual(grant['kind'], 'token')
        self.assertEqual(grant['secret'], 'secret-1')
        self.assertIsNone(grant.get('password'))


class PlayerEndpoint(_CredentialCase):
    def test_player_can_add_login_credential_directly(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = c.post('/api/keys/credentials', json={
                'name': 'portal', 'service': 'Portal', 'kind': 'login',
                'username': 'alice', 'password': 'pw'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(serve._credential_payload('portal'),
                         {'kind': 'login', 'username': 'alice', 'password': 'pw'})
        with serve._db() as conn:
            status = conn.execute('SELECT status FROM external_credentials WHERE name = ?',
                                  ('portal',)).fetchone()[0]
        self.assertEqual(status, 'active')


class LoginAdapter(_CredentialCase):
    """The /api/credentials/login skeleton: capability-gated dispatch to a
    per-service login adapter. No real account needed -- a fake adapter proves
    the gating, the scope enforcement, and the secret scrubbing."""

    def setUp(self):
        super().setUp()
        self._saved_adapters = dict(serve._LOGIN_ADAPTERS)
        self.key = serve.get_or_create_agent_key('ben')
        self.headers = {'X-Agent-Key': self.key}
        serve._store_credential('portal', 'Portal', {'kind': 'login', 'username': 'alice', 'password': 'hunter2'},
                                status='active', proposed_by='player')

    def tearDown(self):
        serve._LOGIN_ADAPTERS.clear()
        serve._LOGIN_ADAPTERS.update(self._saved_adapters)
        super().tearDown()

    def _register_fake(self, captures):
        def fake(grant, action):
            captures.append((grant, action))
            return {'ok': True, 'status': 200,
                    'result': {'title': 'Dashboard', 'balance': 100,
                               'password': 'hunter2', 'nested': {'token': 'secret-token'}}}
        serve._LOGIN_ADAPTERS['portal'] = {'login_url': 'https://example.com/login', 'fn': fake}

    def _mint_handle(self, host='example.com'):
        h, reason = serve.mint_capability_handle('ben', 'portal', 'read portal balance',
                                                 [host], ['GET'], 'player', 600)
        self.assertTrue(h, reason)
        return h

    def test_login_dispatch_scrubs_secrets_and_scopes(self):
        from starlette.testclient import TestClient
        captures = []
        self._register_fake(captures)
        c = TestClient(serve.app)
        h = self._mint_handle()
        r = c.post('/api/credentials/login', json={
            'agentId': 'ben', 'capabilityHandle': h, 'service': 'portal',
            'action': {'method': 'GET', 'url': 'https://example.com/balance'}},
            headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        # The adapter saw the resolved username/password (that is its contract).
        self.assertEqual(len(captures), 1)
        grant, action = captures[0]
        self.assertEqual(grant['username'], 'alice')
        self.assertEqual(grant['password'], 'hunter2')
        self.assertEqual(action['url'], 'https://example.com/balance')
        # But the agent never sees them: the response is scrubbed.
        self.assertNotIn('hunter2', json.dumps(body))
        self.assertNotIn('secret-token', json.dumps(body))
        inner = body['result']['result']
        self.assertEqual(inner['password'], serve._mask_secret('hunter2'))
        self.assertEqual(inner['nested']['token'], serve._mask_secret('secret-token'))

    def test_out_of_scope_action_host_is_refused_before_dispatch(self):
        from starlette.testclient import TestClient
        captures = []
        self._register_fake(captures)
        c = TestClient(serve.app)
        h = self._mint_handle('example.com')
        r = c.post('/api/credentials/login', json={
            'agentId': 'ben', 'capabilityHandle': h, 'service': 'portal',
            'action': {'method': 'GET', 'url': 'https://evil.example.com/steal'}},
            headers=self.headers)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(captures, [])

    def test_token_kind_handle_is_refused(self):
        from starlette.testclient import TestClient
        serve._store_credential('gh', 'github', 'secret-1', status='active')
        h, _ = serve.mint_capability_handle('ben', 'gh', 'deploy', ['api.github.com'], ['GET'], 'player', 600)
        c = TestClient(serve.app)
        r = c.post('/api/credentials/login', json={
            'agentId': 'ben', 'capabilityHandle': h, 'service': 'github',
            'action': {'method': 'GET', 'url': 'https://api.github.com/repos/x'}},
            headers=self.headers)
        self.assertEqual(r.status_code, 400, r.text)

    def test_missing_adapter_returns_501_with_contract(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        h = self._mint_handle()
        r = c.post('/api/credentials/login', json={
            'agentId': 'ben', 'capabilityHandle': h, 'service': 'nosuchsvc',
            'action': {'method': 'GET', 'url': 'https://example.com/balance'}},
            headers=self.headers)
        self.assertEqual(r.status_code, 501, r.text)
        self.assertIn('_LOGIN_ADAPTERS["nosuchsvc"]', r.json()['error'])

    def test_invalid_handle_is_refused(self):
        from starlette.testclient import TestClient
        self._register_fake([])
        c = TestClient(serve.app)
        r = c.post('/api/credentials/login', json={
            'agentId': 'ben', 'capabilityHandle': 'x' * 64, 'service': 'portal',
            'action': {'method': 'GET', 'url': 'https://example.com/balance'}},
            headers=self.headers)
        self.assertEqual(r.status_code, 403, r.text)

    def test_scrub_masks_secret_keys_recursively(self):
        result = {'ok': True, 'data': {'Password': 'a1b2', 'user': 'x', 'nested': {'auth': 'y'}}}
        scrubbed = serve._login_scrub_result(result)
        self.assertNotIn('a1b2', json.dumps(scrubbed))
        self.assertNotIn('y', json.dumps(scrubbed))
        self.assertEqual(scrubbed['data']['user'], 'x')


if __name__ == '__main__':
    unittest.main()