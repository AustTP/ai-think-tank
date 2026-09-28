"""Tests for wake-on-mail (2026-09-25): an off-duty agent woken by action-needed
mail must come online, route to the referenced work, act, and only then return
offline -- never parked-idle in the same tick before acting.

Covers the pure sim.py helpers:

1. `_deliver_mail` to an OFF-DUTY agent wakes her (offDuty False, visible True,
   `_mailAwake` set); to an on-duty agent it only files the mailbox note.
2. Park guard: `_park_idle_wanderers` skips a woken-but-unacted mail agent (holds
   `_mailAwake`), so she is not vanished before she acts; once acting (clear) she
   is parked when idle.
3. `_mail_action_step` routes a woken agent to her referenced card, pinning the
   resume work to HER (not round-robin), then clears the marker once assigned.
4. Dependency-landed wake NAMES the blocking story (the completed task's title)
   and does NOT name a bare task id; and the LOOP GUARD wakes no agent who is
   STILL blocked on another story's unmet dependency.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402

_NOW_MS = 1_725_000_000_000


def _state(**over):
    base = {
        'sim': {'owner': 'server'},
        'teamBlockAt': {},
        'agents': {
            'sm': {'id': 'sm', 'name': 'SM', 'busy': False, 'offDuty': False},
            'nadia': {'id': 'nadia', 'name': 'Nadia', 'busy': False, 'offDuty': False},
            'priya': {'id': 'priya', 'name': 'Priya', 'busy': False, 'offDuty': False},
        },
        'teams': [{'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev',
                   'scrumMasterId': 'sm', 'members': ['nadia', 'priya'],
                   'createdAt': 1}],
        'agentRoster': [{'id': 'sm', 'isAdmin': True, 'isDirector': False},
                        {'id': 'nadia', 'isAdmin': False, 'isDirector': False},
                        {'id': 'priya', 'isAdmin': False, 'isDirector': False}],
        'researchTopics': [],
        'tasks': {},
        'workQueue': [],
        'issues': {},
    }
    base.update(over)
    return base


def _file(state, summary='Ship the reports API', t='story'):
    return sim.file_issue(state, 'dev', t, summary, 'checkout', 'priya',
                          now_ms=_NOW_MS)


def _pending_kinds(state, issue_key):
    return [c['kind'] for c in sim._pending_block_changes(state, issue_key)]


class DeliverMail(unittest.TestCase):
    def test_wakes_off_duty_recipient(self):
        st = _state()
        st['agents']['nadia']['offDuty'] = True
        st['agents']['nadia']['visible'] = False
        ok = sim._deliver_mail(st, 'nadia', 'player_answer',
                               {'issueKey': 'DEV-1',
                                'text': 'Player answered on DEV-1: go'})
        self.assertTrue(ok)
        self.assertIs(st['agents']['nadia']['offDuty'], False)
        self.assertIs(st['agents']['nadia']['visible'], True)
        self.assertTrue(st['agents']['nadia'].get('_mailAwake'))
        self.assertEqual(len(st['agents']['nadia']['mailbox']), 1)

    def test_on_duty_recipient_only_files_mail(self):
        st = _state()
        # nadia is awake (offDuty False); _deliver_mail must NOT set _mailAwake.
        ok = sim._deliver_mail(st, 'nadia', 'player_answer',
                               {'issueKey': 'DEV-1', 'text': 'answering'})
        self.assertTrue(ok)
        self.assertFalse(st['agents']['nadia'].get('_mailAwake'))
        self.assertEqual(len(st['agents']['nadia']['mailbox']), 1)

    def test_unknown_agent_returns_false(self):
        self.assertFalse(sim._deliver_mail(_state(), 'ghost', 'player_answer'))

    def test_mailbox_is_capped_to_keep_recent_entries(self):
        st = _state()
        for i in range(sim.MAILBOX_KEEP_COUNT + 25):
            sim._deliver_mail(st, 'nadia', 'player_answer',
                              {'issueKey': f'DEV-{i}', 'text': f'mail {i}'})
        mailbox = st['agents']['nadia']['mailbox']
        self.assertEqual(len(mailbox), sim.MAILBOX_KEEP_COUNT)
        self.assertEqual(mailbox[0]['text'], 'mail 25')
        self.assertEqual(mailbox[-1]['text'], f'mail {sim.MAILBOX_KEEP_COUNT + 24}')

    def test_peer_review_request_mailbox_is_trimmed_too(self):
        # Regression (2026-09-28 audit): MAILBOX_KEEP_COUNT was only enforced in
        # _deliver_mail -- the _enter_peer_review / _sim_notify_author / gate
        # re-pick append paths grew the mailbox unboundedly. Every append path
        # must trim to the same cap, so a busy reviewer's mailbox can't balloon.
        st = _state()
        author = st['agents']['nadia']
        for i in range(sim.MAILBOX_KEEP_COUNT + 25):
            sim._sim_notify_author(st, {'id': f'story-{i}',
                                        'assignedTo': 'nadia',
                                        'title': f'Story {i}'}, 'reviewer-x')
        mailbox = author['mailbox']
        self.assertEqual(len(mailbox), sim.MAILBOX_KEEP_COUNT)
        self.assertEqual(mailbox[0]['kind'], 'peer_review_rejected')
        self.assertEqual(mailbox[0]['about'], 'story-25')
        self.assertEqual(mailbox[-1]['about'], f'story-{sim.MAILBOX_KEEP_COUNT + 24}')


class ParkGuard(unittest.TestCase):
    def test_unacted_mail_agent_is_not_parked(self):
        st = _state()
        # Priya is also awake+idle in fixtures; take her offline first so the only
        # awake candidate is the mail-woken nadia.
        st['agents']['priya']['offDuty'] = True
        st['agents']['priya']['visible'] = False
        st['agents']['nadia']['offDuty'] = True
        st['agents']['nadia']['visible'] = False
        sim._deliver_mail(st, 'nadia', 'player_answer',
                          {'issueKey': 'DEV-1', 'text': 'go'})
        parked = sim._park_idle_wanderers(st)
        self.assertEqual(parked, 0)
        self.assertIs(st['agents']['nadia']['offDuty'], False)  # still awake

    def test_acted_mail_agent_is_parked_when_idle(self):
        st = _state()
        # Wake + give her a task so she acted, then clear marker; idle again.
        st['agents']['nadia']['offDuty'] = True
        st['agents']['nadia']['visible'] = False
        sim._deliver_mail(st, 'nadia', 'dependency_landed',
                          {'issueKey': 'DEV-1', 'text': 'unblocked'})
        st['agents']['nadia'].pop('_mailAwake', None)  # she acted/cleared
        parked = sim._park_idle_wanderers(st)
        self.assertGreaterEqual(parked, 1)
        self.assertIs(st['agents']['nadia']['offDuty'], True)


class MailActionStep(unittest.TestCase):
    def test_routes_woken_agent_to_card_and_pins_her(self):
        st = _state()
        issue = _file(st)
        st['agents']['nadia']['offDuty'] = True
        st['agents']['nadia']['visible'] = False
        sim._deliver_mail(st, 'nadia', 'player_answer',
                          {'issueKey': issue['key'], 'text': 'answering'})
        routed = sim._mail_action_step(st, 0, _NOW_MS + 1000)
        self.assertEqual(routed, 1)
        self.assertFalse(st['agents']['nadia'].get('_mailAwake'))
        # The queued resume work is pinned to nadia -- not round-robin.
        queued = [w for w in st['workQueue'] if w.get('_mailResume')]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]['assignedTo'], 'nadia')


class DependencyLandWake(unittest.TestCase):
    def _set_up_dependency_block(self):
        st = _state()
        issue = _file(st)
        sim.request_block_dependency(st, issue['key'], 'priya', 'task-5',
                                     'waiting on the pipeline rewrite',
                                     now_ms=_NOW_MS)
        sim._block_step(st, 0, _NOW_MS + 10_000)
        st['tasks']['task-5'] = {'id': 'task-5',
                                 'title': 'Rewrite the payment pipeline',
                                 'assignedTo': 'nadia', 'status': 'done'}
        st['agents']['priya']['offDuty'] = True
        st['agents']['priya']['visible'] = False
        return st, issue

    def test_auto_clear_wakes_waiting_agent_and_names_story(self):
        st, issue = self._set_up_dependency_block()
        cleared = sim._auto_clear_dependency_blocks(st, 'task-5',
                                                    now_ms=_NOW_MS + 20_000)
        self.assertEqual(cleared, 1)
        self.assertIs(st['agents']['priya']['offDuty'], False)  # woken
        priya_mail = [m for m in st['agents']['priya']['mailbox']
                      if m.get('kind') == 'dependency_landed']
        self.assertEqual(len(priya_mail), 1)
        # Names the blocking STORY (the completed task's title), not a bare task
        # id -- so the woken agent knows exactly what freed her up.
        self.assertIn('Rewrite the payment pipeline', priya_mail[0]['text'])
        self.assertNotIn('task 5 landed', priya_mail[0]['text'])

    def test_loop_guard_does_not_wake_agent_still_blocked_elsewhere(self):
        st, issue = self._set_up_dependency_block()
        # A SECOND story for priya, still blocked on an UNMET dependency (task-9).
        other = sim.file_issue(st, 'dev', 'story', 'A second card', 'checkout',
                               'priya', now_ms=_NOW_MS + 1)
        st.setdefault('_pendingBlockChanges', [])
        sim.request_block_dependency(st, other['key'], 'priya', 'task-9',
                                     'blocked on the migrations', now_ms=_NOW_MS + 2)
        sim._block_step(st, 0, _NOW_MS + 30_000)  # commit the second block
        # task-5 lands, but priya is STILL blocked on task-9 -> no wake.
        cleared = sim._auto_clear_dependency_blocks(st, 'task-5',
                                                    now_ms=_NOW_MS + 40_000)
        self.assertEqual(cleared, 1)
        self.assertIs(st['agents']['priya']['offDuty'], True)  # NOT woken
        self.assertFalse(st['agents']['priya'].get('_mailAwake'))


if __name__ == '__main__':
    unittest.main()