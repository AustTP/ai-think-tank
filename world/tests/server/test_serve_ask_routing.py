"""Coverage tests for serve.py cluster D: large-request delegation + backlog
breakdown, player story reject, publish, spike/shadow promote, sprints,
issues, player inbox/email, device check-in, clarify, the ask lane + parking
drain, request routing lanes, pipelines/incidents, products/releases, wiki,
teams, and role templates.

Same isolation contract as test_serve_gap_E.py: the module patches the
derived-time file paths + rate limiting for the whole module, and each test
isolates a fresh in-memory state via patched get_state_from_db. sim.py calls
that need deterministic control are patched per-test (sim.py is outside the
coverage scope, so patching it never costs coverage).
"""
import asyncio
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
from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve
import sim
import content

_MODULE_TMP_DIR = tempfile.mkdtemp(prefix="think-tank-serve-gap-")

_EXTRA_PATCHER = None
_RATE_PATCHER = None


def setUpModule():
    global _MODULE_PATCHER, _EXTRA_PATCHER, _RATE_PATCHER
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, "think-tank.db"),
        THINK_TANK_DIR=os.path.join(_MODULE_TMP_DIR, "think-tank"),
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, "agents"),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, "library"),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, "passport.json"),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    _EXTRA_PATCHER = unittest.mock.patch.multiple(
        serve,
        LIBRARY_ARCHIVE_DIR=os.path.join(_MODULE_TMP_DIR, "library", "archive"),
        LIBRARY_USAGE_PATH=os.path.join(_MODULE_TMP_DIR, "library_usage.json"),
        SANDBOXES_DIR=os.path.join(_MODULE_TMP_DIR, "sandboxes"),
        ESCALATIONS_PATH=os.path.join(_MODULE_TMP_DIR, "escalations.json"),
        BROWSE_TRAIL_PATH=os.path.join(_MODULE_TMP_DIR, "browse_trail.json"),
    )
    _EXTRA_PATCHER.start()
    _RATE_PATCHER = unittest.mock.patch.object(serve, "check_rate_limit", return_value=True)
    _RATE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _RATE_PATCHER.stop()
    _EXTRA_PATCHER.stop()
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _client():
    return TestClient(serve.app)


def _uid(prefix="id"):
    _uid.n += 1
    return f"{prefix}-{_uid.n}"


_uid.n = 0


def _roster_state():
    return {
        "sim": {"owner": "server"},
        "agentRoster": [
            {"id": "agent-1", "name": "Theo", "role": "admin", "isAdmin": True},
            {"id": "agent-2", "name": "Nora", "role": "director", "isDirector": True},
            {"id": "agent-3", "name": "Bo", "role": "worker"},
        ],
        "agents": {
            "agent-1": {"name": "Theo", "busy": False, "offDuty": False, "role": "admin",
                        "profile": {"mission": "run the think tank"}},
            "agent-2": {"name": "Nora", "busy": False, "offDuty": False, "role": "director",
                        "profile": {"mission": "lead the team"}},
            "agent-3": {"name": "Bo", "busy": False, "offDuty": False, "role": "worker",
                        "profile": {"mission": "build things"}},
        },
        "teams": [
            {"id": "team-1", "name": "Alpha", "directorId": "agent-2", "scrumMasterId": "agent-3"},
        ],
        "workQueue": [],
    }


def _player_env(state=None, **patches):
    p = {"get_state_from_db": lambda: state, "verify_session": lambda cookie: True}
    p.update(patches)
    return unittest.mock.patch.multiple(serve, **p)


def _agent_env(state=None, requester="agent-1", key_ok=True):
    return unittest.mock.patch.multiple(
        serve,
        get_state_from_db=lambda: state,
        verify_session=lambda cookie: True,
        _resolve_requester=lambda request: requester,
        verify_agent_key=lambda *a, **k: True if key_ok else "mismatch",
    )


def _post(client, path, payload=None, session=True, content=None):
    if content is not None:
        return client.post(path, content=content,
                           headers={"Content-Type": "application/json"})
    return client.post(path, json=payload or {})


