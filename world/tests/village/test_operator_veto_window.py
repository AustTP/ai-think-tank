"""Operator veto windows (owner direction 2026-11-11): a repeat-failure
freeze must never hard-block on the operator. The freeze notifies the
operator with a veto option; no veto inside the 12h window processes the
story as planned -- one clean retry, then ship-what-exists."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import agents.repeat_work as repeat_work
import serve
import sim

_VETO_WINDOW_MS = repeat_work._OPERATOR_VETO_WINDOW_MS


def _frozen_parent(state, tid='task-9', now=1_700_000_000_000):
    """Drive the real freeze path once: two identical failures on the same
    story parent (record() counts DISTINCT attempts -- fresh task copies)."""
    state['tasks'] = {tid: {
        'id': tid, 'title': 'Froze twice', 'room': 'pressoffice',
        'status': 'working', 'assignedTo': 'ada'}}
    result = {'ok': False, 'peerSummary': 'Cited artifact is missing '
              'from Library: same-missing-file.md'}
    with unittest.mock.patch.object(serve, '_safe_library_path',
                                    return_value=None), \
         unittest.mock.patch.object(os.path, 'isfile', return_value=False):
        first = repeat_work.record(state, state['tasks'][tid], result, now)
        retry = dict(state['tasks'][tid], id='task-9-retry', reviewOf=tid)
        state['tasks']['task-9-retry'] = retry
        second = repeat_work.record(state, retry, result, now + 60_000)
    return first, second


class FreezeSetsVetoWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-veto-')
        self._cm = unittest.mock.patch.multiple(
            serve, DB_PATH=os.path.join(self.tmp, 't.db'),
            THINK_TANK_DIR=self.tmp, AGENTS_DIR=os.path.join(self.tmp, 'a'),
            LIBRARY_DIR=os.path.join(self.tmp, 'lib'))
        self._cm.start()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_freeze_stamps_deadline_and_notifies_operator(self):
        import time as _time
        state = {'playerInbox': [], 'emailOutbox': []}
        now = int(_time.time() * 1000)
        first, second = _frozen_parent(state, now=now)
        self.assertTrue(first is False or first is True)  # ran
        self.assertTrue(second)
        parent = state['tasks']['task-9']
        self.assertEqual(parent['status'], 'blocked')
        self.assertTrue(parent['manualAttention'])
        self.assertTrue(parent['_peerGate']['escalated'])
        self.assertEqual(parent['_operatorVetoExpiries'], 0)
        self.assertAlmostEqual(parent['_operatorVetoBy'], now + _VETO_WINDOW_MS,
                               delta=61_000)
        # The operator is told the default: no action processes as planned.
        self.assertEqual(len(state['playerInbox']), 1)
        self.assertIn('processes as planned', state['playerInbox'][0]['context'])
        self.assertEqual(len(state['emailOutbox']), 1)
        self.assertIn('operator_veto_window',
                      state['emailOutbox'][0].get('kind', ''))


class VetoPassTests(unittest.TestCase):
    def _frozen(self, now_ms):
        state = {'playerInbox': [], 'emailOutbox': [],
                 'tasks': {'task-9': {
                     'id': 'task-9', 'title': 'Froze twice',
                     'room': 'pressoffice', 'status': 'blocked',
                     'manualAttention': True,
                     'blockedReason': 'unchanged_failure',
                     'failNote': 'recurred',
                     '_peerGate': {'approvals': 0, 'approvers': [],
                                   'reviewerIds': ['leo', 'mark'],
                                   'enteredMs': now_ms - 9_000_000,
                                   'cycleCount': 4, 'escalated': True},
                     '_operatorVetoBy': now_ms,
                     '_operatorVetoExpiries': 0}}}
        return state

    def test_before_deadline_leaves_the_freeze(self):
        now = 1_700_000_000_000
        state = self._frozen(now)
        state['tasks']['task-9']['_operatorVetoBy'] = now + 60_000
        self.assertEqual(sim._operator_veto_pass(state, now), 0)
        self.assertEqual(state['tasks']['task-9']['status'], 'blocked')

    def test_first_expiry_processes_as_planned_retry(self):
        now = 1_700_000_000_000
        state = self._frozen(now)
        with unittest.mock.patch.object(serve, 'log_action') as la:
            self.assertEqual(sim._operator_veto_pass(state, now), 1)
        t = state['tasks']['task-9']
        self.assertEqual(t['status'], 'needs_review')
        self.assertEqual(t['deliveryState'], 'awaiting_review')
        self.assertNotIn('manualAttention', t)
        self.assertNotIn('blockedReason', t)
        self.assertNotIn('failNote', t)
        self.assertNotIn('repeatWorkFailures', t)
        self.assertNotIn('repeatWorkStop', t)
        gate = t['_peerGate']
        self.assertFalse(gate['escalated'])
        self.assertEqual(gate['approvals'], 0)
        self.assertEqual(gate['reviewerIds'], ['leo', 'mark'])
        self.assertEqual(gate['enteredMs'], now)
        self.assertEqual(t['_operatorVetoExpiries'], 1)
        self.assertIn('operator_veto_window_expired',
                      [c[0][1] for c in la.call_args_list])

    def test_second_expiry_ships_what_exists(self):
        now = 1_700_000_000_000
        state = self._frozen(now)
        state['tasks']['task-9']['_operatorVetoExpiries'] = 1
        with unittest.mock.patch.object(sim, '_close_gated_story') as close, \
             unittest.mock.patch.object(serve, 'log_action') as la:
            self.assertEqual(sim._operator_veto_pass(state, now), 1)
        close.assert_called_once()
        self.assertTrue(state['tasks']['task-9']['_peerGate']['escalated'])
        actions = [c[0][1] for c in la.call_args_list]
        self.assertIn('task_auto_closed', actions)

    def test_explicit_veto_keeps_the_freeze(self):
        now = 1_700_000_000_000
        state = self._frozen(now)
        state['tasks']['task-9']['_operatorVetoed'] = True
        self.assertEqual(sim._operator_veto_pass(state, now + 1), 0)
        self.assertEqual(state['tasks']['task-9']['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
