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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


class BrowseEndpointRedirectReGate(unittest.TestCase):
    """The short-link laundering vector: Jev approves the ORIGINAL url, then a
    redirect lands the content on a DIFFERENT host Jev never classified. The
    fetch re-checks the final host for NETWORK safety only -- this re-gate
    re-judges it for CONTENT before any content is returned, and fails closed.
    Same-host redirects (echo) and redirects to player-vetted destinations
    (allowlist) skip the extra round trip."""

    def _client(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _common_mocks(self, final_url):
        patchers = [
            unittest.mock.patch.object(serve, 'BROWSING_ENABLED', True),
            unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'fake-key'),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()),
            unittest.mock.patch.object(
                serve, '_fetch_page_sync',
                return_value=(final_url, 'text/html', '<html><body>hi</body></html>', False, None)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_cross_host_redirect_to_a_blocked_category_destination_is_denied(self):
        # The user's worst fear: an innocuous-looking short link Jev approves,
        # whose redirect lands on a blocked-category host. The re-gate must
        # classify the FINAL destination and deny it -- no content reaches the
        # agent even though the ORIGINAL url was allowed.
        self._common_mocks('https://definitely-not-blocked.example/landing')
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev, \
             unittest.mock.patch.object(serve, '_jev_choice', side_effect=[
                 ('allow', 0.95, 0.0),   # original short-link url: allowed
                 ('block', 0.99, 0.0),   # redirect destination: blocked
             ]):
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://t.co/abc123', 'purpose': 'check this link'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertIn('redirected', r.json()['reason'])
        # The re-gate DID judge the final destination against Jev's criteria.
        self.assertEqual(jev.call_count, 2)
        redirect_instructions = jev.call_args_list[1][0][2]['choice']['instructions']
        self.assertIn('definitely-not-blocked.example/landing', redirect_instructions)
        self.assertIn('REDIRECTED', redirect_instructions)

    def test_same_host_redirect_needs_no_second_classification(self):
        # A redirect that stays on the SAME host (http->https, path moves,
        # www. normalizing) is the host Jev already judged -- no extra call.
        # Host is a NON-standing site: a standing safe domain would skip the
        # ORIGINAL classification too (by design), making the "one call only"
        # premise untestable there.
        self._common_mocks('https://www.sample-research.org/guide.html#http-redirections')
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev, \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)):
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://www.sample-research.org/guide.html',
                'purpose': 'read the guide'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        jev.assert_called_once()  # only the original pre-fetch classification

    def test_cross_host_redirect_to_an_allowlisted_final_host_skips_the_extra_call(self):
        # A redirect landing on a host the PLAYER vetted (allowlist) is a
        # destination the player approved directly -- no re-classification.
        # The ORIGINAL short link (t.co) is NOT allowlisted, so it still went
        # through Jev once; only the final destination skips a second call.
        self._common_mocks('https://www.dreyx.com/deep/landing')
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev, \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)), \
             unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}):
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://t.co/abc123', 'purpose': 'follow a link'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        jev.assert_called_once()  # original short link classified once; allowlisted final host skipped

    def test_cross_host_redirect_that_jev_allows_is_returned(self):
        # A legit host-changing redirect (e.g. a shortener for a real docs
        # page) is re-classified and allowed -- one extra call, content flows.
        # Final host is a NON-standing site: a standing safe domain would
        # (by design, STANDING_SAFE_DOMAINS) skip the re-gate's Jev call.
        self._common_mocks('https://www.dreyx.com/3/library/urllib.html')
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev, \
             unittest.mock.patch.object(serve, '_jev_choice', side_effect=[
                 ('allow', 0.95, 0.0),   # original short-link url: allowed
                 ('allow', 0.96, 0.0),   # redirect destination: also allowed
             ]):
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://t.co/abc123', 'purpose': 'follow a link'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        self.assertEqual(jev.call_count, 2)  # original url + re-classified destination

    def test_player_authored_work_context_final_host_still_skips_the_re_gate(self):
        # Provenance semantics carry through to the re-gate: a redirect landing
        # on a host the PLAYER explicitly named in a player-authored task is a
        # destination the player vetted -- no re-classification, matching the
        # original gate's bypass exactly. The ORIGINAL short link (t.co) is
        # not a work-context host, so it still went through Jev once.
        self._common_mocks('https://docs.python.org/3/library/urllib.html#redirections')
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as jev, \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('allow', 0.95, 0.0)):
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://t.co/abc123',
                'purpose': 'read the urllib docs',
                'workContext': 'Investigate urllib -- see https://docs.python.org/3/library/urllib.html',
                'workTrustNonce': serve._mint_work_trust_nonce()})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        # One call (the unvetted short link); the player-vetted final host
        # (docs.python.org, named in the player-authored work) skipped the
        # re-gate.
        jev.assert_called_once()


