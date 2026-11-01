"""POST /mcp -- the read-only MCP edge over the board (2026-11-01).

Pinned here: the edge is DISABLED without THINK_TANK_MCP_TOKEN (503, no
partial exposure), a wrong token is 401 and logged, and the protocol answers
initialize / ping / tools/list / tools/call statelessly. The tool set is
read-only projections of the House board -- board_overview, list_cards,
card_detail -- and every tool call is written to the action log.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_STATE = {
    'agents': {'w1': {'id': 'w1', 'name': 'W One'}},
    'tasks': {
        'task-1': {'id': 'task-1', 'title': 'Research the market',
                   'status': 'working', 'assignedTo': 'w1', 'teamId': 'ops',
                   'phase': 'prep', 'taskType': 'research', 'note': 'Found three competitors.'},
        'task-2': {'id': 'task-2', 'title': 'Build the portal',
                   'status': 'done', 'assignedTo': 'w1', 'teamId': 'ops'},
    },
    'workQueue': [
        {'title': 'Parked experiment', 'lane': 'parking-lot', 'notBefore': 1},
    ],
    'backlogRequests': [
        {'id': 'wrq-1', 'status': 'pending', 'goal': 'Build the onboarding portal'},
    ],
}


def _state():
    import copy
    return copy.deepcopy(_STATE)


def _post(client, payload, token='tok-123'):
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return client.post('/mcp', json=payload, headers=headers)


class McpEdgeTest(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient
        self._patch = unittest.mock.patch.object(
            serve, 'get_state_from_db', side_effect=lambda: _state())
        self._patch.start()
        self._env = unittest.mock.patch.dict(os.environ,
                                             {'THINK_TANK_MCP_TOKEN': 'tok-123'})
        self._env.start()
        self.client = TestClient(serve.app)

    def tearDown(self):
        self._patch.stop()
        self._env.stop()

    def test_disabled_without_a_token(self):
        with unittest.mock.patch.dict(os.environ, {'THINK_TANK_MCP_TOKEN': ''}):
            resp = _post(self.client, {'jsonrpc': '2.0', 'id': 1,
                                            'method': 'tools/list'})
        self.assertEqual(resp.status_code, 503)

    def test_wrong_token_is_unauthorized(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 1,
                                        'method': 'tools/list'}, token='wrong')
        self.assertEqual(resp.status_code, 401)

    def test_initialize_reports_the_server(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 1,
                                        'method': 'initialize',
                                        'params': {'protocolVersion': '2024-11-05'}})
        self.assertEqual(resp.status_code, 200)
        result = resp.json()['result']
        self.assertEqual(result['serverInfo']['name'], 'think-tank-board')
        self.assertEqual(result['protocolVersion'], '2024-11-05')

    def test_tools_list_is_read_only(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 2,
                                        'method': 'tools/list'})
        # tools/list returns a STRUCTURED result (the object itself), not a
        # content block -- only tools/call wraps its payload in content[].
        tools = resp.json()['result']['tools']
        names = [t['name'] for t in tools]
        self.assertEqual(names, ['board_overview', 'list_cards', 'card_detail'])
        self.assertTrue(all(t['inputSchema'].get('type') == 'object' for t in tools))

    def test_board_overview_counts(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 3,
                                        'method': 'tools/call',
                                        'params': {'name': 'board_overview',
                                                   'arguments': {}}})
        text = resp.json()['result']['content'][0]['text']
        payload = __import__('json').loads(text)
        self.assertEqual(payload['counts']['inProgress'], 1)
        self.assertEqual(payload['counts']['parkedCards'], 1)
        self.assertEqual(payload['counts']['done'], 1)
        self.assertEqual(payload['pendingLargeRequests'][0]['id'], 'wrq-1')

    def test_list_cards_filters(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 4,
                                        'method': 'tools/call',
                                        'params': {'name': 'list_cards',
                                                   'arguments': {'status': 'done'}}})
        payload = __import__('json').loads(resp.json()['result']['content'][0]['text'])
        self.assertEqual([c['taskId'] for c in payload['cards']], ['task-2'])

    def test_card_detail_carries_the_note(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 5,
                                        'method': 'tools/call',
                                        'params': {'name': 'card_detail',
                                                   'arguments': {'taskId': 'task-1'}}})
        payload = __import__('json').loads(resp.json()['result']['content'][0]['text'])
        self.assertEqual(payload['note'], 'Found three competitors.')
        self.assertEqual(payload['phase'], 'prep')

    def test_card_detail_unknown_task(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 6,
                                        'method': 'tools/call',
                                        'params': {'name': 'card_detail',
                                                   'arguments': {'taskId': 'nope'}}})
        payload = __import__('json').loads(resp.json()['result']['content'][0]['text'])
        self.assertIn('error', payload)

    def test_unknown_tool_is_a_protocol_error(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 7,
                                        'method': 'tools/call',
                                        'params': {'name': 'move_card',
                                                   'arguments': {}}})
        self.assertEqual(resp.json()['error']['code'], -32602)

    def test_notifications_get_202(self):
        resp = _post(self.client, {'jsonrpc': '2.0',
                                        'method': 'notifications/initialized'})
        self.assertEqual(resp.status_code, 202)

    def test_tool_calls_are_logged(self):
        with unittest.mock.patch.object(serve, 'log_action') as logged:
            _post(self.client, {'jsonrpc': '2.0', 'id': 8,
                                     'method': 'tools/call',
                                     'params': {'name': 'board_overview',
                                                'arguments': {}}})
        self.assertTrue(any(c.args[1] == 'mcp_edge_call' for c in logged.call_args_list))

    def test_bad_json_body(self):
        resp = _post(self.client, {'jsonrpc': '2.0', 'id': 9, 'method': 'x'})
        # A valid JSON dict with an unknown method is a protocol error, not 400.
        self.assertEqual(resp.json()['error']['code'], -32601)


if __name__ == '__main__':
    unittest.main()
