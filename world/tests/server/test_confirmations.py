import os
import tempfile
import unittest
from unittest import mock

import core.confirmations as c


class ConfirmationLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'CONFIRMATIONS_DB': os.path.join(self.tmp.name, 'state.db')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_phrase_confirm_and_target_binding(self):
        item = c.propose('git.push', {'repo': 'a/b'}, {'branch': 'main'}, 'ada')
        self.assertEqual(item['status'], 'pending')
        with self.assertRaises(c.ConfirmationError):
            c.assert_ready(item['id'], 'git.push', {'repo': 'a/b'},
                           {'branch': 'main'}, 'ada')
        with self.assertRaises(c.ConfirmationError):
            c.confirm(item['id'], 'yes')
        confirmed = c.confirm(item['id'], item['phrase'])
        self.assertEqual(confirmed['status'], 'confirmed')
        ready = c.assert_ready(item['id'], 'git.push', {'repo': 'a/b'},
                               {'branch': 'main'}, 'ada')
        self.assertEqual(ready['id'], item['id'])
        with self.assertRaises(c.ConfirmationError) as changed:
            c.assert_ready(item['id'], 'git.push', {'repo': 'a/b'},
                           {'branch': 'release'}, 'ada')
        self.assertEqual(changed.exception.code, 'payload')

    def test_actor_cannot_use_another_actors_proposal(self):
        item = c.propose('mail.send', {'to': 'bob'}, {'text': 'hi'}, 'ada')
        c.confirm(item['id'], item['phrase'])
        with self.assertRaises(c.ConfirmationError) as wrong:
            c.assert_ready(item['id'], 'mail.send', {'to': 'bob'},
                           {'text': 'hi'}, 'nora')
        self.assertEqual(wrong.exception.code, 'actor')

    def test_expiry_blocks_execution(self):
        item = c.propose('execute', {'sandboxId': 's'}, {'command': 'ls'}, 'ada',
                         ttl_s=60)
        c.confirm(item['id'], item['phrase'])
        with mock.patch.object(c.time, 'time', return_value=item['expires_at'] + 1):
            with self.assertRaises(c.ConfirmationError) as expired:
                c.assert_ready(item['id'], 'execute', {'sandboxId': 's'},
                               {'command': 'ls'}, 'ada')
        self.assertEqual(expired.exception.code, 'expired')

    def test_finish_is_idempotent_for_terminal_states(self):
        item = c.propose('execute', {'sandboxId': 's'}, {'command': 'ls'}, 'ada')
        c.confirm(item['id'], item['phrase'])
        c.finish(item['id'], 'executed')
        c.finish(item['id'], 'failed', 'late')
        self.assertEqual(c.get(item['id'])['status'], 'executed')


if __name__ == '__main__':
    unittest.main()


class ExecutionGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'CONFIRMATIONS_DB': os.path.join(self.tmp.name, 'state.db'),
            'REQUIRE_CONFIRMATION_FOR_EXECUTION': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_gate_returns_a_proposal_then_accepts_the_confirmed_id(self):
        import json
        import serve
        target = {'sandboxId': 'sandbox-a'}
        args = {'command': 'echo ok', 'purpose': 'test'}
        with mock.patch.object(serve, '_load_env',
                               return_value={'REQUIRE_CONFIRMATION_FOR_EXECUTION': '1'}):
            response, confirmation_id = serve._confirmation_gate_or_response(
                {}, 'ada', 'execute', target, args)
        self.assertEqual(response.status_code, 428)
        body = json.loads(response.body)
        c.confirm(body['confirmationId'], body['phrase'])
        with mock.patch.object(serve, '_load_env',
                               return_value={'REQUIRE_CONFIRMATION_FOR_EXECUTION': '1'}):
            response, confirmation_id = serve._confirmation_gate_or_response(
                {'confirmationId': body['confirmationId']}, 'ada', 'execute',
                target, args)
        self.assertIsNone(response)
        self.assertEqual(confirmation_id, body['confirmationId'])

    def test_gate_rejects_changed_command(self):
        import json
        import serve
        target = {'sandboxId': 'sandbox-a'}
        args = {'command': 'echo ok', 'purpose': 'test'}
        with mock.patch.object(serve, '_load_env',
                               return_value={'REQUIRE_CONFIRMATION_FOR_EXECUTION': '1'}):
            response, _ = serve._confirmation_gate_or_response(
                {}, 'ada', 'execute', target, args)
        body = json.loads(response.body)
        c.confirm(body['confirmationId'], body['phrase'])
        with mock.patch.object(serve, '_load_env',
                               return_value={'REQUIRE_CONFIRMATION_FOR_EXECUTION': '1'}):
            response, _ = serve._confirmation_gate_or_response(
                {'confirmationId': body['confirmationId']}, 'ada', 'execute',
                target, {'command': 'echo changed', 'purpose': 'test'})
        self.assertEqual(response.status_code, 409)


class ConfirmationHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'CONFIRMATIONS_DB': os.path.join(self.tmp.name, 'state.db')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_player_can_propose_and_confirm(self):
        from starlette.testclient import TestClient
        import serve
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, 'log_action'):
            client = TestClient(serve.app)
            created = client.post('/api/confirmations', json={
                'action': 'library.write',
                'target': {'path': 'notes/a.md'},
                'args': {'body': 'hello'},
            })
            self.assertEqual(created.status_code, 201)
            proposal = created.json()['proposal']
            confirmed = client.post(
                f"/api/confirmations/{proposal['id']}/confirm",
                json={'phrase': proposal['phrase']})
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()['proposal']['status'], 'confirmed')


class ConfirmationEdgesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'CONFIRMATIONS_DB': os.path.join(self.tmp.name, 'state.db')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_propose_rejects_bad_inputs(self):
        for args in (('', {}, {}, 'a'), ('x', {}, {}, ''), ('x' * 81, {}, {}, 'a')):
            with self.assertRaises(c.ConfirmationError):
                c.propose(*args)
        circular = {}
        circular['self'] = circular
        with self.assertRaises(c.ConfirmationError):
            c.propose('a', circular, {}, 'a')
        with self.assertRaises(c.ConfirmationError):
            c.propose('a', {'x': 'y' * (c.MAX_PAYLOAD_BYTES + 1)}, {}, 'a')
        item = c.propose('a', None, None, 'a', ttl_s='bad')
        self.assertEqual(item['target'], {})
        self.assertEqual(item['args'], {})

    def test_confirm_and_cancel_edges(self):
        with self.assertRaises(c.ConfirmationError) as missing:
            c.confirm('nope', 'x')
        self.assertEqual(missing.exception.code, 'not_found')
        item = c.propose('a', {}, {}, 'ada')
        first = c.confirm(item['id'], item['phrase'])
        self.assertEqual(c.confirm(item['id'], 'wrong')['status'], first['status'])
        c.finish(item['id'], 'executed')
        with self.assertRaises(c.ConfirmationError) as done:
            c.confirm(item['id'], item['phrase'])
        self.assertEqual(done.exception.code, 'executed')
        with self.assertRaises(c.ConfirmationError):
            c.finish('x', 'bogus')
        with self.assertRaises(c.ConfirmationError):
            c.cancel('nope', 'player')

    def test_expired_and_cancel_paths(self):
        item = c.propose('a', {}, {}, 'ada', ttl_s=60)
        with mock.patch.object(c.time, 'time', return_value=item['expires_at'] + 1):
            with self.assertRaises(c.ConfirmationError) as expired:
                c.confirm(item['id'], item['phrase'])
        self.assertEqual(expired.exception.code, 'expired')
        other = c.propose('a', {}, {}, 'ada')
        with self.assertRaises(c.ConfirmationError):
            c.cancel(other['id'], 'nora')
        cancelled = c.cancel(other['id'], 'player')
        self.assertEqual(cancelled['status'], 'cancelled')
        with self.assertRaises(c.ConfirmationError):
            c.cancel(other['id'], 'player')

    def test_list_expire_and_bad_row(self):
        c.propose('a', {}, {}, 'ada')
        c.propose('b', {}, {}, 'ada')
        self.assertEqual(len(c.list_for_actor('ada')), 2)
        self.assertEqual(len(c.list_for_actor('ada', 'pending')), 2)
        self.assertEqual(len(c.list_for_actor('ada', 'pending', limit=1)), 1)
        self.assertEqual(c.expire_stale(), 0)
        with c._connect() as conn:
            conn.execute("UPDATE confirmation_proposals SET target_json='bad'")
        self.assertIsNone(c.list_for_actor('ada')[0]['target'])

    def test_gate_finish_ignores_missing_and_errors(self):
        c.gate_finish(None)
        with mock.patch.object(c, 'finish', side_effect=RuntimeError('x')):
            c.gate_finish('id')


class ConfirmationHttpEdgesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'CONFIRMATIONS_DB': os.path.join(self.tmp.name, 'state.db')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _client(self, session=True):
        from starlette.testclient import TestClient
        import serve
        self.session = mock.patch.object(serve, 'verify_session', return_value=session)
        self.session.start()
        self.addCleanup(self.session.stop)
        return TestClient(serve.app)

    def test_unauthorized_and_malformed(self):
        client = self._client(False)
        self.assertEqual(client.get('/api/confirmations').status_code, 401)
        self.assertEqual(client.post('/api/confirmations', json={}).status_code, 401)
        client = self._client(True)
        self.assertEqual(client.post('/api/confirmations', content=b'x').status_code, 400)
        self.assertEqual(client.post('/api/confirmations', json=[]).status_code, 400)
        self.assertEqual(client.post('/api/confirmations', json={}).status_code, 400)

    def test_player_flow_and_not_found(self):
        client = self._client(True)
        created = client.post('/api/confirmations', json={
            'action': 'x', 'target': {}, 'args': {}})
        pid = created.json()['proposal']['id']
        self.assertEqual(client.get('/api/confirmations').status_code, 200)
        self.assertEqual(client.get('/api/confirmations/nope').status_code, 404)
        self.assertEqual(client.get(f'/api/confirmations/{pid}').status_code, 200)
        self.assertEqual(client.post('/api/confirmations/nope/cancel').status_code, 409)
        self.assertEqual(client.post(f'/api/confirmations/{pid}/cancel').status_code, 200)
        self.assertEqual(client.post(f'/api/confirmations/{pid}/confirm',
                                     json={'phrase': 'x'}).status_code, 409)
        created = client.post('/api/confirmations', json={
            'action': 'x', 'target': {}, 'args': {}}).json()['proposal']
        self.assertEqual(client.post(f"/api/confirmations/{created['id']}/confirm",
                                     content=b'x').status_code, 400)
        self.assertEqual(client.post(f"/api/confirmations/{created['id']}/confirm",
                                     json={'phrase': created['phrase']}).status_code, 200)


class ConfirmationFinalEdgesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'CONFIRMATIONS_DB': os.path.join(self.tmp.name, 'state.db')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_connect_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with c._connect() as conn:
                conn.execute('CREATE TABLE t (x)')
                raise RuntimeError('boom')

    def test_assert_ready_missing_and_changed_action(self):
        with self.assertRaises(c.ConfirmationError):
            c.assert_ready('nope', 'a', {}, {}, 'ada')
        item = c.propose('a', {}, {}, 'ada')
        c.confirm(item['id'], item['phrase'])
        with self.assertRaises(c.ConfirmationError) as changed:
            c.assert_ready(item['id'], 'b', {}, {}, 'ada')
        self.assertEqual(changed.exception.code, 'action')

    def test_list_limit_is_clamped(self):
        c.propose('a', {}, {}, 'ada')
        self.assertEqual(len(c.list_for_actor('ada', limit=0)), 1)

    def test_api_direct_unauthorized_and_agent_actor(self):
        import asyncio
        import server.confirmations_api as api
        from starlette.requests import Request

        def req(body=None):
            r = Request({'type': 'http', 'method': 'POST', 'path': '/',
                         'headers': [], 'query_string': b''})
            async def _json():
                return body
            r.json = _json
            return r

        with mock.patch.object(api, '_actor', return_value=None):
            self.assertEqual(asyncio.run(api.get_confirmation('x', req())).status_code, 401)
            self.assertEqual(asyncio.run(api.cancel_confirmation('x', req())).status_code, 401)
        with mock.patch.object(api, '_actor', return_value='ada'), \
             mock.patch.object(api.confirmations, 'get', return_value={'id': 'x', 'actor': 'bob'}):
            self.assertEqual(asyncio.run(api.get_confirmation('x', req())).status_code, 404)
        with mock.patch.object(api, '_actor', return_value='ada'):
            self.assertEqual(asyncio.run(api.confirm_confirmation('x', req({}))).status_code, 403)
