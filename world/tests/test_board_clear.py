import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve


class BoardClear(unittest.TestCase):
    def test_only_selected_terminal_tasks_are_dismissed(self):
        state = {'tasks': {
            'failed': {'status': 'failed', 'note': 'Evidence'},
            'cancelled': {'status': 'cancelled'},
            'active': {'status': 'working'},
            'other': {'status': 'failed'},
        }}
        request = Mock()
        request.json = AsyncMock(return_value={'taskIds': ['failed', 'failed', 'cancelled', 'active', 'missing']})
        with patch.object(serve, '_require_player_session', return_value=True), \
                patch.object(serve, 'get_state_from_db', return_value=state), \
                patch.object(serve, 'save_state_to_db') as save, \
                patch.object(serve, 'log_action'):
            response = asyncio.run(serve.intent_clear_failed_board(request))
        self.assertEqual(json.loads(response.body)['taskIds'], ['failed', 'cancelled'])
        self.assertEqual(state['tasks']['failed']['status'], 'failed')
        self.assertEqual(state['tasks']['failed']['note'], 'Evidence')
        self.assertNotIn('boardClearedAt', state['tasks']['active'])
        self.assertNotIn('boardClearedAt', state['tasks']['other'])
        save.assert_called_once_with(state)

    def test_authentication_and_bad_payload(self):
        request = Mock()
        request.json = AsyncMock(return_value={'taskIds': 'bad'})
        with patch.object(serve, '_require_player_session', return_value=False):
            self.assertEqual(asyncio.run(serve.intent_clear_failed_board(request)).status_code, 401)
        with patch.object(serve, '_require_player_session', return_value=True):
            self.assertEqual(asyncio.run(serve.intent_clear_failed_board(request)).status_code, 400)
