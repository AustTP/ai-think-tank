"""Village resource policy endpoint and director visibility."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _state():
    return {
        'agentRoster': [
            {'id': 'dana', 'name': 'Dana', 'isDirector': True},
            {'id': 'wren', 'name': 'Wren', 'director': 'dana'},
            {'id': 'other', 'name': 'Other', 'isDirector': True},
            {'id': 'x', 'name': 'X', 'director': 'other'},
        ],
        'agents': {
            'dana': {'villageId': 'main'},
            'wren': {'villageId': 'main'},
            'other': {'villageId': 'vlg-2'},
            'x': {'villageId': 'vlg-2'},
        },
        'villages': [
            {'id': 'main', 'name': 'Main'},
            {'id': 'vlg-2', 'name': 'Second'},
        ],
        'tasks': {},
    }


class VillageResourceEndpoint(unittest.TestCase):
    def _client(self):
        return TestClient(serve.app)

    def test_get_returns_policy_and_usage(self):
        state = _state()
        with mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_resolve_requester', return_value='dana'):
            r = self._client().get('/api/villages/main/resources',
                                   headers={'X-Agent-Key': 'k'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['villageId'], 'main')
        self.assertEqual(body['policy']['maxConcurrent'], 4)
        self.assertIn('usage', body)

    def test_other_village_director_is_denied(self):
        state = _state()
        with mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_resolve_requester', return_value='other'):
            r = self._client().get('/api/villages/main/resources',
                                   headers={'X-Agent-Key': 'k'})
        self.assertEqual(r.status_code, 403)

    def test_post_sets_limit_and_hold(self):
        state = _state()

        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'dana')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            r = self._client().post(
                '/api/villages/main/resources',
                json={'maxConcurrent': 1, 'maxHeavy': 0,
                      'hold': ['media', 'gpu'], 'reason': 'laptop busy'})
        self.assertEqual(r.status_code, 200)
        policy = state['villages'][0]['resourcePolicy']
        self.assertEqual(policy['maxConcurrent'], 1)
        self.assertEqual(policy['maxHeavy'], 0)
        self.assertEqual(policy['hold'], ['gpu', 'media'])
        self.assertEqual(policy['reason'], 'laptop busy')

    def test_post_rejects_unknown_hold_class(self):
        state = _state()

        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'dana')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            r = self._client().post(
                '/api/villages/main/resources',
                json={'hold': ['toaster']})
        self.assertEqual(r.status_code, 400)
