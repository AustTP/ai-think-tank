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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve

_REAL_SLEEP = asyncio.sleep

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


class PruneAndBackupLoops(unittest.TestCase):
    def test_backup_loop_snapshots_then_sleeps(self):
        calls = []

        def fake_to_thread(fn, *a, **kw):
            calls.append(fn)
            return _completed(None)

        with unittest.mock.patch.object(serve.asyncio, 'sleep',
                                        side_effect=[0, asyncio.CancelledError()]), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=fake_to_thread):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._backup_loop())
        self.assertEqual(calls, [serve._backup_think_tank_db])

    def test_prune_logs_deletes_old_rows(self):
        with serve._db() as conn:
            conn.execute('INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) VALUES (?,?,?,?,?,?)',
                         ('old-agent', 'browse', None, None, time.time() - 20 * 86400, None))
            conn.execute('INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) VALUES (?,?,?,?,?,?)',
                         ('new-agent', 'browse', None, None, time.time(), None))
        with unittest.mock.patch.object(serve, '_load_env', return_value={}):
            deleted = serve._prune_logs()
        self.assertGreaterEqual(deleted, 1)
        with serve._db() as conn:
            self.assertEqual(conn.execute("SELECT agent_id FROM action_log WHERE agent_id = 'old-agent'").fetchall(), [])
            self.assertEqual(len(conn.execute("SELECT agent_id FROM action_log WHERE agent_id = 'new-agent'").fetchall()), 1)

    def test_prune_logs_falls_back_on_env_error(self):
        with unittest.mock.patch.object(serve, '_load_env', side_effect=RuntimeError('env boom')):
            self.assertIsInstance(serve._prune_logs(), int)

    def test_prune_logs_is_best_effort_on_db_failure(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db down')):
            self.assertEqual(serve._prune_logs(), 0)

    def test_prune_logs_drops_resolved_escalations_past_retention(self):
        old_resolved = {'esc-old': {'status': 'approved', 'ts': time.time() - 30 * 86400,
                                    'resolvedAt': time.time() - 30 * 86400}}
        fresh_resolved = {'esc-fresh': {'status': 'denied', 'ts': time.time(),
                                        'resolvedAt': time.time()}}
        pending = {'esc-pending': {'status': 'pending', 'ts': time.time() - 30 * 86400}}
        with unittest.mock.patch.object(serve, '_load_escalations',
                                        return_value=dict(old_resolved, **fresh_resolved, **pending)), \
             unittest.mock.patch.object(serve, '_save_escalations') as save:
            serve._prune_logs()
        saved = save.call_args.args[0]
        self.assertNotIn('esc-old', saved)
        self.assertIn('esc-fresh', saved)
        self.assertIn('esc-pending', saved)

    def test_prune_logs_tolerates_escalation_prune_failure(self):
        with unittest.mock.patch.object(serve, '_load_escalations', side_effect=OSError('locked')):
            self.assertIsInstance(serve._prune_logs(), int)

    def test_log_prune_loop_runs(self):
        with unittest.mock.patch.object(serve, '_prune_logs') as prune, \
             unittest.mock.patch.object(serve.asyncio, 'sleep',
                                        side_effect=[0, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._log_prune_loop())
        prune.assert_called_once()
        self.assertIsNotNone(serve._LAST_LOG_PRUNE)


class ModelTierRefreshLoop(unittest.TestCase):
    def _run_loop(self, **patchers):
        with unittest.mock.patch.object(serve.asyncio, 'sleep',
                                        side_effect=[0, asyncio.CancelledError()]):
            with unittest.mock.patch.multiple(serve, **patchers) as mocks:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(serve._model_tier_refresh_loop())
                self._last_mocks = mocks

    def test_refresh_loop_logs_for_admin_actor(self):
        state = {'agentRoster': [{'id': 'theo', 'isAdmin': True}]}
        self._run_loop(
            refresh_model_tiers=unittest.mock.AsyncMock(return_value={'high': {'id': 'high-model'}}),
            get_state_from_db=lambda: state,
            log_action=unittest.mock.DEFAULT,
            _auto_failover_jev_if_gone=unittest.mock.AsyncMock(),
        )
        log = self._last_mocks['log_action']
        log.assert_called_once()
        self.assertEqual(log.call_args.args[0], 'theo')
        self.assertEqual(log.call_args.args[1], 'model_tiers_refreshed')

    def test_refresh_loop_prints_when_no_actor(self):
        self._run_loop(
            refresh_model_tiers=unittest.mock.AsyncMock(return_value={'high': {'id': 'high-model'}}),
            get_state_from_db=lambda: {'agentRoster': []},
            log_action=unittest.mock.DEFAULT,
            _auto_failover_jev_if_gone=unittest.mock.AsyncMock(),
        )
        self._last_mocks['log_action'].assert_not_called()

    def test_refresh_loop_failure_keeps_current_tiers(self):
        self._run_loop(
            refresh_model_tiers=unittest.mock.AsyncMock(side_effect=RuntimeError('catalog down')),
        )


class AutoFailoverJev(unittest.TestCase):
    def test_failover_noop_when_no_current_model(self):
        with unittest.mock.patch.object(serve, '_jev_model', return_value=''):
            self.assertIsNone(asyncio.run(serve._auto_failover_jev_if_gone({'high': {'id': 'x'}}, None)))

    def test_failover_noop_when_current_is_loopback(self):
        with unittest.mock.patch.object(serve, '_jev_model', return_value='http://127.0.0.1:8939/v1/systemone'):
            self.assertIsNone(asyncio.run(serve._auto_failover_jev_if_gone({}, None)))

    def test_failover_noop_when_current_still_works(self):
        with unittest.mock.patch.object(serve, '_jev_model', return_value='model-a'), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=[True]):
            self.assertIsNone(asyncio.run(serve._auto_failover_jev_if_gone({}, None)))

    def test_failover_noop_when_chain_fallback_works(self):
        with unittest.mock.patch.object(serve, '_jev_model', return_value='model-a'), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['model-a', 'model-b']), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=[False, True]):
            self.assertIsNone(asyncio.run(serve._auto_failover_jev_if_gone({}, None)))

    def test_failover_noop_without_high_replacement(self):
        with unittest.mock.patch.object(serve, '_jev_model', return_value='model-a'), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['model-a']), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=[False]):
            self.assertIsNone(asyncio.run(serve._auto_failover_jev_if_gone({}, None)))

    def test_failover_noop_when_replacement_is_current(self):
        with unittest.mock.patch.object(serve, '_jev_model', return_value='model-a'), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['model-a']), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=[False]):
            self.assertIsNone(asyncio.run(serve._auto_failover_jev_if_gone({'high': {'id': 'model-a'}}, None)))

    def test_failover_switches_with_actor_and_logs(self):
        with unittest.mock.patch.object(serve, '_jev_model', return_value='model-a'), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['model-a']), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=[False]), \
             unittest.mock.patch.object(serve, '_set_setting') as set_setting, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            asyncio.run(serve._auto_failover_jev_if_gone({'high': {'id': 'model-b'}}, 'theo'))
        set_setting.assert_called_once_with('jev_model', 'model-b')
        log.assert_called_once()
        self.assertEqual(log.call_args.args[1], 'jev_model_auto_failover')

    def test_failover_switches_without_actor(self):
        with unittest.mock.patch.object(serve, '_jev_model', return_value='model-a'), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['model-a']), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=[False]), \
             unittest.mock.patch.object(serve, '_set_setting'), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            asyncio.run(serve._auto_failover_jev_if_gone({'high': {'id': 'model-b'}}, None))
        log.assert_not_called()


