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


