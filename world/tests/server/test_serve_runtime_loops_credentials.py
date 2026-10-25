"""Coverage-gap tests for serve.py -- Cluster A.

Covers the log-pruning/backup/idle loops, model-tier refresh loop, JEV
auto-failover, spend accounting helpers, forecast/bank views,
social/refinement/escalation digest loggers, fernet/secret helpers, external
account-balance calls (_treg_call, _digitalocean_account_balance,
_pixellab_call, _google_call, _github_call), telegram poll/api, health check
loop, tier-gate decider default, escalation/peer-review loops, _lifespan,
director backfill/chain/teams-in-db helpers, credential helpers, and
server-secret/device-key helpers.

Run: COVERAGE_FILE=/tmp/cov_gap_A.coverage python3 -m coverage run --source=serve tests/test_serve_gap_A.py
"""
import asyncio
import contextlib
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
import urllib.error

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_REAL_SLEEP = asyncio.sleep

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-serve-gap-')
    # Every module-level path constant derived from THINK_TANK_DIR must be
    # repointed too: they are computed AT IMPORT, so patching THINK_TANK_DIR
    # alone leaves them pointing at the real repo root and any test that hits a
    # write path pollutes it. That is exactly how the `e1` fixture
    # ({"kind": "routine", "question": "q?", "askCount": MAX}) escaped
    # test_stops_at_ask_cap into the live escalations.json on 2026-10-04 and sat
    # there as an unresolvable "routine x1" until 2026-10-13 -- the other test
    # modules already patch ESCALATIONS_PATH for this reason (test_browse_
    # allowlist.py, test_failure_ledger.py, test_serve_gap_E.py).
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        ESCALATIONS_PATH=os.path.join(_MODULE_TMP_DIR, 'escalations.json'),
        FAILURES_PATH=os.path.join(_MODULE_TMP_DIR, 'failures.json'),
        RULE_PROPOSALS_PATH=os.path.join(_MODULE_TMP_DIR, 'rule_proposals.json'),
        SUCCESSES_PATH=os.path.join(_MODULE_TMP_DIR, 'successes.json'),
        SUCCESS_PROPOSALS_PATH=os.path.join(_MODULE_TMP_DIR, 'success_proposals.json'),
        BROWSE_TRAIL_PATH=os.path.join(_MODULE_TMP_DIR, 'browse_trail.json'),
        LIBRARY_USAGE_PATH=os.path.join(_MODULE_TMP_DIR, 'library_usage.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class _FakeResp:
    """Minimal stand-in for urllib response objects (supports `with` + read())."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload.encode('utf-8')


class _FakeCounter:
    """Thread-safety-free stand-in for serve._thread_safe_counter."""

    def __init__(self):
        self._vals = {}

    def bump(self, key):
        self._vals[key] = self._vals.get(key, 0) + 1

    def reset(self, key):
        self._vals.pop(key, None)

    def get(self, key):
        return self._vals.get(key, 0)


async def _completed(value):
    return value


def _http_error(code, reason='err'):
    err = urllib.error.HTTPError('http://example.invalid', code, reason, {}, None)
    err.read = lambda: b'error detail'
    return err
class FernetAndSecrets(unittest.TestCase):
    def test_seal_open_round_trips(self):
        tmp = tempfile.mkdtemp(prefix='gap-fernet-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with unittest.mock.patch.multiple(serve, _FERNET_EDEK_DIR=tmp,
                                          _FERNET_EDEK_PATH=os.path.join(tmp, 'edek.key')):
            sealed = serve._seal_secret('sup3r-secret')
        with unittest.mock.patch.multiple(serve, _FERNET_EDEK_DIR=tmp,
                                          _FERNET_EDEK_PATH=os.path.join(tmp, 'edek.key')):
            self.assertEqual(serve._open_secret(sealed), 'sup3r-secret')

    def test_fernet_none_without_cryptography(self):
        with unittest.mock.patch.dict(sys.modules, {'cryptography.fernet': None}):
            self.assertIsNone(serve._fernet())

    def test_seal_raises_when_fernet_unavailable(self):
        with unittest.mock.patch.object(serve, '_fernet', return_value=None):
            with self.assertRaises(RuntimeError):
                serve._seal_secret('x')

    def test_open_secret_none_when_fernet_unavailable(self):
        with unittest.mock.patch.object(serve, '_fernet', return_value=None):
            self.assertIsNone(serve._open_secret('x'))

    def test_open_secret_fails_closed_on_bad_token(self):
        fake = unittest.mock.Mock()
        fake.decrypt.side_effect = Exception('bad token')
        with unittest.mock.patch.object(serve, '_fernet', return_value=fake):
            self.assertIsNone(serve._open_secret('x'))


class DigitalOceanBalance(unittest.TestCase):
    def test_balance_none_without_token(self):
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=True), \
             unittest.mock.patch.object(serve, '_open_secret', return_value=''):
            self.assertIsNone(serve._digitalocean_account_balance())

    def test_balance_uses_cache(self):
        cached = {'at': time.time(), 'data': 12.34}
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=True), \
             unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_DIGITALOCEAN_BALANCE_CACHE', cached):
            self.assertEqual(serve._digitalocean_account_balance(), 12.34)

    def test_balance_fetches_live(self):
        cached = {'at': 0.0, 'data': None}
        payload = {'month_to_date_usage': '7.50'}
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=True), \
             unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_DIGITALOCEAN_BALANCE_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps(payload))):
            self.assertEqual(serve._digitalocean_account_balance(), 7.5)
        self.assertEqual(cached['data'], 7.5)

    def test_balance_fails_closed_on_network_error(self):
        cached = {'at': 0.0, 'data': None}
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=True), \
             unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_DIGITALOCEAN_BALANCE_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=urllib.error.URLError('no net')):
            self.assertIsNone(serve._digitalocean_account_balance())


class TregBalanceAndCall(unittest.TestCase):
    def test_balance_none_without_token(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=''):
            self.assertIsNone(serve._treg_account_balance())

    def test_balance_uses_cache(self):
        cached = {'at': time.time(), 'data': 3.0}
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_TREG_BALANCE_CACHE', cached):
            self.assertEqual(serve._treg_account_balance(), 3.0)

    def test_balance_fetches_live(self):
        cached = {'at': 0.0, 'data': None}
        orgs = [{'org_id': 'org-1'}]
        bal = {'blocks': [{'amount_micro': 2000000}], 'balance_usd': '0.50'}
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_TREG_BALANCE_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=[_FakeResp(json.dumps(orgs)), _FakeResp(json.dumps(bal))]):
            self.assertEqual(serve._treg_account_balance(), 1.5)
        self.assertEqual(cached['data'], 1.5)

    def test_balance_fails_closed_on_network_error(self):
        cached = {'at': 0.0, 'data': None}
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_TREG_BALANCE_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=urllib.error.URLError('no net')):
            self.assertIsNone(serve._treg_account_balance())

    def test_call_no_credential(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=''):
            data, error = serve._treg_call('ep')
        self.assertIsNone(data)
        self.assertIn('not configured', error)

    def test_call_post_with_query_string(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps({'ok': True}))):
            data, error = serve._treg_call('ep.post', body={'q': 1}, method='POST',
                                           query={'maxTotalChargeUsd': '1'})
        self.assertIsNone(error)
        self.assertEqual(data, {'ok': True})

    def test_call_get_puts_params_in_query(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps({'ok': True}))) as urlopen:
            data, error = serve._treg_call('ep.get', body={'woeid': 123}, method='GET', query={'a': 'b'})
        self.assertIsNone(error)
        self.assertEqual(data, {'ok': True})
        url = urlopen.call_args.args[0].full_url
        self.assertIn('woeid=123', url)
        self.assertIn('a=b', url)

    def test_call_non_json_response_fallback(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp('plain text here')):
            data, error = serve._treg_call('ep')
        self.assertIsNone(error)
        self.assertIn('_raw', data)

    def test_call_http_error(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=_http_error(400)):
            data, error = serve._treg_call('ep')
        self.assertIsNone(data)
        self.assertIn('400', error)

    def test_call_generic_error(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=ConnectionError('refused')):
            data, error = serve._treg_call('ep')
        self.assertIsNone(data)
        self.assertIn('failed', error)


class PixelLabBalanceAndCall(unittest.TestCase):
    def test_balance_none_without_token(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=''), \
             unittest.mock.patch.object(serve, '_pixellab_token', return_value=None):
            self.assertIsNone(serve._pixellab_account_balance())

    def test_balance_uses_cache_without_force(self):
        cached = {'at': time.time(), 'data': 3.0}
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_PIXELLAB_BALANCE_CACHE', cached):
            self.assertEqual(serve._pixellab_account_balance(), 3.0)

    def test_balance_tracks_depletion_from_first_observation(self):
        cached = {'at': 0.0, 'data': None}
        payloads = [{'credits': {'usd': '10.00'}}, {'credits': {'usd': '6.00'}}]
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_PIXELLAB_BALANCE_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=[_FakeResp(json.dumps(p)) for p in payloads]):
            self.assertEqual(serve._pixellab_account_balance(force=True), 0.0)
            self.assertEqual(serve._pixellab_account_balance(force=True), 4.0)
        self.assertEqual(cached['baseline'], 10.0)
        self.assertEqual(cached['data'], 4.0)

    def test_balance_fails_closed_on_network_error(self):
        cached = {'at': 0.0, 'data': None}
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve, '_PIXELLAB_BALANCE_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=urllib.error.URLError('no net')):
            self.assertIsNone(serve._pixellab_account_balance(force=True))

    def test_call_success(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps({'id': 'job-1'}))):
            data, error = serve._pixellab_call('POST', '/background-jobs', body={})
        self.assertIsNone(error)
        self.assertEqual(data['id'], 'job-1')

    def test_call_no_credential(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=''), \
             unittest.mock.patch.object(serve, '_pixellab_token', return_value=None):
            data, error = serve._pixellab_call('GET', '/x')
        self.assertIsNone(data)
        self.assertIn('not configured', error)

    def test_call_generic_error(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=ConnectionError('no')):
            data, error = serve._pixellab_call('GET', '/x')
        self.assertIsNone(data)
        self.assertIn('failed', error)


class GoogleAccessToken(unittest.TestCase):
    def _cached(self):
        return {'at': 0.0, 'token': None}

    def test_malformed_credential(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='not-json'), \
             unittest.mock.patch.object(serve, '_GOOGLE_ACCESS_TOKEN_CACHE', self._cached()):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('malformed', error)

    def test_missing_refresh_token(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=json.dumps({'client_id': 'i'})), \
             unittest.mock.patch.object(serve, '_GOOGLE_ACCESS_TOKEN_CACHE', self._cached()):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('refresh_token', error)

    def test_no_access_token_returned(self):
        creds = {'client_id': 'i', 'client_secret': 's', 'refresh_token': 'r'}
        payload = {'error': 'invalid_grant'}
        with unittest.mock.patch.object(serve, '_open_secret', return_value=json.dumps(creds)), \
             unittest.mock.patch.object(serve, '_GOOGLE_ACCESS_TOKEN_CACHE', self._cached()), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps(payload))):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('no access_token', error)

    def test_success_returns_and_caches_token(self):
        cached = self._cached()
        creds = {'client_id': 'i', 'client_secret': 's', 'refresh_token': 'r'}
        payload = {'access_token': 'tok123', 'expires_in': 3599}
        with unittest.mock.patch.object(serve, '_open_secret', return_value=json.dumps(creds)), \
             unittest.mock.patch.object(serve, '_GOOGLE_ACCESS_TOKEN_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps(payload))):
            token, error = serve._google_access_token()
        self.assertEqual((token, error), ('tok123', None))
        self.assertEqual(cached['token'], 'tok123')

    def test_http_error(self):
        with unittest.mock.patch.object(serve, '_open_secret',
                                        return_value=json.dumps({'client_id': 'i', 'client_secret': 's', 'refresh_token': 'r'})), \
             unittest.mock.patch.object(serve, '_GOOGLE_ACCESS_TOKEN_CACHE', self._cached()), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=_http_error(400)):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('400', error)

    def test_generic_error(self):
        with unittest.mock.patch.object(serve, '_open_secret',
                                        return_value=json.dumps({'client_id': 'i', 'client_secret': 's', 'refresh_token': 'r'})), \
             unittest.mock.patch.object(serve, '_GOOGLE_ACCESS_TOKEN_CACHE', self._cached()), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=ConnectionError('no')):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('failed', error)


class GoogleCall(unittest.TestCase):
    def test_token_error_short_circuits(self):
        with unittest.mock.patch.object(serve, '_google_access_token',
                                        return_value=(None, 'Google is not configured')):
            data, error = serve._google_call('GET', 'https://sheets.googleapis.com/x')
        self.assertIsNone(data)
        self.assertEqual(error, 'Google is not configured')

    def test_success(self):
        with unittest.mock.patch.object(serve, '_google_access_token', return_value=('tok', None)), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps({'ok': True}))):
            data, error = serve._google_call('GET', 'https://sheets.googleapis.com/x')
        self.assertIsNone(error)
        self.assertEqual(data, {'ok': True})

    def test_401_retries_with_forced_refresh(self):
        with unittest.mock.patch.object(serve, '_google_access_token',
                                        side_effect=[('stale', None), ('fresh', None)]), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=[_http_error(401), _FakeResp(json.dumps({'ok': True}))]):
            data, error = serve._google_call('GET', 'https://sheets.googleapis.com/x')
        self.assertIsNone(error)
        self.assertEqual(data, {'ok': True})

    def test_401_refresh_failure_returns_error(self):
        with unittest.mock.patch.object(serve, '_google_access_token',
                                        side_effect=[('stale', None), (None, 'refresh failed')]), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=_http_error(401)):
            data, error = serve._google_call('GET', 'https://sheets.googleapis.com/x')
        self.assertIsNone(data)
        self.assertIn('refresh failed', error)

    def test_http_error_returns_detail(self):
        with unittest.mock.patch.object(serve, '_google_access_token', return_value=('tok', None)), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=_http_error(403)):
            data, error = serve._google_call('GET', 'https://sheets.googleapis.com/x')
        self.assertIsNone(data)
        self.assertIn('403', error)

    def test_generic_error(self):
        with unittest.mock.patch.object(serve, '_google_access_token', return_value=('tok', None)), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=ConnectionError('no')):
            data, error = serve._google_call('GET', 'https://sheets.googleapis.com/x')
        self.assertIsNone(data)
        self.assertIn('failed', error)


class GithubCall(unittest.TestCase):
    def test_no_token(self):
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', ''):
            data, error = serve._github_call('GET', 'https://api.github.com/repos/x')
        self.assertIsNone(data)
        self.assertIn('not configured', error)

    def test_success(self):
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps({'full_name': 'a/b'}))):
            data, error = serve._github_call('GET', 'https://api.github.com/repos/a/b')
        self.assertIsNone(error)
        self.assertEqual(data, {'full_name': 'a/b'})

    def test_http_error(self):
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=_http_error(404)):
            data, error = serve._github_call('GET', 'https://api.github.com/repos/x')
        self.assertIsNone(data)
        self.assertIn('404', error)

    def test_generic_error(self):
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=ConnectionError('no')):
            data, error = serve._github_call('GET', 'https://api.github.com/repos/x')
        self.assertIsNone(data)
        self.assertIn('failed', error)


class CredentialOverCap(unittest.TestCase):
    def test_unlisted_credential_unaffected(self):
        self.assertEqual(serve._credential_over_cap('somecred', {}), (False, None))

    def test_no_cap_configured(self):
        with unittest.mock.patch.object(serve, '_HARD_CAPPED_CREDENTIALS', {'digitalocean': lambda: 0.0}):
            self.assertEqual(serve._credential_over_cap('digitalocean', {'products': {}}), (False, None))

    def test_unverifiable_balance_refuses(self):
        snapshot = {'products': {'digitalocean': {'budgetCapUsd': 25.0}}}
        with unittest.mock.patch.object(serve, '_HARD_CAPPED_CREDENTIALS', {'digitalocean': lambda: None}):
            over, reason = serve._credential_over_cap('digitalocean', snapshot)
        self.assertTrue(over)
        self.assertIn('could not verify', reason)

    def test_refuses_at_cap(self):
        snapshot = {'products': {'treg': {'budgetCapUsd': 10.0}}}
        with unittest.mock.patch.object(serve, '_HARD_CAPPED_CREDENTIALS', {'treg': lambda: 12.0}), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
            over, reason = serve._credential_over_cap('treg', snapshot)
        self.assertTrue(over)
        self.assertIn('$12.00 of $10.00', reason)

    def test_allows_under_cap(self):
        snapshot = {'products': {'treg': {'budgetCapUsd': 10.0}}}
        with unittest.mock.patch.object(serve, '_HARD_CAPPED_CREDENTIALS', {'treg': lambda: 2.0}), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
            self.assertEqual(serve._credential_over_cap('treg', snapshot), (False, None))


