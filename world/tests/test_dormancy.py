"""Tests for sleep-not-die idle dormancy.

Previously the idle watcher (`--max-idle-minutes`) EXITED the server, which
left nothing port-bound to hear a remote wake request -- so resuming the
think tank required being physically at a computer. Sleep-not-die changes the
action: the process stays up and keeps the port, but the simulation (movement,
task cycle, content executors, and every model/OpenRouter spend) is skipped
while DORMANT. ANY request flips it back awake instantly (zero infra, identical
on a Mac or a headless VPS).

Invariants pinned here:
1. `_set_dormant(True)`/`_set_dormant(False)` toggle the flag and return the new
   value; `_dormant()` reads it.
2. While dormant, `_sim_loop_pass` does NOT run the engine tick (no DB read/write,
   no movement, no spend) yet still does the cheap `_expire_handles` housekeeping.
3. Waking clears the flag so the next pass resumes a normal tick.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import sim  # noqa: E402


class DormancyFlag(unittest.TestCase):
    def setUp(self):
        serve._set_dormant(False)

    def test_starts_not_dormant(self):
        self.assertFalse(serve._dormant())

    def test_sets_and_reads_true(self):
        self.assertTrue(serve._set_dormant(True))
        self.assertTrue(serve._dormant())

    def test_clears_to_false(self):
        serve._set_dormant(True)
        self.assertFalse(serve._set_dormant(False))
        self.assertFalse(serve._dormant())


class DormancyGatesSim(unittest.TestCase):
    @mock.patch.object(serve, 'get_state_from_db', return_value={'sim': {'owner': 'server'}})
    @mock.patch.object(serve, 'save_state_to_db')
    @mock.patch.object(sim._engine, 'tick')
    def test_while_dormant_tick_is_skipped_and_state_untouched(self, mock_tick, mock_save, mock_get):
        serve._set_dormant(True)
        result = sim._sim_loop_pass()
        self.assertIsNone(result, 'dormant pass returns None: nothing to do')
        mock_get.assert_not_called()
        mock_save.assert_not_called()
        mock_tick.assert_not_called()

    @mock.patch.object(serve, 'get_state_from_db', return_value={'sim': {'owner': 'server'}})
    @mock.patch.object(serve, 'save_state_to_db')
    @mock.patch.object(sim._engine, 'tick', return_value={'sim': {}})
    def test_waking_resumes_normal_tick(self, mock_tick, mock_save, mock_get):
        serve._set_dormant(False)  # woken by a request
        sim._sim_loop_pass()
        mock_tick.assert_called_once()
        mock_save.assert_called_once()

    @mock.patch.object(serve, '_expire_handles')
    @mock.patch.object(serve, 'get_state_from_db', return_value=None)
    def test_dormancy_still_runs_expire_handles_housekeeping(self, mock_get, mock_expire):
        # The security invariant (stale capability handles must be dropped even
        # while paused) keeps running, independent of the sim gate.
        serve._set_dormant(True)
        sim._sim_loop_pass()
        mock_expire.assert_called_once()

    def tearDown(self):
        serve._set_dormant(False)


if __name__ == '__main__':
    unittest.main()