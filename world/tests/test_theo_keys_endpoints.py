"""Theo key registry endpoints (ask 3).

theo_policy.py is pure and already tested on its own.
These tests cover the WIRING in serve: the
operator-gated /api/theo/keys routes (list, grant,
revoke) and /api/theo/ask, the key-authenticated ask
lane. The registry, the policy, the lane decider, and
every lane handler are replaced per test, and the
registry save is patched out -- no file is written,
no model is called, and the DB is isolated.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import theo_policy  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-theo-keys-')
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
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'ben', 'director': 'nora'},
            {'id': 'cora'},
        ],
        'agents': {
            'theo': {'id': 'theo', 'busy': False},
            'nora': {'id': 'nora', 'busy': False},
            'ben': {'id': 'ben', 'busy': False},
            'cora': {'id': 'cora', 'busy': False},
        },
    }


def _client():
    from starlette.testclient import TestClient
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME,
                  serve.create_session())
    return c


class _KeyFixture(unittest.TestCase):
    """A fresh registry and policy per test, with the
    state reader, the registry save, and log_action
    patched out -- the grant endpoints must not touch
    the real library or the real audit log."""

    def setUp(self):
        self.registry = theo_policy.KeyRegistry()
        self.policy = theo_policy.TheoPolicy(
            registry=self.registry)
        self.state = _state()
        self._patches = [
            unittest.mock.patch.object(
                serve, '_KEY_REGISTRY', self.registry),
            unittest.mock.patch.object(
                serve, '_theo_policy', self.policy),
            unittest.mock.patch.object(
                serve, '_save_key_registry'),
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


class TheoKeysList(_KeyFixture):
    def test_requires_a_player_session(self):
        from starlette.testclient import TestClient
        response = TestClient(serve.app).get(
            '/api/theo/keys')
        self.assertEqual(response.status_code, 401)

    def test_lists_grants_secret_free(self):
        raw, key = self.registry.grant_key(
            'owner', 'cora', 'worker', rate_limit=5)
        response = _client().get('/api/theo/keys')
        self.assertEqual(response.status_code, 200)
        listed = response.json()['keys']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['keyId'], key.key_id)
        self.assertEqual(listed[0]['agentId'], 'cora')
        self.assertEqual(listed[0]['role'], 'worker')
        self.assertEqual(listed[0]['rateLimit'], 5)
        self.assertNotIn(raw, response.text)


class TheoKeysGrant(_KeyFixture):
    def _grant(self, body):
        return _client().post('/api/theo/keys', json=body)

    def test_requires_a_player_session(self):
        from starlette.testclient import TestClient
        response = TestClient(serve.app).post(
            '/api/theo/keys', json={})
        self.assertEqual(response.status_code, 401)

    def test_agent_id_and_role_are_required(self):
        response = self._grant({'role': 'worker'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('agentId', response.json()['error'])
        response = self._grant({'agentId': 'cora'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('role', response.json()['error'])

    def test_an_unknown_role_is_a_400(self):
        response = self._grant(
            {'agentId': 'cora', 'role': 'superuser'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('unknown role',
                      response.json()['error'])
        self.assertEqual(self.registry.list_keys(), [])

    def test_rate_limit_must_be_an_integer(self):
        response = self._grant(
            {'agentId': 'cora', 'role': 'worker',
             'rateLimit': 'many'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('rateLimit', response.json()['error'])

    def test_grant_returns_the_raw_key_once(self):
        response = self._grant(
            {'agentId': 'cora', 'role': 'worker',
             'rateLimit': 5, 'budget': 2.0,
             'ttlSeconds': 90})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        raw = body['key']
        self.assertIsInstance(raw, str)
        record = body['record']
        self.assertEqual(record['agentId'], 'cora')
        self.assertEqual(record['role'], 'worker')
        self.assertEqual(record['rateLimit'], 5)
        self.assertEqual(record['budget'], 2.0)
        self.assertIsNotNone(record['expiresAt'])
        resolved = self.registry.verify(raw)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.agent_id, 'cora')


class TheoKeysRevoke(_KeyFixture):
    def test_requires_a_player_session(self):
        from starlette.testclient import TestClient
        response = TestClient(serve.app).post(
            '/api/theo/keys/revoke', json={'keyId': 'x'})
        self.assertEqual(response.status_code, 401)

    def test_key_id_is_required(self):
        response = _client().post(
            '/api/theo/keys/revoke', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn('keyId', response.json()['error'])

    def test_an_unknown_key_id_is_a_404(self):
        response = _client().post(
            '/api/theo/keys/revoke',
            json={'keyId': 'key-nope'})
        self.assertEqual(response.status_code, 404)

    def test_a_revoked_key_stops_working(self):
        raw, key = self.registry.grant_key(
            'owner', 'cora', 'worker')
        response = _client().post(
            '/api/theo/keys/revoke',
            json={'keyId': key.key_id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertIsNone(self.registry.verify(raw))


class TheoAsk(_KeyFixture):
    """The key-authenticated ask lane: the key's agent
    acts through the role the owner granted, decided by
    evaluate_key -- not by the roster lane the agent
    would hold talking to Theo directly."""

    def setUp(self):
        super().setUp()
        self.lane = 'ask'
        self.handled = []
        self._orig_decider = serve._lane_decider
        self._orig_handlers = dict(serve._ROUTING_HANDLERS)
        serve._lane_decider = (
            lambda state, text: self.lane)

        async def fake_handler(state, text, admin_id,
                                session_id=None):
            self.handled.append(
                {'lane': self.lane, 'text': text,
                 'admin': admin_id,
                 'session': session_id})
            return {'reply': 'handled'}

        for lane in ('ask', 'unclear', 'schedule',
                     'schedule_once', 'spike', 'story',
                     'incident'):
            serve._ROUTING_HANDLERS[lane] = fake_handler
        self.addCleanup(self._restore)

    def _restore(self):
        serve._ROUTING_HANDLERS.clear()
        serve._ROUTING_HANDLERS.update(
            self._orig_handlers)
        serve._lane_decider = self._orig_decider

    def _ask(self, text='what is the plan?', raw_key=None):
        headers = {}
        if raw_key is not None:
            headers['X-Agent-Key'] = raw_key
        return _client().post(
            '/api/theo/ask', json={'text': text},
            headers=headers)

    def test_the_key_header_is_required(self):
        response = self._ask(raw_key=None)
        self.assertEqual(response.status_code, 401)
        self.assertIn('X-Agent-Key',
                      response.json()['error'])

    def test_text_is_required(self):
        raw, _ = self.registry.grant_key(
            'owner', 'cora', 'worker')
        response = _client().post(
            '/api/theo/ask', json={},
            headers={'X-Agent-Key': raw})
        self.assertEqual(response.status_code, 400)

    def test_an_invalid_key_is_a_401(self):
        response = self._ask(raw_key='not-a-key')
        self.assertEqual(response.status_code, 401)
        self.assertIn('invalid, revoked, or expired key',
                      response.json()['error'])

    def test_a_registryless_policy_is_a_503(self):
        serve._theo_policy = theo_policy.TheoPolicy()
        raw, _ = self.registry.grant_key(
            'owner', 'cora', 'worker')
        response = self._ask(raw_key=raw)
        self.assertEqual(response.status_code, 503)
        self.assertIn('key registry not configured',
                      response.json()['error'])

    def test_a_worker_key_may_ask(self):
        raw, key = self.registry.grant_key(
            'owner', 'cora', 'worker')
        response = self._ask(raw_key=raw)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reply'],
                         'handled')
        self.assertEqual(len(self.handled), 1)
        self.assertEqual(self.handled[0]['lane'], 'ask')

    def test_a_worker_key_is_denied_director_lanes(self):
        raw, _ = self.registry.grant_key(
            'owner', 'cora', 'worker')
        self.lane = 'story'
        response = self._ask(raw_key=raw,
                                 text='build a story')
        self.assertEqual(response.status_code, 200)
        self.assertIn("I can't do that:",
                      response.json()['reply'])
        self.assertIn(
            'role worker may not request create_story',
            response.json()['reply'])
        self.assertEqual(self.handled, [])

    def test_a_director_key_reaches_director_lanes(self):
        raw, _ = self.registry.grant_key(
            'owner', 'nora', 'director')
        self.lane = 'story'
        response = self._ask(raw_key=raw,
                                 text='build a story')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reply'],
                         'handled')
        self.assertEqual(len(self.handled), 1)

    def test_a_revoked_key_is_a_401(self):
        raw, key = self.registry.grant_key(
            'owner', 'cora', 'worker')
        self.registry.revoke('owner', key.key_id)
        response = self._ask(raw_key=raw)
        self.assertEqual(response.status_code, 401)

    def _bare_client(self):
        # No session cookie anywhere: the way an
        # external agent actually calls the ask lane.
        from starlette.testclient import TestClient
        return TestClient(serve.app)

    def test_a_key_only_ask_passes_the_login_door(self):
        # Regression: the require_login middleware used to
        # bounce key-only asks as "please log in" before the
        # route ever ran, so the key lane only worked for
        # callers that also held a player session.
        raw, _ = self.registry.grant_key(
            'owner', 'cora', 'worker')
        response = self._bare_client().post(
            '/api/theo/ask', json={'text': 'hello'},
            headers={'X-Agent-Key': raw})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reply'],
                         'handled')
        self.assertEqual(len(self.handled), 1)

    def test_a_bad_key_still_bounces_at_the_door(self):
        response = self._bare_client().post(
            '/api/theo/ask', json={'text': 'hello'},
            headers={'X-Agent-Key': 'not-a-key'})
        self.assertEqual(response.status_code, 401)
        self.assertIn('please log in',
                      response.json()['error'])

    def test_a_theo_key_opens_no_other_door(self):
        # The key is scoped to the ask lane: it must not
        # pass the login door for any other protected route.
        raw, _ = self.registry.grant_key(
            'owner', 'cora', 'worker')
        response = self._bare_client().get(
            '/api/state', headers={'X-Agent-Key': raw})
        self.assertEqual(response.status_code, 401)
        self.assertIn('please log in',
                      response.json()['error'])


if __name__ == '__main__':
    unittest.main()
