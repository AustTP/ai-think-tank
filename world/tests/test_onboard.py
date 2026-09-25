"""Tests for Phase B: the onboard ceremony (mirrors firing).

A fresh hire (Phase A) is stamped with `_pendingOnboard`. Over successive
`_governance_pass` ticks the ceremony: (1) embarks the Town Hall -- the director
+ the employees who'll work with the new hire convene at Command Center, (2)
after ONBOARD_MEET_DURATION_MS stages the new agent's AGENT.md progressively, and
(3) on the final stage removes the `onboarding` marker so a later
sync_agent_directories finalizes AGENTS.md. Deterministic -- no Jev spend.
The "no serve import" claim above was wrong: _governance_pass's onboarding
path has inline `from serve import log_action` calls, a real side effect
that writes into whatever real village.db sits at serve.py's default path
unless DB_PATH is redirected below. Found 2026-09-25 via a live production
village.db that picked up test fixture rows (fake onboard meetings) after a
routine `tests/run_all.sh` run.
"""
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

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='village-onboard-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        VILLAGE_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _seed(**over):
    now = int(time.time() * 1000)
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'faye', 'name': 'Faye', 'role': 'Control Room', 'isAdmin': True},
            {'id': 'nora', 'name': 'Nora', 'role': 'Personnel', 'isDirector': True},
            {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'model': 'small', 'director': 'faye'},
            {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'model': 'small', 'director': 'faye'},
        ],
        'agents': {
            'faye': {'id': 'faye', 'x': 0, 'y': 0, 'busy': False, 'task': None,
                     'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                     'approvedCount': 0, 'droppedCount': 0, 'hiredAt': 0,
                     'visible': True},
            'nora': {'id': 'nora', 'x': 100, 'y': 100, 'busy': False, 'task': None,
                     'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                     'approvedCount': 0, 'droppedCount': 0, 'hiredAt': 0,
                     'visible': True},
            'ada': {'id': 'ada', 'x': 200, 'y': 200, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                    'approvedCount': 0, 'droppedCount': 0, 'hiredAt': now,
                    'visible': True},
            'ben': {'id': 'ben', 'x': 300, 'y': 300, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                    'approvedCount': 0, 'droppedCount': 0, 'hiredAt': now,
                    'visible': True},
        },
        'reports': [],
        'workQueue': [],
        'lastHireAt': 0,
        'lastFiringReviewAt': 0,
    }
    state.update(over)
    return state


def _idle_with_work(state):
    """Give the village something to do so the idle-quiet gate won't no-op."""
    state.setdefault('workQueue', []).append({
        'title': 'Scheduled research: weather data', 'room': 'observatory',
        'instructions': 'crawl', 'pair': False, 'notBefore': None,
        'priority': sim.WORK_PRIORITY['normal'], 'goal': 'weather-data',
        'research': {'topicId': 't1', 'since': 0}, 'taskType': 'research',
        'skillReview': False,
    })
    return state


def _run_governance(state, ticks, decider, now=1000.0):
    """Step _governance_pass forward in SIM_TICK_S increments, like the live loop.
    `now` advances monotonically and is returned so a caller can continue from
    where a previous run left off. Returns (state, now)."""
    return _continue(state, ticks, decider, now)


def _continue(state, ticks, decider, now):
    """Advance from an explicit `now` (already advanced by prior runs) -- the
    ceremony is wall-time cadenced, so reusing a stale `now` would freeze the
    stage timers. Returns (state, now)."""
    grid, _ = sim._load_outdoor_geometry()
    for _ in range(int(ticks)):
        now += sim.SIM_TICK_S
        sim._governance_pass(state, now=now, now_ms=int(now * 1000), grid=grid,
                             decider=decider)
    return state, now


def _fresh_hire(state, decider):
    """Run the governance loop long enough to complete a hire into faye's team.
    Still the ONLY hire: block further hires (cooldown) so the ceremony under
    test is that single new agent's, not a later hire's. Returns the new agent's
    id (the onboard ceremony's subject)."""
    _run_governance(state, 12, decider)  # 2026-09-23: was 6 @ 2s tick (12s wall); keeps same wall-time @ 1s
    roster = state.get('agentRoster') or []
    new_ids = [d['id'] for d in roster if d.get('director') == 'faye'
               and d['id'] not in ('faye', 'nora', 'ada', 'ben')]
    # Freeze hires so only this ceremony runs to completion.
    state['lastHireAt'] = int(time.time() * 1000)
    return new_ids[0] if new_ids else None


