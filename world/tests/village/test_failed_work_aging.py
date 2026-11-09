"""Failed-work aging: a card stranded in 'failed' auto re-issues after a grace
window instead of sticking forever (2026-11-08: the dankornas X-post verdict
cards stranded a day when OpenRouter credits ran out mid-run -- the stale
in-flight sweep covered walking/working only, failed cards had no path)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402


def _state(failed_task):
    return {
        'sim': {'owner': 'server'},
        'agents': {},
        'tasks': {failed_task['id']: failed_task},
        'workQueue': [],
        'agentRoster': [],
        'lastStaleWorkSweep': 0,
    }


def _failed_card(failed_at_offset_ms=-3 * 3600 * 1000):
    return {
        'id': 'task-x',
        'title': 'Verdict on the X-post delta report',
        'status': 'failed',
        'taskType': 'code',
        'room': 'workroom',
        'instructions': 'Read the report and file a verdict.',
        'failNote': 'OpenRouter credits are exhausted.',
        'failedAt': 1_700_000_000_000 + failed_at_offset_ms,
        'createdAt': 1_700_000_000_000,
    }


class FailedWorkAging(unittest.TestCase):
    def _run(self, state, now_ms=1_700_000_000_000 + 4 * 3600 * 1000):
        return sim._stale_work_step(state, now_ms / 1000.0, now_ms)

    def test_failed_card_past_grace_is_reissued_with_failure_note(self):
        state = _state(_failed_card())
        with mock.patch.object(sim, 'queue_work') as qw, \
                mock.patch.object(sim, '_log_governance'):
            replanned = self._run(state)
        self.assertEqual(replanned, 1)
        self.assertEqual(qw.call_count, 1)
        item = qw.call_args[0][1][0]
        self.assertEqual(item['reissueOf'], 'task-x')
        self.assertIn('FAILED earlier', item['instructions'])
        self.assertIn('OpenRouter credits are exhausted', item['instructions'])
        self.assertNotIn('task-x', state['tasks'])

    def test_failed_card_within_grace_is_untouched(self):
        # failed 30 minutes before the sweep's now (base + 4h)
        state = _state(_failed_card(failed_at_offset_ms=3.5 * 3600 * 1000))
        with mock.patch.object(sim, 'queue_work') as qw:
            replanned = self._run(state)
        self.assertEqual(replanned, 0)
        self.assertEqual(qw.call_count, 0)
        self.assertIn('task-x', state['tasks'])

    def test_repeat_offender_routes_to_scrum_master(self):
        card = _failed_card()
        state = _state(card)
        key = repr((card['title'], card['room']))
        state['_staleWorkReplans'] = {key: sim.STALE_WORK_MAX_REPLANS}
        with mock.patch.object(sim, '_sm_replan_stale_work', return_value=True) as sm, \
                mock.patch.object(sim, 'queue_work') as qw, \
                mock.patch.object(sim, '_log_governance'):
            replanned = self._run(state)
        self.assertEqual(replanned, 1)
        self.assertEqual(sm.call_count, 1)
        self.assertEqual(qw.call_count, 0)
        self.assertNotIn('task-x', state['tasks'])

    def test_bugs_and_review_subtasks_keep_their_own_machinery(self):
        card = _failed_card()
        card['reviewOf'] = 'task-parent'
        state = _state(card)
        with mock.patch.object(sim, 'queue_work') as qw:
            replanned = self._run(state)
        self.assertEqual(replanned, 0)
        self.assertEqual(qw.call_count, 0)
        self.assertIn('task-x', state['tasks'])


if __name__ == '__main__':
    unittest.main()


class ContentResidueSweep(unittest.TestCase):
    """Restart-orphaned content flags on non-working cards are dead and must
    be swept, or the dispatch governor's count stays poisoned."""

    def test_residue_cleared_off_non_working_tasks(self):
        from sim import _clear_content_residue
        state = {'tasks': {
            'task-a': {'status': 'queued', '_contentInFlight': True,
                       '_contentGraceUsed': 60.0},
            'task-b': {'status': 'blocked', '_contentInFlight': True},
            'task-c': {'status': 'working', '_contentInFlight': True},
            'task-d': {'status': 'done'},
        }}
        cleaned = _clear_content_residue(state)
        self.assertEqual(cleaned, 2)
        self.assertNotIn('_contentInFlight', state['tasks']['task-a'])
        self.assertNotIn('_contentGraceUsed', state['tasks']['task-a'])
        self.assertNotIn('_contentInFlight', state['tasks']['task-b'])
        self.assertTrue(state['tasks']['task-c']['_contentInFlight'],
                        'a working task keeps its live-run flags')
        self.assertEqual(cleaned, 0) if False else None
        self.assertEqual(_clear_content_residue(state), 0,
                         'a second pass finds nothing left to clean')