def _queue_work_append(state, items):
    state.setdefault("workQueue", []).extend(items)
    return len(state["workQueue"])


def _loop_fake(script=None, reply="final answer", capture=None):
    def fake(model, messages, tools, execute_tool, max_iterations, max_tokens,
             force_first_tool=None, village_id=None, escalate_model=None,
             conversation_policy=None, **kwargs):
        if capture is not None:
            capture["force_first_tool"] = force_first_tool
            capture["escalate_model"] = escalate_model
        for name, args in (script or []):
            execute_tool(name, args)
        return reply
    return fake


def _wiki_state():
    state = _roster_state()
    state["wiki"] = {
        "pages": {},
        "categories": {
            "operations": {"label": "Operations", "order": 0},
            "think_tank": {"label": "Think Tank", "order": 1},
        },
    }
    return state


class AskCore(unittest.TestCase):
    def _ask_state(self, candidates=("agent-2", "agent-3")):
        state = _roster_state()
        return state

    def _run(self, state, question, agent_id_hint=None, **kw):
        return asyncio.run(serve._ask_core(state, question, agent_id_hint, **kw))

    def test_empty_question(self):
        result = self._run(_roster_state(), "   ")
        self.assertEqual(result["status"], 400)

    def test_requested_pin_valid(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-2", "agent-3"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop", _loop_fake()):
            result = self._run(state, "hello?", agent_id_hint="agent-2")
        self.assertEqual(result["agent"], "agent-2")

    def test_admin_pin_rejected_by_default(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop", _loop_fake()):
            result = self._run(state, "hello?", agent_id_hint="agent-1")
        self.assertEqual(result["agent"], "agent-2")

    def test_admin_pin_allowed(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop", _loop_fake()):
            result = self._run(state, "hello?", agent_id_hint="agent-1", allow_admin_pin=True)
        self.assertEqual(result["agent"], "agent-1")

    def test_no_candidates_no_park(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=[]):
            result = self._run(state, "hello?", allow_park=False)
        self.assertEqual(result["status"], 409)

    def test_no_candidates_park(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=[]):
            result = self._run(state, "hello?")
        self.assertTrue(result["queued"])
        self.assertEqual(len(state["_pendingAsks"]), 1)

    def test_candidates_first(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-3", "agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop", _loop_fake()):
            result = self._run(state, "hello?")
        self.assertEqual(result["agent"], "agent-3")

    def test_security_role_tools(self):
        state = self._ask_state()
        state["agents"]["agent-4"] = {
            "name": "Red", "busy": False, "offDuty": False, "role": "Red Team Auditor",
            "profile": {"mission": "test security", "instructions": ["run curl", "ask handle"]}}
        state["agentRoster"].append({"id": "agent-4", "name": "Red",
                                     "role": "Red Team Auditor"})
        script = [("attempt_curl", {"url": "https://x.com"}),
                  ("request_capability_handle", {"credentialName": "c"})]
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-4"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop", _loop_fake(script)), \
             unittest.mock.patch.object(serve, "_http_json", return_value={"ok": True}):
            result = self._run(state, "run the boundary test")
        self.assertEqual(result["agent"], "agent-4")
        self.assertIn("attempt_curl", result["tools"])
        self.assertIn("request_capability_handle", result["tools"])

    def test_digest_and_peer_review_tools(self):
        state = self._ask_state()
        script = [
            ("team_digest", {}),
            ("read_peer_reviews", {"targetAgentId": "agent-2"}),
            ("read_peer_reviews", {"targetAgentId": "agent-2", "filename": "peer.md"}),
        ]
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop", _loop_fake(script)), \
             unittest.mock.patch.object(serve, "_team_digest_text", return_value="digest"), \
             unittest.mock.patch.object(serve, "_http_json",
                                        side_effect=[{"files": [{"path": "reports/peer.md"}]},
                                                     {"content": "note content"}]):
            result = self._run(state, "how is the team?")
        self.assertEqual(result["reply"], "final answer")
        self.assertIn("team_digest", result["tools"])

    def test_peer_review_missing_target(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop",
                                        _loop_fake([("read_peer_reviews", {})])):
            result = self._run(state, "question")
        self.assertEqual(result["reply"], "final answer")
        self.assertIn("read_peer_reviews", result["tools"])

    def test_peer_review_no_reports(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop",
                                        _loop_fake([("read_peer_reviews",
                                                     {"targetAgentId": "agent-2"})])), \
             unittest.mock.patch.object(serve, "_http_json", return_value={"files": []}):
            result = self._run(state, "question")
        self.assertEqual(result["reply"], "final answer")
        self.assertIn("read_peer_reviews", result["tools"])

    def test_peer_review_error_and_fallback(self):
        state = self._ask_state()
        script = [
            ("read_peer_reviews", {"targetAgentId": "agent-2"}),
            ("read_peer_reviews", {"targetAgentId": "agent-2"}),
        ]
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop", _loop_fake(script)), \
             unittest.mock.patch.object(serve, "_http_json",
                                        side_effect=[{"error": "denied"}, {"foo": "bar"}]):
            result = self._run(state, "question")
        self.assertEqual(result["reply"], "final answer")

    def test_unknown_tool_raises_500(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop",
                                        _loop_fake([("bogus", {})])):
            result = self._run(state, "question")
        self.assertEqual(result["status"], 500)
        self.assertIn("ask failed", result["error"])

    def test_model_none_fallback_reply(self):
        state = self._ask_state()
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value=None):
            result = self._run(state, "question")
        self.assertIn("couldn't settle", result["reply"])

    def test_force_first_tool_trending(self):
        state = self._ask_state()
        capture = {}
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop",
                                        _loop_fake([("x_trending_topics", {})], capture=capture)), \
             unittest.mock.patch.object(content, "_spike_wants_x_trending", return_value=True), \
             unittest.mock.patch.object(content, "_make_treg_tools_executor",
                                        return_value=lambda name, args: "trends"):
            result = self._run(state, "what is trending on X?")
        self.assertEqual(capture["force_first_tool"], "x_trending_topics")
        self.assertIn("x_trending_topics", result["tools"])

    def test_force_first_tool_linkedin(self):
        state = self._ask_state()
        capture = {}
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop",
                                        _loop_fake([("search_linkedin_posts", {})], capture=capture)), \
             unittest.mock.patch.object(content, "_spike_wants_x_trending", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_linkedin_search",
                                        return_value=True), \
             unittest.mock.patch.object(content, "_make_treg_tools_executor",
                                        return_value=lambda name, args: "posts"):
            result = self._run(state, "recent linkedin posts on AI")
        self.assertEqual(capture["force_first_tool"], "search_linkedin_posts")

    def test_force_first_tool_ais_feed(self):
        state = self._ask_state()
        capture = {}
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop",
                                        _loop_fake([("read_ais_feed", {})], capture=capture)), \
             unittest.mock.patch.object(content, "_spike_wants_x_trending", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_linkedin_search", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_ais_feed", return_value=True), \
             unittest.mock.patch.object(content, "_make_treg_tools_executor",
                                        return_value=lambda name, args: "feed"):
            result = self._run(state, "which ships are off the coast of North Carolina?")
        self.assertEqual(capture["force_first_tool"], "read_ais_feed")
        self.assertIn("read_ais_feed", result["tools"])

    def test_force_first_tool_rss_feed(self):
        state = self._ask_state()
        capture = {}
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop",
                                        _loop_fake([("read_rss_feed", {})], capture=capture)), \
             unittest.mock.patch.object(content, "_spike_wants_x_trending", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_linkedin_search", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_ais_feed", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_rss", return_value=True), \
             unittest.mock.patch.object(content, "_make_treg_tools_executor",
                                        return_value=lambda name, args: "feed"):
            result = self._run(state, "any breaking news?")
        self.assertEqual(capture["force_first_tool"], "read_rss_feed")
        self.assertIn("read_rss_feed", result["tools"])

    def test_force_first_tool_market_feed(self):
        state = self._ask_state()
        capture = {}
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop",
                                        _loop_fake([("read_market_feed", {})], capture=capture)), \
             unittest.mock.patch.object(content, "_spike_wants_x_trending", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_linkedin_search", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_ais_feed", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_rss", return_value=False), \
             unittest.mock.patch.object(content, "_spike_wants_market", return_value=True), \
             unittest.mock.patch.object(content, "_make_treg_tools_executor",
                                        return_value=lambda name, args: "feed"):
            result = self._run(state, "what is bitcoin worth?")
        self.assertEqual(capture["force_first_tool"], "read_market_feed")
        self.assertIn("read_market_feed", result["tools"])



