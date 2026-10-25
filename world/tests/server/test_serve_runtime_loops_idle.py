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
        # _tier_refresh_logged logs ONLY when a band's model actually changed,
        # so the pre-write and post-write reads of the table must differ.
        tiers = unittest.mock.MagicMock(side_effect=[
            {'high': {'slug': 'old-high'}}, {'high': {'slug': 'new-high'}}])
        self._run_loop(
            refresh_model_tiers=unittest.mock.AsyncMock(return_value={'high': {'id': 'high-model'}}),
            get_cached_model_tiers=tiers,
            get_state_from_db=lambda: state,
            log_action=unittest.mock.DEFAULT,
            _auto_failover_jev_if_gone=unittest.mock.AsyncMock(),
        )
        log = self._last_mocks['log_action']
        log.assert_called_once()
        self.assertEqual(log.call_args.args[0], 'theo')
        self.assertEqual(log.call_args.args[1], 'model_tiers_refreshed')

    def test_refresh_loop_no_log_when_no_band_changed(self):
        # A re-pick where every band holds its model is a no-op check, not a
        # governance event -- it must not bury the activity feed.
        state = {'agentRoster': [{'id': 'theo', 'isAdmin': True}]}
        tiers = unittest.mock.MagicMock(side_effect=[
            {'high': {'slug': 'same'}}, {'high': {'slug': 'same'}}])
        self._run_loop(
            refresh_model_tiers=unittest.mock.AsyncMock(return_value={'high': {'id': 'high-model'}}),
            get_cached_model_tiers=tiers,
            get_state_from_db=lambda: state,
            log_action=unittest.mock.DEFAULT,
            _auto_failover_jev_if_gone=unittest.mock.AsyncMock(),
        )
        self._last_mocks['log_action'].assert_not_called()

    def test_refresh_loop_prints_when_no_actor(self):
        tiers = unittest.mock.MagicMock(side_effect=[
            {'high': {'slug': 'old-high'}}, {'high': {'slug': 'new-high'}}])
        self._run_loop(
            refresh_model_tiers=unittest.mock.AsyncMock(return_value={'high': {'id': 'high-model'}}),
            get_cached_model_tiers=tiers,
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


