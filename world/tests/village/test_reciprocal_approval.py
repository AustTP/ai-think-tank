"""Reciprocal-approval detection (2026-11-26, scaling-swarms lesson 7):
counted peer approvals are logged as (reviewer -> author) at the gate, and a
cadence-gated sweep flags mutual approval loops into the governance log.
Detection-only: gate behavior and assignments are never touched.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402

NOW = 1_770_000_000_000


def _approval(reviewer='felix', author='ada', age_ms=0):
    return {'atMs': NOW - age_ms, 'taskId': 'task-x',
            'reviewer': reviewer, 'author': author}


class ApprovalLedger(unittest.TestCase):
    def test_record_event_appends_and_caps(self):
        state = {}
        for i in range(sim._APPROVAL_LOG_MAX + 5):
            sim._note_approval_event(
                state, {'id': 'task-%d' % i, 'assignedTo': 'ada'},
                {}, 'felix', NOW + i)
        ledger = state['approvalLog']
        self.assertEqual(len(ledger), sim._APPROVAL_LOG_MAX)
        self.assertEqual(ledger[0]['taskId'], 'task-5')  # oldest dropped

    def test_author_from_task_override_or_parent_assignee(self):
        state = {}
        sim._note_approval_event(state, {'id': 't1', 'assignedTo': 'ada'},
                                 {}, 'felix', NOW)
        sim._note_approval_event(
            state, {'id': 't2', 'assignedTo': 'ada'},
            {'reviewAuthorId': 'bob'}, 'felix', NOW)
        self.assertEqual(state['approvalLog'][0]['author'], 'ada')
        self.assertEqual(state['approvalLog'][1]['author'], 'bob')

    def test_missing_author_or_reviewer_is_skipped(self):
        state = {}
        sim._note_approval_event(state, {'id': 't'}, {}, 'felix', NOW)
        sim._note_approval_event(state, {'id': 't', 'assignedTo': 'ada'},
                                 {}, None, NOW)
        sim._note_approval_event(state, None, {}, 'felix', NOW)
        self.assertEqual(state.get('approvalLog', []), [])

    def test_clean_gate_approval_is_logged(self):
        parent = {'id': 'p1', 'assignedTo': 'ada', 'deliverable': 'note',
                  'room': 'observatory', 'title': 'parent',
                  '_peerGate': {'enteredMs': 1, 'approvals': 0,
                                'approvers': [], 'reviewedTaskIds': []}}
        state = {'tasks': {'p1': parent}}
        task = {'id': 'r1', 'reviewOf': 'p1', 'assignedTo': 'felix',
                'taskType': 'review', 'createdAt': 200}
        result = {'peerVerdict': 'clean', 'pipelineOk': True}
        with mock.patch.object(sim, '_resolve_review_parent', return_value=parent):
            sim._apply_content_result(state, task, result, now_ms=NOW)
        self.assertEqual(parent['_peerGate']['approvals'], 1)
        self.assertEqual(state['approvalLog'][0]['reviewer'], 'felix')
        self.assertEqual(state['approvalLog'][0]['author'], 'ada')


class ReciprocalApprovalSweep(unittest.TestCase):
    def _state(self, approvals):
        return {'approvalLog': list(approvals), 'lastRecipApprovalSweep': 0,
                '_recipApprovalState': {}, 'agents': {}, 'tasks': {}}

    def test_one_way_approvals_are_not_flagged(self):
        state = self._state([_approval(), _approval(age_ms=1000)])
        with mock.patch('serve.log_action') as log:
            sim._reciprocal_approval_step(state, NOW)
        log.assert_not_called()

    def test_mutual_approvals_are_flagged_once(self):
        state = self._state([
            _approval('felix', 'ada'), _approval('felix', 'ada', 1000),
            _approval('ada', 'felix', 2000), _approval('ada', 'felix', 3000),
        ])
        with mock.patch('serve.log_action') as log:
            sim._reciprocal_approval_step(state, NOW)
        log.assert_called_once()
        name, details = log.call_args[0][1], log.call_args[0][2]
        self.assertEqual(name, 'reciprocal_approval_flagged')
        self.assertEqual(details['aApprovedB'], 2)
        self.assertEqual(details['bApprovedA'], 2)
        key = repr(('ada', 'felix'))
        self.assertIn(key, state['_recipApprovalState'])

    def test_cooldown_and_cadence_block_repeat_flags(self):
        state = self._state([
            _approval('felix', 'ada'), _approval('felix', 'ada', 1000),
            _approval('ada', 'felix', 2000), _approval('ada', 'felix', 3000),
        ])
        with mock.patch('serve.log_action') as log:
            sim._reciprocal_approval_step(state, NOW)
            # Same sweep: cadence gate blocks a second call.
            sim._reciprocal_approval_step(state, NOW + 1000)
            # Cooldown blocks even once the cadence window has passed.
            sim._reciprocal_approval_step(
                state, NOW + sim._APPROVAL_RECIP_SWEEP_MS + 1)
        self.assertEqual(log.call_count, 1)

    def test_old_entries_are_pruned(self):
        state = self._state([_approval(age_ms=sim._APPROVAL_RECIP_WINDOW_MS + 1)])
        sim._reciprocal_approval_step(state, NOW)
        self.assertEqual(state['approvalLog'], [])


if __name__ == '__main__':
    unittest.main()