class BrowseTrailBuilding(unittest.TestCase):
    """record_browse_success: the trail sidecar file, and the one-time
    escalation once a domain crosses BROWSE_ALLOWLIST_CANDIDATE_THRESHOLD."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-browse-trail-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = unittest.mock.patch.multiple(
            serve, BROWSE_TRAIL_PATH=os.path.join(self.tmp, 'browse_trail.json'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_falsy_hostname_is_a_noop(self):
        serve.record_browse_success('')
        serve.record_browse_success(None)
        self.assertEqual(serve._browse_trail_read(), {})

    def test_records_lowercase_and_accumulates_count(self):
        serve.record_browse_success('Example.COM')
        serve.record_browse_success('example.com')
        self.assertEqual(serve._browse_trail_read()['example.com']['count'], 2)

    def test_no_escalation_below_threshold(self):
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            for _ in range(serve.BROWSE_ALLOWLIST_CANDIDATE_THRESHOLD - 1):
                serve.record_browse_success('example.com')
        esc.assert_not_called()

    def test_escalates_exactly_once_when_threshold_is_crossed_and_again_never(self):
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            for _ in range(serve.BROWSE_ALLOWLIST_CANDIDATE_THRESHOLD + 5):
                serve.record_browse_success('example.com')
        esc.assert_called_once()
        self.assertIn('example.com', esc.call_args[0][1])


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


class CandidateDirectorDelegation(unittest.TestCase):
    """'allowlist candidate' is DELEGABLE (not floor 1.0): the director loop
    auto-approving it must APPLY the runtime grant (the 2026-10 fix), with the
    director named in the action log -- not leave a silent no-op approval the
    player never hears about."""

    def test_director_auto_approve_applies_grant_with_attribution(self):
        esc = {'status': 'pending', 'kind': 'allowlist candidate',
               'question': 'CandHost.example has been confidently approved by Jev 5 times',
               'note': 'candhost.example', 'askCount': 0, 'lastAskedAt': 0}
        pending_id = 'esc-cand'
        for name, mock_value in (
            ('_load_escalations', unittest.mock.Mock(return_value={pending_id: esc})),
            ('_save_escalations', unittest.mock.Mock()),
            ('get_state_from_db', unittest.mock.Mock(return_value={
                'agentRoster': [{'id': 'nora', 'name': 'Nora', 'isDirector': True,
                                 'isAdmin': False}]})),
            ('_jev_quorum_choice_sync', unittest.mock.Mock(return_value=('approve', 0.95, 0.0))),
            ('ensure_sandbox_networking', unittest.mock.Mock()),
            ('log_action', unittest.mock.Mock()),
        ):
            patcher = unittest.mock.patch.object(serve, name, mock_value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()):
            serve._resolve_pending_escalations_sync()
            self.assertEqual(esc['status'], 'approved')
            self.assertIn('nora', esc.get('resolvedBy', ''))
            self.assertTrue(serve._is_allowlisted_host('candhost.example'))
        # Attribution: the GRANT itself names the director and the delegation
        # path, so the audit trail distinguishes it from the human's link.
        grant_calls = [c for c in serve.log_action.call_args_list
                       if c.args[:2] == ('nora', 'allowlist_grant')]
        self.assertTrue(grant_calls, 'director-attributed grant must be logged')
        self.assertEqual(grant_calls[0].args[2]['via'], 'director delegation')


class BrowseEndpointMullvadVpn(unittest.TestCase):
    """POST /api/browse with viaVpnCountry -- real subprocess calls are
    mocked throughout (_mullvad_connect_sync/_mullvad_disconnect_sync);
    nothing here ever touches the real Mullvad daemon or the host's real
    network state. Uses the allowlisted-domain path to skip Jev, keeping
    focus purely on the VPN branch."""

    def _client(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _common_mocks(self):
        patchers = [
            unittest.mock.patch.object(serve, 'BROWSING_ENABLED', True),
            unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'fake-key'),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True),
            unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', {'dreyx.com'}),
            unittest.mock.patch.object(
                serve, '_fetch_page_sync',
                side_effect=lambda url: (url, 'text/html', '<html><body>hi</body></html>', False, None)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _post(self, country):
        return self._client().post('/api/browse', json={
            'agentId': 'ben', 'url': 'https://dreyx.com/tools', 'purpose': 'look around',
            'viaVpnCountry': country})

    def test_missing_mullvad_binary_blocks_before_any_connect_attempt(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'MULLVAD_BIN', None), \
             unittest.mock.patch.object(serve, 'MULLVAD_COUNTRY_ALLOWLIST', {'de'}), \
             unittest.mock.patch.object(serve, '_mullvad_connect_sync') as connect:
            r = self._post('de')
        self.assertFalse(r.json()['allowed'])
        connect.assert_not_called()

    def test_country_not_on_allowlist_blocks_before_any_connect_attempt(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'MULLVAD_BIN', '/usr/local/bin/mullvad'), \
             unittest.mock.patch.object(serve, 'MULLVAD_COUNTRY_ALLOWLIST', {'de'}), \
             unittest.mock.patch.object(serve, '_mullvad_connect_sync') as connect:
            r = self._post('jp')
        self.assertFalse(r.json()['allowed'])
        connect.assert_not_called()

    def test_allowlisted_country_connects_fetches_and_disconnects(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'MULLVAD_BIN', '/usr/local/bin/mullvad'), \
             unittest.mock.patch.object(serve, 'MULLVAD_COUNTRY_ALLOWLIST', {'de'}), \
             unittest.mock.patch.object(serve, '_mullvad_connect_sync', return_value=(True, None)) as connect, \
             unittest.mock.patch.object(serve, '_mullvad_disconnect_sync') as disconnect:
            r = self._post('de')
        self.assertTrue(r.json()['allowed'])
        connect.assert_called_once_with('de')
        disconnect.assert_called_once()

    def test_failed_connect_still_disconnects_and_reports_not_fetched(self):
        # A real risk this covers: `mullvad connect` may already have been
        # issued even though we never confirmed Connected -- disconnect
        # must still run so the host isn't left mid-connecting/connected.
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'MULLVAD_BIN', '/usr/local/bin/mullvad'), \
             unittest.mock.patch.object(serve, 'MULLVAD_COUNTRY_ALLOWLIST', {'de'}), \
             unittest.mock.patch.object(serve, '_mullvad_connect_sync', return_value=(False, 'timed out')) as connect, \
             unittest.mock.patch.object(serve, '_mullvad_disconnect_sync') as disconnect, \
             unittest.mock.patch.object(serve, '_fetch_page_sync') as fetch:
            r = self._post('de')
        data = r.json()
        self.assertTrue(data['allowed'])
        self.assertIn('timed out', data['error'])
        connect.assert_called_once_with('de')
        disconnect.assert_called_once()
        fetch.assert_not_called()

    def test_fetch_exception_after_successful_connect_still_disconnects(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, 'MULLVAD_BIN', '/usr/local/bin/mullvad'), \
             unittest.mock.patch.object(serve, 'MULLVAD_COUNTRY_ALLOWLIST', {'de'}), \
             unittest.mock.patch.object(serve, '_mullvad_connect_sync', return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_mullvad_disconnect_sync') as disconnect, \
             unittest.mock.patch.object(serve, '_fetch_page_sync', side_effect=RuntimeError('boom')):
            r = self._post('de')
        self.assertTrue(r.json()['allowed'])
        self.assertIn('boom', r.json()['error'])
        disconnect.assert_called_once()

    def test_no_country_never_touches_mullvad_at_all(self):
        self._common_mocks()
        with unittest.mock.patch.object(serve, '_mullvad_connect_sync') as connect, \
             unittest.mock.patch.object(serve, '_mullvad_disconnect_sync') as disconnect:
            r = self._client().post('/api/browse', json={
                'agentId': 'ben', 'url': 'https://dreyx.com/tools', 'purpose': 'look around'})
        self.assertTrue(r.json()['allowed'])
        connect.assert_not_called()
        disconnect.assert_not_called()


class StandingTierAndDelegation(unittest.TestCase):
    """2026-10-13 agent-feedback changes: STANDING_SAFE_DOMAINS (read-only
    tier that skips Jev but is NOT a full allowlist grant), allowlist requests
    escalated to DIRECTORS (floor dropped to delegable, grant applies on
    director approve), the wait estimate surfaced to agents, and the two
    read-only status tools."""

    def test_standing_host_is_not_in_the_full_allowlist(self):
        # The read-only promise: a standing host skips Jev on browse/GET but
        # must NOT appear in _effective_allowlist_domains -- the sandbox
        # egress proxy and Colab gate read that set and would otherwise gain
        # standing WRITE reachability from a reference-reading tier.
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_runtime_allowlist_grants', return_value=set()):
            self.assertTrue(serve._is_standing_safe_host('en.wikipedia.org'))
            self.assertFalse(serve._is_allowlisted_host('en.wikipedia.org'))
            self.assertNotIn('en.wikipedia.org', serve._effective_allowlist_domains())

    def test_standing_suffix_matching_is_subdomain_only(self):
        # Bare 'gov'/'edu' entries must match real institutional hosts, not
        # lookalike strings: 'evil-gov' or 'x.edu.attacker.com' don't match.
        self.assertTrue(serve._is_standing_safe_host('www.nih.gov'))
        self.assertTrue(serve._is_standing_safe_host('berkeley.edu'))
        self.assertFalse(serve._is_standing_safe_host('evil-gov.example.com'))
        self.assertFalse(serve._is_standing_safe_host('x.edu.attacker.example'))

    def test_director_auto_approve_applies_grant_for_allowlist_request(self):
        # Operator decision 2026-10-13: an agent-filed 'allowlist request' is
        # DELEGABLE now -- the director loop approving it must apply the
        # runtime grant with director attribution, same as 'allowlist
        # candidate', instead of the old floor-1.0 human-only path.
        esc = {'status': 'pending', 'kind': 'allowlist request',
               'question': 'Agent june requests delegationhost.example',
               'note': 'delegationhost.example', 'askCount': 0, 'lastAskedAt': 0}
        pending_id = 'esc-alw'
        for name, mock_value in (
            ('_load_escalations', unittest.mock.Mock(return_value={pending_id: esc})),
            ('_save_escalations', unittest.mock.Mock()),
            ('get_state_from_db', unittest.mock.Mock(return_value={
                'agentRoster': [{'id': 'nora', 'name': 'Nora', 'isDirector': True,
                                 'isAdmin': False}]})),
            ('_jev_quorum_choice_sync', unittest.mock.Mock(return_value=('approve', 0.95, 0.0))),
            ('ensure_sandbox_networking', unittest.mock.Mock()),
            ('log_action', unittest.mock.Mock()),
        ):
            patcher = unittest.mock.patch.object(serve, name, mock_value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()):
            serve._resolve_pending_escalations_sync()
            self.assertEqual(esc['status'], 'approved')
            self.assertIn('nora', esc.get('resolvedBy', ''))
            self.assertTrue(serve._is_allowlisted_host('delegationhost.example'))
        grant_calls = [c for c in serve.log_action.call_args_list
                       if c.args[:2] == ('nora', 'allowlist_grant')]
        self.assertTrue(grant_calls, 'director-attributed grant must be logged')
        self.assertEqual(grant_calls[0].args[2]['via'], 'director delegation')

    def test_wait_estimate_reports_median_from_real_pairs(self):
        with unittest.mock.patch.object(serve, '_allowlist_wait_samples',
                                        return_value=[120.0, 300.0, 600.0]):
            self.assertIn('about 5 minutes', serve._allowlist_wait_estimate())
        with unittest.mock.patch.object(serve, '_allowlist_wait_samples',
                                        return_value=[40.0]):
            self.assertIn('under a minute', serve._allowlist_wait_estimate())

    def test_wait_estimate_degrades_to_honest_no_history(self):
        with unittest.mock.patch.object(serve, '_allowlist_wait_samples', return_value=[]):
            self.assertIn('no approval history', serve._allowlist_wait_estimate())

    def test_wait_samples_pair_requests_to_grants(self):
        # Two requests, one resolved grant: exactly one sample, sized
        # request->grant. Unpaired rows (grant without request) are ignored.
        now = time.time()
        rows = [
            ('allowlist_request', now - 1000, json.dumps({'host': 'a.example'})),
            ('allowlist_grant', now - 400, json.dumps({'host': 'a.example', 'via': 'x'})),
            ('allowlist_request', now - 900, json.dumps({'host': 'b.example'})),
            ('allowlist_grant', now - 800, json.dumps({'host': 'other.example'})),
        ]

        class _Conn:
            def execute(self, _sql, _args):
                class _R:
                    def fetchall(self_inner):
                        return rows
                return _R()

        class _Ctx:
            def __enter__(self):
                return _Conn()

            def __exit__(self, *a):
                return False

        with unittest.mock.patch.object(serve, '_db', lambda: _Ctx()):
            samples = serve._allowlist_wait_samples()
        self.assertEqual(samples, [600.0])

    def test_allowlist_status_text_reports_tiers_pending_and_estimate(self):
        esc = {'status': 'pending', 'kind': 'allowlist request',
               'note': 'pendinghost.example', 'ts': time.time() - 600,
               'askCount': 2}
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_runtime_allowlist_grants',
                                        return_value={'granted.example'}), \
             unittest.mock.patch.object(serve, '_load_escalations',
                                        return_value={'esc-1': esc}), \
             unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('no db')), \
             unittest.mock.patch.object(serve, '_allowlist_wait_samples', return_value=[]):
            out = serve._allowlist_status_text('en.wikipedia.org')
            out2 = serve._allowlist_status_text('granted.example')
        self.assertIn('standing safe domain', out)
        self.assertIn('pendinghost.example', out)
        self.assertIn('director pass 2', out)
        self.assertIn('no approval history', out)
        self.assertIn('ALLOWLISTED (runtime grant)', out2)

    def test_team_status_text_shows_queue_and_working(self):
        fake_state = {
            'agents': {'nora': {'task': 'task-1'}, 'ben': {}},
            'tasks': {'task-1': {'title': 'Write the verdict section',
                                 'status': 'working', 'workUntil': time.time() + 300},
                      'task-2': {'title': 'A failed card', 'status': 'failed'}},
            'workQueue': [{'taskType': 'spike', 'title': 'Survey the fleet'}],
        }
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=fake_state), \
             unittest.mock.patch.object(serve, '_load_escalations', return_value={}):
            out = serve._team_status_text()
        self.assertIn('nora: Write the verdict section [working]', out)
        self.assertIn('[spike] Survey the fleet', out)
        self.assertIn('failed=1', out)
        self.assertIn('NEEDS ATTENTION', out)


class MullvadHelpers(unittest.TestCase):
    """_mullvad_status_sync/_mullvad_connect_sync/_mullvad_disconnect_sync --
    real subprocess.run calls are mocked; nothing here ever invokes the real
    mullvad binary or touches the host's actual network state."""

    def test_status_true_only_on_a_real_connected_prefix(self):
        with unittest.mock.patch.object(
                serve.subprocess, 'run',
                return_value=unittest.mock.Mock(returncode=0, stdout='Connected\n  Relay: de-fra-wg-001\n')):
            self.assertTrue(serve._mullvad_status_sync())

    def test_status_false_when_disconnected(self):
        with unittest.mock.patch.object(
                serve.subprocess, 'run',
                return_value=unittest.mock.Mock(returncode=0, stdout='Disconnected\n')):
            self.assertFalse(serve._mullvad_status_sync())

    def test_status_false_on_subprocess_error(self):
        with unittest.mock.patch.object(serve.subprocess, 'run', side_effect=OSError('no such binary')):
            self.assertFalse(serve._mullvad_status_sync())

    def test_connect_returns_ok_once_status_confirms_connected(self):
        with unittest.mock.patch.object(serve, '_mullvad_ensure_logged_in_sync', return_value=(True, None)), \
             unittest.mock.patch.object(serve.subprocess, 'run', return_value=unittest.mock.Mock(returncode=0)), \
             unittest.mock.patch.object(serve, '_mullvad_status_sync', return_value=True):
            ok, error = serve._mullvad_connect_sync('de')
        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_connect_times_out_if_status_never_confirms(self):
        with unittest.mock.patch.object(serve, '_mullvad_ensure_logged_in_sync', return_value=(True, None)), \
             unittest.mock.patch.object(serve.subprocess, 'run', return_value=unittest.mock.Mock(returncode=0)), \
             unittest.mock.patch.object(serve, '_mullvad_status_sync', return_value=False), \
             unittest.mock.patch.object(serve, 'MULLVAD_CONNECT_TIMEOUT_S', 0), \
             unittest.mock.patch.object(serve, 'MULLVAD_STATUS_POLL_S', 0):
            ok, error = serve._mullvad_connect_sync('de')
        self.assertFalse(ok)
        self.assertIn('did not reach Connected', error)

    def test_connect_command_failure_returns_error_without_polling(self):
        with unittest.mock.patch.object(serve, '_mullvad_ensure_logged_in_sync', return_value=(True, None)), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=OSError('no such binary')), \
             unittest.mock.patch.object(serve, '_mullvad_status_sync') as status:
            ok, error = serve._mullvad_connect_sync('de')
        self.assertFalse(ok)
        self.assertIn('mullvad connect failed', error)
        status.assert_not_called()

    def test_connect_fails_fast_when_not_logged_in_and_never_attempts_relay_or_connect(self):
        with unittest.mock.patch.object(serve, '_mullvad_ensure_logged_in_sync', return_value=(False, 'not logged in')), \
             unittest.mock.patch.object(serve.subprocess, 'run') as run:
            ok, error = serve._mullvad_connect_sync('de')
        self.assertFalse(ok)
        self.assertEqual(error, 'not logged in')
        run.assert_not_called()

    def test_disconnect_swallows_errors(self):
        with unittest.mock.patch.object(serve.subprocess, 'run', side_effect=OSError('no such binary')):
            serve._mullvad_disconnect_sync()  # must not raise


