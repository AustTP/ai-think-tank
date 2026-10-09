"""Player mail to the admin surfaces: /api/mail/send pushes the message through
the same request routing the Telegram bridge uses (so the admin is dispatched
live or the request is filed as work), instead of a mailbox note the admin
never reads. The mailbox entry stays as the durable record; the admin's reply
lands in the player's inbox.

Hermetic: state read, the routing call, and the player session are mocked;
all writes go to a throwaway temp DB.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-admin-mail-test-')
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


def _roster():
    return [
        {'id': 'theo', 'name': 'Theo', 'role': 'Control Room', 'isAdmin': True},
        {'id': 'ben', 'name': 'Ben', 'role': 'Banking'},
        {'id': 'cora', 'name': 'Cora', 'role': 'Post Office'},
    ]


def _state(**over):
    roster = _roster()
    state = {
        'agentRoster': roster,
        'agents': {a['id']: {'id': a['id'], 'name': a['name'], 'role': a['role'],
                             'offDuty': False, 'busy': False, 'task': None,
                             'pairWith': None, 'mailbox': []}
                   for a in roster},
        'playerInbox': [],
        'workQueue': [],
        'tasks': {},
    }
    state.update(over)
    return state


class AdminMailPush(unittest.TestCase):
    def _client(self, state):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        state_patch = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        return c

    def test_player_mail_to_admin_routes_and_delivers_reply(self):
        # Player mails the admin -> routing is invoked AND Theo's live reply is
        # filed into the player's inbox (alongside the durable mailbox record).
        s = _state()
        c = self._client(s)
        with unittest.mock.patch.object(serve, '_route_player_request',
                                        new=unittest.mock.AsyncMock(
                                            return_value={'reply': 'On it -- checking all 13 services now.',
                                                          'agent': 'theo'})) as route:
            r = c.post('/api/mail/send', json={
                'fromId': 'player', 'toId': 'theo', 'text': 'check all the APIs'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])
        self.assertEqual(route.call_count, 1)
        called_state, called_text = route.call_args.args
        self.assertIs(called_state, s)
        self.assertEqual(called_text, 'check all the APIs')
        # Durable mailbox record still exists (from the player).
        theo_mail = s['agents']['theo']['mailbox']
        self.assertEqual(len(theo_mail), 1)
        self.assertTrue(theo_mail[0]['text'].startswith('You: '))
        # Theo's reply is in the player's inbox.
        self.assertEqual(len(s['playerInbox']), 1)
        self.assertEqual(s['playerInbox'][0]['answer'], 'On it -- checking all 13 services now.')
        self.assertEqual(s['playerInbox'][0]['agentId'], 'theo')

    def test_player_mail_to_non_admin_does_not_route(self):
        # Workers read their mailboxes during content tasks, so only the admin
        # (who never runs content work) gets the routing push.
        s = _state()
        c = self._client(s)
        with unittest.mock.patch.object(serve, '_route_player_request',
                                        new=unittest.mock.AsyncMock(
                                            return_value={'reply': 'x', 'agent': 'ben'})) as route:
            r = c.post('/api/mail/send', json={
                'fromId': 'player', 'toId': 'ben', 'text': 'heads up'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(route.call_count, 0)
        self.assertEqual(len(s['agents']['ben']['mailbox']), 1)
        self.assertEqual(s['playerInbox'], [])

    def test_admin_mail_parked_by_routing_leaves_no_duplicate_inbox_entry(self):
        # When the admin is busy the routing parks the ask (queued) and the
        # standing ask-drain delivers the answer later -- so the mail push must
        # NOT create its own inbox entry that would duplicate the drain's.
        s = _state()
        c = self._client(s)
        with unittest.mock.patch.object(serve, '_route_player_request',
                                        new=unittest.mock.AsyncMock(
                                            return_value={'queued': True, 'askId': 'ask-1'})) as route:
            r = c.post('/api/mail/send', json={
                'fromId': 'player', 'toId': 'theo', 'text': 'check the APIs'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(route.call_count, 1)
        self.assertEqual(len(s['agents']['theo']['mailbox']), 1)  # record kept
        self.assertEqual(s['playerInbox'], [])  # no premature answer


if __name__ == '__main__':
    unittest.main()