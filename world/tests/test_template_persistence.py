import asyncio
import copy
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402


class TemplatePersistence(unittest.TestCase):
    def test_new_role_without_holders_survives_a_fresh_read(self):
        stored = {'agentRoster': [{'id': 'admin', 'isAdmin': True}],
                  'agents': {'admin': {'role': 'Admin'}}, 'templates': {}}

        def save(state):
            stored.clear()
            stored.update(copy.deepcopy(state))

        request = Mock()
        request.json = AsyncMock(return_value={
            'role': 'Trader', 'mission': 'Draft sourced trading setups',
            'instructions': ['Track realized profit and loss'], 'notes': []})
        with patch.object(serve, '_resolve_requester', return_value='admin'), \
                patch.object(serve, 'verify_agent_key', return_value=True), \
                patch.object(serve, 'get_state_from_db', side_effect=lambda: copy.deepcopy(stored)), \
                patch.object(serve, 'save_state_to_db', side_effect=save) as persist, \
                patch.object(serve, 'log_action'), \
                patch.object(serve, '_append_passport_decision'):
            response = asyncio.run(serve.upsert_template(request))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(json.loads(response.body)['created'])
            self.assertEqual(json.loads(response.body)['appliedTo'], [])
            persist.assert_called_once()
            fresh = serve.get_state_from_db()
            self.assertEqual(fresh['templates']['Trader']['mission'],
                             'Draft sourced trading setups')