class MullvadEnsureLoggedIn(unittest.TestCase):
    """_mullvad_ensure_logged_in_sync -- real subprocess.run calls are
    mocked throughout with a FAKE account number; nothing here ever
    invokes the real mullvad binary or logs this host into or out of any
    real account."""

    _FAKE_ACCOUNT = '0000000000000000'

    def test_no_account_number_configured_fails_without_touching_the_cli(self):
        with unittest.mock.patch.object(serve, 'MULLVAD_ACCOUNT_NUMBER', None), \
             unittest.mock.patch.object(serve.subprocess, 'run') as run:
            ok, error = serve._mullvad_ensure_logged_in_sync()
        self.assertFalse(ok)
        self.assertIn('not set', error)
        run.assert_not_called()

    def test_already_logged_into_the_right_account_skips_login(self):
        with unittest.mock.patch.object(serve, 'MULLVAD_ACCOUNT_NUMBER', self._FAKE_ACCOUNT), \
             unittest.mock.patch.object(
                 serve.subprocess, 'run',
                 return_value=unittest.mock.Mock(returncode=0, stdout=f'Mullvad account:    {self._FAKE_ACCOUNT}\n')) as run:
            ok, error = serve._mullvad_ensure_logged_in_sync()
        self.assertTrue(ok)
        self.assertIsNone(error)
        run.assert_called_once()  # only the `account get` check -- no login attempted

    def test_logged_into_a_different_account_triggers_a_real_login_call(self):
        get_result = unittest.mock.Mock(returncode=0, stdout='Mullvad account:    1111111111111111\n')
        login_result = unittest.mock.Mock(returncode=0)
        with unittest.mock.patch.object(serve, 'MULLVAD_ACCOUNT_NUMBER', self._FAKE_ACCOUNT), \
             unittest.mock.patch.object(serve, 'MULLVAD_BIN', '/usr/local/bin/mullvad'), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=[get_result, login_result]) as run:
            ok, error = serve._mullvad_ensure_logged_in_sync()
        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1][0][0],
                          ['/usr/local/bin/mullvad', 'account', 'login', self._FAKE_ACCOUNT])

    def test_not_logged_in_at_all_triggers_a_real_login_call(self):
        get_result = unittest.mock.Mock(returncode=1, stdout='')
        login_result = unittest.mock.Mock(returncode=0)
        with unittest.mock.patch.object(serve, 'MULLVAD_ACCOUNT_NUMBER', self._FAKE_ACCOUNT), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=[get_result, login_result]):
            ok, error = serve._mullvad_ensure_logged_in_sync()
        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_login_command_failure_is_reported(self):
        get_result = unittest.mock.Mock(returncode=1, stdout='')
        with unittest.mock.patch.object(serve, 'MULLVAD_ACCOUNT_NUMBER', self._FAKE_ACCOUNT), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=[get_result, OSError('boom')]):
            ok, error = serve._mullvad_ensure_logged_in_sync()
        self.assertFalse(ok)
        self.assertIn('mullvad account login failed', error)

    def test_account_get_exception_is_reported_without_attempting_login(self):
        with unittest.mock.patch.object(serve, 'MULLVAD_ACCOUNT_NUMBER', self._FAKE_ACCOUNT), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=OSError('no such binary')) as run:
            ok, error = serve._mullvad_ensure_logged_in_sync()
        self.assertFalse(ok)
        self.assertIn('mullvad account get failed', error)
        run.assert_called_once()  # never got to a second (login) call


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


