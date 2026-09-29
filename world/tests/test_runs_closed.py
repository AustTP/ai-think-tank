"""Phase 5 acceptance: the simulation runs with NO browser.

The think tank's whole tick used to live in index.html setInterval timers; closing
the tab froze it. After the migration the server IS the engine. This test is
the "proof of runs-closed": it boots the real loop seam (get_state_from_db ->
SimEngine.tick -> save_state_to_db, the exact body of sim._sim_loop_pass)
against a fresh temp DB, registers a deterministic content executor, and fast-
forwards ticks with an injected clock. It asserts a queued task is assigned,
walked to, worked (real content result), completed, and the worker cycles off
duty -- all read back from the authoritative SQLite DB, with zero browser.

Wall-clock would make this 400 ticks x SIM_TICK_S = 13+ min, so `now` is
injected (engine.tick already thread-an injectable clock; sim._sim_loop_pass is
a thin wrapper around the same three calls the test reuses here).
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim
import serve


class RunsClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-runs-closed-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        self._real_store = sim._store_content_result
        sim._content_executor = None

    def tearDown(self):
        sim._content_executor = None
        sim._store_content_result = self._real_store
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, work_queue):
        far_future = int(time.time() * 1000) + 60 * 60 * 24 * 365 * 10
        # Agent selection must be deterministic. An off-duty engineer wakes at a
        # free reachable spot chosen by pick_free_spot (appearFromOutskirts),
        # which can flip how far her walk to the door is -- and an idle-agent
        # wake adds non-determinism to the run-closed proof. To make it immune
        # to that cast, seed `ben` ON-DUTY and idle right at the observatory
        # door approach: the
        # engine's short walk into the door always has a path, so assignment is
        # deterministic. The entry point mirrors _assign_due_item's target.
        _, doors = sim._load_outdoor_geometry()
        obs = (doors or {}).get('observatory') or {'x': 1188, 'y': 646, 'w': 52, 'h': 50}
        door_x = obs['x'] + obs['w'] / 2
        door_y = obs['y'] + obs['h'] + 4
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': [
                # Faye must be marked isAdmin (the think tank shape) so the gate's
                # reviewer-selection excludes her; a bare role='admin' would let
                # her be pinned as a reviewer, and when her (busy) pin falls
                # through the author gets wrongly self-assigned the review.
                {'id': 'faye', 'name': 'Faye', 'role': 'admin', 'isAdmin': True},
                {'id': 'ada', 'name': 'Ada', 'role': 'engineer'},
                {'id': 'ben', 'name': 'Ben', 'role': 'engineer'},
            ],
            'agents': {
                'faye': {'id': 'faye', 'x': 0, 'y': 0, 'busy': True, 'task': None,
                         'inRoom': None, 'offDuty': True, 'dir': 'south', 'path': []},
                'ada': {'id': 'ada', 'x': 600, 'y': 340, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': True, 'dir': 'south', 'path': [],
                        'visible': True},
                'ben': {'id': 'ben', 'x': door_x, 'y': door_y, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                        'visible': True},
            },
            'reports': [],
            # A real topic must exist for the research work item's topicId to
            # resolve back to (the engine merges grown seenUrls into the topic
            # the work item references -- an empty list here would silently
            # no-op that write path, which is exactly what we're asserting).
            'researchTopics': [
                {'id': 't1', 'topic': 'weather data', 'seenUrls': [],
                 'lastRunAt': 0, 'cadenceMs': 99999999},
            ],
            'lastSkillReviewAt': far_future,  # don't inject the standing sweep
            'workQueue': work_queue,
        }
        serve.save_state_to_db(state)
        return state

    @staticmethod
    def _item(**over):
        item = {'title': 'Scheduled research: weather data', 'room': 'observatory',
                'instructions': 'crawl + synthesize a skill file',
                'pair': False, 'notBefore': None, 'priority': sim.WORK_PRIORITY['normal'],
                'goal': 'weather-data',
                'research': {'topicId': 't1', 'since': 0},
                'taskType': 'research', 'skillReview': False}
        item.update(over)
        return item

    def test_full_headless_lifecycle_persists_to_db(self):
        # A content executor stands in for the real (network-bound) ported
        # executors. It records the arriving room and lands a real result into
        # sim._store_content_result -- exactly what the dispatcher router does.
        #
        # The observatory is a DELIVERABLE room, so under the Phase E addendum
        # peer-approval gate its story does NOT complete to 'done' -- it moves to
        # 'needs_review' and the loop enqueues review subtasks (which a real
        # think tank resolves with two peer approvals). This test asserts the gated
        # lifecycle persists: primary work done -> needs_review + review subtasks
        # queued + author released off duty.
        #
        # A genuine authored deliverable (taskType='code', no `research` marker)
        # -- NOT a scheduled research task. That used to be this fixture's shape
        # until a real, confirmed production bug (2026-09-26): task['research']
        # is exempt from the peer gate now (see _peer_gated_lane), because a
        # scheduled crawl has no real "fix" a reviewer can send back -- its
        # "review" always found it actionable (no passing flake8/mypy/pytest-cov
        # suite to fail cleanly), spiraling one real "weather data" schedule
        # into 7,408 calls in a single evening before the fix. See
        # test_scheduled_research_completes_without_gating below for that path.
        calls = {}

        def fake_executor(snapshot, agent_id, task, base_ctx):
            calls['room'] = task.get('room')
            calls['id'] = task.get('id')
            self._real_store(task['id'], {'note': 'collected 1 page, wrote updated skill'})

        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        self._seed([self._item(title='Build the widget dashboard', research=None, taskType='code')])
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        gated = False
        for _ in range(400):
            now += sim.SIM_TICK_S
            # The exact headless loop body: read authoritative state, tick,
            # persist. No browser involved.
            state = serve.get_state_from_db()
            state = engine.tick(state, now=now)
            serve.save_state_to_db(state)
            tasks = state.get('tasks') or {}
            if tasks and any(t.get('status') == 'needs_review' for t in tasks.values()):
                gated = True
                break

        self.assertTrue(gated, 'deliverable task did not reach needs_review after 400 ticks')

        # Re-read from the DB -- the authoritative persistence layer -- proving
        # the gated outcome is durable, not just in-memory.
        final = serve.get_state_from_db()
        tasks = final.get('tasks') or {}
        story = next((t for t in tasks.values() if t.get('status') == 'needs_review'), None)
        self.assertIsNotNone(story, 'a story must persist in needs_review')
        self.assertTrue(story['_peerGate']['reviewerIds'],
                        'the gate must assign review subtasks')
        # The gate's review subtasks exist -- either still in the work queue OR
        # already lifted into live review tasks (the same _task_cycle pass can
        # enqueue AND start assigning a review). Either way they're wired back
        # to the story via reviewOf.
        q = final.get('workQueue') or []
        live = [t for t in tasks.values() if t.get('reviewOf') == story['id'] and t.get('id') != story['id']]
        review_links = [x for x in q if x.get('reviewOf') == story['id']] + live
        self.assertGreaterEqual(len(review_links), 1,
                                'a review subtask must be queued (or live) for the story')
        self.assertEqual(calls.get('room'), 'observatory',
                         'a deliverable task must dispatch real content work')
        # The author was released (approvedCount bumped) and is off duty, while
        # the story waits for review.
        author = story.get('assignedTo')
        author_state = final['agents'].get(author, {})
        self.assertGreater(author_state.get('approvedCount', 0), 0,
                           'a gated worker must still bump approvedCount on finishing primary work')
        self.assertIn(author_state.get('task'), (None, story['id']),
                      'the author must not be stranded on a freshly-assigned task')
        self.assertFalse(author_state.get('busy'),
                         'a gated worker must be released from busy while the story awaits review')

    def test_scheduled_research_completes_without_gating(self):
        # Companion to the gated-lifecycle test above: a scheduled research
        # task (task['research'] set, as _check_schedules queues it) must
        # complete straight to 'done' -- never needs_review -- while the
        # content result's seenUrl still flows back into the topic, exactly
        # as it always has. See _peer_gated_lane's docstring for the real
        # production incident this fixes.
        def fake_executor(snapshot, agent_id, task, base_ctx):
            self._real_store(task['id'], {'note': 'collected 1 page, wrote updated skill',
                                          'seenUrls': ['https://w.example']})

        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        self._seed([self._item()])
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        done = False
        for _ in range(400):
            now += sim.SIM_TICK_S
            state = serve.get_state_from_db()
            state = engine.tick(state, now=now)
            serve.save_state_to_db(state)
            tasks = state.get('tasks') or {}
            if tasks and any(t.get('status') == 'needs_review' for t in tasks.values()):
                self.fail('a scheduled research task must never reach needs_review')
            if tasks and any(t.get('status') == 'done' and t.get('research') for t in tasks.values()):
                done = True
                break

        self.assertTrue(done, 'scheduled research task did not reach done after 400 ticks')
        final = serve.get_state_from_db()
        topics = final.get('researchTopics') or []
        self.assertIn('https://w.example',
                      next((t.get('seenUrls') for t in topics if t.get('id') == 't1'), []),
                      'content result seenUrl must still merge back into the topic')


if __name__ == '__main__':
    unittest.main(verbosity=2)