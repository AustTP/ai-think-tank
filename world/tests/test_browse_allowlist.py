"""Player-vetted browse allowlist (2026-09-26): a small set of domains that
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
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='village-browse-allowlist-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        VILLAGE_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        # BROWSE_TRAIL_PATH is a module-level constant computed once from the
        # REAL VILLAGE_DIR at import time -- patching VILLAGE_DIR alone does
        # NOT recompute it, so any test exercising a confident Jev allow
        # without explicitly mocking record_browse_success would otherwise
        # write a real browse_trail.json into the actual project directory
        # (caught live running this exact suite).
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
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
            unittest.mock.patch.object(
                serve, '_fetch_page_sync',
                return_value=('https://dreyx.com/', 'text/html', '<html><body>hi</body></html>', False, None)),
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


class BrowseTrailBuilding(unittest.TestCase):
    """record_browse_success: the trail sidecar file, and the one-time
    escalation once a domain crosses BROWSE_ALLOWLIST_CANDIDATE_THRESHOLD."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='village-browse-trail-')
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
                return_value=('https://dreyx.com/', 'text/html', '<html><body>hi</body></html>', False, None)),
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


if __name__ == '__main__':
    unittest.main()
