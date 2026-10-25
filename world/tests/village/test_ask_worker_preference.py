"""The ask lane prefers workers. A director is a fallback voice only when no
worker is free, so routine player questions do not pull a director off her
team's work."""

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
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-ask-worker-')
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


def _state():
    roster = [
        {'id': 'maya', 'name': 'Maya', 'role': 'admin', 'isAdmin': True},
        {'id': 'ben', 'name': 'Ben', 'role': 'engineer'},
        {'id': 'cora', 'name': 'Cora', 'role': 'engineer'},
        {'id': 'nora', 'name': 'Nora', 'role': 'Personnel',
         'isDirector': True, 'director': None},
    ]
    return {
        'agentRoster': roster,
        'agents': {a['id']: {'id': a['id'], 'name': a['name'],
                             'role': a['role'], 'offDuty': False,
                             'busy': False, 'task': None, 'pairWith': None,
                             'profile': {'mission': 'help the think tank'}}
                   for a in roster},
        'workQueue': [],
        'tasks': {},
        'products': {},
        'completedDeliverables': [],
    }


class WorkerPreference(unittest.TestCase):
    def _client(self, state):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        patcher = unittest.mock.patch.object(serve, 'get_state_from_db',
                                             return_value=state)
        patcher.start()
        self.addCleanup(patcher.stop)
        return c

    def _ask(self, state):
        c = self._client(state)
        with unittest.mock.patch.object(serve, '_coding_tier_slug',
                                        return_value='fake-model'), \
                unittest.mock.patch.object(serve, '_mid_tier_slug',
                                           return_value='fake-model'), \
                unittest.mock.patch.object(
                    serve, '_post_openrouter_raw',
                    return_value={'choices': [{'message': {
                        'role': 'assistant', 'content': 'ok'}}]}):
            return c.post('/api/intent/ask', json={'question': 'hi'})

    def test_worker_chosen_over_director(self):
        r = self._ask(_state())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(r.json()['agent'], ('ben', 'cora'))
        self.assertNotEqual(r.json()['agent'], 'nora')

    def test_director_is_used_when_no_worker_is_free(self):
        s = _state()
        for aid in ('ben', 'cora'):
            s['agents'][aid]['busy'] = True
            s['agents'][aid]['task'] = 't-x'
        r = self._ask(s)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['agent'], 'nora')


if __name__ == '__main__':
    unittest.main()
