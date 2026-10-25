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


class SimTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-test-')
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

    @staticmethod
    def _state():
        return {
            'agents': {
                'ada': {'id': 'ada', 'x': 10, 'y': 20, 'dir': 'right', 'busy': True, 'task': 'check weather', 'inRoom': 'weather-station', 'offDuty': False, 'path': [{'x': 0, 'y': 0}]},
                'ben': {'id': 'ben', 'x': 5, 'y': 5, 'dir': 'down', 'busy': False, 'task': None, 'inRoom': None, 'offDuty': True, 'path': []},
            },
        }

    def test_tick_increments_tick_and_records_epoch(self):
        state = self._state()
        engine = sim.SimEngine()
        out = engine.tick(state, now=1234.0)
        self.assertEqual(out['sim']['tick'], 1)
        self.assertEqual(out['sim']['lastTickEpochS'], 1234.0)
        out = engine.tick(out, now=1236.0)
        self.assertEqual(out['sim']['tick'], 2)
        self.assertEqual(out['sim']['lastTickEpochS'], 1236.0)

    def test_snapshot_mirrors_agent_truth(self):
        engine = sim.SimEngine()
        out = engine.tick(self._state())
        snap = out['sim']['agents']
        self.assertEqual(snap['ada']['x'], 10)
        self.assertEqual(snap['ada']['busy'], True)
        self.assertEqual(snap['ada']['task'], 'check weather')
        self.assertEqual(snap['ada']['pathActive'], True)   # non-empty path
        self.assertEqual(snap['ben']['busy'], False)
        self.assertEqual(snap['ben']['offDuty'], True)
        self.assertEqual(snap['ben']['pathActive'], False)  # empty path

    def test_off_duty_agent_is_never_snapshot_busy(self):
        # A stale busy+offDuty ghost (abandoned firing review) otherwise reads
        # as busy to the client's 1s poll, which re-imposes it and wedges all
        # delegation + hiring with no in-system recovery. The snapshot is the
        # authoritative owner, so it must report such an agent as not busy.
        state = {
            'agents': {
                'faye': {'id': 'faye', 'x': 0, 'y': 0, 'dir': 'south',
                         'busy': True, 'task': None, 'inRoom': None,
                         'offDuty': True, 'path': []},
            },
        }
        engine = sim.SimEngine()
        snap = engine.tick(state)['sim']['agents']
        self.assertEqual(snap['faye']['offDuty'], True)
        self.assertEqual(snap['faye']['busy'], False)  # off duty => not busy

    def test_tick_tolerates_missing_or_malformed_agents(self):
        engine = sim.SimEngine()
        out = engine.tick({'sim': {'tick': 5}}, now=1.0)
        self.assertEqual(out['sim']['tick'], 6)
        self.assertEqual(out['sim']['agents'], {})
        out = engine.tick({'agents': 'not-a-dict'}, now=2.0)
        self.assertEqual(out['sim']['agents'], {})

    def test_status_reflects_state(self):
        engine = sim.SimEngine()
        state = engine.tick(self._state(), now=time.time())
        st = engine.status(state)
        self.assertTrue(st['running'])
        self.assertEqual(st['tick'], 1)
        self.assertIsNotNone(st['lastTickEpochS'])

    def test_loop_pass_round_trips_through_real_db(self):
        serve.save_state_to_db(self._state())
        # Drive off the real serve.get_state_from_db, not the in-memory pass-in.
        sim._sim_loop_pass()
        state = serve.get_state_from_db()
        self.assertIn('sim', state)
        self.assertEqual(state['sim']['tick'], 1)
