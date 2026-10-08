"""Line-coverage gap tests for serve.py, cluster B.

Covers: page-budget ledger read/write/exhausted, page-request accrual,
high-tier spend accounting, apify spend accounting + _apify_call, allowlist
grants, _mullvad_connect_sync, browse-trail read, the email senders
(_send_player_email_sync / _send_escalation_email_sync) + telegram/player-email
provisioning, the docker helpers + sandbox networking, fetch/urlopen resilience
helpers, the openrouter sync callers, weather geocode/fetch, tavily search,
the decision sync caller, and _get_setting.

Hermetic: every network / subprocess / docker / SMTP / playwright / model
seam is mocked. DB writes go to a throwaway temp dir (see setUpModule).
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-serve-gap-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _fake_run_result(returncode=0, stdout='', stderr=''):
    m = unittest.mock.Mock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _fake_urlopen_resp(body_bytes):
    resp = unittest.mock.MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = body_bytes
    return resp


class PageBudgetLedger(unittest.TestCase):
    def test_read_returns_empty_when_no_row(self):
        self.assertEqual(serve._page_budget_ledger_read(), {})

    def test_read_returns_saved_ledger(self):
        serve._page_budget_ledger_write({'2026-10': {'used': 3}})
        self.assertEqual(serve._page_budget_ledger_read(), {'2026-10': {'used': 3}})

    def test_read_corrupt_row_returns_empty(self):
        serve._page_budget_ledger_read()  # ensure the kv_pagebudget table exists
        with serve._db() as conn:
            conn.execute('INSERT INTO kv_pagebudget (id, blob, updated_at) VALUES (1, ?, ?)',
                         ('not-json{{{', time.time()))
        self.assertEqual(serve._page_budget_ledger_read(), {})

    def test_write_survives_db_failure(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db down')):
            serve._page_budget_ledger_write({'x': 1})  # must not raise


class PageBudgetExhausted(unittest.TestCase):
    def test_disabled_budget_never_exhausted(self):
        with unittest.mock.patch.object(serve, 'PAGE_REQUEST_MONTHLY_BUDGET', 0):
            self.assertIs(serve._page_budget_exhausted(), False)

    def test_exhausted_when_used_meets_budget(self):
        with unittest.mock.patch.object(serve, 'PAGE_REQUEST_MONTHLY_BUDGET', 3), \
             unittest.mock.patch.object(serve, '_page_budget_used', return_value=3):
            self.assertIs(serve._page_budget_exhausted(), True)


class AccruePageRequest(unittest.TestCase):
    def test_returns_false_when_budget_exhausted(self):
        with unittest.mock.patch.object(serve, '_page_budget_exhausted', return_value=True):
            self.assertIs(serve._accrue_page_request(), False)

    def test_records_one_request_against_this_month(self):
        written = {}

        def fake_write(ledger):
            written['ledger'] = ledger

        with unittest.mock.patch.object(serve, 'PAGE_REQUEST_MONTHLY_BUDGET', 100), \
             unittest.mock.patch.object(serve, '_page_budget_exhausted', return_value=False), \
             unittest.mock.patch.object(serve, '_page_budget_ledger_read', return_value={}), \
             unittest.mock.patch.object(serve, '_page_budget_ledger_write', side_effect=fake_write):
            self.assertIs(serve._accrue_page_request(), True)
        month = serve._page_budget_month()
        self.assertEqual(written['ledger'][serve.PAGE_REQUEST_LEDGER_KEY], month)
        self.assertEqual(written['ledger'][month]['used'], 1)
        self.assertIn(serve.PAGE_REQUEST_BUDGET_START_KEY, written['ledger'])

    def test_ledger_write_failure_is_swallowed(self):
        with unittest.mock.patch.object(serve, '_page_budget_exhausted', return_value=False), \
             unittest.mock.patch.object(serve, '_page_budget_ledger_write',
                                        side_effect=RuntimeError('write down')):
            self.assertIs(serve._accrue_page_request(), True)


class HighTierSpend(unittest.TestCase):
    def test_spend_this_month_reads_current_month_bucket(self):
        month = serve._high_tier_budget_month()
        ledger = {serve.HIGH_TIER_LEDGER_KEY: {'byMonth': {month: 2.5}}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            self.assertEqual(serve._high_tier_spend_this_month(), 2.5)

    def test_spend_this_month_falls_back_to_zero_on_error(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        side_effect=RuntimeError('down')):
            self.assertEqual(serve._high_tier_spend_this_month(), 0.0)

    def test_budget_not_exceeded_when_disabled(self):
        with unittest.mock.patch.object(serve, 'HIGH_TIER_MONTHLY_BUDGET_USD', 0):
            self.assertIs(serve._high_tier_budget_exceeded(), False)

    def test_budget_exceeded_after_cap(self):
        with unittest.mock.patch.object(serve, 'HIGH_TIER_MONTHLY_BUDGET_USD', 1.0), \
             unittest.mock.patch.object(serve, '_high_tier_spend_this_month', return_value=1.0):
            self.assertIs(serve._high_tier_budget_exceeded(), True)


class AccrueHighTierSpend(unittest.TestCase):
    def test_ignores_non_numeric_cost(self):
        holder = {}

        def fake_read():
            return holder

        def fake_write(ledger):
            holder.clear()
            holder.update(ledger)

        with unittest.mock.patch.object(serve, '_spend_ledger_read', side_effect=fake_read), \
             unittest.mock.patch.object(serve, '_spend_ledger_write', side_effect=fake_write):
            serve._accrue_high_tier_spend(None)
            serve._accrue_high_tier_spend('oops')
            serve._accrue_high_tier_spend(0)
        self.assertEqual(holder, {})

    def test_accrues_into_reserved_bucket(self):
        serve._accrue_high_tier_spend(1.25)
        ledger = serve._spend_ledger_read()
        bucket = ledger[serve.HIGH_TIER_LEDGER_KEY]
        self.assertEqual(bucket['used'], 1.25)
        self.assertEqual(bucket['calls'], 1)
        month = serve._high_tier_budget_month()
        self.assertEqual(bucket['byMonth'][month], 1.25)

    def test_ledger_write_failure_is_swallowed(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_write',
                                        side_effect=RuntimeError('down')):
            serve._accrue_high_tier_spend(0.5)  # must not raise


class ApifySpend(unittest.TestCase):
    def test_spend_this_month_reads_current_month_bucket(self):
        month = serve._apify_budget_month()
        ledger = {serve.APIFY_LEDGER_KEY: {'byMonth': {month: 3.0}}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            self.assertEqual(serve._apify_spend_this_month(), 3.0)

    def test_spend_this_month_falls_back_to_zero_on_error(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        side_effect=RuntimeError('down')):
            self.assertEqual(serve._apify_spend_this_month(), 0.0)


class AccrueApifySpend(unittest.TestCase):
    def test_ignores_non_numeric_cost(self):
        holder = {}

        def fake_read():
            return holder

        def fake_write(ledger):
            holder.clear()
            holder.update(ledger)

        with unittest.mock.patch.object(serve, '_spend_ledger_read', side_effect=fake_read), \
             unittest.mock.patch.object(serve, '_spend_ledger_write', side_effect=fake_write):
            serve._accrue_apify_spend(None)
            serve._accrue_apify_spend('oops')
        self.assertEqual(holder, {})

    def test_accrues_into_apify_bucket(self):
        serve._accrue_apify_spend(0.5)
        ledger = serve._spend_ledger_read()
        bucket = ledger[serve.APIFY_LEDGER_KEY]
        self.assertEqual(bucket['used'], 0.5)
        self.assertEqual(bucket['calls'], 1)
        month = serve._apify_budget_month()
        self.assertEqual(bucket['byMonth'][month], 0.5)

    def test_ledger_write_failure_is_swallowed(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_write',
                                        side_effect=RuntimeError('down')):
            serve._accrue_apify_spend(0.5)  # must not raise


class ApifyCall(unittest.TestCase):
    def test_no_api_key_fails_closed(self):
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', ''):
            data, err = serve._apify_call('/users/me')
        self.assertIsNone(data)
        self.assertIn('not configured', err)

    def test_get_returns_parsed_json(self):
        resp = _fake_urlopen_resp(b'{"data": {"plan": "Free"}}')
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'key'), \
             unittest.mock.patch('urllib.request.urlopen', return_value=resp):
            data, err = serve._apify_call('/users/me')
        self.assertIsNone(err)
        self.assertEqual(data, {'data': {'plan': 'Free'}})

    def test_post_sends_body_and_query(self):
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured['req'] = req
            return _fake_urlopen_resp(b'{"ok": true}')

        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'key'), \
             unittest.mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            data, err = serve._apify_call('/runs', method='POST',
                                          body={'input': {'url': 'x'}},
                                          query={'timeout': 60})
        self.assertIsNone(err)
        self.assertEqual(data, {'ok': True})
        req = captured['req']
        self.assertEqual(req.get_method(), 'POST')
        self.assertIn('?timeout=60', req.full_url)
        self.assertEqual(json.loads(req.data), {'input': {'url': 'x'}})
        self.assertEqual({k.lower(): v for k, v in req.headers.items()},
                         {'authorization': 'Bearer key', 'content-type': 'application/json'})

    def test_non_json_response_falls_back_to_raw(self):
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'key'), \
             unittest.mock.patch('urllib.request.urlopen',
                                 return_value=_fake_urlopen_resp(b'not json at all')):
            data, err = serve._apify_call('/users/me')
        self.assertIsNone(err)
        self.assertEqual(data['_raw'], 'not json at all')

    def test_http_error_returns_detail(self):
        err = urllib.error.HTTPError('https://api.apify.com/v2/x', 403, 'Forbidden', {},
                                     io.BytesIO(b'{"error": "nope"}'))
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'key'), \
             unittest.mock.patch('urllib.request.urlopen', side_effect=err):
            data, err = serve._apify_call('/users/me')
        self.assertIsNone(data)
        self.assertIn('403', err)
        self.assertIn('nope', err)

    def test_generic_error_returns_message(self):
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'key'), \
             unittest.mock.patch('urllib.request.urlopen', side_effect=ValueError('boom')):
            data, err = serve._apify_call('/users/me')
        self.assertIsNone(data)
        self.assertIn('boom', err)


class GrantAllowlist(unittest.TestCase):
    def test_empty_host_returns_none(self):
        self.assertIsNone(serve._grant_allowlist(''))
        self.assertIsNone(serve._grant_allowlist(None))
        self.assertIsNone(serve._grant_allowlist('   '))

    def test_grant_persists_and_refreshes_proxy(self):
        with unittest.mock.patch.object(serve, 'ensure_sandbox_networking') as ensure:
            result = serve._grant_allowlist('Example.COM')
        self.assertEqual(result, 'example.com')
        ensure.assert_called_once()
        self.assertIn('example.com', serve._runtime_allowlist_grants())

    def test_proxy_refresh_failure_does_not_veto_grant(self):
        with unittest.mock.patch.object(serve, 'ensure_sandbox_networking',
                                        side_effect=RuntimeError('docker down')), \
             unittest.mock.patch('builtins.print') as pr:
            result = serve._grant_allowlist('api.example.com')
        self.assertEqual(result, 'api.example.com')
        pr.assert_called()
        self.assertIn('api.example.com', serve._runtime_allowlist_grants())


class MullvadConnectSync(unittest.TestCase):
    def test_success_returns_ok(self):
        with unittest.mock.patch.object(serve, '_mullvad_ensure_logged_in_sync',
                                        return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_mullvad_status_sync', return_value=True), \
             unittest.mock.patch.object(serve.subprocess, 'run', return_value=_fake_run_result()):
            ok, err = serve._mullvad_connect_sync('us')
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_polls_until_connected_timeout(self):
        with unittest.mock.patch.object(serve, '_mullvad_ensure_logged_in_sync',
                                        return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_mullvad_status_sync', return_value=False), \
             unittest.mock.patch.object(serve.subprocess, 'run', return_value=_fake_run_result()), \
             unittest.mock.patch.object(serve, 'MULLVAD_CONNECT_TIMEOUT_S', 0.05), \
             unittest.mock.patch.object(serve, 'MULLVAD_STATUS_POLL_S', 0), \
             unittest.mock.patch.object(serve.time, 'sleep'):
            ok, err = serve._mullvad_connect_sync('us')
        self.assertFalse(ok)
        self.assertIn('did not reach Connected', err)

    def test_returns_ensure_login_error(self):
        with unittest.mock.patch.object(serve, '_mullvad_ensure_logged_in_sync',
                                        return_value=(False, 'not logged in')):
            ok, err = serve._mullvad_connect_sync('us')
        self.assertFalse(ok)
        self.assertEqual(err, 'not logged in')


class BrowseTrailRead(unittest.TestCase):
    def setUp(self):
        self.trail_path = os.path.join(serve.THINK_TANK_DIR, 'browse_trail.json')
        self._p = unittest.mock.patch.object(serve, 'BROWSE_TRAIL_PATH', self.trail_path)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_missing_file_returns_empty(self):
        self.assertEqual(serve._browse_trail_read(), {})

    def test_returns_saved_trail(self):
        with open(self.trail_path, 'w') as f:
            json.dump({'example.com': {'count': 3, 'notifiedAt': None}}, f)
        self.assertEqual(serve._browse_trail_read(),
                         {'example.com': {'count': 3, 'notifiedAt': None}})

    def test_corrupt_file_returns_empty(self):
        with open(self.trail_path, 'w') as f:
            f.write('{not json')
        self.assertEqual(serve._browse_trail_read(), {})


def _fake_smtp():
    smtp = unittest.mock.MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.__exit__.return_value = False
    return smtp


class SendEscalationEmailSync(unittest.TestCase):
    def _cfg(self, enabled=True):
        return unittest.mock.patch.multiple(
            serve,
            ESCALATION_EMAIL_TO='admin@example.com' if enabled else '',
            SMTP_HOST='smtp.example.com' if enabled else '',
            SMTP_USER='user@example.com' if enabled else '',
            SMTP_PASSWORD='pw' if enabled else '',
        )

    def test_not_configured_fails_closed(self):
        with self._cfg(enabled=False), unittest.mock.patch('builtins.print') as pr:
            self.assertIs(serve._send_escalation_email_sync('subj', 'body'), False)
        pr.assert_called()

    def test_sends_via_smtp(self):
        smtp = _fake_smtp()
        with self._cfg(), unittest.mock.patch('smtplib.SMTP', return_value=smtp):
            self.assertIs(serve._send_escalation_email_sync('subj', 'body'), True)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once()
        smtp.send_message.assert_called_once()

    def test_smtp_failure_fails_closed(self):
        with self._cfg(), \
             unittest.mock.patch('smtplib.SMTP', side_effect=RuntimeError('smtp down')), \
             unittest.mock.patch('builtins.print'):
            self.assertIs(serve._send_escalation_email_sync('subj', 'body'), False)


class SendPlayerEmailSync(unittest.TestCase):
    def test_disabled_returns_false(self):
        with unittest.mock.patch.object(serve, 'PLAYER_EMAIL_ENABLED', False):
            self.assertIs(serve._send_player_email_sync('subj', 'body'), False)

    def test_credential_lookup_error_fails_closed(self):
        with unittest.mock.patch.object(serve, 'PLAYER_EMAIL_ENABLED', True), \
             unittest.mock.patch.object(serve, '_credential_token',
                                        side_effect=RuntimeError('db down')), \
             unittest.mock.patch('builtins.print'):
            self.assertIs(serve._send_player_email_sync('subj', 'body'), False)

    def test_no_credential_fails_closed(self):
        with unittest.mock.patch.object(serve, 'PLAYER_EMAIL_ENABLED', True), \
             unittest.mock.patch.object(serve, '_credential_token', return_value=None), \
             unittest.mock.patch('builtins.print'):
            self.assertIs(serve._send_player_email_sync('subj', 'body'), False)

    def test_undecryptable_credential_fails_closed(self):
        with unittest.mock.patch.object(serve, 'PLAYER_EMAIL_ENABLED', True), \
             unittest.mock.patch.object(serve, '_credential_token', return_value='enc'), \
             unittest.mock.patch.object(serve, '_open_secret', return_value=None), \
             unittest.mock.patch('builtins.print'):
            self.assertIs(serve._send_player_email_sync('subj', 'body'), False)

    def test_sends_via_smtp(self):
        smtp = _fake_smtp()
        with unittest.mock.patch.object(serve, 'PLAYER_EMAIL_ENABLED', True), \
             unittest.mock.patch.object(serve, '_credential_token', return_value='enc'), \
             unittest.mock.patch.object(serve, '_open_secret', return_value='abcd efgh ijkl mnop'), \
             unittest.mock.patch('smtplib.SMTP', return_value=smtp):
            self.assertIs(serve._send_player_email_sync('subj', 'body'), True)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with(serve.GMAIL_SMTP, 'abcd efgh ijkl mnop')
        smtp.send_message.assert_called_once()

    def test_smtp_failure_fails_closed(self):
        with unittest.mock.patch.object(serve, 'PLAYER_EMAIL_ENABLED', True), \
             unittest.mock.patch.object(serve, '_credential_token', return_value='enc'), \
             unittest.mock.patch.object(serve, '_open_secret', return_value='abcd efgh ijkl mnop'), \
             unittest.mock.patch('smtplib.SMTP', side_effect=RuntimeError('smtp down')), \
             unittest.mock.patch('builtins.print'):
            self.assertIs(serve._send_player_email_sync('subj', 'body'), False)


class SendPlayerTelegramSync(unittest.TestCase):
    def test_not_configured_returns_false(self):
        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', ''):
            self.assertIs(serve.send_player_telegram_sync('subj', 'body'), False)

    def test_sends_to_each_allowed_chat(self):
        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'1', '2'}), \
             unittest.mock.patch.object(serve, '_telegram_api_sync', return_value={'ok': True}):
            self.assertIs(serve.send_player_telegram_sync('subj', 'body'), True)

    def test_api_failure_prints_and_returns_false(self):
        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'1'}), \
             unittest.mock.patch.object(serve, '_telegram_api_sync',
                                        side_effect=RuntimeError('telegram down')), \
             unittest.mock.patch('builtins.print') as pr:
            self.assertIs(serve.send_player_telegram_sync('subj', 'body'), False)
        pr.assert_called()

    def test_partial_failure_still_reports_ok(self):
        def fake_telegram(method, params=None, timeout=30):
            if params['chat_id'] == '1':
                return {'ok': True}
            raise RuntimeError('down')

        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'1', '2'}), \
             unittest.mock.patch.object(serve, '_telegram_api_sync', side_effect=fake_telegram), \
             unittest.mock.patch('builtins.print'):
            self.assertIs(serve.send_player_telegram_sync('subj', 'body'), True)


class ProvisionPlayerEmail(unittest.TestCase):
    def test_invalid_password_rejected(self):
        res = serve.provision_player_email('short')
        self.assertFalse(res['ok'])
        self.assertIn('error', res)

    def test_stores_credential_and_fires_self_test(self):
        with unittest.mock.patch.object(serve, '_store_credential') as store, \
             unittest.mock.patch.object(serve, '_send_player_email_sync', return_value=True):
            res = serve.provision_player_email('abcd efgh ijkl mnop')
        store.assert_called_once()
        self.assertEqual(res, {'ok': True, 'test_ok': True})

    def test_self_test_failure_reported(self):
        with unittest.mock.patch.object(serve, '_store_credential'), \
             unittest.mock.patch.object(serve, '_send_player_email_sync', return_value=False):
            res = serve.provision_player_email('abcd efgh ijkl mnop')
        self.assertEqual(res, {'ok': True, 'test_ok': False})


class DockerHelpers(unittest.TestCase):
    def test_network_exists_true_on_zero(self):
        with unittest.mock.patch.object(serve.subprocess, 'run',
                                        return_value=_fake_run_result(returncode=0)):
            self.assertTrue(serve._docker_network_exists('ai-think-tank-sandbox-net'))

    def test_network_exists_false_on_nonzero(self):
        with unittest.mock.patch.object(serve.subprocess, 'run',
                                        return_value=_fake_run_result(returncode=1)):
            self.assertFalse(serve._docker_network_exists('nope'))

    def test_container_running_true_when_stdout_true(self):
        with unittest.mock.patch.object(serve.subprocess, 'run',
                                        return_value=_fake_run_result(returncode=0, stdout='true\n')):
            self.assertTrue(serve._docker_container_running('c'))

    def test_container_running_false_when_stdout_not_true(self):
        with unittest.mock.patch.object(serve.subprocess, 'run',
                                        return_value=_fake_run_result(returncode=0, stdout='false\n')):
            self.assertFalse(serve._docker_container_running('c'))

    def test_env_value_returns_matching_line(self):
        stdout = 'PATH=/usr/bin\nSANDBOX_EGRESS_EXTRA_HOSTS=dreyx.com\n'
        with unittest.mock.patch.object(serve.subprocess, 'run',
                                        return_value=_fake_run_result(returncode=0, stdout=stdout)):
            self.assertEqual(serve._docker_container_env_value('c', 'SANDBOX_EGRESS_EXTRA_HOSTS'),
                             'dreyx.com')

    def test_env_value_none_on_inspect_failure(self):
        with unittest.mock.patch.object(serve.subprocess, 'run',
                                        return_value=_fake_run_result(returncode=1, stdout='')):
            self.assertIsNone(serve._docker_container_env_value('c', 'KEY'))

    def test_env_value_none_when_key_absent(self):
        with unittest.mock.patch.object(serve.subprocess, 'run',
                                        return_value=_fake_run_result(returncode=0,
                                                                      stdout='PATH=/usr/bin\n')):
            self.assertIsNone(serve._docker_container_env_value('c', 'SANDBOX_EGRESS_EXTRA_HOSTS'))


class EnsureSandboxNetworkingGap(unittest.TestCase):
    def test_creates_missing_networks(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _fake_run_result()

        def fake_network_exists(name):
            return name != serve.SANDBOX_NETWORK and name != serve.EGRESS_NETWORK

        with unittest.mock.patch.object(serve, '_docker_network_exists',
                                        side_effect=fake_network_exists), \
             unittest.mock.patch.object(serve, '_docker_container_running', return_value=False), \
             unittest.mock.patch.object(serve, '_docker_container_env_value', return_value=None), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=fake_run):
            serve.ensure_sandbox_networking()
        creates = [c for c in calls if c[:3] == ['docker', 'network', 'create']]
        self.assertEqual(len(creates), 2)
        self.assertIn(['docker', 'network', 'create', '--internal', serve.SANDBOX_NETWORK], creates)
        self.assertIn(['docker', 'network', 'create', serve.EGRESS_NETWORK], creates)


class RunInSandboxSync(unittest.TestCase):
    def test_local_backend_runs_command(self):
        with unittest.mock.patch.object(serve, 'SANDBOX_EXECUTION', 'local'), \
             unittest.mock.patch.object(serve.subprocess, 'run',
                                        return_value=_fake_run_result(returncode=0,
                                                                      stdout='hi\n', stderr='')):
            out = serve._run_in_sandbox_sync('/tmp/sb', 'echo hi')
        self.assertEqual(out['exitCode'], 0)
        self.assertEqual(out['stdout'], 'hi\n')
        self.assertFalse(out['timedOut'])

    def test_local_backend_timeout_returns_timed_out(self):
        exc = subprocess.TimeoutExpired(['docker', 'run'], 30,
                                        output='partial out', stderr='partial err')
        with unittest.mock.patch.object(serve, 'SANDBOX_EXECUTION', 'local'), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=exc):
            out = serve._run_in_sandbox_sync('/tmp/sb', 'sleep 100')
        self.assertTrue(out['timedOut'])
        self.assertEqual(out['stdout'], 'partial out')
        self.assertEqual(out['stderr'], 'partial err')
        self.assertIsNone(out['exitCode'])

    def test_digitalocean_unprovisioned_fails_closed(self):
        with unittest.mock.patch.object(serve, 'SANDBOX_EXECUTION', 'digitalocean'):
            out = serve._run_in_sandbox_sync('/tmp/sb', 'ls')
        self.assertIsNone(out['exitCode'])
        self.assertIn('not been provisioned', out['stderr'])


class FetchRenderedPageSync(unittest.TestCase):
    def _playwright_mock(self, final_url='https://example.com/real'):
        page = unittest.mock.MagicMock()
        page.url = final_url
        page.evaluate.side_effect = [
            'Body text here',
            [{'url': 'https://example.com/link', 'text': 'A link'},
             {'url': '/relative', 'text': 'relative'}],
        ]
        browser = unittest.mock.MagicMock()
        browser.new_page.return_value = page
        p = unittest.mock.MagicMock()
        p.chromium.launch.return_value = browser
        sp = unittest.mock.MagicMock()
        sp.__enter__.return_value = p
        return page, browser, sp

    def test_renders_page_and_returns_text_and_links(self):
        page, browser, sp = self._playwright_mock()
        with unittest.mock.patch.object(serve, 'sync_playwright', return_value=sp), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True):
            final_url, text, links = serve._fetch_rendered_page_sync('https://example.com')
        self.assertEqual(final_url, 'https://example.com/real')
        self.assertEqual(text, 'Body text here')
        self.assertEqual(links, [{'url': 'https://example.com/link', 'text': 'A link'}])
        page.goto.assert_called_once()
        page.wait_for_timeout.assert_called_once()
        browser.close.assert_called_once()

    def test_unsafe_redirect_raises_and_closes_browser(self):
        page, browser, sp = self._playwright_mock()
        with unittest.mock.patch.object(serve, 'sync_playwright', return_value=sp), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False):
            with self.assertRaises(ValueError):
                serve._fetch_rendered_page_sync('https://example.com')
        browser.close.assert_called_once()


class FetchPageSync(unittest.TestCase):
    def _resp(self, body=b'<html>hi</html>', final_url='https://example.com/final'):
        resp = unittest.mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.geturl.return_value = final_url
        resp.headers = {'Content-Type': 'text/html; charset=utf-8',
                        'Last-Modified': 'Wed, 21 Oct 2015 07:28:00 GMT'}
        resp.read.return_value = body
        return resp

    def test_returns_page_metadata_and_truncates(self):
        body = b'<html>hello</html>' + b'x' * (serve.BROWSE_MAX_BYTES + 10)
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=self._resp(body=body)), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True):
            final_url, ctype, content, truncated, last_modified = serve._fetch_page_sync(
                'https://example.com')
        self.assertEqual(final_url, 'https://example.com/final')
        self.assertEqual(ctype, 'text/html; charset=utf-8')
        self.assertTrue(truncated)
        self.assertEqual(len(content), serve.BROWSE_MAX_BYTES)
        self.assertIsInstance(last_modified, int)

    def test_no_truncation_when_under_max(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=self._resp()), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True):
            _f, _c, content, truncated, _lm = serve._fetch_page_sync('https://example.com')
        self.assertFalse(truncated)
        self.assertEqual(content, '<html>hi</html>')

    def test_unsafe_redirect_raises(self):
        with unittest.mock.patch('urllib.request.urlopen',
                                 return_value=self._resp(final_url='http://10.0.0.1/private')), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False):
            with self.assertRaises(ValueError):
                serve._fetch_page_sync('https://example.com')


class UrlopenWithResilience(unittest.TestCase):
    def test_success_returns_bytes(self):
        resp = _fake_urlopen_resp(b'data')
        with unittest.mock.patch('urllib.request.urlopen', return_value=resp):
            self.assertEqual(serve._urlopen_with_resilience('req', 30), b'data')

    def test_non_retryable_http_error_raises_immediately(self):
        err = urllib.error.HTTPError('http://x', 404, 'Not Found', {}, io.BytesIO(b''))
        with unittest.mock.patch('urllib.request.urlopen', side_effect=err), \
             unittest.mock.patch.object(serve.time, 'sleep') as sl:
            with self.assertRaises(urllib.error.HTTPError):
                serve._urlopen_with_resilience('req', 30, max_attempts=3)
        sl.assert_not_called()

    def test_retryable_http_error_then_success(self):
        err = urllib.error.HTTPError('http://x', 503, 'Unavailable', {}, io.BytesIO(b''))
        resp = _fake_urlopen_resp(b'recovered')
        with unittest.mock.patch('urllib.request.urlopen', side_effect=[err, resp]), \
             unittest.mock.patch.object(serve.time, 'sleep'):
            self.assertEqual(serve._urlopen_with_resilience('req', 30, max_attempts=3),
                             b'recovered')

    def test_urlerror_retries_then_raises_on_last_attempt(self):
        err = urllib.error.URLError('net down')
        with unittest.mock.patch('urllib.request.urlopen', side_effect=err), \
             unittest.mock.patch.object(serve.time, 'sleep'):
            with self.assertRaises(urllib.error.URLError):
                serve._urlopen_with_resilience('req', 30, max_attempts=2)

    def test_retryable_http_error_on_last_attempt_raises(self):
        err = urllib.error.HTTPError('http://x', 503, 'Unavailable', {}, io.BytesIO(b''))
        with unittest.mock.patch('urllib.request.urlopen', side_effect=err), \
             unittest.mock.patch.object(serve.time, 'sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                serve._urlopen_with_resilience('req', 30, max_attempts=2)

    def test_zero_attempts_raises_last_exc(self):
        with self.assertRaises(TypeError):
            serve._urlopen_with_resilience('req', 30, max_attempts=0)


class CallOpenrouterSync(unittest.TestCase):
    def _tier_patches(self):
        return unittest.mock.patch.multiple(
            serve,
            _coding_tier_slug=lambda: 'coding',
            _high_tier_slug=lambda: 'high',
            _mid_tier_slug=lambda: 'mid',
            _reasoning_tier_slug=lambda: 'reasoning',
        )

    def test_non_tier_model_disables_reasoning_and_records_success(self):
        with unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=False), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             self._tier_patches(), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        return_value=b'{"choices": []}'), \
             unittest.mock.patch.object(serve, 'record_model_result') as record:
            result = serve._call_openrouter_sync('other-model',
                                                 [{'role': 'user', 'content': 'hi'}], 100)
        self.assertEqual(result, {'choices': []})
        record.assert_called_once_with('other-model', success=True)

    def test_tier_model_enables_reasoning(self):
        with unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=False), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             self._tier_patches(), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        return_value=b'{}'):
            result = serve._call_openrouter_sync('high',
                                                 [{'role': 'user', 'content': 'hi'}], 100)
        self.assertEqual(result, {})

    def test_failure_records_failure_and_reraises(self):
        with unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=False), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        side_effect=RuntimeError('api down')), \
             unittest.mock.patch.object(serve, 'record_model_result') as record:
            with self.assertRaises(RuntimeError):
                serve._call_openrouter_sync('m', [{'role': 'user', 'content': 'hi'}], 100)
        record.assert_called_once_with('m', success=False)


class PostOpenrouterRaw(unittest.TestCase):
    def test_circuit_broken_raises(self):
        with unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=False), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'circuit-broken'):
                serve._post_openrouter_raw('m', [{'role': 'user', 'content': 'x'}])

    def test_success_returns_parsed_body(self):
        with unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=False), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        return_value=b'{"ok": true}'), \
             unittest.mock.patch.object(serve, 'record_model_result') as record:
            result = serve._post_openrouter_raw(
                'm', [{'role': 'user', 'content': 'x'}],
                tools=[{'type': 'function'}], max_tokens=10, tool_choice='required')
        self.assertEqual(result, {'ok': True})
        record.assert_called_once_with('m', success=True)


class CallAgentToolLoop(unittest.TestCase):
    def test_empty_content_prints_debug(self):
        data = {'choices': [{'message': {'content': '', 'tool_calls': []}}]}
        with unittest.mock.patch.object(serve, '_post_openrouter_raw', return_value=data), \
             unittest.mock.patch('builtins.print') as pr:
            result = serve._call_agent_tool_loop('m', [{'role': 'user', 'content': 'q'}], [],
                                                 lambda n, a: '')
        self.assertEqual(result, '')
        pr.assert_called()

    def test_tool_call_with_invalid_json_arguments_falls_back_to_empty(self):
        executed = []

        def execute_tool(name, args):
            executed.append((name, args))
            return 'tool result'

        calls = iter([
            {'choices': [{'message': {'content': 'thinking', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'lookup', 'arguments': '{bad json'}}]}}]},
            {'choices': [{'message': {'content': 'final answer', 'tool_calls': []}}]},
        ])
        with unittest.mock.patch.object(serve, '_post_openrouter_raw',
                                        side_effect=lambda *a, **k: next(calls)):
            result = serve._call_agent_tool_loop('m', [{'role': 'user', 'content': 'q'}], [],
                                                 execute_tool)
        self.assertEqual(result, 'final answer')
        self.assertEqual(executed, [('lookup', {})])

    def test_max_iterations_exhausted_returns_none(self):
        data = {'choices': [{'message': {'content': '', 'tool_calls': [
            {'id': 'c', 'function': {'name': 'f', 'arguments': '{}'}}]}}]}
        with unittest.mock.patch.object(serve, '_post_openrouter_raw', return_value=data):
            result = serve._call_agent_tool_loop('m', [{'role': 'user', 'content': 'q'}], [],
                                                 lambda n, a: 'r', max_iterations=1)
        self.assertIsNone(result)


class TavilySearchSync(unittest.TestCase):
    def test_no_api_key_returns_tool_error(self):
        with unittest.mock.patch.object(serve, 'TAVILY_API_KEY', ''):
            out = serve._tavily_search_sync('query')
        self.assertIn('not configured', out)

    def test_budget_exhausted_returns_tool_error(self):
        with unittest.mock.patch.object(serve, 'TAVILY_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, '_page_budget_exhausted', return_value=True):
            out = serve._tavily_search_sync('query')
        self.assertIn('page-request budget', out)

    def test_search_success_includes_answer_and_results(self):
        body = b'{"answer": "The sky is blue", "results": [{"title": "T1", "url": "https://e.com", "content": "snippet text"}]}'
        with unittest.mock.patch.object(serve, 'TAVILY_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, '_page_budget_exhausted', return_value=False), \
             unittest.mock.patch.object(serve, '_accrue_page_request') as accrue, \
             unittest.mock.patch('urllib.request.urlopen', return_value=_fake_urlopen_resp(body)):
            out = serve._tavily_search_sync('sky', max_results=5)
        self.assertIn('Quick answer: The sky is blue', out)
        self.assertIn('- T1 (https://e.com): snippet text', out)
        accrue.assert_called_once()

    def test_no_results_returns_note(self):
        with unittest.mock.patch.object(serve, 'TAVILY_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, '_page_budget_exhausted', return_value=False), \
             unittest.mock.patch.object(serve, '_accrue_page_request'), \
             unittest.mock.patch('urllib.request.urlopen',
                                 return_value=_fake_urlopen_resp(b'{"results": []}')):
            out = serve._tavily_search_sync('nothing')
        self.assertEqual(out, 'No search results found for "nothing".')

    def test_fetch_failure_returns_tool_error(self):
        with unittest.mock.patch.object(serve, 'TAVILY_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, '_page_budget_exhausted', return_value=False), \
             unittest.mock.patch('urllib.request.urlopen', side_effect=OSError('down')):
            out = serve._tavily_search_sync('query')
        self.assertIn('__TOOL_ERROR__: search failed', out)


class CallOpenrouterDecisionSync(unittest.TestCase):
    def test_model_not_in_chain_leads_chain(self):
        questions = {'choice': {'instructions': 'Approve or deny this?', 'criteria': ['a']}}
        data = {'answers': {'q': {'choice': 'proceed', 'confidence': 0.9}},
                'usage': {'cost': 0.01}}
        with unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_decision_model_chain',
                                        return_value=['typesafe/jev-1.13']), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        return_value=json.dumps(data).encode()), \
             unittest.mock.patch.object(serve, 'record_model_result') as record:
            result = serve._call_openrouter_decision_sync('my-model', {}, questions)
        self.assertEqual(result['answers']['q']['choice'], 'proceed')
        record.assert_called_once_with('my-model', True)


class GetSetting(unittest.TestCase):
    def test_returns_value_when_present(self):
        serve._set_setting('test_key', 'v1')
        self.assertEqual(serve._get_setting('test_key'), 'v1')

    def test_returns_default_when_absent(self):
        self.assertEqual(serve._get_setting('nope_key', 'dflt'), 'dflt')
        self.assertIsNone(serve._get_setting('nope_key'))

    def test_db_failure_returns_default(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db down')):
            self.assertEqual(serve._get_setting('any_key', 'dflt'), 'dflt')
            self.assertIsNone(serve._get_setting('any_key'))


if __name__ == '__main__':
    unittest.main()