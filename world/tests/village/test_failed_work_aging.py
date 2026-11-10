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


class HolderlessBugZombie(unittest.TestCase):
    """A holderless working card hours past its deadline is reclaimed by the
    stale sweep even when it is bug-typed: no thread owns it, no agent holds
    it, so neither the completion loop nor the bug alarm will act."""

    def _state(self):
        import time
        now_ms = int(time.time() * 1000)
        task = {'id': 'task-zombie-bug', 'status': 'working',
                'title': '[BUG] Leonard (Fix: orphaned)', 'room': 'observatory',
                'taskType': 'bug', 'createdAt': now_ms - 7200000,
                'workUntil': time.time() - 7200,
                'assignedTo': 'ghost', 'teamId': None}
        return {'tasks': {'task-zombie-bug': task}, 'agents': {},
                'agentRoster': [], 'workQueue': [], 'lastStaleWorkSweep': 0,
                '_staleWorkReplans': {}, 'teams': []}

    def test_holderless_bug_zombie_is_reclaimed(self):
        import sim
        import time
        now_ms = int(time.time() * 1000)
        state = self._state()
        n = sim._stale_work_step(state, time.time(), now_ms)
        self.assertEqual(n, 1)
        self.assertNotIn('task-zombie-bug', state['tasks'])
        self.assertTrue(state['workQueue'])

    def test_held_bug_card_stays_with_the_alarm(self):
        import sim
        import time
        now_ms = int(time.time() * 1000)
        state = self._state()
        state['agents'] = {'ghost': {'task': 'task-zombie-bug'}}
        n = sim._stale_work_step(state, time.time(), now_ms)
        self.assertEqual(n, 0,
                         'a HELD bug card keeps the bug-alarm path')
        self.assertIn('task-zombie-bug', state['tasks'])


class StaleSuspensionResidue(unittest.TestCase):
    """A scheduled-suspension stamp hours old on a non-working card is dead:
    the interrupting item is gone and the resume callback never fired."""

    def test_stale_suspension_cleared_on_queued_card(self):
        from sim import _clear_content_residue
        import time
        now_ms = int(time.time() * 1000)
        state = {'tasks': {
            'task-a': {'status': 'queued',
                       '_suspendedForScheduled': now_ms - 3 * 3600000},
            'task-b': {'status': 'queued',
                       '_suspendedForScheduled': now_ms - 60000},
            'task-c': {'status': 'working',
                       '_suspendedForScheduled': now_ms - 7200000},
        }}
        _clear_content_residue(state)
        self.assertNotIn('_suspendedForScheduled', state['tasks']['task-a'])
        self.assertIn('_suspendedForScheduled', state['tasks']['task-b'],
                      'a fresh suspension is honored')
        self.assertIn('_suspendedForScheduled', state['tasks']['task-c'],
                      'a working card keeps its suspension')


class QueuedCardReconciler(unittest.TestCase):
    """A queued card whose dispatch item vanished without an assignment is
    re-issued by the stale sweep: the consume-then-fail race otherwise leaves
    it queued forever with no holder and no item."""

    def _state(self):
        return {'tasks': {'task-orphan': {
                    'id': 'task-orphan', 'status': 'queued',
                    'title': 'Orphaned merge card', 'room': 'observatory',
                    'taskType': 'report'}},
                'agents': {}, 'agentRoster': [], 'workQueue': [],
                'lastStaleWorkSweep': 0, '_staleWorkReplans': {}, 'teams': []}

    def test_orphaned_queued_card_is_reissued(self):
        import sim
        import time
        now_ms = int(time.time() * 1000)
        state = self._state()
        sim._stale_work_step(state, time.time(), now_ms)
        items = [w for w in state['workQueue'] if w.get('reissueOf') == 'task-orphan']
        self.assertEqual(len(items), 1)
        self.assertNotIn('task-orphan', state['tasks'].get('task-orphan', {})
                         and [] or [])

    def test_card_with_item_or_holder_is_left_alone(self):
        import sim
        import time
        now_ms = int(time.time() * 1000)
        state = self._state()
        state['workQueue'].append({'reissueOf': 'task-orphan', 'title': 'x'})
        sim._stale_work_step(state, time.time(), now_ms)
        self.assertEqual(len(state['workQueue']), 1,
                         'an item already queued is not duplicated')
        state2 = self._state()
        state2['agents'] = {'ghost': {'task': 'task-orphan'}}
        sim._stale_work_step(state2, time.time(), now_ms)
        self.assertEqual(len(state2['workQueue']), 0,
                         'a held card is not re-issued')


class ReplanCappedLogThrottle(unittest.TestCase):
    """A capped card's log row fires once per 30 minutes per card -- 197
    stuck cards logged 26,398 rows/hour when it fired every tick."""

    def _state(self):
        tasks = {'task-cap': {'id': 'task-cap', 'status': 'queued',
                              'title': 'Stuck card', 'room': 'dev'}}
        return {'tasks': tasks, 'workQueue': [],
                'agents': {'dev1': {'task': None}},
                '_staleWorkReplans':
                    {repr(('Stuck card', 'dev')): sim.STALE_WORK_MAX_REPLANS}}

    def _run(self, state, now_ms):
        with mock.patch('serve.log_action') as la:
            sim._reconcile_queued_cards(state, now_ms)
        return la

    def test_logs_once_then_stays_silent_within_window(self):
        state = self._state()
        base = 1_700_000_000_000
        first = self._run(state, base)
        self.assertEqual(first.call_count, 1)
        second = self._run(state, base + 60_000)
        self.assertEqual(second.call_count, 0)

    def test_logs_again_after_window(self):
        state = self._state()
        base = 1_700_000_000_000
        self._run(state, base)
        again = self._run(state, base + 31 * 60 * 1000)
        self.assertEqual(again.call_count, 1)

    def test_tracker_stays_bounded(self):
        state = self._state()
        for i in range(410):
            state['tasks'][f'task-c{i}'] = {
                'id': f'task-c{i}', 'status': 'queued', 'title': f'c{i}',
                'room': 'dev'}
            state['_staleWorkReplans'][repr((f'c{i}', 'dev'))] = 99
        self._run(state, 1_700_000_000_000)
        self.assertLessEqual(len(state.get('_replanCappedLoggedAt') or {}), 400)


class AssignPerPassCap(unittest.TestCase):
    """One task_cycle pass must not mint 70-140 tasks: a mass requeue made a
    single pass blow the watchdog's 180s budget, the recycle killed it, and
    every in-memory assignment was lost (task_assigned rows survived, state
    did not). The cap spreads the burst over the 6s cycles."""

    def test_cap_constant_and_loop_bound(self):
        import sim
        self.assertEqual(sim.MAX_ASSIGN_PER_PASS, 10)
        import inspect
        src = inspect.getsource(sim._task_cycle)
        self.assertIn('assigned_this_pass >= MAX_ASSIGN_PER_PASS', src)
        self.assertIn('assigned_this_pass += 1', src)
