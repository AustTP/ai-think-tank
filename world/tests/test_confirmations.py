import os
import tempfile
import unittest
from unittest import mock

import confirmations as c


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
