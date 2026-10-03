"""Tests for the weekly cross-team Knowledge Social.

A weekly 30-minute conversation in the Hangout for agents that PRODUCED an
approved deliverable this week (weekApprovals > 0). Attendees return exactly to
their prior state on resolve: a mid-task worker keeps her task (budget extended),
an off-duty agent is woken for the event and returned off-duty, an idle on-duty
one returns to idle. Runs ungated (the event is the point even on an idle
think tank). Deterministic -- no Jev, direct _social_step calls. NOT actually
DB-free: the carry-away/digest logging path has inline `from serve import
log_action` calls, a real side effect that writes into whatever real
think_tank.db sits at serve.py's default path unless DB_PATH is redirected
below. Found via a live production think_tank.db that picked up test
fixture rows after a routine `tests/run_all.sh` run.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-social-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _stub_decider(choice='adopt', confidence=0.9):
    """Deterministic stand-in for the Jev-backed _social_decider so tests never
    hit the network. Returns a fixed (choice, confidence)."""
    def decider(instructions, criteria):
        return choice, confidence
    return decider


def _step(state, _zero_unused, now):
    """Drive one social pass with the deterministic decider threaded (a resolve
    would otherwise hit the network default)."""
    return sim._social_step(state, 1000.0, now, decider=_stub_decider())


def _seed(**over):
    now_ms = 1_000_000
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'ada', 'name': 'Ada', 'role': 'Research'},
            {'id': 'ben', 'name': 'Ben', 'role': 'Banking'},
            {'id': 'faye', 'name': 'Faye', 'role': 'Admin'},
        ],
        'agents': {
            # Eligible + idle on-duty.
            'ada': {'id': 'ada', 'x': 10, 'y': 10, 'dir': 'south', 'visible': True,
                    'busy': False, 'task': None, 'inRoom': None, 'offDuty': False,
                    'weekApprovals': 3, 'approvedCount': 3},
            # Eligible + mid-task (working).
            'ben': {'id': 'ben', 'x': 40, 'y': 10, 'dir': 'south', 'visible': True,
                    'busy': True, 'task': 'task-1', 'inRoom': 'pressoffice',
                    'offDuty': False, 'weekApprovals': 2, 'approvedCount': 5},
            # Ineligible (no week's work).
            'faye': {'id': 'faye', 'x': 70, 'y': 10, 'dir': 'south', 'visible': True,
                     'busy': False, 'task': None, 'inRoom': None, 'offDuty': False,
                     'weekApprovals': 0, 'approvedCount': 0},
        },
        'tasks': {'task-1': {'id': 'task-1', 'assignedTo': 'ben', 'status': 'working',
                             'workUntil': 5_000_0, 'title': 'Build ledger tool'}},
        'lastSocialAt': 0,
    }
    state.update(over)
    return state


class KnowledgeSocial(unittest.TestCase):

    def test_eligible_only_attend_and_ineligible_untouched(self):
        state = _seed()
        # Fire the weekly pass; it stamps pending + convenes eligible present
        # attendees (ada idle, ben working) into the Hangout.
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS)
        pending = state['_pendingSocial']
        # Convene happens on the immediately-following pass.
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1)
        people = pending['people']
        self.assertIn('ada', people, 'eligible idle agent attends')
        self.assertIn('ben', people, 'eligible mid-task agent attends')
        self.assertNotIn('faye', people, 'no week work -> not eligible')
        # Faye stays untouched: still idle on-duty, no Hangout, no busy flag.
        faye = state['agents']['faye']
        self.assertFalse(faye['busy'])
        self.assertEqual(faye['inRoom'], None)
        self.assertTrue(faye['visible'])
        self.assertFalse(faye['offDuty'])

    def test_idle_on_duty_returns_to_idle(self):
        state = _seed()
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS)
        # Convene.
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1)
        ada = state['agents']['ada']
        self.assertTrue(ada['busy'], 'attendee is recessed during the event')
        self.assertEqual(ada['inRoom'], 'hangout', 'convened into the Hangout')
        self.assertTrue(ada['visible'])
        # Resolve after the 30-minute meet.
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2)
        ada = state['agents']['ada']
        self.assertFalse(ada['busy'], 'returns to idle on-duty')
        self.assertFalse(ada['offDuty'])
        self.assertEqual(ada['inRoom'], None)
        self.assertTrue(ada['visible'])

    def test_working_agent_keeps_task_and_budget_extended(self):
        state = _seed()
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS)
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1)
        ben = state['agents']['ben']
        self.assertIsNone(ben['task'], 'task ptr detached during the event')
        # The task is flagged so the orphan-reclaim pass won't re-issue it.
        self.assertTrue(state['tasks']['task-1'].get('_inSocial'))
        # Resolve: reclaim + restore + extend budget by the full 30-min duration.
        before_wu = state['tasks']['task-1']['workUntil']
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2)
        ben = state['agents']['ben']
        self.assertEqual(ben['task'], 'task-1', 'worker reclaims their task')
        self.assertTrue(ben['busy'])
        self.assertEqual(ben['inRoom'], 'pressoffice')
        self.assertNotIn('_inSocial', state['tasks']['task-1'],
                         'social flag removed so the task is live again')
        self.assertEqual(state['tasks']['task-1']['workUntil'],
                         before_wu + sim.SOCIAL_MEET_MS // 1000,
                         'work budget extended so the conversation burned none')

    def test_off_duty_agent_woken_then_returned_off_duty(self):
        state = _seed()
        # Ada eligible but currently off-duty.
        state['agents']['ada']['offDuty'] = True
        state['agents']['ada']['visible'] = False
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS)
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1)
        ada = state['agents']['ada']
        self.assertFalse(ada['offDuty'], 'woken (on duty) for the event')
        self.assertTrue(ada['visible'], 'visible in the Hangout during the event')
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2)
        ada = state['agents']['ada']
        self.assertTrue(ada['offDuty'], 'returns to off-duty')
        self.assertFalse(ada['visible'], 'vanishes again (off-duty sprite gone)')
        self.assertEqual(ada['inRoom'], None)

    def test_week_approvals_reset_on_resolve_for_next_window(self):
        state = _seed()
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS)
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1)
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2)
        self.assertEqual(state['agents']['ada']['weekApprovals'], 0,
                         'week window rolls so next week is measured fresh')
        self.assertEqual(state['agents']['ben']['weekApprovals'], 0)
        self.assertEqual(state['agents']['faye']['weekApprovals'], 0)

    def test_pending_dropped_after_resolve(self):
        state = _seed()
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS)
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1)
        self.assertIn('_pendingSocial', state)
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2)
        self.assertNotIn('_pendingSocial', state)

    def test_decision_tape_recorded_per_attendee(self):
        """Each eligible attendee leaves a typed carry-away decision (the
        Jev-article tape), recorded on state with choice + confidence."""
        state = _seed()
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS)
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1)
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2)
        tape = state.get('_socialDecisions') or []
        # ada + ben attended (both eligible); faye didn't.
        self.assertEqual(len(tape), 2, 'one decision per eligible attendee')
        by_id = {d['agentId']: d for d in tape}
        self.assertIn('ada', by_id)
        self.assertIn('ben', by_id)
        self.assertNotIn('faye', by_id, 'ineligible agent emits no decision')
        for aid in ('ada', 'ben'):
            self.assertEqual(by_id[aid]['choice'], 'adopt', 'stub decider choice recorded')
            self.assertEqual(by_id[aid]['confidence'], 0.9)
            self.assertIn('weekApprovals', by_id[aid])

    def test_no_eligible_attendees_is_a_noop(self):
        # Nobody has done week work.
        state = _seed()
        for a in state['agents'].values():
            a['weekApprovals'] = 0
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS)
        # Still schedules, but convene pulls nobody in and resolves cleanly.
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1)
        self.assertEqual(state['_pendingSocial']['people'], {})
        _step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2)
        self.assertNotIn('_pendingSocial', state)


class SocialAdoptLands(unittest.TestCase):
    """W2: a Knowledge Social 'adopt' carry-away must LAND -- it routes a
    coaching note through the growth-plan loop to the worker's NEXT task, so the
    adoption changes execution instead of staying a logged tape line. A fresh
    weekly adopt is a new commitment (repeat=True), so it re-lands even when an
    older note of the same kind is still queued."""

    def _resolve(self, choice, confidence):
        # Drive schedule -> convene -> resolve with the given decision.
        state = _seed()
        sim._social_step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS,
                         decider=_stub_decider(choice, confidence))
        sim._social_step(state, 1000.0, 1_000_000 + sim.SOCIAL_CADENCE_MS + 1,
                         decider=_stub_decider(choice, confidence))
        sim._social_step(state, 1000.0,
                         1_000_000 + sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2,
                         decider=_stub_decider(choice, confidence))
        return state

    def test_confident_adopt_lands_a_coaching_note(self):
        state = self._resolve('adopt', 0.9)
        plans = state.get('growthPlans', {}).get('ada')
        self.assertIsNotNone(plans, 'an adopt must write a growth plan')
        self.assertTrue(any(p.get('kind') == 'social_adopt' for p in plans),
                        'the note is tagged social_adopt')
        note = next(p for p in plans if p.get('kind') == 'social_adopt')
        self.assertIn('trying/applying', note['note'])
        self.assertFalse(note['applied'], 'queued for the worker\'s NEXT task')

    def test_repeated_weekly_adopts_re_land(self):
        # A fresh adopt each week is a NEW commitment: repeat=True lets it land
        # even though a social_adopt plan already exists for the worker.
        state = self._resolve('adopt', 0.9)
        # The first resolve reset weekApprovals; give everyone week work again
        # so the second event has eligible attendees.
        for a in state['agents'].values():
            a['weekApprovals'] = 2
        # Simulate a second weekly event: another adopt at a later cadence.
        sim._social_step(state, 1000.0, 1_000_000 + 2 * sim.SOCIAL_CADENCE_MS,
                         decider=_stub_decider('adopt', 0.9))
        sim._social_step(state, 1000.0, 1_000_000 + 2 * sim.SOCIAL_CADENCE_MS + 1,
                         decider=_stub_decider('adopt', 0.9))
        sim._social_step(state, 1000.0,
                         1_000_000 + 2 * sim.SOCIAL_CADENCE_MS + sim.SOCIAL_MEET_MS + 2,
                         decider=_stub_decider('adopt', 0.9))
        social_plans = [p for p in state.get('growthPlans', {}).get('ada', [])
                        if p.get('kind') == 'social_adopt']
        self.assertGreaterEqual(len(social_plans), 2,
                                'each weekly adopt re-lands (repeat, not deduped)')

    def test_low_confidence_adopt_does_not_land(self):
        # A wavering adopt (below SOCIAL_ADOPT_CONFIDENCE) stays a tape line.
        state = self._resolve('adopt', 0.4)
        self.assertEqual(state.get('growthPlans', {}).get('ada'), None,
                          'a low-confidence adopt does not write a coaching note')

    def test_note_and_skip_never_land(self):
        for choice in ('note', 'skip'):
            state = self._resolve(choice, 0.9)
            self.assertEqual(state.get('growthPlans', {}).get('ada'), None,
                              f'{choice} carry-away must not land a coaching note')

    def test_adopt_note_is_appended_to_next_task(self):
        # The landed note flows through the existing loop: _coaching_note_for
        # pops it onto the worker's next assigned task, exactly once.
        state = self._resolve('adopt', 0.9)
        coaching = sim._coaching_note_for(state, 'ada')
        self.assertIsNotNone(coaching, 'the adopt note is served to the next task')
        self.assertIn('trying/applying', coaching)
        self.assertIsNone(sim._coaching_note_for(state, 'ada'),
                          'each note is applied exactly once')


if __name__ == '__main__':
    unittest.main()