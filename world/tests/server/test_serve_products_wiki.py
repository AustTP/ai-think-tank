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


class RouteLaneIncident(unittest.TestCase):
    def test_no_product_falls_back_to_spike(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_classify_product_default",
                                        return_value=None), \
             unittest.mock.patch.object(serve, "_team_decider", return_value=None), \
             unittest.mock.patch.object(serve, "_classify_room_default",
                                        return_value="observatory"), \
             unittest.mock.patch.object(sim, "queue_spike", return_value={"id": "s"}):
            result = asyncio.run(serve._route_lane_incident(state, "site is down", "agent-1"))
        self.assertIn("investigation", result["reply"])

    def test_queue_bug_none_falls_back(self):
        state = _roster_state()
        state["products"] = {"prod-1": {"name": "Atlas"}}
        with unittest.mock.patch.object(serve, "_classify_product_default",
                                        return_value="prod-1"), \
             unittest.mock.patch.object(sim, "queue_bug", return_value=None), \
             unittest.mock.patch.object(serve, "_team_decider", return_value=None), \
             unittest.mock.patch.object(serve, "_classify_room_default",
                                        return_value="observatory"), \
             unittest.mock.patch.object(sim, "queue_spike", return_value={"id": "s"}):
            result = asyncio.run(serve._route_lane_incident(state, "site is down", "agent-1"))
        self.assertIn("investigation", result["reply"])

    def test_success(self):
        state = _roster_state()
        state["products"] = {"prod-1": {"name": "Atlas", "teamId": "team-1"}}
        with unittest.mock.patch.object(serve, "_classify_product_default",
                                        return_value="prod-1"), \
             unittest.mock.patch.object(sim, "queue_bug", return_value={"id": "bug-1"}), \
             unittest.mock.patch.object(sim, "on_call_agent", return_value="agent-3"):
            result = asyncio.run(serve._route_lane_incident(state, "site is down", "agent-1"))
        self.assertIn("Atlas", result["reply"])
        self.assertIn("Bo is on it", result["reply"])



class RoutePlayerRequest(unittest.TestCase):
    def test_empty_text(self):
        result = asyncio.run(serve._route_player_request(_roster_state(), "   "))
        self.assertEqual(result["status"], 400)

    def test_lane_spike(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_lane_decider", return_value="spike"), \
             unittest.mock.patch.object(serve, "_team_decider", return_value=None), \
             unittest.mock.patch.object(serve, "_classify_room_default",
                                        return_value="observatory"), \
             unittest.mock.patch.object(sim, "queue_spike", return_value={"id": "s"}):
            result = asyncio.run(serve._route_player_request(state, "look into x"))
        self.assertIn("investigation", result["reply"])

    def test_unclear_lane(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_lane_decider", return_value=None), \
             unittest.mock.patch.object(serve, "_unclear_lane_authority",
                                        return_value={"id": "agent-2"}), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"queued": False, "reply": "y"})):
            result = asyncio.run(serve._route_player_request(state, "do all the things"))
        self.assertEqual(result["reply"], "y")



class IntentSchedule(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/schedule",
                      {"topic": "x", "startUrl": "https://example.com", "cadenceMs": 3600000})
        self.assertEqual(r.status_code, 401)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/schedule",
                      {"topic": "x", "startUrl": "https://example.com", "cadenceMs": 3600000})
        self.assertEqual(r.status_code, 503)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/schedule", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_bad_record(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "add_research_topic", return_value=None):
            r = _post(_client(), "/api/intent/schedule",
                      {"topic": "x", "startUrl": "not-a-url", "cadenceMs": 3600000})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "add_research_topic",
                                        return_value={"id": "rt-1", "topic": "x",
                                                      "startUrl": "https://example.com",
                                                      "cadenceMs": 3600000}):
            r = _post(_client(), "/api/intent/schedule",
                      {"topic": "x", "startUrl": "https://example.com", "cadenceMs": 3600000})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["topic"]["id"], "rt-1")



