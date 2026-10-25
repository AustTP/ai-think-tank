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


