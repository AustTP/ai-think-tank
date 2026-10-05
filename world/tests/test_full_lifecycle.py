"""Full-lifecycle integration test: the whole ship-path, headless and hermetic.

From a seeded think tank, drive the complete loop seam (get_state_from_db ->
SimEngine.tick -> save_state_to_db, the exact body of sim._sim_loop_pass) and
follow one deliverable story all the way through:

    assigned -> walked -> real content produced -> peer gate (needs_review)
        -> two distinct clean votes -> done -> graded + lastReviewed
        -> product released (RELEASE.md frozen under library/projects/<id>/v1/)
        -> artifacts published (git push mocked)

Hire/onboard are governance concerns already driven by test_onboard.py and
test_governance.py; this test starts from a working team and proves the
SHIP path (the part no single test covered end-to-end). Every inter-step
boundary reads back from the authoritative SQLite DB -- nothing is held in
memory between phases. The publish's real `git push` + `gh auth token` are
mocked out; the release's on-disk sandbox is a static fixture dir.

Wall-clock would make this 400 ticks x SIM_TICK_S tens of minutes, so `now` is
injected (engine.tick already threads an injectable clock).
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim
import serve


def _pressoffice_item(**over):
    item = {'title': 'Build the checkout flow', 'room': 'pressoffice',
            'instructions': 'implement the payment + order confirmation',
            'pair': False, 'notBefore': None, 'priority': sim.WORK_PRIORITY['normal'],
            'goal': 'proj', 'taskType': 'code', 'skillReview': False}
    item.update(over)
    return item


def _seed(work_queue):
    """Same team shape as test_runs_closed: ben ON-DUTY and idle right at the
    pressoffice door so assignment is deterministic (an off-duty wake casts an
    unpredictable walk). Faye is isAdmin so the gate excludes her as a reviewer."""
    far_future = int(time.time() * 1000) + 60 * 60 * 24 * 365 * 10
    _, doors = sim._load_outdoor_geometry()
    po = (doors or {}).get('pressoffice') or {'x': 1220, 'y': 646, 'w': 52, 'h': 50}
    door_x = po['x'] + po['w'] / 2
    door_y = po['y'] + po['h'] + 4
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'faye', 'name': 'Faye', 'role': 'admin', 'isAdmin': True},
            {'id': 'ada', 'name': 'Ada', 'role': 'engineer'},
            {'id': 'ben', 'name': 'Ben', 'role': 'engineer'},
            {'id': 'cora', 'name': 'Cora', 'role': 'engineer'},
        ],
        'agents': {
            'faye': {'id': 'faye', 'x': 0, 'y': 0, 'busy': True, 'task': None,
                     'inRoom': None, 'offDuty': True, 'dir': 'south', 'path': [],
                     'approvedCount': 0, 'droppedCount': 0, 'hiredAt': 0, 'visible': True},
            'ada': {'id': 'ada', 'x': 600, 'y': 340, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': True, 'dir': 'south', 'path': [],
                    'approvedCount': 0, 'droppedCount': 0, 'hiredAt': 0, 'visible': True},
            'ben': {'id': 'ben', 'x': door_x, 'y': door_y, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                    'approvedCount': 0, 'droppedCount': 0, 'hiredAt': 0, 'visible': True},
            'cora': {'id': 'cora', 'x': 700, 'y': 340, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': True, 'dir': 'south', 'path': [],
                    'approvedCount': 0, 'droppedCount': 0, 'hiredAt': 0, 'visible': True},
        },
        'reports': [],
        'researchTopics': [],
        'lastSkillReviewAt': far_future,  # don't inject the standing sweep
        'lastHireAt': far_future,
        'lastFiringReviewAt': far_future,
        'workQueue': work_queue,
        'products': {
            'p1': {'id': 'p1', 'name': 'Storefront', 'teamId': 'faye',
                   'status': 'in_progress', 'sandboxId': 'sb-p1'},
        },
        'completedDeliverables': [],
        'growthPlans': {},
    }
    serve.save_state_to_db(state)
    # The release step needs a real sandbox repo dir on disk.
    sb = os.path.join(serve.SANDBOXES_DIR, 'sb-p1')
    os.makedirs(sb, exist_ok=True)
    with open(os.path.join(sb, 'checkout.py'), 'w') as f:
        f.write('# storefront checkout\n')
    return state


class FullLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-lifecycle-')
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

    def _tick_until(self, engine, now, predicate, max_ticks=400):
        for _ in range(max_ticks):
            now += sim.SIM_TICK_S
            state = serve.get_state_from_db()
            state = engine.tick(state, now=now)
            serve.save_state_to_db(state)
            if predicate(serve.get_state_from_db()):
                break
        return now, serve.get_state_from_db()

    def test_ship_path_produce_to_publish(self):
        # ---- produce: a deliverable story is written and lands in the gate ----
        calls = {}

        def fake_executor(snapshot, agent_id, task, base_ctx):
            calls['room'] = task.get('room')
            calls['author'] = agent_id
            self._real_store(task['id'], {'note': 'implemented checkout flow',
                                          'seenUrls': []})

        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        _seed([_pressoffice_item(goal='proj')])
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0

        def gated(s):
            return any(t.get('status') == 'needs_review' for t in (s.get('tasks') or {}).values())

        now, state = self._tick_until(engine, now, gated)
        tasks = state.get('tasks') or {}
        story = next(t for t in tasks.values() if t.get('status') == 'needs_review')
        self.assertEqual(story['room'], 'pressoffice')
        gate = story['_peerGate']
        self.assertEqual(gate['approvals'], 0)
        self.assertTrue(gate['reviewerIds'], 'gate must assign reviewers')
        self.assertEqual(calls.get('room'), 'pressoffice',
                         'a code deliverable must dispatch real content work')
        # Grading is fire-and-forget at gate entry: a completed deliverable with
        # a grade + Last-reviewed date already exists, awaiting (re)review.
        dels = state.get('completedDeliverables') or []
        self.assertEqual(len(dels), 1)
        self.assertEqual(dels[0]['id'], story['id'])
        self.assertIsInstance(dels[0].get('grade'), (int, float))
        self.assertTrue(dels[0].get('lastReviewed'), 'a landed deliverable carries a Last-reviewed date')

        # ---- accept: two distinct clean votes close the gate deterministically ----
        # The vote fold is a hermetic pure path (unit-tested in test_peer_approval);
        # inject both clean verdicts exactly as a completed review subtask would.
        # Two distinct reviewers cast clean votes; the fold counts each once.
        for rid in gate['reviewerIds']:
            sim._apply_content_result(
                state, {'id': f'rev-{rid}', 'reviewOf': story['id'], 'assignedTo': rid,
                        'taskType': 'review', 'status': 'working'},
                {'note': 'looks correct', 'peerVerdict': 'clean', 'pipelineOk': True,
                 'evidence': '[flake8]\nno issues\n[pytest-cov]\nTOTAL 100%'})
        self.assertEqual(state['tasks'][story['id']]['_peerGate']['approvals'], len(gate['reviewerIds']))
        self.assertTrue(sim._parent_close_from_vote(state, state['tasks'][story['id']], now_ms=2**62),
                        'two distinct clean votes must close the story')
        self.assertEqual(state['tasks'][story['id']]['status'], 'done')

        # ---- release: snapshot the sandbox into a frozen v1 with a RELEASE.md ----
        from starlette.testclient import TestClient as StarletteClient
        sid = serve.create_session()
        client = StarletteClient(serve.app)
        client.cookies.set(serve.SESSION_COOKIE_NAME, sid)
        r = client.post('/api/intent/product/p1/release', json={'revisionNote': 'ship it'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body.get('revision', {}).get('n'), 1)
        released = serve.get_state_from_db()['products']['p1']
        self.assertEqual(released['status'], 'released')

        # ---- publish: the released snapshot stages + (mock) pushes to GitHub ----
        fake_calls = {}

        def fake_git(*a, **k):
            fake_calls['git'] = fake_calls.get('git', 0) + 1
            return 0

        push_proc = unittest.mock.Mock()
        push_proc.returncode = 0
        push_proc.stdout = 'pushed main to github'
        push_proc.stderr = ''
        with unittest.mock.patch.object(serve, 'PUBLISH_REPO', 'testowner/test-repo'), \
             unittest.mock.patch.object(serve, '_run_git_sync', fake_git), \
             unittest.mock.patch.object(serve, 'subprocess') as fake_sp:
            fake_sp.run.return_value = push_proc
            r2 = client.post('/api/intent/publish')
        self.assertEqual(r2.status_code, 200, r2.text)
        pub = r2.json()
        self.assertEqual(pub.get('repo'), 'testowner/test-repo')
        staged_names = [p for p in pub.get('staged', []) if p.startswith('projects/p1')]
        self.assertTrue(staged_names, f'released project must be staged: {pub.get("staged")}')

        # The import-time CWD is never left with a staging dir or token on disk.
        self.assertFalse(os.path.exists(serve.PUBLISH_STAGING),
                         'staging dir (with its ephemeral token in .git) must be cleaned up')


if __name__ == '__main__':
    unittest.main(verbosity=2)