"""Tests for Bot Ops / shadow mode (world/sim.py + the shadow endpoints in
world/serve.py).

A SHADOW (dry-run) work item does its work but changes nothing: on completion
its outcome lands in the append-only state['shadowLedger'] draft instead of
shipping -- no peer gate, no approvedCount/weekApprovals bump, no
completedDeliverables/completedRooms, no dependency unblock. The PLAYER reviews
the draft and, when satisfied, promotes an entry into REAL queued work
(POST /api/shadow/{idx}/promote).

Mirrors the ServerOwnedSeed isolation pattern from test_serve.py / test_sim.py:
every test redirects state/disk side effects into a throwaway temp directory so
the real think_tank.db is never touched.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim


class ShadowSim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-shadow-sim-')
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

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _roster(self):
        return [
            {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False},
            {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'isAdmin': False},
            {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
        ]

    def _agents(self):
        return {
            'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                    'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False,
                    'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0,
                    'weekApprovals': 0, 'completedRooms': []},
            'ben': {'id': 'ben', 'x': sim.SPAWN['x'] + 30, 'y': sim.SPAWN['y'],
                    'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False,
                    'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0,
                    'weekApprovals': 0, 'completedRooms': []},
            'faye': {'id': 'faye', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                     'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                     'inRoom': None, 'offDuty': False,
                     'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0,
                     'weekApprovals': 0, 'completedRooms': []},
        }

    def _base_state(self, work_queue=None):
        far_future = int(time.time() * 1000) + 60 * 60 * 24 * 365 * 10
        return {
            'sim': {'owner': 'server'},
            'agentRoster': self._roster(),
            'agents': self._agents(),
            'reports': [],
            'researchTopics': [],
            'lastSkillReviewAt': far_future,
            'lastStuckGateSweep': far_future,
            'lastSocialAt': far_future,
            'lastDistillAt': far_future,
            'workQueue': work_queue if work_queue is not None else [],
        }

    def test_queue_work_carries_the_shadow_flag_onto_the_durable_item(self):
        # The dry-run marker must survive the queue whitelist round trip, or the
        # real task would never know it is a dry run (same whitelist contract as
        # 'distill'/'checklist').
        state = self._base_state()
        sim.queue_work(state, [{
            'title': 'Draft a landing page', 'room': 'pressoffice',
            'instructions': 'Explore it, ship nothing.', 'shadow': True,
        }])
        self.assertEqual(state['workQueue'][0]['shadow'], True)
        # A normal item stays a normal (non-shadow) item.
        sim.queue_work(state, [{
            'title': 'Real task', 'room': 'pressoffice', 'instructions': 'ship it',
        }])
        self.assertEqual(state['workQueue'][1]['shadow'], False)

    def test_assign_task_stamps_shadow_onto_the_real_task(self):
        # assignment stamps the dry-run flag so the completion path captures to
        # the shadow ledger instead of shipping.
        state = self._base_state()
        grid, doors = sim._load_outdoor_geometry()
        task = sim.assign_task(
            state, 'ada', 'Draft', 'pressoffice', 'go', 'p', {'shadow': True},
            grid, doors, time.time(), [0])
        self.assertTrue(task['shadow'])
        self.assertEqual(task['status'], 'walking')

    def test_complete_shadow_task_ledgers_draft_and_releases_without_credits(self):
        # The heart of shadow mode: the task is marked done, its outcome lands in
        # state['shadowLedger'] with promoted=False, and the agent is released --
        # but approvedCount/weekApprovals/completedRooms are untouched (nothing
        # ships).
        state = self._base_state()
        a = state['agents']['ada']
        task = {'id': 'task-9', 'title': 'Dry run', 'room': 'pressoffice',
                'instructions': 'explore', 'projectLabel': 'p', 'taskType': 'code',
                'note': 'findings here', 'libraryPath': 'archive/9.md'}
        a['task'] = 'task-9'
        a['busy'] = True
        a['inRoom'] = 'pressoffice'
        entry = sim._complete_shadow_task(state, 'ada', task, now_ms=5000)
        self.assertEqual(task['status'], 'done')
        self.assertEqual(task['shadowDoneAt'], 5000)
        self.assertEqual(entry['title'], 'Dry run')
        self.assertEqual(entry['room'], 'pressoffice')
        self.assertEqual(entry['note'], 'findings here')
        self.assertEqual(entry['agentId'], 'ada')
        self.assertEqual(entry['completedAt'], 5000)
        self.assertIs(entry['promoted'], False)
        # Append-only ledger got the draft.
        self.assertEqual(len(state['shadowLedger']), 1)
        self.assertIs(state['shadowLedger'][0], entry)
        # Agent released...
        self.assertIsNone(a['task'])
        self.assertFalse(a['busy'])
        self.assertIsNone(a['inRoom'])
        # ...with NO real-world credit.
        self.assertEqual(a['approvedCount'], 0)
        self.assertEqual(a['weekApprovals'], 0)
        self.assertEqual(a['completedRooms'], [])

    def test_full_shadow_task_cycle_completes_to_ledger_with_no_approvals(self):
        # Drive the whole engine (assignment + walk + work + completion) on a
        # single shadow item. It must complete into the shadow ledger and the
        # worker must go off duty WITHOUT any approvedCount/weekApprovals bump --
        # the "work happened, the world didn't move" invariant.
        state = self._base_state(work_queue=[{
            'title': 'Draft the checkout flow', 'room': 'pressoffice',
            'instructions': 'Design it on paper only.', 'shadow': True,
        }])
        grid, doors = sim._load_outdoor_geometry()
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        for _ in range(400):
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            if state.get('shadowLedger'):
                break
        self.assertEqual(len(state['shadowLedger']), 1,
                         'the shadow task must complete into the draft ledger')
        entry = state['shadowLedger'][0]
        self.assertEqual(entry['title'], 'Draft the checkout flow')
        self.assertIs(entry['promoted'], False)
        # The completed task exists and is done (shadow branch skips the gate).
        tasks = state.get('tasks') or {}
        self.assertTrue(any(t.get('status') == 'done' and t.get('shadow')
                            for t in tasks.values()))
        # The worker who did the dry run earned NO real credit.
        worker = entry['agentId']
        self.assertEqual(state['agents'][worker].get('approvedCount', 0), 0,
                         'a shadow dry run must not bump approvedCount')
        self.assertEqual(state['agents'][worker].get('weekApprovals', 0), 0,
                         'a shadow dry run must not bump weekApprovals')
        # Queue drained -- the shadow item was consumed.
        self.assertEqual(state['workQueue'], [])

    def test_sprint_progress_excludes_shadow_work(self):
        # A dry-run must not count toward a sprint -- it ships nothing. The same
        # (title, room) pair queued as shadow and completed as a shadow task must
        # not move the sprint's done/in-progress/queued counts.
        state = self._base_state()
        state['sprints'] = {
            'sp-1': {'id': 'sp-1', 'status': 'active',
                     'items': [('Draft a feature', 'pressoffice')]},
        }
        state['workQueue'] = [{'title': 'Draft a feature', 'room': 'pressoffice',
                               'shadow': True}]
        state['tasks'] = {
            'task-1': {'id': 'task-1', 'title': 'Draft a feature',
                       'room': 'pressoffice', 'status': 'done', 'shadow': True},
        }
        progress = sim.sprint_progress(state, 'sp-1')
        # Shadow work is invisible to the sprint: the item is not queued, not
        # done, not in progress -- it simply doesn't exist to the sprint.
        self.assertEqual(progress['queued'], 0)
        self.assertEqual(progress['done'], 0)
        self.assertEqual(progress['inProgress'], 0)
        self.assertEqual(progress['landed'], [])
        # Control: the same pair WITHOUT the shadow flags counts normally.
        state2 = self._base_state()
        state2['sprints'] = {
            'sp-1': {'id': 'sp-1', 'status': 'active',
                     'items': [('Draft a feature', 'pressoffice')]},
        }
        state2['tasks'] = {
            'task-1': {'id': 'task-1', 'title': 'Draft a feature',
                       'room': 'pressoffice', 'status': 'done'},
        }
        progress2 = sim.sprint_progress(state2, 'sp-1')
        self.assertEqual(progress2['done'], 1)
        self.assertEqual(progress2['landed'], ['Draft a feature'])


class ShadowEndpoints(unittest.TestCase):
    """The player-facing shadow surface: read the draft ledger and promote a
    draft into REAL queued work. TestClient used WITHOUT a context manager so the
    app's lifespan never runs; state/DB seams patched so nothing touches the real
    think_tank.db."""
    from fastapi.testclient import TestClient

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-shadow-endpoint-')
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

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger_state(self):
        return {
            'agentRoster': [],
            'agents': {},
            'workQueue': [],
            'reports': [],
            'researchTopics': [],
            'shadowLedger': [
                {'title': 'Draft the auth flow', 'room': 'observatory',
                 'instructions': 'explore', 'projectLabel': 'auth',
                 'taskType': 'spike', 'note': 'OAuth works, JWT is easier.',
                 'libraryPath': None, 'agentId': 'ada',
                 'completedAt': 1000, 'promoted': False},
            ],
        }

    def test_get_shadow_returns_ledger_newest_first(self):
        state = self._ledger_state()
        state['shadowLedger'].append(
            {'title': 'Draft the billing UI', 'room': 'pressoffice',
             'instructions': 'explore', 'projectLabel': 'billing',
             'taskType': 'code', 'note': 'needs a tier picker',
             'agentId': 'ben', 'completedAt': 2000, 'promoted': False})
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = self.TestClient(serve.app)
            r = c.get('/api/shadow')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['count'], 2)
        # Newest first: the billing UI (completedAt 2000) is the first entry.
        self.assertEqual(body['shadow'][0]['title'], 'Draft the billing UI')
        self.assertEqual(body['shadow'][1]['title'], 'Draft the auth flow')

    def test_promote_queues_real_work_and_marks_the_draft_promoted(self):
        state = self._ledger_state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            c = self.TestClient(serve.app)
            r = c.post('/api/shadow/0/promote', json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['promoted'], 0)
        # A REAL (non-shadow) follow-up task was queued -- the draft's finding
        # becomes the new task's instructions so it rides the normal gate path.
        queued = saved['workQueue'][0]
        self.assertEqual(queued['shadow'], False)
        self.assertIn('OAuth works', queued['instructions'])
        self.assertEqual(queued['title'], 'Follow up: Draft the auth flow')
        # The ledger entry is now marked promoted (immutable record of the dry run).
        self.assertTrue(saved['shadowLedger'][0]['promoted'])
        self.assertIsNotNone(saved['shadowLedger'][0]['promotedAt'])
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1], 'shadow_promoted')
        self.assertEqual(passport.call_args[0][0], 'shadow_promoted')

    def test_promote_spike_keeps_the_follow_up_a_spike(self):
        state = self._ledger_state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            c = self.TestClient(serve.app)
            r = c.post('/api/shadow/0/promote', json={'taskType': 'spike'})
        self.assertEqual(r.status_code, 200, r.text)
        queued = saved['workQueue'][0]
        self.assertEqual(queued['taskType'], 'spike')
        self.assertIn('budgetMs', queued)

    def test_promote_bad_index_is_400(self):
        state = self._ledger_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = self.TestClient(serve.app)
            r = c.post('/api/shadow/not-an-int/promote', json={})
        self.assertEqual(r.status_code, 400)
        save.assert_not_called()

    def test_promote_unknown_index_is_404(self):
        state = self._ledger_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = self.TestClient(serve.app)
            r = c.post('/api/shadow/9/promote', json={})
        self.assertEqual(r.status_code, 404)
        save.assert_not_called()

    def test_promote_already_promoted_is_409(self):
        state = self._ledger_state()
        state['shadowLedger'][0]['promoted'] = True
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = self.TestClient(serve.app)
            r = c.post('/api/shadow/0/promote', json={})
        self.assertEqual(r.status_code, 409)
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
