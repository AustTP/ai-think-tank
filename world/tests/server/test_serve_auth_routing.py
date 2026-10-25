"""Split part of test_serve.py (serve.py behavior tests)."""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse

import asyncio
import openpyxl

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import sim  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402


class ChatEndpointAuth(unittest.TestCase):
    """/api/chat guards its own OpenRouter spend by EITHER a player session OR a
    valid agent key (the server's own assign-big-task planning loopback carries
    only X-Agent-Key). A request with neither must fail closed (401); each valid
    credential alone must pass."""

    def test_chat_requires_session_or_agent_key(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync') as model:
            c = TestClient(serve.app)
            r = c.post('/api/chat', json={'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}], 'agentId': 'faye'})
        self.assertEqual(r.status_code, 401)
        model.assert_not_called()

    def test_chat_accepts_valid_agent_key_without_session(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'content': 'ok'}}]}), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/chat', json={'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}], 'agentId': 'faye'},
                       headers={'X-Agent-Key': 'real-agent-key'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['reply'], 'ok')

    def test_protected_endpoint_accepts_valid_agent_key_loopback(self):
        # Server content executors loopback to protected endpoints carrying only
        # an agent key (no session cookie). A valid stored key must pass the
        # session middleware so the pipeline/execute/library loopbacks work;
        # with no session AND no key it must still 401.
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True):
            r = c.get('/api/sim/status')
        self.assertEqual(r.status_code, 200, 'a valid agent key is a credential for a loopback')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=False):
            r2 = c.get('/api/sim/status')
        self.assertEqual(r2.status_code, 401, 'no session and no key fails closed')

    def test_decisions_and_player_inbox_require_credentials(self):
        # Regression: /api/decisions (JEV decision tape) and
        # /api/player-inbox (read + the responding WRITE) were absent from
        # AUTH_PROTECTED_PREFIXES, so anonymous callers could read the tape and
        # -- worse -- the respond endpoint could mutate state with no auth at
        # all. The prefix list must now bounce all three without credentials.
        c = TestClient(serve.app)
        for path in ('/api/decisions', '/api/player-inbox', '/api/player-inbox/ask-1/respond'):
            with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
                 unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=False):
                r = c.get(path) if 'respond' not in path else c.post(path, json={'answer': 'x'})
            self.assertEqual(r.status_code, 401, f'{path} must require a credential')

    def test_player_inbox_respond_rejects_valid_agent_key(self):
        # The respond endpoint is PLAYER-ONLY: a valid agent key must NOT be able
        # to answer an awaiting question, because doing so would let an agent
        # unblock its own stalled work around the human-in-the-loop gate.
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True):
            r = c.post('/api/player-inbox/ask-1/respond', json={'answer': 'x'})
        self.assertEqual(r.status_code, 401, 'an agent key is not a player credential here')

    def test_intent_surface_is_operator_gated(self):
        # Regression: every /api/intent/* write handler runs the OPERATOR
        # gate (_operator_gate: a valid player session, or an agent who
        # is a director/admin). An anonymous caller and a worker's key
        # both get the gate's bare 401 before any body parsing, so a
        # bare POST with a bogus path param is still 401; a
        # director/admin key passes the gate and meets the route's own
        # field validation (which is never 401). (The team-prefix
        # endpoint is operator-gated too -- it lives in the test below.)
        c = TestClient(serve.app, raise_server_exceptions=False)
        paths = [
            ('/api/intent/assign-big-task', {}),
            ('/api/intent/story/story-1/reject', {}),
            ('/api/intent/publish', {}),
            ('/api/intent/spike/spike-done/promote', {}),
            ('/api/intent/sprint', {}),
            ('/api/intent/sprint/spr-1/close', {}),
            ('/api/intent/issues', {}),
            ('/api/intent/issues/KEY-1/status', {}),
            ('/api/intent/clarify', {}),
            ('/api/intent/ask', {}),
            ('/api/intent/schedule', {}),
            ('/api/intent/incidents', {}),
            ('/api/intent/product', {}),
            ('/api/intent/product/p1/release', {}),
        ]
        for path, body in paths:
            with self.subTest(path=path, who='anonymous'), \
                 unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
                 unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=False):
                r = c.post(path, json=body)
            self.assertEqual(r.status_code, 401,
                             f'{path} must reject an anonymous caller')
            # The require_login middleware fires first with no
            # credential at all.
            self.assertEqual(r.json()['error'], 'Unauthorized -- please log in')
            with self.subTest(path=path, who='worker'), \
                 unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
                 unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True), \
                 unittest.mock.patch.object(serve, '_resolve_requester', return_value='worker'), \
                 unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=False):
                r = c.post(path, json=body)
            self.assertEqual(r.status_code, 401,
                             f'{path} must reject a worker key as an operator')
            self.assertEqual(r.json()['error'], 'Unauthorized')
            with self.subTest(path=path, who='director'), \
                 unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
                 unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True), \
                 unittest.mock.patch.object(serve, '_resolve_requester', return_value='nora'), \
                 unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=True):
                r = c.post(path, json=body)
            self.assertNotEqual(r.status_code, 401,
                                f'{path} must admit a director/admin through the gate')

    def test_team_prefix_rejects_non_director_agent_key(self):
        # Team-prefix is OPERATOR-gated (player session or director/admin), so
        # a valid agent key is a credential here -- but only for a rostered
        # director. An ordinary worker's key must still be refused; the error
        # is the gate's bare "Unauthorized", not the player-only "log in".
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='worker'), \
             unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=False):
            r = c.post('/api/teams/dev/prefix', json={})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error'], 'Unauthorized')

    def test_require_player_session_accepts_only_a_real_session(self):
        # The gate is exactly "a valid session cookie", nothing else: an agent
        # key is never consulted by the handler-level check, and a missing/
        # unverifiable cookie fails closed.
        req = unittest.mock.Mock()
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            self.assertFalse(serve._require_player_session(req))
            req.cookies.get.assert_called_once_with(serve.SESSION_COOKIE_NAME)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            self.assertTrue(serve._require_player_session(req))
        # A valid key presented in the header is irrelevant to this check.
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True):
            self.assertFalse(serve._require_player_session(req))

    def test_high_tier_chat_call_accrues_monthly_budget_exactly_once(self):
        # Regression: the /api/chat choke point accrued a high-tier
        # call to the monthly budget TWICE via two duplicate blocks, so a $2/mo
        # allowance was exhausted in half the intended calls. A single high-tier
        # call must land exactly once in the kv_spend high-tier monthly series.
        calls = []
        def fake_accrue(cost):
            calls.append(cost)
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_high_tier_slug', return_value='expensive-model'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'content': 'ok'}}],
                                                      'usage': {'cost': 0.25}}), \
             unittest.mock.patch.object(serve, '_accrue_spend'), \
             unittest.mock.patch.object(serve, '_accrue_high_tier_spend', side_effect=fake_accrue), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/chat', json={'model': 'expensive-model',
                                          'messages': [{'role': 'user', 'content': 'x'}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(calls), 1, 'a high-tier call accrues to the monthly budget exactly once')
        self.assertEqual(calls, [0.25])