class IntentAsk(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/ask", {"question": "q"})
        self.assertEqual(r.status_code, 401)

    def test_rate_limited(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "check_rate_limit", return_value=False):
            r = _post(_client(), "/api/intent/ask", {"question": "q"})
        self.assertEqual(r.status_code, 429)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/ask", {"question": "q"})
        self.assertEqual(r.status_code, 503)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/ask", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"reply": "hi", "agent": "agent-2",
                                                          "tools": []})):
            r = _post(_client(), "/api/intent/ask", {"question": "q"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reply"], "hi")

    def test_error_result(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"error": "boom", "status": 400})):
            r = _post(_client(), "/api/intent/ask", {"question": "q"})
        self.assertEqual(r.status_code, 400)

    def test_queued_result(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"queued": True, "askId": "ask-1",
                                                          "reply": "queued"})):
            r = _post(_client(), "/api/intent/ask", {"question": "q"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["queued"])



class PendingAskDrainPass(unittest.TestCase):
    def setUp(self):
        serve._pending_ask_inflight.clear()
        serve._take_pending_ask_results()

    def _state(self):
        state = _roster_state()
        state["_pendingAsks"] = [{"id": "ask-1", "question": "q", "ts": 1}]
        return state

    def _wait_results(self):
        for _ in range(200):
            out = serve._take_pending_ask_results()
            if out:
                return out
            time.sleep(0.01)
        return []

    def test_no_pending(self):
        serve._pending_ask_drain_pass(_roster_state())

    def test_inflight_guard(self):
        state = self._state()
        serve._pending_ask_inflight.add("ask-1")
        serve._pending_ask_drain_pass(state)
        self.assertEqual(serve._take_pending_ask_results(), [])

    def test_requested_in_candidates(self):
        state = self._state()
        state["_pendingAsks"][0]["agentId"] = "agent-2"
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-2", "agent-3"]), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"reply": "x", "agent": "agent-2"})):
            serve._pending_ask_drain_pass(state)
        results = self._wait_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["askId"], "ask-1")

    def test_candidates_first(self):
        state = self._state()
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-3"]), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"reply": "x", "agent": "agent-3"})):
            serve._pending_ask_drain_pass(state)
        results = self._wait_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["agentId"], "agent-3")

    def test_no_candidates(self):
        state = self._state()
        with unittest.mock.patch.object(sim, "_eligible_candidates", return_value=[]):
            serve._pending_ask_drain_pass(state)
        self.assertEqual(serve._take_pending_ask_results(), [])

    def test_ask_core_error_result(self):
        state = self._state()
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"error": "boom"})):
            serve._pending_ask_drain_pass(state)
        results = self._wait_results()
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])



