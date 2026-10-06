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

    def test_reload_cannot_clear_active_work_or_resurrect_fired_agent(self):
        existing = {'sim': {'owner': 'server'}, 'agents': {
            'ada': {'busy': True, 'task': 't1', 'inRoom': 'observatory',
                    'approvedCount': 4, 'profile': {'notes': ['real result']}}},
                    'researchTopics': [{'id': 'topic', 'lastRunAt': 123}],
                    'reports': [{'id': 1}], 'nextReportId': 2}
        incoming = {'agents': {'ada': {'busy': False, 'task': None, 'inRoom': None,
                                      'approvedCount': 0, 'profile': {'notes': []}},
                               'fired': {'id': 'fired'}},
                    'researchTopics': [], 'reports': [], 'nextReportId': 1}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['agents']['ada'], existing['agents']['ada'])
        self.assertNotIn('fired', merged['agents'])
        self.assertEqual(merged['researchTopics'], existing['researchTopics'])
        self.assertEqual(merged['reports'], existing['reports'])
        self.assertEqual(merged['nextReportId'], 2)