class IdleShutdownLoop(unittest.TestCase):
    def test_idle_loop_skips_when_no_request_seen(self):
        original = serve._LAST_REQUEST_TIME
        serve._LAST_REQUEST_TIME = None
        try:
            with unittest.mock.patch.object(serve.asyncio, 'sleep',
                                            side_effect=[0, asyncio.CancelledError()]), \
                 unittest.mock.patch.object(serve, '_dormant') as dormant:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(serve._idle_shutdown_loop(poll_s=0.01))
            dormant.assert_not_called()
        finally:
            serve._LAST_REQUEST_TIME = original

    def test_idle_loop_goes_dormant_after_grace(self):
        original = serve._LAST_REQUEST_TIME
        serve._LAST_REQUEST_TIME = time.time() - 100
        try:
            with unittest.mock.patch.object(serve, '_MAX_IDLE_MINUTES', 0.5), \
                 unittest.mock.patch.object(serve, '_dormant', return_value=False), \
                 unittest.mock.patch.object(serve, '_set_dormant') as set_dormant, \
                 unittest.mock.patch.object(serve.asyncio, 'sleep',
                                            side_effect=[0, asyncio.CancelledError()]):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(serve._idle_shutdown_loop(poll_s=0.01))
            set_dormant.assert_called_once_with(True)
        finally:
            serve._LAST_REQUEST_TIME = original


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


