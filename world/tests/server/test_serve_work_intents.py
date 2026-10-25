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


class RoomDefinitions(unittest.TestCase):
    def test_backfill_and_preserve(self):
        state = {}
        defs = serve._room_definitions(state)
        self.assertIn("pressoffice", defs)
        self.assertIn("purpose", defs["pressoffice"])

    def test_preserves_director_purpose_and_repairs_label(self):
        state = {"roomDefinitions": {"pressoffice": {"purpose": "edited"}}}
        defs = serve._room_definitions(state)
        self.assertEqual(defs["pressoffice"]["purpose"], "edited")
        self.assertEqual(defs["pressoffice"]["label"], "Work Room")



class FreeAuthority(unittest.TestCase):
    def test_free_admin_picked(self):
        state = _roster_state()
        self.assertEqual(serve._free_authority(state)["id"], "agent-1")

    def test_busy_admin_then_free_director(self):
        state = _roster_state()
        state["agents"]["agent-1"]["busy"] = True
        self.assertEqual(serve._free_authority(state)["id"], "agent-2")

    def test_director_with_director_attr_skipped(self):
        state = _roster_state()
        state["agents"]["agent-1"]["busy"] = True
        state["agentRoster"][1]["director"] = "agent-1"
        self.assertIsNone(serve._free_authority(state))

    def test_all_busy(self):
        state = _roster_state()
        for a in state["agents"].values():
            a["busy"] = True
        self.assertIsNone(serve._free_authority(state))



class WakeAuthority(unittest.TestCase):
    def test_wakes_off_duty_admin(self):
        state = _roster_state()
        state["agents"]["agent-1"]["offDuty"] = True
        with unittest.mock.patch.object(sim, "appear_from_outskirts") as wake:
            d = serve._wake_authority_on_request(state)
        self.assertEqual(d["id"], "agent-1")
        wake.assert_called_once_with(state, "agent-1")

    def test_wake_helper_falls_back_to_flag_clear(self):
        state = _roster_state()
        state["agents"]["agent-1"]["offDuty"] = True
        with unittest.mock.patch.object(sim, "appear_from_outskirts", side_effect=RuntimeError):
            d = serve._wake_authority_on_request(state)
        self.assertEqual(d["id"], "agent-1")
        self.assertFalse(state["agents"]["agent-1"]["offDuty"])
        self.assertTrue(state["agents"]["agent-1"]["visible"])

    def test_no_op_when_all_busy(self):
        state = _roster_state()
        for a in state["agents"].values():
            a["busy"] = True
        self.assertIsNone(serve._wake_authority_on_request(state))



class StaffingTeam(unittest.TestCase):
    def test_own_team_when_free(self):
        state = _roster_state()
        t = serve._staffing_team_for_authority(state, "agent-2")
        self.assertEqual(t["id"], "team-1")

    def test_first_free_team_when_own_busy(self):
        state = _roster_state()
        state["sprints"] = {"s-1": {"status": "active", "teamIds": ["team-1"]}}
        state["teams"].append({"id": "team-2", "name": "Beta", "directorId": "agent-9"})
        t = serve._staffing_team_for_authority(state, "agent-2")
        self.assertEqual(t["id"], "team-2")

    def test_none_when_no_team_free(self):
        state = _roster_state()
        state["sprints"] = {"s-1": {"status": "active", "teamIds": ["team-1", "team-2"]}}
        state["teams"].append({"id": "team-2", "name": "Beta", "directorId": "agent-9"})
        self.assertIsNone(serve._staffing_team_for_authority(state, "agent-2"))



class AllTeamsBusy(unittest.TestCase):
    def test_no_teams(self):
        self.assertFalse(serve._all_teams_busy_in_sprint({"teams": []}))

    def test_no_active_sprints(self):
        state = {"teams": [{"id": "team-1"}], "sprints": {}}
        self.assertFalse(serve._all_teams_busy_in_sprint(state))

    def test_some_team_not_in_sprint(self):
        state = _roster_state()
        state["sprints"] = {"s-1": {"status": "active", "teamIds": ["team-1"]}}
        state["teams"].append({"id": "team-2", "name": "Beta", "directorId": "agent-9"})
        self.assertFalse(serve._all_teams_busy_in_sprint(state))

    def test_every_team_busy(self):
        state = _roster_state()
        state["sprints"] = {"s-1": {"status": "active", "teamIds": ["team-1"]}}
        self.assertTrue(serve._all_teams_busy_in_sprint(state))



