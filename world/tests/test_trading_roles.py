import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trading_roles


class TradingRoles(unittest.TestCase):
    def test_four_distinct_roles_with_persistent_profiles(self):
        state = {}
        created = trading_roles.create_roles(state)
        self.assertEqual(len(created), 4)
        self.assertEqual(len(state['cryptoTradingBrief']['roleAssignments']), 4)
        for aid in created:
            agent = state['agents'][aid]
            self.assertTrue(agent['profile']['mission'])
            self.assertGreaterEqual(len(agent['profile']['instructions']), 4)
            self.assertTrue(agent['offDuty'])

    def test_repeated_provisioning_preserves_existing_agent_work(self):
        state = {}
        trading_roles.create_roles(state)
        state['agents']['crypto-researcher']['task'] = 'existing-task'
        self.assertEqual(trading_roles.create_roles(state), [])
        self.assertEqual(len(state['agentRoster']), 4)
        self.assertEqual(state['agents']['crypto-researcher']['task'], 'existing-task')

    def test_risk_and_execution_roles_keep_user_limits_and_paper_boundary(self):
        state = {}
        trading_roles.create_roles(state)
        self.assertIn('$50', state['agents']['risk-manager']['profile']['mission'])
        instructions = ' '.join(state['agents']['execution-operator']['profile']['instructions'])
        self.assertIn('Live trading is unavailable', instructions)
        self.assertIn('player authorization', instructions)
