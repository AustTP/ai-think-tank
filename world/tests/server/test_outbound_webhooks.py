"""Outbound webhooks (operator direction, 2026-11-01): the tank notifies
subscribed external systems when action-log events match their patterns.

These tests pin:
- registration CRUD through /api/webhooks (player-or-admin only, secrets
  are stored but never listed back -- hasSecret replaces the value);
- SSRF refusal at registration and at delivery (a webhook pointed at
  loopback, private, or link-local space is a credential-theft vector);
- event-pattern matching: substring, case-insensitive, bare '*' wildcard,
  and an EMPTY pattern list matches nothing (a silent default is safer
  than firehosing every action to a new subscriber);
- the GitHub-style signature: sha256=<hex hmac-sha256(body, secret)> over
  the exact POSTed bytes;
- delivery is bounded: one POST, short timeout, capped fan-out, oversized
  details truncated, and delivery records logged under self-kinds the
  emit skips so a receiver can never loop the tank into itself.
The network is mocked throughout -- no test touches the live internet.
"""
import hashlib
import hmac
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _state():
    return {'agents': {'theo': {'id': 'theo', 'role': 'admin'}},
            'teams': []}


def _gate(player=True):
    return (mock.patch.object(serve, '_operator_gate',
                              return_value=(True, 'player' if player else 'theo')),)


class WebhookRegistry(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        p_get = mock.patch.object(serve, '_get_setting',
                                  side_effect=lambda key, default='': self.settings.get(key, default))
        p_set = mock.patch.object(serve, '_set_setting',
                                  side_effect=lambda key, val: self.settings.__setitem__(key, val))
        p_get.start(); p_set.start()
        self.addCleanup(p_get.stop); self.addCleanup(p_set.stop)
        p_host = mock.patch.object(serve, '_is_safe_public_host', return_value=True)
        p_host.start()
        self.addCleanup(p_host.stop)
        serve._WEBHOOK_CACHE['at'] = 0.0

    def _post(self, body, actor='player'):
        state = _state()
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate', return_value=(True, actor)), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            r = TestClient(serve.app).post('/api/webhooks', json=body)
        return r, state

    def test_registration_stores_and_redacts(self):
        r, state = self._post({'url': 'https://hooks.example.com/tank',
                               'secret': 's3cret', 'events': ['card_report']})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['webhook']['hasSecret'])
        self.assertNotIn('s3cret', r.text)
        items = json.loads(self.settings['outbound_webhooks'])
        self.assertEqual(items[0]['secret'], 's3cret')
        self.assertEqual(items[0]['id'], 'wh-1')

    def test_registration_refuses_private_targets(self):
        for url in ('http://127.0.0.1:9222/x', 'http://169.254.169.254/latest',
                    'http://localhost:8937/'):
            with mock.patch.object(serve, '_is_safe_public_host', return_value=False):
                r, _state_ = self._post({'url': url, 'events': ['card_report']})
            self.assertEqual(r.status_code, 400, url)

    def test_registration_rejects_bad_shape(self):
        r, _ = self._post({'url': 'https://hooks.example.com/x', 'events': 'card_report'})
        self.assertEqual(r.status_code, 400)
        r, _ = self._post({'url': 'not a url', 'events': ['x']})
        self.assertEqual(r.status_code, 400)
        r, _ = self._post({'url': 'https://hooks.example.com/x',
                           'events': [f'e{i}' for i in range(30)]})
        self.assertEqual(r.status_code, 400)

    def test_worker_agents_cannot_register(self):
        state = _state()
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate', return_value=(True, 'ada')), \
             mock.patch.object(serve, '_agent_is_admin', return_value=False), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'):
            r = TestClient(serve.app).post('/api/webhooks',
                                           json={'url': 'https://hooks.example.com/x',
                                                 'events': ['card_report']})
        self.assertEqual(r.status_code, 403)

    def test_delete_removes_and_404s_unknown(self):
        r, _ = self._post({'url': 'https://hooks.example.com/x', 'events': ['x']})
        whid = r.json()['webhook']['id']
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=_state()), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            r2 = TestClient(serve.app).delete(f'/api/webhooks/{whid}')
            r3 = TestClient(serve.app).delete('/api/webhooks/wh-99')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r3.status_code, 404)
        self.assertEqual(json.loads(self.settings['outbound_webhooks']), [])

    def test_id_gaps_do_not_collide(self):
        self.settings['outbound_webhooks'] = json.dumps(
            [{'id': 'wh-3', 'url': 'https://x.example.com', 'events': ['a'], 'enabled': True}])
        p_host = mock.patch.object(serve, '_is_safe_public_host', return_value=True)
        p_host.start()
        self.addCleanup(p_host.stop)
        serve._WEBHOOK_CACHE['at'] = 0.0
        r, _ = self._post({'url': 'https://hooks.example.com/y', 'events': ['b']})
        self.assertEqual(r.json()['webhook']['id'], 'wh-4')


