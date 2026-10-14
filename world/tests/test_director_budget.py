"""Per-director monthly budget slices (R8): the admin slices the
tank's overall budget between directors (POST /api/budget/director);
each slice is a hard monthly ceiling for that director's WHOLE team.
A director who burns their slice pauses their own team's calls only
and asks the admin for a raise (POST /api/budget/request, mailed to
the admin). Covers the Bank rollup (accrual + cap status), the
shared chokepoint gate, and the three API surfaces (view, set,
request).

Same isolation contract as test_serve_gap_D.py: module patcher for
the derived-time paths, fresh in-memory state per test via patched
get_state_from_db.
"""
import datetime
import json
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
import bank

_MODULE_TMP_DIR = tempfile.mkdtemp(prefix="think-tank-director-budget-")

_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_PATCHER
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
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _client():
    return TestClient(serve.app)


MONTH = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')


def _state():
    return {
        "sim": {"owner": "server"},
        "agentRoster": [
            {"id": "admin-1", "name": "Theo", "role": "admin", "isAdmin": True},
            {"id": "dir-1", "name": "Nora", "role": "director", "isDirector": True},
            {"id": "dir-2", "name": "Ruth", "role": "director", "isDirector": True},
            {"id": "worker-1", "name": "Ada", "role": "worker", "director": "dir-1"},
        ],
        "agents": {
            "admin-1": {"name": "Theo", "busy": False, "offDuty": False,
                        "role": "admin", "profile": {"mission": "run the tank"}},
            "dir-1": {"name": "Nora", "busy": False, "offDuty": False,
                      "role": "director", "profile": {"mission": "lead"}},
            "dir-2": {"name": "Ruth", "busy": False, "offDuty": False,
                      "role": "director", "profile": {"mission": "lead"}},
            "worker-1": {"name": "Ada", "busy": False, "offDuty": False,
                         "role": "worker", "profile": {"mission": "build"}},
        },
        "teams": [
            {"id": "team-1", "name": "Alpha", "directorId": "dir-1"},
            {"id": "team-2", "name": "Beta", "directorId": "dir-2"},
        ],
        "directorBudgets": {},
        "budgetRequests": [],
        "workQueue": [],
    }


def _bank_env(state, ledger):
    return unittest.mock.patch.multiple(
        serve,
        get_state_from_db=lambda: state,
        _spend_ledger_read=lambda: ledger,
        _spend_ledger_write=lambda ledger: None,
    )


def _api_env(state, player=False, agent=None, saved=None):
    patches = {
        "get_state_from_db": lambda: state,
        "save_state_to_db": (lambda s: saved.update(s)) if saved is not None else (lambda s: None),
        "verify_session": lambda cookie: player,
        "log_action": lambda *a, **k: None,
    }
    if agent:
        patches["_resolve_requester"] = lambda request: agent
        patches["verify_agent_key"] = lambda *a, **k: True
        patches["_valid_agent_key_presented"] = lambda key: True
    else:
        patches["_resolve_requester"] = lambda request: None
    return unittest.mock.patch.multiple(serve, **patches)


# ---------------------------------------------------------------------------
# Bank: the __director__/<id> rollup bucket and the cap status.
# ---------------------------------------------------------------------------


