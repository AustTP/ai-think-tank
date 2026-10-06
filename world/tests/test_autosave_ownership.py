"""A stale browser must not downgrade server ownership."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve


class AutosaveOwnership(unittest.TestCase):
    def test_two_stale_saves_preserve_roster_and_ownership(self):
        existing = {'sim': {'owner': 'server', 'tick': 10},
                    'agentRoster': [{'id': 'admin', 'isAdmin': True}],
                    'agents': {'admin': {'id': 'admin', 'busy': False}},
                    'teams': [{'id': 'research'}]}
        incoming = {'sim': {'owner': 'client', 'tick': 1},
                    'agentRoster': [], 'agents': {}, 'teams': []}
        first = serve._merge_server_owned(existing, incoming)
        second = serve._merge_server_owned(first, incoming)
        self.assertEqual(second['sim'], existing['sim'])
        self.assertEqual(second['agentRoster'], existing['agentRoster'])
        self.assertEqual(second['teams'], existing['teams'])
        self.assertIn('admin', second['agents'])
        self.assertEqual(incoming['sim']['owner'], 'client')
