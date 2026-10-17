"""Agent-to-agent mail without wake-ups, plus the phone-like unread
surface: read_mailbox gains an unreadOnly filter, a new send_mail tool
delivers into a mailbox WITHOUT waking the recipient (the server-side
/api/mail/send path appends through the pure mailbox writer, no
mailAwake marker, no dispatch), and mark_mailbox_read clears the
unread pile (own mailbox always; another's for the Post Office, a
director, or the admin -- the same gate as read_mailbox).

Hermetic: state reads/writes and the loopback HTTP helper are mocked;
endpoint tests run against a throwaway temp DB.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import sim  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-messaging-test-')
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
        {'id': 'dax', 'name': 'Dax', 'role': 'Banking'},
    ]


def _state():
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
    state['agents']['ben']['director'] = 'dax'
    return state


class AppendMailboxStamp(unittest.TestCase):
    def test_stamps_unread_by_default(self):
        agent = {}
        sim._append_mailbox(agent, {'kind': 'agent_mail', 'text': 'hi'})
        self.assertEqual(len(agent['mailbox']), 1)
        self.assertFalse(agent['mailbox'][0]['read'])

    def test_preserves_explicit_read(self):
        agent = {}
        sim._append_mailbox(agent, {'text': 'already seen', 'read': True})
        self.assertTrue(agent['mailbox'][0]['read'])

    def test_trims_to_keep_count(self):
        agent = {}
        for i in range(sim.MAILBOX_KEEP_COUNT + 5):
            sim._append_mailbox(agent, {'text': f'm{i}'})
        self.assertEqual(len(agent['mailbox']), sim.MAILBOX_KEEP_COUNT)
        self.assertEqual(agent['mailbox'][0]['text'], 'm5')


class ReadMailboxUnreadOnly(unittest.TestCase):
    def _state_with_mail(self):
        s = _state()
        s['agents']['ben']['mailbox'] = [
            {'kind': 'agent_mail', 'text': 'first unread', 'read': False},
            {'kind': 'agent_mail', 'text': 'already read', 'read': True},
            {'kind': 'agent_mail', 'text': 'second unread', 'read': False},
        ]
        return s

    def test_unread_only_lists_only_unread(self):
        s = self._state_with_mail()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            out = serve._read_mailbox_tool_text('ben', {'unreadOnly': True})
        self.assertIn('UNREAD MAILBOX OF ben (2 unread of 3 messages', out)
        self.assertIn('first unread', out)
        self.assertIn('second unread', out)
        self.assertNotIn('already read', out)

    def test_default_lists_all(self):
        s = self._state_with_mail()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            out = serve._read_mailbox_tool_text('ben')
        self.assertIn('MAILBOX OF ben (3 messages', out)
        self.assertIn('first unread', out)
        self.assertIn('already read', out)

    def test_unread_only_does_not_mutate(self):
        s = self._state_with_mail()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            serve._read_mailbox_tool_text('ben', {'unreadOnly': True})
        self.assertFalse(s['agents']['ben']['mailbox'][0]['read'])

    def test_unread_only_all_read(self):
        s = _state()
        s['agents']['ben']['mailbox'] = [{'text': 'old', 'read': True}]
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            out = serve._read_mailbox_tool_text('ben', {'unreadOnly': True})
        self.assertIn("ben's mailbox has no unread messages (1 read)", out)

    def test_unread_only_empty_mailbox(self):
        s = _state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            out = serve._read_mailbox_tool_text('ben', {'unreadOnly': True})
        self.assertIn("ben's mailbox is empty.", out)


class MarkMailboxRead(unittest.TestCase):
    def _state_with_mail(self):
        s = _state()
        s['agents']['ben']['mailbox'] = [
            {'text': 'one', 'read': False},
            {'text': 'two', 'read': True},
            {'text': 'three', 'read': False},
        ]
        s['agents']['cora']['mailbox'] = [
            {'text': 'cora mail', 'read': False},
        ]
        return s

    def test_own_marks_all_unread(self):
        s = self._state_with_mail()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
                unittest.mock.patch.object(serve, 'save_state_to_db',
                                           side_effect=lambda st: saved.update(st)):
            result = serve._mark_mailbox_read('ben')
        self.assertTrue(result['ok'])
        self.assertEqual(result['agentId'], 'ben')
        self.assertEqual(result['marked'], 2)
        reads = [m['read'] for m in s['agents']['ben']['mailbox']]
        self.assertEqual(reads, [True, True, True])
        self.assertIs(saved.get('agents'), s['agents'])

    def test_other_denied_for_ordinary_agent(self):
        s = self._state_with_mail()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            result = serve._mark_mailbox_read('ben', 'cora')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 403)
        self.assertIn('may not mark another agent', result['error'])
        self.assertFalse(s['agents']['cora']['mailbox'][0]['read'])

    def test_post_office_may_mark_others(self):
        s = self._state_with_mail()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
                unittest.mock.patch.object(serve, 'save_state_to_db'):
            result = serve._mark_mailbox_read('cora', 'ben')
        self.assertTrue(result['ok'])
        self.assertEqual(result['marked'], 2)

    def test_director_may_mark_others(self):
        s = self._state_with_mail()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
                unittest.mock.patch.object(serve, 'save_state_to_db'):
            result = serve._mark_mailbox_read('dax', 'ben')
        self.assertTrue(result['ok'])
        self.assertEqual(result['marked'], 2)

    def test_admin_may_mark_others(self):
        s = self._state_with_mail()
        s['agents']['theo']['isAdmin'] = True
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
                unittest.mock.patch.object(serve, 'save_state_to_db'):
            result = serve._mark_mailbox_read('theo', 'ben')
        self.assertTrue(result['ok'])
        self.assertEqual(result['marked'], 2)

    def test_unknown_agent(self):
        s = self._state_with_mail()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            result = serve._mark_mailbox_read('cora', 'nobody')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 404)

    def test_noop_when_all_read(self):
        s = _state()
        s['agents']['ben']['mailbox'] = [{'text': 'old', 'read': True}]
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
                unittest.mock.patch.object(serve, 'save_state_to_db'):
            result = serve._mark_mailbox_read('ben')
        self.assertTrue(result['ok'])
        self.assertEqual(result['marked'], 0)


class MailToolsRegistered(unittest.TestCase):
    def _tool(self, name):
        for t in serve.AGENT_ASK_TOOLS:
            if t.get('function', {}).get('name') == name:
                return t['function']
        return None

    def test_send_mail_registered(self):
        tool = self._tool('send_mail')
        self.assertIsNotNone(tool)
        self.assertEqual(set(tool['parameters']['required']), {'toAgentId', 'text'})

    def test_mark_mailbox_read_registered(self):
        tool = self._tool('mark_mailbox_read')
        self.assertIsNotNone(tool)
        self.assertEqual(tool['parameters']['required'], [])

    def test_read_mailbox_advertises_unread_only(self):
        tool = self._tool('read_mailbox')
        self.assertIsNotNone(tool)
        props = tool['parameters']['properties']
        self.assertIn('unreadOnly', props)


class MailToolDispatch(unittest.TestCase):
    def _executor(self):
        return serve._make_web_tools_executor('ben', 'key-123')

    def test_send_mail_forwards_to_endpoint(self):
        ex = self._executor()
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'ok': True, 'mail': {}}) as http:
            out = ex('send_mail', {'toAgentId': 'cora', 'text': 'ping'})
        self.assertEqual(http.call_count, 1)
        method, base, path, body, key = http.call_args.args
        self.assertEqual(method, 'POST')
        self.assertEqual(base, serve.SELF_BASE_URL)
        self.assertEqual(path, '/api/mail/send')
        self.assertEqual(body, {'toId': 'cora', 'text': 'ping'})
        self.assertEqual(key, 'key-123')
        self.assertIn('Mail sent to cora', out)
        self.assertIn('does not wake', out)

    def test_send_mail_requires_args(self):
        ex = self._executor()
        out = ex('send_mail', {'toAgentId': 'cora', 'text': '   '})
        self.assertIn('requires a toAgentId', out)
        out = ex('send_mail', {'text': 'no recipient'})
        self.assertIn('requires a toAgentId', out)

    def test_send_mail_failure_message(self):
        ex = self._executor()
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'error': 'recipient not found'}):
            out = ex('send_mail', {'toAgentId': 'ghost', 'text': 'hi'})
        self.assertIn('Could not send the mail', out)
        self.assertIn('recipient not found', out)

    def test_mark_mail_read_forwards_to_endpoint(self):
        ex = self._executor()
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'ok': True, 'agentId': 'ben',
                                                      'marked': 3}) as http:
            out = ex('mark_mailbox_read', {})
        self.assertEqual(http.call_count, 1)
        method, base, path, body, key = http.call_args.args
        self.assertEqual(path, '/api/mail/read')
        self.assertEqual(body, {})
        self.assertEqual(key, 'key-123')
        self.assertIn('Marked 3 unread message(s)', out)
        self.assertIn("in ben's mailbox", out)

    def test_mark_mail_read_forwards_target(self):
        ex = self._executor()
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'ok': True, 'agentId': 'cora',
                                                      'marked': 1}) as http:
            ex('mark_mailbox_read', {'agentId': 'cora'})
        self.assertEqual(http.call_args.args[3], {'agentId': 'cora'})

    def test_mark_mail_read_failure_message(self):
        ex = self._executor()
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'error': 'You may not mark'}):
            out = ex('mark_mailbox_read', {'agentId': 'cora'})
        self.assertIn('Could not mark the mailbox read', out)
        self.assertIn('You may not mark', out)


class MailReadEndpoint(unittest.TestCase):
    def _client(self, key):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        if key:
            c.headers['X-Agent-Key'] = key
        return c

    def _state_with_mail(self):
        s = _state()
        s['agents']['ben']['mailbox'] = [
            {'text': 'one', 'read': False},
            {'text': 'two', 'read': False},
        ]
        s['agents']['cora']['mailbox'] = [
            {'text': 'cora mail', 'read': False},
        ]
        return s

    def test_requires_agent_key(self):
        c = self._client(None)
        r = c.post('/api/mail/read', json={})
        self.assertEqual(r.status_code, 401)

    def test_marks_own_mailbox(self):
        s = self._state_with_mail()
        key = serve.get_or_create_agent_key('ben')
        c = self._client(key)
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            r = c.post('/api/mail/read', json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['agentId'], 'ben')
        self.assertEqual(body['marked'], 2)
        self.assertTrue(all(m['read'] for m in s['agents']['ben']['mailbox']))

    def test_other_mailbox_denied_for_ordinary_agent(self):
        s = self._state_with_mail()
        key = serve.get_or_create_agent_key('ben')
        c = self._client(key)
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            r = c.post('/api/mail/read', json={'agentId': 'cora'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('may not mark another agent', r.json()['error'])
        self.assertFalse(s['agents']['cora']['mailbox'][0]['read'])

    def test_post_office_may_mark_others(self):
        s = self._state_with_mail()
        key = serve.get_or_create_agent_key('cora')
        c = self._client(key)
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            r = c.post('/api/mail/read', json={'agentId': 'ben'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['marked'], 2)

    def test_unknown_agent(self):
        s = self._state_with_mail()
        key = serve.get_or_create_agent_key('cora')
        c = self._client(key)
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s):
            r = c.post('/api/mail/read', json={'agentId': 'ghost'})
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()
