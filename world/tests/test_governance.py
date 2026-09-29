"""Tests for Phase 4 server-owned governance (auto-hire + auto-firing review).

These exercise sim._governance_pass directly on in-memory state dicts with an
injected deterministic decider -- no network, no serve.py content executor --
matching how test_sim.py tests the rest of the pure lifecycle. The decider
replaces the Jev call so tests are deterministic and offline. The "no DB"
claim above was wrong: _governance_pass has inline `from serve import
log_action` calls, a real side effect that writes into whatever real
think_tank.db sits at serve.py's default path unless DB_PATH is redirected
below. Found 2026-09-25, same class of bug as test_onboard.py/test_social.py/
test_refinement.py/test_cut2_processes.py/test_oncall_escalation.py -- this
file isn't even wired into tests/run_all.sh, but is fixed for the same reason
those were: it pollutes the real DB the moment anyone runs it directly.
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-governance-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        # Standby off in the hermetic process keeps decision chains singular
        # so the shared-process breaker never arms or leaks.
        COLAB_STANDBY_ENABLED=False,
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
            # faye leads a team (ada + ben report to her) so auto-hire has a
            # team-scoped pool to draw from; nora stays a director with no
            # reports (not a hire-director under the structural rule).
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
    """Give the think tank something to do so the idle-quiet gate won't no-op."""
    state.setdefault('workQueue', []).append({
        'title': 'Scheduled research: weather data', 'room': 'observatory',
        'instructions': 'crawl', 'pair': False, 'notBefore': None,
        'priority': sim.WORK_PRIORITY['normal'], 'goal': 'weather-data',
        'research': {'topicId': 't1', 'since': 0}, 'taskType': 'research',
        'skillReview': False,
    })
    return state


def _fresh(contacted_now=True):
    """The neglect term is absent (~0) when the agent was contacted effectively
    now -- so a test isolating one penalty can set lastContactedAt to `now`."""
    now = 10 ** 12
    return now, (now if contacted_now else None)


class MoraleFor(unittest.TestCase):
    def test_high_approved_count_fills_bonus_to_cap(self):
        state = _seed()
        now, lc = _fresh()
        state['agents']['ada'].update({'approvedCount': 100, 'droppedCount': 0,
                                       'lastContactedAt': lc, 'hiredAt': now})
        # Bonus capped at 15; no drops/reports/neglect -> 115 -> clipped to 100.
        self.assertEqual(sim.morale_for(state, 'ada', now_ms=now), 100)

    def test_dropped_work_penalty_with_decay(self):
        state = _seed()
        now, lc = _fresh()
        a = state['agents']['ada']
        a.update({'approvedCount': 5, 'droppedCount': 3, 'lastContactedAt': lc,
                  'hiredAt': now})
        # Fresh hire: dropDecay=1 -> 3 dropped * 6 = 18. approved 5*0.5=2.5.
        # raw = 100 + 2.5 - 18 - 0 - 0 = 84.5 -> round 84.
        self.assertEqual(sim.morale_for(state, 'ada', now_ms=now), 84)
        # Same record but a hire 14+ days ago -> decay -> 0 -> no dropped penalty.
        # raw = 100 + 2.5 - 0 = 102.5, but morale_for clamps to max 100.
        a['hiredAt'] = now - 30 * 24 * 3600 * 1000
        self.assertEqual(sim.morale_for(state, 'ada', now_ms=now), 100)

    def test_report_penalty(self):
        state = _seed()
        now, lc = _fresh()
        state['reports'] = [{'aboutId': 'ada', 'fromId': 'ben', 'quote': 'x', 'severity': 'minor'}]
        a = state['agents']['ada']
        a.update({'approvedCount': 0, 'droppedCount': 0, 'lastContactedAt': lc,
                  'hiredAt': now})
        self.assertEqual(sim.morale_for(state, 'ada', now_ms=now), 90)  # 100 - 10

    def test_neglect_cap_and_decay(self):
        state = _seed()
        now, _ = _fresh()
        a = state['agents']['ada']
        # Never contacted -> full NEGLECT_CAP (30).
        a.update({'approvedCount': 0, 'droppedCount': 0, 'lastContactedAt': None,
                  'hiredAt': now})
        self.assertEqual(sim.morale_for(state, 'ada', now_ms=now), 70)
        # Long-ago contact at 3/day -> caps at 30 after 10+ days regardless.
        a['lastContactedAt'] = now - 20 * 24 * 3600 * 1000
        self.assertEqual(sim.morale_for(state, 'ada', now_ms=now), 70)