class SyncPrototypes(unittest.TestCase):
    def test_sync_prototypes_ignores_placeholder_identities(self):
        self.assertIsNone(serve.sync_prototypes('player', '/nonexistent'))
        self.assertIsNone(serve.sync_prototypes(None, '/nonexistent'))
        self.assertIsNone(serve.sync_prototypes('unknown', '/nonexistent'))

    def test_sync_prototypes_mirrors_sandbox(self):
        sandbox = tempfile.mkdtemp(prefix='gap-sandbox-')
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        with open(os.path.join(sandbox, 'index.html'), 'w') as f:
            f.write('<html>x</html>')
        agent_dir = os.path.join(serve.AGENTS_DIR, 'gap-agent')
        os.makedirs(os.path.join(agent_dir, 'prototypes'), exist_ok=True)
        with open(os.path.join(agent_dir, 'prototypes', 'old.txt'), 'w') as f:
            f.write('old')
        self.addCleanup(shutil.rmtree, agent_dir, ignore_errors=True)
        serve.sync_prototypes('gap-agent', sandbox)
        dest = os.path.join(serve.AGENTS_DIR, 'gap-agent', 'prototypes')
        self.assertTrue(os.path.isfile(os.path.join(dest, 'index.html')))
        self.assertFalse(os.path.exists(os.path.join(dest, 'old.txt')))

    def test_sync_prototypes_tolerates_oserror(self):
        sandbox = tempfile.mkdtemp(prefix='gap-sandbox-')
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        with unittest.mock.patch.object(serve.shutil, 'copytree', side_effect=OSError('permission denied')):
            serve.sync_prototypes('gap-agent-err', sandbox)


class SocialDigest(unittest.TestCase):
    def test_log_social_digest_writes_attendee_lines(self):
        serve.log_action('ada', 'task_completed', {'title': 'Weekly Bank Report'}, authorized=False)
        state = {
            'agents': {'ada': {'name': 'Ada', 'weekApprovals': 3}},
            'agentRoster': [{'id': 'ada', 'name': 'Ada'}],
        }
        serve.log_social_digest(state, {'ada': {}}, decisions=[{'agentId': 'ada', 'choice': 'adopt', 'confidence': 0.8}])
        files = os.listdir(os.path.join(_MODULE_TMP_DIR, 'library', 'social'))
        self.assertEqual(len(files), 1)
        with open(os.path.join(_MODULE_TMP_DIR, 'library', 'social', files[0])) as f:
            content = f.read()
        self.assertIn('recently delivered: "Weekly Bank Report"', content)
        self.assertIn('carry-away: **adopt** (conf 0.80)', content)

    def test_log_social_digest_tolerates_corrupt_details_row(self):
        with serve._db() as conn:
            conn.execute('INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) VALUES (?,?,?,?,?,?)',
                         ('ada', 'task_completed', 'not-json{{{', None, time.time(), None))
        state = {'agents': {'ada': {}}, 'agentRoster': [{'id': 'ada', 'name': 'Ada'}]}
        serve.log_social_digest(state, {'ada': {}})

    def test_log_social_digest_tolerates_write_failure(self):
        blocker = os.path.join(_MODULE_TMP_DIR, 'library-blocker')
        with open(blocker, 'w') as f:
            f.write('x')
        self.addCleanup(os.remove, blocker)
        with unittest.mock.patch.object(serve, 'LIBRARY_DIR', blocker):
            serve.log_social_digest({'agents': {}, 'agentRoster': []}, {'ada': {}})


class RefinementEscalationDigests(unittest.TestCase):
    def test_log_refinement_digest_writes_accepted_rows(self):
        state = {'agents': {}, 'agentRoster': [{'id': 'nora', 'name': 'Nora'}]}
        groom = {'scrumMasterId': 'nora',
                 'accepted': [{'room': 'bank', 'title': 'Reconcile ledger', 'filedBy': 'ada'}]}
        serve.log_refinement_digest(state, state['agents'], groom)
        files = os.listdir(os.path.join(_MODULE_TMP_DIR, 'library', 'refinement'))
        self.assertEqual(len(files), 1)
        with open(os.path.join(_MODULE_TMP_DIR, 'library', 'refinement', files[0])) as f:
            self.assertIn('Reconcile ledger', f.read())

    def test_log_refinement_digest_none_accepted_branch(self):
        serve.log_refinement_digest({'agents': {}, 'agentRoster': []}, {}, {})

    def test_log_refinement_digest_tolerates_write_failure(self):
        blocker = os.path.join(_MODULE_TMP_DIR, 'refinement-blocker')
        with open(blocker, 'w') as f:
            f.write('x')
        self.addCleanup(os.remove, blocker)
        with unittest.mock.patch.object(serve, 'LIBRARY_DIR', blocker):
            serve.log_refinement_digest({'agents': {}, 'agentRoster': []}, {}, {'scrumMasterId': 'n'})

    def test_log_escalation_digest_writes(self):
        state = {'agents': {}, 'agentRoster': [{'id': 'nora', 'name': 'Nora'}]}
        pending = {'scrumMasterId': 'nora', 'productId': 'bank',
                   'source': 'assignment-abandoned', 'title': 'restore failed'}
        serve.log_escalation_digest(state, pending, 'story')
        files = os.listdir(os.path.join(_MODULE_TMP_DIR, 'library', 'escalations'))
        self.assertEqual(len(files), 1)

    def test_log_escalation_digest_tolerates_write_failure(self):
        blocker = os.path.join(_MODULE_TMP_DIR, 'escalation-blocker')
        with open(blocker, 'w') as f:
            f.write('x')
        self.addCleanup(os.remove, blocker)
        with unittest.mock.patch.object(serve, 'LIBRARY_DIR', blocker):
            serve.log_escalation_digest({'agents': {}, 'agentRoster': []}, None, 'spike')


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
        with unittest.mock.patch.object(serve, '_open_secret', return_value=''):
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
        with unittest.mock.patch.object(serve, '_open_secret', return_value=''):
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


