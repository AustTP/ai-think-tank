"""Agent phone endpoints (ask 13).

phone.py is pure and already tested on its own. These
tests cover the WIRING in serve: the X-Agent-Key
authenticated /api/phone routes (send, inbox, read,
read-all) that file chat into the recipient's ordinary
mailbox as phone-channel entries. The state reader is
patched to an in-memory roster, the save and the audit
log are patched out, and the DB is isolated -- no
model is called and no file is written.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import phone  # noqa: E402
import serve  # noqa: E402
import sim  # noqa: E402
import theo_policy  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-phone-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library',
                                    '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR,
                                         'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _state():
    # Plain roster agents resolve as workers, and
    # workers may send and read messages -- the phone
    # gate. Mailboxes start empty.
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'ada'},
            {'id': 'bee'},
        ],
        'agents': {
            'theo': {'id': 'theo', 'mailbox': []},
            'nora': {'id': 'nora', 'mailbox': []},
            'ada': {'id': 'ada', 'mailbox': []},
            'bee': {'id': 'bee', 'mailbox': []},
        },
    }


def _client():
    from starlette.testclient import TestClient
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME,
                  serve.create_session())
    return c


class _PhoneFixture(unittest.TestCase):
    """The agent-authenticated phone routes, over an
    in-memory state: _agent_requester's two checks are
    patched to authenticate `agent_id`, the state
    reader returns the fixture's roster, and the save
    and the audit log are patched out."""

    def setUp(self):
        self.state = _state()
        self.policy = theo_policy.TheoPolicy()
        self._patches = [
            unittest.mock.patch.object(
                serve, '_resolve_requester',
                return_value='ada'),
            unittest.mock.patch.object(
                serve, 'verify_agent_key',
                return_value=True),
            unittest.mock.patch.object(
                serve, '_theo_policy', self.policy),
            unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value=self.state),
            unittest.mock.patch.object(
                serve, 'save_state_to_db'),
            unittest.mock.patch.object(serve, 'log_action'),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _as(self, agent_id='ada', key_ok=True):
        self._patches[0].stop()
        self._patches[1].stop()
        resolve = unittest.mock.patch.object(
            serve, '_resolve_requester',
            return_value=agent_id)
        key = unittest.mock.patch.object(
            serve, 'verify_agent_key',
            return_value=key_ok)
        resolve.start()
        key.start()
        self.addCleanup(resolve.stop)
        self.addCleanup(key.stop)


class PhoneSend(_PhoneFixture):
    def test_unauthenticated_is_a_401(self):
        self._as(agent_id=None)
        response = _client().post('/api/phone/send',
                                   json={'recipientId':
                                         'bee',
                                         'text': 'hi'})
        self.assertEqual(response.status_code, 401)

    def test_the_player_session_is_not_an_agent(self):
        self._as(agent_id='player')
        response = _client().post('/api/phone/send',
                                   json={'recipientId':
                                         'bee',
                                         'text': 'hi'})
        self.assertEqual(response.status_code, 401)

    def test_an_agent_key_mismatch_is_a_403(self):
        self._as(key_ok=False)
        response = _client().post('/api/phone/send',
                                   json={'recipientId':
                                         'bee',
                                         'text': 'hi'})
        self.assertEqual(response.status_code, 403)

    def test_recipient_and_text_are_required(self):
        response = _client().post('/api/phone/send',
                                   json={'text': 'hi'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('recipientId',
                      response.json()['error'])
        response = _client().post(
            '/api/phone/send',
            json={'recipientId': 'bee'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('text', response.json()['error'])

    def test_send_files_a_phone_message(self):
        response = _client().post(
            '/api/phone/send',
            json={'recipientId': 'bee',
                  'text': 'blocked on the build'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        mid = body['messageId']
        self.assertRegex(mid, r'^m-[0-9a-f]{8}$')
        entry = self.state['agents']['bee']['mailbox'][0]
        self.assertEqual(entry['channel'], 'phone')
        self.assertEqual(entry['mid'], mid)
        self.assertEqual(entry['from'], 'ada')
        self.assertEqual(entry['to'], 'bee')
        self.assertEqual(entry['text'],
                         'blocked on the build')

    def test_an_unknown_recipient_is_a_404(self):
        response = _client().post(
            '/api/phone/send',
            json={'recipientId': 'ghost', 'text': 'hi'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error'],
                         'unknown recipient')

    def test_a_rate_limited_send_is_a_403(self):
        self._patches[2].stop()
        rate_limited = theo_policy.TheoPolicy(
            rate_limit=1)
        policy_patch = unittest.mock.patch.object(
            serve, '_theo_policy', rate_limited)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)
        first = _client().post(
            '/api/phone/send',
            json={'recipientId': 'bee', 'text': 'one'})
        second = _client().post(
            '/api/phone/send',
            json={'recipientId': 'bee', 'text': 'two'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 403)
        self.assertEqual(second.json()['error'],
                         'rate limit exceeded')
        self.assertEqual(
            len(self.state['agents']['bee']['mailbox']),
            1)


class PhoneInbox(_PhoneFixture):
    def test_lists_unread_phone_oldest_first(self):
        lane = phone.Phone(self.policy)
        lane.send(self.state, 'ada', 'bee', 'a')
        lane.send(self.state, 'nora', 'bee', 'b')
        sim._append_mailbox(self.state['agents']['bee'],
                            {'kind': 'review',
                             'channel': 'mail',
                             'text': 'not phone'})
        self._as(agent_id='bee')
        response = _client().get('/api/phone/inbox')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['agentId'], 'bee')
        self.assertEqual(
            [message['text'] for message
             in body['messages']],
            ['a', 'b'])

    def test_an_empty_inbox_is_not_an_error(self):
        self._as(agent_id='bee')
        response = _client().get('/api/phone/inbox')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['messages'], [])


class PhoneRead(_PhoneFixture):
    def _seed(self):
        lane = phone.Phone(self.policy)
        first = lane.send(self.state, 'ada', 'bee', 'a')
        second = lane.send(self.state, 'ada', 'bee', 'b')
        return first.message_id, second.message_id

    def test_read_returns_what_is_still_unread(self):
        first, second = self._seed()
        self._as(agent_id='bee')
        response = _client().post(
            '/api/phone/read',
            json={'messageId': first})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['agentId'], 'bee')
        self.assertEqual(
            [message['mid'] for message
             in body['messages']],
            [second])
        mailbox = self.state['agents']['bee']['mailbox']
        self.assertTrue(mailbox[0]['read'])
        self.assertFalse(mailbox[1]['read'])

    def test_message_id_is_required(self):
        self._as(agent_id='bee')
        response = _client().post('/api/phone/read',
                                   json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn('messageId',
                      response.json()['error'])

    def test_an_unknown_message_is_a_404(self):
        self._seed()
        self._as(agent_id='bee')
        response = _client().post(
            '/api/phone/read',
            json={'messageId': 'm-deadbeef'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error'],
                         'unknown message')


class PhoneReadAll(_PhoneFixture):
    def test_clears_the_phone_inbox(self):
        lane = phone.Phone(self.policy)
        lane.send(self.state, 'ada', 'bee', 'a')
        lane.send(self.state, 'nora', 'bee', 'b')
        self._as(agent_id='bee')
        response = _client().post('/api/phone/read-all')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['agentId'], 'bee')
        bee_mailbox = self.state['agents']['bee']['mailbox']
        for entry in bee_mailbox:
            self.assertTrue(entry['read'])


if __name__ == '__main__':
    unittest.main()
