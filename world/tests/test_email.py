"""Tests for real player-notification email (2026-09-25). An action-needed event
(agent asks a question / a card gets blocked / a story lands for review) queues a
one-way notification that serve.py drains and emails to the player via SMTP using
a vault-held Gmail app-password.

Covers the hermetic sim.py side (queueing + dedupe + fail-closed drain) and the
serve.py fail-closed sender. NO network is touched: the sender is exercised only
with no credential provisioned, which must fail closed (return False, never
raise). A real send is only testable once the player provisions the live
app-password, so the endpoint's self-test is the live-verification path.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402
import serve  # noqa: E402

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
        'agentRoster': [{'id': 'sm', 'isAdmin': True, 'director': None},
                        {'id': 'nadia', 'isAdmin': False, 'director': 'dev'},
                        {'id': 'priya', 'isAdmin': False, 'director': 'dev'}],
        'researchTopics': [],
        'tasks': {},
        'workQueue': [],
        'issues': {},
        'playerInbox': [],
    }
    base.update(over)
    return base


def _file(state, summary='Ship the reports API', t='story'):
    return sim.file_issue(state, 'dev', t, summary, 'checkout', 'priya',
                          now_ms=_NOW_MS)


class QueuePlayerEmail(unittest.TestCase):
    def test_queues_entry(self):
        st = _state()
        e = sim._queue_player_email(st, 'card_blocked', 'Subject here', 'body',
                                    now_ms=_NOW_MS)
        self.assertIsNotNone(e)
        self.assertEqual(len(st['emailOutbox']), 1)
        self.assertEqual(st['emailOutbox'][0]['kind'], 'card_blocked')

    def test_dedupes_same_kind_within_window(self):
        st = _state()
        sim._queue_player_email(st, 'card_blocked', 'a', 'body', now_ms=_NOW_MS)
        dupe = sim._queue_player_email(st, 'card_blocked', 'b', 'body',
                                       now_ms=_NOW_MS + 5_000)  # within 30s
        self.assertIsNone(dupe)
        diff = sim._queue_player_email(st, 'agent_ask', 'c', 'body',
                                       now_ms=_NOW_MS + 5_000)
        self.assertIsNotNone(diff)  # different kind bypasses the dedupe
        self.assertEqual(len(st['emailOutbox']), 2)

    def test_allows_after_window(self):
        st = _state()
        sim._queue_player_email(st, 'card_blocked', 'a', 'body', now_ms=_NOW_MS)
        late = sim._queue_player_email(st, 'card_blocked', 'b', 'body',
                                       now_ms=_NOW_MS + 60_000)
        self.assertIsNotNone(late)


class TriggerSites(unittest.TestCase):
    def test_player_ask_queues_email(self):
        st = _state()
        issue = _file(st)
        sim._deliver_player_ask(st, issue['key'], 'nadia', 'Should I merge now?',
                                'the release gate')
        self.assertEqual(st['emailOutbox'][0]['kind'], 'agent_ask')
        self.assertIn(issue['key'], st['emailOutbox'][0]['subject'])

    def test_block_commit_queues_email(self):
        st = _state()
        issue = _file(st)
        sim.request_block_dependency(st, issue['key'], 'nadia', 'task-9',
                                     'waiting on migrations', now_ms=_NOW_MS)
        sim._block_step(st, 0, _NOW_MS + 10_000)
        kinds = [e['kind'] for e in st['emailOutbox']]
        self.assertIn('card_blocked', kinds)

    def test_peer_review_queues_email(self):
        st = _state()
        st['agents'].setdefault('priya', {})['offDuty'] = False
        task = {'id': 'task-r1', 'title': 'Author a research brief',
                'room': 'pressoffice', 'projectLabel': 'PR-1',
                'assignedTo': 'nadia', 'status': 'working', 'workUntil': 0}
        gate = sim._enter_peer_review(st, task, _NOW_MS)
        self.assertIsNotNone(gate)
        kinds = [e['kind'] for e in st['emailOutbox']]
        self.assertIn('story_needs_review', kinds)


class FailClosed(unittest.TestCase):
    def test_send_without_credential_fails_closed(self):
        # No gmail_smtp credential provisioned (fresh vault in real use if never
        # set; here we delete any leftover + guarantee absent).
        serve._delete_credential(serve._GMAIL_CRED_NAME)
        # Must return False, NOT raise.
        self.assertIs(serve.send_player_email_sync('subj', 'body'), False)

    def test_drain_empties_outbox_without_raising(self):
        st = _state()
        sim._queue_player_email(st, 'card_blocked', 'subj', 'body', now_ms=_NOW_MS)
        # No credential -> send fails closed + outbox cleared.
        results = sim._drain_email_outbox_sync(st)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], 'card_blocked')
        self.assertEqual(st['emailOutbox'], [])

    def test_app_password_validation(self):
        self.assertTrue(serve._looks_like_gmail_app_password('abcd efgh ijkl mnop'))
        self.assertTrue(serve._looks_like_gmail_app_password('abcdefghijklmnop'))
        self.assertFalse(serve._looks_like_gmail_app_password('short'))
        self.assertFalse(serve._looks_like_gmail_app_password('ab cd ef gh ij kl mn opX'))


if __name__ == '__main__':
    unittest.main()