class TelegramApiSync(unittest.TestCase):
    def test_success_returns_result(self):
        body = json.dumps({'ok': True, 'result': {'update_id': 1}})
        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', return_value=_FakeResp(body)):
            self.assertEqual(serve._telegram_api_sync('getUpdates', {}), {'update_id': 1})

    def test_not_ok_returns_none(self):
        body = json.dumps({'ok': False, 'description': 'x'})
        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', return_value=_FakeResp(body)):
            self.assertIsNone(serve._telegram_api_sync('getUpdates', {}))

    def test_409_raises_conflict_error(self):
        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=_http_error(409)):
            with self.assertRaises(serve._TelegramConflictError):
                serve._telegram_api_sync('getUpdates', {})

    def test_http_error_fails_closed(self):
        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=_http_error(500)):
            self.assertIsNone(serve._telegram_api_sync('getUpdates', {}))

    def test_generic_error_fails_closed(self):
        with unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=ConnectionError('no')):
            self.assertIsNone(serve._telegram_api_sync('getUpdates', {}))


class TelegramPollLoop(unittest.TestCase):
    def test_processes_updates_then_handles_conflict(self):
        updates = [
            {'update_id': 1, 'message': {'chat': {'id': '111'}, 'text': 'hi'}},
            {'update_id': 2, 'message': {'chat': {'id': '222'}, 'text': 'ignored'}},
        ]
        with unittest.mock.patch.object(serve, '_telegram_process_update',
                                        new=unittest.mock.AsyncMock(side_effect=[('111', 'hello back'), None])) as proc, \
             unittest.mock.patch.object(serve.asyncio, 'to_thread',
                                        side_effect=[updates, {'ok': True},
                                                     serve._TelegramConflictError('another poller')]) as to_thread, \
             unittest.mock.patch.object(serve.asyncio, 'sleep', side_effect=[asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._telegram_poll_loop())
        self.assertEqual(proc.await_count, 2)
        self.assertEqual(to_thread.call_count, 3)

    def test_tolerates_generic_loop_errors(self):
        with unittest.mock.patch.object(serve.asyncio, 'to_thread',
                                        side_effect=[RuntimeError('blip')]), \
             unittest.mock.patch.object(serve.asyncio, 'sleep', side_effect=[asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._telegram_poll_loop())


class HealthCheckLoop(unittest.TestCase):
    def test_runs_pipeline(self):
        snapshot = {'alerts': []}
        with unittest.mock.patch.object(serve.asyncio, 'to_thread',
                                        side_effect=[snapshot, [], None, None]), \
             unittest.mock.patch.object(serve.asyncio, 'sleep', side_effect=[asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._health_check_loop())

    def test_tolerates_loop_errors(self):
        with unittest.mock.patch.object(serve.asyncio, 'to_thread',
                                        side_effect=[RuntimeError('boom')]), \
             unittest.mock.patch.object(serve.asyncio, 'sleep', side_effect=[asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._health_check_loop())


class TierGateAndSeniorDirector(unittest.TestCase):
    def test_tier_gate_decider_propagates_decision(self):
        with unittest.mock.patch.object(serve, '_jev_quorum_choice_sync', return_value=('mid', 0.8, 0.02)):
            self.assertEqual(serve._tier_gate_decider_default('inst', 'crit'), ('mid', 0.8))

    def test_tier_gate_decider_fails_closed(self):
        with unittest.mock.patch.object(serve, '_jev_quorum_choice_sync', side_effect=RuntimeError('jev down')):
            self.assertEqual(serve._tier_gate_decider_default('inst', 'crit'), (None, 0.0))

    def test_senior_most_director_id_none_without_director(self):
        state = {'agentRoster': [{'id': 'w', 'director': 'nora'}]}
        self.assertIsNone(serve._senior_most_director_id(state))

    def test_senior_most_director_id_finds_non_admin_director(self):
        state = {'agentRoster': [{'id': 'theo', 'isAdmin': True, 'isDirector': True},
                                 {'id': 'nora', 'isDirector': True},
                                 {'id': 'zara', 'director': 'nora'}]}
        self.assertEqual(serve._senior_most_director_id(state), 'nora')


class ResolvePendingEscalations(unittest.TestCase):
    def _state(self):
        return {'agentRoster': [{'id': 'nora', 'isDirector': True, 'name': 'Nora'}]}

    def _patch_checks(self):
        return unittest.mock.patch.multiple(
            serve,
            _escalation_floor=unittest.mock.Mock(return_value=0.0),
            _escalation_judge_drift=unittest.mock.Mock(return_value=0),
        )

    def test_no_pending_returns_zero(self):
        with unittest.mock.patch.object(serve, '_load_escalations', return_value={}):
            self.assertEqual(serve._resolve_pending_escalations_sync(), 0)

    def test_no_director_returns_zero(self):
        esc = {'e1': {'status': 'pending', 'kind': 'routine', 'question': 'q?'}}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value={'agentRoster': [{'id': 'w', 'director': 'nora'}]}):
            self.assertEqual(serve._resolve_pending_escalations_sync(), 0)

    def test_skips_within_reask_cooldown(self):
        esc = {'e1': {'status': 'pending', 'kind': 'routine', 'question': 'q?',
                      'lastAskedAt': time.time(), 'askCount': 0}}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()):
            with self._patch_checks():
                with unittest.mock.patch.object(serve, '_save_escalations') as save:
                    self.assertEqual(serve._resolve_pending_escalations_sync(), 0)
            save.assert_not_called()

    def test_stops_at_ask_cap(self):
        esc = {'e1': {'status': 'pending', 'kind': 'routine', 'question': 'q?',
                      'lastAskedAt': 0, 'askCount': serve.DIRECTOR_ESCALATION_MAX_ASKS}}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()):
            with self._patch_checks():
                self.assertEqual(serve._resolve_pending_escalations_sync(), 0)

    def test_approves_escalation(self):
        esc = {'e1': {'status': 'pending', 'kind': 'routine', 'question': 'Approve this?',
                      'lastAskedAt': 0, 'askCount': 0}}
        saved = {}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()):
            with self._patch_checks():
                with unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                                return_value=('approve', 0.95, 0.1)), \
                     unittest.mock.patch.object(serve, '_escalation_jev_errors', _FakeCounter()), \
                     unittest.mock.patch.object(serve, '_jev_directory_score', return_value=0.95), \
                     unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
                     unittest.mock.patch.object(serve, '_jev_judge_model', return_value=None), \
                     unittest.mock.patch.object(serve, 'log_action') as log, \
                     unittest.mock.patch.object(serve, '_save_escalations',
                                                side_effect=lambda d: saved.update(d)) as save:
                    result = serve._resolve_pending_escalations_sync()
        self.assertEqual(result, 1)
        self.assertEqual(esc['e1']['status'], 'approved')
        save.assert_called_once()
        log.assert_called_once()
        self.assertEqual(log.call_args.args[1], 'escalation_approve')