class BreakdownIntoSharedBacklog(unittest.TestCase):
    def test_no_admin_returns_none(self):
        self.assertIsNone(serve._breakdown_into_shared_backlog(_roster_state(), "goal", None))

    def test_http_json_error_returns_none(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_http_json", return_value={"error": "boom"}):
            self.assertIsNone(serve._breakdown_into_shared_backlog(state, "goal", "agent-1"))

    def test_reply_not_json_returns_none(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_http_json", return_value={"reply": "not json"}):
            self.assertIsNone(serve._breakdown_into_shared_backlog(state, "goal", "agent-1"))

    def test_empty_items_returns_none(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_http_json",
                                        return_value={"reply": json.dumps({"feature": "F", "items": []})}):
            self.assertIsNone(serve._breakdown_into_shared_backlog(state, "goal", "agent-1"))

    def test_add_backlog_item_none_returns_none(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_http_json",
                                        return_value={"reply": json.dumps(
                                            {"feature": "F", "items": [{"title": "a"}]})}), \
             unittest.mock.patch.object(sim, "add_backlog_item", return_value=None):
            self.assertIsNone(serve._breakdown_into_shared_backlog(state, "goal", "agent-1"))

    def test_success(self):
        state = _roster_state()
        reply = json.dumps({"feature": "Big Ask", "items": [
            {"title": "Do the thing", "type": "story", "sizeEstimate": "M"}]})
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_http_json", return_value={"reply": reply}):
            out = serve._breakdown_into_shared_backlog(state, "goal", "agent-1")
        self.assertIsNotNone(out)
        self.assertEqual(len(out["items"]), 1)



class IntentAssignBigTask(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/assign-big-task", content=b"not-json")
        self.assertEqual(r.status_code, 400)

    def test_no_goal(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "  "})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 503)

    def test_all_busy_backlogged(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_all_teams_busy_in_sprint", return_value=True), \
             unittest.mock.patch.object(serve, "_breakdown_into_shared_backlog",
                                        return_value={"feature": {"id": "f-1", "name": "Big"},
                                                      "items": [{"id": "b-1"}]}):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["backlogged"])
        self.assertEqual(r.json()["feature"], "f-1")

    def test_all_busy_spawn_team(self):
        state = _roster_state()
        new_team = {"id": "team-9", "name": "Bravo", "members": ["agent-5"]}
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_all_teams_busy_in_sprint", return_value=True), \
             unittest.mock.patch.object(serve, "_breakdown_into_shared_backlog",
                                        return_value=None), \
             unittest.mock.patch.object(sim, "spawn_new_team_for_request", return_value=new_team):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["newTeam"])
        self.assertEqual(r.json()["team"], "team-9")

    def test_no_authority_and_none_woken(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority", return_value=None), \
             unittest.mock.patch.object(serve, "_wake_authority_on_request", return_value=None):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("No admin or director is free", r.json()["error"])

    def test_woken_authority_staffs(self):
        state = _roster_state()
        team = {"id": "team-1", "name": "Alpha", "directorId": "agent-2"}
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority", return_value=None), \
             unittest.mock.patch.object(serve, "_wake_authority_on_request",
                                        return_value={"id": "agent-1", "name": "Theo"}), \
             unittest.mock.patch.object(serve, "_staffing_team_for_authority",
                                        return_value=team), \
             unittest.mock.patch.object(sim, "file_large_request",
                                        return_value={"id": "req-1"}):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["staffed"])

    def test_no_free_team(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}), \
             unittest.mock.patch.object(serve, "_staffing_team_for_authority",
                                        return_value=None):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("No free team", r.json()["error"])

    def test_file_large_request_failure(self):
        state = _roster_state()
        team = {"id": "team-1", "name": "Alpha", "directorId": "agent-2"}
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}), \
             unittest.mock.patch.object(serve, "_staffing_team_for_authority",
                                        return_value=team), \
             unittest.mock.patch.object(sim, "file_large_request", return_value=None):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 500)

    def test_staffed_success(self):
        state = _roster_state()
        team = {"id": "team-1", "name": "Alpha", "directorId": "agent-2"}
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}), \
             unittest.mock.patch.object(serve, "_staffing_team_for_authority",
                                        return_value=team), \
             unittest.mock.patch.object(sim, "file_large_request",
                                        return_value={"id": "req-1"}):
            r = _post(_client(), "/api/intent/assign-big-task", {"goal": "g"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["staffed"])
        self.assertEqual(body["admin"], "agent-1")
        self.assertEqual(body["team"], "team-1")
        self.assertEqual(body["request"], "req-1")



class IntentRejectStory(unittest.TestCase):
    def _state_with_done_task(self):
        state = _roster_state()
        state["tasks"] = {"task-1": {"id": "task-1", "title": "Shipped thing",
                                     "status": "done", "assignedTo": "agent-3"}}
        return state

    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/story/task-1/reject", {"reason": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body_proceeds(self):
        state = self._state_with_done_task()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "_enter_peer_review",
                                        return_value={"reviewerIds": ["agent-2"]}), \
             unittest.mock.patch.object(sim, "_cascade_rereview", return_value=[]):
            r = _post(_client(), "/api/intent/story/task-1/reject", content=b"not-json")
        self.assertEqual(r.status_code, 200)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/story/task-1/reject", {"reason": "x"})
        self.assertEqual(r.status_code, 503)

    def test_unknown_story(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/story/nope/reject", {"reason": "x"})
        self.assertEqual(r.status_code, 404)

    def test_not_done(self):
        state = _roster_state()
        state["tasks"] = {"task-1": {"id": "task-1", "title": "T",
                                     "status": "in_progress", "assignedTo": "agent-3"}}
        with _player_env(state):
            r = _post(_client(), "/api/intent/story/task-1/reject", {"reason": "x"})
        self.assertEqual(r.status_code, 409)

    def test_no_author(self):
        state = _roster_state()
        state["tasks"] = {"task-1": {"id": "task-1", "title": "T", "status": "done"}}
        with _player_env(state):
            r = _post(_client(), "/api/intent/story/task-1/reject", {"reason": "x"})
        self.assertEqual(r.status_code, 409)

    def test_no_reviewer_available(self):
        state = self._state_with_done_task()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "_enter_peer_review", return_value=None):
            r = _post(_client(), "/api/intent/story/task-1/reject", {"reason": "x"})
        self.assertEqual(r.status_code, 409)

    def test_success(self):
        state = self._state_with_done_task()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "_enter_peer_review",
                                        return_value={"reviewerIds": ["agent-2"]}), \
             unittest.mock.patch.object(sim, "_cascade_rereview", return_value=["task-2"]):
            r = _post(_client(), "/api/intent/story/task-1/reject", {"reason": "redo it"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["taskId"], "task-1")
        self.assertEqual(body["author"], "agent-3")
        self.assertEqual(body["reviewers"], ["agent-2"])
        self.assertEqual(body["cascaded"], ["task-2"])
        mailbox = state["agents"]["agent-3"]["mailbox"]
        self.assertEqual(mailbox[0]["kind"], "player_veto")



class IntentPublish(unittest.TestCase):
    def _publish_env(self, **patches):
        base = {
            "PUBLISH_REPO": "acme/think-tank",
            "PUBLISH_REMOTE_URL": "https://github.com/acme/think-tank.git",
            "_stage_released_work": lambda state: ["projects/proj1"],
            "_write_publish_readme": lambda staged: None,
            "_run_git_sync": lambda *a, **k: None,
        }
        base.update(patches)
        return unittest.mock.patch.multiple(serve, **base)

    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/publish", {})
        self.assertEqual(r.status_code, 401)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/publish", {})
        self.assertEqual(r.status_code, 503)

    def test_not_configured(self):
        with _player_env(_roster_state()), \
             unittest.mock.patch.multiple(serve, PUBLISH_REPO="", PUBLISH_REMOTE_URL=""):
            r = _post(_client(), "/api/intent/publish", {})
        self.assertEqual(r.status_code, 409)

    def test_nothing_staged(self):
        with _player_env(_roster_state()), self._publish_env(_stage_released_work=lambda s: []):
            r = _post(_client(), "/api/intent/publish", {})
        self.assertEqual(r.status_code, 409)

    def test_success(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "gh":
                return SimpleNamespace(returncode=0, stdout="tok\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="pushed\n", stderr="")

        with _player_env(_roster_state()), self._publish_env(), \
             unittest.mock.patch.object(serve.subprocess, "run", side_effect=fake_run):
            r = _post(_client(), "/api/intent/publish", {})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["repo"], "acme/think-tank")

    def test_push_rejected(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "gh":
                return SimpleNamespace(returncode=0, stdout="tok\n", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="non-fast-forward")

        with _player_env(_roster_state()), self._publish_env(), \
             unittest.mock.patch.object(serve.subprocess, "run", side_effect=fake_run):
            r = _post(_client(), "/api/intent/publish", {})
        self.assertEqual(r.status_code, 502)
        self.assertIn("push rejected", r.json()["error"])

    def test_timeout(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "gh":
                return SimpleNamespace(returncode=0, stdout="tok\n", stderr="")
            raise subprocess.TimeoutExpired(cmd="git push", timeout=60)

        with _player_env(_roster_state()), self._publish_env(), \
             unittest.mock.patch.object(serve.subprocess, "run", side_effect=fake_run):
            r = _post(_client(), "/api/intent/publish", {})
        self.assertEqual(r.status_code, 502)
        self.assertIn("timed out", r.json()["error"])

    def test_generic_failure(self):
        with _player_env(_roster_state()), \
             self._publish_env(_run_git_sync=unittest.mock.Mock(
                 side_effect=RuntimeError("boom"))):
            r = _post(_client(), "/api/intent/publish", {})
        self.assertEqual(r.status_code, 502)
        self.assertIn("publish failed", r.json()["error"])



class IntentPromoteSpike(unittest.TestCase):
    def _spike_state(self, task_type="spike", status="done", **task_over):
        state = _roster_state()
        task = {"id": "spike-1", "title": "Investigate X", "taskType": task_type,
                "status": status, "note": "short pointer note", "goal": "G",
                "budgetMs": 60000}
        task.update(task_over)
        state["tasks"] = {"spike-1": task}
        return state

    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/spike/spike-1/promote", {"taskType": "code"})
        self.assertEqual(r.status_code, 401)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/spike/spike-1/promote", {"taskType": "code"})
        self.assertEqual(r.status_code, 503)

    def test_unknown_spike(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/spike/nope/promote", {"taskType": "code"})
        self.assertEqual(r.status_code, 404)

    def test_not_a_spike(self):
        with _player_env(self._spike_state(task_type="story")):
            r = _post(_client(), "/api/intent/spike/spike-1/promote", {"taskType": "code"})
        self.assertEqual(r.status_code, 409)

    def test_not_done(self):
        with _player_env(self._spike_state(status="working")):
            r = _post(_client(), "/api/intent/spike/spike-1/promote", {"taskType": "code"})
        self.assertEqual(r.status_code, 409)

    def test_malformed_body_returns_400(self):
        state = self._spike_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/intent/spike/spike-1/promote", content=b"not-json")
        self.assertEqual(r.status_code, 400)

    def test_invalid_room_defaults_to_pressoffice(self):
        state = self._spike_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/intent/spike/spike-1/promote",
                      {"taskType": "code", "room": "bogus"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["queued"]["room"], "pressoffice")

    def test_invalid_task_type(self):
        with _player_env(self._spike_state()):
            r = _post(_client(), "/api/intent/spike/spike-1/promote", {"taskType": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_success_from_note_fallback(self):
        state = self._spike_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/intent/spike/spike-1/promote",
                      {"taskType": "code", "room": "media"})
        self.assertEqual(r.status_code, 200)
        queued = r.json()["queued"]
        self.assertEqual(queued["taskType"], "code")
        self.assertEqual(queued["room"], "media")
        self.assertIn("short pointer note", state["workQueue"][0]["instructions"])

    def test_success_from_library_file(self):
        lib = os.path.join(serve.LIBRARY_DIR, "spikes")
        os.makedirs(lib, exist_ok=True)
        target = os.path.join(lib, "findings.md")
        with open(target, "w") as f:
            f.write("The full research findings here.")
        state = self._spike_state(libraryPath="spikes/findings.md")
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/intent/spike/spike-1/promote",
                      {"taskType": "spike", "room": "observatory"})
        self.assertEqual(r.status_code, 200)
        queued = r.json()["queued"]
        self.assertEqual(queued["taskType"], "spike")
        self.assertIn("full research findings", state["workQueue"][0]["instructions"])

    def test_oserror_finding_falls_back(self):
        lib = os.path.join(serve.LIBRARY_DIR, "spikes")
        os.makedirs(lib, exist_ok=True)
        target = os.path.join(lib, "findings.md")
        with open(target, "w") as f:
            f.write("findings")
        state = self._spike_state(libraryPath="spikes/findings.md")
        real_open = open

        def raise_on_target(path, *a, **k):
            if path == target:
                raise OSError("boom")
            return real_open(path, *a, **k)

        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", return_value=None), \
             unittest.mock.patch("builtins.open", side_effect=raise_on_target):
            r = _post(_client(), "/api/intent/spike/spike-1/promote", {"taskType": "code"})
        self.assertEqual(r.status_code, 200)



class ShadowLedger(unittest.TestCase):
    def test_no_state(self):
        with _player_env(None):
            r = _client().get("/api/shadow")
        self.assertEqual(r.status_code, 503)

    def test_get_ledger(self):
        state = _roster_state()
        state["shadowLedger"] = [{"title": "a"}, {"title": "b"}]
        with _player_env(state):
            r = _client().get("/api/shadow")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        self.assertEqual(r.json()["shadow"][0]["title"], "b")



class PromoteShadowEntry(unittest.TestCase):
    def _shadow_state(self):
        state = _roster_state()
        state["shadowLedger"] = [{"title": "Dry run", "note": "draft finding",
                                  "room": "pressoffice", "taskType": "code",
                                  "projectLabel": "proj"}]
        return state

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/shadow/0/promote", {})
        self.assertEqual(r.status_code, 503)

    def test_bad_index(self):
        with _player_env(self._shadow_state()):
            r = _post(_client(), "/api/shadow/abc/promote", {})
        self.assertEqual(r.status_code, 400)

    def test_out_of_range(self):
        with _player_env(self._shadow_state()):
            r = _post(_client(), "/api/shadow/99/promote", {})
        self.assertEqual(r.status_code, 404)

    def test_already_promoted(self):
        state = self._shadow_state()
        state["shadowLedger"][0]["promoted"] = True
        with _player_env(state):
            r = _post(_client(), "/api/shadow/0/promote", {})
        self.assertEqual(r.status_code, 409)

    def test_success_with_invalid_overrides(self):
        state = self._shadow_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/shadow/0/promote",
                      {"room": "bogus", "taskType": "bogus"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["promoted"], 0)
        self.assertEqual(body["queued"]["room"], "pressoffice")
        self.assertEqual(body["queued"]["taskType"], "code")
        self.assertTrue(state["shadowLedger"][0]["promoted"])
        self.assertIn("draft finding", state["workQueue"][0]["instructions"])

    def test_spike_type(self):
        state = self._shadow_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "queue_work", side_effect=_queue_work_append):
            r = _post(_client(), "/api/shadow/0/promote", {"taskType": "spike"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["queued"]["taskType"], "spike")



class AgentRecordFor(unittest.TestCase):
    def test_roster_hit(self):
        state = _roster_state()
        self.assertEqual(serve._agent_record_for(state, "agent-1")["name"], "Theo")

    def test_agents_hit(self):
        state = _roster_state()
        state["agentRoster"] = []
        self.assertEqual(serve._agent_record_for(state, "agent-3")["name"], "Bo")

    def test_unknown(self):
        self.assertEqual(serve._agent_record_for(_roster_state(), "ghost"), {})



class IntentSprint(unittest.TestCase):
    def _items(self):
        return [{"title": "Build the thing", "room": "pressoffice"}]

    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items()})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/sprint", content=b"not-json")
        self.assertEqual(r.status_code, 400)

    def test_no_goal(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/sprint", {"items": self._items()})
        self.assertEqual(r.status_code, 400)

    def test_no_items(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/sprint", {"goal": "g", "items": []})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items()})
        self.assertEqual(r.status_code, 503)

    def test_no_free_authority(self):
        with _player_env(_roster_state()), \
             unittest.mock.patch.object(serve, "_free_authority", return_value=None):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items()})
        self.assertEqual(r.status_code, 200)
        self.assertIn("nobody can sponsor", r.json()["error"])

    def test_missing_scrum_master(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}), \
             unittest.mock.patch.object(serve, "_teams_missing_scrum_master",
                                        return_value=[{"id": "team-1", "name": "Alpha"}]):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items(), "teamIds": ["team-1"]})
        self.assertEqual(r.status_code, 409)

    def test_bad_worker_count(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items(), "workerCount": "many"})
        self.assertEqual(r.status_code, 400)

    def test_out_of_range_worker_count(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items(), "workerCount": 999})
        self.assertEqual(r.status_code, 400)

    def test_queue_sprint_none(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}), \
             unittest.mock.patch.object(sim, "queue_sprint", return_value=None):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "items": self._items()})
        self.assertEqual(r.status_code, 400)

    def test_success_with_target_date(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(serve, "_free_authority",
                                        return_value={"id": "agent-1", "name": "Theo"}):
            r = _post(_client(), "/api/intent/sprint",
                      {"goal": "g", "name": "Sprint 1", "items": self._items(),
                       "targetDate": 1700000000000, "workerCount": 2})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["queuedItems"][0]["title"], "Build the thing")



