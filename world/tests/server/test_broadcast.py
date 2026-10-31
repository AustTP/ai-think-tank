"""Broadcast primitive (scale slice 3): POST /api/intent/broadcast.

One note to many mailboxes and NOTHING else: no issue, no lane routing, no
grooming -- direction must never become a card (the DEV-85 lesson). Operator
gate; scopes directors (default) / room / all; empty recipient set refused.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-broadcast-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _state():
    roster = [
        {'id': 'theo', 'name': 'Theo', 'role': 'Control Room', 'isAdmin': True},
        {'id': 'nora', 'name': 'Nora', 'role': 'Team Lead', 'isDirector': True},
        {'id': 'ada', 'name': 'Ada', 'role': 'Engineer', 'director': 'nora'},
        {'id': 'ben', 'name': 'Ben', 'role': 'Engineer', 'director': 'nora'},
    ]
    return {
        'agentRoster': roster,
        'agents': {a['id']: {'id': a['id'], 'name': a['name'], 'role': a['role'],
                             'inRoom': 'library', 'mailbox': []}
                   for a in roster},
        'playerInbox': [], 'workQueue': [], 'tasks': {}, 'issues': {},
    }


class BroadcastEndpoint(unittest.TestCase):
    def _client(self, state, authed=True):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        if authed:
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        patcher = unittest.mock.patch.object(serve, 'get_state_from_db',
                                             return_value=state)
        patcher.start()
        self.addCleanup(patcher.stop)
        return c

    def test_requires_operator_auth(self):
        r = self._client(_state(), authed=False).post(
            '/api/intent/broadcast', json={'text': 'hello'})
        self.assertEqual(r.status_code, 401)

    def test_default_scope_reaches_directors_and_admin_only(self):
        s = _state()
        r = self._client(s).post('/api/intent/broadcast',
                                 json={'text': 'new skills are live'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['delivered'], 2)
        self.assertEqual(r.json()['scope'], 'directors')
        for aid in ('theo', 'nora'):
            box = s['agents'][aid]['mailbox']
            self.assertEqual(len(box), 1)
            self.assertTrue(box[0]['text'].startswith('Operator (broadcast): '))
            self.assertFalse(box[0]['read'])
        for aid in ('ada', 'ben'):
            self.assertEqual(s['agents'][aid]['mailbox'], [])

    def test_broadcast_never_creates_work(self):
        # The whole point: direction that must never become a card.
        s = _state()
        self._client(s).post('/api/intent/broadcast',
                             json={'text': 'STORY-free direction'})
        self.assertEqual(s['workQueue'], [])
        self.assertEqual(s['tasks'], {})
        self.assertEqual(s.get('issues'), {})

    def test_room_scope_reaches_only_that_room(self):
        s = _state()
        s['agents']['ada']['inRoom'] = 'observatory'
        r = self._client(s).post('/api/intent/broadcast',
                                 json={'text': 'meetup', 'scope': 'room',
                                       'room': 'observatory'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['delivered'], 1)
        self.assertEqual(len(s['agents']['ada']['mailbox']), 1)
        self.assertEqual(s['agents']['theo']['mailbox'], [])

    def test_all_scope_reaches_everyone(self):
        s = _state()
        r = self._client(s).post('/api/intent/broadcast',
                                 json={'text': 'all hands', 'scope': 'all'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['delivered'], 4)

    def test_empty_recipient_set_refused(self):
        s = _state()
        s['agents']['ada']['inRoom'] = 'nowhere'
        r = self._client(s).post('/api/intent/broadcast',
                                 json={'text': 'x', 'scope': 'room',
                                       'room': 'observatory'})
        self.assertEqual(r.status_code, 400)

    def test_text_and_scope_validated(self):
        s = _state()
        c = self._client(s)
        self.assertEqual(c.post('/api/intent/broadcast',
                                json={'text': '   '}).status_code, 400)
        self.assertEqual(c.post('/api/intent/broadcast',
                                json={'text': 'x', 'scope': 'bogus'}).status_code, 400)
        self.assertEqual(c.post('/api/intent/broadcast',
                                json={'text': 'x', 'scope': 'room'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
