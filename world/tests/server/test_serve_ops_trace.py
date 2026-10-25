import os
import sys
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


def _state():
    return {
        'agents': {'nora': {'busy': False, 'offDuty': False, 'task': None},
                   'ben': {'busy': True, 'offDuty': False, 'task': 'task-2'}},
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'role': 'Chief of Staff', 'isAdmin': True},
            {'id': 'nora', 'name': 'Nora', 'role': 'Supervisor', 'isDirector': True},
            {'id': 'ben', 'name': 'Ben', 'role': 'Supervisor', 'isDirector': True},
        ],
        'tasks': {'task-1': {'id': 'task-1', 'assignedTo': 'nora', 'title': 'A'}},
    }


class _Conn:
    def __init__(self):
        self.sql = ''
    def execute(self, sql, params=None):
        self.sql = sql
        return self
    def fetchall(self):
        if 'MAX(ts)' in self.sql:
            return [('nora', 30.0, 2), ('ben', 20.0, 1), ('ghost', 10.0, 1)]
        if 'FROM action_log' in self.sql:
            return [(1, 'execute', '{"command":"pytest -q","purpose":"check"}', 30.0, 'tr-1'),
                    (2, 'chat', '{"text":"looked at the code"}', 29.0, None)]
        if 'FROM decision_tape' in self.sql:
            return [('classify', 'm', 'allow', 0.81, 0.02, 28.0)]
        if 'FROM stm_messages' in self.sql:
            return [('assistant', 'I will check the failing test.', 27.0),
                    ('user', 'Please inspect it.', 26.0)]
        if 'FROM task_checkpoints' in self.sql:
            return [('task-1', 'execute', '{"plan":"Read the test, then fix it."}', 25.0)]
        return []
    def __iter__(self):
        return iter(self.fetchall())


class OpsTrace(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(serve.app)
        self.patches = [
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')),
            unittest.mock.patch.object(serve, 'get_state_from_db', return_value=_state()),
        ]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        p = unittest.mock.patch.object(serve, '_db')
        self.db = p.start(); self.addCleanup(p.stop)
        self.db.return_value.__enter__.return_value = _Conn()

    def test_agents_are_listed_by_recent_activity(self):
        r = self.client.get('/api/ops/agents')
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()['agents']
        self.assertEqual(rows[0]['id'], 'nora')
        self.assertIn('ghost', [row['id'] for row in rows])
        self.assertEqual(rows[0]['lastDay'], 2)

    def test_trace_merges_actions_thoughts_decisions_and_plans(self):
        r = self.client.get('/api/ops/agents/nora/trace')
        self.assertEqual(r.status_code, 200, r.text)
        events = r.json()['events']
        kinds = {e['kind'] for e in events}
        self.assertEqual(kinds, {'action', 'thought', 'decision', 'message'})
        self.assertTrue(any('pytest' in str(e.get('details')) for e in events))
        self.assertTrue(any(e.get('taskId') == 'task-1' for e in events))

    def test_unknown_agent_and_auth(self):
        self.assertEqual(self.client.get('/api/ops/agents/ghost/trace').status_code, 404)
        with unittest.mock.patch.object(serve, '_operator_gate', return_value=(False, None)):
            self.assertEqual(self.client.get('/api/ops/agents').status_code, 401)
            self.assertEqual(self.client.get('/api/ops/agents/nora/trace').status_code, 401)


def test_trace_helpers_cover_non_json_shapes():
    assert serve._trace_json(None) is None
    assert serve._trace_json('not json') is None
    assert serve._trace_json('123') is None
    assert serve._action_summary('execute', 'plain text') == 'plain text'
    assert serve._action_summary('execute', {}) == 'execute'
    event = serve._action_event((1, 'execute', 'plain text', 2.0, None))
    assert event['tool'] == 'execute'
    assert event['summary'] == 'plain text'


class OpsTraceEdges(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(serve.app)
        self.state = _state()
        self.state['agentRoster'].append({'id': 'nora', 'name': 'Duplicate'})
        for p in (
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')),
            unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self.state),
        ):
            p.start(); self.addCleanup(p.stop)

    def test_duplicate_roster_entry_and_checkpoint_without_plan(self):
        class _Conn:
            def execute(self, sql, params=None):
                self.sql = sql
                return self
            def fetchall(self):
                if 'MAX(ts)' in self.sql:
                    return []
                if 'FROM action_log' in self.sql:
                    return []
                if 'FROM task_checkpoints' in self.sql:
                    return [('task-1', 'execute', '{}', 5.0)]
                return []
            def __iter__(self):
                return iter(self.fetchall())
        with unittest.mock.patch.object(serve, '_db') as db:
            db.return_value.__enter__.return_value = _Conn()
            rows = self.client.get('/api/ops/agents').json()['agents']
            self.assertEqual([r['id'] for r in rows].count('nora'), 1)
            trace = self.client.get('/api/ops/agents/nora/trace').json()
            self.assertEqual(trace['events'], [])