class DirectorApprovalLoop(unittest.TestCase):
    def test_runs_and_handles_errors(self):
        with unittest.mock.patch.object(serve.asyncio, 'to_thread',
                                        side_effect=[0, RuntimeError('boom')]), \
             unittest.mock.patch.object(serve.asyncio, 'sleep',
                                        side_effect=[0, 0, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._director_approval_loop())


class PeerReviewHelpers(unittest.TestCase):
    def test_loop_pass_returns_zero_without_state(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None):
            self.assertEqual(serve._peer_review_loop_pass(None, 1000.0), 0)

    def test_loop_pass_loads_state_and_saves(self):
        state = {'agentRoster': [], 'agents': {}}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_peer_review_pass', return_value=2) as prp, \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            n = serve._peer_review_loop_pass(None, 12345.0)
        self.assertEqual(n, 2)
        prp.assert_called_once_with(state, 12345.0)
        save.assert_called_once_with(state)

    def test_loop_pass_uses_provided_state(self):
        state = {}
        with unittest.mock.patch.object(serve, '_peer_review_pass', return_value=0) as prp:
            self.assertEqual(serve._peer_review_loop_pass(state, 99.0), 0)
        prp.assert_called_once_with(state, 99.0)

    def test_tick_skips_within_cadence(self):
        state = {'lastPeerReviewAt': time.time()}
        with unittest.mock.patch.object(serve, '_peer_review_pass') as prp:
            self.assertEqual(serve._peer_review_tick(state), 0)
        prp.assert_not_called()

    def test_tick_prunes_stale_reports(self):
        now = time.time()
        fresh_ts = int(now * 1000)
        state = {'lastPeerReviewAt': 0,
                 'reports': [{'aboutId': 'a', 'ts': 1}, {'aboutId': 'b', 'ts': fresh_ts}]}
        with unittest.mock.patch.object(serve, '_peer_review_pass', return_value=1) as prp:
            self.assertEqual(serve._peer_review_tick(state), 1)
        self.assertEqual(state['reports'], [{'aboutId': 'b', 'ts': fresh_ts}])
        prp.assert_called_once()

    def test_loop_backcompat_stub(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep',
                                        side_effect=[0, 0, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._peer_review_loop())

    def test_loop_tolerates_errors(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep',
                                        side_effect=[0, RuntimeError('x'),
                                                     asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._peer_review_loop())


class PeerReviewPass(unittest.TestCase):
    def _log_work(self, agent_id, n):
        for _ in range(n):
            serve.log_action(agent_id, 'task_completed', {'title': 'x'}, authorized=False)

    def test_files_a_peer_note(self):
        now = time.time()
        state = {
            'agentRoster': [
                {'id': None, 'name': 'Nameless'},
                {'id': 'ghost', 'name': 'Ghost', 'role': 'Worker'},
                {'id': 'busy', 'name': 'Busy', 'role': 'Worker'},
                {'id': 'idle', 'name': 'Idle', 'role': 'Worker'},
            ],
            'agents': {'busy': {'name': 'Busy'}, 'idle': {'name': 'Idle'}},
            'reports': 'not-a-list',
        }
        self._log_work('busy', 3)
        with unittest.mock.patch.object(serve.random, 'random', return_value=0.0):
            n = serve._peer_review_pass(state, now)
        self.assertEqual(n, 1)
        self.assertEqual(len(state['reports']), 1)
        self.assertEqual(state['reports'][0]['aboutId'], 'idle')
        self.assertEqual(state['reports'][0]['fromId'], 'busy')

    def test_returns_zero_without_observers(self):
        now = time.time()
        state = {
            'agentRoster': [{'id': 'target', 'name': 'T', 'role': 'Worker'}],
            'agents': {'target': {'name': 'T'}},
            'reports': [],
        }
        self._log_work('target', 5)
        with unittest.mock.patch.object(serve.random, 'random', return_value=0.0):
            self.assertEqual(serve._peer_review_pass(state, now), 0)
        self.assertEqual(state['reports'], [])

    def test_returns_zero_when_village_idle(self):
        state = {'agentRoster': [{'id': 'a', 'name': 'A', 'role': 'W'}],
                 'agents': {'a': {}}, 'reports': []}
        self.assertEqual(serve._peer_review_pass(state, time.time()), 0)


class Lifespan(unittest.TestCase):
    async def _run(self):
        async with serve._lifespan(serve.app):
            await _REAL_SLEEP(0.01)

    def _run_patched(self, **kwargs):
        with contextlib.ExitStack() as stack:
            for p in self._patches(**kwargs):
                stack.enter_context(p)
            asyncio.run(self._run())

    def _patches(self, telegram=False, sim_ok=True):
        async def _noop(*a, **k):
            return None

        patches = []
        for name in ('_health_check_loop', '_director_approval_loop', '_backup_loop',
                     '_log_prune_loop', '_model_tier_refresh_loop', '_calibration_loop',
                     '_weekly_review_loop', '_colab_failover_loop', '_colab_compute_idle_loop',
                     '_telegram_poll_loop', '_pending_ask_drain_loop'):
            patches.append(unittest.mock.patch.object(serve, name, _noop))
        if telegram:
            patches.append(unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'))
            patches.append(unittest.mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'123'}))
        else:
            patches.append(unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', ''))
            patches.append(unittest.mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', set()))
        fake_sim = types.ModuleType('sim')
        fake_sim._sim_loop = _noop
        fake_content = types.ModuleType('content')
        if sim_ok:
            fake_content._server_content_dispatcher = lambda *a, **k: None
        patches.append(unittest.mock.patch.dict(sys.modules, {'sim': fake_sim, 'content': fake_content}))
        return patches

    def test_backfill_failures_are_logged_and_loops_start(self):
        with unittest.mock.patch.object(serve, '_backfill_directors_in_db',
                                        side_effect=RuntimeError('dir down')), \
             unittest.mock.patch.object(serve, '_backfill_teams_in_db',
                                        side_effect=RuntimeError('teams down')), \
             unittest.mock.patch.object(serve, '_init_templates_in_db',
                                        side_effect=RuntimeError('tpl down')), \
             unittest.mock.patch.object(serve, '_backfill_agent_identity_in_db',
                                        side_effect=RuntimeError('id down')):
            self._run_patched(sim_ok=True)

    def test_templates_backfill_saves_state(self):
        with unittest.mock.patch.object(serve, '_backfill_directors_in_db'), \
             unittest.mock.patch.object(serve, '_backfill_teams_in_db'), \
             unittest.mock.patch.object(serve, '_backfill_agent_identity_in_db'), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value={'agentRoster': [], 'agents': {}}), \
             unittest.mock.patch.object(serve, '_init_templates_in_db', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            self._run_patched(sim_ok=True)
        save.assert_called_once()

    def test_templates_backfill_failure_is_logged(self):
        with unittest.mock.patch.object(serve, '_backfill_directors_in_db'), \
             unittest.mock.patch.object(serve, '_backfill_teams_in_db'), \
             unittest.mock.patch.object(serve, '_backfill_agent_identity_in_db'), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        side_effect=RuntimeError('state down')):
            self._run_patched(sim_ok=True)

    def test_sim_failure_is_logged(self):
        self._run_patched(sim_ok=False)

    def test_telegram_bridge_starts_and_cancels(self):
        self._run_patched(telegram=True, sim_ok=True)


class DirectorAndTeams(unittest.TestCase):
    def test_director_backfill_map_skips_invalid_pairs(self):
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'SEED_DIRECTOR_MAP': 'ada:theo,badpair,zed:nora'}):
            self.assertEqual(serve._director_backfill_map(), {'ada': 'theo', 'zed': 'nora'})

    def test_director_chain_appends_unresolved_tail(self):
        state = {'agentRoster': [{'id': 'a', 'director': 'b'}, {'id': 'b', 'director': 'c'}]}
        self.assertEqual(serve._director_chain(state, 'a'), ['a', 'b', 'c'])

    def test_director_chain_is_cycle_safe(self):
        state = {'agentRoster': [{'id': 'a', 'director': 'b'}, {'id': 'b', 'director': 'a'}]}
        self.assertEqual(len(serve._director_chain(state, 'a')), 2)

    def test_backfill_teams_drops_orphaned_team(self):
        state = {
            'agentRoster': [{'id': 'x', 'name': 'X', 'role': 'Lead', 'isDirector': True},
                            {'id': 'w', 'name': 'W', 'role': 'Worker', 'director': 'x'}],
            'teams': [{'id': 'x', 'directorId': 'old-dir', 'name': 'Old', 'purpose': 'P', 'members': []}],
        }
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            serve._backfill_teams_in_db()
        self.assertEqual(state['teams'], [])
        save.assert_called_once()

    def test_backfill_agent_identity_heals_and_saves(self):
        state = {'agentRoster': [{'id': 'a', 'name': 'A'}], 'agents': {'a': {'role': 'Worker'}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            serve._backfill_agent_identity_in_db()
        save.assert_called_once()

    def test_backfill_agent_identity_no_state(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            serve._backfill_agent_identity_in_db()
        save.assert_not_called()


class PromoteAndTeamRecords(unittest.TestCase):
    def test_promote_to_director_unknown_promotee(self):
        self.assertIsNone(serve._promote_to_director({'agentRoster': []}, 'nobody', 'theo'))

    def test_promote_to_director_updates_existing_team(self):
        state = {
            'agentRoster': [{'id': 'maya', 'name': 'Maya', 'director': 'nora'}],
            'teams': [{'directorId': 'maya', 'name': None, 'purpose': None, 'members': []}],
        }
        team = serve._promote_to_director(state, 'maya', 'nora')
        self.assertEqual(team['directorId'], 'maya')
        self.assertIn('Crew', team['name'])
        self.assertTrue(state['agentRoster'][0]['isDirector'])
        self.assertEqual(state['agentRoster'][0]['director'], 'nora')

    def test_promote_to_director_creates_new_team(self):
        state = {'agentRoster': [{'id': 'maya', 'name': 'Maya'}]}
        team = serve._promote_to_director(state, 'maya', 'nora')
        self.assertEqual(team['id'], 'maya')
        self.assertIn(team, state['teams'])

    def test_can_write_team_false_for_unknown_team(self):
        self.assertFalse(serve._can_write_team({'teams': []}, 'theo', 'nope'))

    def test_can_write_team_admin_can_write(self):
        state = {'agentRoster': [{'id': 'theo', 'isAdmin': True},
                                 {'id': 'x', 'isDirector': True, 'director': 'theo'},
                                 {'id': 'w', 'director': 'x'}],
                 'teams': [{'id': 'x', 'directorId': 'x', 'members': []}]}
        self.assertTrue(serve._can_write_team(state, 'theo', 'x'))

    def test_team_record_returns_derived_members_copy(self):
        state = {'agentRoster': [{'id': 'x', 'isDirector': True, 'director': None},
                                 {'id': 'w', 'director': 'x'}],
                 'teams': [{'id': 'x', 'name': 'Team X', 'members': []}]}
        rec = serve._team_record(state, 'x')
        self.assertEqual(rec['id'], 'x')
        self.assertEqual(rec['members'], ['w'])
        self.assertEqual(state['teams'][0]['members'], [], 'original team record must not be mutated')

    def test_team_record_unknown(self):
        self.assertIsNone(serve._team_record({'teams': []}, 'nope'))

    def test_teams_missing_scrum_master_ignores_unknown_team(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim._team_member_count = lambda state, did: 5
        state = {'teams': [{'id': 't1', 'directorId': 'nora'}]}
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            self.assertEqual(serve._teams_missing_scrum_master(state, ['ghost-team']), [])

    def test_teams_missing_scrum_master_flags_big_team(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim._team_member_count = lambda state, did: 5
        state = {'teams': [{'id': 't1', 'directorId': 'nora', 'name': 'Team One'}]}
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            missing = serve._teams_missing_scrum_master(state, ['t1'])
        self.assertEqual(missing, [{'id': 't1', 'name': 'Team One'}])

    def test_teams_missing_scrum_master_small_team_ok(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim._team_member_count = lambda state, did: 2
        state = {'teams': [{'id': 't1', 'directorId': 'nora'}]}
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            self.assertEqual(serve._teams_missing_scrum_master(state, ['t1']), [])


class CredentialAndDeviceHelpers(unittest.TestCase):
    def test_higgsfield_configured_true_when_both_keys(self):
        with unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', 'id'), \
             unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec'):
            self.assertTrue(serve._higgsfield_configured())

    def test_higgsfield_configured_false_without_keys(self):
        with unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', ''), \
             unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec'):
            self.assertFalse(serve._higgsfield_configured())

    def test_server_secret_generates_and_persists(self):
        tmp = tempfile.mkdtemp(prefix='gap-secret-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with unittest.mock.patch.object(serve, 'THINK_TANK_DIR', tmp), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}):
            key = serve._get_or_create_server_secret()
        self.assertEqual(len(key), 64)
        with open(os.path.join(tmp, '.env')) as f:
            self.assertIn(f'SERVER_SECRET={key}', f.read())

    def test_server_secret_returns_existing(self):
        with unittest.mock.patch.object(serve, '_load_env', return_value={'SERVER_SECRET': 'existing'}):
            self.assertEqual(serve._get_or_create_server_secret(), 'existing')

    def test_admin_credentials_generate_and_persist(self):
        tmp = tempfile.mkdtemp(prefix='gap-admin-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with unittest.mock.patch.object(serve, 'THINK_TANK_DIR', tmp), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}):
            username, salt, digest, password = serve._get_or_create_admin_credentials()
        self.assertEqual(username, 'admin')
        self.assertTrue(password)
        self.assertTrue(salt)
        self.assertTrue(digest)
        with open(os.path.join(tmp, '.env')) as f:
            content = f.read()
        self.assertIn('ADMIN_USERNAME=admin', content)
        self.assertIn('ADMIN_PASSWORD_SALT=', content)

    def test_admin_credentials_return_existing(self):
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'ADMIN_PASSWORD_SALT': 's', 'ADMIN_PASSWORD_HASH': 'h'}):
            self.assertEqual(serve._get_or_create_admin_credentials(), ('admin', 's', 'h', None))

    def test_device_key_generates_and_persists(self):
        tmp = tempfile.mkdtemp(prefix='gap-device-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with unittest.mock.patch.object(serve, 'THINK_TANK_DIR', tmp), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}):
            key, fresh = serve._get_or_create_device_key()
        self.assertTrue(key)
        self.assertEqual(key, fresh)
        with open(os.path.join(tmp, '.env')) as f:
            self.assertIn(f'DEVICE_API_KEY={key}', f.read())

    def test_device_key_returns_existing(self):
        with unittest.mock.patch.object(serve, '_load_env', return_value={'DEVICE_API_KEY': 'existing'}):
            self.assertEqual(serve._get_or_create_device_key(), ('existing', None))


if __name__ == '__main__':
    unittest.main()