"""Tests for the server-side simulation engine (world/sim.py).

Mirrors the ServerOwnedSeed isolation pattern from test_serve.py: every test
redirects state/disk side effects into a throwaway temp directory so the real
think_tank.db is never touched.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.config as config
import serve
import sim


class ServerOwnedPairingHandoff(unittest.TestCase):
    """W1: server-owned pairing/handoff. The movement engine already emits
    ('arrive','pair'/'handoff') events but the server dispatch dropped them
    (only ('arrive','task') resolved), and a `pair` card was assigned SOLO. Now
    the server recruits a navigator for a `pair` card, resolves pair/handoff
    arrivals, and releases a pair's navigator when the driver's task ships."""

    def _state(self):
        far_future = int(time.time() * 1000) + 60 * 60 * 24 * 365 * 10
        return {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False},
                {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'isAdmin': False},
                {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            ],
            'agents': {
                'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                        'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': False,
                        'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
                'ben': {'id': 'ben', 'x': sim.SPAWN['x'] + 30, 'y': sim.SPAWN['y'],
                        'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': False,
                        'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
                'faye': {'id': 'faye', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                         'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                         'inRoom': None, 'offDuty': False,
                         'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
            },
            'lastSkillReviewAt': far_future,
            'lastStuckGateSweep': far_future,
            'lastSocialAt': far_future,
            'lastDistillAt': far_future,
            'workQueue': [],
        }

    def test_pair_arrival_resolves_the_navigator_into_the_session(self):
        # A pair session's navigator reaching the driver's door must RESOLVE
        # (navigator joins the driver's workstation), not be dropped -- she
        # would otherwise stand frozen at the door forever.
        state = self._state()
        state['tasks'] = {'task-1': {'id': 'task-1', 'room': 'pressoffice',
                                     'status': 'working', 'title': 'Pair me'}}
        a = state['agents']['ada']
        a['pairWith'] = 'ben'
        a['pairTaskId'] = 'task-1'
        a['path'] = [{'x': 1, 'y': 1}]
        driver = state['agents']['ben']
        driver['busy'] = True
        driver['inRoom'] = 'pressoffice'
        driver['roomX'] = 84
        driver['roomY'] = 200
        self.assertTrue(sim._arrive_at_pair(state, 'ada', now=100.0))
        self.assertFalse(a['visible'])
        self.assertTrue(a['busy'])
        self.assertEqual(a['inRoom'], 'pressoffice')
        self.assertEqual(a['roomX'], 84 + 25, 'navigator sits beside the driver')
        self.assertIsNone(a['path'], 'arrived navigator clears her path')

    def test_pair_arrival_with_missing_other_half_releases_cleanly(self):
        # The session's other half gone (driver released / task vanished) must
        # release the navigator off-duty, never park her holding the session.
        state = self._state()
        state['tasks'] = {}
        a = state['agents']['ada']
        a['pairWith'] = 'ghost'
        a['pairTaskId'] = 'task-9'
        a['path'] = [{'x': 1, 'y': 1}]
        self.assertFalse(sim._arrive_at_pair(state, 'ada', now=100.0))
        self.assertIsNone(a.get('pairWith'))
        self.assertIsNone(a.get('pairTaskId'))
        self.assertTrue(a.get('offDuty'))
        self.assertFalse(a.get('visible'))

    def test_handoff_arrival_delivers_and_clocks_off(self):
        # A handoff walker reaching her recipient delivers the handoff (the
        # finished title as the message) and clocks off -- previously the
        # arrival was dropped and she froze beside the recipient.
        state = self._state()
        a = state['agents']['ada']
        a['handoff'] = {'toId': 'ben', 'title': 'The report', 'fromId': 'ada'}
        a['path'] = [{'x': 1, 'y': 1}]
        a['visible'] = True
        self.assertTrue(sim._arrive_at_handoff(state, 'ada', now=100.0))
        self.assertIsNone(a.get('handoff'))
        self.assertTrue(a.get('offDuty'))
        self.assertFalse(a.get('visible'))
        self.assertEqual(len(state['handoffs']), 1)
        self.assertEqual(state['handoffs'][0]['title'], 'The report')
        self.assertEqual(state['handoffs'][0]['toId'], 'ben')
        self.assertEqual(state['agents']['ben'].get('contactedAt'), 100000)

    def _walking_task_state(self):
        state = {'sim': {'owner': 'server'},
                 'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                    'dir': 'south', 'visible': True, 'busy': False,
                                    'task': None, 'inRoom': None, 'offDuty': False,
                                    'path': [{'x': 1, 'y': 1}], 'pathIndex': 0,
                                    'pathTarget': {'x': 1, 'y': 1},
                                    'stuckTimer': 0, 'replanCount': 0,
                                    'respawnedForTask': False}},
                 'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
                                      'status': 'walking', 'assignedTo': 'ada'}}}
        return state

    def test_tick_dispatches_pair_arrival_to_the_session(self):
        # REGRESSION (W1): a pair session's navigator arriving was DROPPED by
        # the movement dispatch (only ('arrive','task') resolved), so she stood
        # frozen at the door. The tick must route it to _arrive_at_pair.
        state = self._walking_task_state()
        a = state['agents']['ada']
        a['task'] = None
        a['pairWith'] = 'ben'
        a['pairTaskId'] = 'task-1'
        a['path'] = [{'x': 1, 'y': 1}]
        state['tasks']['task-1']['room'] = 'pressoffice'
        state['tasks']['task-1']['status'] = 'working'
        state['agents']['ben'] = {'id': 'ben', 'x': 0, 'y': 0, 'busy': True,
                                  'inRoom': 'pressoffice', 'roomX': 84, 'roomY': 200}
        engine = sim.SimEngine()
        with unittest.mock.patch('sim.step_agent_movement',
                                 return_value=[('arrive', 'pair', 'ada')]):
            out = engine.tick(state, now=100.0)
        a = out['agents']['ada']
        self.assertFalse(a.get('visible'))
        self.assertTrue(a.get('busy'))
        self.assertEqual(a.get('inRoom'), 'pressoffice')
        self.assertEqual(a.get('roomX'), 84 + 25)

    def test_tick_dispatches_handoff_arrival_to_delivery(self):
        # REGRESSION (W1): a handoff walker arriving was DROPPED -- she froze
        # beside the recipient, handoff never delivered. The tick must route it
        # to _arrive_at_handoff.
        state = self._walking_task_state()
        a = state['agents']['ada']
        a['task'] = None
        a['handoff'] = {'toId': 'ben', 'title': 'The report', 'fromId': 'ada'}
        a['path'] = [{'x': 1, 'y': 1}]
        engine = sim.SimEngine()
        with unittest.mock.patch('sim.step_agent_movement',
                                 return_value=[('arrive', 'handoff', 'ada')]):
            out = engine.tick(state, now=100.0)
        a = out['agents']['ada']
        self.assertIsNone(a.get('handoff'))
        self.assertTrue(a.get('offDuty'))
        self.assertFalse(a.get('visible'))
        self.assertEqual(len(out.get('handoffs', [])), 1)
        self.assertEqual(out['handoffs'][0]['title'], 'The report')

    def test_credit_pause_leaves_the_queue_untouched(self):
        state = self._state()
        grid, doors = sim._load_outdoor_geometry()
        item = {'title': 'Paused work', 'room': 'pressoffice',
                'instructions': 'Go.', 'pair': False, 'notBefore': None,
                'priority': sim.WORK_PRIORITY['normal'], 'goal': None,
                'research': None, 'taskType': 'code', 'skillReview': False}
        state['workQueue'] = [item]
        with unittest.mock.patch('serve.credits_pause_active', return_value=True):
            sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                            task_id_holder=[0])
        self.assertEqual(state['workQueue'], [item],
                         'a credit pause must not consume the queued card')
        self.assertNotIn('tasks', state)

    def test_pair_card_recruits_a_navigator_server_side(self):
        # A `pair` card assigned server-side must recruit a navigator (walking
        # her over) instead of being assigned solo -- the driver still goes
        # alone when no second eligible hand exists.
        state = self._state()
        grid, doors = sim._load_outdoor_geometry()
        item = {'title': 'Maintain tooling', 'room': 'pressoffice',
                'instructions': 'Go.', 'pair': True, 'notBefore': None,
                'priority': sim.WORK_PRIORITY['normal'], 'goal': None,
                'research': None, 'taskType': 'code', 'skillReview': False}
        state['workQueue'] = [item]
        with unittest.mock.patch('serve.tier_value_ratios', return_value={}):
            sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                            task_id_holder=[0])
        tasks = [t for t in state['tasks'].values() if t.get('title') == 'Maintain tooling']
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task['status'], 'walking')
        driver = state['agents'][task['assignedTo']]
        self.assertTrue(driver.get('path'))
        nav_id = task.get('pairWith')
        self.assertIsNotNone(nav_id, 'a pair card recruits a navigator')
        self.assertNotEqual(nav_id, task['assignedTo'])
        nav = state['agents'][nav_id]
        self.assertEqual(nav.get('pairWith'), task['assignedTo'])
        self.assertEqual(nav.get('pairTaskId'), task['id'])
        self.assertTrue(nav.get('path'), 'the navigator walks over to pair')

    def test_pair_task_completion_releases_the_navigator(self):
        # When the driver's pair task ships, the navigator must be released
        # back to the pool -- she'd otherwise sit busy at the desk forever.
        state = self._state()
        state['tasks'] = {'task-1': {'id': 'task-1', 'room': 'pressoffice',
                                     'title': 'Pair me', 'entryX': 10, 'entryY': 20,
                                     'pairWith': 'ben'}}
        driver = state['agents']['ada']
        driver['task'] = 'task-1'
        driver['busy'] = True
        nav = state['agents']['ben']
        nav['pairWith'] = 'ada'
        nav['pairTaskId'] = 'task-1'
        nav['busy'] = True
        nav['inRoom'] = 'pressoffice'
        sim.finish_task(state, 'ada')
        self.assertIsNone(nav.get('pairWith'))
        self.assertIsNone(nav.get('pairTaskId'))
        self.assertFalse(nav.get('busy'))
        self.assertIsNone(nav.get('inRoom'))
        self.assertTrue(nav.get('visible'))