class EventMatching(unittest.TestCase):
    def test_substring_case_insensitive(self):
        self.assertTrue(serve._event_matches(['card_report'], 'card_report'))
        self.assertTrue(serve._event_matches(['CARD'], 'card_report'))
        self.assertFalse(serve._event_matches(['card'], 'issue_blocked'))

    def test_star_matches_everything_and_empty_matches_nothing(self):
        self.assertTrue(serve._event_matches(['*'], 'anything_at_all'))
        self.assertFalse(serve._event_matches([], 'card_report'))
        self.assertFalse(serve._event_matches(None, 'card_report'))


class Delivery(unittest.TestCase):
    def setUp(self):
        self.settings = {'outbound_webhooks': json.dumps(
            [{'id': 'wh-1', 'url': 'https://hooks.example.com/x', 'secret': 'k9',
              'events': ['card_report'], 'enabled': True}])}
        p_get = mock.patch.object(serve, '_get_setting',
                                  side_effect=lambda key, default='': self.settings.get(key, default))
        p_get.start()
        self.addCleanup(p_get.stop)
        p_host = mock.patch.object(serve, '_is_safe_public_host', return_value=True)
        p_host.start()
        self.addCleanup(p_host.stop)
        serve._WEBHOOK_CACHE['at'] = 0.0

    def test_signature_signs_the_exact_posted_body(self):
        seen = {}

        class _Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{}'

        def _fake_urlopen(req, timeout):
            seen['body'] = req.data
            # urllib normalizes header keys through str.capitalize(), so the
            # test reads them back case-insensitively via header_items().
            seen['headers'] = {k.lower(): v for k, v in req.header_items()}
            return _Resp()

        with mock.patch('urllib.request.urlopen', side_effect=_fake_urlopen):
            ok, note = serve._deliver_webhook(
                serve._outbound_webhooks()[0], 'ada', 'card_report', {'k': 'v'})
        self.assertTrue(ok, note)
        expected = 'sha256=' + hmac.new(b'k9', seen['body'], hashlib.sha256).hexdigest()
        self.assertEqual(seen['headers']['x-tank-signature'], expected)
        self.assertEqual(seen['headers']['x-tank-event'], 'card_report')
        self.assertIn('card_report', seen['body'].decode())

    def test_oversized_details_are_truncated(self):
        body = serve._webhook_body('ada', 'card_report', {'blob': 'x' * (serve.WEBHOOK_BODY_MAX_BYTES + 10)})
        self.assertLessEqual(len(body), serve.WEBHOOK_BODY_MAX_BYTES)
        self.assertIn('truncated', body.decode())

    def test_emit_skips_its_own_delivery_kinds(self):
        # webhook_delivery / webhook_delivery_failed must never re-fan-out:
        # that is the recursion guard.
        with mock.patch.object(serve, '_deliver_webhook') as d:
            serve._outbound_webhook_emit('server', 'webhook_delivery', {})
            serve._outbound_webhook_emit('server', 'webhook_delivery_failed', {})
        d.assert_not_called()

    def test_emit_is_bounded_and_no_match_is_cheap(self):
        self.settings['outbound_webhooks'] = json.dumps(
            [{'id': f'wh-{i}', 'url': 'https://hooks.example.com/x', 'events': ['*'],
              'enabled': True} for i in range(1, 20)])
        p_host = mock.patch.object(serve, '_is_safe_public_host', return_value=True)
        p_host.start()
        self.addCleanup(p_host.stop)
        serve._WEBHOOK_CACHE['at'] = 0.0
        calls = []

        def _fake(record, agent, action, details):
            calls.append(record['id'])
            return True, 200

        with mock.patch.object(serve, '_deliver_webhook', side_effect=_fake), \
             mock.patch.object(serve, 'log_action'):
            serve._outbound_webhook_emit('ada', 'card_report', {})
        self.assertEqual(len(calls), serve.WEBHOOK_MAX_FANOUT)
        # A non-matching kind delivers nothing.
        serve._outbound_webhook_emit('ada', 'unrelated_kind', {})
        self.assertEqual(len(calls), serve.WEBHOOK_MAX_FANOUT)


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        self.settings = {'outbound_webhooks': json.dumps(
            [{'id': 'wh-1', 'url': 'https://hooks.example.com/x', 'secret': None,
              'events': ['card_report'], 'enabled': True}])}
        p_get = mock.patch.object(serve, '_get_setting',
                                  side_effect=lambda key, default='': self.settings.get(key, default))
        p_get.start()
        self.addCleanup(p_get.stop)
        p_host = mock.patch.object(serve, '_is_safe_public_host', return_value=True)
        p_host.start()
        self.addCleanup(p_host.stop)
        serve._WEBHOOK_CACHE['at'] = 0.0

    def _post_test(self, whid):
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=_state()), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            return TestClient(serve.app).post(f'/api/webhooks/{whid}/test')

    def test_unknown_webhook_is_404(self):
        self.assertEqual(self._post_test('wh-99').status_code, 404)

    def test_failed_delivery_is_502_not_a_crash(self):
        with mock.patch.object(serve, '_deliver_webhook', return_value=(False, 'HTTP 500')):
            r = self._post_test('wh-1')
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.json()['ok'])

    def test_successful_delivery_is_200(self):
        with mock.patch.object(serve, '_deliver_webhook', return_value=(True, 200)):
            r = self._post_test('wh-1')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['ok'])


if __name__ == '__main__':
    unittest.main()