class PendingAskDrainLoop(unittest.TestCase):
    def test_loop_iterations(self):
        state = _roster_state()
        real_sleep = asyncio.sleep

        def fake_sleep(seconds):
            nonlocal calls
            calls += 1
            if calls >= 4:
                raise KeyboardInterrupt
            return real_sleep(0)

        calls = 0
        with unittest.mock.patch.object(serve, "get_state_from_db",
                                        side_effect=[state, None, RuntimeError("boom")]), \
             unittest.mock.patch.object(serve, "_pending_ask_drain_pass",
                                        return_value=None), \
             unittest.mock.patch.object(asyncio, "sleep", side_effect=fake_sleep):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(serve._pending_ask_drain_loop())



class ApplyPendingAskResults(unittest.TestCase):
    def setUp(self):
        serve._take_pending_ask_results()

    def test_no_results(self):
        state = _roster_state()
        self.assertEqual(serve._apply_pending_ask_results(state), 0)

    def test_delivered(self):
        state = _roster_state()
        state["_pendingAsks"] = [{"id": "ask-1", "question": "q", "ts": 1}]
        serve._store_pending_ask_result({"askId": "ask-1", "reply": "answer",
                                         "agentId": "agent-2"})
        with unittest.mock.patch.object(sim, "_queue_player_email"):
            n = serve._apply_pending_ask_results(state)
        self.assertEqual(n, 1)
        self.assertEqual(state["_pendingAsks"], [])
        self.assertEqual(state["playerInbox"][0]["answer"], "answer")
        self.assertEqual(state["playerInbox"][0]["agentId"], "agent-2")

    def test_transient_409_keeps_parked(self):
        state = _roster_state()
        state["_pendingAsks"] = [{"id": "ask-2", "question": "q", "ts": 1}]
        serve._store_pending_ask_result({"askId": "ask-2", "error": "busy", "status": 409})
        n = serve._apply_pending_ask_results(state)
        self.assertEqual(n, 0)
        self.assertEqual(len(state["_pendingAsks"]), 1)
        self.assertEqual(state.get("playerInbox"), None)

    def test_real_failure_drops_ask(self):
        state = _roster_state()
        state["_pendingAsks"] = [{"id": "ask-3", "question": "q", "ts": 1}]
        serve._store_pending_ask_result({"askId": "ask-3", "error": "boom"})
        with unittest.mock.patch.object(sim, "_queue_player_email"):
            n = serve._apply_pending_ask_results(state)
        self.assertEqual(n, 1)
        self.assertEqual(state["_pendingAsks"], [])
        self.assertEqual(state["playerInbox"][0]["answer"], "boom")
        self.assertIsNone(state["playerInbox"][0]["agentId"])

    def test_unknown_ask_skipped(self):
        state = _roster_state()
        state["_pendingAsks"] = [{"id": "ask-4", "question": "q", "ts": 1}]
        serve._store_pending_ask_result({"askId": "ghost", "reply": "x"})
        self.assertEqual(serve._apply_pending_ask_results(state), 0)



