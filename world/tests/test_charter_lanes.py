"""Tests for the tank-level spine (the player's charter) and attention lanes
(2026-10-06).

Two whole-tank gaps closed against the "lanes, not sequences" + "find the
spine" essay:
  1. The SPINE: a player-owned charter (goal + interests). `_roadmap_step`
     weighs charter alignment so fresh work lands where the goal points, the
     consensus relay names the goal, refinement grooms toward it, and every
     task's instructions open with it.
  2. LANES: queue items may ride build/reading/open/parking-lot. parking-lot
     cards wait with a bookmark (never auto-assigned, never dropped); build >
     open > reading breaks priority ties; the reading lane is rate-limited to
     one active card per room.
Hermetic: DB redirected to a throwaway temp dir (the pattern every other sim
test uses) so log_action writes are harmless.
"""
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

_MODULE_TMP_DIR = tempfile.mkdtemp(prefix="think-tank-charter-lanes-")


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


class CharterCore(unittest.TestCase):
    def test_set_charter_requires_a_goal(self):
        state = {}
        self.assertIsNone(sim.set_charter(state, '   '))
        self.assertIsNone(sim.set_charter(state, ''))
        self.assertNotIn('charter', state)

    def test_set_and_get_charter_roundtrip(self):
        state = {}
        ch = sim.set_charter(state, 'ship a research-backed product',
                             interests=['research', 'writing', 'media'],
                             notes='the product matters more than the rooms',
                             now_ms=1234)
        self.assertEqual(ch['goal'], 'ship a research-backed product')
        self.assertEqual(ch['interests'], ['research', 'writing', 'media'])
        self.assertEqual(ch['updatedAt'], 1234)
        got = sim.get_charter(state)
        self.assertEqual(got['goal'], ch['goal'])

    def test_get_charter_none_without_one(self):
        self.assertIsNone(sim.get_charter({}))
        self.assertIsNone(sim.get_charter({'charter': {'interests': []}}))

    def test_charter_alignment_matches_room_keywords(self):
        state = {}
        # No charter -> no alignment signal (the tank never fabricates one).
        self.assertIsNone(sim._charter_alignment(state, 'observatory'))
        sim.set_charter(state, 'advance the research program', now_ms=0)
        self.assertTrue(sim._charter_alignment(state, 'observatory'))
        self.assertFalse(sim._charter_alignment(state, 'media'))
        # An interest alone can align a room.
        sim.set_charter(state, 'win the design contest', interests=['media'], now_ms=0)
        self.assertTrue(sim._charter_alignment(state, 'media'))

    def test_roadmap_step_defaults_fresh_aligned_room_to_the_spine(self):
        state = {'lastRoadmapReviewAt': 0}
        sim.set_charter(state, 'research everything', now_ms=0)
        sim._roadmap_step(state, 10 ** 12)
        roadmap = state['roadmap']
        # observatory is charter-aligned -> baseline 1; an off-spine fresh room
        # (media) stays 0. The spine decides a new tank's direction.
        self.assertEqual(roadmap['observatory']['priority'], 1)
        self.assertTrue(roadmap['observatory']['charterAligned'])
        self.assertEqual(roadmap['media']['priority'], 0)
        self.assertFalse(roadmap['media']['charterAligned'])

    def test_roadmap_step_boosts_aligned_room_even_when_weak(self):
        state = {'lastRoadmapReviewAt': 0,
                 'completedDeliverables': [
                     {'room': 'observatory', 'grade': 5.0, 'gradeIsReal': True},
                     {'room': 'media', 'grade': 5.0, 'gradeIsReal': True},
                 ]}
        sim.set_charter(state, 'research everything', now_ms=0)
        sim._roadmap_step(state, 10 ** 12)
        # Both are weak-grade + starved (+2, +1); observatory gets the charter
        # bonus (+1) on top because it is on the spine.
        self.assertGreater(state['roadmap']['observatory']['priority'],
                           state['roadmap']['media']['priority'])

    def test_consensus_relay_carries_the_charter_goal(self):
        state = {'lastRoadmapReviewAt': 0}
        sim.set_charter(state, 'research everything', now_ms=0)
        sim._roadmap_step(state, 10 ** 12)
        sim._consensus_relay_step(state, 10 ** 12)
        note = sim._consensus_relay_note(state)
        self.assertIsNotNone(note)
        self.assertIn('research everything', note)
        self.assertIn('observatory', note)

    def test_refinement_context_notes_charter_alignment(self):
        state = {'roadmap': {}}
        sim.set_charter(state, 'research everything', now_ms=0)
        self.assertIn('charter aligned', sim._refinement_context_for_room(state, 'observatory'))
        self.assertIn('charter: off-spine', sim._refinement_context_for_room(state, 'media'))
        state = {'roadmap': {}}
        self.assertIsNone(sim._refinement_context_for_room(state, 'observatory'))

    def test_task_instructions_open_with_the_charter(self):
        state = {}
        sim.set_charter(state, 'research everything', now_ms=0)
        out = sim._augment_task_instructions(state, 'ada', None, 'do the thing')
        self.assertTrue(out.startswith('Tank charter: research everything'))
        self.assertIn('do the thing', out)
        # No charter -> instructions untouched.
        self.assertEqual(sim._augment_task_instructions({}, 'ada', None, 'do it'), 'do it')