class AutoHire(unittest.TestCase):
    def decider_pick(self, choice):
        def decider(state, instructions, candidates):
            for c in candidates:
                if c['id'] == choice:
                    return choice
            return candidates[0]['id'] if candidates else None
        return decider

    def _run_hire(self, state, decider, ticks=16):
        # 2026-09-23: was 8 ticks @ SIM_TICK_S=2.0 (=16s wall); with the finer
        # 1.0s tick, 8 ticks is only 8s -- not enough to cross the hire/ceremony
        # wall-time intervals. Doubled so the same wall-time elapses.
        now = 1000.0
        grid, doors = sim._load_outdoor_geometry()
        for _ in range(ticks):
            now += sim.SIM_TICK_S
            sim._governance_pass(state, now=now, now_ms=int(now * 1000), grid=grid,
                                 decider=decider)
        return state

    def test_idle_think_tank_hires_nothing(self):
        # No work queue, nobody busy -> idle-quiet gate returns before anything.
        state = _seed()
        called = []
        def decider(*a, **k):
            called.append(1)
            return 'ada'
        self._run_hire(state, decider)
        self.assertEqual(called, [], 'idle think tank must not call the decider (no Jev spend)')
        self.assertEqual(len(state.get('agentRoster')), 4, 'no hire happened')

    def test_nonidle_but_cooldown_blocks(self):
        state = _idle_with_work(_seed())
        state['lastHireAt'] = int(time.time() * 1000)  # just hired
        called = []
        def decider(*a, **k):
            called.append(1)
            return 'ada'
        self._run_hire(state, decider)
        self.assertTrue(len(called) <= 1, 'cooldown must choke repeated hires')
        self.assertEqual(len(state.get('agentRoster')), 4, 'no new agent inside cooldown')

    def test_hires_new_agent_with_correct_fields(self):
        state = _idle_with_work(_seed())
        # Ensure cooldown elapsed.
        state['lastHireAt'] = 0
        decider = self.decider_pick('ben')  # hire help for ben
        self._run_hire(state, decider)
        roster = state.get('agentRoster') or []
        self.assertEqual(len(roster), 5, 'a hire added one roster entry')
        new_def = next((d for d in roster if d.get('id') not in ('faye', 'nora', 'ada', 'ben')), None)
        self.assertIsNotNone(new_def, 'a brand-new agent exists')
        self.assertEqual(new_def['role'], 'Assistant to Ben')
        self.assertEqual(new_def['model'], 'small')
        self.assertTrue(new_def.get('elevatedAccess'), 'command-center hires get elevated access')
        self.assertIn(new_def['id'], state['agents'], 'hired agent exists in the live map')
        self.assertEqual(new_def['id'], new_def.get('name', '').lower())
        # Team-scoped hire: the new agent reports to faye (the hiring director),
        # so they resolve into faye's team + shared dir immediately.
        self.assertEqual(new_def.get('director'), 'faye', 'new hire joins the hiring director\'s team')

    def test_team_scoped_candidates_only(self):
        # The hire candidate pool is the hiring director's OWN direct reports,
        # never the whole roster -- so a director can't hire help for someone
        # outside their team.
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        # nora leads NO team (no reports), so she must not be a hire-director.
        seen_pools = []
        def decider(state, instructions, candidates):
            seen_pools.append([c['id'] for c in candidates])
            return candidates[0]['id'] if candidates else None
        self._run_hire(state, decider, ticks=6)
        self.assertTrue(seen_pools, 'decider was consulted')
        for pool in seen_pools:
            self.assertTrue(pool, 'no empty pool offered')
            self.assertTrue(all(cid in ('ada', 'ben') for cid in pool),
                            f'candidate pool must be faye\'s team only, got {pool}')

    def test_admin_off_duty_blocks_hire(self):
        state = _idle_with_work(_seed())
        state['agents']['faye']['offDuty'] = True
        state['lastHireAt'] = 0
        called = []
        def decider(*a, **k):
            called.append(1)
            return 'ada'
        self._run_hire(state, decider)
        # The decider may still be called (cooldown claimed before idle checks in
        # our port?) -- assert NOTHING started: no pending hire, roster unchanged.
        self.assertTrue(state.get('_pendingHire') is None, 'no hire started with admin off-duty')
        self.assertEqual(len(state.get('agentRoster')), 4)

    def test_headcount_cap_blocks(self):
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        # Fake a full roster.
        state['agentRoster'] = [dict(d, id=f'm{i}') for i, d in enumerate(state['agentRoster'])]
        # Reach the cap by pre-populating.
        for i in range(500 - len(state['agentRoster'])):
            state['agentRoster'].append({'id': f'cap{i}', 'name': f'Cap{i}', 'role': 'r'})
        called = []
        def decider(*a, **k):
            called.append(1)
            return 'ada'
        self._run_hire(state, decider)
        self.assertTrue(state.get('_pendingHire') is None, 'cap must block a hire')
        self.assertEqual(len(state.get('agentRoster')), 500)

    def test_jev_outage_falls_back_to_lowest_morale(self):
        state = _idle_with_work(_seed())
        state['lastHireAt'] = 0
        # Make ben clearly worst-off.
        state['agents']['ben']['droppedCount'] = 40
        state['agents']['ben']['lastContactedAt'] = None
        state['agents']['ben']['hiredAt'] = 10 ** 12
        def decider(*a, **k):
            return None  # Jev outage
        self._run_hire(state, decider, ticks=6)
        pending = state.get('_pendingHire') or {}
        self.assertEqual(pending.get('helpForId'), 'ben', 'lowest morale fallback picks ben')