class TeamDigestText(unittest.TestCase):
    class _Cursor:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.row if isinstance(self.row, list) else []

    class _Conn:
        def __init__(self, review_row, tape_row):
            self.review_row = review_row
            self.tape_row = tape_row

        def execute(self, sql, *args):
            if "weekly_reviews" in sql:
                return TeamDigestText._Cursor(self.review_row)
            if "action_log" in sql:
                return TeamDigestText._Cursor([])  # no actions -> empty fallback
            return TeamDigestText._Cursor(self.tape_row)

    def _patched_db(self, review_row, tape_row):
        @contextmanager
        def fake_db():
            yield TeamDigestText._Conn(review_row, tape_row)
        return unittest.mock.patch.object(serve, "_db", fake_db)

    def test_no_review_row(self):
        with self._patched_db(None, (3, 2)):
            out = serve._team_digest_text()
        # No weekly review yet -> live fallback from the action log, not a
        # bare "none yet" (agents asked for shipped-work data, not a wall).
        self.assertIn("Live summary from the action log", out)
        self.assertIn("Live signal (last 24h): 3 Jev decisions logged, 2 ok.", out)

    def test_empty_markdown(self):
        with self._patched_db((123, ""), (1, 0)):
            out = serve._team_digest_text()
        self.assertIn("No weekly review has been written yet", out)

    def test_markdown_row(self):
        with self._patched_db((123, "the digest body"), (1, 0)):
            out = serve._team_digest_text()
        self.assertIn("the digest body", out)



