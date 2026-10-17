"""Hermetic tests for the Theo role policy wiring in the request
router: the same lane means different authority depending on who
is talking to Theo (_route_player_request). No Jev, no network,
no database -- the lane decider and every lane handler are
replaced with fakes, and log_action is patched out."""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..')))

import serve  # noqa: E402  (import serve before content)
import theo_policy  # noqa: E402


def _state():
    """A minimal roster the role resolver can read: one admin
    (Theo), two directors (nora directly, ben under nora), and
    one worker (cora)."""
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'ben', 'director': 'nora'},
            {'id': 'cora'},
        ],
        'agents': {
            'theo': {'id': 'theo', 'busy': False},
            'nora': {'id': 'nora', 'busy': False},
            'ben': {'id': 'ben', 'busy': False},
            'cora': {'id': 'cora', 'busy': False},
        },
    }


class RouterPolicy(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.state = _state()
        self.handled = []
        self.lane = 'ask'
        serve._lane_decider = lambda state, text: self.lane
        serve._theo_policy = theo_policy.TheoPolicy()

        async def fake_handler(state, text, admin_id, session_id=None):
            self.handled.append(
                {'lane': self.lane, 'text': text, 'admin': admin_id,
                 'session': session_id})
            return {'reply': 'handled'}

        self._orig_handlers = dict(serve._ROUTING_HANDLERS)
        self._orig_decider = serve._lane_decider
        self._orig_policy = serve._theo_policy
        for lane in ('ask', 'unclear', 'schedule', 'schedule_once',
                     'spike', 'story', 'incident'):
            serve._ROUTING_HANDLERS[lane] = fake_handler
        log_patch = unittest.mock.patch.object(serve, 'log_action')
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def tearDown(self):
        serve._ROUTING_HANDLERS.clear()
        serve._ROUTING_HANDLERS.update(self._orig_handlers)
        serve._lane_decider = self._orig_decider
        serve._theo_policy = self._orig_policy

    async def test_owner_reaches_every_lane(self):
        for lane in ('ask', 'unclear', 'schedule', 'schedule_once',
                     'spike', 'story', 'incident'):
            self.lane = lane
            self.handled.clear()
            result = await serve._route_player_request(
                self.state, 'do the thing', session_id='chat-1')
            self.assertEqual(result.get('reply'), 'handled', lane)
            self.assertEqual(len(self.handled), 1, lane)
            if lane in ('ask', 'unclear'):
                self.assertEqual(self.handled[0]['session'], 'chat-1',
                                 lane)
            else:
                self.assertIsNone(self.handled[0]['session'], lane)

    async def test_director_reaches_action_lanes(self):
        for lane in ('schedule', 'schedule_once', 'spike', 'story'):
            self.lane = lane
            self.handled.clear()
            result = await serve._route_player_request(
                self.state, 'do the thing', requester_id='nora')
            self.assertEqual(result.get('reply'), 'handled', lane)
            self.assertEqual(len(self.handled), 1, lane)

    async def test_worker_converse_and_report_only(self):
        self.lane = 'ask'
        result = await serve._route_player_request(
            self.state, 'what is the plan?', requester_id='cora')
        self.assertEqual(result.get('reply'), 'handled')
        self.lane = 'incident'
        result = await serve._route_player_request(
            self.state, 'the shop is down', requester_id='cora')
        self.assertEqual(result.get('reply'), 'handled')

    async def test_worker_denied_action_lanes(self):
        for lane in ('schedule', 'schedule_once', 'spike', 'story'):
            self.lane = lane
            self.handled.clear()
            result = await serve._route_player_request(
                self.state, 'do the thing', requester_id='cora')
            self.assertIn("I can't do that:", result.get('reply', ''), lane)
            self.assertIn('role worker', result.get('reply', ''), lane)
            self.assertEqual(self.handled, [], lane)

    async def test_unknown_requester_is_a_worker(self):
        self.lane = 'story'
        result = await serve._route_player_request(
            self.state, 'build it', requester_id='ghost')
        self.assertIn("I can't do that:", result.get('reply', ''))
        self.assertEqual(self.handled, [])

    async def test_empty_text_rejected_before_policy(self):
        result = await serve._route_player_request(self.state, '   ')
        self.assertEqual(result, {'error': 'a message is required',
                                  'status': 400})
        self.assertEqual(self.handled, [])

    async def test_owner_approves_pending_gated_action(self):
        decision = serve._theo_policy.evaluate(
            self.state, 'cora', 'spend', {'amountUsd': 5.0})
        self.assertTrue(decision.requires_approval)
        approval_id = decision.approval_id
        result = await serve._route_player_request(
            self.state, 'approve %s' % approval_id)
        self.assertEqual(result.get('reply'),
                         'Approval %s approved.' % approval_id)
        again = serve._theo_policy.evaluate(
            self.state, 'cora', 'spend', {'amountUsd': 5.0})
        self.assertTrue(again.allowed)

    async def test_owner_denies_pending_gated_action(self):
        decision = serve._theo_policy.evaluate(
            self.state, 'cora', 'spend', {'amountUsd': 5.0})
        approval_id = decision.approval_id
        result = await serve._route_player_request(
            self.state, 'deny %s' % approval_id)
        self.assertEqual(result.get('reply'),
                         'Approval %s denied.' % approval_id)
        again = serve._theo_policy.evaluate(
            self.state, 'cora', 'spend', {'amountUsd': 5.0})
        self.assertTrue(again.requires_approval)
        self.assertNotEqual(again.approval_id, approval_id)

    async def test_owner_approval_command_needs_real_id(self):
        result = await serve._route_player_request(
            self.state, 'approve apr-0000000000000000')
        self.assertIn('No pending approval', result.get('reply', ''))

    async def test_agent_cannot_adjudicate_approvals(self):
        self.lane = 'ask'
        result = await serve._route_player_request(
            self.state, 'approve apr-0000000000000000',
            requester_id='cora')
        self.assertEqual(result.get('reply'), 'handled')
        self.assertEqual(len(self.handled), 1)

    async def test_gated_lane_action_asks_owner(self):
        self.lane = 'spike'
        result = await serve._route_player_request(
            self.state, 'look into something', requester_id='nora')
        self.assertEqual(result.get('reply'), 'handled')


if __name__ == '__main__':
    unittest.main()
