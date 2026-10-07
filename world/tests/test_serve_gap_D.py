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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
        return serve._make_web_tools_executor("agent-3", "key", struck_tools=struck_tools)

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


def _loop_fake(script=None, reply="final answer", capture=None):
    def fake(model, messages, tools, execute_tool, max_iterations, max_tokens,
             force_first_tool=None, village_id=None):
        if capture is not None:
            capture["force_first_tool"] = force_first_tool
        for name, args in (script or []):
            execute_tool(name, args)
        return reply
    return fake


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

    class _Conn:
        def __init__(self, review_row, tape_row):
            self.review_row = review_row
            self.tape_row = tape_row

        def execute(self, sql, *args):
            if "weekly_reviews" in sql:
                return TeamDigestText._Cursor(self.review_row)
            return TeamDigestText._Cursor(self.tape_row)

    def _patched_db(self, review_row, tape_row):
        @contextmanager
        def fake_db():
            yield TeamDigestText._Conn(review_row, tape_row)
        return unittest.mock.patch.object(serve, "_db", fake_db)

    def test_no_review_row(self):
        with self._patched_db(None, (3, 2)):
            out = serve._team_digest_text()
        self.assertIn("No weekly review has been generated yet", out)
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


if __name__ == '__main__':
    unittest.main()


