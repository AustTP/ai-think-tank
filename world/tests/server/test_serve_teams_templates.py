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


class WriteWikiCategory(unittest.TestCase):
    def test_malformed_body(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/category", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_id(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/category", {"label": "X"})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/wiki/category", {"id": "x"})
        self.assertEqual(r.status_code, 503)

    def test_not_director(self):
        with _player_env(_wiki_state()):
            r = _post(_client(), "/api/intent/wiki/category", {"id": "x"})
        self.assertEqual(r.status_code, 403)

    def test_success(self):
        state = _wiki_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/intent/wiki/category",
                      {"id": "new-cat", "label": "New", "order": 3, "room": "pressoffice"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state["wiki"]["categories"]["new-cat"]["label"], "New")
        self.assertEqual(state["wiki"]["categoryRooms"]["new-cat"], "pressoffice")

    def test_update_existing(self):
        state = _wiki_state()
        with _player_env(state, _resolve_requester=lambda r: "agent-1"):
            r = _post(_client(), "/api/intent/wiki/category",
                      {"id": "operations", "label": "Ops", "order": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state["wiki"]["categories"]["operations"]["label"], "Ops")
        self.assertEqual(state["wiki"]["categories"]["operations"]["order"], 5)



class ListTeams(unittest.TestCase):
    def test_success(self):
        state = _roster_state()
        with _player_env(state):
            r = _client().get("/api/teams")
        self.assertEqual(r.status_code, 200)
        team = r.json()["teams"][0]
        self.assertEqual(team["id"], "team-1")
        self.assertEqual(team["scrumMaster"]["id"], "agent-3")
        self.assertIn("sharedDir", team)
        self.assertIn("members", team)



class ListBacklog(unittest.TestCase):
    def test_success(self):
        state = _roster_state()
        state["backlog"] = [{"id": "b-1", "title": "T", "teamId": "agent-2"}]
        state["features"] = {"f-1": {"id": "f-1"}}
        state["retrospectives"] = {"r-1": {"id": "r-1"}}
        with _player_env(state):
            r = _client().get("/api/backlog")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["features"][0]["id"], "f-1")
        self.assertEqual(r.json()["backlog"][0]["teamName"], "Nora")