class OnboardCeremony(unittest.TestCase):
    def decider_ben(self):
        # Hire help for ben (faye's report) so ben is the `helpFor` coworker.
        def decider(state, instructions, candidates):
            for c in candidates:
                if c['id'] == 'ben':
                    return 'ben'
            return candidates[0]['id'] if candidates else None
        return decider

    def test_hire_stamps_pending_onboard_with_expected_coworkers(self):
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        new_id = _fresh_hire(state, self.decider_ben())
        self.assertIsNotNone(new_id, 'a hire completed')
        pending = state.get('_pendingOnboard')
        self.assertIsNotNone(pending, 'a fresh hire enqueues an onboard ceremony')
        self.assertEqual(pending['agentId'], new_id)
        self.assertEqual(pending['directorId'], 'faye')
        # Coworkers = the hire's `helpFor` plus the director's other reports
        # (faye's team: ada; ben is the helpFor). No self.
        self.assertNotIn(new_id, pending['coworkerIds'], 'the hire is not their own coworker')
        self.assertIn('ben', pending['coworkerIds'], 'helpFor ben is a coworker')
        self.assertIn('ada', pending['coworkerIds'], 'teammate ada is a coworker')
        # The new agent carries the staged onboarding marker, not yet finalized.
        new_agent = state['agents'][new_id]
        self.assertIsNotNone(new_agent.get('profile', {}).get('onboarding'),
                             'ceremony marks the new hire as under staged onboarding')

    def test_meeting_convenes_director_and_coworkers_at_command_center(self):
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        new_id = _fresh_hire(state, self.decider_ben())
        grid, _ = sim._load_outdoor_geometry()
        # Step just one tick after the hire lands so the onboard meeting embarks --
        # before ONBOARD_MEET_DURATION_MS elapses.
        now = 1000.0 + 7 * sim.SIM_TICK_S
        sim._governance_pass(state, now=now, now_ms=int(now * 1000), grid=grid,
                             decider=self.decider_ben())
        self.assertTrue(state.get('_pendingOnboard', {}).get('embarked'),
                        'the Town Hall embarks')
        faye = state['agents']['faye']
        ada = state['agents']['ada']
        self.assertTrue(faye.get('busy') and not faye.get('visible'),
                        'director convenes (busy, off the floor)')
        self.assertEqual(faye.get('inRoom'), 'commandcenter')
        self.assertTrue(ada.get('busy') and not ada.get('visible'),
                        'a coworker convenes too')
        self.assertEqual(ada.get('inRoom'), 'commandcenter')
        self.assertNotEqual(faye.get('roomX'), ada.get('roomX'),
                            'participants spread across the room like firing reviewers')
        # The new hire is NOT yet staged -- the meet hasn't elapsed.
        new_agent = state['agents'][new_id]
        self.assertEqual(new_agent['profile']['onboarding'].get('stage'), 0)

    def test_busy_coworker_defers_embark(self):
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        # Make the helpFor (ben) busy BEFORE the hire lands, so the moment the
        # governance pass completes the hire and tries to embark the Town Hall,
        # ben is busy -- the ceremony must defer rather than strand a live
        # collaboration reference.
        state['agents']['ben']['busy'] = True
        new_id = _fresh_hire(state, self.decider_ben())
        self.assertIsNotNone(new_id, 'a hire still completes (busy coworker only defers the meeting)')
        self.assertFalse(state.get('_pendingOnboard', {}).get('embarked'),
                         'busy coworker defers the meeting embark')
        self.assertIsNotNone(state.get('_pendingOnboard'),
                             'the pending ceremony survives the deferral')
        # Clear the busy flag and the meeting proceeds next pass.
        state['agents']['ben']['busy'] = False
        grid, _ = sim._load_outdoor_geometry()
        sim._governance_pass(state, now=1000.0 + 7 * sim.SIM_TICK_S,
                             now_ms=int((1000.0 + 7 * sim.SIM_TICK_S) * 1000),
                             grid=grid, decider=self.decider_ben())
        self.assertTrue(state.get('_pendingOnboard', {}).get('embarked'),
                        'a freed coworker lets the meeting embark')

    def test_staged_agend_md_progresses_and_finalizes_when_hire_claims_a_task(self):
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        new_id = _fresh_hire(state, self.decider_ben())
        # Run through the ceremony stages (meeting elapsed). 2026-09-23: was 40 @
        # 2s tick (80s wall); 80 @ 1s keeps the same wall-time.
        state, now = _run_governance(state, 80, self.decider_ben())
        new_agent = state['agents'][new_id]
        profile = new_agent.get('profile', {})
        # Stage 3 readiness hold is reached but NOT yet passed: without a real
        # claimed task (and far short of the 15-min timeout) the hire waits in
        # limbo rather than being finalized.
        self.assertEqual(profile.get('onboarding', {}).get('stage'), 3,
                         'hire holds in readiness until it claims a real task')
        self.assertIn('you report to', ' '.join(profile.get('instructions') or []).lower(),
                      'stage 1 drafted who the hire reports to')
        self.assertIn('overflow work', ' '.join(profile.get('instructions') or []).lower(),
                      'stage 2 drafted the concrete assignment')
        # Everyone returns to duty after the meeting proper.
        self.assertFalse(state['agents']['faye'].get('busy'))
        self.assertTrue(state['agents']['faye'].get('visible'))

        # The hire actually claims a real co-work task (the task cycle would do
        # this via assign_task: agent.task -> a live walking/working task). The
        # next governance pass must observe readiness and finalize immediately.
        # Give it a few more passes with nothing claimed: still holding.
        state, now = _continue(state, 5, self.decider_ben(), now)
        self.assertEqual(state['agents'][new_id]['profile']['onboarding']['stage'], 3,
                         'still holds with no real task claimed')
        state['agents'][new_id]['task'] = 'task-x'
        state.setdefault('tasks', {})['task-x'] = {
            'id': 'task-x', 'assignedTo': new_id, 'status': 'working',
        }
        state, _ = _continue(state, 2, self.decider_ben(), now)
        self.assertNotIn('onboarding', state['agents'][new_id].get('profile', {}),
                         'a hire that claimed a real task finishes onboarding')
        self.assertIsNone(state.get('_pendingOnboard'),
                          'ceremony completes and no longer pending')

    def test_readiness_hold_finalizes_on_real_claimed_task(self):
        """Onboarding stays open while the hire has nothing it actually holds,
        and completes the moment it claims a real task -- the observed-readiness
        fast path."""
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        new_id = _fresh_hire(state, self.decider_ben())
        # Step through the ceremony to the stage-3 readiness hold, threading now.
        state, now = _continue(state, 60, self.decider_ben(), 1000.0)
        self.assertEqual(state['agents'][new_id]['profile']['onboarding']['stage'], 3)
        # Give it a couple more passes; nothing claimed, still not done.
        state, now = _continue(state, 5, self.decider_ben(), now)
        self.assertEqual(state['agents'][new_id]['profile']['onboarding']['stage'], 3,
                         'unclaimed hire stays in readiness')

        # Claim a task that is held by the hire (working, assignee is the hire).
        state['agents'][new_id]['task'] = 'task-a'
        state.setdefault('tasks', {})['task-a'] = {
            'id': 'task-a', 'assignedTo': new_id, 'status': 'working',
        }
        state, _ = _continue(state, 2, self.decider_ben(), now)
        self.assertNotIn('onboarding', state['agents'][new_id].get('profile', {}),
                         'claiming a held task finalizes onboarding')
        self.assertIsNone(state.get('_pendingOnboard'))

    def test_readiness_hold_does_not_complete_on_stale_refs(self):
        """A task pointer that is NOT a live held task (reclaimed/parked/done, or
        the hire no longer holds it) must not satisfy the readiness gate."""
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        new_id = _fresh_hire(state, self.decider_ben())
        state, now = _continue(state, 60, self.decider_ben(), 1000.0)
        # Pointer to a done task -- not the hire holding live work.
        state['agents'][new_id]['task'] = 'task-done'
        state.setdefault('tasks', {})['task-done'] = {
            'id': 'task-done', 'assignedTo': new_id, 'status': 'done',
        }
        state, now = _continue(state, 3, self.decider_ben(), now)
        self.assertEqual(state['agents'][new_id]['profile']['onboarding']['stage'], 3,
                         'a done task is not live held work')
        # Pointer to a task another agent holds -- the hire is not the holder.
        state['agents'][new_id]['task'] = 'task-other'
        state['agents']['ada']['task'] = 'task-other'
        state.setdefault('tasks', {})['task-other'] = {
            'id': 'task-other', 'assignedTo': 'ada', 'status': 'walking',
        }
        state, now = _continue(state, 3, self.decider_ben(), now)
        self.assertEqual(state['agents'][new_id]['profile']['onboarding']['stage'], 3,
                         'a task held by someone else is not the hire claiming work')
        # Clean up so 'ada' doesn't carry a phantom task into later assertions.
        state['agents']['ada']['task'] = None

    def test_readiness_hold_timeout_finalizes_idle_village(self):
        """An idle/empty village (workQueue drained; the user's current state)
        never assigns the hire a task. The readiness hold must not strand the
        hire forever -- after ONBOARD_READINESS_TIMEOUT_MS full RT it completes
        anyway."""
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        new_id = _fresh_hire(state, self.decider_ben())
        state, now = _continue(state, 60, self.decider_ben(), 1000.0)
        self.assertEqual(state['agents'][new_id]['profile']['onboarding']['stage'], 3)
        # Jump well past the readiness cap; the hold finalizes with reason 'timeout'.
        grid, _ = sim._load_outdoor_geometry()
        now = now + sim.ONBOARD_READINESS_TIMEOUT_MS / 1000.0 + 5 * sim.SIM_TICK_S
        sim._governance_pass(state, now=now, now_ms=int(now * 1000), grid=grid,
                             decider=self.decider_ben())
        self.assertNotIn('onboarding', state['agents'][new_id].get('profile', {}),
                         'the readiness cap finalizes an otherwise-idle hire')
        self.assertIsNone(state.get('_pendingOnboard'))
        # (reason='timeout' is passed to the durable serve.log_action in live
        # operation; the pure-sim unit test sees only the state thin-out, which
        # is what the assertions above cover.)


if __name__ == '__main__':
    unittest.main()