class ClassifyDefaults(unittest.TestCase):
    def test_request_lane_known(self):
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("story", 0.9, 0.1)):
            self.assertEqual(serve._classify_request_lane_default({}, "text"), "story")

    def test_request_lane_unknown(self):
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("bogus", 0.9, 0.1)):
            self.assertIsNone(serve._classify_request_lane_default({}, "text"))

    def test_team_no_teams(self):
        self.assertIsNone(serve._classify_team_default({"teams": []}, "text"))

    def test_team_known(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("team-1", 0.9, 0.1)):
            self.assertEqual(serve._classify_team_default(state, "text"), "team-1")

    def test_team_unknown(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("team-9", 0.9, 0.1)):
            self.assertIsNone(serve._classify_team_default(state, "text"))

    def test_room_known(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("observatory", 0.9, 0.1)):
            self.assertEqual(serve._classify_room_default(state, "text"), "observatory")

    def test_room_unknown_falls_back(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("bogus", 0.9, 0.1)):
            self.assertEqual(serve._classify_room_default(state, "text"), "observatory")

    def test_product_no_products(self):
        self.assertIsNone(serve._classify_product_default({}, "text"))

    def test_product_known(self):
        state = _roster_state()
        state["products"] = {"prod-1": {"name": "Atlas"}}
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("prod-1", 0.9, 0.1)):
            self.assertEqual(serve._classify_product_default(state, "text"), "prod-1")

    def test_product_unknown(self):
        state = _roster_state()
        state["products"] = {"prod-1": {"name": "Atlas"}}
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("prod-9", 0.9, 0.1)):
            self.assertIsNone(serve._classify_product_default(state, "text"))

    def test_room_keyword_fast_path_skips_jev(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        side_effect=AssertionError("Jev must not run")):
            self.assertEqual(serve._classify_room_default(state, "write and publish an article"), "pressoffice")

    def test_room_keyword_no_match_falls_to_jev(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("bank", 0.9, 0.1)):
            self.assertEqual(serve._classify_room_default(state, "text"), "bank")

    def test_room_keyword_tie_falls_to_jev(self):
        # "email" (postoffice) and "article" (pressoffice) hit one
        # keyword each: a tie is not a classification, Jev decides.
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("postoffice", 0.9, 0.1)):
            self.assertEqual(serve._classify_room_default(state, "email the article"), "postoffice")

    def test_room_keyword_word_boundary(self):
        # 'art' is a media keyword but must not match inside
        # 'article' -- with real boundaries pressoffice wins
        # outright and Jev never runs.
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        side_effect=AssertionError("Jev must not run")):
            self.assertEqual(serve._classify_room_default(state, "an article draft"), "pressoffice")

    def test_product_name_pin_skips_jev(self):
        state = _roster_state()
        state["products"] = {"prod-1": {"name": "Atlas"}, "prod-2": {"name": "Boreal"}}
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        side_effect=AssertionError("Jev must not run")):
            self.assertEqual(serve._classify_product_default(state, "Atlas is down for everyone"), "prod-1")

    def test_product_ambiguous_name_falls_to_jev(self):
        # "Atlas Pro" contains both product names verbatim: ambiguous,
        # so the Jev choice (here pinned to prod-2) decides.
        state = _roster_state()
        state["products"] = {"prod-1": {"name": "Atlas"}, "prod-2": {"name": "Atlas Pro"}}
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("prod-2", 0.9, 0.1)):
            self.assertEqual(serve._classify_product_default(state, "Atlas Pro is down"), "prod-2")

    def test_product_short_name_not_pinned(self):
        # Names under 4 chars never pin (too false-positive-prone).
        state = _roster_state()
        state["products"] = {"prod-1": {"name": "AI"}}
        with unittest.mock.patch.object(serve, "_jev_quorum_choice_sync",
                                        return_value=("prod-1", 0.9, 0.1)):
            self.assertEqual(serve._classify_product_default(state, "the AI is down"), "prod-1")



