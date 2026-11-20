"""Operator card management endpoints (2026-11-20): /api/tasks/{id}/edit and
/api/tasks/{id}/cancel. Tests drive TestClient with the DB, session, and log
seams patched -- nothing touches the real think_tank.db. TestClient is used
WITHOUT a context manager so the app's lifespan never runs."""
import contextlib
import os
import sys
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402


def _card(task_id='task-100', status='working', instructions='do the thing',
          assigned_to='gabri'):
    return {
        'id': task_id, 'title': 'A card', 'status': status,
        'assignedTo': assigned_to, 'instructions': instructions,
        'priority': 2, 'createdAt': 1795000000000,
    }


def _state():
    parent = _card('task-100', instructions='parent story')
    child = _card('task-101', assigned_to='rhonda')
    parent['tasks'] = {'task-101': child}
    done_card = _card('task-102', status='done')
    return {'tasks': {
        'task-100': parent,
        'task-102': done_card,
    }, 'agents': {}}


class TaskCardAdminEndpoints(unittest.TestCase):

    def _patch_write(self, state):
        saved = {}
        stack = contextlib.ExitStack()
        for p in (
            unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state),
            unittest.mock.patch.object(serve, 'save_state_to_db',
                                       side_effect=lambda s: saved.update(s)),
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, 'log_action'),
        ):
            stack.enter_context(p)
        return saved, stack

    def test_edit_requires_auth(self):
        state = _state()
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state):
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-100/edit', json={'instructions': 'x'})
        self.assertEqual(r.status_code, 401)

    def test_edit_unknown_task_404(self):
        state = _state()
        with self._patch_write(state)[1]:
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-999/edit', json={'instructions': 'x'})
        self.assertEqual(r.status_code, 404)

    def test_edit_updates_instructions_and_priority(self):
        state = _state()
        saved, patches = self._patch_write(state)
        with patches:
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-100/edit',
                       json={'instructions': 'read the rubric first', 'priority': 1})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertIsNone(body['parentTaskId'])
        card = saved['tasks']['task-100']
        self.assertEqual(card['instructions'], 'read the rubric first')
        self.assertEqual(card['priority'], 1)
        self.assertTrue(card['operatorEdited'])
        kinds = [h['kind'] for h in card['history']]
        self.assertIn('operator_edit', kinds)

    def test_edit_reaches_nested_piece_and_reports_parent(self):
        state = _state()
        saved, patches = self._patch_write(state)
        with patches:
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-101/edit',
                       json={'instructions': 'child instructions v2'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['parentTaskId'], 'task-100')
        self.assertEqual(
            saved['tasks']['task-100']['tasks']['task-101']['instructions'],
            'child instructions v2')

    def test_edit_without_editable_fields_400(self):
        state = _state()
        with self._patch_write(state)[1]:
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-100/edit', json={'status': 'done'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('editable', r.json()['error'])

    def test_edit_refuses_terminal_card(self):
        state = _state()
        with self._patch_write(state)[1]:
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-102/edit', json={'instructions': 'x'})
        self.assertEqual(r.status_code, 409)

    def test_cancel_marks_card_terminal_with_provenance(self):
        state = _state()
        saved, patches = self._patch_write(state)
        with patches:
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-100/cancel',
                       json={'reason': 'superseded by rubric edition'})
        self.assertEqual(r.status_code, 200, r.text)
        card = saved['tasks']['task-100']
        self.assertEqual(card['status'], 'cancelled')
        self.assertEqual(card['deliveryState'], 'cancelled')
        self.assertEqual(card['cancelledBy'], 'player')
        self.assertEqual(card['cancelledReason'], 'superseded by rubric edition')
        self.assertNotIn('_contentInFlight', card)
        self.assertNotIn('_peerRepairPending', card)
        self.assertIn('operator_cancel', [h['kind'] for h in card['history']])

    def test_cancel_refuses_already_terminal(self):
        state = _state()
        with self._patch_write(state)[1]:
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-102/cancel', json={'reason': 'x'})
        self.assertEqual(r.status_code, 409)

    def test_cancel_unknown_task_404(self):
        state = _state()
        with self._patch_write(state)[1]:
            c = TestClient(serve.app)
            r = c.post('/api/tasks/task-999/cancel', json={'reason': 'x'})
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()
