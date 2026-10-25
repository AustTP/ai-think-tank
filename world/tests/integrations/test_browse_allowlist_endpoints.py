"""Player-vetted browse allowlist: a small set of domains that
skip Jev's classify+escalate round trip for /api/browse entirely, because the
PLAYER already vetted them -- everything else still goes through full Jev
policy-as-code, unchanged. Real request after a DreyX.com investigation got
blocked mid-run: Jev's classification confidence for the SAME url swung
across runs (one run: clean allow; the next: 'escalated_unsure' at 0.56
confidence, denied by the director escalation), so a site the player had
already decided was fine could still get randomly blocked.

DB-isolated (setUpModule below) since /api/browse's every path calls
log_action, a real DB write.
"""
import json
import os
import shutil
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-browse-allowlist-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        # BROWSE_TRAIL_PATH is a module-level constant computed once from the
        # REAL THINK_TANK_DIR at import time -- patching THINK_TANK_DIR alone does
        # NOT recompute it, so any test exercising a confident Jev allow
        # without explicitly mocking record_browse_success would otherwise
        # write a real browse_trail.json into the actual project directory
        # (running this exact suite).
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        # The CLI-run Laya standby appends a provider to every decision chain
        # when enabled (it IS enabled in the project .env, which serves loads).
        # Hermetic modules must pin it off so decision chains stay single-slug
        # and the multi-slug circuit breaker never arms in the test process
        # (arm + leak would poison later modules with open breakers).
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
def _fake_db_ctx(rows):
    """A stand-in for `with serve._db() as conn:` whose execute() returns
    `rows` from fetchall() -- used by the team_status note query tests."""
    conn = unittest.mock.MagicMock()
    conn.execute.return_value.fetchall.return_value = list(rows)
    ctx = unittest.mock.MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx


class AllowlistRequestEndpoint(unittest.TestCase):
    """POST /api/allowlist/request -- the agent-facing path to ask the player
    for a new allowlist grant. Grants are permanent capability changes, so the
    request fires a floor-1.0 escalation (never director-auto-approved) whose
    approval by the human's email link is what actually applies the grant."""

    def _client(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _common_mocks(self):
        patchers = [
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, '_send_escalation_email_sync'),
            # create_escalation now also pushes on Telegram; pin it off so
            # tests never hit the live bot API (token is in the real .env).
            unittest.mock.patch.object(serve, 'send_player_telegram_sync', return_value=False),
            unittest.mock.patch.object(serve, 'ensure_sandbox_networking'),
            # ESCALATIONS_PATH is a module constant from the REAL THINK_TANK_DIR
            # at import time -- same pitfall as BROWSE_TRAIL_PATH, so any test
            # creating/resolving an escalation must pin it to the temp dir or
            # it writes the live ~/ai-think-tank/escalations.json.
            unittest.mock.patch.object(serve, 'ESCALATIONS_PATH',
                                       os.path.join(_TMP_DIR, 'escalations.json')),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_unauthenticated_request_is_rejected(self):
        from starlette.testclient import TestClient
        r = TestClient(serve.app).post('/api/allowlist/request', json={
            'agentId': 'ben', 'host': 'example.com', 'purpose': 'research'})
        self.assertEqual(r.status_code, 401)

    def test_private_host_is_refused_before_any_request(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False):
            r = self._client().post('/api/allowlist/request', json={
                'agentId': 'ben', 'host': '10.0.0.5', 'purpose': 'internal api'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('never be allowlisted', r.json()['error'])

    def test_already_allowlisted_host_returns_without_an_escalation(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}), \
             unittest.mock.patch.object(serve, 'create_escalation') as esc:
            r = self._client().post('/api/allowlist/request', json={
                'agentId': 'ben', 'host': 'dreyx.com', 'purpose': 'already fine'})
        self.assertTrue(r.json()['allowlisted'])
        esc.assert_not_called()

    def test_url_input_is_normalized_to_hostname(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'create_escalation', return_value='esc-test') as esc:
            r = self._client().post('/api/allowlist/request', json={
                'agentId': 'ben', 'host': 'https://api.example.net/v1/data', 'purpose': 'quotes'})
        body = r.json()
        self.assertEqual(body['host'], 'api.example.net')
        esc.assert_called_once()
        kind, question = esc.call_args[0]
        self.assertEqual(kind, 'allowlist request')
        self.assertEqual(esc.call_args.kwargs['on_approve_note'], 'api.example.net')

    def test_pending_duplicate_returns_same_escalation(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, '_load_escalations', return_value={
            'esc-1': {'kind': 'allowlist request', 'status': 'pending', 'note': 'example.com'},
        }):
            r = self._client().post('/api/allowlist/request', json={
                'agentId': 'ben', 'host': 'example.com', 'purpose': 'again'})
        body = r.json()
        self.assertTrue(body['requested'])
        self.assertEqual(body['escalationId'], 'esc-1')

    def test_resolve_approval_applies_the_grant(self):
        # The approval side-effect is the whole point: an approved 'allowlist
        # request' escalation actually grants the host to the runtime list,
        # which then feeds every gate (browse, colab, egress proxy alike).
        import asyncio
        self._common_mocks()
        _grant_escalation_id = serve.create_escalation(
            'allowlist request', 'grant me', on_approve_note='GrantHost.example')
        esc = serve._load_escalations()[_grant_escalation_id]
        with unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'ensure_sandbox_networking'):
            asyncio.run(serve.resolve_escalation(_grant_escalation_id, esc['token'], 'approve'))
        self.assertTrue(serve._is_allowlisted_host('GrantHost.example'))
        self.assertTrue(serve._is_allowlisted_host('www.granthost.example'))
        # And the grant is durable: persisted in the settings table.
        with serve._db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'allowlist_grants'").fetchone()
        self.assertIn('granthost.example', row[0])
        # The email-link path keeps its DEFAULT attribution even after the
        # helper grew actor/via params for the operator endpoints: 'admin'
        # and 'escalation approval', never a stale endpoint actor.
        log.assert_any_call('admin', 'allowlist_grant',
                            {'host': 'granthost.example', 'via': 'escalation approval'},
                            authorized=True)

    def test_resolve_denial_never_grants(self):
        import asyncio
        self._common_mocks()
        _denied_id = serve.create_escalation(
            'allowlist request', 'deny me', on_approve_note='deniedhost.example')
        esc = serve._load_escalations()[_denied_id]
        with unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'ensure_sandbox_networking'):
            asyncio.run(serve.resolve_escalation(_denied_id, esc['token'], 'deny'))
        self.assertFalse(serve._is_allowlisted_host('deniedhost.example'))

    def test_candidate_approval_grants_runtime_host(self):
        # The 2026-10 fix: an approved 'allowlist candidate' is NOT a no-op.
        # Its note carries the hostname (set by record_browse_success), and the
        # human's approve link applies the same runtime grant 'allowlist
        # request' does -- this was the escalation the player tapped Approve on
        # and got nothing for.
        import asyncio
        self._common_mocks()
        cand_id = serve.create_escalation(
            'allowlist candidate', 'candhost.example is a good candidate',
            on_approve_note='candhost.example')
        esc = serve._load_escalations()[cand_id]
        with unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'ensure_sandbox_networking'):
            asyncio.run(serve.resolve_escalation(cand_id, esc['token'], 'approve'))
        self.assertTrue(serve._is_allowlisted_host('candhost.example'))
        with serve._db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'allowlist_grants'").fetchone()
        self.assertIn('candhost.example', row[0])


