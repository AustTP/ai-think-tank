"""Coverage-push tests for world/sim.py residual branches: the peer-review
gate, firing-signal, morale, author-notification, completed-room, and
stuck-gate roster-top-up helpers. Uses the temp-DB isolation pattern from
test_sim_gap.py so the real think_tank.db is never touched.
"""
import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-simgapC-')
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
        sim._content_results.clear()


class PeerGateMoraleGap(SimIsolation):
    def test_morale_for_missing_agent_returns_none(self):
        self.assertIsNone(sim.morale_for({'agents': {}}, 'ghost'))

    def test_has_firing_signal_missing_agent(self):
        self.assertFalse(sim._has_firing_signal({'agents': {}}, 'ghost'))

    def test_firing_consultation_skips_ghost_roster(self):
        state = {'agents': {'ada': {'id': 'ada'}},
                 'agentRoster': [{'id': 'ada'}, {'id': 'ghost'}]}
        info = sim._firing_consultation(state, 'ada')
        self.assertEqual(info, {'reporters': [], 'coworkers': []})

    def test_consultation_blocks_firing_single_serious_defers(self):
        state = {'agents': {},
                 'agentRoster': [],
                 'reports': [{'aboutId': 'ada', 'fromId': 'ben',
                              'severity': 'serious', 'quote': 'q', 'note': 'n'}]}
        blocked, info = sim._consultation_blocks_firing(state, 'ada', now_ms=1)
        self.assertTrue(blocked)
        self.assertEqual(len(info['reporters']), 1)

    def test_enter_peer_review_no_reviewers_returns_none(self):
        task = {'id': 'task-1', 'assignedTo': 'ada', 'room': 'pressoffice',
                'title': 't'}
        with unittest.mock.patch.object(sim, '_pick_reviewer_ids', return_value=[]):
            self.assertIsNone(sim._enter_peer_review({}, task, now_ms=1000))

    def test_cascade_rereview_no_reopened_task(self):
        self.assertEqual(sim._cascade_rereview({'tasks': {}}, None), 0)

    def test_cascade_rereview_skips_escalated_dependent(self):
        state = {'tasks': {'task-2': {'id': 'task-2', 'title': 'dep',
                                      'room': 'pressoffice', 'taskType': 'code',
                                      'status': 'done', 'dependsOn': 'task-1',
                                      '_peerGate': {'escalated': True}}}}
        self.assertEqual(sim._cascade_rereview(state, 'task-1'), 0)

    def test_resolve_review_parent_no_review_of(self):
        self.assertIsNone(sim._resolve_review_parent({'tasks': {}},
                                                     {'reviewOf': None}))

    def test_resolve_review_parent_missing_parent(self):
        self.assertIsNone(sim._resolve_review_parent({'tasks': {}},
                                                     {'reviewOf': 'task-1'}))

    def test_resolve_review_parent_no_gate(self):
        state = {'tasks': {'task-1': {'id': 'task-1', 'title': 't'}}}
        self.assertIsNone(sim._resolve_review_parent(state,
                                                     {'reviewOf': 'task-1'}))

    def test_sim_notify_author_no_author_returns(self):
        self.assertIsNone(sim._sim_notify_author({'agents': {}},
                                                 {'id': 't', 'title': 'T'}, 'ben'))

    def test_sim_notify_author_failed_no_author_returns(self):
        self.assertIsNone(sim._sim_notify_author_failed({'agents': {}},
                                                        {'id': 't', 'title': 'T'}))

    def test_note_completed_room_no_room(self):
        sim._note_completed_room({'agents': {}}, 'ada', {'id': 't'})
        sim._note_completed_room({'agents': {}}, 'ada', None)

    def test_note_completed_room_missing_agent(self):
        sim._note_completed_room({'agents': {}}, 'ada',
                                 {'id': 't', 'room': 'pressoffice'})

    def test_sweep_stuck_gates_roster_top_up_reachable(self):
        now_ms = 40 * 60 * 1000
        state = {
            'agents': {
                'ava': {'id': 'ava', 'busy': False, 'task': None},
                'zed': {'id': 'zed', 'busy': False, 'task': None},
                'dan': {'id': 'dan', 'busy': False, 'task': None},
            },
            'agentRoster': [
                {'id': 'maya', 'name': 'Maya', 'isAdmin': True},
                {'id': 'ava', 'name': 'Ava'},
                {'id': 'cora', 'name': 'Cora'},
                {'id': 'dax', 'name': 'Dax'},
                {'id': 'zed', 'name': 'Zed'},
                {'id': 'dan', 'name': 'Dan'},
            ],
            'tasks': {'task-1': {'id': 'task-1', 'title': 't',
                                 'room': 'pressoffice', 'assignedTo': 'ava',
                                 'status': 'needs_review',
                                 '_peerGate': {'approvals': 0, 'approvers': [],
                                               'reviewerIds': ['cora', 'dax'],
                                               'enteredMs': 0}}},
            'workQueue': [],
        }
        with unittest.mock.patch.object(sim, '_pick_reviewer_ids',
                                        return_value=['zed']):
            sim._sweep_stuck_gates(state, now_ms)
        gate = state['tasks']['task-1']['_peerGate']
        self.assertEqual(gate['reviewerIds'], ['zed', 'dan'])
        self.assertEqual(gate['stuckRescueTs'], now_ms)


if __name__ == '__main__':
    unittest.main()