class DirectorBankRollup(unittest.TestCase):
    def test_spend_key_namespacing(self):
        self.assertEqual(bank._director_spend_key("dir-1"), "__director__/dir-1")

    def test_director_of_agent(self):
        state = _state()
        self.assertEqual(bank._director_of_agent("worker-1", state), "dir-1")
        # A director (and the admin) has no `director` pointer, so
        # their own spend answers to their OWN budget.
        self.assertEqual(bank._director_of_agent("dir-1", state), "dir-1")
        self.assertEqual(bank._director_of_agent("admin-1", state), "admin-1")
        self.assertIsNone(bank._director_of_agent("ghost", state))
        self.assertIsNone(bank._director_of_agent(None, state))

    def test_director_budget_map(self):
        state = _state()
        state["directorBudgets"] = {"dir-1": 5, "dir-2": 0, "bad": "x"}
        with _bank_env(state, {}):
            self.assertEqual(bank._director_budget_map(), {"dir-1": 5.0})
        state["directorBudgets"] = "not-a-dict"
        with _bank_env(state, {}):
            self.assertEqual(bank._director_budget_map(), {})

    def test_accrue_writes_director_bucket(self):
        state = _state()
        ledger = {}
        with _bank_env(state, ledger):
            bank._accrue_spend("__general__", 0.5, agent_id="worker-1")
        bucket = ledger["__director__/dir-1"]
        self.assertEqual(bucket["byMonth"][MONTH], 0.5)
        self.assertEqual(bucket["calls"], 1)
        # The service and agent rollups still land alongside.
        self.assertEqual(ledger["__general__"]["used"], 0.5)
        self.assertEqual(ledger["__agent__/worker-1"]["used"], 0.5)

    def test_accrue_without_agent_skips_director(self):
        state = _state()
        ledger = {}
        with _bank_env(state, ledger):
            bank._accrue_spend("__general__", 0.5)
        self.assertNotIn("__director__/dir-1", ledger)

    def test_spend_status_under_cap(self):
        state = _state()
        state["directorBudgets"] = {"dir-1": 10.0}
        ledger = {"__director__/dir-1": {"used": 1.0, "calls": 1,
                                         "byMonth": {MONTH: 1.0}}}
        with _bank_env(state, ledger):
            self.assertIsNone(bank._director_spend_status("worker-1"))

    def test_spend_status_at_cap(self):
        state = _state()
        state["directorBudgets"] = {"dir-1": 10.0}
        ledger = {"__director__/dir-1": {"used": 10.0, "calls": 2,
                                         "byMonth": {MONTH: 10.0}}}
        with _bank_env(state, ledger):
            self.assertEqual(bank._director_spend_status("worker-1"),
                             {"directorId": "dir-1", "cap": 10.0, "used": 10.0})

    def test_spend_status_feature_off(self):
        state = _state()  # no directorBudgets at all
        ledger = {"__director__/dir-1": {"used": 99.0, "calls": 1,
                                         "byMonth": {MONTH: 99.0}}}
        with _bank_env(state, ledger):
            self.assertIsNone(bank._director_spend_status("worker-1"))
        # Unknown agent: never a false block.
        state["directorBudgets"] = {"dir-1": 1.0}
        with _bank_env(state, ledger):
            self.assertIsNone(bank._director_spend_status("ghost"))
            self.assertIsNone(bank._director_spend_status(None))


# ---------------------------------------------------------------------------
# The shared chokepoint gate.
# ---------------------------------------------------------------------------


class DirectorGate(unittest.TestCase):
    def test_gate_returns_429_when_exhausted(self):
        state = _state()
        state["directorBudgets"] = {"dir-1": 1.0}
        ledger = {"__director__/dir-1": {"used": 2.0, "calls": 2,
                                         "byMonth": {MONTH: 2.0}}}
        actions = []
        with _bank_env(state, ledger), \
                unittest.mock.patch.object(serve, "log_action",
                                           lambda *a, **k: actions.append(a)):
            resp = serve._director_budget_gate("worker-1", "m", "svc", True, "chat")
        self.assertEqual(resp.status_code, 429)
        data = json.loads(resp.body)
        self.assertTrue(data["directorBudgetExhausted"])
        self.assertEqual(data["directorId"], "dir-1")
        self.assertEqual(data["directorBudgetUsd"], 1.0)
        self.assertEqual(actions[0][1], "chat_director_budget_blocked")

    def test_gate_none_when_uncapped(self):
        state = _state()
        with _bank_env(state, {}):
            self.assertIsNone(
                serve._director_budget_gate("worker-1", "m", "svc", True, "chat"))
        self.assertIsNone(
            serve._director_budget_gate(None, "m", "svc", True, "chat"))


# ---------------------------------------------------------------------------
# API: view, admin set, director request.
# ---------------------------------------------------------------------------