class FiringReview(unittest.TestCase):
    def _low_morale(self, agent_id):
        state = _seed()
        # Crush morale below threshold (50) without a Jev: give ben a bad record.
        a = state['agents'][agent_id]
        a.update({'droppedCount': 10, 'lastContactedAt': None, 'hiredAt': 10 ** 12,
                  'approvedCount': 0})
        return state

    def decider_fire(self, *a, **k):
        return 'fire'

    def decider_keep(self, *a, **k):
        return 'keep'

    def _run(self, state, decider, ticks=16):
        # 2026-09-23: was 8 ticks @ 2s tick (=16s wall); doubled for the 1.0s
        # tick so the same wall-time (and the same firing intervals) elapse.
        now = 1000.0
        grid, doors = sim._load_outdoor_geometry()
        # These tests isolate the FIRING loop: pin the hire cooldown far in the
        # future so the admin (who is also the admin reviewer) is never busy on a
        # hire -- otherwise the hire's decider call pollutes the Jev counters and
        # the two loops fight over faye within this short tick window.
        state['lastHireAt'] = 10 ** 12
        for _ in range(ticks):
            now += sim.SIM_TICK_S
            sim._governance_pass(state, now=now, now_ms=int(now * 1000), grid=grid,
                                 decider=decider)
        return state

    def test_idle_blocks_firing(self):
        state = self._low_morale('ben')
        called = []
        def decider(*a, **k):
            called.append(1)
            return 'fire'
        self._run(state, decider)
        self.assertEqual(called, [], 'idle think tank must not run a firing review')
        self.assertIn('ben', state['agents'])

    def test_candidate_below_threshold_is_picked_and_fired(self):
        state = self._low_morale('ben')
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        # Severe negative evidence so consultationGuardrail B doesn't block (a
        # worker with zero dropped-report history can't be fired on morale alone).
        state['reports'] = [{'aboutId': 'ben', 'fromId': 'ada',
                             'quote': 'severely dropped a critical task',
                             'severity': 'severe', 'note': 'left a handoff hanging'}]
        self._run(state, self.decider_fire)
        self.assertNotIn('ben', state['agents'], 'fire removes the candidate')
        self.assertNotIn('ben', [d['id'] for d in state['agentRoster']], 'removed from roster')
        self.assertTrue(state.get('_pendingFiringReview') is None, 'review resolved')

    def test_clean_keep_records_lastFiringReview(self):
        state = self._low_morale('ben')
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        self._run(state, self.decider_keep)
        self.assertIn('ben', state['agents'], 'keep leaves the agent')
        last = state['agents']['ben'].get('lastFiringReview')
        self.assertIsNotNone(last, 'keep records a lastFiringReview')
        self.assertEqual(last.get('verdict'), 'keep')
        self.assertEqual(last.get('morale'), sim.morale_for(state, 'ben', now_ms=int(1000 * 1000)))

    def test_stale_review_skips(self):
        state = self._low_morale('ben')
        # A previous 'keep' on the exact same evidence -> stale -> not re-picked.
        # The pass opens with now=1000.0 -> now_ms=1000000; record morale under
        # that same clock so reviewIsStale sees it as unchanged.
        state['agents']['ben']['lastFiringReview'] = {
            'morale': sim.morale_for(state, 'ben', now_ms=1000000),
            'reportCount': 0, 'verdict': 'keep', 'at': 1000000, }
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        called = []
        decider = lambda *a, **k: called.append(1) or 'fire'
        self._run(state, decider)
        self.assertIn('ben', state['agents'], 'stale (unchanged keep) is not re-litigated')
        self.assertEqual(called, [], 'no Jev call for a stale candidate')

    def test_consultation_blocks_fire_with_active_collaborator(self):
        state = self._low_morale('ben')
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        # Give ben an active pair partner -> guardrail A blocks firing.
        state['agents']['ada']['pairWith'] = 'ben'
        self._run(state, self.decider_fire)
        self.assertIn('ben', state['agents'], 'active collaborator defers a fire')

    def test_consultation_blocks_fire_without_negative_report(self):
        state = self._low_morale('ben')
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        self._run(state, self.decider_fire)
        # No coworker and no negative report -> guardrail B blocks the fire.

        self.assertIn('ben', state['agents'], 'no corroborated negative report defers')

    def test_severe_report_allows_fire(self):
        state = self._low_morale('ben')
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        state['reports'] = [{'aboutId': 'ben', 'fromId': 'ada',
                             'quote': 'severely dropped a critical task',
                             'severity': 'severe', 'note': 'left a handoff hanging'}]
        self._run(state, self.decider_fire)
        self.assertNotIn('ben', state['agents'], 'severe justified evidence allows the fire')

    def test_both_reviewers_must_be_free_and_on_duty(self):
        state = self._low_morale('ben')
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        # Busy the senior director -> reviewers not both free -> no review.
        state['agents']['nora']['busy'] = True
        self._run(state, self.decider_fire)
        self.assertIn('ben', state['agents'], 'senior director busy blocks review')
        # Off-duty admin also blocks.
        state = self._low_morale('ben')
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        state['agents']['faye']['offDuty'] = True
        self._run(state, self.decider_fire)
        self.assertIn('ben', state['agents'], 'off-duty admin blocks review')

    # --- 2026-09-23 morale decouple: firing keys off the firing SIGNAL, not
    # the composite morale score. Low morale from neglect alone (no negative
    # report, no drop-off) makes an agent a HELP/hiring target, not a firing
    # one. Dropped-work overload alone IS a signal.

    def test_neglect_only_low_morale_is_not_a_firing_candidate(self):
        # Ben is low-morale from pure neglect (never contacted, no approved
        # work, no drops, no reports). Old code: morale<50 -> firing target.
        # New code: morale is load-spreading signal; firing needs a real
        # report or drop-off, so this agent must NOT come up for review.
        state = self._low_morale('ben')
        state['agents']['ben'].update({'droppedCount': 0, 'approvedCount': 0})
        state['reports'] = []
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        called = []
        def decider(*a, **k):
            called.append(1)
            return 'fire'
        self._run(state, decider)
        self.assertIn('ben', state['agents'], 'neglect-only low morale must not fire')
        self.assertEqual(called, [], 'no firing review at all for a neglect-only agent')

    def test_dropoff_alone_is_a_firing_signal(self):
        # Handed work and dropped more than a third of what was approved is a
        # genuine overload/failure signal on its own -- a firing candidate
        # even with zero reports. (A severe report is present so guardrail B
        # doesn't defer.) Mirrors _has_firing_signal's dropped>approved*0.3.
        state = self._low_morale('ben')  # droppedCount 10, approvedCount 0
        state['reports'] = [{'aboutId': 'ben', 'fromId': 'ada',
                             'quote': 'kept dropping assigned work',
                             'severity': 'severe', 'note': 'overloaded'}]
        _idle_with_work(state)
        state['lastFiringReviewAt'] = 0
        state['lastHireAt'] = 0
        self._run(state, self.decider_fire)
        self.assertNotIn('ben', state['agents'],
                         'drop-off (handed work, dropped it) is a firing signal')


