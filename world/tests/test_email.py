"""Tests for real player-notification email. An action-needed event
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
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402
import serve  # noqa: E402

_NOW_MS = 1_725_000_000_000
_TMP_DIR = None
_PATCHER = None


def setUpModule():
    # Gap (same class as test_composite_trust.py's own
    # fix): this file was never DB-isolated -- FailClosed.
    # test_send_without_credential_fails_closed calls serve._delete_credential
    # directly, which would delete a REAL player's provisioned Gmail
    # credential if this ever ran against a live think_tank.db. Also not in
    # tests/run_all.sh, so it had never actually been exercised as part of
    # "the test suite" at all until now.
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-email-test-')
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
        # Peer review is agent-to-agent: entering review notifies
        # the picked reviewers via their MAILBOX, not the player -- the player
        # only hears about a story when it actually SHIPS (story_done). This
        # asserts both halves: the reviewers got their mailbox notice, and no
        # player email fired merely for entering review.
        st = _state()
        st['agents'].setdefault('priya', {})['offDuty'] = False
        task = {'id': 'task-r1', 'title': 'Author a research brief',
                'room': 'pressoffice', 'projectLabel': 'PR-1',
                'assignedTo': 'nadia', 'status': 'working', 'workUntil': 0}
        gate = sim._enter_peer_review(st, task, _NOW_MS)
        self.assertIsNotNone(gate)
        # Reviewers were notified in-band (mailbox), not via a player email.
        reviewer_mailboxes = [st['agents'][r].get('mailbox', []) for r in gate.get('reviewerIds', [])]
        self.assertTrue(any('peer_review_request' in (m.get('kind') if isinstance(m, dict) else m)
                            for mb in reviewer_mailboxes for m in mb),
                        'reviewers get a mailbox peer_review_request on entry')
        # No player email was queued just for entering review.
        self.assertEqual(st.get('emailOutbox', []), [])


class EndpointAuth(unittest.TestCase):
    """Gap: /api/player-email/credential's handler
    checks _resolve_requester to reject an agent that explicitly self-
    identifies (?requesterId=<id>), but the path was missing from
    AUTH_PROTECTED_PREFIXES entirely -- that check was the ONLY gate, so a
    request with NO session cookie and NO agent key at all reached the
    handler and silently overwrote the player's real credential. Confirmed
    live against a real think tank before this fix existed. Two-layer shape,
    matching /api/keys: the middleware must see SOME valid auth first."""

    def _client(self):
        from starlette.testclient import TestClient
        return TestClient(serve.app)

    def test_unauthenticated_request_is_rejected(self):
        c = self._client()  # no session cookie, no X-Agent-Key at all
        r = c.post('/api/player-email/credential', json={'appPassword': 'abcdefghijklmnop'})
        self.assertEqual(r.status_code, 401)

    def test_authenticated_agent_still_gets_player_only_rejection(self):
        # The middleware's OWN auth check (any real agent key) is not the
        # same thing as being the player -- the handler's _resolve_requester
        # check must still fire on top of it when the caller explicitly
        # self-identifies as an agent.
        agent_id = 'test-endpointauth-agent'
        key = serve.get_or_create_agent_key(agent_id)
        c = self._client()
        r = c.post(f'/api/player-email/credential?requesterId={agent_id}',
                   json={'appPassword': 'abcdefghijklmnop'},
                   headers={'X-Agent-Key': key})
        self.assertEqual(r.status_code, 403)

    def test_real_session_is_accepted(self):
        c = self._client()
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        r = c.post('/api/player-email/credential', json={'appPassword': 'not-16-chars'})
        # Reaches the handler (past auth) and fails on format, not auth --
        # proves a real session is NOT blocked by this fix.
        self.assertEqual(r.status_code, 400)
        self.assertIn('app-password', r.json().get('error', ''))


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


class PlayerEmailDisabled(unittest.TestCase):
    """Telegram only, no more email. Same
    kill-switch shape as AGENT_BROWSING_ENABLED -- flips the send off without
    touching the credential/outbox machinery."""

    def test_disabled_short_circuits_even_with_a_real_credential(self):
        serve._store_credential(serve._GMAIL_CRED_NAME, 'Gmail SMTP (player notifications)', 'abcd efgh ijkl mnop')
        self.addCleanup(serve._delete_credential, serve._GMAIL_CRED_NAME)
        with unittest.mock.patch.object(serve, 'PLAYER_EMAIL_ENABLED', False):
            self.assertIs(serve.send_player_email_sync('subj', 'body'), False)

    def test_enabled_still_reaches_the_credential_check(self):
        # Not disabled -> falls through to the real logic, which fails
        # closed for a different reason (no credential) -- proves the
        # toggle isn't accidentally short-circuiting everything.
        serve._delete_credential(serve._GMAIL_CRED_NAME)
        with unittest.mock.patch.object(serve, 'PLAYER_EMAIL_ENABLED', True):
            self.assertIs(serve.send_player_email_sync('subj', 'body'), False)


if __name__ == '__main__':
    unittest.main()