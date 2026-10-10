"""Coverage for the player's work feedback: /api/player-inbox/{id}/feedback.

The player can rate and comment ONLY on completed work the think tank shared
with them (card_report inbox messages, see sim._deliver_card_report) -- never
on chat replies or pending questions. The feedback is stamped onto the inbox
message and pushed into the completing agent's feedback buffer via
sim._append_feedback, so the agent carries it into its next task.

Same isolation contract as test_serve_gap_D.py: module patches the derived
paths + rate limit, each test injects state via patched get_state_from_db,
and sim calls needing deterministic control are patched per-test.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve
import sim

_MODULE_TMP_DIR = tempfile.mkdtemp(prefix="think-tank-player-feedback-")

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


def _roster_state():
    return {
        "sim": {"owner": "server"},
        "agentRoster": [
            {"id": "agent-1", "name": "Theo", "role": "admin", "isAdmin": True},
        ],
        "agents": {
            "agent-1": {"name": "Theo", "busy": False, "offDuty": False, "role": "admin",
                        "profile": {"mission": "run the think tank"}},
        },
        "teams": [],
        "workQueue": [],
        "playerInbox": [],
    }


def _player_env(state=None, **patches):
    p = {"get_state_from_db": lambda: state, "verify_session": lambda cookie: True}
    p.update(patches)
    return unittest.mock.patch.multiple(serve, **p)


def _client():
    return TestClient(serve.app)


def _post(client, path, payload=None, content=None):
    if content is not None:
        return client.post(path, content=content,
                           headers={"Content-Type": "application/json"})
    return client.post(path, json=payload or {})


def _card_report(id, task_id="task-1", agent_id="agent-1"):
    return {
        "id": id, "kind": "card_report", "taskId": task_id, "agentId": agent_id,
        "title": "Ship the observatory report", "body": "Done.", "createdAt": 1,
    }


class WorkFeedback(unittest.TestCase):
    def test_unauthorized(self):
        with _player_env(_roster_state(), verify_session=lambda c: False):
            r = _post(_client(), "/api/player-inbox/m-1/feedback", {"rating": 5})
        self.assertEqual(r.status_code, 401)

    def test_malformed_body(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/player-inbox/m-1/feedback", content=b"x")
        self.assertEqual(r.status_code, 400)

    def test_no_state(self):
        with _player_env(None):
            r = _post(_client(), "/api/player-inbox/m-1/feedback", {"rating": 5})
        self.assertEqual(r.status_code, 503)

    def test_unknown_message(self):
        with _player_env(_roster_state()):
            r = _post(_client(), "/api/player-inbox/m-1/feedback", {"rating": 5})
        self.assertEqual(r.status_code, 404)

    def test_non_completed_work_rejected(self):
        state = _roster_state()
        state["playerInbox"] = [{"id": "ask-1", "kind": "ask", "question": "hi"}]
        with _player_env(state):
            r = _post(_client(), "/api/player-inbox/ask-1/feedback", {"rating": 5})
        self.assertEqual(r.status_code, 400)
        self.assertIn("completed work", r.json()["error"])

    def test_rating_validation(self):
        for bad in (0, 6, "x", 2.5):
            with self.subTest(rating=bad):
                state = _roster_state()
                state["playerInbox"] = [_card_report("m-1")]
                with _player_env(state):
                    r = _post(_client(), "/api/player-inbox/m-1/feedback", {"rating": bad})
                self.assertEqual(r.status_code, 400)

    def test_requires_rating_or_comment(self):
        state = _roster_state()
        state["playerInbox"] = [_card_report("m-1")]
        with _player_env(state):
            r = _post(_client(), "/api/player-inbox/m-1/feedback", {})
        self.assertEqual(r.status_code, 400)

    def test_success_rating_stamps_and_feeds_agent(self):
        state = _roster_state()
        state["playerInbox"] = [_card_report("m-1")]
        with _player_env(state):
            r = _post(_client(), "/api/player-inbox/m-1/feedback",
                      {"rating": 4, "comment": "Great writeup"})
        self.assertEqual(r.status_code, 200)
        m = state["playerInbox"][0]
        self.assertEqual(m["feedback"]["rating"], 4)
        self.assertEqual(m["feedback"]["comment"], "Great writeup")
        self.assertEqual(m["feedback"]["agentId"], "agent-1")
        bucket = state["_feedback"]["agent-1"]
        self.assertEqual(bucket[-1]["source"], "player_feedback")
        self.assertIn("Ship the observatory report", bucket[-1]["text"])
        self.assertIn("4/5", bucket[-1]["text"])
        self.assertIn("Great writeup", bucket[-1]["text"])

    def test_comment_only(self):
        state = _roster_state()
        state["playerInbox"] = [_card_report("m-1")]
        with _player_env(state):
            r = _post(_client(), "/api/player-inbox/m-1/feedback", {"comment": "redo the tables"})
        self.assertEqual(r.status_code, 200)
        m = state["playerInbox"][0]
        self.assertIsNone(m["feedback"]["rating"])
        self.assertEqual(m["feedback"]["comment"], "redo the tables")

    def test_update_overwrites_previous_feedback(self):
        state = _roster_state()
        state["playerInbox"] = [_card_report("m-1")]
        with _player_env(state):
            r1 = _post(_client(), "/api/player-inbox/m-1/feedback", {"rating": 2, "comment": "weak"})
            r2 = _post(_client(), "/api/player-inbox/m-1/feedback", {"rating": 5, "comment": "fixed it"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        m = state["playerInbox"][0]
        self.assertEqual(m["feedback"]["rating"], 5)
        self.assertEqual(m["feedback"]["comment"], "fixed it")
        self.assertEqual(len(state["_feedback"]["agent-1"]), 2)

    def test_card_report_carries_agent_id(self):
        state = _roster_state()
        task = {"id": "task-9", "assignedTo": "agent-1", "title": "Deploy release",
                "taskType": "task", "note": "done", "userStory": "ship it"}
        sim._deliver_card_report(state, task, "completed")
        card = state["playerInbox"][0]
        self.assertEqual(card["kind"], "card_report")
        self.assertEqual(card["agentId"], "agent-1")
        self.assertEqual(card["taskId"], "task-9")


if __name__ == "__main__":
    unittest.main()