class ExtractScheduleFields(unittest.TestCase):
    def test_http_json_raises(self):
        with unittest.mock.patch.object(serve, "_http_json", side_effect=RuntimeError):
            self.assertIsNone(serve._extract_schedule_fields_sync("a", "k", "text"))

    def test_non_dict(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value="junk"):
            self.assertIsNone(serve._extract_schedule_fields_sync("a", "k", "text"))

    def test_error_result(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value={"error": "x"}):
            self.assertIsNone(serve._extract_schedule_fields_sync("a", "k", "text"))

    def test_bad_json(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value={"reply": "nope"}):
            self.assertIsNone(serve._extract_schedule_fields_sync("a", "k", "text"))

    def test_missing_fields(self):
        with unittest.mock.patch.object(serve, "_http_json",
                                        return_value={"reply": json.dumps({"topic": "x"})}):
            self.assertIsNone(serve._extract_schedule_fields_sync("a", "k", "text"))

    def test_success_with_fences(self):
        reply = '```json\n{"topic": "watch x", "startUrl": "https://example.com", "cadenceMs": 3600000}\n```'
        with unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_http_json", return_value={"reply": reply}):
            out = serve._extract_schedule_fields_sync("a", "k", "text")
        self.assertEqual(out["topic"], "watch x")
        self.assertEqual(out["cadenceMs"], 3600000)



class PickTeamWorker(unittest.TestCase):
    def test_on_call(self):
        with unittest.mock.patch.object(sim, "on_call_agent", return_value="agent-3"):
            self.assertEqual(serve._pick_team_worker(_roster_state(), "agent-2"), "agent-3")



class RouteLaneAsk(unittest.TestCase):
    def test_queued_saves(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"queued": True, "askId": "a"})), \
             unittest.mock.patch.object(serve, "save_state_to_db") as save:
            result = asyncio.run(serve._route_lane_ask(state, "text", "agent-1"))
        self.assertTrue(result["queued"])
        save.assert_called_once()

    def test_not_queued(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"reply": "y", "queued": False})), \
             unittest.mock.patch.object(serve, "save_state_to_db") as save:
            result = asyncio.run(serve._route_lane_ask(state, "text", "agent-1"))
        self.assertEqual(result["reply"], "y")
        save.assert_not_called()



class RouteLaneUnclear(unittest.TestCase):
    def test_uses_authority_pin(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_unclear_lane_authority",
                                        return_value={"id": "agent-2"}), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"queued": False, "reply": "y"})) as ask:
            result = asyncio.run(serve._route_lane_unclear(state, "text", "agent-1"))
        self.assertEqual(result["reply"], "y")
        ask.assert_awaited_once()



class UnclearLaneAuthority(unittest.TestCase):
    def test_senior_director_first(self):
        state = _roster_state()
        self.assertEqual(serve._unclear_lane_authority(state)["id"], "agent-2")

    def test_admin_fallback(self):
        state = _roster_state()
        state["agents"]["agent-2"]["busy"] = True
        self.assertEqual(serve._unclear_lane_authority(state)["id"], "agent-1")

    def test_none_when_all_busy(self):
        state = _roster_state()
        state["agents"]["agent-2"]["busy"] = True
        state["agents"]["agent-1"]["busy"] = True
        self.assertIsNone(serve._unclear_lane_authority(state))



