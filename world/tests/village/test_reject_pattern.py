"""Rejection-to-lesson loop (2026-11-10): structured reject events at the
peer gate, the pattern aggregator minting one fix-the-pattern card per
recurring (team, workType, kind), team lessons harvested from completed
pattern cards and injected into future instructions, and failure-kind
weights so infra noise never routes work away from healthy workers."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402


NOW = 1_760_000_000_000


def _reject(team='nora', work_type='task', kind='rejected', age_ms=0, note='missing evidence'):
    return {'atMs': NOW - age_ms, 'taskId': 'task-p' + str(len(note)) + str(age_ms),
            'title': 'Do the thing', 'teamId': team, 'room': 'workroom',
            'workType': work_type, 'kind': kind, 'reviewer': 'felix', 'note': note}


class RejectLedger(unittest.TestCase):
    def test_record_event_appends_and_caps(self):
        state = {}
        for i in range(sim._REJECT_LEDGER_MAX + 5):
            sim._record_reject_event(state, {'id': 'task-%d' % i, 'title': 't%d' % i,
                                             'teamId': 'nora', 'room': 'r',
                                             'taskType': 'task'},
                                     None, 'felix', 'rejected', 'note %d' % i, NOW + i)
        ledger = state['rejectLedger']
        self.assertEqual(len(ledger), sim._REJECT_LEDGER_MAX)
        self.assertEqual(ledger[0]['note'], 'note 5')  # oldest dropped

    def test_record_event_skips_non_dict_parent(self):
        state = {}
        sim._record_reject_event(state, None, None, 'felix', 'rejected', 'note', NOW)
        self.assertEqual(state.get('rejectLedger', []), [])


class RejectPatternAggregation(unittest.TestCase):
    def _state(self, rejects):
        return {'sim': {'owner': 'server'}, 'agents': {}, 'tasks': {},
                'workQueue': [], 'agentRoster': [], 'lastStaleWorkSweep': 0,
                'rejectLedger': list(rejects)}

    def test_below_threshold_mints_nothing(self):
        state = self._state([_reject(), _reject(age_ms=1000)])
        sim._reject_pattern_step(state, NOW)
        self.assertEqual(state['workQueue'], [])

    def test_threshold_mints_one_pattern_card(self):
        state = self._state([_reject(), _reject(age_ms=1000), _reject(age_ms=2000)])
        with mock.patch.object(sim, 'queue_work') as qw, \
                mock.patch('serve.log_action'):
            sim._reject_pattern_step(state, NOW)
        self.assertEqual(qw.call_count, 1)
        card = qw.call_args[0][1][0]
        self.assertIn('[PATTERN]', card['title'])
        self.assertIn('rejected', card['title'])
        self.assertEqual(card['teamId'], 'nora')
        self.assertEqual(card['taskType'], 'task')
        self.assertIn('task task-p', card['instructions'])
        key = repr(('nora', 'task', 'rejected'))
        self.assertIn(key, state['_patternFixState'])
        self.assertEqual(state['_patternFixState'][key]['title'], card['title'])

    def test_active_pattern_card_blocks_duplicate_mint(self):
        state = self._state([_reject(), _reject(age_ms=1000), _reject(age_ms=2000)])
        with mock.patch.object(sim, 'queue_work') as qw, \
                mock.patch('serve.log_action'):
            sim._reject_pattern_step(state, NOW)
            self.assertEqual(qw.call_count, 1)
            # a live pattern card blocks a second mint on the next sweep
            state['lastRejectPatternSweep'] = NOW - sim._REJECT_PATTERN_SWEEP_MS - 1
            state['tasks']['task-live'] = {'id': 'task-live', 'status': 'queued',
                                           'title': state['_patternFixState'][
                                               repr(('nora', 'task', 'rejected'))]['title']}
            sim._reject_pattern_step(state, NOW + sim._REJECT_PATTERN_SWEEP_MS + 1)
        self.assertEqual(qw.call_count, 1)

    def test_cooldown_blocks_remint(self):
        state = self._state([_reject(), _reject(age_ms=1000), _reject(age_ms=2000)])
        key = repr(('nora', 'task', 'rejected'))
        with mock.patch.object(sim, 'queue_work') as qw, \
                mock.patch('serve.log_action'):
            sim._reject_pattern_step(state, NOW)
            state['_patternFixState'][key]['cooldownUntil'] = NOW + sim._PATTERN_FIX_COOLDOWN_MS
            state['lastRejectPatternSweep'] = NOW - sim._REJECT_PATTERN_SWEEP_MS - 1
            sim._reject_pattern_step(state, NOW + sim._REJECT_PATTERN_SWEEP_MS + 1)
        self.assertEqual(qw.call_count, 1)

    def test_done_pattern_card_becomes_team_lesson(self):
        state = self._state([_reject(), _reject(age_ms=1000), _reject(age_ms=2000)])
        with mock.patch.object(sim, 'queue_work'), \
                mock.patch('serve.log_action'):
            sim._reject_pattern_step(state, NOW)
            title = state['_patternFixState'][repr(('nora', 'task', 'rejected'))]['title']
            state['tasks']['task-fix'] = {'id': 'task-fix', 'status': 'done',
                                          'title': title,
                                          'note': 'Always attach pipeline evidence to a clean vote.'}
            state['lastRejectPatternSweep'] = NOW - sim._REJECT_PATTERN_SWEEP_MS - 1
            sim._reject_pattern_step(state, NOW + sim._REJECT_PATTERN_SWEEP_MS + 1)
        lesson = state['teamLessons'][repr(('nora', 'task'))]
        self.assertIn('pipeline evidence', lesson['lesson'])
        self.assertIsNotNone(state['_patternFixState'][repr(('nora', 'task', 'rejected'))]['cooldownUntil'])

    def test_cancelled_pattern_card_arms_cooldown_without_lesson(self):
        state = self._state([_reject(), _reject(age_ms=1000), _reject(age_ms=2000)])
        with mock.patch.object(sim, 'queue_work'), \
                mock.patch('serve.log_action'):
            sim._reject_pattern_step(state, NOW)
            title = state['_patternFixState'][repr(('nora', 'task', 'rejected'))]['title']
            state['tasks']['task-fix'] = {'id': 'task-fix', 'status': 'cancelled',
                                          'title': title, 'note': 'intended behavior'}
            state['lastRejectPatternSweep'] = NOW - sim._REJECT_PATTERN_SWEEP_MS - 1
            sim._reject_pattern_step(state, NOW + sim._REJECT_PATTERN_SWEEP_MS + 1)
        self.assertNotIn(repr(('nora', 'task')), state.get('teamLessons') or {})
        self.assertIsNotNone(state['_patternFixState'][repr(('nora', 'task', 'rejected'))]['cooldownUntil'])


class TeamLessonInjection(unittest.TestCase):
    def test_lesson_note_renders_for_matching_family(self):
        state = {'teamLessons': {repr(('nora', 'task')):
                                 {'lesson': 'Attach evidence.', 'atMs': NOW}}}
        note = sim._team_lesson_note_for(state, {'teamId': 'nora', 'taskType': 'task'})
        self.assertIn('Attach evidence.', note)
        self.assertIn('Team lesson', note)

    def test_no_lesson_for_other_family(self):
        state = {'teamLessons': {repr(('nora', 'task')):
                                 {'lesson': 'Attach evidence.', 'atMs': NOW}}}
        self.assertIsNone(sim._team_lesson_note_for(state, {'teamId': 'argo', 'taskType': 'task'}))
        self.assertIsNone(sim._team_lesson_note_for(state, {'teamId': 'nora', 'taskType': 'code'}))
        self.assertIsNone(sim._team_lesson_note_for(state, None))


class FailureKinds(unittest.TestCase):
    def test_infra_failure_barely_steers(self):
        state = {}
        sim.record_agent_failure(state, 'ada', now_ms=NOW, kind='infra')
        self.assertLess(sim._agent_failure_score(state, 'ada', NOW),
                        sim.FAILURE_COOLDOWN_THRESHOLD)

    def test_quality_failure_steers(self):
        state = {}
        sim.record_agent_failure(state, 'ada', now_ms=NOW, kind='quality')
        self.assertGreaterEqual(sim._agent_failure_score(state, 'ada', NOW),
                                sim.FAILURE_COOLDOWN_THRESHOLD)

    def test_mixed_kinds_weight(self):
        state = {}
        sim.record_agent_failure(state, 'ada', now_ms=NOW, kind='infra')
        sim.record_agent_failure(state, 'ada', now_ms=NOW, kind='quality')
        score = sim._agent_failure_score(state, 'ada', NOW)
        self.assertAlmostEqual(score, 1.2, places=6)

    def test_legacy_entry_without_kinds_keeps_flat_count(self):
        state = {'agentFailureMemory': {'ada': {'count': 2, 'lastFailedAt': NOW}}}
        self.assertEqual(sim._agent_failure_score(state, 'ada', NOW), 2.0)
