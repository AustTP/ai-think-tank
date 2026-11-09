"""A dict filedBy on a stored request must not crash the sim loop.

The adversarial launch passed _free_authority's ROSTER ROW (a dict) as the
authority id into file_large_request, which stored it verbatim as filedBy;
_breakdown_step then raised TypeError (unhashable type: dict) on EVERY sim
tick and froze the tank for hours (2026-11-08, 10764 loop errors).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402


class DictFiledBy(unittest.TestCase):
    def _state(self):
        return {'agentRoster': [{'id': 'ben', 'name': 'Ben', 'isAdmin': True}],
                'agents': {'ben': {'id': 'ben', 'offDuty': False}},
                'tasks': {}, 'workQueue': [], 'backlogRequests': [],
                'teams': [], 'sprints': {}}

    def test_file_large_request_normalizes_dict_authority(self):
        state = self._state()
        roster_row = {'id': 'ben', 'name': 'Ben'}
        req = sim.file_large_request(state, roster_row, 'A big goal', 'ben')
        self.assertEqual(req.get('filedBy'), 'ben',
                         'a roster row passed as the authority is stored as '
                         'its id, never as a dict')

    def test_breakdown_tolerates_dict_filed_by_in_stored_data(self):
        state = self._state()
        state['backlogRequests'].append({
            'id': 'wrq-bad', 'status': 'pending', 'title': 'Bad filedBy',
            'room': 'pressoffice', 'teamId': 'ben',
            'filedBy': {'id': 'ben', 'name': 'Ben'},  # legacy malformed row
        })
        pending = {'teamId': 'ben', 'reqIds': ['wrq-bad']}
        # The resolve must not raise; a dict filedBy reads as its id.
        sim._resolve_breakdown(state, pending, int(__import__('time').time() * 1000))
        self.assertTrue(True)


if __name__ == '__main__':
    unittest.main()