class Pipelines(unittest.TestCase):
    def test_create_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/pipelines", {"name": "n", "cadenceMs": 3600000, "steps": []})
        self.assertEqual(r.status_code, 503)

    def test_create_malformed(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/pipelines", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_create_bad_record(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "add_pipeline", return_value=None):
            r = _post(_client(), "/api/pipelines", {"name": "n", "cadenceMs": 3600000, "steps": []})
        self.assertEqual(r.status_code, 400)

    def test_create_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "add_pipeline",
                                        return_value={"id": "p-1", "name": "n", "steps": []}):
            r = _post(_client(), "/api/pipelines",
                      {"name": "n", "cadenceMs": 3600000,
                       "steps": [{"title": "t", "room": "observatory"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["pipeline"]["id"], "p-1")

    def test_list_no_state(self):
        with _player_env(None):
            r = _client().get("/api/pipelines")
        self.assertEqual(r.status_code, 503)

    def test_list_success(self):
        state = _roster_state()
        state["pipelines"] = [{"id": "p-1"}, {"id": "p-2"}]
        with _player_env(state):
            r = _client().get("/api/pipelines")
        self.assertEqual(r.json()["pipelines"][0]["id"], "p-2")

    def test_delete_no_state(self):
        with _player_env(None):
            r = _client().delete("/api/pipelines/p-1")
        self.assertEqual(r.status_code, 503)

    def test_delete_not_found(self):
        state = _roster_state()
        state["pipelines"] = [{"id": "p-1"}]
        with _player_env(state):
            r = _client().delete("/api/pipelines/p-9")
        self.assertEqual(r.status_code, 404)

    def test_delete_success(self):
        state = _roster_state()
        state["pipelines"] = [{"id": "p-1"}, {"id": "p-2"}]
        with _player_env(state):
            r = _client().delete("/api/pipelines/p-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(state["pipelines"]), 1)



class IntentIncidents(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/incidents", {"productId": "prod-1", "title": "down"})
        self.assertEqual(r.status_code, 401)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/incidents", {"productId": "prod-1", "title": "down"})
        self.assertEqual(r.status_code, 503)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/incidents", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_missing_fields(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/incidents", {"productId": "prod-1"})
        self.assertEqual(r.status_code, 400)

    def test_unroutable(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_bug", return_value=None):
            r = _post(_client(), "/api/intent/incidents", {"productId": "prod-1", "title": "down"})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_bug", return_value={"id": "bug-1"}):
            r = _post(_client(), "/api/intent/incidents", {"productId": "prod-1", "title": "down"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["productId"], "prod-1")



class ValidSandboxIds(unittest.TestCase):
    def test_missing_dir(self):
        with unittest.mock.patch.object(serve, "SANDBOXES_DIR",
                                        "/nonexistent/path/xyz"):
            self.assertEqual(serve._valid_sandbox_ids(), ["workroom-shared"])

    def test_dir_with_entries(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "workroom-shared"))
        os.makedirs(os.path.join(tmp, "research-shared"))
        with unittest.mock.patch.object(serve, "SANDBOXES_DIR", tmp):
            self.assertEqual(serve._valid_sandbox_ids(),
                             ["research-shared", "workroom-shared"])
        shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_dir(self):
        tmp = tempfile.mkdtemp()
        with unittest.mock.patch.object(serve, "SANDBOXES_DIR", tmp):
            self.assertEqual(serve._valid_sandbox_ids(), ["workroom-shared"])
        shutil.rmtree(tmp, ignore_errors=True)

    def test_listdir_oserror(self):
        tmp = tempfile.mkdtemp()
        with unittest.mock.patch.object(serve, "SANDBOXES_DIR", tmp), \
             unittest.mock.patch.object(os, "listdir", side_effect=OSError):
            self.assertEqual(serve._valid_sandbox_ids(), ["workroom-shared"])
        shutil.rmtree(tmp, ignore_errors=True)



class IntentProduct(unittest.TestCase):
    def _sandbox_env(self):
        sandboxes = os.path.join(_MODULE_TMP_DIR, "test-sandboxes")
        os.makedirs(os.path.join(sandboxes, "workroom-shared"), exist_ok=True)
        return unittest.mock.patch.object(serve, "SANDBOXES_DIR", sandboxes)

    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/product", {"name": "P"})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/product", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_name(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/product", {"name": "  "})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/product", {"name": "P"})
        self.assertEqual(r.status_code, 503)

    def test_no_authority(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority", return_value=None):
            r = _post(_client(), "/api/intent/product", {"name": "P"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("No admin or director is free", r.json()["error"])

    def test_unknown_owner(self):
        state = _roster_state()
        with _player_env(state), self._sandbox_env(), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1"}):
            r = _post(_client(), "/api/intent/product",
                      {"name": "P", "ownerId": "ghost", "sandboxId": "workroom-shared"})
        self.assertEqual(r.status_code, 404)

    def test_unknown_sandbox(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1"}), \
             unittest.mock.patch.object(serve, "_valid_sandbox_ids",
                                        return_value=["workroom-shared"]):
            r = _post(_client(), "/api/intent/product",
                      {"name": "P", "sandboxId": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_create_none(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1"}), \
             unittest.mock.patch.object(serve, "_valid_sandbox_ids",
                                        return_value=["workroom-shared"]), \
             unittest.mock.patch.object(sim, "next_product_id", return_value="prod-1"), \
             unittest.mock.patch.object(sim, "create_product", return_value=None):
            r = _post(_client(), "/api/intent/product",
                      {"name": "P", "sandboxId": "workroom-shared"})
        self.assertEqual(r.status_code, 409)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}), \
             unittest.mock.patch.object(serve, "_valid_sandbox_ids",
                                        return_value=["workroom-shared"]), \
             unittest.mock.patch.object(sim, "next_product_id", return_value="prod-1"), \
             unittest.mock.patch.object(sim, "create_product",
                                        return_value={"id": "prod-1", "name": "P"}):
            r = _post(_client(), "/api/intent/product",
                      {"name": "P", "sandboxId": "workroom-shared", "ownerId": "player"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["id"], "prod-1")



class SetProductStatus(unittest.TestCase):
    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/product/prod-1/status", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/product/prod-1/status", {"status": "draft"})
        self.assertEqual(r.status_code, 503)

    def test_forbidden(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-3"), \
             unittest.mock.patch.object(serve, "_is_director_or_admin", return_value=False):
            r = _post(_client(), "/api/intent/product/prod-1/status", {"status": "draft"})
        self.assertEqual(r.status_code, 403)

    def test_unknown(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"), \
             unittest.mock.patch.object(serve, "_is_director_or_admin", return_value=True), \
             unittest.mock.patch.object(sim, "set_product_status", return_value=None):
            r = _post(_client(), "/api/intent/product/prod-1/status", {"status": "draft"})
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        state = _roster_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"), \
             unittest.mock.patch.object(serve, "_is_director_or_admin", return_value=True), \
             unittest.mock.patch.object(sim, "set_product_status",
                                        return_value={"id": "prod-1", "status": "in_progress"}):
            r = _post(_client(), "/api/intent/product/prod-1/status", {"status": "in_progress"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["status"], "in_progress")



class ReleaseProduct(unittest.TestCase):
    def _sandbox_env(self):
        sandboxes = os.path.join(_MODULE_TMP_DIR, "test-sandboxes")
        os.makedirs(os.path.join(sandboxes, "workroom-shared"), exist_ok=True)
        with open(os.path.join(sandboxes, "workroom-shared", "code.py"), "w") as f:
            f.write("print('hi')")
        return unittest.mock.patch.object(serve, "SANDBOXES_DIR", sandboxes)

    def _product_state(self):
        state = _roster_state()
        state["products"] = {"prod-1": {"id": "prod-1", "name": "Atlas",
                                        "sandboxId": "workroom-shared",
                                        "status": "review", "ownerId": "agent-1"}}
        return state

    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/product/prod-1/release", {})
        self.assertEqual(r.status_code, 401)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/product/prod-1/release", {})
        self.assertEqual(r.status_code, 503)

    def test_unknown_product(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/product/prod-1/release", {})
        self.assertEqual(r.status_code, 404)

    def test_sandbox_missing(self):
        state = self._product_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "SANDBOXES_DIR", "/nonexistent/path"):
            r = _post(_client(), "/api/intent/product/prod-1/release", {})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = self._product_state()
        with _player_env(state), self._sandbox_env(), \
             unittest.mock.patch.object(sim, "next_product_revision", return_value=(2, {})), \
             unittest.mock.patch.object(sim, "product_release_record",
                                        return_value={"revision": 2}), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}):
            r = _post(_client(), "/api/intent/product/prod-1/release",
                      {"revisionNote": "shipped it"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revision"]["revision"], 2)
        rel_path = os.path.join(serve.LIBRARY_DIR, "projects", "prod-1", "v2", "RELEASE.md")
        self.assertTrue(os.path.isfile(rel_path))



class CopyReleaseSnapshot(unittest.TestCase):
    def test_copies_dir_and_file(self):
        src = tempfile.mkdtemp()
        dest = os.path.join(tempfile.mkdtemp(), "v1")
        os.makedirs(os.path.join(src, "subdir"))
        with open(os.path.join(src, "subdir", "inner.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(src, "code.py"), "w") as f:
            f.write("print(1)")
        serve._copy_release_snapshot(src, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "code.py")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "subdir", "inner.txt")))

    def test_skips_unreadable(self):
        src = tempfile.mkdtemp()
        dest = os.path.join(tempfile.mkdtemp(), "v1")
        os.symlink("/nonexistent/target", os.path.join(src, "broken"))
        serve._copy_release_snapshot(src, dest)
        self.assertFalse(os.path.exists(os.path.join(dest, "broken")))



class GetWiki(unittest.TestCase):
    def test_empty_state(self):
        with _player_env({}):
            r = _client().get("/api/intent/wiki")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["pages"], {})

    def test_success(self):
        state = _wiki_state()
        with _player_env(state):
            r = _client().get("/api/intent/wiki")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["categories"]["operations"]["label"], "Operations")



class GetWikiPage(unittest.TestCase):
    def test_unknown_page(self):
        with _player_env(_wiki_state()):
            r = _client().get("/api/intent/wiki/page/nope")
        self.assertEqual(r.status_code, 404)

    def test_missing_body_file(self):
        state = _wiki_state()
        state["wiki"]["pages"]["ghost-page"] = {"id": "ghost-page", "title": "T",
                                                "category": "operations", "version": 1}
        with _player_env(state):
            r = _client().get("/api/intent/wiki/page/ghost-page")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["page"]["body"], "")

    def test_success(self):
        state = _wiki_state()
        state["wiki"]["pages"]["w-1"] = {"id": "w-1", "title": "Runbooks",
                                         "category": "operations", "version": 1}
        cat_dir = os.path.join(serve.LIBRARY_DIR, "wiki", "operations")
        os.makedirs(cat_dir, exist_ok=True)
        with open(os.path.join(cat_dir, "w-1.md"), "w") as f:
            f.write("the page body")
        with _player_env(state):
            r = _client().get("/api/intent/wiki/page/w-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["page"]["body"], "the page body")



class WriteWikiPage(unittest.TestCase):
    def test_malformed_body(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/page", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_missing_fields(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/page", {"title": "T"})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/wiki/page",
                      {"id": "w-1", "category": "operations"})
        self.assertEqual(r.status_code, 503)

    def test_unknown_category(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/page",
                      {"id": "w-1", "category": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_not_director(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/page",
                      {"id": "w-1", "category": "operations", "body": "x"})
        self.assertEqual(r.status_code, 403)

    def test_write_failed(self):
        state = _wiki_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"), \
             unittest.mock.patch.object(sim, "wiki_write_page", return_value=(None, False)):
            r = _post(_client(), "/api/intent/wiki/page",
                      {"id": "w-1", "category": "operations", "body": "x"})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _wiki_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/intent/wiki/page",
                      {"id": "w-1", "title": "Runbooks", "category": "operations",
                       "body": "the body"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["isNew"])
        self.assertEqual(r.json()["page"]["version"], 1)
        body_path = os.path.join(serve.LIBRARY_DIR, "wiki", "operations", "w-1.md")
        self.assertTrue(os.path.isfile(body_path))
        self.assertEqual(state["wiki"]["pages"]["w-1"]["editedBy"], "agent-1")



class ProposeWikiPage(unittest.TestCase):
    def test_malformed_body(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/propose", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_agent_key(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/propose",
                      {"id": "w-1", "category": "operations", "body": "x"})
        self.assertEqual(r.status_code, 403)

    def test_missing_fields(self):
        with _player_env(_wiki_state(), _resolve_requester=lambda r: "agent-3"):
            r = _post(_client(), "/api/intent/wiki/propose", {"id": "w-1"})
        self.assertEqual(r.status_code, 400)

    def test_invalid_page_id(self):
        with _player_env(_wiki_state(), _resolve_requester=lambda r: "agent-3"):
            r = _post(_client(), "/api/intent/wiki/propose",
                      {"id": "a/b", "category": "operations", "body": "x"})
        self.assertEqual(r.status_code, 400)

    def test_no_body(self):
        with _player_env(_wiki_state(), _resolve_requester=lambda r: "agent-3"):
            r = _post(_client(), "/api/intent/wiki/propose",
                      {"id": "w-1", "category": "operations", "body": "  "})
        self.assertEqual(r.status_code, 400)

    def test_body_too_large(self):
        with _player_env(_wiki_state(), _resolve_requester=lambda r: "agent-3"):
            r = _post(_client(), "/api/intent/wiki/propose",
                      {"id": "w-1", "category": "operations", "body": "x" * 200_001})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None, _resolve_requester=lambda r: "agent-3"):
            r = _post(_client(), "/api/intent/wiki/propose",
                      {"id": "w-1", "category": "operations", "body": "x"})
        self.assertEqual(r.status_code, 503)

    def test_unknown_category(self):
        with _player_env(_wiki_state(), _resolve_requester=lambda r: "agent-3"):
            r = _post(_client(), "/api/intent/wiki/propose",
                      {"id": "w-1", "category": "bogus", "body": "x"})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _wiki_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-3"):
            r = _post(_client(), "/api/intent/wiki/propose",
                      {"id": "w-1", "category": "operations", "body": "draft",
                       "summary": "a draft"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["proposal"]["proposedBy"], "agent-3")
        prop_path = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki",
                                 "operations", "w-1.json")
        self.assertTrue(os.path.isfile(prop_path))
        with open(prop_path) as f:
            self.assertEqual(json.load(f)["body"], "draft")
        self.assertNotIn("w-1", state["wiki"]["pages"])



class ListWikiProposals(unittest.TestCase):
    def _make_proposal(self, page_id, proposed_at):
        prop_dir = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki", "operations")
        os.makedirs(prop_dir, exist_ok=True)
        with open(os.path.join(prop_dir, f"{page_id}.json"), "w") as f:
            json.dump({"id": page_id, "title": page_id, "category": "operations",
                       "body": "x", "proposedBy": "agent-3", "proposedAt": proposed_at}, f)

    def test_no_proposals(self):
        shutil.rmtree(os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki"),
                      ignore_errors=True)
        with _player_env(_wiki_state()):
            r = _client().get("/api/intent/wiki/proposals")
        self.assertEqual(r.json()["proposals"], [])

    def test_skips_invalid_json(self):
        self._make_proposal("w-1", 100)
        prop_dir = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki", "operations")
        with open(os.path.join(prop_dir, "bad.json"), "w") as f:
            f.write("not json")
        with _player_env(_wiki_state()):
            r = _client().get("/api/intent/wiki/proposals")
        self.assertEqual(len(r.json()["proposals"]), 1)

    def test_sorted(self):
        self._make_proposal("w-1", 200)
        self._make_proposal("w-2", 100)
        with _player_env(_wiki_state()):
            r = _client().get("/api/intent/wiki/proposals")
        self.assertEqual(r.json()["proposals"][0]["id"], "w-2")



class FindWikiProposal(unittest.TestCase):
    def _make_proposal(self, page_id):
        prop_dir = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki", "operations")
        os.makedirs(prop_dir, exist_ok=True)
        with open(os.path.join(prop_dir, f"{page_id}.json"), "w") as f:
            json.dump({"id": page_id, "title": "T", "category": "operations",
                       "body": "x", "proposedBy": "agent-3", "proposedAt": 1}, f)

    def test_missing(self):
        proposal, rel = serve._find_wiki_proposal("operations", "nope")
        self.assertIsNone(proposal)
        self.assertEqual(rel, "pending_review/wiki/operations/nope.json")

    def test_invalid_json(self):
        prop_dir = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki", "operations")
        os.makedirs(prop_dir, exist_ok=True)
        with open(os.path.join(prop_dir, "bad.json"), "w") as f:
            f.write("junk")
        proposal, _rel = serve._find_wiki_proposal("operations", "bad")
        self.assertIsNone(proposal)

    def test_found(self):
        self._make_proposal("w-1")
        proposal, rel = serve._find_wiki_proposal("operations", "w-1")
        self.assertEqual(proposal["id"], "w-1")
        self.assertEqual(rel, "pending_review/wiki/operations/w-1.json")



class ApproveWikiProposal(unittest.TestCase):
    def _make_proposal(self, page_id="w-1"):
        prop_dir = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki", "operations")
        os.makedirs(prop_dir, exist_ok=True)
        with open(os.path.join(prop_dir, f"{page_id}.json"), "w") as f:
            json.dump({"id": page_id, "title": "T", "category": "operations",
                       "body": "approved body", "summary": "s", "proposedBy": "agent-3",
                       "proposedAt": 1}, f)

    def test_malformed_body(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/approve", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_category(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/approve", {})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/approve",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 503)

    def test_not_director(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/approve",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 403)

    def test_no_pending(self):
        shutil.rmtree(os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki"),
                      ignore_errors=True)
        with _player_env(_wiki_state(), _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/approve",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        self._make_proposal()
        state = _wiki_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/approve",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["proposedBy"], "agent-3")
        self.assertEqual(state["wiki"]["pages"]["w-1"]["version"], 1)
        body_path = os.path.join(serve.LIBRARY_DIR, "wiki", "operations", "w-1.md")
        self.assertTrue(os.path.isfile(body_path))
        archive = os.path.join(serve.LIBRARY_DIR, "archive", "wiki-proposals",
                               "operations", "w-1.json")
        self.assertTrue(os.path.isfile(archive))
        src = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki",
                           "operations", "w-1.json")
        self.assertFalse(os.path.isfile(src))



class RejectWikiProposal(unittest.TestCase):
    def _make_proposal(self, page_id="w-1"):
        prop_dir = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki", "operations")
        os.makedirs(prop_dir, exist_ok=True)
        with open(os.path.join(prop_dir, f"{page_id}.json"), "w") as f:
            json.dump({"id": page_id, "title": "T", "category": "operations",
                       "body": "draft", "proposedBy": "agent-3", "proposedAt": 1}, f)

    def test_malformed_body(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/reject", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_category(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/reject", {})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/reject",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 503)

    def test_not_director(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/reject",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 403)

    def test_no_pending(self):
        shutil.rmtree(os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki"),
                      ignore_errors=True)
        with _player_env(_wiki_state(), _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/reject",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        self._make_proposal()
        state = _wiki_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/reject",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["rejected"]["to"], "rejected/wiki/operations/w-1.json")
        dest = os.path.join(serve.LIBRARY_DIR, "rejected", "wiki", "operations", "w-1.json")
        self.assertTrue(os.path.isfile(dest))
        self.assertNotIn("w-1", state["wiki"]["pages"])



class WriteWikiServer(unittest.TestCase):
    def test_no_state(self):
        with _player_env(None):
            self.assertIsNone(serve._write_wiki_server("p", "T", "operations", "x"))

    def test_unknown_category(self):
        with _player_env(_roster_state()):
            self.assertIsNone(serve._write_wiki_server("p", "T", "bogus", "x"))

    def test_success_with_think_tank_seed(self):
        state = _roster_state()
        with _player_env(state):
            record = serve._write_wiki_server("p-1", "Distilled", "think_tank", "body here")
        self.assertIsNotNone(record)
        self.assertEqual(record["version"], 1)
        self.assertEqual(state["wiki"]["categories"]["think_tank"]["label"], "Think Tank")
        self.assertEqual(state["wiki"]["pages"]["p-1"]["editedBy"], "distill")

    def test_write_failed(self):
        state = _wiki_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "wiki_write_page", return_value=(None, False)):
            self.assertIsNone(serve._write_wiki_server("p", "T", "operations", "x"))
