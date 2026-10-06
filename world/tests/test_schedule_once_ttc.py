"""Tests for the one-off scheduled-task lane (queue_once / schedule_once) and
the test-time-compute (deliberation) path in /api/chat.

Hermetic: DB redirected to a throwaway temp dir so no real think_tank.db is
touched. The schedule_once extraction/lane are exercised directly with mocked
model calls (same pattern as ExtractScheduleFields/RouteLaneSchedule), the
endpoint through TestClient with a player session, and the TTC machinery with
patched OpenRouter calls so nothing is ever billed.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import serve  # noqa: E402
import sim  # noqa: E402
from sim_helpers import is_work_item_due  # noqa: E402

_MODULE_TMP_DIR = tempfile.mkdtemp(prefix="think-tank-schedule-once-ttc-")


def setUpModule():
    global _MODULE_PATCHER, _RATE_PATCHER
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, "think-tank.db"),
        THINK_TANK_DIR=os.path.join(_MODULE_TMP_DIR, "think-tank"),
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, "agents"),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, "library"),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, "passport.json"),
    )
    _MODULE_PATCHER.start()
    _RATE_PATCHER = unittest.mock.patch.object(serve, "check_rate_limit", return_value=True)
    _RATE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _RATE_PATCHER.stop()
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _client():
    return TestClient(serve.app)


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
    }


class QueueOnce(unittest.TestCase):
    def test_queues_item_with_notbefore_and_default_instructions(self):
        state = {"workQueue": []}
        at_ms = int(time.time() * 1000) + 3600_000
        item = sim.queue_once(state, "Deploy the release", at_ms)
        self.assertIsNotNone(item)
        self.assertEqual(item["notBefore"], at_ms)
        self.assertEqual(item["title"], "Deploy the release")
        self.assertIn("Deploy the release", item["instructions"])
        self.assertEqual(item["room"], None)
        self.assertEqual(len(state["workQueue"]), 1)
        # It rides the same notBefore gate as every other queued item.
        self.assertFalse(is_work_item_due(item, at_ms - 1))
        self.assertTrue(is_work_item_due(item, at_ms))

    def test_queues_with_full_fields(self):
        state = {"workQueue": []}
        at_ms = int(time.time() * 1000) + 60_000
        item = sim.queue_once(state, "Run monthly report", at_ms, room="observatory",
                              instructions="Do the thing", task_type="code",
                              goal="deliver", priority="high",
                              project_label="release-1")
        self.assertEqual(item["room"], "observatory")
        self.assertEqual(item["instructions"], "Do the thing")
        self.assertEqual(item["taskType"], "code")
        self.assertEqual(item["goal"], "deliver")
        self.assertEqual(item["priority"], sim.WORK_PRIORITY["high"])
        self.assertEqual(item["projectLabel"], "release-1")

    def test_rejects_empty_title_or_non_positive_at(self):
        state = {"workQueue": []}
        self.assertIsNone(sim.queue_once(state, "", 1000))
        self.assertIsNone(sim.queue_once(state, "t", 0))
        self.assertIsNone(sim.queue_once(state, "t", -5))
        self.assertEqual(state["workQueue"], [])


class ParseScheduleOnceAt(unittest.TestCase):
    def test_iso_zulu(self):
        at_ms = serve._parse_schedule_once_at("2026-10-20T14:00:00Z")
        self.assertEqual(at_ms, 1792504800000)

    def test_iso_with_offset(self):
        at_ms = serve._parse_schedule_once_at("2026-10-20T10:00:00-04:00")
        self.assertEqual(at_ms, 1792504800000)

    def test_naive_iso_treated_as_utc(self):
        at_ms = serve._parse_schedule_once_at("2026-10-20T14:00:00")
        self.assertEqual(at_ms, 1792504800000)

    def test_epoch_ms_and_seconds(self):
        self.assertEqual(serve._parse_schedule_once_at(1792504800000), 1792504800000)
        self.assertEqual(serve._parse_schedule_once_at(1782064800), 1782064800000)
        self.assertEqual(serve._parse_schedule_once_at("1782064800"), 1782064800000)

    def test_invalid_and_non_positive(self):
        self.assertIsNone(serve._parse_schedule_once_at("not a time"))
        self.assertIsNone(serve._parse_schedule_once_at("2026-99-99T25:00:00"))
        self.assertIsNone(serve._parse_schedule_once_at(""))
        self.assertIsNone(serve._parse_schedule_once_at(0))
        self.assertIsNone(serve._parse_schedule_once_at(-100))
        self.assertIsNone(serve._parse_schedule_once_at(True))
        self.assertIsNone(serve._parse_schedule_once_at(None))


class ExtractScheduleOnceFields(unittest.TestCase):
    def test_http_json_raises(self):
        with unittest.mock.patch.object(serve, "_http_json", side_effect=RuntimeError):
            self.assertIsNone(serve._extract_schedule_once_fields_sync("a", "k", "text"))

    def test_non_dict(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value="junk"):
            self.assertIsNone(serve._extract_schedule_once_fields_sync("a", "k", "text"))

    def test_error_result(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value={"error": "x"}):
            self.assertIsNone(serve._extract_schedule_once_fields_sync("a", "k", "text"))

    def test_bad_json(self):
        with unittest.mock.patch.object(serve, "_http_json", return_value={"reply": "nope"}):
            self.assertIsNone(serve._extract_schedule_once_fields_sync("a", "k", "text"))

    def test_missing_at(self):
        with unittest.mock.patch.object(serve, "_http_json",
                                        return_value={"reply": json.dumps({"title": "x"})}):
            self.assertIsNone(serve._extract_schedule_once_fields_sync("a", "k", "text"))

    def test_unparseable_at(self):
        with unittest.mock.patch.object(serve, "_http_json",
                                        return_value={"reply": json.dumps(
                                            {"title": "x", "at": "someday"})}):
            self.assertIsNone(serve._extract_schedule_once_fields_sync("a", "k", "text"))

    def test_success_with_fences(self):
        reply = '```json\n{"title": "run the report", "at": "2026-10-20T14:00:00Z", "instructions": "send it"}\n```'
        with unittest.mock.patch.object(serve, "_resolve_model_tier", return_value="m"), \
             unittest.mock.patch.object(serve, "_http_json", return_value={"reply": reply}):
            out = serve._extract_schedule_once_fields_sync("a", "k", "text")
        self.assertEqual(out["title"], "run the report")
        self.assertEqual(out["at"], "2026-10-20T14:00:00Z")
        self.assertEqual(out["atMs"], 1792504800000)
        self.assertEqual(out["instructions"], "send it")


class RouteLaneScheduleOnce(unittest.TestCase):
    def test_missing_fields_asks_for_more(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_once_fields_sync", return_value=None):
            result = asyncio.run(serve._route_lane_schedule_once(state, "text", "agent-1"))
        self.assertIn("day+time", result["reply"])
        self.assertEqual(state["workQueue"], [])

    def test_past_time_rejected(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_once_fields_sync",
                                        return_value={"title": "t", "at": "2020-01-01T00:00:00Z",
                                                     "atMs": 1577836800000}):
            result = asyncio.run(serve._route_lane_schedule_once(state, "text", "agent-1"))
        self.assertIn("past", result["reply"])
        self.assertEqual(state["workQueue"], [])

    def test_beyond_horizon_rejected(self):
        state = _roster_state()
        far_at = int(time.time() * 1000) + serve.SCHEDULE_ONCE_MAX_AHEAD_MS + 1_000_000
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_once_fields_sync",
                                        return_value={"title": "t", "at": "x", "atMs": far_at}):
            result = asyncio.run(serve._route_lane_schedule_once(state, "text", "agent-1"))
        self.assertIn("year", result["reply"])
        self.assertEqual(state["workQueue"], [])

    def test_queue_once_failed(self):
        state = _roster_state()
        future = int(time.time() * 1000) + 3600_000
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_once_fields_sync",
                                        return_value={"title": "t", "at": "x", "atMs": future}), \
             unittest.mock.patch.object(sim, "queue_once", return_value=None):
            result = asyncio.run(serve._route_lane_schedule_once(state, "text", "agent-1"))
        self.assertIn("couldn't schedule", result["reply"])

    def test_success_queues_once_and_reports_time(self):
        state = _roster_state()
        future = int(time.time() * 1000) + 3600_000
        item = {"notBefore": future, "title": "Deploy release"}
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_once_fields_sync",
                                        return_value={"title": "Deploy release", "at": "x",
                                                     "atMs": future}), \
             unittest.mock.patch.object(sim, "queue_once", return_value=item), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            result = asyncio.run(serve._route_lane_schedule_once(state, "text", "agent-1"))
        self.assertIn("Deploy release", result["reply"])
        self.assertIn("UTC", result["reply"])


class IntentScheduleOnceEndpoint(unittest.TestCase):
    def test_requires_player_session(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=False):
            r = _client().post("/api/intent/schedule-once", json={"title": "t", "at": "2026-10-20T14:00:00Z"})
        self.assertEqual(r.status_code, 401)

    def test_agent_key_passes_middleware_but_handler_rejects(self):
        # /api/intent is behind the AUTH_PROTECTED_PREFIXES middleware, which
        # accepts a valid agent key -- but the handler's own gate is session-only
        # (an agent must never file work as the player). Exercise exactly that
        # split: middleware passes on the key, handler 401s.
        with unittest.mock.patch.object(serve, "verify_session", return_value=False), \
             unittest.mock.patch.object(serve, "_valid_agent_key_presented", return_value=True):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "t", "at": "2026-10-20T14:00:00Z"},
                               headers={"X-Agent-Key": "agent-key"})
        self.assertEqual(r.status_code, 401)

    def test_state_unavailable(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=None):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "t", "at": "2026-10-20T14:00:00Z"})
        self.assertEqual(r.status_code, 503)

    def test_malformed_body(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=_roster_state()):
            r = _client().post("/api/intent/schedule-once", content=b"not-json",
                               headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("malformed", r.json()["error"])

    def test_beyond_year_rejected(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=_roster_state()):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "t",
                                     "at": int(time.time()) + (serve.SCHEDULE_ONCE_MAX_AHEAD_MS // 1000) + 1000})
        self.assertEqual(r.status_code, 400)
        self.assertIn("year", r.json()["error"])

    def test_queue_once_failed(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=_roster_state()), \
             unittest.mock.patch.object(sim, "queue_once", return_value=None):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "t", "at": int(time.time()) + 3600})
        self.assertEqual(r.status_code, 400)
        self.assertIn("queue", r.json()["error"])

    def test_missing_title_or_at(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=_roster_state()):
            r = _client().post("/api/intent/schedule-once", json={"at": "2026-10-20T14:00:00Z"})
        self.assertEqual(r.status_code, 400)
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=_roster_state()):
            r = _client().post("/api/intent/schedule-once", json={"title": "t"})
        self.assertEqual(r.status_code, 400)

    def test_past_time_rejected(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=_roster_state()):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "t", "at": "2020-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("future", r.json()["error"])

    def test_success_queues_real_item(self):
        state = _roster_state()
        future = int(time.time() * 1000) + 3600_000
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "Deploy", "at": future,
                                     "room": "observatory", "instructions": "go"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["notBefore"], future)
        self.assertEqual(state["workQueue"][0]["notBefore"], future)
        self.assertEqual(state["workQueue"][0]["room"], "observatory")

    def test_epoch_seconds_at_accepted(self):
        state = _roster_state()
        future = int(time.time()) + 3600
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "Deploy", "at": future})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["notBefore"], future * 1000)


class TtcShouldDeliberate(unittest.TestCase):
    def _slug_patches(self, low="low", mid="mid", high="high", coding="coding", reasoning="reasoning"):
        return unittest.mock.patch.multiple(
            serve,
            _low_tier_slug=lambda: low,
            _mid_tier_slug=lambda: mid,
            _high_tier_slug=lambda: high,
            _coding_tier_slug=lambda: coding,
            _reasoning_tier_slug=lambda: reasoning,
        )

    def test_disabled_when_ttc_off(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", False), self._slug_patches():
            self.assertFalse(serve._ttc_should_deliberate("low"))
            self.assertFalse(serve._ttc_should_deliberate("low", best_of=5))

    def test_expensive_tiers_never_deliberate(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", True), self._slug_patches():
            self.assertFalse(serve._ttc_should_deliberate("high"))
            self.assertFalse(serve._ttc_should_deliberate("coding"))
            self.assertFalse(serve._ttc_should_deliberate("reasoning"))

    def test_low_and_mid_deliberate_by_default(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", True), self._slug_patches():
            self.assertTrue(serve._ttc_should_deliberate("low"))
            self.assertTrue(serve._ttc_should_deliberate("mid"))

    def test_opt_out_respected(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", True), self._slug_patches():
            self.assertFalse(serve._ttc_should_deliberate("low", deliberate=False))

    def test_explicit_best_of_forces_deliberation(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", True), self._slug_patches():
            self.assertTrue(serve._ttc_should_deliberate("mid", best_of=3))

    def test_none_or_unknown_model(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", True), self._slug_patches():
            self.assertFalse(serve._ttc_should_deliberate(None))
            self.assertFalse(serve._ttc_should_deliberate("some-unconfigured-model"))


class TtcBestOf(unittest.TestCase):
    def test_disabled_returns_one(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", False):
            self.assertEqual(serve._ttc_best_of(), 1)
            self.assertEqual(serve._ttc_best_of(10), 1)

    def test_clamps_to_range(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", True), \
             unittest.mock.patch.object(serve, "TTC_BEST_OF", 2), \
             unittest.mock.patch.object(serve, "TTC_MAX_BEST_OF", 3):
            self.assertEqual(serve._ttc_best_of(), 2)
            self.assertEqual(serve._ttc_best_of(1), 2)
            self.assertEqual(serve._ttc_best_of(2), 2)
            self.assertEqual(serve._ttc_best_of(3), 3)
            self.assertEqual(serve._ttc_best_of(99), 3)

    def test_invalid_best_of_falls_back_to_default(self):
        with unittest.mock.patch.object(serve, "TTC_ENABLED", True), \
             unittest.mock.patch.object(serve, "TTC_BEST_OF", 2):
            self.assertEqual(serve._ttc_best_of("banana"), 2)


class TtcMajorityJson(unittest.TestCase):
    @staticmethod
    def _sample(content, cost=0.0):
        return {"choices": [{"message": {"content": content}}], "usage": {"cost": cost}}

    def test_majority_wins_after_fence_strip(self):
        samples = [
            self._sample('```json\n{"ok": true}\n```'),
            self._sample('{"ok": true}'),
            self._sample('{"ok": false}'),
        ]
        self.assertEqual(serve._ttc_majority_json(samples), '{"ok": true}')

    def test_tie_broken_by_first_appearance(self):
        samples = [
            self._sample('{"a": 1}'),
            self._sample('{"b": 2}'),
            self._sample('{"a": 1}'),
            self._sample('{"b": 2}'),
        ]
        self.assertEqual(serve._ttc_majority_json(samples), '{"a": 1}')

    def test_no_repeat_winner_returns_none(self):
        samples = [self._sample('{"a": 1}'), self._sample('{"b": 2}')]
        self.assertIsNone(serve._ttc_majority_json(samples))

    def test_empty_and_unparseable_return_none(self):
        self.assertIsNone(serve._ttc_majority_json([self._sample(""), self._sample("   ")]))
        self.assertIsNone(serve._ttc_majority_json([]))


class TtcSelfVerify(unittest.TestCase):
    def _result(self, content):
        return {"choices": [{"message": {"content": content}}], "usage": {}}

    def _result_cost(self, content, cost):
        return {"choices": [{"message": {"content": content}}], "usage": {"cost": cost}}

    def test_reuses_given_drafts_and_judge_picks_verbatim(self):
        calls = []
        def fake(model, messages, max_tokens, best_of=1):
            calls.append(messages)
            return self._result("the chosen draft")
        drafts = ["draft one", "draft two"]
        with unittest.mock.patch.object(serve, "_call_openrouter_sync", side_effect=fake):
            out = serve._ttc_self_verify("m", [{"role": "user", "content": "q"}], 2, 100,
                                         drafts=drafts, service="s")
        self.assertEqual(out, "the chosen draft")
        self.assertEqual(len(calls), 1, "only the judge call -- drafts were supplied")

    def test_empty_judge_falls_back_to_first_draft(self):
        calls = []
        def fake(model, messages, max_tokens, best_of=1):
            calls.append(messages)
            return self._result("")
        drafts = ["draft one", "draft two"]
        with unittest.mock.patch.object(serve, "_call_openrouter_sync", side_effect=fake):
            out = serve._ttc_self_verify("m", [{"role": "user", "content": "q"}], 2, 100,
                                         drafts=drafts)
        self.assertEqual(out, "draft one")

    def test_single_draft_skips_judge(self):
        calls = []
        def fake(model, messages, max_tokens, best_of=1):
            calls.append(messages)
            return self._result("solo")
        with unittest.mock.patch.object(serve, "_call_openrouter_sync", side_effect=fake):
            out = serve._ttc_self_verify("m", [{"role": "user", "content": "q"}], 2, 100,
                                         drafts=["solo"])
        self.assertEqual(out, "solo")
        self.assertEqual(len(calls), 0, "a single draft needs no judge")

    def test_no_drafts_returns_empty(self):
        with unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        return_value=self._result("")):
            out = serve._ttc_self_verify("m", [{"role": "user", "content": "q"}], 2, 100,
                                         drafts=["", ""])
        self.assertEqual(out, "")

    def test_samples_and_accrues_when_drafts_none(self):
        accrued = []
        def fake(model, messages, max_tokens, best_of=1):
            return self._result_cost("d", 0.01)
        with unittest.mock.patch.object(serve, "_call_openrouter_sync", side_effect=fake), \
             unittest.mock.patch.object(serve, "_accrue_spend",
                                        side_effect=lambda s, c, village_id=None: accrued.append((s, c))):
            out = serve._ttc_self_verify("m", [{"role": "user", "content": "q"}], 2, 100,
                                         drafts=None, service="s")
        self.assertEqual(out, "d")
        # Two drafts sampled + one judge call, all accrued to the service.
        self.assertEqual(len(accrued), 3)


class CallOpenrouterSyncBestOf(unittest.TestCase):
    def test_best_of_one_returns_single_dict(self):
        with unittest.mock.patch.object(serve, "_call_openrouter_once_sync",
                                        return_value={"choices": []}):
            result = serve._call_openrouter_sync("m", [{"role": "user", "content": "hi"}], 100)
        self.assertEqual(result, {"choices": []})

    def test_best_of_greater_than_one_returns_list(self):
        with unittest.mock.patch.object(serve, "_call_openrouter_once_sync",
                                        side_effect=lambda *a, **k: {"choices": [k]}):
            result = serve._call_openrouter_sync("m", [{"role": "user", "content": "hi"}], 100, best_of=3)
        self.assertEqual(len(result), 3)


class ChatEndpointDeliberation(unittest.TestCase):
    def _post(self, body):
        with unittest.mock.patch.object(serve, "OPENROUTER_API_KEY", "k"), \
             unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "log_action"):
            return _client().post("/api/chat", json=body)

    @staticmethod
    def _sample(content, cost):
        return {"choices": [{"message": {"content": content}}], "usage": {"cost": cost}}

    def test_low_tier_uses_majority_vote_and_accrues_each_sample(self):
        samples = [self._sample('{"ok": true}', 0.01), self._sample('{"ok": true}', 0.02)]
        with unittest.mock.patch.object(serve, "_ttc_should_deliberate", return_value=True), \
             unittest.mock.patch.object(serve, "_ttc_best_of", return_value=2), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        return_value=samples), \
             unittest.mock.patch.object(serve, "_accrue_spend") as accrue:
            r = self._post({"model": "low-model",
                            "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reply"], '{"ok": true}')
        accrue.assert_called_once_with("__general__", 0.03, village_id='main')

    def test_prose_falls_back_to_self_verification(self):
        samples = [self._sample("draft one", 0.01), self._sample("draft two", 0.02)]
        with unittest.mock.patch.object(serve, "_ttc_should_deliberate", return_value=True), \
             unittest.mock.patch.object(serve, "_ttc_best_of", return_value=2), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        return_value=samples), \
             unittest.mock.patch.object(serve, "_ttc_self_verify",
                                        return_value="best draft") as verify, \
             unittest.mock.patch.object(serve, "_accrue_spend") as accrue:
            r = self._post({"model": "low-model",
                            "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reply"], "best draft")
        self.assertEqual(verify.call_args.kwargs["drafts"], ["draft one", "draft two"],
                         "the sampled drafts are reused for the judge pass")
        accrue.assert_called_once_with("__general__", 0.03, village_id='main')

    def test_majority_winner_not_json_falls_back_to_self_verify(self):
        # A structured request whose drafts all "agree" on something that isn't
        # valid JSON must not be returned as-is -- it falls through to the
        # self-verification judge pass like prose would.
        samples = [self._sample("plain text answer", 0.01), self._sample("plain text answer", 0.02)]
        with unittest.mock.patch.object(serve, "_ttc_should_deliberate", return_value=True), \
             unittest.mock.patch.object(serve, "_ttc_best_of", return_value=2), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        return_value=samples), \
             unittest.mock.patch.object(serve, "_ttc_self_verify",
                                        return_value="judged answer") as verify, \
             unittest.mock.patch.object(serve, "_accrue_spend"):
            r = self._post({"model": "low-model",
                            "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reply"], "judged answer")
        self.assertTrue(verify.called, "non-JSON majority winner must be judged, not echoed")

    def test_opt_out_keeps_single_call_behavior(self):
        with unittest.mock.patch.object(serve, "_ttc_should_deliberate", return_value=False), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync",
                                        return_value={"choices": [{"message": {"content": "ok"}}],
                                                      "usage": {"cost": 0.0}}), \
             unittest.mock.patch.object(serve, "_accrue_spend"):
            r = self._post({"model": "m", "deliberate": False,
                            "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reply"], "ok")


class RoutingLaneRegistry(unittest.TestCase):
    def test_schedule_once_lane_registered(self):
        lane_ids = [l["id"] for l in serve._ROUTING_LANES]
        self.assertIn("schedule_once", lane_ids)
        self.assertIn("schedule_once", serve._ROUTING_HANDLERS)


if __name__ == "__main__":
    unittest.main()