"""A2A facade: Agent Card, message/send -> request, tasks/get, tasks/cancel.

Hermetic: DB redirected to a temp dir; no model calls, no network.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_TMP = None
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='tt-a2a-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP, 'test.db'),
        THINK_TANK_DIR=_TMP,
        AGENTS_DIR=os.path.join(_TMP, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP, 'library'),
        PASSPORT_PATH=os.path.join(_TMP, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _state(**overrides):
    state = {
        'agents': {
            'theo': {'id': 'theo', 'name': 'Theo', 'busy': False, 'offDuty': False},
            'nora': {'id': 'nora', 'name': 'Nora', 'busy': False, 'offDuty': False},
            'ruth': {'id': 'ruth', 'name': 'Ruth', 'busy': False, 'offDuty': False},
        },
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'name': 'Nora', 'isDirector': True},
            {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'},
        ],
        'teams': [{'id': 'nora', 'directorId': 'nora', 'members': ['ruth']}],
        'tasks': {}, 'workQueue': [], 'backlogRequests': [], 'sim': {},
    }
    state.update(overrides)
    return state


class A2A(unittest.TestCase):
    def setUp(self):
        self.theo_key = serve.get_or_create_agent_key('theo')
        self.ruth_key = serve.get_or_create_agent_key('ruth')
        serve.save_state_to_db(_state())
        self.client = TestClient(serve.app)

    def _send(self, text, key=None, message_id='m1', metadata=None):
        return self.client.post(
            '/a2a',
            headers={'Authorization': f'Bearer {key or self.theo_key}'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'message/send',
                  'params': {'message': {
                      'role': 'user', 'messageId': message_id,
                      'parts': [{'kind': 'text', 'text': text}],
                      'metadata': metadata or {}}}})

    def test_agent_card_is_public_and_lists_skills(self):
        for path in ('/.well-known/agent-card.json', '/.well-known/agent.json'):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            card = r.json()
            self.assertEqual(card['protocolVersion'], '0.3.0')
            self.assertIn('file_work', [s['id'] for s in card['skills']])
            self.assertEqual(card['url'].rsplit('/', 1)[-1], 'a2a')

    def test_message_send_files_a_request_for_a_team(self):
        r = self._send('Build a small status page', metadata={'team': 'nora'})
        self.assertEqual(r.status_code, 200, r.text)
        task = r.json()['result']
        self.assertTrue(task['id'].startswith('wrq-'))
        self.assertEqual(task['status']['state'], 'submitted')
        state = serve.get_state_from_db()
        req = next(x for x in state['backlogRequests'] if x['id'] == task['id'])
        self.assertEqual(req['origin'], 'a2a')
        self.assertEqual(req['teamId'], 'nora')
        self.assertEqual(req['a2aCaller'], 'theo')
        self.assertEqual(req['a2aMessageId'], 'm1')

    def test_same_message_id_is_idempotent(self):
        first = self._send('Build one page', metadata={'team': 'nora'}).json()['result']
        second = self._send('Build one page', metadata={'team': 'nora'}).json()['result']
        self.assertEqual(first['id'], second['id'])
        state = serve.get_state_from_db()
        self.assertEqual(len(state['backlogRequests']), 1)

    def test_tasks_get_reports_working_completed_and_artifacts(self):
        task_id = self._send('Ship the report', metadata={'team': 'nora'}).json()['result']['id']
        state = serve.get_state_from_db()
        state['tasks']['task-1'] = {
            'id': 'task-1', 'title': 'Ship the report', 'requestId': task_id,
            'status': 'working', 'libraryPath': None}
        serve.save_state_to_db(state)
        r = self.client.post('/a2a', headers={'Authorization': f'Bearer {self.theo_key}'},
                             json={'jsonrpc': '2.0', 'id': 2, 'method': 'tasks/get',
                                   'params': {'id': task_id}})
        self.assertEqual(r.json()['result']['status']['state'], 'working')

        state = serve.get_state_from_db()
        state['tasks']['task-1']['status'] = 'done'
        state['tasks']['task-1']['libraryPath'] = 'archive/report.md'
        serve.save_state_to_db(state)
        r = self.client.post('/a2a', headers={'Authorization': f'Bearer {self.theo_key}'},
                             json={'jsonrpc': '2.0', 'id': 3, 'method': 'tasks/get',
                                   'params': {'id': task_id}})
        task = r.json()['result']
        self.assertEqual(task['status']['state'], 'completed')
        self.assertEqual(task['artifacts'][0]['parts'][0]['text'], 'archive/report.md')

    def test_tasks_cancel_removes_only_queued_cards(self):
        task_id = self._send('Cancel me', metadata={'team': 'nora'}).json()['result']['id']
        state = serve.get_state_from_db()
        state['backlogRequests'][0]['status'] = 'accepted'
        state['workQueue'].append({'title': 'card', 'requestId': task_id, 'taskType': 'code'})
        serve.save_state_to_db(state)
        r = self.client.post('/a2a', headers={'Authorization': f'Bearer {self.theo_key}'},
                             json={'jsonrpc': '2.0', 'id': 4, 'method': 'tasks/cancel',
                                   'params': {'id': task_id}})
        self.assertEqual(r.json()['result']['status']['state'], 'canceled')
        state = serve.get_state_from_db()
        self.assertEqual(state['workQueue'], [])
        req = next(x for x in state['backlogRequests'] if x['id'] == task_id)
        self.assertEqual(req['status'], 'cancelled')

    def test_tasks_cancel_refuses_work_already_assigned(self):
        task_id = self._send('Already running', metadata={'team': 'nora'}).json()['result']['id']
        state = serve.get_state_from_db()
        state['tasks']['task-9'] = {'id': 'task-9', 'title': 'Already running',
                                    'requestId': task_id, 'status': 'working'}
        serve.save_state_to_db(state)
        r = self.client.post('/a2a', headers={'Authorization': f'Bearer {self.theo_key}'},
                             json={'jsonrpc': '2.0', 'id': 5, 'method': 'tasks/cancel',
                                   'params': {'id': task_id}})
        self.assertEqual(r.json()['error']['code'], -32002)

    def test_non_director_key_is_rejected_by_the_handler(self):
        r = self._send('Should not file', key=self.ruth_key)
        self.assertEqual(r.status_code, 403)
        state = serve.get_state_from_db()
        self.assertEqual(state['backlogRequests'], [])

    def test_bad_bearer_is_rejected_by_the_middleware(self):
        r = self._send('Nope', key='not-a-key')
        self.assertEqual(r.status_code, 401)

    def test_unknown_method_and_missing_task(self):
        r = self.client.post('/a2a', headers={'Authorization': f'Bearer {self.theo_key}'},
                             json={'jsonrpc': '2.0', 'id': 6, 'method': 'nope'})
        self.assertEqual(r.json()['error']['code'], -32601)
        r = self.client.post('/a2a', headers={'Authorization': f'Bearer {self.theo_key}'},
                             json={'jsonrpc': '2.0', 'id': 7, 'method': 'tasks/get',
                                   'params': {'id': 'wrq-999'}})
        self.assertEqual(r.json()['error']['code'], -32001)


if __name__ == '__main__':
    unittest.main()
