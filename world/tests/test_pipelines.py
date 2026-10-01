"""Tests for the ordered-pipeline scheduler (sim.add_pipeline /
_check_pipelines + the /api/pipelines surface).

A pipeline is a named, cadenced sequence of steps that fires in STRICT order:
step N+1 is only queued after step N's task reaches 'done' (a pending/active
predecessor suppresses every later step -- never skipped). Steps ride the
normal queue_work -> assign -> content lifecycle; completion is read back off
the durable task mirror by matching the pipelineStep marker. cadence_ms is
floored to MIN_PIPELINE_CADENCE_MS; each step's offsetMs is a minimum delay
(from the run's start for step 0, from the predecessor's completion for the
rest) that strict ordering can only lengthen, never bypass.

Hermetic: DB redirected to a throwaway temp dir so no real think_tank.db is
touched; the pipeline sweep is exercised directly through sim helpers with
synthetic tasks, and the HTTP surface through TestClient without lifespan.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import sim  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-pipeline-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _state():
    return {
        'agents': {},
        'workQueue': [],
        'tasks': {},
        'researchTopics': [],
        'pipelines': [],
    }


class AddPipeline(unittest.TestCase):
    def test_rejects_empty_name(self):
        state = _state()
        self.assertIsNone(sim.add_pipeline(state, '', 3600000, [{'title': 'a', 'room': 'observatory'}]))
        self.assertEqual(state.get('pipelines'), [])

    def test_rejects_empty_or_bad_steps(self):
        state = _state()
        self.assertIsNone(sim.add_pipeline(state, 'pl', 3600000, []))
        self.assertIsNone(sim.add_pipeline(state, 'pl', 3600000, [{'title': 'no room'}]))
        self.assertIsNone(sim.add_pipeline(state, 'pl', 3600000, [{'room': 'observatory'}]))
        self.assertIsNone(sim.add_pipeline(state, 'pl', 3600000, ['not a dict']))
        self.assertEqual(state.get('pipelines'), [])

    def test_clamps_cadence_to_floor(self):
        state = _state()
        p = sim.add_pipeline(state, 'pl', 5000, [{'title': 'a', 'room': 'observatory'}])
        self.assertIsNotNone(p)
        self.assertEqual(p['cadenceMs'], sim.MIN_PIPELINE_CADENCE_MS)

    def test_clamps_negative_offset_and_ids_monotonic(self):
        state = _state()
        p1 = sim.add_pipeline(state, 'one', 7200000, [{'title': 'a', 'room': 'observatory', 'offsetMs': -5}])
        p2 = sim.add_pipeline(state, 'two', 7200000, [{'title': 'b', 'room': 'pressoffice'}])
        self.assertEqual(p1['steps'][0]['offsetMs'], 0)
        self.assertEqual(p1['id'], 'pl-1')
        self.assertEqual(p2['id'], 'pl-2')

    def test_creates_record_with_defaults(self):
        state = _state()
        p = sim.add_pipeline(state, 'launch', 7200000, [
            {'title': 'step one', 'room': 'observatory', 'instructions': 'do it',
             'tool': 'search_linkedin_posts', 'args': {'query': 'x'}},
        ])
        self.assertIsNotNone(p)
        self.assertEqual(p['name'], 'launch')
        self.assertEqual(p['steps'][0]['instructions'], 'do it')
        self.assertEqual(p['steps'][0]['tool'], 'search_linkedin_posts')
        self.assertEqual(p['runStepIndex'], 0)
        self.assertEqual(p['runId'], 0)
        self.assertEqual(p['lastRunAt'], 0)
        self.assertIsNone(p['lastStepCompletedAt'])


class CheckPipelines(unittest.TestCase):
    def test_fires_step_zero_on_first_pass(self):
        state = _state()
        p = sim.add_pipeline(state, 'pl', 7200000, [
            {'title': 'first', 'room': 'observatory'},
            {'title': 'second', 'room': 'pressoffice'},
        ])
        sim._check_pipelines(state, 1_000_000)
        self.assertEqual(len(state['workQueue']), 1)
        item = state['workQueue'][0]
        self.assertEqual(item['pipelineStep'], {'pipelineId': p['id'], 'runId': 1, 'stepIndex': 0})
        self.assertIn('first', item['title'])
        self.assertEqual(p['lastRunAt'], 1_000_000)

    def test_holds_sequence_while_predecessor_active(self):
        state = _state()
        p = sim.add_pipeline(state, 'pl', 7200000, [
            {'title': 'first', 'room': 'observatory'},
            {'title': 'second', 'room': 'pressoffice'},
        ])
        sim._check_pipelines(state, 1_000_000)
        # Simulate step 0 assigned and still working: a task carrying the marker.
        state['workQueue'] = []
        state['tasks'] = {'task-1': {
            'id': 'task-1', 'status': 'working', 'room': 'observatory',
            'title': 'first', 'pipelineStep': {'pipelineId': p['id'], 'runId': 1, 'stepIndex': 0},
        }}
        sim._check_pipelines(state, 1_000_001)
        self.assertEqual(len(state['workQueue']), 0, 'step 1 must NOT fire while step 0 is active')

    def test_fires_next_step_after_predecessor_done(self):
        state = _state()
        p = sim.add_pipeline(state, 'pl', 7200000, [
            {'title': 'first', 'room': 'observatory'},
            {'title': 'second', 'room': 'pressoffice'},
        ])
        sim._check_pipelines(state, 1_000_000)
        state['workQueue'] = []
        state['tasks'] = {'task-1': {
            'id': 'task-1', 'status': 'done', 'room': 'observatory',
            'title': 'first', 'pipelineStep': {'pipelineId': p['id'], 'runId': 1, 'stepIndex': 0},
        }}
        sim._check_pipelines(state, 1_100_000)
        self.assertEqual(len(state['workQueue']), 1)
        item = state['workQueue'][0]
        self.assertEqual(item['pipelineStep'], {'pipelineId': p['id'], 'runId': 1, 'stepIndex': 1})
        self.assertEqual(p['runStepIndex'], 1)

    def test_offset_gates_next_step_from_predecessor_completion(self):
        state = _state()
        p = sim.add_pipeline(state, 'pl', 7200000, [
            {'title': 'first', 'room': 'observatory'},
            {'title': 'second', 'room': 'pressoffice', 'offsetMs': 5000},
        ])
        sim._check_pipelines(state, 1_000_000)
        state['workQueue'] = []
        state['tasks'] = {'task-1': {
            'id': 'task-1', 'status': 'done', 'room': 'observatory',
            'title': 'first', 'pipelineStep': {'pipelineId': p['id'], 'runId': 1, 'stepIndex': 0},
        }}
        # 1ms after the pass that observed completion: offset not yet met.
        sim._check_pipelines(state, 1_100_001)
        self.assertEqual(len(state['workQueue']), 0)
        # 5s after completion: offset met -> fires.
        sim._check_pipelines(state, 1_105_001)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['pipelineStep']['stepIndex'], 1)

    def test_run_completes_then_waits_full_cadence(self):
        state = _state()
        p = sim.add_pipeline(state, 'pl', 7200000, [
            {'title': 'first', 'room': 'observatory'},
        ])
        sim._check_pipelines(state, 1_000_000)
        state['workQueue'] = []
        state['tasks'] = {'task-1': {
            'id': 'task-1', 'status': 'done', 'room': 'observatory',
            'title': 'first', 'pipelineStep': {'pipelineId': p['id'], 'runId': 1, 'stepIndex': 0},
        }}
        sim._check_pipelines(state, 1_100_000)
        self.assertEqual(len(state['workQueue']), 0, 'single-step run finished; must wait out cadence')
        # Just before the cadence window closes: still idle.
        sim._check_pipelines(state, 1_000_000 + 7200000 - 1)
        self.assertEqual(len(state['workQueue']), 0)
        # Window closed: a fresh run begins (step 0 re-fires, with a new run id).
        sim._check_pipelines(state, 1_000_000 + 7200000)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['pipelineStep']['stepIndex'], 0)
        self.assertEqual(state['workQueue'][0]['pipelineStep']['runId'], 2)

    def test_step_zero_offset_measured_from_run_start(self):
        state = _state()
        p = sim.add_pipeline(state, 'pl', 7200000, [
            {'title': 'first', 'room': 'observatory', 'offsetMs': 10000},
        ])
        sim._check_pipelines(state, 1_000_000)
        self.assertEqual(len(state['workQueue']), 0, 'step 0 offset runs from run start')
        sim._check_pipelines(state, 1_010_000)
        self.assertEqual(len(state['workQueue']), 1)


class PipelineMarkerThroughQueue(unittest.TestCase):
    def test_pipeline_step_survives_queue_whitelist(self):
        state = _state()
        sim.queue_work(state, [{
            'title': 'x', 'room': 'observatory',
            'pipelineStep': {'pipelineId': 'pl-1', 'stepIndex': 0},
        }])
        self.assertEqual(state['workQueue'][0]['pipelineStep'], {'pipelineId': 'pl-1', 'stepIndex': 0})

    def test_reclaimed_task_rethreads_pipeline_step(self):
        # A pipeline step that gets orphaned and reclaimed must keep its marker,
        # or _check_pipelines can never see its completion and the pipeline
        # wedges on that step forever.
        state = _state()
        state['workQueue'] = []
        state['tasks'] = {'task-1': {
            'id': 'task-1', 'status': 'working', 'room': 'observatory',
            'title': 'first', 'pipelineStep': {'pipelineId': 'pl-1', 'stepIndex': 0},
        }}
        reclaimed = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(reclaimed, 1)
        self.assertEqual(state['workQueue'][0]['pipelineStep'], {'pipelineId': 'pl-1', 'stepIndex': 0})


class PipelineEndpoints(unittest.TestCase):
    def _client(self, state):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        state_patch = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        return c

    def test_create_list_delete(self):
        state = _state()
        saved = {}
        with unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)):
            c = self._client(state)
            r = c.post('/api/pipelines', json={
                'name': 'linkedin',
                'cadenceMs': 7200000,
                'steps': [{'title': 'search', 'room': 'observatory', 'tool': 'search_linkedin_posts',
                           'args': {'query': 'think tanks'}}],
            })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        pl = body['pipeline']
        self.assertEqual(pl['name'], 'linkedin')
        self.assertEqual(saved.get('pipelines', [])[0]['id'], pl['id'])

        c = self._client(saved)
        r = c.get('/api/pipelines')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()['pipelines']), 1)

        c = self._client(saved)
        r = c.delete(f"/api/pipelines/{pl['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])
        c = self._client(saved)
        r = c.get('/api/pipelines')
        self.assertEqual(len(r.json()['pipelines']), 0)

    def test_create_rejects_bad_body(self):
        state = _state()
        c = self._client(state)
        r = c.post('/api/pipelines', json={'name': 'x'})
        self.assertEqual(r.status_code, 400)

    def test_delete_unknown_404(self):
        state = _state()
        c = self._client(state)
        r = c.delete('/api/pipelines/pl-999')
        self.assertEqual(r.status_code, 404)

    def test_auth_required(self):
        # No session cookie + no agent key -> the middleware bounces the write.
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/pipelines', json={'name': 'x', 'cadenceMs': 7200000, 'steps': []})
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()