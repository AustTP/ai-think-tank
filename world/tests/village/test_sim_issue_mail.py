"""Tests for the server-side simulation engine (world/sim.py).

Mirrors the ServerOwnedSeed isolation pattern from test_serve.py: every test
redirects state/disk side effects into a throwaway temp directory so the real
think_tank.db is never touched.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.config as config
import serve
import sim


class IssueReporterMail(unittest.TestCase):
    """BP-62: the owning side closes the loop with the reporter through quiet
    mailbox notes. set_issue_status mails the reporter on started/done/closed
    transitions; cross-team works because reporter and owning team are
    independent fields; the player is skipped; same-status no-ops stay quiet."""

    def _state(self):
        return {
            'agentRoster': [{'id': 'rep', 'name': 'Repa', 'role': 'Research'},
                            {'id': 'xi', 'name': 'Xi', 'role': 'Banking'}],
            'agents': {
                'rep': {'id': 'rep', 'inRoom': 'pressoffice', 'offDuty': False},
                'xi': {'id': 'xi', 'inRoom': 'observatory', 'offDuty': False},
            },
            'teams': [{'id': 't1', 'name': 'Fixers', 'directorId': 'faye',
                       'scrumMasterId': 'ada'}],
            'issues': {},
            'issueCounters': {},
            'backlogRequests': [],
        }

    def _issue(self, reporter='rep', team='t1'):
        state = self._state()
        issue = sim.file_issue(state, team, 'story', 'Add dark mode',
                               'Sh Theme', reporter)
        return state, issue

    def test_started_done_and_closed_mail_the_reporter(self):
        state, issue = self._issue()
        sim.set_issue_status(state, issue['key'], 'in_progress')
        sim.set_issue_status(state, issue['key'], 'done')
        sim.set_issue_status(state, issue['key'], 'closed')
        mail = state['agents']['rep'].get('mailbox') or []
        kinds = [m['kind'] for m in mail]
        self.assertEqual(kinds, ['issue_started', 'issue_done', 'issue_closed'])
        for m in mail:
            self.assertIn(issue['key'], m['text'])
            self.assertIn('Add dark mode', m['text'])
        self.assertIn('Fixers', mail[0]['text'])
        self.assertFalse(mail[0].get('read'))

    def test_same_status_transition_mails_nothing(self):
        state, issue = self._issue()
        sim.set_issue_status(state, issue['key'], 'in_progress')
        sim.set_issue_status(state, issue['key'], 'in_progress')
        self.assertEqual(len(state['agents']['rep']['mailbox']), 1)

    def test_player_and_unknown_reporters_are_skipped(self):
        state, issue = self._issue(reporter='player')
        sim.set_issue_status(state, issue['key'], 'done')
        self.assertFalse(state['agents']['rep'].get('mailbox'))
        state2, issue2 = self._issue(reporter='ghost')
        sim.set_issue_status(state2, issue2['key'], 'done')  # must not crash
        self.assertTrue(state2['issues'][issue2['key']]['status'] == 'done')


class IssueDuplicateStamp(unittest.TestCase):
    """file_issue stamps an exact-title match against an OPEN card in the same
    team with duplicateOf instead of rejecting: the record survives, the
    scrum master sees why, and a decline can name the real key. Cross-team
    same-title cards stay unlinked; closed originals never suppress."""

    def _state(self):
        return {
            'agentRoster': [{'id': 'rep', 'name': 'Repa', 'role': 'Research'}],
            'agents': {'rep': {'id': 'rep', 'inRoom': 'pressoffice', 'offDuty': False}},
            'teams': [{'id': 't1', 'name': 'Fixers', 'directorId': 'faye',
                       'scrumMasterId': 'ada'},
                      {'id': 't2', 'name': 'Builders', 'directorId': 'bo',
                       'scrumMasterId': 'sm2'}],
            'issues': {},
            'issueCounters': {},
            'backlogRequests': [],
        }

    def _file(self, state, title, team='t1'):
        issue = sim.file_issue(state, team, 'story', 'Add dark mode', 'Sh Theme',
                               'rep', title=title)
        return state, issue

    def test_open_same_title_case_and_space_insensitive(self):
        state, first = self._file(self._state(), 'Add dark mode')
        _, second = self._file(state, '  add  DARK mode ')
        self.assertEqual(second['duplicateOf'], first['key'])

    def test_first_card_carries_no_stamp(self):
        state, first = self._file(self._state(), 'Add dark mode')
        self.assertIsNone(first['duplicateOf'])

    def test_other_titles_and_teams_and_closed_originals_are_clean(self):
        state, first = self._file(self._state(), 'Add dark mode')
        _, rename = self._file(state, 'Add light mode')
        self.assertIsNone(rename['duplicateOf'])
        _, cross = self._file(state, 'Add dark mode', team='t2')
        self.assertIsNone(cross['duplicateOf'])
        sim.set_issue_status(state, first['key'], 'closed')
        _, refill = self._file(state, 'Add dark mode')
        self.assertIsNone(refill['duplicateOf'])


class CardAppealMailReply(unittest.TestCase):
    """BP-62 appeal path: the reporter of a declined card can reply in mail
    with new information; the reply re-surfaces the card exactly once into the
    owning team's next refinement ceremony. Only the reporter, only a truly
    rejected card, addressed to the owning team's scrum master; a bystander
    citing the key, a second reply, or a pending/accepted card never
    triggers one."""

    def _state(self, req_status='rejected'):
        return {
            'agentRoster': [{'id': 'rep', 'name': 'Repa', 'role': 'Research'},
                            {'id': 'ada', 'name': 'Ada', 'role': 'SM'},
                            {'id': 'xi', 'name': 'Xi', 'role': 'Worker'}],
            'agents': {
                'rep': {'id': 'rep', 'inRoom': 'pressoffice', 'offDuty': False},
                'ada': {'id': 'ada', 'inRoom': 'command', 'offDuty': False},
                'xi': {'id': 'xi', 'inRoom': 'observatory', 'offDuty': False},
            },
            'teams': [{'id': 't1', 'name': 'Fixers', 'directorId': 'faye',
                       'scrumMasterId': 'ada'},
                      {'id': 't2', 'name': 'Builders', 'directorId': 'bo',
                       'scrumMasterId': 'sm2'}],
            'issues': {'T1-1': {'key': 'T1-1', 'teamId': 't1',
                                'title': 'Add dark mode', 'status': 'open'}},
            'issueCounters': {},
            'backlogRequests': [{'id': 'iss-T1-1', 'issueKey': 'T1-1',
                                 'status': req_status, 'filedBy': 'rep',
                                 'teamId': 't1', 'title': 'Add dark mode'}],
        }

    def test_reporter_reply_resurfaces_the_card_once(self):
        state = self._state()
        self.assertTrue(sim._register_card_appeal(
            state, 'rep', 'T1-1', 'I checked again: it also breaks on Safari, not just on mobile.'))
        req = state['backlogRequests'][0]
        self.assertEqual(req['status'], 'pending')
        self.assertTrue(req['appealedAt'])
        self.assertIn('Safari', req['appeal'])
        mail = state['agents']['ada']['mailbox'][0]
        self.assertEqual(mail['kind'], 'issue_appeal')
        self.assertEqual(mail['about'], 'T1-1')
        self.assertIn('Safari', mail['text'])
        self.assertFalse(state['agents']['xi'].get('mailbox'))

    def test_bystander_and_second_and_nondeclined_are_refused(self):
        state = self._state()
        self.assertFalse(sim._register_card_appeal(state, 'xi', 'T1-1', 'me too'))
        self.assertTrue(sim._register_card_appeal(state, 'rep', 'T1-1', 'new info'))
        self.assertFalse(sim._register_card_appeal(state, 'rep', 'T1-1', 'again'))
        fresh = self._state(req_status='pending')
        self.assertFalse(sim._register_card_appeal(fresh, 'rep', 'T1-1', 'nope'))
        # An appeal only lands when the reply is addressed to the OWNING
        # team's scrum master -- a reply to anyone else stays quiet.
        wrong_sm = self._state(req_status='rejected')
        self.assertFalse(sim._register_card_appeal(wrong_sm, 'rep', 'T1-1', 'x', via_sm='sm2'))

    def test_unknown_issue_key_is_refused(self):
        state = self._state()
        self.assertFalse(sim._register_card_appeal(state, 'rep', 'T1-99', 'hi'))

    def test_player_and_empty_reporters_and_empty_key_are_refused(self):
        state = self._state()
        self.assertFalse(sim._register_card_appeal(state, 'player', 'T1-1', 'me'))
        self.assertFalse(sim._register_card_appeal(state, '', 'T1-1', 'me'))
        self.assertFalse(sim._register_card_appeal(state, 'rep', '  ', 'me'))

    def test_missing_issue_and_missing_sm_agent_are_refused(self):
        no_issue = self._state()
        del no_issue['issues']['T1-1']
        self.assertFalse(sim._register_card_appeal(no_issue, 'rep', 'T1-1', 'x'))
        no_sm = self._state()
        del no_sm['agents']['ada']
        self.assertFalse(sim._register_card_appeal(no_sm, 'rep', 'T1-1', 'x'))

    def test_serve_replies_find_the_named_card_key(self):
        state = self._state()
        serve._maybe_register_card_appeal(
            state, 'rep', 'Re T1-1: it also breaks on Safari, and my other card T2-99 too.',
            'ada')
        req = state['backlogRequests'][0]
        self.assertEqual(req['status'], 'pending')
        self.assertIn('Safari', req['appeal'])
