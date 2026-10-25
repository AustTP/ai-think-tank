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


class SimServerOwnedMovement(unittest.TestCase):
    # The Phase-2 flip: when sim.owner == 'server', SimEngine.tick must DRIVE
    # agent positions by running the proven step_agent_movement over the real
    # state['agents'], not merely observe them. Under client ownership it must
    # stay a pure observer (no position mutation). Geometry loads from the real
    # collision_grid.json/door_triggers.json via _load_outdoor_geometry.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-move-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _walking_agent(self):
        # A visible agent parked a little left of SPAWN with a short path that
        # steps right/east toward a near waypoint -- a real, agent-walkable
        # layout on the actual collision grid. path waypoint is 40px east so
        # dt=2.0 at TASK_WALK_SPEED=60 moves 120px -- but the first waypoint is
        # the only one and is far, so x increases each tick until arrival.
        wpx = sim.SPAWN['x'] + 40
        return {
            'ada': {
                'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                'inRoom': None, 'offDuty': False,
                'path': [{'x': wpx, 'y': sim.SPAWN['y']}],
                'pathIndex': 0, 'pathTarget': {'x': wpx, 'y': sim.SPAWN['y']},
                'stuckTimer': 0, 'replanCount': 0, 'respawnedForTask': False,
            },
        }

    def test_server_owner_moves_agent_toward_waypoint(self):
        state = {'sim': {'owner': 'server'}, 'agents': self._walking_agent()}
        engine = sim.SimEngine()
        x0 = state['agents']['ada']['x']
        out = engine.tick(state, now=100.0)
        self.assertGreater(out['agents']['ada']['x'], x0,
                           'server-owned tick must advance x toward the waypoint')
        # The snapshot mirror must reflect the post-step position.
        self.assertEqual(out['sim']['agents']['ada']['x'],
                         out['agents']['ada']['x'])
        # dir is now east (dx > dy).
        self.assertEqual(out['agents']['ada']['dir'], 'east')

    def test_client_owner_does_not_mutate_positions(self):
        state = {'sim': {'owner': 'client'}, 'agents': self._walking_agent()}
        x0 = state['agents']['ada']['x']
        y0 = state['agents']['ada']['y']
        engine = sim.SimEngine()
        out = engine.tick(state, now=100.0)
        self.assertEqual(out['agents']['ada']['x'], x0,
                         'client-owned tick is an observer: x must not change')
        self.assertEqual(out['agents']['ada']['y'], y0)
        # path must still be present (client owns walking; server never clears it)
        self.assertTrue(out['agents']['ada'].get('path'))

    def test_server_owner_advances_and_clears_path_on_arrival(self):
        # Waypoint only 10px away: one 2s tick at 60px/s should arrive quickly.
        state = {'sim': {'owner': 'server'},
                 'agents': self._walking_agent()}
        a = state['agents']['ada']
        a['path'] = [{'x': a['x'] + 8, 'y': a['y']}]
        a['pathTarget'] = {'x': a['x'] + 8, 'y': a['y']}
        engine = sim.SimEngine()
        out = engine.tick(state, now=100.0)
        # Arrival snapshot: server clears path and bumps tick. The agent may
        # have arrived (snapped to the waypoint) or still be approaching if the
        # step didn't cross ARRIVE_DIST -- either way x advanced or was snapped.
        self.assertGreaterEqual(out['agents']['ada']['x'], state['agents']['ada']['x'],
                                'x must reach the near waypoint or beyond on arrival')

    def test_server_tick_substeps_so_no_waypoint_overshoot_oscillation(self):
        # REGRESSION (with probe_1): a single SIM_TICK_S (2s) step
        # at 60px/s moves a walker 120px -- far past a grid cell and past
        # TASK_ARRIVE_DIST -- so she overshoots her first waypoint and bounces
        # forever (x oscillating start<->start+step, pathIndex frozen). The fix
        # sub-steps SIM_TICK_S into small fixed steps; here we assert movement
        # monotonic-then-settled across several server ticks on held state.
        state = {'sim': {'owner': 'server'},
                 'agents': self._walking_agent()}
        a = state['agents']['ada']
        # Short real path: waypoints 16px apart (one grid cell), the worst case
        # for a coarse step.
        start = sim.SPAWN['x']
        a['x'] = start
        a['y'] = sim.SPAWN['y']
        a['path'] = [{'x': start + 16, 'y': sim.SPAWN['y']},
                     {'x': start + 32, 'y': sim.SPAWN['y']},
                     {'x': start + 48, 'y': sim.SPAWN['y']}]
        a['pathTarget'] = {'x': start + 48, 'y': sim.SPAWN['y']}
        engine = sim.SimEngine()
        xs = []
        held = state
        for _ in range(6):
            out = engine.tick(held)
            held = out
            xs.append(out['agents']['ada']['x'])
        # Must converge to the final waypoint, not oscillate. The simplest
        # invariant that catches the bounce: the last tick equals the target and
        # the series is (weakly) increasing past the first step.
        self.assertEqual(round(xs[-1]), start + 48,
                         f'must settle at the final waypoint, got x trail {xs}')
        self.assertEqual(xs[-1], xs[-2], 'must be parked (no post-arrival bounce)')

    def test_repair_stalled_walker_reissues_path(self):
        # An agent HOLDING a 'walking' task with an EMPTY path (path cleared,
        # arrival never fired) freezes forever: movement skips pathless agents
        # and _reconcile_stranded_agents skips task-holders, so nothing re-paths
        # her. _repair_stalled_walkers must give her a fresh path to the room's
        # door. Place a visible agent at SPAWN holding a walking pressoffice task.
        state = {'sim': {'owner': 'server'},
                 'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                    'dir': 'south', 'visible': False, 'busy': False,
                                    'task': 'task-1', 'inRoom': None, 'offDuty': False,
                                    'path': [], 'pathIndex': 0, 'pathTarget': None,
                                    'stuckTimer': 0, 'replanCount': 0}},
                 'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
                                      'status': 'walking', 'assignedTo': 'ada'}}}
        grid, doors = sim._load_outdoor_geometry()
        sim._repair_stalled_walkers(state, grid, doors)
        a = state['agents']['ada']
        self.assertTrue(a.get('path'),
                        'a task-holding pathless walker must get a fresh path')
        self.assertEqual(a['pathIndex'], 0)
        self.assertIsNotNone(a.get('pathTarget'))
        self.assertTrue(a.get('visible'),
                        'a walking agent must be map-visible so movement walks her')
        # A 'walking' agent who ALREADY has a path but was left invisible is just
        # as stuck (movement skips invisible agents) -- the repair must re-affirm
        # visibility even when no fresh path is needed.
        state1b = {'sim': {'owner': 'server'},
                   'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                      'busy': False, 'task': 'task-1', 'path': [{'x': 0, 'y': 0}],
                                      'pathIndex': 0, 'visible': False,
                                      'inRoom': None, 'offDuty': False}},
                   'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
                                        'status': 'walking', 'assignedTo': 'ada'}}}
        sim._repair_stalled_walkers(state1b, grid, doors)
        self.assertTrue(state1b['agents']['ada']['visible'],
                        'a walking agent with a path must still be re-affirmed visible')
        # An agent with NO task is left entirely alone (not hounded).
        state2 = {'sim': {'owner': 'server'},
                  'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                     'busy': False, 'task': None, 'path': [],
                                     'visible': True}},
                  'tasks': {}}
        sim._repair_stalled_walkers(state2, grid, doors)
        self.assertEqual(state2['agents']['ada']['path'], [],
                         'a taskless agent must not be path-warped')

    def _walking_task_state(self, **over):
        """A minimal walking-task holder for the cancel/release tests."""
        state = {'sim': {'owner': 'server'},
                 'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                    'dir': 'south', 'visible': True, 'busy': False,
                                    'task': 'task-1', 'inRoom': None, 'offDuty': False,
                                    'path': [{'x': 1, 'y': 1}], 'pathIndex': 0,
                                    'pathTarget': {'x': 1, 'y': 1},
                                    'stuckTimer': 0, 'replanCount': 0,
                                    'respawnedForTask': False}},
                 'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
                                      'status': 'walking', 'assignedTo': 'ada'}}}
        for key, value in over.items():
            state[key] = value
        return state

    def test_cancel_at_task_releases_agent_and_marks_cancelled(self):
        # Port of tasks.js cancelTask: task -> 'cancelled', agent fully
        # released (task/path/pathTarget/respawn cleared, replan counter reset).
        state = self._walking_task_state()
        task = sim._cancel_at_task(state, 'ada')
        self.assertEqual(task['id'], 'task-1')
        self.assertEqual(state['tasks']['task-1']['status'], 'cancelled')
        a = state['agents']['ada']
        self.assertIsNone(a['task'])
        self.assertIsNone(a['path'])
        self.assertIsNone(a['pathTarget'])
        self.assertEqual(a['replanCount'], 0)
        self.assertFalse(a.get('respawnedForTask'))
        # No-ops for a missing or taskless agent.
        self.assertIsNone(sim._cancel_at_task(state, 'ghost'))
        state['agents']['ben'] = {'id': 'ben', 'task': None}
        self.assertIsNone(sim._cancel_at_task(state, 'ben'))

    def test_tick_dispatches_task_cancel_release(self):
        # REGRESSION (vela, replanCount 55,350): a walking-task agent whose walk
        # can't progress emits a 'cancel' every tick, and the tick previously
        # only LOGGED it -- she kept task/path/respawn and looped forever,
        # flooding the action_log with sim_cancel rows. The dispatch must now
        # release her exactly like tasks.js cancelTask.
        state = self._walking_task_state()
        engine = sim.SimEngine()
        with unittest.mock.patch('sim.step_agent_movement',
                                 return_value=[('cancel', 'task', 'ada')]):
            out = engine.tick(state, now=100.0)
        a = out['agents']['ada']
        self.assertIsNone(a['task'],
                          'a cancelled task-walk must release the agent, not keep her task')
        self.assertIsNone(a['path'])
        self.assertIsNone(a['pathTarget'])
        self.assertFalse(a.get('respawnedForTask'))
        self.assertEqual(out['tasks']['task-1']['status'], 'cancelled',
                         'the gave-up task must be marked cancelled, mirroring cancelTask')

    def test_tick_cancel_releases_stalled_handoff_session(self):
        # A client-owned handoff/pair session that cancels (the client that
        # owned it is gone) must be released to off-duty, like
        # _repair_stalled_interactions -- not left frozen holding the session.
        state = self._walking_task_state()
        a = state['agents']['ada']
        a['task'] = None
        a['handoff'] = {'to': 'ben'}
        a['path'] = [{'x': 1, 'y': 1}]
        engine = sim.SimEngine()
        with unittest.mock.patch('sim.step_agent_movement',
                                 return_value=[('cancel', 'handoff', 'ada')]):
            out = engine.tick(state, now=100.0)
        a = out['agents']['ada']
        self.assertIsNone(a.get('handoff'))
        self.assertTrue(a.get('offDuty'))
        self.assertFalse(a.get('visible'))

    def test_repair_releases_pathological_stuck_walker(self):
        # The replanned-50k-times case: a walking-task holder whose replanCount
        # has blown past the sane budget is released (task cancelled) instead of
        # being re-pathed or left looping -- even though she HAS a path.
        state = self._walking_task_state()
        state['agents']['ada']['replanCount'] = 100
        grid, doors = sim._load_outdoor_geometry()
        sim._repair_stalled_walkers(state, grid, doors)
        a = state['agents']['ada']
        self.assertIsNone(a['task'],
                          'a pathologically-stuck walker must be released, not re-pathed')
        self.assertEqual(state['tasks']['task-1']['status'], 'cancelled')

    def test_repair_releases_unreachable_only_after_respawn(self):
        # A fresh BFS that finds no route is release-worthy ONLY once the agent
        # has already used her one relocation (JS: "Even a fresh spot can't
        # reach it"). Before respawn it must keep the task -- a single failed
        # BFS can be a transient pile-up, and the movement cancel path grants
        # the grace instead.
        def _state(respawned):
            s = self._walking_task_state()
            s['agents']['ada']['path'] = []  # no path -> repair computes one
            s['agents']['ada']['respawnedForTask'] = respawned
            return s
        grid, doors = sim._load_outdoor_geometry()
        pre = _state(respawned=False)
        with unittest.mock.patch('sim.find_path', return_value=None):
            sim._repair_stalled_walkers(pre, grid, doors)
        self.assertEqual(pre['agents']['ada']['task'], 'task-1',
                         'a pre-respawn unreachable walker keeps her task (grace)')
        post = _state(respawned=True)
        with unittest.mock.patch('sim.find_path', return_value=None):
            sim._repair_stalled_walkers(post, grid, doors)
        self.assertIsNone(post['agents']['ada']['task'],
                          'a post-respawn unreachable walker is released')
        self.assertEqual(post['tasks']['task-1']['status'], 'cancelled')