class GetSprints(unittest.TestCase):
    def test_no_state(self):
        with _player_env(None):
            r = _client().get("/api/intent/sprints")
        self.assertEqual(r.json(), {"sprints": {}})

    def test_success(self):
        state = _roster_state()
        state["sprints"] = {"s-1": {"id": "s-1", "goal": "g", "status": "active"}}
        with _player_env(state), \
             unittest.mock.patch.object(sim, "sprint_progress", return_value={"done": 1}):
            r = _client().get("/api/intent/sprints")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sprints"]["s-1"]["progress"]["done"], 1)



class CloseSprint(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/sprint/s-1/close", {})
        self.assertEqual(r.status_code, 401)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/sprint/s-1/close", {})
        self.assertEqual(r.status_code, 503)

    def test_unknown_sprint(self):
        with _player_env(_roster_state()), \
             unittest.mock.patch.object(sim, "close_sprint", return_value=None):
            r = _post(_client(), "/api/intent/sprint/s-1/close", {})
        self.assertEqual(r.status_code, 404)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "close_sprint",
                                        return_value={"id": "s-1", "ownerId": "agent-1",
                                                      "goal": "g"}):
            r = _post(_client(), "/api/intent/sprint/s-1/close", {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sprint"]["id"], "s-1")



class GetIssues(unittest.TestCase):
    def test_no_state(self):
        with _player_env(None):
            r = _client().get("/api/intent/issues")
        self.assertEqual(r.status_code, 503)

    def test_success(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "list_issues", return_value=[{"key": "TEAM-1"}]):
            r = _client().get("/api/intent/issues?teamId=team-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["issues"][0]["key"], "TEAM-1")



class CreateIssue(unittest.TestCase):
    def _issue_body(self, **over):
        body = {"teamId": "team-1", "type": "story", "summary": "Build stuff",
                "feature": "Feature A"}
        body.update(over)
        return body

    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/intent/issues", self._issue_body())
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/issues", content=b"not-json")
        self.assertEqual(r.status_code, 400)

    def test_missing_fields(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/intent/issues", {"teamId": "team-1"})
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/intent/issues", self._issue_body())
        self.assertEqual(r.status_code, 503)

    def test_unknown_team(self):
        state = _roster_state()
        with _player_env(state):
            r = _post(_client(), "/api/intent/issues", self._issue_body(teamId="nope"))
        self.assertEqual(r.status_code, 404)

    def test_invalid_type(self):
        state = _roster_state()
        with _player_env(state):
            r = _post(_client(), "/api/intent/issues", self._issue_body(type="bogus"))
        self.assertEqual(r.status_code, 400)

    def test_file_issue_none(self):
        state = _roster_state()
        with _player_env(state), \
             unittest.mock.patch.object(sim, "file_issue", return_value=None):
            r = _post(_client(), "/api/intent/issues", self._issue_body())
        self.assertEqual(r.status_code, 400)

    def test_success(self):
        state = _roster_state()
        with _player_env(state):
            r = _post(_client(), "/api/intent/issues",
                      self._issue_body(description={"userStory": "As a user..."}))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