class RouteLaneSchedule(unittest.TestCase):
    def test_no_fields(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_fields_sync",
                                        return_value=None):
            result = asyncio.run(serve._route_lane_schedule(state, "text", "agent-1"))
        self.assertIn("couldn't pin down", result["reply"])

    def test_add_research_topic_none(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_fields_sync",
                                        return_value={"topic": "x",
                                                      "startUrl": "https://example.com",
                                                      "cadenceMs": 3600000}), \
             unittest.mock.patch.object(sim, "add_research_topic", return_value=None):
            result = asyncio.run(serve._route_lane_schedule(state, "text", "agent-1"))
        self.assertIn("didn't look like a real link", result["reply"])

    def test_success_daily(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_fields_sync",
                                        return_value={"topic": "x",
                                                      "startUrl": "https://example.com",
                                                      "cadenceMs": 3600000}), \
             unittest.mock.patch.object(sim, "add_research_topic",
                                        return_value={"id": "rt-1", "topic": "x",
                                                      "startUrl": "https://example.com",
                                                      "cadenceMs": 3600000}):
            result = asyncio.run(serve._route_lane_schedule(state, "text", "agent-1"))
        self.assertIn("every 1.0h", result["reply"])

    def test_success_weekly(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_fields_sync",
                                        return_value={"topic": "x",
                                                      "startUrl": "https://example.com",
                                                      "cadenceMs": 7 * 24 * 3600000}), \
             unittest.mock.patch.object(sim, "add_research_topic",
                                        return_value={"id": "rt-1", "topic": "x",
                                                      "startUrl": "https://example.com",
                                                      "cadenceMs": 7 * 24 * 3600000}):
            result = asyncio.run(serve._route_lane_schedule(state, "text", "agent-1"))
        self.assertIn("every 7.0d", result["reply"])



class RouteLaneSpike(unittest.TestCase):
    def test_queued_with_team(self):
        state = _roster_state()
        state["workQueue"] = [{"id": "w-1"}]
        with unittest.mock.patch.object(serve, "_team_decider", return_value="team-1"), \
             unittest.mock.patch.object(serve, "_classify_room_default",
                                        return_value="observatory"), \
             unittest.mock.patch.object(sim, "queue_spike", return_value={"id": "spike-1"}), \
             unittest.mock.patch.object(sim, "on_call_agent", return_value="agent-3"):
            result = asyncio.run(serve._route_lane_spike(state, "investigate x", "agent-1"))
        self.assertIn("with team-1's team", result["reply"])
        self.assertEqual(state["workQueue"][-1]["assignedTo"], "agent-3")
        self.assertTrue(state["workQueue"][-1]["directRoute"])

    def test_queued_no_team(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_team_decider", return_value=None), \
             unittest.mock.patch.object(serve, "_classify_room_default",
                                        return_value="observatory"), \
             unittest.mock.patch.object(sim, "queue_spike", return_value={"id": "spike-1"}):
            result = asyncio.run(serve._route_lane_spike(state, "investigate x", "agent-1"))
        self.assertNotIn("team's team", result["reply"])

    def test_queue_failed(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_team_decider", return_value="team-1"), \
             unittest.mock.patch.object(serve, "_classify_room_default",
                                        return_value="observatory"), \
             unittest.mock.patch.object(sim, "queue_spike", return_value=None):
            result = asyncio.run(serve._route_lane_spike(state, "investigate x", "agent-1"))
        self.assertIn("couldn't queue", result["reply"])



class RouteLaneStory(unittest.TestCase):
    def test_no_team_falls_back_to_unclear(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_team_decider", return_value=None), \
             unittest.mock.patch.object(serve, "_unclear_lane_authority",
                                        return_value={"id": "agent-2"}), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"queued": False, "reply": "y"})):
            result = asyncio.run(serve._route_lane_story(state, "build x", "agent-1"))
        self.assertEqual(result["reply"], "y")

    def test_file_issue_none_falls_back(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_team_decider", return_value="team-1"), \
             unittest.mock.patch.object(sim, "file_issue", return_value=None), \
             unittest.mock.patch.object(serve, "_unclear_lane_authority",
                                        return_value={"id": "agent-2"}), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"queued": False, "reply": "y"})):
            result = asyncio.run(serve._route_lane_story(state, "build x", "agent-1"))
        self.assertEqual(result["reply"], "y")

    def test_success(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_team_decider", return_value="team-1"), \
             unittest.mock.patch.object(sim, "file_issue",
                                        return_value={"key": "ALPHA-1", "summary": "s"}):
            result = asyncio.run(serve._route_lane_story(state, "build x", "agent-1"))
        self.assertIn("ALPHA-1", result["reply"])