class AllowlistOperatorEndpoints(unittest.TestCase):
    """GET/POST /api/allowlist/grant + /api/allowlist/revoke -- the director/
    admin path to add AND remove allowlist entries directly, next to the
    player's email-link escalation. Gated by _operator_gate: a player session
    OR a director/admin agent key; an ordinary agent is refused."""

    def _client(self, session=True, agent_id=None):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        if session:
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        if agent_id:
            # A real agent key: the login middleware needs it to pass, and
            # _resolve_requester returns the KEY'S OWNER as the requester, so
            # the director path is exercised end-to-end (no gate mocking).
            c.headers['X-Agent-Key'] = serve.get_or_create_agent_key(agent_id)
        return c

    def _common_mocks(self):
        patchers = [
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, 'ensure_sandbox_networking'),
            unittest.mock.patch.object(serve, 'log_action'),
        ]
        started = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)
        # The started patchers, so a test can reach an individual mock
        # (e.g. the log_action mock) by index.
        return started

    def _clear_grants(self):
        serve._set_setting(serve.ALLOWLIST_GRANTS_SETTING, '')

    def test_unauthenticated_allowlist_endpoints_are_rejected(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        for path in ('/api/allowlist', '/api/allowlist/grant', '/api/allowlist/revoke'):
            if path.endswith('/grant') or path.endswith('/revoke'):
                r = c.post(path, json={'host': 'example.com', 'purpose': 'x'})
            else:
                r = c.get(path)
            self.assertEqual(r.status_code, 401, path)

    def test_ordinary_agent_key_is_rejected_on_grant_and_revoke(self):
        # A valid agent key that is NOT a director/admin fails _operator_gate.
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='ben'), \
             unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=False):
            for path in ('/api/allowlist/grant', '/api/allowlist/revoke'):
                r = self._client().post(path, json={'host': 'example.com', 'purpose': 'x'})
                self.assertEqual(r.status_code, 401, path)

    def test_player_session_can_grant_and_it_persists(self):
        self._common_mocks()
        self._clear_grants()
        self.addCleanup(self._clear_grants)
        r = self._client().post('/api/allowlist/grant', json={
            'host': 'https://API.Example.COM/v1', 'purpose': 'quotes'})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body['granted'])
        self.assertEqual(body['host'], 'api.example.com')  # normalized
        self.assertTrue(serve._is_allowlisted_host('api.example.com'))
        self.assertTrue(serve._is_allowlisted_host('www.api.example.com'))
        self.assertIn('api.example.com', serve._runtime_allowlist_grants())

    def test_grant_is_idempotent_for_an_already_effective_host(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}):
            r = self._client().post('/api/allowlist/grant', json={
                'host': 'dreyx.com', 'purpose': 'already there'})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(body['granted'])
        self.assertIn('already on the allowlist', body['message'])

    def test_grant_refuses_a_private_host(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False):
            r = self._client().post('/api/allowlist/grant', json={
                'host': '10.0.0.5', 'purpose': 'internal'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('never be allowlisted', r.json()['error'])

    def test_director_agent_key_can_grant_and_revoke(self):
        # The actual director path: a real agent key passes the login
        # middleware, then _operator_gate's role check authorizes the mutation
        # directly (no email escalation). The role check is mocked (the test
        # DB has no seeded roster, same approach the feed-registry tests use).
        self._common_mocks()
        self._clear_grants()
        self.addCleanup(self._clear_grants)
        c = self._client(session=False, agent_id='theo')
        with unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=True):
            r_grant = c.post('/api/allowlist/grant', json={
                'host': 'granted.example', 'purpose': 'director vetted'})
            self.assertEqual(r_grant.status_code, 200)
            self.assertTrue(r_grant.json()['granted'])
            self.assertTrue(serve._is_allowlisted_host('granted.example'))
            r_revoke = c.post('/api/allowlist/revoke', json={
                'host': 'granted.example', 'purpose': 'no longer needed'})
        self.assertEqual(r_revoke.status_code, 200)
        self.assertTrue(r_revoke.json()['revoked'])
        self.assertFalse(serve._is_allowlisted_host('granted.example'))

    def test_revoke_removes_only_runtime_grants_not_env_baseline(self):
        self._common_mocks()
        self._clear_grants()
        self.addCleanup(self._clear_grants)
        # A runtime grant is removable, and the revoke input takes a URL the
        # same way the grant does (normalized to the bare host)...
        serve._set_setting(serve.ALLOWLIST_GRANTS_SETTING, 'runtime.example')
        r = self._client().post('/api/allowlist/revoke', json={
            'host': 'https://Runtime.Example/deep/path', 'purpose': 'changed'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['revoked'])
        self.assertFalse(serve._is_allowlisted_host('runtime.example'))
        # ...but the code-reviewed env baseline is not -- that's a .env edit.
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'baseline.example'}):
            r = self._client().post('/api/allowlist/revoke', json={
                'host': 'baseline.example', 'purpose': 'try'})
            self.assertEqual(r.status_code, 400)
            self.assertIn('.env', r.json()['error'])
            self.assertTrue(serve._is_allowlisted_host('baseline.example'))

    def test_revoke_of_an_absent_host_is_a_noop(self):
        self._common_mocks()
        self._clear_grants()
        self.addCleanup(self._clear_grants)
        r = self._client().post('/api/allowlist/revoke', json={
            'host': 'never-granted.example', 'purpose': 'noop'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['revoked'])
        self.assertIn('nothing to revoke', r.json()['message'])

    def test_get_lists_env_baseline_and_runtime_grants_separately(self):
        self._common_mocks()
        self._clear_grants()
        self.addCleanup(self._clear_grants)
        serve._grant_allowlist('runtime.example', actor='theo', via='operator grant')
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'baseline.example'}):
            r = self._client().get('/api/allowlist')
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertIn('baseline.example', body['envBaseline'])
        self.assertIn('runtime.example', body['runtimeGrants'])
        self.assertIn('baseline.example', body['effective'])
        self.assertIn('runtime.example', body['effective'])

    def test_grant_endpoint_attributes_actor_in_action_log(self):
        # The action log must show WHO granted it and via which path, so a
        # director's direct grant is auditable and distinguishable from the
        # player's email-link escalation. The requester is the real key's
        # owner (theo), resolved by _resolve_requester from the header.
        started = self._common_mocks()
        log = started[2]  # patchers[2] is log_action
        self._clear_grants()
        self.addCleanup(self._clear_grants)
        c = self._client(session=False, agent_id='theo')
        with unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=True):
            c.post('/api/allowlist/grant', json={
                'host': 'audited.example', 'purpose': 'trace me'})
        log.assert_called_with('theo', 'allowlist_grant',
                               {'host': 'audited.example', 'via': 'operator grant'},
                               authorized=True)