class LaneCore(unittest.TestCase):
    def test_normalize_lane_accepts_valid_only(self):
        for lane in ('build', 'reading', 'open', 'parking-lot'):
            self.assertEqual(sim.normalize_lane(lane), lane)
        self.assertIsNone(sim.normalize_lane('side-quest'))
        self.assertIsNone(sim.normalize_lane(''))
        self.assertIsNone(sim.normalize_lane(None))

    def test_queue_work_whitelists_lane(self):
        state = {'workQueue': []}
        sim.queue_work(state, [{'title': 't', 'room': 'observatory', 'lane': 'build'}])
        self.assertEqual(state['workQueue'][0]['lane'], 'build')
        sim.queue_work(state, [{'title': 't2', 'room': 'observatory', 'lane': 'bogus'}])
        self.assertIsNone(state['workQueue'][1]['lane'])

    def test_queue_spike_and_queue_once_accept_lane(self):
        state = {'workQueue': []}
        sim.queue_spike(state, 'S', 'observatory', 60_000, lane='open')
        sim.queue_once(state, 'O', 10 ** 12, room='observatory', lane='parking-lot')
        self.assertEqual(state['workQueue'][0]['lane'], 'open')
        self.assertEqual(state['workQueue'][1]['lane'], 'parking-lot')

    def test_parked_cards_are_never_auto_picked_and_do_not_count_as_work(self):
        state = {'workQueue': []}
        sim.queue_once(state, 'parked forever', 10 ** 12, room='observatory',
                       lane='parking-lot')
        now = int(time.time() * 1000)
        self.assertEqual(sim.pick_next_due_index(state['workQueue'], now, set(), state), -1)
        self.assertFalse(sim.think_tank_has_work(state, now),
                         'a parking-lot card must not keep the tank busy')

    def test_parked_card_survives_without_being_dropped(self):
        # The bookmark: the card stays in the queue (nothing deletes it), it is
        # just never selected. An unlaned sibling IS selected.
        state = {'workQueue': []}
        sim.queue_once(state, 'parked', 10 ** 12, room='observatory', lane='parking-lot')
        sim.queue_work(state, [{'title': 'active', 'room': 'observatory'}])
        now = int(time.time() * 1000)
        self.assertEqual(sim.pick_next_due_index(state['workQueue'], now, set(), state), 1)
        self.assertEqual(len(state['workQueue']), 2)

    def test_lane_weight_orders_build_over_open_over_reading(self):
        state = {'workQueue': []}
        sim.queue_work(state, [
            {'title': 'read slowly', 'room': 'observatory', 'lane': 'reading'},
            {'title': 'side quest', 'room': 'observatory', 'lane': 'open'},
            {'title': 'main build', 'room': 'observatory', 'lane': 'build'},
            {'title': 'no lane', 'room': 'observatory'},
        ])
        q = state['workQueue']
        now = int(time.time() * 1000)
        order = []
        excluded = set()
        for _ in range(len(q)):
            idx = sim.pick_next_due_index(q, now, excluded, state)
            self.assertNotEqual(idx, -1)
            excluded.add(id(q[idx]))
            order.append(q[idx]['title'])
        self.assertEqual(order, ['main build', 'side quest', 'read slowly', 'no lane'])

    def test_priority_still_beats_lane(self):
        # Lanes break ties, they never override urgency: urgent beats build.
        state = {'workQueue': []}
        sim.queue_work(state, [
            {'title': 'urgent but unlaned', 'room': 'observatory', 'priority': 'urgent'},
            {'title': 'normal build', 'room': 'observatory', 'lane': 'build'},
        ])
        now = int(time.time() * 1000)
        idx = sim.pick_next_due_index(state['workQueue'], now, set(), state)
        self.assertEqual(state['workQueue'][idx]['title'], 'urgent but unlaned')

    def test_reading_lane_is_rate_limited_to_one_active_per_room(self):
        state = {'workQueue': [], 'tasks': {
            'task-1': {'id': 'task-1', 'lane': 'reading', 'room': 'observatory',
                       'status': 'working', 'assignedTo': 'ada'},
        }}
        sim.queue_work(state, [
            {'title': 'second reading card', 'room': 'observatory', 'lane': 'reading'},
            {'title': 'reading elsewhere', 'room': 'media', 'lane': 'reading'},
        ])
        now = int(time.time() * 1000)
        # The observatory reading card waits its turn; the media one is a fresh
        # room (no active reading there) so it is picked.
        idx = sim.pick_next_due_index(state['workQueue'], now, set(), state)
        self.assertEqual(state['workQueue'][idx]['title'], 'reading elsewhere')
        # Once the active one finishes, the room's reading card becomes eligible.
        state['tasks']['task-1']['status'] = 'done'
        idx = sim.pick_next_due_index(state['workQueue'], now, set(), state)
        self.assertEqual(state['workQueue'][idx]['title'], 'second reading card')

    def test_lane_survives_queue_assign_onto_the_task(self):
        state = {
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'director': 't1'},
                {'id': 'faye', 'name': 'Faye', 'isAdmin': True},
            ],
            'agents': {
                'ada': {'id': 'ada', 'offDuty': False, 'x': 680, 'y': 340},
                'faye': {'id': 'faye', 'offDuty': False, 'x': 100, 'y': 100,
                         'isAdmin': True},
            },
            'sim': {'rr': {'task': 0}},
            'workQueue': [],
            'tasks': {},
        }
        sim.queue_work(state, [{'title': 'build the thing', 'room': 'observatory',
                                'lane': 'build'}])
        grid, doors = sim._load_outdoor_geometry()
        pick = state['workQueue'].pop(0)
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['lane'], 'build')

    def test_player_task_carries_lane_onto_task_and_inbox(self):
        state = {'workQueue': [], 'tasks': {}, 'issues': {},
                 'agentRoster': [{'id': 'faye', 'name': 'Faye', 'isAdmin': True}],
                 'agents': {'faye': {'id': 'faye', 'offDuty': False}}}
        sim.queue_spike(state, 'Player reading card', 'observatory', 60_000,
                        assigned_to='player', lane='reading')
        pick = state['workQueue'].pop(0)
        task = sim._assign_due_item(state, pick, False, {}, {},
                                    int(time.time() * 1000), [0])
        self.assertEqual(task['lane'], 'reading')
        card = next(m for m in state['playerInbox'] if m.get('id') == task['playerInboxId'])
        self.assertEqual(card['lane'], 'reading')