class WhoNeedsReview(unittest.TestCase):
    """who_needs_review picks the STRONGEST firing signal among candidates,
    not the first in roster order (2026-09-28 audit: the severity weighting
    was documented in a comment but never actually computed)."""

    def _idle(self, state):
        for a in state['agents'].values():
            a['busy'] = False
            a['task'] = None
            a['pairWith'] = None
            a['handoff'] = None
        return state

    def test_severity_weighted_report_beats_dropoff_only(self):
        state = _seed()
        self._idle(state)
        # ada: a severe report filed against her (weight 3) but no overload.
        state['reports'] = [{'aboutId': 'ada', 'fromId': 'ben',
                             'quote': 'botched a critical task',
                             'severity': 'severe', 'note': 'left it broken'}]
        # ben: no reports, but a real drop-off (dropped 10, approved 0) --
        # strength (0, 10) under the tuple sort.
        state['agents']['ben'].update({'droppedCount': 10, 'approvedCount': 0})
        pick = sim.who_needs_review(state, now_ms=10 ** 12)
        self.assertEqual(pick.get('id'), 'ada',
                         'a severe report must outrank an unreported overload')

    def test_more_reports_outrank_fewer(self):
        state = _seed()
        self._idle(state)
        # ada: two serious reports (2+2=4). ben: one severe (3). 4 > 3.
        state['reports'] = [
            {'aboutId': 'ada', 'fromId': 'ben', 'quote': 'a', 'severity': 'serious', 'note': 'x'},
            {'aboutId': 'ada', 'fromId': 'nora', 'quote': 'b', 'severity': 'serious', 'note': 'x'},
            {'aboutId': 'ben', 'fromId': 'ada', 'quote': 'c', 'severity': 'severe', 'note': 'x'},
        ]
        pick = sim.who_needs_review(state, now_ms=10 ** 12)
        self.assertEqual(pick.get('id'), 'ada')

    def test_overload_tiebreaks_equal_report_weight(self):
        state = _seed()
        self._idle(state)
        # Both have one severe report (weight 3); ada is far more overloaded.
        state['reports'] = [
            {'aboutId': 'ada', 'fromId': 'ben', 'quote': 'a', 'severity': 'severe', 'note': 'x'},
            {'aboutId': 'ben', 'fromId': 'ada', 'quote': 'b', 'severity': 'severe', 'note': 'x'},
        ]
        state['agents']['ada'].update({'droppedCount': 100, 'approvedCount': 10})
        state['agents']['ben'].update({'droppedCount': 10, 'approvedCount': 10})
        pick = sim.who_needs_review(state, now_ms=10 ** 12)
        self.assertEqual(pick.get('id'), 'ada',
                         'most-overloaded wins among equally-reported candidates')


