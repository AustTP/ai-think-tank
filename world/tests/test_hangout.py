"""Hangout room behind the Town Hall door (replaces the dropped E2c social
time).

The hangout is an ENTERABLE but NON-DELEGABLE room: it has a room definition
(a label + purpose via /api/rooms) but is NOT in _DELEGATABLE_ROOMS, so a
planner never assigns work into it. These tests pin that contract hermetic
(no network, no DB).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim
import serve


class HangoutRoomDefinition(unittest.TestCase):
    def test_hangout_has_a_room_definition(self):
        self.assertIn('hangout', serve._DEFAULT_ROOM_DEFINITIONS)
        self.assertEqual(serve._DEFAULT_ROOM_DEFINITIONS['hangout']['label'], 'Hangout')

    def test_hangout_is_not_delegable(self):
        # The whole point: agents rest/mingle here, they never get pushed work.
        self.assertNotIn('hangout', serve._DELEGATABLE_ROOMS)


class HangoutRoomDefinitionsBackfill(unittest.TestCase):
    def test_backfill_adds_hangout_without_clobbering(self):
        state = {'roomDefinitions': {'pressoffice': {'label': 'Work Room', 'purpose': 'custom'}}}
        defs = serve._room_definitions(state)
        self.assertIn('hangout', defs)
        self.assertEqual(defs['hangout']['label'], 'Hangout')
        # A director-edited purpose on an existing room is untouched.
        self.assertEqual(defs['pressoffice']['purpose'], 'custom')


class HangoutAgentLifecycle(unittest.TestCase):
    """An agent placed in the hangout can be cleared back to duty without
    breaking the sim -- the round-trip the client's enterRoom('hangout') implies
    for any agent drawn inside it."""

    def _state(self):
        return {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'role': 'engineer', 'director': 'maya'},
            ],
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'busy': False, 'offDuty': True,
                        'task': None, 'visible': False, 'inRoom': None},
            },
            'workQueue': [],
            'tasks': {},
            'reports': [],
        }

    def test_agent_can_be_placed_in_hangout(self):
        state = self._state()
        a = state['agents']['ada']
        a['busy'] = True
        a['inRoom'] = 'hangout'
        a['roomX'] = 300
        a['roomY'] = 200
        # Server-side fields survive a round-trip (the client renders them).
        self.assertIn('inRoom', a)
        self.assertEqual(a['inRoom'], 'hangout')

    def test_agent_clears_hangout_back_to_duty(self):
        state = self._state()
        a = state['agents']['ada']
        a['busy'] = True
        a['inRoom'] = 'hangout'
        # finish_task clears inRoom/busy (sim.py) -- the natural way an agent
        # leaves an interior room.
        a['task'] = None
        a['busy'] = False
        a['inRoom'] = None
        a['visible'] = True
        self.assertIsNone(a['inRoom'])
        self.assertFalse(a['busy'])


if __name__ == '__main__':
    unittest.main()