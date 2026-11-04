"""The task replay endpoint (450_task_replay.py): one request answers "what
did the agent actually do?" for a task -- its record, its child cards, and
its ordered action_log trail. Covers the auth gate (player session or admin
agent key; ordinary agents get 401), the 404 for unknown tasks, and the trail
ordering/composition on the sqlite backend."""
import json
import os
import tempfile
import time
import unittest
import unittest.mock

import serve


class TaskReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix='task-replay-')
        cls._patcher = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(cls._tmp, 'test.db'),
            THINK_TANK_DIR=cls._tmp,
            AGENTS_DIR=os.path.join(cls._tmp, 'agents'),
            LIBRARY_DIR=os.path.join(cls._tmp, 'library'),
            PASSPORT_PATH=os.path.join(cls._tmp, 'library', '.passport.json'),
            COLAB_STANDBY_ENABLED=False,
        )
        cls._patcher.start()
        serve.init_db()

    @classmethod
    def tearDownClass(cls):
        cls._patcher.stop()
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _seed(self):
        with serve._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO action_log (ts, agent_id, action, details) "
                "VALUES (?, ?, ?, ?)",
                (time.time() - 60, 'ruth', 'task_assigned',
                 json.dumps({'taskId': 'task-1', 'title': 'x'})),
            )
            conn.execute(
                "INSERT OR REPLACE INTO action_log (ts, agent_id, action, details) "
                "VALUES (?, ?, ?, ?)",
                (time.time() - 30, 'ruth', 'execute',
                 json.dumps({'taskId': 'task-1', 'command': 'ls'})),
            )
            conn.execute(
                "INSERT OR REPLACE INTO action_log (ts, agent_id, action, details) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), 'ruth', 'task_completion_refused',
                 'not json at all but mentions "task-1" inline'),
            )
        state = serve.get_state_from_db() or {}
        state['agentRoster'] = [{'id': 'theo', 'isAdmin': True}]
        state['tasks'] = {
            'task-1': {'id': 'task-1', 'title': 'Review the thing',
                       'status': 'failed', 'assignedTo': 'ruth',
                       'taskType': 'research'},
            'task-2': {'id': 'task-2', 'title': 'Fix: Review the thing',
                       'status': 'walking', 'assignedTo': 'ruth',
                       'reviewOf': 'task-1'},
        }
        serve.save_state_to_db(state)

    def _client(self):
        from starlette.testclient import TestClient
        return TestClient(serve.app)

    def _get(self, path):
        with unittest.mock.patch.object(serve, 'verify_agent_key',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_agent_id_for_key',
                                        return_value='theo'):
            return self._client().get(path, headers={'X-Agent-Key': 'k'})

    def test_requires_operator(self):
        self._seed()
        r = self._client().get('/api/replay/task-1')
        self.assertEqual(r.status_code, 401)

    def test_unknown_task_is_404(self):
        self._seed()
        r = self._get('/api/replay/task-nope')
        self.assertEqual(r.status_code, 404)

    def test_trail_is_ordered_and_children_listed(self):
        self._seed()
        r = self._get('/api/replay/task-1')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        actions = [row['action'] for row in body['trail']]
        self.assertEqual(actions, ['task_assigned', 'execute',
                                   'task_completion_refused'])
        self.assertEqual(body['task']['title'], 'Review the thing')
        self.assertEqual([c['id'] for c in body['children']], ['task-2'])


if __name__ == '__main__':
    unittest.main()
