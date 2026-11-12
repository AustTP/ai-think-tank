"""TELEPORT_ASSIGNMENT: task assignment may skip the walking half entirely --
the assigned agent is placed at the room's door front and arrives in the same
bookkeeping a real walk produces. The switch exists because the measured walk
overhead is pathing (median 52s vs ~13s direct-line, p90 234s) and because a
failed walk is the only mechanism in the tank that LOSES work (cancel ->
requeue -> park after 3 distinct failed agents). Teleport makes assignment
failure-by-path unreachable by construction.

The default stays '0' (walk): the walking half is load-bearing for the visible
map and for every ceremony/pair flow built on movement, so teleport is an
operator opt-in, read at call time from .env.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402


def _agent(x, y):
    return {'id': 'ada', 'name': 'Ada', 'x': x, 'y': y, 'dir': 'south',
            'visible': True, 'busy': False, 'task': None, 'inRoom': None,
            'offDuty': False, 'path': None, 'pathIndex': 0, 'pathTarget': None,
            'stuckTimer': 0, 'replanCount': 0}


def _state(ada):
    return {'sim': {'owner': 'server'},
            'agents': {'ada': ada},
            'tasks': {}, 'agentRoster': [], 'workQueue': []}


def _env(teleport):
    return {'TELEPORT_ASSIGNMENT': '1' if teleport else '0'}


class TeleportAssignment(unittest.TestCase):
    def setUp(self):
        # The real outdoor geometry: a spawn position whose path to the room
        # door EXISTS normally -- the teleport tests don't depend on it, but
        # the walk-mode control case does.
        self.grid, self.doors = sim._load_outdoor_geometry()
        self.ada = _agent(sim.SPAWN['x'], sim.SPAWN['y'])

    def _assign(self, teleport):
        with mock.patch.object(serve, '_load_env', return_value=_env(teleport)):
            return sim.assign_task(_state(self.ada), 'ada', 't', 'pressoffice',
                                   'do it', None, {'taskType': 'report'},
                                   self.grid, self.doors, 1_000,
                                   sim._TASK_ID_HOLDER)

    def test_teleport_arrives_in_the_same_bookkeeping(self):
        state = _state(self.ada)
        with mock.patch.object(serve, '_load_env', return_value=_env(True)):
            task = sim.assign_task(state, 'ada', 't', 'pressoffice', 'do it',
                                   None, {'taskType': 'report'},
                                   self.grid, self.doors, 1_000,
                                   sim._TASK_ID_HOLDER)
        self.assertIsNotNone(task)
        ada = state['agents']['ada']
        # the task is WORKING (arrived), not walking
        self.assertEqual(task['status'], 'working')
        self.assertEqual(ada['inRoom'], 'pressoffice')
        self.assertTrue(ada['busy'])
        # desk placement and no transit left behind
        self.assertEqual(ada['path'], None)
        self.assertEqual(ada['pathTarget'], None)
        self.assertEqual(ada['replanCount'], 0)
        # the work budget (or content dispatch) started NOW, not after a walk
        self.assertTrue(task.get('workUntil'))
        # standing at the door front, same spot a real walk ends at
        door = self.doors['pressoffice']
        self.assertAlmostEqual(ada['x'], door['x'] + door['w'] / 2, places=4)

    def test_teleport_succeeds_where_walking_fails(self):
        # The whole point: a pathless map must no longer lose the assignment.
        # Wall the agent into an unwalkable pocket by emptying the grid to a
        # solid block around her -- find_path returns nothing, walking bails,
        # teleport arrives.
        ada = _agent(5000.0, 5000.0)  # off-map: no geometry serves this spot
        with mock.patch.object(serve, '_load_env', return_value=_env(False)):
            walked = sim.assign_task(_state(ada), 'ada', 't', 'pressoffice',
                                     'do it', None, {'taskType': 'report'},
                                     self.grid, self.doors, 1_000,
                                     sim._TASK_ID_HOLDER)
        self.assertIsNone(walked, 'walk mode still fails an unreachable spawn')
        with mock.patch.object(serve, '_load_env', return_value=_env(True)):
            teleported = sim.assign_task(_state(_agent(5000.0, 5000.0)), 'ada',
                                         't', 'pressoffice', 'do it', None,
                                         {'taskType': 'report'},
                                         self.grid, self.doors, 1_000,
                                         sim._TASK_ID_HOLDER)
        self.assertIsNotNone(teleported)
        self.assertEqual(teleported['status'], 'working')

    def test_default_and_missing_env_walk(self):
        # Default '0': absent from .env, blank, or any non-'1' value keeps the
        # walking half. The assignment produces a WALKING task with a real path.
        for env in ({}, {'TELEPORT_ASSIGNMENT': ''},
                    {'TELEPORT_ASSIGNMENT': '0'}, {'TELEPORT_ASSIGNMENT': 'on'}):
            with mock.patch.object(serve, '_load_env', return_value=env):
                state = _state(_agent(sim.SPAWN['x'], sim.SPAWN['y']))
                task = sim.assign_task(state, 'ada', 't', 'pressoffice',
                                       'do it', None, {'taskType': 'report'},
                                       self.grid, self.doors, 1_000,
                                       sim._TASK_ID_HOLDER)
            self.assertIsNotNone(task)
            self.assertEqual(task['status'], 'walking')
            self.assertTrue(state['agents']['ada']['path'],
                            'walk mode issues a real path')

    def test_broken_env_reader_walks(self):
        # A broken env read must never strand assignment: fail safe to walk.
        with mock.patch.object(serve, '_load_env', side_effect=RuntimeError('db')):
            self.assertFalse(sim._teleport_assignment())

    def test_busy_agent_still_refused(self):
        # Teleport does not override the eligibility gate: a busy agent is
        # refused exactly as before.
        self.ada['busy'] = True
        state = _state(self.ada)
        with mock.patch.object(serve, '_load_env', return_value=_env(True)):
            task = sim.assign_task(state, 'ada', 't', 'pressoffice', 'do it',
                                   None, {'taskType': 'report'},
                                   self.grid, self.doors, 1_000,
                                   sim._TASK_ID_HOLDER)
        self.assertIsNone(task)
        self.assertEqual(state.get('tasks') or {}, {})

    def test_unknown_room_still_refused(self):
        with mock.patch.object(serve, '_load_env', return_value=_env(True)):
            task = sim.assign_task(_state(self.ada), 'ada', 't', 'narnia',
                                   'do it', None, {'taskType': 'report'},
                                   self.grid, self.doors, 1_000,
                                   sim._TASK_ID_HOLDER)
        self.assertIsNone(task)


def _village_state(ada, villages):
    state = _state(ada)
    state['villages'] = list(villages)
    return state


class TeleportVillages(unittest.TestCase):
    """Cross-village teleport legs: a task filed for another village first
    teleports the agent to THAT village's access point (access-before-entry),
    then into the room. Room names repeat across villages, so the door is
    looked up in the task village's own geometry."""

    def setUp(self):
        self.grid, self.doors = sim._load_outdoor_geometry()

    def _assign(self, state, extra, teleport=True):
        with mock.patch.object(serve, '_load_env', return_value=_env(teleport)):
            return sim.assign_task(state, 'ada', 't', 'pressoffice', 'do it',
                                   None, extra, self.grid, self.doors, 1_000,
                                   sim._TASK_ID_HOLDER)

    def test_cross_village_assignment_lands_access_then_room(self):
        ada = _agent(sim.SPAWN['x'], sim.SPAWN['y'])
        ada['villageId'] = 'main'
        state = _village_state(ada, [{'id': 'main', 'name': 'Main Village'},
                                     {'id': 'south', 'name': 'South Village'}])
        task = self._assign(state, {'villageId': 'south', 'taskType': 'report'})
        self.assertIsNotNone(task)
        self.assertEqual(task['status'], 'working')
        self.assertEqual(task['villageId'], 'south')
        ada = state['agents']['ada']
        # identity followed the task: she now BELONGS to the south village
        self.assertEqual(ada['villageId'], 'south')
        # and she is inside the room, not stranded at the village gate
        self.assertEqual(ada['inRoom'], 'pressoffice')
        self.assertTrue(ada['busy'])
        # the door front she was placed at is SOUTH's pressoffice door, not
        # main's -- room names repeat, geometry does not.
        sdoor = sim._load_outdoor_geometry('south')[1]['pressoffice']
        self.assertAlmostEqual(ada['x'], sdoor['x'] + sdoor['w'] / 2, places=4)
        self.assertAlmostEqual(ada['y'], sdoor['y'] + sdoor['h'] + 4, places=4)

    def test_same_village_assignment_skips_the_village_leg(self):
        ada = _agent(sim.SPAWN['x'], sim.SPAWN['y'])
        ada['villageId'] = 'south'
        state = _village_state(ada, [{'id': 'main', 'name': 'Main Village'},
                                     {'id': 'south', 'name': 'South Village'}])
        task = self._assign(state, {'villageId': 'south', 'taskType': 'report'})
        self.assertIsNotNone(task)
        self.assertEqual(task['status'], 'working')
        ada = state['agents']['ada']
        self.assertEqual(ada['villageId'], 'south')
        self.assertEqual(ada['inRoom'], 'pressoffice')
        sdoor = sim._load_outdoor_geometry('south')[1]['pressoffice']
        self.assertAlmostEqual(ada['x'], sdoor['x'] + sdoor['w'] / 2, places=4)

    def test_unregistered_village_falls_back_home(self):
        # 'narnia' is not in state['villages']: the access leg is refused, and
        # the assignment still happens in the agent's CURRENT village rather
        # than being lost. Identity never desyncs.
        ada = _agent(sim.SPAWN['x'], sim.SPAWN['y'])
        ada['villageId'] = 'main'
        state = _village_state(ada, [{'id': 'main', 'name': 'Main Village'}])
        task = self._assign(state, {'villageId': 'narnia', 'taskType': 'report'})
        self.assertIsNotNone(task)
        self.assertEqual(task['status'], 'working')
        ada = state['agents']['ada']
        self.assertEqual(ada['villageId'], 'main')
        self.assertEqual(ada['inRoom'], 'pressoffice')
        self.assertAlmostEqual(ada['x'], self.doors['pressoffice']['x']
                               + self.doors['pressoffice']['w'] / 2, places=4)

    def test_task_village_defaults_to_the_agents_village(self):
        # A task with no villageId filed for an agent standing in a satellite
        # village must NOT drag her to main: the room resolves at home.
        ada = _agent(sim.SPAWN['x'], sim.SPAWN['y'])
        ada['villageId'] = 'south'
        state = _village_state(ada, [{'id': 'main', 'name': 'Main Village'},
                                     {'id': 'south', 'name': 'South Village'}])
        task = self._assign(state, {'taskType': 'report'})
        self.assertIsNotNone(task)
        ada = state['agents']['ada']
        self.assertEqual(ada['villageId'], 'south')
        self.assertEqual(ada['inRoom'], 'pressoffice')

    def test_teleport_to_village_primitive(self):
        # The leg-1 primitive is reusable on its own: village -> village and
        # room -> village land at the target's access point.
        ada = _agent(sim.SPAWN['x'], sim.SPAWN['y'])
        ada['villageId'] = 'main'
        state = _village_state(ada, [{'id': 'main', 'name': 'Main Village'},
                                     {'id': 'south', 'name': 'South Village'},
                                     {'id': 'winter', 'name': 'Winter Village'}])
        self.assertTrue(sim._teleport_to_village(state, 'ada', 'south'))
        ada = state['agents']['ada']
        self.assertEqual(ada['villageId'], 'south')
        self.assertIsNone(ada['inRoom'])
        spot = sim._village_arrival_point('south')
        self.assertAlmostEqual(ada['x'], spot['x'], places=4)
        self.assertAlmostEqual(ada['y'], spot['y'], places=4)
        # village -> village: south -> winter
        self.assertTrue(sim._teleport_to_village(state, 'ada', 'winter'))
        self.assertEqual(state['agents']['ada']['villageId'], 'winter')
        self.assertAlmostEqual(state['agents']['ada']['x'],
                               sim._village_arrival_point('winter')['x'], places=4)

    def test_teleport_to_village_refusals(self):
        ada = _agent(sim.SPAWN['x'], sim.SPAWN['y'])
        ada['villageId'] = 'main'
        state = _village_state(ada, [{'id': 'main', 'name': 'Main Village'}])
        # unregistered village: refused, identity untouched
        self.assertFalse(sim._teleport_to_village(state, 'ada', 'narnia'))
        self.assertEqual(state['agents']['ada']['villageId'], 'main')
        # a busy agent is not movable
        ada['busy'] = True
        state = _village_state(ada, [{'id': 'main'}, {'id': 'south'}])
        self.assertFalse(sim._teleport_to_village(state, 'ada', 'south'))
        # unknown agent
        self.assertFalse(sim._teleport_to_village(state, 'ghost', 'south'))

    def test_arrival_points_are_inside_the_map(self):
        # Every constant access point sits inside the world bounds and, for
        # the villages with their own geometry, on walkable ground.
        for village in ('main', 'south', 'winter'):
            spot = sim._village_arrival_point(village)
            self.assertGreaterEqual(spot['x'], 0)
            self.assertGreaterEqual(spot['y'], 0)
            self.assertLessEqual(spot['x'], sim.GROUND_W)
            self.assertLessEqual(spot['y'], sim.GROUND_H)

    def test_unknown_village_arrival_falls_back_to_main(self):
        self.assertEqual(sim._village_arrival_point('narnia'),
                         sim._village_arrival_point('main'))


if __name__ == '__main__':
    unittest.main()
