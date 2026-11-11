"""The door-mouth dispersal sweep must actually clear a standing clot.

2026-11-11 diagnosis: 27 visible agents (most fully idle by every mirrored
flag) stood packed in the pressoffice approach column for hours while
_disperse_door_mouth_loiterers ran every pass -- the submit-the-change-request
card churned through ~20 reissues because every assignee's walk stalled behind
the bodies. These tests pin the sweep's contract on a clot shaped like the
live one: stamp on first sight, park after the dwell gate, bounded per pass,
converged after a few sweeps.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402

# world-space door rect for pressoffice (door_triggers.json x2, as the sim sees it)
PRESSOFFICE_DOOR = {'x': 484.0, 'y': 220.0, 'w': 62.0, 'h': 62.0}


def _clot_state(n=27, y0=288.0, dy=10.0):
    """A clot of n visible, fully-idle agents stacked down the approach
    column below the pressoffice door mouth. The live 2026-11-11 clot held
    27 agents between y=288 and y=368 (eight deep at y=368); this builder
    packs them the same way, every one inside the sweep's 90px radius."""
    roster = [{'id': 'a%d' % i} for i in range(n)]
    agents = {}
    for i in range(n):
        agents['a%d' % i] = {
            'x': 510.0, 'y': min(y0 + dy * i, 368.0), 'visible': True,
            'busy': False, 'pathActive': False, 'inRoom': None,
            'task': None, 'offDuty': False, 'path': None,
            'pairWith': None, 'handoff': None,
        }
    return {'agentRoster': roster, 'agents': agents}


class DoorMouthDispersal(unittest.TestCase):
    def _run_sweeps(self, state, now_ms, count, step_ms=60_000):
        out = []
        for i in range(count):
            out.append(sim._disperse_door_mouth_loiterers(
                state, {'pressoffice': PRESSOFFICE_DOOR},
                now_ms + i * step_ms))
        return out

    def test_first_sweep_stamps_without_parking(self):
        state = _clot_state()
        parked = self._run_sweeps(state, 1_000_000, 1)
        self.assertEqual(parked, [0],
                         'the first sweep only stamps the loiter clock')
        visible = [a for a in state['agents'].values() if a.get('visible')]
        self.assertEqual(len(visible), 27)
        self.assertTrue(all(a.get('_loiterSinceMs') == 1_000_000
                            for a in state['agents'].values()))

    def test_clot_converges_within_the_dwell_gate(self):
        state = _clot_state()
        t0 = 1_000_000
        self._run_sweeps(state, t0, 1)                       # stamp
        # 16 minutes later: parked in bounded batches of 8 until clear.
        t = t0 + 16 * 60_000
        parked = []
        for i in range(6):
            parked.append(sim._disperse_door_mouth_loiterers(
                state, {'pressoffice': PRESSOFFICE_DOOR}, t + i * 60_000))
        self.assertGreater(sum(parked), 0, 'a standing clot must be parked')
        visible = [a for a in state['agents'].values() if a.get('visible')]
        self.assertEqual(
            len(visible), 0,
            'after the dwell gate + bounded sweeps the mouth is clear')


class DoorBandReconcile(unittest.TestCase):
    """The geometry healer: idle bodies in a door approach band get relocated
    regardless of any hidden flag that freezes the idle sweeps (_suspendedTask,
    a stale path, a stale inRoom -- none mirrored in any snapshot, so a statue
    with one is unparkable by every existing sweep)."""

    DOORS = {'pressoffice': PRESSOFFICE_DOOR}

    def setUp(self):
        self.grid = sim._load_outdoor_geometry('main')[0]

    def test_idle_statue_in_band_is_relocated(self):
        state = _clot_state(n=1)
        state['agents']['a0']['_suspendedTask'] = {'task': 'task-1'}
        moved = sim._reconcile_door_band_idles(state, self.DOORS, self.grid)
        self.assertEqual(moved, 1)
        a = state['agents']['a0']
        self.assertIsNone(a['path'])
        self.assertEqual(a['pathIndex'], 0)
        bands = sim._door_band_boxes(self.DOORS)
        self.assertIsNone(sim._in_door_band(a['x'], a['y'], bands),
                          'the relocated body no longer corks any band')

    def test_walkers_and_task_holders_are_left_alone(self):
        state = _clot_state(n=3)
        state['agents']['a0']['task'] = 'task-9'
        state['agents']['a1']['pathActive'] = True
        moved = sim._reconcile_door_band_idles(state, self.DOORS, self.grid)
        self.assertEqual(moved, 1, 'only the plain idle body moves')
        self.assertEqual(state['agents']['a0']['x'], 510.0)
        self.assertEqual(state['agents']['a1']['x'], 510.0)

    def test_bounded_per_pass(self):
        state = _clot_state(n=27)
        moved = sim._reconcile_door_band_idles(state, self.DOORS, self.grid)
        self.assertLessEqual(
            moved, sim._DOOR_BAND_CLEAR_PER_PASS,
            'one sweep does bounded work so the tick stays cheap')


if __name__ == '__main__':
    unittest.main()
