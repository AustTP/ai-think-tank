"""The operator policy data that steers agents or weakens a safety net is
Tier B: editable, but only with the player's signature (selfedit audit tiers)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selfedit  # noqa: E402

WORLD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

POLICY_DATA = (
    'tool_descriptions.json', 'redaction.json', 'actions.json',
    'model_bands.json', 'service_costs.json', 'browse_policy.json',
    'rooms.json', 'seed_profiles.json', 'endpoint_policy.json',
)


class PolicyDataSignatureTest(unittest.TestCase):
    def test_policy_data_requires_a_player_signature(self):
        for name in POLICY_DATA:
            with self.subTest(name=name):
                self.assertIn(name, selfedit.PLAYER_SIGNATURE_BASENAMES)
                self.assertTrue(selfedit.is_player_signature(os.path.join(WORLD, name)))

    def test_agent_visible_policy_stays_unsigned(self):
        # The watchlist has a deliberate village write-back lane, and the
        # plain-writing list is content the agents are told about directly.
        for name in ('watchlist.json', 'plain_writing.json', 'research_desk.json'):
            with self.subTest(name=name):
                self.assertFalse(selfedit.is_player_signature(os.path.join(WORLD, name)))

    def test_gated_data_is_unreadable_to_agents(self):
        for name in POLICY_DATA:
            with self.subTest(name=name):
                self.assertTrue(selfedit.is_unreadable(os.path.join(WORLD, name)))


if __name__ == '__main__':
    unittest.main()
