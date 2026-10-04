"""Tests for the composed "scheduled AND dependent on another story" feature:
a one-off scheduled task, a recurring research topic, or an ordered pipeline can
optionally hold until a named task reaches 'done' in the durable mirror.

Hermetic: DB redirected to a throwaway temp dir so no real think_tank.db is
touched. The gates are exercised at sim level (pick_next_due_index /
think_tank_has_work / _check_schedules / _check_pipelines) and the wiring at
serve level (routing lanes + structured endpoints pass dependsOnTask through).
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

_MODULE_TMP_DIR = tempfile.mkdtemp(prefix="think-tank-schedule-dependency-")


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
        "tasks": {},
        "researchTopics": [],
        "pipelines": [],
    }


class QueueOnceDependency(unittest.TestCase):
    def test_queue_once_stores_depends_on(self):
        state = {"workQueue": []}
        at_ms = int(time.time() * 1000) + 3600_000
        item = sim.queue_once(state, "Deploy after landing", at_ms, depends_on_task="task-9")
        self.assertIsNotNone(item)
        self.assertEqual(item["dependsOn"], "task-9")
        self.assertEqual(state["workQueue"][0]["dependsOn"], "task-9")

    def test_queue_once_defaults_no_dependency(self):
        state = {"workQueue": []}
        item = sim.queue_once(state, "plain", int(time.time() * 1000) + 60_000)
        self.assertIsNone(item["dependsOn"])


class AddResearchTopicDependency(unittest.TestCase):
    def test_add_research_topic_stores_depends_on_task(self):
        state = {"researchTopics": []}
        record = sim.add_research_topic(state, "watch the market", "https://example.com",
                                        3600_000, depends_on_task="task-9")
        self.assertIsNotNone(record)
        self.assertEqual(record["dependsOnTask"], "task-9")
        self.assertEqual(state["researchTopics"][0]["dependsOnTask"], "task-9")

    def test_add_research_topic_defaults_no_dependency(self):
        state = {"researchTopics": []}
        record = sim.add_research_topic(state, "watch the market", "https://example.com",
                                        3600_000)
        self.assertIsNone(record["dependsOnTask"])


class AddPipelineDependency(unittest.TestCase):
    def test_add_pipeline_stores_depends_on_task(self):
        state = {"pipelines": []}
        p = sim.add_pipeline(state, "pl", 7200_000,
                             [{"title": "a", "room": "observatory"}],
                             depends_on_task="task-9")
        self.assertIsNotNone(p)
        self.assertEqual(p["dependsOnTask"], "task-9")
        self.assertEqual(state["pipelines"][0]["dependsOnTask"], "task-9")

    def test_add_pipeline_defaults_no_dependency(self):
        state = {"pipelines": []}
        p = sim.add_pipeline(state, "pl", 7200_000, [{"title": "a", "room": "observatory"}])
        self.assertIsNone(p["dependsOnTask"])


class DependencyHelpers(unittest.TestCase):
    def test_dependency_landed_requires_done_dict(self):
        state = {"tasks": {"task-9": {"id": "task-9", "status": "done"}}}
        self.assertTrue(sim._dependency_landed(state, "task-9"))
        self.assertFalse(sim._dependency_landed(state, "task-missing"))
        state["tasks"]["task-9"]["status"] = "working"
        self.assertFalse(sim._dependency_landed(state, "task-9"))
        state["tasks"]["task-9"] = "done"  # stale/corrupt mirror value
        self.assertFalse(sim._dependency_landed(state, "task-9"))

    def test_work_item_dependency_met(self):
        state = {}
        self.assertTrue(sim._work_item_dependency_met(state, {"dependsOn": None}))
        self.assertTrue(sim._work_item_dependency_met(state, {}))
        self.assertFalse(sim._work_item_dependency_met(state, {"dependsOn": "task-9"}))
        state["tasks"] = {"task-9": {"status": "done"}}
        self.assertTrue(sim._work_item_dependency_met(state, {"dependsOn": "task-9"}))


class PickNextDueIndexDependency(unittest.TestCase):
    def test_pick_skips_unlanded_dependency_when_state_given(self):
        now = 10 ** 12
        q = [{"title": "gated", "priority": sim.WORK_PRIORITY["normal"],
              "dependsOn": "task-9"}]
        # No state -> legacy behavior, dependency not consulted.
        self.assertEqual(sim.pick_next_due_index(q, now, set()), 0)
        # With state -> a not-yet-landed dependency makes the card unpickable.
        self.assertEqual(sim.pick_next_due_index(q, now, set(), state={"tasks": {}}), -1)
        # Landed -> picked normally.
        self.assertEqual(sim.pick_next_due_index(q, now, set(),
                                                 state={"tasks": {"task-9": {"status": "done"}}}), 0)

    def test_pick_skips_only_gated_items(self):
        now = 10 ** 12
        q = [
            {"title": "gated", "priority": sim.WORK_PRIORITY["normal"], "dependsOn": "task-9"},
            {"title": "free", "priority": sim.WORK_PRIORITY["normal"]},
        ]
        idx = sim.pick_next_due_index(q, now, set(), state={"tasks": {}})
        self.assertEqual(q[idx]["title"], "free")


class ThinkTankHasWorkDependency(unittest.TestCase):
    def test_think_tank_has_work_gates_depends_on(self):
        now = 10 ** 12
        state = {"workQueue": [{"title": "gated", "dependsOn": "task-9"}], "agents": {}}
        self.assertFalse(sim.think_tank_has_work(state, now))
        state["tasks"] = {"task-9": {"status": "done"}}
        self.assertTrue(sim.think_tank_has_work(state, now))

    def test_think_tank_has_work_ungated_item_always_work(self):
        now = 10 ** 12
        state = {"workQueue": [{"title": "free"}], "agents": {}}
        self.assertTrue(sim.think_tank_has_work(state, now))


class CheckSchedulesResearchGate(unittest.TestCase):
    def _sweep_mocks(self):
        return unittest.mock.patch.multiple(
            sim,
            _check_pipelines=unittest.mock.Mock(),
            _pending_player_ask_sweep=unittest.mock.Mock(),
            _supervisor_block_vote_sweep=unittest.mock.Mock(),
            _skill_review_has_pending=lambda: False,
            _distill_has_new_archives=lambda *a, **k: False,
        )

    def test_topic_holds_marker_until_dependency_lands(self):
        state = {"researchTopics": [
            {"id": "topic-1", "topic": "crypto", "startUrl": "https://example.com",
             "lastRunAt": 0, "cadenceMs": sim.MIN_RESEARCH_CADENCE_MS,
             "dependsOnTask": "task-9"},
        ]}
        with self._sweep_mocks():
            sim._check_schedules(state, 1000.0, 1_000_000)
        # Gated: no crawl queued AND the marker is NOT advanced -- the crawl
        # must fire on the first pass AFTER the dependency lands, not burn a
        # cadence cycle while waiting.
        self.assertEqual(state.get('workQueue'), None)
        self.assertEqual(state['researchTopics'][0]['lastRunAt'], 0)
        # Land the dependency: the very next pass queues the crawl and stamps.
        state['tasks'] = {'task-9': {'id': 'task-9', 'status': 'done'}}
        with self._sweep_mocks():
            sim._check_schedules(state, 1000.0, 1_000_000)
        self.assertTrue(any('Scheduled research' in w['title']
                            for w in state['workQueue']))
        self.assertEqual(state['researchTopics'][0]['lastRunAt'], 1_000_000)

    def test_topic_without_dependency_fires_immediately(self):
        state = {"researchTopics": [
            {"id": "topic-1", "topic": "crypto", "startUrl": "https://example.com",
             "lastRunAt": 0, "cadenceMs": sim.MIN_RESEARCH_CADENCE_MS},
        ]}
        with self._sweep_mocks():
            sim._check_schedules(state, 1000.0, 1_000_000)
        self.assertTrue(any('Scheduled research' in w['title']
                            for w in state['workQueue']))


class CheckPipelinesDependencyGate(unittest.TestCase):
    def test_never_started_pipeline_holds_at_run_boundary(self):
        state = {"pipelines": [{
            'id': 'pl-1', 'name': 'pl', 'cadenceMs': 7200_000,
            'lastRunAt': 0, 'runId': 0, 'runStepIndex': 0,
            'dependsOnTask': 'task-9',
            'steps': [{'title': 'first', 'room': 'observatory'}],
        }]}
        sim._check_pipelines(state, 1_000_000)
        self.assertEqual(state.get('workQueue'), None)
        self.assertEqual(state['pipelines'][0]['lastRunAt'], 0)
        # Land the dependency -> the run starts on the next pass.
        state['tasks'] = {'task-9': {'id': 'task-9', 'status': 'done'}}
        sim._check_pipelines(state, 1_000_000)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['pipelines'][0]['lastRunAt'], 1_000_000)

    def test_midrun_pipeline_ignores_unlanded_dependency(self):
        # The dependency gates the RUN'S START, not its tail: once step 0 has
        # fired, the remaining steps keep firing even if the dependency is gone.
        state = {"pipelines": [{
            'id': 'pl-1', 'name': 'pl', 'cadenceMs': 7200_000,
            'lastRunAt': 1_000_000, 'runId': 1, 'runStepIndex': 0,
            'dependsOnTask': 'task-9',
            'steps': [{'title': 'first', 'room': 'observatory'},
                      {'title': 'second', 'room': 'pressoffice'}],
        }],
            'tasks': {'task-0': {'id': 'task-0', 'status': 'done',
                                 'pipelineStep': {'pipelineId': 'pl-1', 'runId': 1,
                                                  'stepIndex': 0}}}}
        sim._check_pipelines(state, 1_100_000)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['pipelineStep']['stepIndex'], 1)

    def test_completed_run_rearm_holds_until_dependency_lands(self):
        # A completed run waiting out its cadence window ALSO holds on the
        # dependency when re-arming -- the gate applies at the run boundary.
        state = {"pipelines": [{
            'id': 'pl-1', 'name': 'pl', 'cadenceMs': 7200_000,
            'lastRunAt': 1_000_000, 'runId': 1, 'runStepIndex': 2,
            'dependsOnTask': 'task-9',
            'steps': [{'title': 'first', 'room': 'observatory'},
                      {'title': 'second', 'room': 'pressoffice'}],
        }]}
        sim._check_pipelines(state, 1_000_000 + 7200_000)
        self.assertEqual(state.get('workQueue'), None)
        self.assertEqual(state['pipelines'][0]['runId'], 1, 'no re-arm while gated')
        # Land it -> the fresh run fires (new run id).
        state['tasks'] = {'task-9': {'id': 'task-9', 'status': 'done'}}
        sim._check_pipelines(state, 1_000_000 + 7200_000)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['pipelineStep']['runId'], 2)


class RouteLaneScheduleOnceDependency(unittest.TestCase):
    def test_depends_on_task_passed_through_to_queue_once(self):
        state = _roster_state()
        future = int(time.time() * 1000) + 3600_000
        captured = {}
        def fake_queue_once(s, title, at_ms, **kwargs):
            captured.update(kwargs)
            return {'title': title, 'notBefore': at_ms, 'dependsOn': kwargs.get('depends_on_task')}
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_once_fields_sync",
                                        return_value={"title": "Deploy", "at": "x",
                                                     "atMs": future,
                                                     "dependsOnTask": "task-9"}), \
             unittest.mock.patch.object(sim, "queue_once", side_effect=fake_queue_once), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            result = asyncio.run(serve._route_lane_schedule_once(state, "text", "agent-1"))
        self.assertEqual(captured["depends_on_task"], "task-9")
        self.assertIn("task-9", result["reply"])


class RouteLaneScheduleDependency(unittest.TestCase):
    def test_depends_on_task_passed_through_to_add_research_topic(self):
        state = _roster_state()
        captured = {}
        def fake_add_topic(s, topic, url, cadence, **kwargs):
            captured.update(kwargs)
            return {'id': 'topic-1', 'topic': topic, 'startUrl': url, 'cadenceMs': cadence,
                    'dependsOnTask': kwargs.get('depends_on_task')}
        with unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"), \
             unittest.mock.patch.object(serve, "_extract_schedule_fields_sync",
                                        return_value={"topic": "t", "startUrl": "https://x",
                                                     "cadenceMs": 3600_000,
                                                     "dependsOnTask": "task-9"}), \
             unittest.mock.patch.object(sim, "add_research_topic", side_effect=fake_add_topic), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            result = asyncio.run(serve._route_lane_schedule(state, "text", "agent-1"))
        self.assertEqual(captured["depends_on_task"], "task-9")
        self.assertIn("task-9", result["reply"])


class IntentScheduleEndpointDependency(unittest.TestCase):
    def test_depends_on_task_stored_on_record(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            r = _client().post("/api/intent/schedule",
                               json={"topic": "watch", "startUrl": "https://example.com",
                                     "cadenceMs": 3600_000, "dependsOnTask": "task-9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["topic"]["dependsOnTask"], "task-9")

    def test_blank_depends_on_task_normalized_to_none(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            r = _client().post("/api/intent/schedule",
                               json={"topic": "watch", "startUrl": "https://example.com",
                                     "cadenceMs": 3600_000, "dependsOnTask": "  "})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["topic"]["dependsOnTask"])


class IntentScheduleOnceEndpointDependency(unittest.TestCase):
    def test_depends_on_task_passed_to_queue_once(self):
        state = _roster_state()
        future = int(time.time() * 1000) + 3600_000
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "Deploy", "at": future,
                                     "dependsOnTask": "task-9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(state["workQueue"][0]["dependsOn"], "task-9")


class PipelinesCreateEndpointDependency(unittest.TestCase):
    def test_depends_on_task_passed_to_add_pipeline(self):
        state = _roster_state()
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            r = _client().post("/api/pipelines",
                               json={"name": "pl", "cadenceMs": 7200_000,
                                     "steps": [{"title": "a", "room": "observatory"}],
                                     "dependsOnTask": "task-9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pipeline"]["dependsOnTask"], "task-9")


if __name__ == "__main__":
    unittest.main()
