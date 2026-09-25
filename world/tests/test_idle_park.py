"""Tests for parking idle (on-duty-but-unoccupied) agents off duty.

2026-09-23: a woken-but-never-assigned agent lingers on-duty and visible
forever (nothing re-parks it until a task completes). The player's rule is
"unless an agent has scheduled work or is active, they should not appear."
_park_idle_wanderers sends every fully-idle, on-duty, non-admin agent back
off duty so its sprite vanishes in place; assignment re-wakes on demand.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim


def _state():
    def agent(aid, **kw):
        base = {'id': aid, 'x': 0, 'y': 0, 'dir': 'down', 'offDuty': False,
                'visible': True, 'busy': False, 'task': None, 'path': None,
                'pathIndex': 0, 'pathTarget': None, 'pathActive': False,
                'pairWith': None, 'handoff': None, 'inRoom': None}
        base.update(kw)
        return base

    return {
        'agentRoster': [
            {'id': 'faye', 'isAdmin': True},
            {'id': 'ada'},
            {'id': 'ben'},
            {'id': 'owen'},
        ],
        'agents': {
            'faye': agent('faye', isAdmin=True),
            'ada': agent('ada'),
            'ben': agent('ben'),
            'owen': agent('owen'),
        },
        # A genuinely quiet village: no standing work due (skill review stamped
        # at now), no research topics, no pending hire/onboard, no tasks.
        'researchTopics': [],
        'tasks': {},
    }


_IDLE_AT_MS = 1_725_000_000_000  # matches _task_cycle(now=1_725_000_000.0)


def _quiet(state):
    """Stamp a village as having NOTHING due so the task cycle doesn't enqueue
    work in the same pass (a standing skill-review sweep would otherwise queue
    a task, keeping agents on duty for it -- which is correct, but not what
    these tests are isolating)."""
    state['sim'] = {'owner': 'server'}
    state['workQueue'] = []
    state['lastSkillReviewAt'] = _IDLE_AT_MS
    state['lastDistillAt'] = _IDLE_AT_MS  # same: sweep must not enqueue work
    state['lastHireAt'] = _IDLE_AT_MS - 1  # recent hire keeps auto-hire quiet
    return state


class IdlePark(unittest.TestCase):
    def test_parks_all_fully_idle_non_admin_agents(self):
        state = _state()
        n = sim._park_idle_wanderers(state)
        # faye is admin -> stays on duty; ada/ben/owen are idle -> parked.
        self.assertEqual(n, 3)
        self.assertTrue(state['agents']['ada']['offDuty'])
        self.assertFalse(state['agents']['ada']['visible'])
        self.assertTrue(state['agents']['owen']['offDuty'])
        self.assertTrue(state['agents']['faye']['offDuty'] is False)
        self.assertTrue(state['agents']['faye']['visible'])

    def test_skips_busy_task_pair_handoff_and_room_agents(self):
        state = _state()
        state['agents']['ada']['task'] = 't-1'
        state['agents']['ben']['pairWith'] = 'owen'
        state['agents']['owen']['inRoom'] = 'pressoffice'
        n = sim._park_idle_wanderers(state)
        self.assertEqual(n, 0, 'no genuinely idle agents -> nothing parked')
        for aid in ('ada', 'ben', 'owen'):
            self.assertIs(state['agents'][aid]['offDuty'], False)

    def test_already_off_duty_agents_are_untouched_and_idempotent(self):
        state = _state()
        state['agents']['ada']['offDuty'] = True
        state['agents']['ada']['visible'] = False
        first = sim._park_idle_wanderers(state)
        second = sim._park_idle_wanderers(state)
        self.assertEqual(first, 2)
        self.assertEqual(second, 0, 'idempotent: second pass parks nothing')
        self.assertIs(state['agents']['ada']['offDuty'], True)

    def test_empty_or_absent_roster_is_safe(self):
        self.assertEqual(sim._park_idle_wanderers({'agents': {}}), 0)
        self.assertEqual(sim._park_idle_wanderers({'agentRoster': []}), 0)
        self.assertEqual(sim._park_idle_wanderers({}), 0)

    def test_task_cycle_parks_idle_wanderers_on_an_empty_queue(self):
        # Integration: with nothing due and no one active, the server-side task
        # cycle routes every fully-idle on-duty non-admin agent off duty.
        state = _quiet(_state())
        out = sim._task_cycle(state, now=1_725_000_000.0,
                              grid=[], doors={}, task_id_holder=[0])
        self.assertTrue(out['agents']['ada']['offDuty'])
        self.assertFalse(out['agents']['ada']['visible'])
        self.assertTrue(out['agents']['owen']['offDuty'])
        # Admin stays on duty regardless.
        self.assertIs(out['agents']['faye']['offDuty'], False)

    def test_task_cycle_does_not_park_when_work_is_queued(self):
        # A queued item means the village is not idle -- the wanderers should
        # stay on duty to take it (they get picked round-robin by assignment).
        state = _quiet(_state())
        state['workQueue'] = [{'title': 'weather', 'room': 'weatherstation',
                               'id': 'w1', 'notBefore': 0, 'dueByMs': 0}]
        out = sim._task_cycle(state, now=1_725_000_000.0,
                              grid=[], doors={}, task_id_holder=[0])
        for aid in ('ada', 'ben', 'owen'):
            self.assertIs(out['agents'][aid]['offDuty'], False,
                          f'{aid} should stay on duty while work is queued')


if __name__ == '__main__':
    unittest.main()