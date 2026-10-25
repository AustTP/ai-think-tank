"""Coverage-gap tests for the research-desk ops part.

Only new tests. The self-edit and budget endpoints are driven through
TestClient with their stores and gates patched; nothing touches the live DB,
network, or model tiers.
"""
import os
import sys
import unittest
from unittest import mock

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402,F401


def _player_client():
    client = TestClient(serve.app)
    client.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return client


class BudgetRequestGapTests(unittest.TestCase):
    def test_missing_requested_usd_is_refused(self):
        client = _player_client()
        resp = client.post('/api/budget/request', json={'directorId': 'd1'})
        self.assertEqual(400, resp.status_code)
        self.assertIn('requestedUsd', resp.json()['error'])


class _FakeSelfEdit:
    def __init__(self, propose=None, status=None, windows=None, revert=None):
        self.propose_result = propose
        self.status_result = status if status is not None else []
        self.windows_result = windows if windows is not None else []
        self.revert_result = revert
        self.calls = []

    def propose(self, *args, **kwargs):
        self.calls.append(('propose', args, kwargs))
        if isinstance(self.propose_result, Exception):
            raise self.propose_result
        return self.propose_result

    def status_snapshot(self, limit):
        self.calls.append(('status', limit))
        return self.status_result

    def windows_snapshot(self, limit):
        self.calls.append(('windows', limit))
        return self.windows_result

    def revert(self, edit_id, actor):
        self.calls.append(('revert', edit_id, actor))
        if isinstance(self.revert_result, Exception):
            raise self.revert_result
        return self.revert_result


class SelfEditProposeGapTests(unittest.TestCase):
    def setUp(self):
        self.client = _player_client()

    def _post(self, body):
        return self.client.post('/api/selfedit/propose', json=body,
                                headers={'X-Agent-Key': 'k'})

    def test_rate_limited(self):
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=False), \
             mock.patch.object(serve, 'log_action'):
            resp = self._post({'agentId': 'a'})
        self.assertEqual(429, resp.status_code)

    def test_validation(self):
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True):
            resp = self._post({'agentId': 'a'})
        self.assertEqual(400, resp.status_code)

    def test_success_and_refusal(self):
        fake = _FakeSelfEdit(propose=(True, {'id': 'e1', 'status': 'applied',
                                             'message': 'queued'}))
        body = {'agentId': 'a', 'path': 'world/x.py', 'find': 'old',
                'replace': 'new', 'summary': 'why', 'windowOnly': True}
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            resp = self._post(body)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('e1', resp.json()['id'])
        self.assertEqual(('propose', ('a', 'world/x.py', 'old', 'new', 'why'),
                          {'window_only': True}), fake.calls[0])

        fake.propose_result = (False, {'error': 'anchor not unique'})
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            resp = self._post(body)
        self.assertEqual(400, resp.status_code)
        self.assertIn('anchor not unique', resp.json()['error'])

    def test_harness_exception(self):
        fake = _FakeSelfEdit(propose=RuntimeError('quorum down'))
        body = {'agentId': 'a', 'path': 'p', 'find': 'f', 'replace': 'r',
                'summary': 's'}
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            resp = self._post(body)
        self.assertEqual(500, resp.status_code)
        self.assertIn('quorum down', resp.json()['error'])


class SelfEditStatusGapTests(unittest.TestCase):
    def setUp(self):
        self.client = _player_client()

    def _post(self, body):
        return self.client.post('/api/selfedit/status', json=body,
                                headers={'X-Agent-Key': 'k'})

    def test_rate_limited(self):
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=False), \
             mock.patch.object(serve, 'log_action'):
            resp = self._post({'agentId': 'a'})
        self.assertEqual(429, resp.status_code)

    def test_invalid_limit_defaults_and_success(self):
        fake = _FakeSelfEdit(status=[{'id': 'e1'}], windows=[{'id': 'w1'}])
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            resp = self._post({'agentId': 'a', 'limit': 'bad'})
        self.assertEqual(200, resp.status_code)
        self.assertEqual({'edits': [{'id': 'e1'}],
                          'windows': [{'id': 'w1'}]}, resp.json())
        self.assertIn(('status', 15), fake.calls)
        self.assertIn(('windows', 15), fake.calls)


class SelfEditRevertGapTests(unittest.TestCase):
    def test_requires_edit_id(self):
        client = _player_client()
        resp = client.post('/api/selfedit/revert', json={})
        self.assertEqual(400, resp.status_code)

    def test_unauthorized(self):
        client = TestClient(serve.app)
        with mock.patch.object(serve, '_valid_agent_key_presented',
                               return_value=True), \
             mock.patch.object(serve, 'verify_session', return_value=False), \
             mock.patch.object(serve, 'verify_agent_key', return_value=False):
            resp = client.post('/api/selfedit/revert',
                               json={'editId': 'e1'})
        self.assertEqual(403, resp.status_code)

    def test_player_success(self):
        client = _player_client()
        fake = _FakeSelfEdit(revert=(True, {'id': 'e1', 'status': 'reverted'}))
        with mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            resp = client.post('/api/selfedit/revert', json={'editId': 'e1'})
        self.assertEqual(200, resp.status_code)
        self.assertEqual(('revert', 'e1', 'player'), fake.calls[0])

    def test_admin_agent_success(self):
        client = TestClient(serve.app)
        fake = _FakeSelfEdit(revert=(True, {'id': 'e1'}))
        with mock.patch.object(serve, '_valid_agent_key_presented',
                               return_value=True), \
             mock.patch.object(serve, 'verify_session', return_value=False), \
             mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             mock.patch.object(serve, '_is_admin', return_value=True), \
             mock.patch.object(serve, 'get_state_from_db', return_value={}), \
             mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            resp = client.post(
                '/api/selfedit/revert',
                json={'editId': 'e1', 'agentId': 'theo'},
                headers={'X-Agent-Key': 'k'})
        self.assertEqual(200, resp.status_code)
        self.assertEqual(('revert', 'e1', 'theo'), fake.calls[0])

    def test_exception(self):
        client = _player_client()
        fake = _FakeSelfEdit(revert=RuntimeError('restore failed'))
        with mock.patch.object(serve, '_selfedit', fake), \
             mock.patch.object(serve, 'log_action'):
            resp = client.post('/api/selfedit/revert', json={'editId': 'e1'})
        self.assertEqual(500, resp.status_code)
        self.assertIn('restore failed', resp.json()['error'])


if __name__ == '__main__':
    unittest.main()