class DirectorBudgetEndpoints(unittest.TestCase):
    def test_get_budget_shows_director_slices(self):
        state = _state()
        state["directorBudgets"] = {"dir-1": 25.0}
        ledger = {"__director__/dir-1": {"used": 7.5, "calls": 1,
                                         "byMonth": {MONTH: 7.5}}}
        with _api_env(state, player=True), \
                unittest.mock.patch.multiple(serve,
                                             _spend_ledger_read=lambda: ledger):
            resp = _client().get("/api/budget")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["directorBudgets"]["dir-1"]["capUsd"], 25.0)
        self.assertEqual(data["directorBudgets"]["dir-1"]["usedUsd"], 7.5)
        self.assertNotIn("dir-2", data["directorBudgets"])
        self.assertEqual(data["budgetRequests"], [])

    def test_set_director_budget_player(self):
        state = _state()
        with _api_env(state, player=True):
            resp = _client().post("/api/budget/director",
                                  json={"directorId": "dir-1", "budgetUsd": 25})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["capUsd"], 25.0)
        self.assertEqual(state["directorBudgets"]["dir-1"], 25.0)

    def test_set_director_budget_admin_agent_allowed(self):
        state = _state()
        with _api_env(state, agent="admin-1"):
            resp = _client().post("/api/budget/director",
                                  json={"directorId": "dir-2", "budgetUsd": 10})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(state["directorBudgets"]["dir-2"], 10.0)

    def test_set_director_budget_worker_forbidden(self):
        state = _state()
        with _api_env(state, agent="worker-1"):
            resp = _client().post("/api/budget/director",
                                  json={"directorId": "dir-1", "budgetUsd": 25})
        self.assertEqual(resp.status_code, 401)

    def test_set_director_budget_unauthenticated(self):
        state = _state()
        with _api_env(state):
            resp = _client().post("/api/budget/director",
                                  json={"directorId": "dir-1", "budgetUsd": 25})
        self.assertEqual(resp.status_code, 401)

    def test_set_director_budget_unknown_director(self):
        state = _state()
        with _api_env(state, player=True):
            resp = _client().post("/api/budget/director",
                                  json={"directorId": "ghost", "budgetUsd": 25})
        self.assertEqual(resp.status_code, 400)

    def test_set_director_budget_negative(self):
        state = _state()
        with _api_env(state, player=True):
            resp = _client().post("/api/budget/director",
                                  json={"directorId": "dir-1", "budgetUsd": -5})
        self.assertEqual(resp.status_code, 400)

    def test_clear_director_budget(self):
        state = _state()
        state["directorBudgets"] = {"dir-1": 25.0}
        with _api_env(state, player=True):
            resp = _client().post("/api/budget/director",
                                  json={"directorId": "dir-1", "budgetUsd": None})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("dir-1", state["directorBudgets"])

    def test_set_grants_pending_request(self):
        state = _state()
        state["directorBudgets"] = {"dir-1": 10.0}
        state["budgetRequests"] = [
            {"id": "br-1", "directorId": "dir-1", "requestedUsd": 20.0,
             "status": "pending"},
            {"id": "br-2", "directorId": "dir-1", "requestedUsd": 30.0,
             "status": "pending"},
        ]
        with _api_env(state, player=True):
            resp = _client().post("/api/budget/director",
                                  json={"directorId": "dir-1", "budgetUsd": 25})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["requestsGranted"], 1)
        self.assertEqual(state["budgetRequests"][0]["status"], "granted")
        self.assertEqual(state["budgetRequests"][1]["status"], "pending")

    def test_request_raise_director_agent(self):
        state = _state()
        mails = []
        with _api_env(state, agent="dir-1"), \
                unittest.mock.patch.multiple(
                    sim, _deliver_mail=lambda s, a, k, p: mails.append((a, k))):
            resp = _client().post("/api/budget/request",
                                  json={"directorId": "dir-1", "requestedUsd": 50,
                                        "reason": "sprint backlog"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(state["budgetRequests"]), 1)
        req = state["budgetRequests"][0]
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["requestedUsd"], 50.0)
        self.assertEqual(req["requestedBy"], "dir-1")
        self.assertEqual(req["reason"], "sprint backlog")
        # The admin is mailed so the request is never silent.
        self.assertEqual(mails, [("admin-1", "budget_request")])
        self.assertTrue(resp.json()["mailedToAdmin"])

    def test_request_raise_worker_forbidden(self):
        state = _state()
        with _api_env(state, agent="worker-1"):
            resp = _client().post("/api/budget/request",
                                  json={"directorId": "dir-1", "requestedUsd": 50})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(state["budgetRequests"], [])

    def test_request_raise_other_director_forbidden(self):
        state = _state()
        with _api_env(state, agent="dir-1"):
            resp = _client().post("/api/budget/request",
                                  json={"directorId": "dir-2", "requestedUsd": 50})
        self.assertEqual(resp.status_code, 403)

    def test_request_raise_player_any_director(self):
        state = _state()
        with _api_env(state, player=True):
            resp = _client().post("/api/budget/request",
                                  json={"directorId": "dir-2", "requestedUsd": 50})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(state["budgetRequests"][0]["directorId"], "dir-2")

    def test_request_raise_admin_agent_any_director(self):
        state = _state()
        with _api_env(state, agent="admin-1"):
            resp = _client().post("/api/budget/request",
                                  json={"directorId": "dir-2", "requestedUsd": 50})
        self.assertEqual(resp.status_code, 200)

    def test_request_raise_unauthenticated(self):
        state = _state()
        with _api_env(state):
            resp = _client().post("/api/budget/request",
                                  json={"directorId": "dir-1", "requestedUsd": 50})
        self.assertEqual(resp.status_code, 401)

    def test_request_raise_bad_values(self):
        state = _state()
        with _api_env(state, agent="dir-1"):
            resp = _client().post("/api/budget/request",
                                  json={"directorId": "dir-1", "requestedUsd": -5})
            self.assertEqual(resp.status_code, 400)
            resp = _client().post("/api/budget/request",
                                  json={"directorId": "ghost", "requestedUsd": 50})
            self.assertEqual(resp.status_code, 400)
            resp = _client().post("/api/budget/request",
                                  json={"requestedUsd": 50})
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
