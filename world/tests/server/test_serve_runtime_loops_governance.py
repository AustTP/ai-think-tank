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

    def test_stale_dormancy_window_expires_without_paging(self):
        esc = {'e1': {'status': 'pending', 'kind': 'dormancy window request',
                      'question': 'Agent worker requests a dormancy window'}}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()), \
             unittest.mock.patch.object(serve, '_dormancy_window_start',
                                        return_value=time.time() - 3600), \
             unittest.mock.patch.object(serve._selfedit, 'resolve_window_escalation') as resolve, \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'notify_human_of_escalation') as notify, \
             unittest.mock.patch.object(serve, '_save_escalations'):
            self.assertEqual(serve._resolve_pending_escalations_sync(), 0)
        self.assertEqual(esc['e1']['status'], 'denied')
        resolve.assert_called_once_with('e1', False)
        notify.assert_not_called()

    def test_orphaned_dormancy_window_expires(self):
        esc = {'e1': {'status': 'pending', 'kind': 'dormancy window request',
                      'question': 'Agent worker requests a dormancy window',
                      'ts': time.time() - 3600, 'note': 'win-gone'}}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()), \
             unittest.mock.patch.object(serve, '_dormancy_window_start', return_value=None), \
             unittest.mock.patch.object(serve._selfedit, 'resolve_window_escalation') as resolve, \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'notify_human_of_escalation') as notify, \
             unittest.mock.patch.object(serve, '_save_escalations'):
            self.assertEqual(serve._resolve_pending_escalations_sync(), 0)
        self.assertEqual(esc['e1']['status'], 'denied')
        self.assertIn('window record', esc['e1']['resolvedBy'])
        resolve.assert_called_once_with('e1', False)
        notify.assert_not_called()

    def test_stale_escalation_is_denied_after_six_hours(self):
        esc = {'e1': {'status': 'pending', 'kind': 'allowlist candidate',
                      'question': 'host?', 'ts': time.time() - 7 * 3600}}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'notify_human_of_escalation') as notify, \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync') as quorum, \
             unittest.mock.patch.object(serve, '_save_escalations'):
            self.assertEqual(serve._resolve_pending_escalations_sync(), 0)
        self.assertEqual(esc['e1']['status'], 'denied')
        self.assertIn('6 hours', esc['e1']['resolvedBy'])
        notify.assert_not_called()
        quorum.assert_not_called()

    def test_an_unsure_rung_is_not_reasked(self):
        esc = {'e1': {'status': 'pending', 'kind': 'routine', 'question': 'q?',
                      'lastAskedAt': 0, 'askCount': 0}}
        calls = []

        def quorum(instr, criteria, **kwargs):
            calls.append(instr)
            return ('approve', 0.10, 0.0)

        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()), \
             unittest.mock.patch.object(serve, '_escalation_floor', return_value=0.9), \
             unittest.mock.patch.object(serve, '_escalation_judge_drift', return_value=0), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync', side_effect=quorum), \
             unittest.mock.patch.object(serve, '_jev_directory_score', return_value=0.1), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_jev_judge_model', return_value=None), \
             unittest.mock.patch.object(serve, '_escalation_jev_errors', _FakeCounter()), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'notify_human_of_escalation'), \
             unittest.mock.patch.object(serve, '_save_escalations'):
            self.assertEqual(serve._resolve_pending_escalations_sync(), 0)
            self.assertEqual(serve._resolve_pending_escalations_sync(), 0)
        self.assertEqual(len(calls), 1, 'the second pass must move on, not re-ask the same rung')
        self.assertTrue(esc['e1'].get('_seniorTried'))

    def test_high_ask_count_does_not_block_the_next_rung(self):
        esc = {'e1': {'status': 'pending', 'kind': 'routine', 'question': 'q?',
                      'lastAskedAt': 0, 'askCount': serve.DIRECTOR_ESCALATION_MAX_ASKS}}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()), \
             unittest.mock.patch.object(serve, '_escalation_floor', return_value=0.0), \
             unittest.mock.patch.object(serve, '_escalation_judge_drift', return_value=0), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                        return_value=('approve', 0.95, 0.0)), \
             unittest.mock.patch.object(serve, '_jev_directory_score', return_value=0.95), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_jev_judge_model', return_value=None), \
             unittest.mock.patch.object(serve, '_escalation_jev_errors', _FakeCounter()), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_save_escalations'):
            self.assertEqual(serve._resolve_pending_escalations_sync(), 1)

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
                     unittest.mock.patch.object(serve, 'notify_human_of_escalation') as notify, \
                     unittest.mock.patch.object(serve, '_save_escalations',
                                                side_effect=lambda d: saved.update(d)) as save:
                    result = serve._resolve_pending_escalations_sync()
        self.assertEqual(result, 1)
        self.assertEqual(esc['e1']['status'], 'approved')
        save.assert_called_once()
        log.assert_called_once()
        self.assertEqual(log.call_args.args[1], 'escalation_approve')
        notify.assert_not_called()


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


class PeerReviewPass(unittest.TestCase):
    def _log_work(self, agent_id, n):
        for _ in range(n):
            serve.log_action(agent_id, 'task_completed', {'title': 'x'}, authorized=False)

    def test_raises_a_help_signal_not_a_report(self):
        now = time.time()
        state = {
            'agentRoster': [
                {'id': None, 'name': 'Nameless'},
                {'id': 'ghost', 'name': 'Ghost', 'role': 'Worker'},
                {'id': 'maya', 'name': 'Maya', 'role': 'Director', 'isDirector': True},
                {'id': 'busy', 'name': 'Busy', 'role': 'Worker', 'director': 'maya'},
                {'id': 'idle', 'name': 'Idle', 'role': 'Worker', 'director': 'maya'},
            ],
            'agents': {'maya': {'id': 'maya'}, 'busy': {'name': 'Busy'}, 'idle': {'name': 'Idle'}},
            'reports': 'not-a-list',
        }
        self._log_work('busy', 3)
        with unittest.mock.patch.object(serve.random, 'random', return_value=0.0):
            n = serve._peer_review_pass(state, now)
        self.assertEqual(n, 1)
        self.assertEqual(state['reports'], [], 'the machine never writes reports')
        signals = state.get('helpSignals') or []
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['aboutId'], 'idle')
        self.assertEqual(signals[0]['directorId'], 'maya')
        self.assertEqual(signals[0]['direction'], 'quiet')
        mailbox = (state.get('agents') or {}).get('maya', {}).get('mailbox') or []
        self.assertTrue(any(m.get('kind') == 'help_signal' for m in mailbox),
                        'the director is told in-world')

    def test_returns_zero_when_target_has_no_director(self):
        now = time.time()
        state = {
            'agentRoster': [{'id': 'target', 'name': 'T', 'role': 'Worker'}],
            'agents': {'target': {'name': 'T'}},
            'reports': [],
        }
        self._log_work('target', 5)
        with unittest.mock.patch.object(serve.random, 'random', return_value=0.0):
            self.assertEqual(serve._peer_review_pass(state, now), 0)
        self.assertIsNone(state.get('helpSignals'))
        self.assertEqual(state['reports'], [])

    def test_returns_zero_when_village_idle(self):
        state = {'agentRoster': [{'id': 'a', 'name': 'A', 'role': 'W'}],
                 'agents': {'a': {}}, 'reports': []}
        self.assertEqual(serve._peer_review_pass(state, time.time()), 0)


