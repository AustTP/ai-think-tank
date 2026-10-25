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
class SaveStateAndSpendAccounting(unittest.TestCase):
    def test_save_state_tolerates_directory_sync_failure(self):
        with unittest.mock.patch.object(serve, 'sync_agent_directories', side_effect=OSError('disk full')):
            serve.save_state_to_db({'agentRoster': [], 'agents': {}})

    def test_budget_cap_falls_back_for_non_string_service(self):
        self.assertEqual(serve._budget_cap_usd(123), serve.DEFAULT_BUDGET_CAP_USD)

    def test_budget_cap_breaks_on_product_with_invalid_cap(self):
        products = [{'id': 'svc', 'name': 'svc', 'budgetCapUsd': 0}]
        self.assertEqual(serve._budget_cap_usd('svc', products), serve.DEFAULT_BUDGET_CAP_USD)

    def test_budget_cap_reads_product_cap(self):
        products = [{'id': 'svc', 'name': 'svc', 'budgetCapUsd': 25.0}]
        self.assertEqual(serve._budget_cap_usd('svc', products), 25.0)

    def test_accrue_spend_tolerates_ledger_failure(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read', side_effect=RuntimeError('boom')):
            serve._accrue_spend('openrouter', 1.25)

    def test_spend_ledger_read_fails_closed(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db down')):
            self.assertEqual(serve._spend_ledger_read(), {})

    def test_spend_cap_repairs_non_numeric_baseline(self):
        period = serve._spend_cap_period()
        ledger = {serve._SPEND_CAP_BASELINE_KEY: {'period': period, 'baseline': 'not-a-number'},
                  'openrouter': {'used': 5.0}}
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 100.0), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger), \
             unittest.mock.patch.object(serve, '_spend_ledger_write') as write:
            self.assertFalse(serve._think_tank_spend_cap_exceeded())
        write.assert_called_once()
        self.assertEqual(ledger[serve._SPEND_CAP_BASELINE_KEY]['baseline'], 5.0)

    def test_spend_cap_disabled_when_zero(self):
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 0.0):
            self.assertFalse(serve._think_tank_spend_cap_exceeded())


class BankViewsAndForecast(unittest.TestCase):
    def test_bank_budget_view_seeds_zero_usage_product_rows(self):
        snapshot = {'products': [{'id': 'digitalocean', 'budgetCapUsd': 25.0}]}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}), \
             unittest.mock.patch.object(serve, 'APIFY_API_KEY', ''), \
             unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 0), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False):
            services = serve._bank_budget_view(snapshot)
        self.assertIn('digitalocean', services)
        self.assertEqual(services['digitalocean']['used'], 0.0)
        self.assertEqual(services['digitalocean']['cap'], 25.0)

    def test_bank_budget_view_adds_apify_and_colab_rows(self):
        snapshot = {'products': {}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}), \
             unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 5.0), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 100.0), \
             unittest.mock.patch.object(serve, '_colab_spend_this_month', return_value=10.0), \
             unittest.mock.patch.object(serve, '_colab_account_usage', return_value={'balance': 90.0}):
            services = serve._bank_budget_view(snapshot)
        self.assertIn(serve.APIFY_LEDGER_KEY, services)
        self.assertIn(serve.COLAB_LEDGER_KEY, services)
        self.assertEqual(services[serve.COLAB_LEDGER_KEY]['used'], 10.0)
        self.assertEqual(services[serve.COLAB_LEDGER_KEY]['balance_units'], 90.0)

    def test_forecast_skips_unparseable_day_keys(self):
        today = datetime.date.today()
        bucket = {'used': 10.0, 'byDay': {'not-a-date': 5.0, str(today): 5.0}}
        burn, days_left = serve._forecast(bucket, 50.0)
        self.assertEqual(burn, 5.0)
        self.assertEqual(days_left, 8.0)

    def test_forecast_no_signal(self):
        self.assertEqual(serve._forecast({'used': 0.0}, 50.0), (0.0, None))

    def test_forecast_over_cap_has_no_days_left(self):
        today = datetime.date.today()
        bucket = {'used': 60.0, 'byDay': {str(today): 60.0}}
        burn, days_left = serve._forecast(bucket, 50.0)
        self.assertIsNone(days_left)


class OpenRouterCredits(unittest.TestCase):
    def test_credits_none_without_key(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', ''):
            self.assertIsNone(serve._openrouter_account_credits())

    def test_credits_uses_cache(self):
        cached = {'at': time.time(), 'data': {'totalCredits': 10.0, 'totalUsage': 2.0, 'remaining': 8.0}}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, '_OPENROUTER_CREDITS_CACHE', cached):
            self.assertEqual(serve._openrouter_account_credits(), cached['data'])

    def test_credits_fetches_live_and_caches(self):
        cached = {'at': 0.0, 'data': None}
        payload = {'data': {'total_credits': '20', 'total_usage': '3.5'}}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, '_OPENROUTER_CREDITS_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=_FakeResp(json.dumps(payload))):
            result = serve._openrouter_account_credits()
        self.assertEqual(result['totalCredits'], 20.0)
        self.assertEqual(result['totalUsage'], 3.5)
        self.assertEqual(result['remaining'], 16.5)
        self.assertIsNotNone(cached['data'])

    def test_credits_fails_closed_on_network_error(self):
        cached = {'at': 0.0, 'data': None}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, '_OPENROUTER_CREDITS_CACHE', cached), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=urllib.error.URLError('no net')):
            self.assertIsNone(serve._openrouter_account_credits())


class RosterAndDigestHelpers(unittest.TestCase):
    def test_default_roster_skips_blank_and_incomplete_entries(self):
        raw = 'ada|Ada|#e06666|Research|small, , solo'
        with unittest.mock.patch.object(serve, '_load_env', return_value={'SEED_ROSTER': raw}):
            roster = serve._default_roster_definitions()
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0]['id'], 'ada')
        self.assertEqual(roster[0]['color'], '#e06666')

    def test_render_conversation_md_labels_speakers(self):
        log = [{'fromId': 'player', 'text': 'hello'}, {'fromId': 'ada', 'text': 'hi there'}]
        out = serve._render_conversation_md(log)
        self.assertIn('**You:** hello', out)
        self.assertIn('**Agent:** hi there', out)

    def test_render_conversation_md_empty(self):
        self.assertTrue(serve._render_conversation_md(None).startswith('# Conversation'))


