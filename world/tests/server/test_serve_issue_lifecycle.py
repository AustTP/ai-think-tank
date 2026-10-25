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


class SetIssueStatus(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/issues/TEAM-1/status", {"status": "done"})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/issues/TEAM-1/status", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/issues/TEAM-1/status", {"status": "done"})
        self.assertEqual(r.status_code, 503)

    def test_unknown(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "set_issue_status", return_value=None):
            r = _post(_client(), "/api/intent/issues/TEAM-1/status", {"status": "done"})
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "set_issue_status",
                                        return_value={"key": "TEAM-1", "status": "done"}):
            r = _post(_client(), "/api/intent/issues/TEAM-1/status", {"status": "done"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["issue"]["status"], "done")



class GetIssueDetail(unittest.TestCase):
    def test_no_state(self):
        with _player_env(None):
            r = _client().get("/api/intent/issues/TEAM-1")
        self.assertEqual(r.status_code, 503)

    def test_unknown(self):
        with _player_env(_roster_state()):
            r = _client().get("/api/intent/issues/TEAM-1")
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        state = _roster_state()
        state["issues"] = {"TEAM-1": {"key": "TEAM-1", "summary": "s"}}
        state["playerInbox"] = [{"id": "m-1", "issueKey": "TEAM-1", "question": "q?"}]
        state["_pendingBlockChanges"] = [
            {"issueKey": "TEAM-1", "state": "committed"},
            {"issueKey": "OTHER", "state": "committed"}]
        with _player_env(state):
            r = _client().get("/api/intent/issues/TEAM-1")
        self.assertEqual(r.status_code, 200)
        detail = r.json()["issue"]
        self.assertEqual(len(detail["questions"]), 1)
        self.assertEqual(len(detail["blockLog"]), 1)



class ClaimIssueMet(unittest.TestCase):
    def _issue_state(self):
        state = _roster_state()
        state["issues"] = {"TEAM-1": {"key": "TEAM-1", "teamId": "team-1"}}
        return state

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/issues/TEAM-1/claim-met", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/issues/TEAM-1/claim-met", {})
        self.assertEqual(r.status_code, 503)

    def test_unknown_issue(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/issues/TEAM-1/claim-met", {})
        self.assertEqual(r.status_code, 404)

    def test_no_director(self):
        state = self._issue_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "_issue_director", return_value=None):
            r = _post(_client(), "/api/intent/issues/TEAM-1/claim-met", {})
        self.assertEqual(r.status_code, 400)

    def test_already_pending(self):
        state = self._issue_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "_issue_director", return_value="agent-2"), \
             unittest.mock.patch.object(sim, "request_block_claim_met", return_value=None):
            r = _post(_client(), "/api/intent/issues/TEAM-1/claim-met", {})
        self.assertEqual(r.status_code, 409)

    def test_success(self):
        state = self._issue_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "_issue_director", return_value="agent-2"), \
             unittest.mock.patch.object(sim, "request_block_claim_met",
                                        return_value={"state": "pending_supervisor"}):
            r = _post(_client(), "/api/intent/issues/TEAM-1/claim-met",
                      {"agentId": "agent-3", "context": "built it"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "pending_supervisor")



class BlockIssueDependency(unittest.TestCase):
    def _issue_state(self):
        state = _roster_state()
        state["issues"] = {"TEAM-1": {"key": "TEAM-1", "teamId": "team-1"}}
        return state

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/issues/TEAM-1/block-dependency", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/issues/TEAM-1/block-dependency", {})
        self.assertEqual(r.status_code, 503)

    def test_unknown_issue(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/issues/TEAM-1/block-dependency", {})
        self.assertEqual(r.status_code, 404)

    def test_missing_fields(self):
        with _player_env(self._issue_state()):
            r = _post(_client(), "/api/intent/issues/TEAM-1/block-dependency",
                      {"agentId": "agent-3"})
        self.assertEqual(r.status_code, 400)

    def test_already_pending(self):
        state = self._issue_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "request_block_dependency", return_value=None):
            r = _post(_client(), "/api/intent/issues/TEAM-1/block-dependency",
                      {"agentId": "agent-3", "dependsOnTask": "task-9"})
        self.assertEqual(r.status_code, 409)

    def test_success(self):
        state = self._issue_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "request_block_dependency", return_value="r-1"):
            r = _post(_client(), "/api/intent/issues/TEAM-1/block-dependency",
                      {"agentId": "agent-3", "dependsOnTask": "task-9", "reason": "waiting"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["requestId"], "r-1")



class GetPlayerInbox(unittest.TestCase):
    def test_no_state(self):
        with _player_env(None):
            r = _client().get("/api/player-inbox")
        self.assertEqual(r.status_code, 503)

    def test_success(self):
        state = _roster_state()
        state["playerInbox"] = [
            {"id": "m-1", "createdAt": 1, "question": "old"},
            {"id": "m-2", "createdAt": 2, "question": "new"},
        ]
        with _player_env(state):
            r = _client().get("/api/player-inbox")
        self.assertEqual(r.json()["messages"][0]["id"], "m-2")



class RespondPlayerInbox(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/player-inbox/m-1/respond", {"answer": "ok"})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/player-inbox/m-1/respond", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/player-inbox/m-1/respond", {"answer": "ok"})
        self.assertEqual(r.status_code, 503)

    def test_no_answer(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/player-inbox/m-1/respond", {"answer": "  "})
        self.assertEqual(r.status_code, 400)

    def test_unknown_message(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "resolve_player_ask", return_value=None):
            r = _post(_client(), "/api/player-inbox/m-1/respond", {"answer": "ok"})
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "resolve_player_ask",
                                        return_value={"id": "m-1", "status": "answered"}):
            r = _post(_client(), "/api/player-inbox/m-1/respond", {"answer": "ok"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["message"]["id"], "m-1")



class ProvisionPlayerEmail(unittest.TestCase):
    def test_agent_requester_rejected(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/player-email/credential", {"appPassword": "abcd efgh ijkl mnop"})
        self.assertEqual(r.status_code, 403)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/player-email/credential", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_bad_provision(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: None), \
             unittest.mock.patch.object(serve, "provision_player_email",
                                        return_value={"ok": False, "error": "bad password"}):
            r = _post(_client(), "/api/player-email/credential", {"appPassword": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: None), \
             unittest.mock.patch.object(serve, "provision_player_email",
                                        return_value={"ok": True, "test_ok": True}):
            r = _post(_client(), "/api/player-email/credential",
                      {"appPassword": "abcd efgh ijkl mnop"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["testSent"])



class TestPlayerEmail(unittest.TestCase):
    def test_agent_requester_rejected(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/player-email/test", {})
        self.assertEqual(r.status_code, 403)

    def test_success(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: None), \
             unittest.mock.patch.object(serve, "_send_player_email_sync", return_value=True):
            r = _post(_client(), "/api/player-email/test", {})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["sent"])

    def test_send_failed(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: None), \
             unittest.mock.patch.object(serve, "_send_player_email_sync", return_value=False):
            r = _post(_client(), "/api/player-email/test", {})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["sent"])



class DeviceCheckin(unittest.TestCase):
    def _device_env(self, state):
        return _player_env(state, DEVICE_API_KEY="test-device-key-abc123",
                           **{"sim.record_device_checkin": None})

    def test_unauthorized(self):
        with _player_env(_roster_state(), DEVICE_API_KEY="test-device-key-abc123"):
            r = _client().post("/api/device/checkin", json={"battery": 50})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state(), DEVICE_API_KEY="test-device-key-abc123"):
            r = _client().post("/api/device/checkin", content=b"x",
                               headers={"X-Device-Key": "test-device-key-abc123",
                                        "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)

    def test_empty_body(self):
        with _player_env(_roster_state(), DEVICE_API_KEY="test-device-key-abc123"):
            r = _client().post("/api/device/checkin", json={},
                               headers={"X-Device-Key": "test-device-key-abc123"})
        self.assertEqual(r.status_code, 400)

    def test_invalid_location(self):
        with _player_env(_roster_state(), DEVICE_API_KEY="test-device-key-abc123"):
            r = _client().post("/api/device/checkin", json={"location": {"lat": "x"}},
                               headers={"X-Device-Key": "test-device-key-abc123"})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None, DEVICE_API_KEY="test-device-key-abc123"):
            r = _client().post("/api/device/checkin", json={"battery": 50},
                               headers={"X-Device-Key": "test-device-key-abc123"})
        self.assertEqual(r.status_code, 503)

    def test_success(self):
        state = _roster_state()
        with _player_env(state, DEVICE_API_KEY="test-device-key-abc123"), \
             unittest.mock.patch.object(sim, "record_device_checkin",
                                        return_value={"receivedAt": 1234}):
            r = _client().post("/api/device/checkin",
                               json={"location": {"lat": 1.0, "lon": 2.0}, "battery": 80},
                               headers={"X-Device-Key": "test-device-key-abc123"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["storedAt"], 1234)



class SetTeamPrefix(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/teams/team-1/prefix", {"prefix": "ENG"})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/teams/team-1/prefix", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/teams/team-1/prefix", {"prefix": "ENG"})
        self.assertEqual(r.status_code, 503)

    def test_unknown_team(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/teams/nope/prefix", {"prefix": "ENG"})
        self.assertEqual(r.status_code, 404)

    def test_bad_prefix(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "set_team_prefix", return_value=None):
            r = _post(_client(), "/api/teams/team-1/prefix", {"prefix": "TOO-LONG!"})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "set_team_prefix", return_value="ENG"):
            r = _post(_client(), "/api/teams/team-1/prefix", {"prefix": "ENG"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["prefix"], "ENG")

    def test_director_agent_key_can_set_prefix(self):
        # The gate is OPERATOR (player session OR director/admin), not
        # player-only: a rostered director's agent key must pass. Role check
        # and requester resolution are mocked against this module's temp DB
        # (no seeded roster); verify_session off forces the key branch.
        state = _roster_state()
        with _player_env(state,
                         verify_session=lambda cookie: False,
                         _valid_agent_key_presented=lambda k: True,
                         _resolve_requester=lambda request: "agent-2",
                         _is_director_or_admin=lambda s, a: a == "agent-2"), \
             unittest.mock.patch.object(sim, "set_team_prefix", return_value="ENG"):
            r = _post(_client(), "/api/teams/team-1/prefix", {"prefix": "ENG"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["prefix"], "ENG")

    def test_non_director_agent_key_is_refused(self):
        # A valid worker key clears the login middleware but not the gate.
        state = _roster_state()
        with _player_env(state,
                         verify_session=lambda cookie: False,
                         _valid_agent_key_presented=lambda k: True,
                         _resolve_requester=lambda request: "agent-3",
                         _is_director_or_admin=lambda s, a: False):
            r = _post(_client(), "/api/teams/team-1/prefix", {"prefix": "ENG"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"], "Unauthorized")



class IntentClarify(unittest.TestCase):
    def _clarify_state(self):
        state = _roster_state()
        state["products"] = {"prod-1": {"id": "prod-1", "name": "Atlas"}}
        return state

    def _plan(self, on_call="agent-3", completing="agent-3"):
        return {"onCall": on_call, "completing": completing,
                "product": {"name": "Atlas"}, "onCallFallback": False}

    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how?"})
        self.assertEqual(r.status_code, 401)

    def test_rate_limited(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "check_rate_limit", return_value=False):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how?"})
        self.assertEqual(r.status_code, 429)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how?"})
        self.assertEqual(r.status_code, 503)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/clarify", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_missing_fields(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/clarify", {"productId": "prod-1"})
        self.assertEqual(r.status_code, 400)

    def test_no_on_call(self):
        state = self._clarify_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "clarify_router_plan",
                                        return_value={"onCall": None}):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how?"})
        self.assertEqual(r.status_code, 404)

    def test_grounding_escalation_model_raise(self):
        state = self._clarify_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "clarify_router_plan",
                                        return_value=self._plan("agent-3", "agent-2")), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        side_effect=RuntimeError("net")), \
             unittest.mock.patch.object(serve, "_accrue_spend"):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how?"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["escalatedTo"], "agent-2")
        self.assertIn("I couldn't reach", body["reply"])

    def test_model_raise_without_escalation(self):
        state = self._clarify_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "clarify_router_plan",
                                        return_value=self._plan("agent-3", "agent-3")), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        side_effect=RuntimeError("net")), \
             unittest.mock.patch.object(serve, "_accrue_spend"):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how?"})
        self.assertEqual(r.status_code, 500)

    def test_success_with_kb_match(self):
        state = self._clarify_state()
        lib = os.path.join(serve.LIBRARY_DIR, "research")
        os.makedirs(lib, exist_ok=True)
        with open(os.path.join(lib, "atlas.md"), "w") as f:
            f.write("Atlas was built with node atlas how it works")
        with _player_env(state), \
             unittest.mock.patch.object(sim, "clarify_router_plan",
                                        return_value=self._plan("agent-3", "agent-3")), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        return_value={"choices": [{"message": {"content": "kb answer"}}],
                                                      "usage": {"cost": 0.1}}), \
             unittest.mock.patch.object(serve, "_accrue_spend"):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how is it built?"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reply"], "kb answer")

    def test_token_escalation_second_call(self):
        state = self._clarify_state()
        token = serve._CLARIFY_ESCALATE_TOKEN
        with _player_env(state), \
             unittest.mock.patch.object(sim, "clarify_router_plan",
                                        return_value=self._plan("agent-3", "agent-2")), \
             unittest.mock.patch.object(serve, "_library_search_matches",
                                        return_value=[{"path": "research/atlas.md", "snippet": "Atlas notes atlas"}]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        side_effect=[
                                            {"choices": [{"message": {"content": token}}],
                                             "usage": {"cost": 0.1}},
                                            {"choices": [{"message": {"content": "real final"}}],
                                             "usage": {"cost": 0.1}},
                                        ]), \
             unittest.mock.patch.object(serve, "_accrue_spend"):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how is it built?"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["reply"], "real final")
        self.assertEqual(body["escalatedTo"], "agent-2")

    def test_token_escalation_second_call_raises(self):
        state = self._clarify_state()
        token = serve._CLARIFY_ESCALATE_TOKEN
        with _player_env(state), \
             unittest.mock.patch.object(sim, "clarify_router_plan",
                                        return_value=self._plan("agent-3", "agent-2")), \
             unittest.mock.patch.object(serve, "_library_search_matches",
                                        return_value=[{"path": "research/atlas.md", "snippet": "Atlas notes atlas"}]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        side_effect=[
                                            {"choices": [{"message": {"content": token}}],
                                             "usage": {"cost": 0.1}},
                                            RuntimeError("net"),
                                        ]), \
             unittest.mock.patch.object(serve, "_accrue_spend"):
            r = _post(_client(), "/api/intent/clarify",
                      {"productId": "prod-1", "question": "how is it built?"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["escalatedTo"], "agent-2")
        self.assertIn("I couldn't reach", r.json()["reply"])



class MakeWebToolsExecutor(unittest.TestCase):
    def _exec(self, struck_tools=None):
        executor = serve._make_web_tools_executor("agent-3", "key", struck_tools=struck_tools)
        executor.observe_urls("https://example.com")
        executor.observe_urls("https://x.com")
        return executor

    def test_request_allowlist_requested(self):
        with unittest.mock.patch.object(serve, "_http_json",
                                        return_value={"requested": True, "message": "pending"}):
            out = self._exec()("request_allowlist", {"host": "example.com", "purpose": "p"})
        self.assertIn("pending", out)

    def test_request_allowlist_not_requested(self):
        with unittest.mock.patch.object(serve, "_http_json",
                                        return_value={"requested": False, "message": "ok"}):
            out = self._exec()("request_allowlist", {"host": "example.com"})
        self.assertIn("no further request needed", out)

    def test_request_allowlist_error(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value={"error": "nope"}):
            out = self._exec()("request_allowlist", {"host": "example.com"})
        self.assertIn("Could not file", out)

    def test_search_web(self):
        with unittest.mock.patch.object(serve, "_api_execute",
                                        return_value={"ok": True, "textForModel": "wrapped",
                                                      "modelInstruction": "inst"}):
            out = self._exec()("search_web", {"query": "news"})
        self.assertIn("wrapped", out)
        self.assertIn("inst", out)

    def test_browse_allowed_with_links(self):
        result = {"allowed": True, "textForModel": "page text", "modelInstruction": "mi",
                  "links": [{"text": "next", "url": "https://example.com/next"}]}
        with unittest.mock.patch.object(serve, "_http_json", return_value=result):
            out = self._exec()("browse_page", {"url": "https://example.com"})
        self.assertIn("Real links found on this page", out)

    def test_browse_allowed_no_links(self):
        result = {"allowed": True, "textForModel": "page text", "modelInstruction": "mi",
                  "links": []}
        with unittest.mock.patch.object(serve, "_http_json", return_value=result):
            out = self._exec()("browse_page", {"url": "https://example.com"})
        self.assertNotIn("Real links", out)

    def test_browse_denied_records_strike(self):
        struck = set()
        result = {"allowed": False, "reason": "policy"}
        with unittest.mock.patch.object(serve, "_http_json", return_value=result):
            out = self._exec(struck_tools=struck)("browse_page", {"url": "https://x.com"})
        self.assertIn("ONE-STRIKE", out)
        self.assertIn("browse_page", struck)

    def test_browse_transient_error(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value={"error": "timeout"}):
            out = self._exec()("browse_page", {"url": "https://x.com"})
        self.assertIn("transient error", out)

    def test_browse_unexpected_response(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value="junk"):
            out = self._exec()("browse_page", {"url": "https://x.com"})
        self.assertIn("unexpected response", out)

    def test_struck_tool_preblocked(self):
        out = self._exec(struck_tools={"search_web"})("search_web", {"query": "news"})
        self.assertIn("already blocked once", out)

    def test_unknown_tool(self):
        with self.assertRaises(ValueError):
            self._exec()("bogus", {})
