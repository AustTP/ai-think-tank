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


class IsAllowlistedHost(unittest.TestCase):
    """Pure function -- no mocking needed."""

    def test_exact_match(self):
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}):
            self.assertTrue(serve._is_allowlisted_host('dreyx.com'))

    def test_subdomain_matches(self):
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}):
            self.assertTrue(serve._is_allowlisted_host('www.dreyx.com'))

    def test_case_insensitive(self):
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}):
            self.assertTrue(serve._is_allowlisted_host('DreyX.com'))

    def test_unrelated_domain_does_not_match(self):
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}):
            self.assertFalse(serve._is_allowlisted_host('notdreyx.com'))
            self.assertFalse(serve._is_allowlisted_host('evil-dreyx.com.attacker.net'))

    def test_empty_allowlist_matches_nothing(self):
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()):
            self.assertFalse(serve._is_allowlisted_host('dreyx.com'))

    def test_empty_hostname_is_safe(self):
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}):
            self.assertFalse(serve._is_allowlisted_host(None))
            self.assertFalse(serve._is_allowlisted_host(''))


class BrowseEndpointAllowlist(unittest.TestCase):
    """POST /api/browse -- an allowlisted domain must skip the Jev call
    entirely; a non-allowlisted domain must still go through it, unchanged."""

    def _client(self):
        from starlette.testclient import TestClient
        # /api/browse is in AUTH_PROTECTED_PREFIXES -- needs a real session
        # (mirrors every other protected-endpoint test's _client pattern).
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _common_mocks(self):
        patchers = [
            unittest.mock.patch.object(serve, 'BROWSING_ENABLED', True),
            unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'fake-key'),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            # Echo the requested URL back as the final URL (same host) -- the
            # redirect re-gate must not fire for these allowlist/provenance
            # tests, which are about the PRE-fetch decision, not redirects.
            unittest.mock.patch.object(
                serve, '_fetch_page_sync',
                side_effect=lambda url: (url, 'text/html', '<html><body>hi</body></html>', False, None)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_allowlisted_domain_skips_jev_entirely(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev:
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://dreyx.com/tools', 'purpose': 'look around'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        jev.assert_not_called()

    def test_non_allowlisted_domain_still_goes_through_jev(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev, \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)):
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://example.com/', 'purpose': 'look around'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        jev.assert_called_once()

    def test_player_authored_work_context_host_bypasses_jev(self):
        # A host the PLAYER explicitly named in a PLAYER-AUTHORED task skips Jev
        # like the allowlist -- the player vetted it by writing it into the
        # assignment. Requires a SERVER-MINTED work-trust nonce (the executor
        # mints it from the stored task's playerAuthored flag); the legacy
        # workContextTrusted boolean is ignored.
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev:
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://docs.python.org/3/library/urllib.html',
                'purpose': 'research urllib for my spike',
                'workContext': 'Investigate urllib -- see https://docs.python.org/3/library/urllib.html',
                'workTrustNonce': serve._mint_work_trust_nonce()})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        jev.assert_not_called()

    def test_agent_authored_work_context_host_does_NOT_bypass_jev(self):
        # The adversarial case: an agent authors its OWN story and names a URL
        # in it. That text is agent-written, so it must NOT skip Jev -- the
        # story text is a laundering vector, not a player-vetted allowlist.
        # The named host still goes through the full story-aware Jev classify.
        # The forged legacy boolean (workContextTrusted=True with no server
        # nonce) is the Fix B regression test: it must not grant the bypass.
        # Host note: this used to be docs.python.org, but that host is now on
        # the STANDING_SAFE_DOMAINS read-only list, which skips Jev for
        # everyone by design -- a standing host can't also prove the
        # provenance gate, so the laundering test uses a non-standing host.
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev, \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)):
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://dreyx.com/tools',
                'purpose': 'research tools for my spike',
                'workContext': 'Investigate tools -- see https://dreyx.com/tools',
                'workContextTrusted': True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        jev.assert_called_once()
        # The work context is still IN the prompt Jev judges against (on-task
        # sites allowed by the subordinate clause) -- but the host did not skip
        # Jev; the bypass is provenance-gated.
        instructions = jev.call_args.args[2]['choice']['instructions']
        self.assertIn('dreyx.com', instructions)
        self.assertIn('Investigate tools', instructions)

    def test_standing_safe_domain_skips_jev_without_any_grant(self):
        # STANDING_SAFE_DOMAINS (read-only curated tier): a public-reference
        # host skips the Jev round trip with NO allowlist entry and NO work
        # context -- the standing list alone carries it. The full allowlist
        # stays empty so this proves the new tier, not the old one.
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev:
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://en.wikipedia.org/wiki/Simulation',
                'purpose': 'background reading'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        jev.assert_not_called()

    def test_allowlist_never_bypasses_the_ssrf_check(self):
        # Even a listed domain must still fail closed if it resolves to a
        # private/internal address -- the allowlist replaces the CONTENT
        # judgment call, never the network-safety one.
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev:
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://dreyx.com/', 'purpose': 'look around'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        jev.assert_not_called()

    def test_empty_allowlist_leaves_every_domain_on_the_jev_path(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev, \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)):
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://dreyx.com/', 'purpose': 'look around'})
        self.assertEqual(r.status_code, 200)
        jev.assert_called_once()

    def test_confident_jev_allow_on_a_non_allowlisted_domain_records_a_trail_success(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync'), \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)), \
             unittest.mock.patch.object(serve, 'record_browse_success') as record:
            self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://example.com/page', 'purpose': 'look around'})
        record.assert_called_once_with('example.com')

    def test_allowlisted_domain_never_records_a_trail_success(self):
        # Already vetted -- it never reaches the Jev branch, so it has
        # nothing left to prove and must not touch the trail file either.
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}), \
             unittest.mock.patch.object(serve, 'record_browse_success') as record:
            self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://dreyx.com/tools', 'purpose': 'look around'})
        record.assert_not_called()