class FiringFallback(unittest.TestCase):
    """Jev-outage fallback in _fire_decision requires BOTH a negative report
    AND a genuine drop-off to fire -- either alone stays 'keep' (2026-09-28
    audit: the OR inside _has_firing_signal collapsed the AND to fire on
    drop-off alone, contradicting the documented rule)."""

    def _state(self, reports, dropped, approved):
        state = _seed()
        state['reports'] = reports
        state['agents']['ben'].update({'droppedCount': dropped,
                                       'approvedCount': approved})
        reviewers = [next(d for d in state['agentRoster'] if d['id'] == rid)
                     for rid in ('faye', 'nora')]
        return state, reviewers

    def _outage_decision(self, state, reviewers):
        return sim._fire_decision(state, reviewers, {'id': 'ben'}, 10 ** 12,
                                  decider=lambda *a, **k: None)[0]

    def test_dropoff_alone_stays_keep(self):
        state, reviewers = self._state(reports=[], dropped=10, approved=0)
        self.assertEqual(self._outage_decision(state, reviewers), 'keep')

    def test_report_alone_stays_keep(self):
        state, reviewers = self._state(
            reports=[{'aboutId': 'ben', 'fromId': 'ada', 'quote': 'x',
                      'severity': 'severe', 'note': 'n'}],
            dropped=0, approved=10)
        self.assertEqual(self._outage_decision(state, reviewers), 'keep')

    def test_report_and_dropoff_fires(self):
        state, reviewers = self._state(
            reports=[{'aboutId': 'ben', 'fromId': 'ada', 'quote': 'x',
                      'severity': 'severe', 'note': 'n'}],
            dropped=10, approved=0)
        self.assertEqual(self._outage_decision(state, reviewers), 'fire')


if __name__ == '__main__':
    unittest.main(verbosity=2)