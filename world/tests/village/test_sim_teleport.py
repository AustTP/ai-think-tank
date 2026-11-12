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


if __name__ == '__main__':
    unittest.main()