class FileIssueLane(unittest.TestCase):
    def test_file_issue_threads_lane_through_refinement(self):
        state = {'issues': {}, 'backlogRequests': [], 'workQueue': [],
                 'agentRoster': [{'id': 'faye', 'name': 'Faye', 'isAdmin': True}],
                 'agents': {'faye': {'id': 'faye', 'offDuty': False}},
                 'teams': [{'id': 'ada', 'directorId': 'ada', 'scrumMasterId': 'ada',
                            'prefix': 'DEV', 'name': 'Dev Team'}]}
        issue = sim.file_issue(state, 'ada', 'story', 'Write the copy',
                               'marketing', 'ben', lane='build')
        self.assertEqual(issue['lane'], 'build')
        self.assertEqual(state['backlogRequests'][0]['lane'], 'build')
        req = state['backlogRequests'][0]
        pending = {'scrumMasterId': 'ada', 'reqIds': [req['id']], 'teamId': 'ada',
                   'people': {}}

        def decider(instructions, criteria):
            return 'accept'

        sim._resolve_refinement(state, pending, int(time.time() * 1000), decider=decider)
        self.assertEqual(state['workQueue'][-1]['lane'], 'build')


class CharterEndpoints(unittest.TestCase):
    def _state(self):
        return {
            "sim": {"owner": "server"},
            "agentRoster": [{"id": "agent-1", "name": "Theo", "role": "admin",
                             "isAdmin": True}],
            "agents": {"agent-1": {"name": "Theo", "busy": False, "offDuty": False,
                                   "role": "admin",
                                   "profile": {"mission": "run the think tank"}}},
            "teams": [], "workQueue": [],
        }

    def test_get_requires_player_session(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=False):
            r = _client().get("/api/intent/charter")
        self.assertEqual(r.status_code, 401)

    def test_post_requires_player_session(self):
        with unittest.mock.patch.object(serve, "verify_session", return_value=False):
            r = _client().post("/api/intent/charter", json={"goal": "x"})
        self.assertEqual(r.status_code, 401)

    def test_post_requires_a_goal(self):
        state = self._state()
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state):
            r = _client().post("/api/intent/charter", json={"interests": ["research"]})
        self.assertEqual(r.status_code, 400)

    def test_post_and_get_charter_roundtrip(self):
        state = self._state()
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            r = _client().post("/api/intent/charter",
                               json={"goal": "research everything",
                                     "interests": ["research", "writing"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(state["charter"]["goal"], "research everything")
        self.assertEqual(state["charter"]["interests"], ["research", "writing"])
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state):
            r = _client().get("/api/intent/charter")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["charter"]["goal"], "research everything")

    def test_schedule_once_accepts_lane(self):
        state = self._state()
        future = int(time.time() * 1000) + 3600_000
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "save_state_to_db"), \
             unittest.mock.patch.object(serve, "log_action"), \
             unittest.mock.patch.object(serve, "_append_passport_decision"):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "read a paper", "at": future,
                                     "lane": "reading"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(state["workQueue"][0]["lane"], "reading")

    def test_schedule_once_rejects_bad_lane(self):
        state = self._state()
        future = int(time.time() * 1000) + 3600_000
        with unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "get_state_from_db", return_value=state):
            r = _client().post("/api/intent/schedule-once",
                               json={"title": "read a paper", "at": future,
                                     "lane": "side-quest"})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()