class OperationalGuardrails(unittest.TestCase):
    def test_backup_creates_rotated_snapshots(self):
        # _backup_think_tank_db must snapshot the DB into DB_BACKUP_DIR with a .bak
        # extension and prune to DB_BACKUP_KEEP newest. Point both paths at a
        # throwaway dir so the REAL think_tank.db is never touched.
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, 'the.db')
            bakdir = os.path.join(td, 'backups')
            with sqlite3.connect(src) as c:
                c.execute('CREATE TABLE t (x INTEGER)')
                c.execute('INSERT INTO t VALUES (1)')
            with unittest.mock.patch.object(serve, 'DB_BACKUP_DIR', bakdir), \
                 unittest.mock.patch.object(serve, 'DB_BACKUP_KEEP', 2), \
                 unittest.mock.patch.object(serve, 'DB_PATH', src):
                serve._backup_think_tank_db()
                serve._backup_think_tank_db()
                serve._backup_think_tank_db()
            snaps = sorted(os.listdir(bakdir))
            self.assertEqual(len(snaps), 2, 'pruned to DB_BACKUP_KEEP')
            self.assertTrue(all(f.endswith('.bak') for f in snaps))
            # newest snapshot must contain the same row (a real backup).
            newest = os.path.join(bakdir, snaps[-1])
            with sqlite3.connect(newest) as c:
                self.assertEqual(c.execute('SELECT x FROM t').fetchone()[0], 1)

    def test_idle_shutdown_goes_dormant_when_exceeded(self):
        # Sleep-not-die: once no request has arrived for >= _MAX_IDLE_MINUTES,
        # the watcher must go DORMANT (think tank paused, process stays bound) --
        # NOT exit, so a remote request can still wake it. Use a tiny poll so
        # the test returns fast; simulate "no request for a while" by backdating
        # _LAST_REQUEST_TIME. Runs to a timeout so the infinite poll loop exits.
        old_last = serve._LAST_REQUEST_TIME
        old_poll = serve._dormant()  # preserve flag across runs
        serve._set_dormant(False)
        went_dormant = False
        try:
            serve._LAST_REQUEST_TIME = time.time() - (5 * 60)
            with unittest.mock.patch.object(serve, '_MAX_IDLE_MINUTES', 1):
                async def run():
                    # Let the loop poll once, then stop it.
                    import asyncio as _a
                    task = _a.create_task(serve._idle_shutdown_loop(poll_s=0.01))
                    try:
                        await _a.wait_for(task, timeout=0.5)
                    except _a.TimeoutError:
                        task.cancel()
                        try:
                            await task
                        except (_a.CancelledError, Exception):
                            pass
                asyncio.run(run())
                went_dormant = serve._dormant()
        finally:
            serve._LAST_REQUEST_TIME = old_last
            serve._set_dormant(old_poll)
        self.assertTrue(went_dormant, 'idle watcher must put the think tank dormant, not exit')