class UpdateTeam(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state()):
            r = _client().put("/api/teams/team-1", json={"name": "Beta"})
        self.assertEqual(r.status_code, 401)

    def test_key_mismatch(self):
        with _agent_env(_roster_state(), key_ok=False):
            r = _client().put("/api/teams/team-1", json={"name": "Beta"})
        self.assertEqual(r.status_code, 403)

    def test_no_state(self):
        with _agent_env(None):
            r = _client().put("/api/teams/team-1", json={"name": "Beta"})
        self.assertEqual(r.status_code, 503)

    def test_unknown_team(self):
        with _agent_env(_roster_state()):
            r = _client().put("/api/teams/nope", json={"name": "Beta"})
        self.assertEqual(r.status_code, 404)

    def test_cannot_write(self):
        state = _roster_state()
        with _agent_env(state), \
             unittest.mock.patch.object(serve, "_can_write_team", return_value=False):
            r = _client().put("/api/teams/team-1", json={"name": "Beta"})
        self.assertEqual(r.status_code, 403)

    def test_success(self):
        state = _roster_state()
        with _agent_env(state):
            r = _client().put("/api/teams/team-1",
                              json={"name": "Beta", "purpose": "New purpose"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state["teams"][0]["name"], "Beta")
        self.assertEqual(state["teams"][0]["purpose"], "New purpose")
        self.assertEqual(state["teams"][0]["updatedBy"], "agent-1")



class PromoteToDirector(unittest.TestCase):
    def _team_state(self):
        state = _roster_state()
        state["teams"][0]["members"] = ["agent-3"]
        return state

    def test_unauthorized(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/teams/team-1/promote", {"promoteeId": "agent-3"})
        self.assertEqual(r.status_code, 401)

    def test_key_mismatch(self):
        with _agent_env(_roster_state(), key_ok=False):
            r = _post(_client(), "/api/teams/team-1/promote", {"promoteeId": "agent-3"})
        self.assertEqual(r.status_code, 403)

    def test_no_state(self):
        with _agent_env(None):
            r = _post(_client(), "/api/teams/team-1/promote", {"promoteeId": "agent-3"})
        self.assertEqual(r.status_code, 503)

    def test_unknown_team(self):
        with _agent_env(_roster_state()):
            r = _post(_client(), "/api/teams/nope/promote", {"promoteeId": "agent-3"})
        self.assertEqual(r.status_code, 404)

    def test_cannot_write(self):
        state = _roster_state()
        with _agent_env(state), \
             unittest.mock.patch.object(serve, "_can_write_team", return_value=False):
            r = _post(_client(), "/api/teams/team-1/promote", {"promoteeId": "agent-3"})
        self.assertEqual(r.status_code, 403)

    def test_not_a_member(self):
        state = self._team_state()
        with _agent_env(state):
            r = _post(_client(), "/api/teams/team-1/promote", {"promoteeId": "agent-9"})
        self.assertEqual(r.status_code, 400)

    def test_promote_failed(self):
        state = self._team_state()
        with _agent_env(state), \
             unittest.mock.patch.object(serve, "_promote_to_director", return_value=None):
            r = _post(_client(), "/api/teams/team-1/promote", {"promoteeId": "agent-3"})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = self._team_state()
        new_team = {"id": "agent-3", "name": "Bo's Crew", "directorId": "agent-3"}
        with _agent_env(state), \
             unittest.mock.patch.object(serve, "_promote_to_director", return_value=new_team):
            r = _post(_client(), "/api/teams/team-1/promote", {"promoteeId": "agent-3"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["promoted"], "agent-3")
        self.assertEqual(r.json()["team"]["id"], "agent-3")



class SetTeamScrumMaster(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/teams/team-1/scrum-master", {"scrumMasterId": "agent-1"})
        self.assertEqual(r.status_code, 401)

    def test_key_mismatch(self):
        with _agent_env(_roster_state(), key_ok=False):
            r = _post(_client(), "/api/teams/team-1/scrum-master", {"scrumMasterId": "agent-1"})
        self.assertEqual(r.status_code, 403)

    def test_no_state(self):
        with _agent_env(None):
            r = _post(_client(), "/api/teams/team-1/scrum-master", {"scrumMasterId": "agent-1"})
        self.assertEqual(r.status_code, 503)

    def test_unknown_team(self):
        with _agent_env(_roster_state()):
            r = _post(_client(), "/api/teams/nope/scrum-master", {"scrumMasterId": "agent-1"})
        self.assertEqual(r.status_code, 404)

    def test_cannot_write(self):
        state = _roster_state()
        with _agent_env(state), \
             unittest.mock.patch.object(serve, "_can_write_team", return_value=False):
            r = _post(_client(), "/api/teams/team-1/scrum-master", {"scrumMasterId": "agent-1"})
        self.assertEqual(r.status_code, 403)

    def test_invalid_scrum_master(self):
        state = _roster_state()
        with _agent_env(state):
            r = _post(_client(), "/api/teams/team-1/scrum-master", {"scrumMasterId": "stranger"})
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _roster_state()
        with _agent_env(state):
            r = _post(_client(), "/api/teams/team-1/scrum-master", {"scrumMasterId": "agent-1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state["teams"][0]["scrumMasterId"], "agent-1")

    def test_clear(self):
        state = _roster_state()
        with _agent_env(state):
            r = _post(_client(), "/api/teams/team-1/scrum-master", {"scrumMasterId": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(state["teams"][0]["scrumMasterId"])

    def test_director_facilitation_acknowledgement(self):
        state = _roster_state()
        with _agent_env(state):
            r = _post(_client(), "/api/teams/team-1/scrum-master",
                      {"directorFacilitation": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(state["teams"][0]["directorFacilitation"])
        # A facilitation-only update must not clear the named scrum master.
        self.assertEqual(state["teams"][0]["scrumMasterId"], "agent-3")

    def test_named_scrum_master_clears_facilitation_ack(self):
        state = _roster_state()
        state["teams"][0]["directorFacilitation"] = True
        with _agent_env(state):
            r = _post(_client(), "/api/teams/team-1/scrum-master",
                      {"scrumMasterId": "agent-1"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("directorFacilitation", state["teams"][0])



class UpdateRoomPurpose(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/rooms/observatory/purpose", {"purpose": "look up"})
        self.assertEqual(r.status_code, 401)

    def test_key_mismatch(self):
        with _agent_env(_roster_state(), key_ok=False):
            r = _post(_client(), "/api/rooms/observatory/purpose", {"purpose": "look up"})
        self.assertEqual(r.status_code, 403)

    def test_unknown_room(self):
        with _agent_env(_roster_state()):
            r = _post(_client(), "/api/rooms/bogus/purpose", {"purpose": "x"})
        self.assertEqual(r.status_code, 400)

    def test_no_purpose(self):
        with _agent_env(_roster_state()):
            r = _post(_client(), "/api/rooms/observatory/purpose", {"purpose": "  "})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _agent_env(None):
            r = _post(_client(), "/api/rooms/observatory/purpose", {"purpose": "x"})
        self.assertEqual(r.status_code, 503)

    def test_not_director(self):
        state = _roster_state()
        with _agent_env(state, requester="agent-3"):
            r = _post(_client(), "/api/rooms/observatory/purpose", {"purpose": "x"})
        self.assertEqual(r.status_code, 403)

    def test_success(self):
        state = _roster_state()
        with _agent_env(state):
            r = _post(_client(), "/api/rooms/observatory/purpose",
                      {"purpose": "Watch the skies"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["purpose"], "Watch the skies")
        self.assertEqual(state["roomDefinitions"]["observatory"]["purpose"],
                         "Watch the skies")



class ListTemplates(unittest.TestCase):
    def test_success(self):
        state = _roster_state()
        state["templates"] = {"Research": {"mission": "Investigate"}}
        with _player_env(state):
            r = _client().get("/api/templates")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["templates"]["Research"]["mission"], "Investigate")



class GetTemplate(unittest.TestCase):
    def test_known_role(self):
        state = _roster_state()
        state["templates"] = {"Research": {"mission": "Investigate"}}
        with _player_env(state):
            r = _client().get("/api/templates/Research")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["seeded"])
        self.assertEqual(r.json()["profile"]["mission"], "Investigate")

    def test_unknown_role_fallback(self):
        with _player_env(_roster_state()), \
             unittest.mock.patch.object(serve, "_profile_for_role",
                                        return_value={"mission": "generic"}):
            r = _client().get("/api/templates/Bogus")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["seeded"])
        self.assertEqual(r.json()["profile"]["mission"], "generic")



class UpsertTemplate(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/templates",
                      {"role": "NewRole", "instructions": ["a"]})
        self.assertEqual(r.status_code, 401)

    def test_key_mismatch(self):
        with _agent_env(_roster_state(), key_ok=False):
            r = _post(_client(), "/api/templates",
                      {"role": "NewRole", "instructions": ["a"]})
        self.assertEqual(r.status_code, 403)

    def test_no_role(self):
        with _agent_env(_roster_state()):
            r = _post(_client(), "/api/templates", {"instructions": ["a"]})
        self.assertEqual(r.status_code, 400)

    def test_instructions_not_list(self):
        with _agent_env(_roster_state()):
            r = _post(_client(), "/api/templates",
                      {"role": "NewRole", "instructions": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _agent_env(None):
            r = _post(_client(), "/api/templates",
                      {"role": "NewRole", "instructions": ["a"]})
        self.assertEqual(r.status_code, 503)

    def test_not_director(self):
        state = _roster_state()
        with _agent_env(state, requester="agent-3"):
            r = _post(_client(), "/api/templates",
                      {"role": "NewRole", "instructions": ["a"]})
        self.assertEqual(r.status_code, 403)

    def test_create_success(self):
        state = _roster_state()
        with _agent_env(state):
            r = _post(_client(), "/api/templates",
                      {"role": "NewRole", "mission": "Do it", "instructions": ["a", "b"],
                       "notes": ["n"]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["created"])
        self.assertEqual(state["templates"]["NewRole"]["mission"], "Do it")
        self.assertEqual(state["templates"]["NewRole"]["instructions"], ["a", "b"])

    def test_update_success(self):
        state = _roster_state()
        state["templates"] = {"Research": {"mission": "Old"}}
        with _agent_env(state):
            r = _post(_client(), "/api/templates",
                      {"role": "Research", "mission": "New", "instructions": []})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["created"])
        self.assertEqual(state["templates"]["Research"]["mission"], "New")



class ApplyTemplate(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/templates/Research/apply", {})
        self.assertEqual(r.status_code, 401)

    def test_key_mismatch(self):
        with _agent_env(_roster_state(), key_ok=False):
            r = _post(_client(), "/api/templates/Research/apply", {})
        self.assertEqual(r.status_code, 403)

    def test_no_state(self):
        with _agent_env(None):
            r = _post(_client(), "/api/templates/Research/apply", {})
        self.assertEqual(r.status_code, 503)

    def test_not_director(self):
        state = _roster_state()
        with _agent_env(state, requester="agent-3"):
            r = _post(_client(), "/api/templates/Research/apply", {})
        self.assertEqual(r.status_code, 403)

    def test_no_template(self):
        with _agent_env(_roster_state()):
            r = _post(_client(), "/api/templates/Research/apply", {})
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        state = _roster_state()
        state["templates"] = {"Research": {"mission": "Investigate"}}
        with _agent_env(state):
            r = _post(_client(), "/api/templates/Research/apply", {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["appliedTo"], [])



class DeleteTemplate(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state()):
            r = _client().delete("/api/templates/Research")
        self.assertEqual(r.status_code, 401)

    def test_key_mismatch(self):
        with _agent_env(_roster_state(), key_ok=False):
            r = _client().delete("/api/templates/Research")
        self.assertEqual(r.status_code, 403)

    def test_no_state(self):
        with _agent_env(None):
            r = _client().delete("/api/templates/Research")
        self.assertEqual(r.status_code, 503)

    def test_not_director(self):
        state = _roster_state()
        with _agent_env(state, requester="agent-3"):
            r = _client().delete("/api/templates/Research")
        self.assertEqual(r.status_code, 403)

    def test_no_template(self):
        with _agent_env(_roster_state()):
            r = _client().delete("/api/templates/Research")
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        state = _roster_state()
        state["templates"] = {"Research": {"mission": "Investigate"}}
        with _agent_env(state):
            r = _client().delete("/api/templates/Research")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Research", state["templates"])



class PromoteShadowEntryBranches(unittest.TestCase):
    def _state(self, **entry_overrides):
        state = _roster_state()
        entry = {"title": "Dry run", "note": "draft finding",
                 "room": "pressoffice", "taskType": "code"}
        entry.update(entry_overrides)
        state["shadowLedger"] = [entry]
        return state

    def test_malformed_body_defaults(self):
        state = self._state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/shadow/0/promote", content=b"not-json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["queued"]["room"], "pressoffice")

    def test_library_file_finding(self):
        state = self._state(libraryPath="reports/finding.md")
        path = os.path.join(serve.LIBRARY_DIR, "reports", "finding.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("file-based finding")
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/shadow/0/promote", {})
        self.assertEqual(r.status_code, 200)
        self.assertIn("file-based finding", state["workQueue"][0]["instructions"])

    def test_library_file_oserror_fallback(self):
        state = self._state(libraryPath="reports/finding.md")
        path = os.path.join(serve.LIBRARY_DIR, "reports", "finding.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")
        real_open = open

        def fake_open(*a, **k):
            if a and str(a[0]).endswith("finding.md"):
                raise OSError("boom")
            return real_open(*a, **k)

        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append), \
             unittest.mock.patch("builtins.open", side_effect=fake_open):
            r = _post(_client(), "/api/shadow/0/promote", {})
        self.assertEqual(r.status_code, 200)
        self.assertIn("no written findings", state["workQueue"][0]["instructions"])

    def test_no_note_fallback(self):
        state = self._state(note="")
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/shadow/0/promote", {})
        self.assertEqual(r.status_code, 200)
        self.assertIn("no written findings", state["workQueue"][0]["instructions"])



class IntentSprintBranches(unittest.TestCase):
    def _items(self):
        return [{"title": "Build the thing", "room": "pressoffice"}]

    def test_non_list_team_ids(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items(), "teamIds": "team-1"})
        self.assertEqual(r.status_code, 200)

    def test_iso_target_date(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items(),
                       "targetDate": "2024-01-15T00:00:00Z"})
        self.assertEqual(r.status_code, 200)

    def test_malformed_target_date(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items(),
                       "targetDate": "not-a-date"})
        self.assertEqual(r.status_code, 200)



class AskCorePeerReviewFilename(unittest.TestCase):
    def _run(self, state, http_result):
        script = [("read_peer_reviews",
                   {"targetAgentId": "agent-2", "filename": "peer.md"})]
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_call_agent_tool_loop", _loop_fake(script)), \
             unittest.mock.patch.object(serve, "_http_json", return_value=http_result):
            return asyncio.run(serve._ask_core(state, "question", "agent-2"))

    def test_filename_error(self):
        state = _roster_state()
        result = self._run(state, {"error": "denied"})
        self.assertEqual(result["reply"], "final answer")

    def test_filename_fallback(self):
        state = _roster_state()
        result = self._run(state, {"foo": "bar"})
        self.assertEqual(result["reply"], "final answer")



class PendingAskDrainPassError(unittest.TestCase):
    def setUp(self):
        serve._pending_ask_inflight.clear()
        serve._take_pending_ask_results()

    def test_ask_core_raises(self):
        state = _roster_state()
        state["_pendingAsks"] = [{"id": "ask-1", "question": "q", "ts": 1}]
        with unittest.mock.patch.object(sim, "_eligible_candidates",
                                        return_value=["agent-2"]), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            side_effect=RuntimeError("boom"))):
            serve._pending_ask_drain_pass(state)
        results = []
        for _ in range(200):
            out = serve._take_pending_ask_results()
            if out:
                results = out
                break
            time.sleep(0.01)
        self.assertEqual(len(results), 1)
        self.assertIn("ask drain failed", results[0]["error"])



class RouteLaneUnclearQueued(unittest.TestCase):
    def test_queued_saves_state(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "_unclear_lane_authority",
                                        return_value={"id": "agent-2"}), \
             unittest.mock.patch.object(serve, "_ask_core",
                                        new=unittest.mock.AsyncMock(
                                            return_value={"queued": True,
                                                          "askId": "a-1"})), \
             unittest.mock.patch.object(serve, "save_state_to_db") as save:
            result = asyncio.run(serve._route_lane_unclear(state, "text", "agent-1"))
        self.assertTrue(result["queued"])
        save.assert_called_once_with(state)



class ReleaseProductMalformed(unittest.TestCase):
    def test_malformed_body_defaults(self):
        state = _roster_state()
        state["products"] = {"prod-1": {"id": "prod-1", "name": "Atlas",
                                        "sandboxId": "workroom-shared",
                                        "status": "review", "ownerId": "agent-1"}}
        sandboxes = os.path.join(_MODULE_TMP_DIR, "test-sandboxes-malformed")
        os.makedirs(os.path.join(sandboxes, "workroom-shared"), exist_ok=True)
        with open(os.path.join(sandboxes, "workroom-shared", "code.py"), "w") as f:
            f.write("print('hi')")
        with _player_env(state), \
             unittest.mock.patch.object(serve, "SANDBOXES_DIR", sandboxes), \
             unittest.mock.patch.object(sim, "next_product_revision",
                                        return_value=(1, {})), \
             unittest.mock.patch.object(sim, "product_release_record",
                                        return_value={"revision": 1}), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}):
            r = _post(_client(), "/api/intent/product/prod-1/release", content=b"not-json")
        self.assertEqual(r.status_code, 200)



class WikiBranchTests(unittest.TestCase):
    def _wiki_state(self):
        state = _roster_state()
        state["wiki"] = {
            "pages": {"w-1": {"id": "w-1", "title": "T", "category": "operations"}},
            "categories": {"operations": {"label": "Operations", "order": 0}},
        }
        return state

    def test_get_page_oserror(self):
        state = self._wiki_state()
        path = os.path.join(serve.LIBRARY_DIR, "wiki", "operations", "w-1.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")
        real_open = open

        def fake_open(*a, **k):
            if a and str(a[0]).endswith("w-1.md"):
                raise OSError("boom")
            return real_open(*a, **k)

        with _player_env(state), unittest.mock.patch("builtins.open", side_effect=fake_open):
            r = _client().get("/api/intent/wiki/page/w-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["page"]["body"], "")

    def test_propose_invalid_destination(self):
        state = self._wiki_state()
        state["wiki"]["categories"]["../../../../evil"] = {"label": "E", "order": 9}
        with _agent_env(state, requester="agent-3"):
            r = _post(_client(), "/api/intent/wiki/propose",
                      {"id": "p2", "category": "../../../../evil", "body": "b"})
        self.assertEqual(r.status_code, 400)

    def test_list_proposals_skips_non_json(self):
        shutil.rmtree(os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki"),
                      ignore_errors=True)
        base = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki", "operations")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "note.txt"), "w") as f:
            f.write("not a proposal")
        with open(os.path.join(base, "good.json"), "w") as f:
            json.dump({"id": "w-1", "title": "T", "category": "operations",
                       "summary": "s", "proposedBy": "agent-3", "proposedAt": 1}, f)
        with _player_env(self._wiki_state()):
            r = _client().get("/api/intent/wiki/proposals")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["proposals"]), 1)
        self.assertEqual(r.json()["proposals"][0]["id"], "w-1")

    def test_approve_write_failed(self):
        state = self._wiki_state()
        base = os.path.join(serve.LIBRARY_DIR, "pending_review", "wiki", "operations")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "w-1.json"), "w") as f:
            json.dump({"id": "w-1", "title": "T", "category": "operations",
                       "body": "b", "proposedBy": "agent-3"}, f)
        with _agent_env(state), \
             unittest.mock.patch.object(sim, "wiki_write_page",
                                        return_value=(None, False)):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/approve",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 400)

    def test_reject_invalid_destination(self):
        state = self._wiki_state()
        with _agent_env(state), \
             unittest.mock.patch.object(serve, "_find_wiki_proposal",
                                        return_value=({"id": "w-1"}, "../../escape.json")):
            r = _post(_client(), "/api/intent/wiki/proposal/w-1/reject",
                      {"category": "operations"})
        self.assertEqual(r.status_code, 400)



class TeamAgreementApi(unittest.TestCase):
    def test_player_agreement_write_and_resolved(self):
        state = _roster_state()
        with _player_env(state):
            r = _client().put("/api/teams/team-1",
                              json={"agreement": {"scrumMasterMinTeamSize": 6,
                                                  "bogusKey": 1,
                                                  "maxTeamMembers": 999}})
            self.assertEqual(r.status_code, 200, r.text)
            ag = state["teams"][0]["agreement"]
            self.assertEqual(ag.get("scrumMasterMinTeamSize"), 6)
            self.assertNotIn("bogusKey", ag)
            r = _client().get("/api/teams")
            self.assertEqual(r.status_code, 200)
            t1 = [t for t in r.json()["teams"] if t["id"] == "team-1"][0]
            self.assertEqual(t1["agreement"], ag)
            # The resolved surface mirrors the operator-policy
            # schema key for key, so it grows when the JSON
            # policy grows (no hardcoded count).
            import core.config as _cfg
            self.assertEqual(len(t1["agreementResolved"]),
                             len(_cfg.OPERATOR_POLICY_DEFAULT))
            self.assertEqual(t1["agreementResolved"]["scrumMasterMinTeamSize"], 6)
            # ceiling clamp at read: team stored 999, operator cap 6 -> 6
            self.assertEqual(t1["agreementResolved"]["maxTeamMembers"], 6)
            # clear
            r = _client().put("/api/teams/team-1", json={"agreement": None})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIsNone(state["teams"][0]["agreement"])
            # bad type
            r = _client().put("/api/teams/team-1", json={"agreement": [1]})
            self.assertEqual(r.status_code, 400)

    def test_player_name_only_still_401(self):
        with _player_env(_roster_state()):
            r = _client().put("/api/teams/team-1", json={"name": "Beta"})
        self.assertEqual(r.status_code, 401)

    def test_non_director_agent_agreement_403(self):
        state = _roster_state()
        with _agent_env(state, requester="agent-3"):
            r = _client().put("/api/teams/team-1",
                              json={"agreement": {"scrumMasterMinTeamSize": 6}})
        self.assertEqual(r.status_code, 403)

    def test_director_agent_agreement_ok(self):
        state = _roster_state()
        with _agent_env(state, requester="agent-2"):
            r = _client().put("/api/teams/team-1",
                              json={"agreement": {"scrumMasterMinTeamSize": 6},
                                    "name": "Alpha2"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(state["teams"][0]["name"], "Alpha2")
        self.assertEqual(state["teams"][0]["agreement"]["scrumMasterMinTeamSize"], 6)
        self.assertEqual(state["teams"][0]["updatedBy"], "agent-2")

    def test_player_agreement_with_name_honored(self):
        state = _roster_state()
        with _player_env(state):
            r = _client().put("/api/teams/team-1",
                              json={"agreement": {"taskCycleS": 7.5}, "name": "Renamed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(state["teams"][0]["name"], "Renamed")
