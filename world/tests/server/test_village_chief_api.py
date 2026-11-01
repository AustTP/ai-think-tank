"""POST /api/villages/chief: the village chief-of-staff appointment endpoint.

Player-only, one chief per village, auto-pick falls back to the village's
teams-row director, and the result is the roster tier core governance
reads -- not a field on the village record.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _state():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'dana', 'director': 'theo'},
            {'id': 'wren', 'director': 'theo'},
        ],
        'agents': {
            'theo': {'villageId': 'main'},
            'dana': {'villageId': 'main'},
            'wren': {'villageId': 'main'},
        },
        'teams': [{'id': 't1', 'directorId': 'dana', 'scrumMasterId': 'wren'},
                  {'id': 't2', 'directorId': 'wren', 'scrumMasterId': None}],
        'villages': [{'id': 'main', 'name': 'Main'}],
        'tasks': {},
    }


class VillageChiefEndpoint(unittest.TestCase):
    def _post(self, state, body):
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'theo')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, 'save_state_to_db') as save, \
             mock.patch.object(serve, 'log_action') as log:
            r = TestClient(serve.app).post('/api/villages/chief', json=body)
        return r, save, log

    def test_auto_picks_the_village_director(self):
        state = _state()
        r, save, log = self._post(state, {'villageId': 'main'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'ok': True, 'villageId': 'main',
                                    'chief': 'dana'})
        save.assert_called_once()
        log.assert_called_once()

    def test_roster_carries_the_tier_not_the_village_record(self):
        state = _state()
        self._post(state, {'villageId': 'main'})
        entry = next(e for e in state['agentRoster'] if e['id'] == 'dana')
        self.assertTrue(entry['villageChief'])
        self.assertEqual(entry['chiefOfVillage'], 'main')
        self.assertEqual(entry['role'], 'chief_of_staff')
        self.assertNotIn('chiefOfVillage', state['villages'][0])

    def test_unknown_village_is_404(self):
        state = _state()
        r, save, _ = self._post(state, {'villageId': 'nope'})
        self.assertEqual(r.status_code, 404)
        save.assert_not_called()

    def test_explicit_agent_must_belong_to_the_village(self):
        state = _state()
        r, save, _ = self._post(state, {'villageId': 'main',
                                        'agentId': 'dana'})
        self.assertEqual(r.status_code, 200)
        r2, save2, _ = self._post(state, {'villageId': 'nope',
                                          'agentId': 'dana'})
        self.assertEqual(r2.status_code, 404)
        save2.assert_not_called()

    def test_reappointment_replaces(self):
        state = _state()
        self._post(state, {'villageId': 'main'})
        r, _, _ = self._post(state, {'villageId': 'main', 'agentId': 'wren'})
        self.assertEqual(r.status_code, 200)
        by_id = {e['id']: e for e in state['agentRoster']}
        self.assertFalse(by_id['dana'].get('villageChief'))
        self.assertTrue(by_id['wren']['villageChief'])


if __name__ == '__main__':
    unittest.main()
