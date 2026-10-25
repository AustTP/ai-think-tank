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

import config
import serve
import sim


class SimOffDutyWake(unittest.TestCase):
    # Redesign: agents VANISH WHERE THEY STAND when
    # going off-duty (no trek to an outskirts door), and WAKE ANYWHERE there's
    # free, reachable space (not at outskirts_east/south). This fixes agents
    # stranding at the map edge (ben at outskirts_south, faye at the corner).

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-offduty-')
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

    def _agent_state(self, **over):
        base = {'id': 'ben', 'x': 500, 'y': 400, 'dir': 'right', 'busy': False,
                'task': None, 'inRoom': None, 'offDuty': False, 'visible': True,
                'path': None, 'pathIndex': 0, 'pathTarget': None,
                'headingOffDuty': None, 'pairWith': None, 'handoff': None,
                'stuckTimer': 0, 'replanCount': 0}
        base.update(over)
        return {'agents': {base['id']: base}}

    def test_send_off_duty_vanishes_in_place_no_outskirts_walk(self):
        # An idle agent goes off-duty where they stand: no path is set, they flip
        # offDuty+invisible, and their position is left untouched (no outreach to
        # an edge door, which is what stranded agents in reachability gaps).
        state = self._agent_state(x=500, y=400)
        grid, doors = sim._load_outdoor_geometry()
        sim.send_agent_off_duty(state, 'ben', doors, grid)
        a = state['agents']['ben']
        self.assertIs(a['offDuty'], True)
        self.assertIs(a['visible'], False)
        self.assertIsNone(a['path'])
        self.assertIsNone(a['headingOffDuty'])
        # Position unchanged -- vanished in place, didn't teleport to outskirts.
        self.assertEqual((a['x'], a['y']), (500, 400))

    def test_send_off_duty_refuses_busy_agent(self):
        state = self._agent_state(busy=True, task='t1')
        grid, doors = sim._load_outdoor_geometry()
        sim.send_agent_off_duty(state, 'ben', doors, grid)
        a = state['agents']['ben']
        self.assertIs(a['offDuty'], False, 'a busy agent must not vanish')
        self.assertIs(a['visible'], True)

    def test_appear_from_outskirts_places_at_free_reachable_spot(self):
        # Waking agent appears ANYWHERE free + reachable from SPAWN -- never at
        # a trailhead edge door (the outskirts rooms are gone). Verify the
        # landed spot is (a) not on collision and (b) reachable.
        state = self._agent_state(y=600, offDuty=True, visible=False)
        grid, doors = sim._load_outdoor_geometry()
        sim.appear_from_outskirts(state, 'ben', doors, grid=grid)
        a = state['agents']['ben']
        self.assertIs(a['offDuty'], False)
        self.assertIs(a['visible'], True)
        mask = sim.compute_reachable_mask(grid, sim.SPAWN)
        cx, cy = a['x'] + sim.AGENT_W / 2, a['y'] + sim.AGENT_H / 2
        self.assertTrue(sim.is_reachable(cx, cy, grid, mask),
                        f'woken agent landed at an UNREACHABLE cell ({a["x"]},{a["y"]})')
        # The outskirts doors are gone from the trigger set altogether.
        self.assertNotIn('outskirts_east', doors)
        self.assertNotIn('outskirts_south', doors)

    def test_off_duty_wake_roundtrip(self):
        # Off duty then wake: the agent is visible again somewhere free.
        grid, doors = sim._load_outdoor_geometry()
        state = self._agent_state(x=500, y=400)
        sim.send_agent_off_duty(state, 'ben', doors, grid)
        self.assertTrue(state['agents']['ben']['offDuty'])
        sim.appear_from_outskirts(state, 'ben', doors, grid=grid)
        a = state['agents']['ben']
        self.assertFalse(a['offDuty'])
        self.assertTrue(a['visible'])

    def test_wake_clears_stale_busy_task_on_off_duty_ghost(self):
        # Regression (faye live-bug): an off-duty agent can carry a stale
        # busy/task from when she clocked off. Waking her must clear those so she
        # is both visible AND eligible for assignment (not visible-but-ineligible,
        # which re-strands her).
        grid, doors = sim._load_outdoor_geometry()
        state = self._agent_state(x=0, y=0, busy=True, task='stale-t',
                                  offDuty=True, visible=False)
        sim.appear_from_outskirts(state, 'ben', doors, grid=grid)
        a = state['agents']['ben']
        self.assertIs(a['offDuty'], False)
        self.assertIs(a['visible'], True)
        self.assertIs(a['busy'], False, 'woken agent must not still read as busy')
        self.assertIsNone(a['task'])

    def test_reconcile_moves_stranded_visible_agent_to_reachable_spot(self):
        # An idle VISIBLE agent parked on an unreachable cell (the reachability-
        # gap bug that left ben/faye at the outskirts) gets re-picked to a free,
        # reachable spot; a reachable one is left alone.
        grid, _ = sim._load_outdoor_geometry()
        # Place the agent hard at the world origin (0,0) -- off-grid/corner.
        state = self._agent_state(x=0, y=0)
        moved = sim._reconcile_stranded_agents(state, grid)
        self.assertEqual(moved, 1)
        a = state['agents']['ben']
        mask = sim.compute_reachable_mask(grid, sim.SPAWN)
        self.assertTrue(sim.is_reachable(a['x'] + sim.AGENT_W / 2, a['y'] + sim.AGENT_H / 2, grid, mask))

    def test_reconcile_leaves_reachable_agent_and_midtask_alone(self):
        grid, _ = sim._load_outdoor_geometry()
        # An agent at SPAWN is reachable -> untouched; a busy one is untouched.
        state = self._agent_state(x=sim.SPAWN['x'], y=sim.SPAWN['y'])
        state['agents']['busy2'] = {'id': 'busy2', 'x': 0, 'y': 0, 'visible': True,
                                    'task': 't1', 'busy': True}
        moved = sim._reconcile_stranded_agents(state, grid)
        self.assertEqual(moved, 0)
        self.assertEqual((state['agents']['ben']['x'], state['agents']['ben']['y']),
                         (sim.SPAWN['x'], sim.SPAWN['y']))