class LatencyRouting(unittest.TestCase):
    def test_pulse_and_triage_skip_jev(self):
        with unittest.mock.patch.object(serve, '_low_tier_slug', return_value='fast-model'), \
             unittest.mock.patch.object(serve, '_tier_gate_decider',
                                        side_effect=AssertionError('JEV must not run')):
            self.assertEqual(serve._resolve_model_tier('pulse triage', 'spike'), 'fast-model')
            self.assertEqual(serve._resolve_model_tier('routine report', 'pulse'), 'fast-model')
            self.assertEqual(serve._resolve_model_tier('live alert check', None), 'fast-model')


class BestCapabilityRouting(unittest.TestCase):
    def test_best_effort_uses_high_when_available(self):
        with unittest.mock.patch.object(serve, '_high_tier_slug', return_value='best-model'), \
             unittest.mock.patch.object(serve, '_high_tier_budget_exceeded', return_value=False):
            self.assertEqual(serve._resolve_model_tier('hard analysis', best_effort=True),
                             'best-model')

    def test_best_effort_falls_back_when_high_budget_is_spent(self):
        with unittest.mock.patch.object(serve, '_high_tier_slug', return_value='best-model'), \
             unittest.mock.patch.object(serve, '_mid_tier_slug', return_value='mid-model'), \
             unittest.mock.patch.object(serve, '_high_tier_budget_exceeded', return_value=True):
            self.assertEqual(serve._resolve_model_tier('hard analysis', best_effort=True),
                             'mid-model')