class AllowlistToolExecutor(unittest.TestCase):
    """The grant_allowlist/revoke_allowlist AGENT TOOLS in the shared web-tools
    executor (_make_web_tools_executor). The executor only FORWARDS with the
    caller's own key; the endpoint's _operator_gate decides authority. These
    tests pin the forward shape (path/body/key), the success message, and that
    a server refusal is surfaced to the model verbatim -- never swallowed."""

    def _drive(self, tool, args, http_result):
        seen = {}

        def fake_http(method, base, path, body=None, header=None, timeout=30):
            seen.update(method=method, path=path, body=body, header=header)
            return http_result

        with unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http):
            execute_tool = serve._make_web_tools_executor('theo', 'theo-key-1')
            out = execute_tool(tool, args)
        return out, seen

    def test_grant_forwards_to_the_grant_endpoint_with_the_callers_key(self):
        out, seen = self._drive('grant_allowlist',
                                {'host': 'X.Example', 'purpose': 'quotes'},
                                {'ok': True, 'host': 'x.example', 'granted': True,
                                 'message': 'x.example is now on the allowlist.'})
        self.assertEqual(seen['method'], 'POST')
        self.assertEqual(seen['path'], '/api/allowlist/grant')
        self.assertEqual(seen['body'], {'agentId': 'theo', 'host': 'X.Example',
                                        'purpose': 'quotes'})
        # The caller's OWN key rides the forward -- the endpoint authenticates
        # the real agent, never a bare server-side loopback.
        self.assertEqual(seen['header'], 'theo-key-1')
        self.assertIn('x.example is now on the allowlist.', out)
        self.assertIn('granted', out)

    def test_revoke_forwards_to_the_revoke_endpoint(self):
        out, seen = self._drive('revoke_allowlist',
                                {'host': 'x.example', 'purpose': 'untrusted now'},
                                {'ok': True, 'host': 'x.example', 'revoked': True,
                                 'message': 'x.example removed from the allowlist.'})
        self.assertEqual(seen['path'], '/api/allowlist/revoke')
        self.assertEqual(seen['body']['host'], 'x.example')
        self.assertIn('x.example removed from the allowlist.', out)
        self.assertIn('revoked', out)

    def test_server_refusal_is_surfaced_to_the_model(self):
        # An ordinary agent gets 401 from the endpoint -- that refusal must
        # REACH the model (the tool description promises the server refuses),
        # not vanish into a cheerful success string.
        out, _seen = self._drive('grant_allowlist', {'host': 'x.example', 'purpose': 'p'},
                                 {'error': 'Unauthorized'})
        self.assertIn('Could not grant the allowlist host: Unauthorized', out)

    def test_non_dict_response_is_reported_not_crashed(self):
        out, _seen = self._drive('revoke_allowlist', {'host': 'x.example', 'purpose': 'p'},
                                 None)
        self.assertIn('unexpected response', out)


if __name__ == '__main__':
    unittest.main()
