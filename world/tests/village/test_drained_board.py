"""A drained board must stay empty: automatic card factories are suppressed in
drain mode, idle supervision does not mint routine cards, exhausted blocked
stories terminalize, and completion requires saved evidence."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import agents.completion_watchdog as watchdog  # noqa: E402
import sim  # noqa: E402


def _state(mode='drain'):
    return {
        'automation': {'mode': mode},
        'agents': {
            'admin': {'id': 'admin', 'name': 'Admin', 'busy': False, 'offDuty': False},
            'dir': {'id': 'dir', 'name': 'Dir', 'busy': False, 'offDuty': False},
        },
        'agentRoster': [
            {'id': 'admin', 'name': 'Admin', 'isAdmin': True},
            {'id': 'dir', 'name': 'Dir'},
        ],
        'teams': [{'id': 'team', 'name': 'Team', 'directorId': 'dir',
                   'members': ['dir']}],
        'tasks': {},
        'workQueue': [],
        'issues': {},
    }


class DrainedBoard(unittest.TestCase):
    def test_drain_mode_never_queues_supervisor_cards(self):
        st = _state('drain')
        for i in range(5):
            sim._supervisor_brief_step(st, 1000.0 + i, 1_000_000 + i)
        self.assertEqual(st['workQueue'], [])
        self.assertEqual(st['tasks'], {})

    def test_idle_normal_mode_never_queues_routine_card(self):
        st = _state('normal')
        for i in range(5):
            sim._supervisor_brief_step(st, 1000.0 + i, 1_000_000 + i)
        self.assertEqual(st['workQueue'], [])
        self.assertEqual(st['tasks'], {})

    def test_empty_board_stays_empty_for_n_watchdog_ticks(self):
        st = {'tasks': {}, 'workQueue': [], 'completionControl': {'enabled': True}}
        for i in range(5):
            watchdog.step(st, 1000 + i, observe_only=False)
        self.assertEqual(st['tasks'], {})
        self.assertEqual(st['workQueue'], [])

    def test_avatar_queue_skips_in_drain_mode(self):
        import serve
        st = _state('drain')
        with mock.patch.object(serve, '_state_begin', return_value=st), \
                mock.patch.object(serve, '_pixellab_configured', return_value=True), \
                mock.patch.object(serve, '_think_tank_spend_cap_exceeded', return_value=False), \
                mock.patch.object(serve, '_avatar_generation_available', return_value=True):
            self.assertEqual(serve._queue_avatar_tasks(), 0)
        self.assertEqual(st['workQueue'], [])


class ExhaustedBlock(unittest.TestCase):
    def test_blocked_recovery_task_terminalizes(self):
        st = {
            'tasks': {'p': {
                'id': 'p', 'title': 'Story', 'status': 'blocked',
                'blockedReason': 'completion_recovery', 'blockedAt': 1000,
                'completionRecovery': {'attempts': 3}, 'assignedTo': 'ada',
            }},
            'agents': {'ada': {}},
            'agentRoster': [{'id': 'ada'}],
            'teams': [], 'workQueue': [],
            'completionControl': {'enabled': True, 'maxRecoveryAttempts': 3},
        }
        watchdog.step(st, 2_000, observe_only=False)
        task = st['tasks']['p']
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual(task['statusReason'], 'recovery_exhausted')


class CompletionEvidence(unittest.TestCase):
    def test_code_card_needs_a_saved_artifact(self):
        self.assertFalse(sim._completion_has_evidence({}, {
            'taskType': 'code', 'room': 'pressoffice'}))
        self.assertTrue(sim._completion_has_evidence({}, {
            'taskType': 'code', 'room': 'pressoffice',
            'libraryPath': 'library/x.md'}))

    def test_bug_card_accepts_a_validated_publish(self):
        # A bug fix that shipped a real staged change is the deliverable; the
        # missing Library record must not refuse it (task-374/377 chain).
        self.assertTrue(sim._completion_has_evidence({}, {
            'taskType': 'bug', 'room': 'pressoffice', 'codePublished': True}))

    def test_distill_card_accepts_the_wiki_write(self):
        # A distill records wikiPage on the task; requiring only task.note
        # refused a successful wiki merge (task-395).
        self.assertTrue(sim._completion_has_evidence({}, {
            'taskType': 'code', 'distill': True,
            'wikiPage': 'state-of-knowledge'}))

    def test_zero_grade_is_refused(self):
        self.assertFalse(sim._completion_has_evidence({}, {
            'taskType': 'code', 'room': 'pressoffice',
            'libraryPath': 'library/x.md', 'grade': 0.0}))

    def test_supervise_card_needs_no_library_artifact(self):
        self.assertTrue(sim._completion_has_evidence({}, {
            'taskType': 'supervise'}))

    def test_peer_review_is_exempt_but_repair_still_needs_artifact(self):
        self.assertTrue(sim._completion_has_evidence({}, {
            'taskType': 'review', 'reviewOf': 'task-1',
            'note': 'Approved after checking the diff.'}))
        self.assertFalse(sim._completion_has_evidence({}, {
            'taskType': 'spike', 'reviewOf': 'task-1',
            'note': 'NOT COMPLETED. No briefing was produced.'}))
        self.assertTrue(sim._completion_has_evidence({}, {
            'taskType': 'spike', 'reviewOf': 'task-1',
            'reportPath': 'x-articles/ACTION-BRIEFING.md',
            'note': 'Saved the briefing.'}))

    def test_finish_task_refuses_empty_deliverable(self):
        st = {
            'tasks': {'t': {'id': 't', 'title': 'x', 'status': 'working',
                            'taskType': 'code', 'assignedTo': 'a'}},
            'agents': {'a': {'id': 'a', 'task': 't', 'busy': True,
                             'inRoom': 'pressoffice'}},
            'agentRoster': [{'id': 'a'}],
        }
        sim.finish_task(st, 'a')
        self.assertEqual(st['tasks']['t']['status'], 'failed')
        self.assertIsNone(st['agents']['a']['task'])
        self.assertFalse(st['agents']['a']['busy'])


if __name__ == '__main__':
    unittest.main()
