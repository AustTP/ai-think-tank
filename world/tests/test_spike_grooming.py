import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402
import sim  # noqa: E402


class SpikeGrooming(unittest.TestCase):
    def state(self):
        return {
            'agentRoster': [{'id': 'theo', 'isAdmin': True}],
            'agents': {'theo': {'id': 'theo', 'busy': False, 'offDuty': False},
                       'nora': {'id': 'nora', 'busy': False, 'offDuty': False}},
            'teams': [{'id': 'theo', 'directorId': 'theo'},
                      {'id': 'nora', 'directorId': 'nora'}],
            'tasks': {'spike': {'id': 'spike', 'taskType': 'spike', 'status': 'done',
                                'title': 'Research', 'note': 'Evidence and gaps'}},
            'workQueue': [],
        }

    def promote(self, state, authenticated=True):
        request = Mock()
        request.json = AsyncMock(return_value={'groom': True})
        with patch.object(serve, '_require_player_session', return_value=authenticated), \
                patch.object(serve, 'get_state_from_db', return_value=state), \
                patch.object(serve, 'save_state_to_db') as save, \
                patch.object(serve, 'log_action'), \
                patch.object(serve, '_append_passport_decision'):
            response = asyncio.run(serve.intent_promote_spike('spike', request))
        return response, save

    def test_promotion_files_theo_request_and_is_idempotent(self):
        state = self.state()
        response, save = self.promote(state)
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with(state)
        req = state['backlogRequests'][0]
        self.assertEqual(req['groomerId'], 'theo')
        self.assertEqual(req['status'], 'pending')
        self.assertEqual(req['taskType'], 'spike')
        self.assertIn('Evidence and gaps', req['reason'])
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(state['tasks']['spike']['status'], 'done')
        self.assertEqual(state['tasks']['spike']['promotedRequestId'], req['id'])
        response, save = self.promote(state)
        self.assertTrue(json.loads(response.body)['alreadyPromoted'])
        save.assert_not_called()
        self.assertEqual(len(state['backlogRequests']), 1)

    def test_waits_for_theo_instead_of_borrowing_nora(self):
        state = self.state()
        self.promote(state)
        self.assertEqual(sim._ceremony_facilitator(state, 'theo'), 'theo')
        state['agents']['theo']['busy'] = True
        self.assertIsNone(sim._ceremony_facilitator(state, 'theo'))

    def test_duplicate_promotions_create_one_request(self):
        state = self.state()
        state['tasks']['duplicate'] = dict(state['tasks']['spike'], id='duplicate', title='  RESEARCH ')
        self.promote(state)
        self.assertEqual(state['tasks']['duplicate']['promotedRequestId'],
                         state['tasks']['spike']['promotedRequestId'])
        self.promote(state)
        self.assertEqual(len(state['backlogRequests']), 1)

    def test_deny_duplicates_preserves_reports_and_blocks_promotion(self):
        state = self.state()
        state['tasks']['spike']['libraryPath'] = 'archive/findings.md'
        state['tasks']['duplicate'] = dict(state['tasks']['spike'], id='duplicate')
        state['tasks']['different'] = dict(state['tasks']['spike'], id='different', projectLabel='Other project')
        request = Mock()
        with patch.object(serve, '_require_player_session', return_value=True), \
                patch.object(serve, 'get_state_from_db', return_value=state), \
                patch.object(serve, 'save_state_to_db'), patch.object(serve, 'log_action'):
            response = asyncio.run(serve.intent_deny_spike('spike', request))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(json.loads(response.body)['deniedIds']), {'spike', 'duplicate'})
        self.assertNotIn('triageDeniedAt', state['tasks']['different'])
        self.assertEqual(state['tasks']['spike']['status'], 'done')
        self.assertEqual(state['tasks']['spike']['libraryPath'], 'archive/findings.md')
        response, save = self.promote(state)
        self.assertEqual(response.status_code, 409)
        save.assert_not_called()

    def test_deny_requires_player_and_completed_spike(self):
        state = self.state()
        request = Mock()
        for authenticated, status, expected in [(False, 'done', 401), (True, 'working', 409)]:
            state['tasks']['spike']['status'] = status
            with patch.object(serve, '_require_player_session', return_value=authenticated), \
                    patch.object(serve, 'get_state_from_db', return_value=state), \
                    patch.object(serve, 'save_state_to_db') as save:
                response = asyncio.run(serve.intent_deny_spike('spike', request))
            self.assertEqual(response.status_code, expected)
            save.assert_not_called()

    def test_unavailable_theo_and_auth_do_not_hide_spike(self):
        state = self.state()
        response, save = self.promote(state, authenticated=False)
        self.assertEqual(response.status_code, 401)
        save.assert_not_called()
        state['agentRoster'] = []
        response, save = self.promote(state)
        self.assertEqual(response.status_code, 409)
        save.assert_not_called()
        self.assertNotIn('promotedRequestId', state['tasks']['spike'])
