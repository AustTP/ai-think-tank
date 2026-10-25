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


