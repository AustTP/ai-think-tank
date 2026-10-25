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
        social_dir = os.path.join(_MODULE_TMP_DIR, 'library', 'social')
        shutil.rmtree(social_dir, ignore_errors=True)
        serve.log_action('ada', 'task_completed', {'title': 'Weekly Bank Report'}, authorized=False)
        state = {
            'agents': {'ada': {'name': 'Ada', 'weekApprovals': 3}},
            'agentRoster': [{'id': 'ada', 'name': 'Ada'}],
        }
        serve.log_social_digest(state, {'ada': {}}, decisions=[{'agentId': 'ada', 'choice': 'adopt', 'confidence': 0.8}])
        files = os.listdir(social_dir)
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


