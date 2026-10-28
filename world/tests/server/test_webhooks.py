"""Webhook intake: channel descriptors, dedupe, tailing, and consumption.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 -m pytest tests/test_webhooks.py -q
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve
import server.webhooks as webhooks


class GoalTextTest(unittest.TestCase):
    def test_x_mention_uses_author_text_and_url(self):
        goal = serve._webhook_goal_text({
            'author': 'Ada', 'text': 'ship the parser', 'url': 'https://x.com/a/1'})
        self.assertEqual(goal, 'X mention from Ada -- ship the parser [https://x.com/a/1]')

    def test_falls_back_to_subject_then_placeholder(self):
        self.assertEqual(
            serve._webhook_goal_text({'author': 'Ada', 'subject': '@us look'}),
            'X mention from Ada -- @us look')
        self.assertEqual(serve._webhook_goal_text({}),
                         'X mention (no text captured)')

    def test_unknown_channel_still_produces_an_ask(self):
        self.assertEqual(serve._webhook_goal_text({'text': 'hi'}, 'nope'), 'nope')


class SelfMentionGateTest(unittest.TestCase):
    def tearDown(self):
        serve.X_HANDLE = ''

    def test_own_activity_is_gated(self):
        serve.X_HANDLE = 'ourhandle'
        self.assertTrue(serve._webhook_self_mention({'author': 'ourhandle'}))
        self.assertFalse(serve._webhook_self_mention({'author': 'someone'}))

    def test_unset_handle_fails_closed(self):
        serve.X_HANDLE = ''
        self.assertTrue(serve._webhook_self_mention({'author': 'anyone'}))


class DedupeTest(unittest.TestCase):
    def test_github_channel_renders_normalized_payload(self):
        text = serve._webhook_goal_text(
            {'author': 'developer',
             'text': 'CI run on main @ abc123 finished: failure (tests)',
             'url': 'https://github.com/x/y/actions/runs/11'},
            channel='github')
        self.assertTrue(text.startswith('GitHub developer'))
        self.assertIn('failure', text)
        self.assertIn('[https://github.com/x/y/actions/runs/11]', text)

    def test_github_channel_is_enabled_without_a_handle_gate(self):
        # github has no own_handle_attr: the tank never posts AS the repo,
        # so nothing can be self-activity. Its requires_env gate (the watch
        # repo) must be satisfied here -- machines without the operator's
        # .env would otherwise see the channel disabled.
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'GITHUB_WATCH_REPO': 'AustTP/ai-think-tank'}):
            self.assertTrue(serve._webhook_channel_enabled('github'))
        self.assertFalse(serve._webhook_self_mention(
            {'author': 'ai-think-tank[bot]', 'text': 'x'}, channel='github'))

    def test_seen_key_prefers_id_then_thread(self):
        self.assertEqual(serve._webhook_seen_key(
            {'channel': 'x-mention', 'payload': {'id': 'n1'}}), 'x-mention:n1')
        self.assertEqual(serve._webhook_seen_key(
            {'channel': 'x-mention', 'payload': {'threadId': 't1'}}), 'x-mention:t1')
        self.assertIsNone(serve._webhook_seen_key({'channel': 'x-mention', 'payload': {}}))

    def test_seed_seen_reads_every_parsable_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'webhooks.jsonl')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'channel': 'x-mention', 'payload': {'id': 'a'}}) + '\n')
                f.write('not json\n')
                f.write(json.dumps({'channel': 'x-mention', 'payload': {}}) + '\n')
                f.write(json.dumps({'channel': 'x-mention', 'payload': {'id': 'b'}}) + '\n')
            with mock.patch.object(serve, 'WEBHOOKS_PATH', path):
                self.assertEqual(serve._webhook_seed_seen(), {'x-mention:a', 'x-mention:b'})

    def test_seed_seen_tolerates_a_missing_file(self):
        with mock.patch.object(serve, 'WEBHOOKS_PATH', '/nonexistent/webhooks.jsonl'):
            self.assertEqual(serve._webhook_seed_seen(), set())


class TailTest(unittest.TestCase):
    def test_read_new_only_returns_complete_lines_after_offset(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'webhooks.jsonl')
            first = json.dumps({'channel': 'x-mention', 'payload': {'id': 'a'}}) + '\n'
            with open(path, 'w', encoding='utf-8') as f:
                f.write(first)
            with mock.patch.object(serve, 'WEBHOOKS_PATH', path):
                found, offset = serve._webhook_read_new(0)
                self.assertEqual([f['entry']['payload']['id'] for f in found], ['a'])
                self.assertEqual(offset, len(first))

                with open(path, 'a', encoding='utf-8') as f:
                    f.write('{"channel": "x-mention", "payload": {"id": "b"')
                found, offset2 = serve._webhook_read_new(offset)
                self.assertEqual(found, [])
                self.assertEqual(offset2, offset)

                with open(path, 'a', encoding='utf-8') as f:
                    f.write('}}\n')
                found, _ = serve._webhook_read_new(offset)
                self.assertEqual([f['entry']['payload']['id'] for f in found], ['b'])

    def test_read_new_missing_file_keeps_offset(self):
        with mock.patch.object(serve, 'WEBHOOKS_PATH', '/nonexistent/webhooks.jsonl'):
            self.assertEqual(serve._webhook_read_new(42), ([], 42))


class HandleEntryTest(unittest.TestCase):
    def tearDown(self):
        serve.X_HANDLE = ''
        serve._WEBHOOK_SEEN.clear()

    def _entry(self, wid='a', **payload):
        p = {'id': wid}
        p.update(payload)
        return {'channel': 'x-mention', 'payload': p}

    def test_unknown_channel_is_dropped(self):
        self.assertEqual(
            asyncio.run(serve._webhook_handle_entry({'channel': 'nope', 'payload': {'id': 'a'}})),
            'dropped')

    def test_entry_without_id_is_dropped(self):
        self.assertEqual(
            asyncio.run(serve._webhook_handle_entry({'channel': 'x-mention', 'payload': {}})),
            'dropped')

    def test_duplicate_is_seen_before_any_work(self):
        serve.X_HANDLE = 'ourhandle'
        entry = self._entry()
        with mock.patch.object(serve, '_state_begin') as begin, \
             mock.patch.object(serve, '_dispatch_big_task', new=mock.AsyncMock()) as dispatch:
            asyncio.run(serve._webhook_handle_entry(entry))
            asyncio.run(serve._webhook_handle_entry(entry))
        self.assertEqual(dispatch.await_count, 1)
        begin.assert_called_once()

    def test_own_activity_is_not_dispatched(self):
        serve.X_HANDLE = 'ourhandle'
        with mock.patch.object(serve, '_state_begin') as begin, \
             mock.patch.object(serve, '_dispatch_big_task', new=mock.AsyncMock()) as dispatch:
            outcome = asyncio.run(serve._webhook_handle_entry(self._entry(author='ourhandle')))
        self.assertEqual(outcome, 'self')
        dispatch.assert_not_awaited()
        begin.assert_not_called()

    def test_external_ask_is_dispatched_as_external(self):
        serve.X_HANDLE = 'ourhandle'
        state = {'teams': []}
        dispatch = mock.AsyncMock(return_value=('staffed', {'teamName': 'Doc', 'request': 7}))
        with mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_dispatch_big_task', new=dispatch):
            outcome = asyncio.run(serve._webhook_handle_entry(
                self._entry(author='Ada', text='fix the thing')))
        self.assertEqual(outcome, 'staffed')
        dispatch.assert_awaited_once_with(
            state, 'X mention from Ada -- fix the thing', 'external')

    def test_missing_state_drops_without_dispatch(self):
        serve.X_HANDLE = 'ourhandle'
        with mock.patch.object(serve, '_state_begin', return_value=None), \
             mock.patch.object(serve, '_dispatch_big_task', new=mock.AsyncMock()) as dispatch:
            self.assertEqual(
                asyncio.run(serve._webhook_handle_entry(self._entry())), 'no-state')
        dispatch.assert_not_awaited()

    def test_dispatch_failure_is_contained(self):
        serve.X_HANDLE = 'ourhandle'
        with mock.patch.object(serve, '_state_begin', return_value={'teams': []}), \
             mock.patch.object(serve, '_dispatch_big_task',
                               new=mock.AsyncMock(side_effect=RuntimeError('boom'))):
            self.assertEqual(
                asyncio.run(serve._webhook_handle_entry(self._entry())), 'failed')


class ChannelRegistryTest(unittest.TestCase):
    def test_channel_is_disabled_until_handle_is_configured(self):
        serve.X_HANDLE = ''
        self.assertFalse(webhooks._webhook_channel_enabled('x-mention'))
        serve.X_HANDLE = 'ourhandle'
        try:
            self.assertTrue(webhooks._webhook_channel_enabled('x-mention'))
        finally:
            serve.X_HANDLE = ''

    def test_unknown_channel_is_never_enabled(self):
        self.assertFalse(webhooks._webhook_channel_enabled('nope'))

    def test_active_list_follows_config(self):
        # github is gated on its producer's env; with the repo unset and no
        # X handle, nothing is active.
        with mock.patch.object(webhooks, 'CHANNELS',
                               {k: v for k, v in webhooks.CHANNELS.items()
                                if k != 'github'}):
            serve.X_HANDLE = ''
            try:
                self.assertEqual(webhooks.active_webhook_channels(), ())
                serve.X_HANDLE = 'ourhandle'
                self.assertEqual(webhooks.active_webhook_channels(), ('x-mention',))
            finally:
                serve.X_HANDLE = ''

    def test_github_channel_follows_the_watch_repo(self):
        # no repo configured -> channel off; repo set -> on
        def env_without(key):
            env = serve._load_env()
            return {k: v for k, v in env.items() if k != key}
        with mock.patch.object(serve, '_load_env', lambda: {}):
            self.assertFalse(webhooks._webhook_channel_enabled('github'))
        with mock.patch.object(serve, '_load_env',
                               lambda: {'GITHUB_WATCH_REPO': 'owner/repo'}):
            self.assertTrue(webhooks._webhook_channel_enabled('github'))

    def test_descriptor_carries_goal_and_gate(self):
        spec = webhooks.CHANNELS['x-mention']
        self.assertEqual(spec['own_handle_attr'], 'X_HANDLE')
        self.assertIn('text', spec['goal']['body_fields'])
        self.assertEqual(tuple(webhooks.CHANNELS), serve.WEBHOOK_CHANNELS)


class EndpointTest(unittest.TestCase):
    """The endpoint is a pure sink: validate channel + key, append one line."""

    def _request(self, body, key='secret'):
        req = mock.Mock()
        req.headers = {'X-Webhook-Key': key} if key is not None else {}
        req.json = mock.AsyncMock(return_value=body)
        return req

    def test_unknown_channel_is_404(self):
        with mock.patch.object(serve, 'WEBHOOK_API_KEY', 'secret'):
            resp = asyncio.run(serve.webhook_channel('nope', self._request({'id': 'a'})))
        self.assertEqual(resp.status_code, 404)

    def test_missing_or_wrong_key_is_401_and_stores_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'webhooks.jsonl')
            with mock.patch.object(serve, 'WEBHOOKS_PATH', path), \
                 mock.patch.object(serve, 'WEBHOOK_API_KEY', 'secret'):
                for key in (None, 'wrong'):
                    resp = asyncio.run(serve.webhook_channel(
                        'x-mention', self._request({'id': 'a'}, key=key)))
                    self.assertEqual(resp.status_code, 401)
            self.assertFalse(os.path.exists(path))

    def test_malformed_and_empty_bodies_are_400(self):
        with mock.patch.object(serve, 'WEBHOOK_API_KEY', 'secret'):
            bad = mock.Mock()
            bad.headers = {'X-Webhook-Key': 'secret'}
            bad.json = mock.AsyncMock(side_effect=ValueError('no body'))
            self.assertEqual(asyncio.run(serve.webhook_channel('x-mention', bad)).status_code, 400)
            self.assertEqual(
                asyncio.run(serve.webhook_channel(
                    'x-mention', self._request({}))).status_code, 400)

    def test_valid_event_appends_one_tagged_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'webhooks.jsonl')
            with mock.patch.object(serve, 'WEBHOOKS_PATH', path), \
                 mock.patch.object(serve, 'WEBHOOK_API_KEY', 'secret'), \
                 mock.patch.object(serve, 'log_action') as log:
                resp = asyncio.run(serve.webhook_channel(
                    'x-mention', self._request({'id': 'n1', 'author': 'Ada'})))
                self.assertEqual(resp.status_code, 200)
            with open(path, encoding='utf-8') as f:
                lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['channel'], 'x-mention')
        self.assertEqual(lines[0]['payload']['id'], 'n1')
        self.assertIn('receivedAt', lines[0])
        log.assert_called_once()
        self.assertEqual(log.call_args[0][0], 'external')
