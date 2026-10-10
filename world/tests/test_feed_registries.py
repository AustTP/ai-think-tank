"""Operator-managed feed registries.

Both RSS feeds and WebSocket feeds are runtime-administered like API
services: DB-backed registries (rss_feeds, ws_feeds), propose/approve/remove
endpoints, re-validation at approval time, and no-restart effect (the RSS
poll loop re-reads the registry each cycle; the WebSocket supervisor spawns/
cancels collector tasks from the registry). The generic WebSocket collector
interprets a declarative descriptor via _ws_extract_frame/_ws_resolve_path.

Hermetic: no live network, no real DB writes (temp dir), gates patched.
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
    # Isolate the shared in-process rate limiter: many test files reuse agent
    # 'ben', and a full-suite run can trip the 60s/20-call window, turning
    # expected 400/422s into 429s. Each file starts with a clean window.
    serve._rate_limit_calls.clear()
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-feed-registry-test-')
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


def _as_player():
    return unittest.mock.patch.object(serve, '_require_player_session', return_value=True)


class RSSValidator(unittest.TestCase):
    def test_valid_spec_normalizes(self):
        fid, norm, err = serve._validate_rss_feed_spec(
            {'id': 'my_feed-2', 'name': 'My Feed', 'url': 'https://example.com/rss.xml',
             'interval_s': 120})
        self.assertIsNone(err)
        self.assertEqual(fid, 'my_feed-2')
        self.assertEqual(norm['interval_s'], 120)

    def test_rejects_bad_urls(self):
        for url in ('http://example.com/rss', 'https://192.168.1.1/rss',
                    'https://localhost/rss', 'ftp://example.com/rss', 'not a url'):
            _, _, err = serve._validate_rss_feed_spec(
                {'id': 'x', 'url': url})
            self.assertIsNotNone(err, url)

    def test_rejects_bad_slug(self):
        _, _, err = serve._validate_rss_feed_spec(
            {'id': 'bad id!', 'url': 'https://example.com/rss'})
        self.assertIsNotNone(err)

    def test_interval_clamped(self):
        _, norm, _ = serve._validate_rss_feed_spec(
            {'id': 'x', 'url': 'https://example.com/rss', 'interval_s': 1})
        self.assertEqual(norm['interval_s'], 60)
        _, norm, _ = serve._validate_rss_feed_spec(
            {'id': 'x', 'url': 'https://example.com/rss', 'interval_s': 999999})
        self.assertEqual(norm['interval_s'], 86400)


class WSValidator(unittest.TestCase):
    def _good_spec(self):
        return {'id': 'myfeed', 'name': 'My Feed',
                'credential': {'type': 'none'},
                'ws': {'url': 'wss://example.com/stream',
                       'subscribe': {'event': 'sub'},
                       'frame_filter': {'data_kind': 'dict', 'topic_path': 't',
                                        'fields': {'v': 'v'}},
                       'buffer': {'kind': 'latest_per_topic'}}}

    def test_valid_spec(self):
        sid, norm, err = serve._validate_ws_feed_spec(self._good_spec())
        self.assertIsNone(err)
        self.assertEqual(sid, 'myfeed')

    def test_requires_wss_public(self):
        spec = self._good_spec()
        for url in ('ws://example.com/stream', 'wss://localhost/stream',
                    'wss://10.0.0.1/stream'):
            spec['ws']['url'] = url
            _, _, err = serve._validate_ws_feed_spec(spec)
            self.assertIsNotNone(err, url)

    def test_requires_known_credential_and_filter(self):
        spec = self._good_spec()
        spec['credential'] = {'type': 'plaintext'}
        _, _, err = serve._validate_ws_feed_spec(spec)
        self.assertIsNotNone(err)
        spec = self._good_spec()
        spec['ws']['frame_filter'] = {'data_kind': 'xml', 'fields': {}}
        _, _, err = serve._validate_ws_feed_spec(spec)
        self.assertIsNotNone(err)

    def test_clamps_timers(self):
        spec = self._good_spec()
        spec['ws']['stall_timeout_s'] = 5
        spec['ws']['reconnect_backoff_s'] = 1
        _, norm, err = serve._validate_ws_feed_spec(spec)
        self.assertIsNone(err)
        self.assertEqual(norm['ws']['stall_timeout_s'], 15)
        self.assertEqual(norm['ws']['reconnect_backoff_s'], 5)


class PathResolver(unittest.TestCase):
    def test_dict_and_list_paths(self):
        frame = {'MetaData': {'ShipName': 'EVER GIVEN', 'latitude': 36.8},
                 'c': ['100.0', '2']}
        self.assertEqual(serve._ws_resolve_path(frame, 'MetaData.ShipName'), 'EVER GIVEN')
        self.assertEqual(serve._ws_resolve_path(frame, 'c[0]'), '100.0')
        self.assertEqual(serve._ws_resolve_path([10, 20], '[1]'), 20)
        self.assertIsNone(serve._ws_resolve_path(frame, '[0]'))  # dict indexed as a list

    def test_missing_paths_return_none(self):
        frame = {'a': {'b': 1}}
        self.assertIsNone(serve._ws_resolve_path(frame, 'a.x'))
        self.assertIsNone(serve._ws_resolve_path(frame, 'a.b.c'))
        self.assertIsNone(serve._ws_resolve_path(frame, 'b[0]'))
        self.assertIsNone(serve._ws_resolve_path({}, 'a'))


class FrameExtraction(unittest.TestCase):
    KRAKEN = {'frame_filter': {'data_kind': 'list', 'data_index': 1, 'topic_path': '[3]',
                               'fields': {'price': 'c[0]', 'bid': 'b[0]'}}}
    AIS = {'frame_filter': {'data_kind': 'dict', 'topic_path': 'MetaData.ShipName',
                            'skip_on_key': 'MessageType', 'skip_values': ['SubscriptionConfirmation'],
                            'fields': {'lat': 'MetaData.latitude'}}}

    def test_kraken_list_frame(self):
        topic, fields, raw = serve._ws_extract_frame(
            self.KRAKEN, [123, {'c': ['82550.0', '1'], 'b': ['82549.0', '1']}, 'ticker', 'XBT/USD'])
        self.assertEqual(topic, 'XBT/USD')
        self.assertEqual(fields['price'], '82550.0')
        self.assertEqual(fields['bid'], '82549.0')
        self.assertEqual(raw[0], 123)

    def test_kraken_dict_control_frames_skipped(self):
        for frame in ({'event': 'heartbeat'}, {'event': 'systemStatus'},
                      {'event': 'subscriptionStatus', 'status': 'subscribed'}):
            self.assertEqual(serve._ws_extract_frame(self.KRAKEN, frame), (None, None, None))

    def test_kraken_malformed_skipped(self):
        for frame in ('nonsense', [], [1], [1, 'x'], [1, {}, 'ticker']):
            self.assertEqual(serve._ws_extract_frame(self.KRAKEN, frame), (None, None, None))

    def test_ais_dict_frame(self):
        frame = {'MessageType': 'PositionReport',
                 'MetaData': {'ShipName': 'EVER GIVEN', 'latitude': 36.8}}
        topic, fields, raw = serve._ws_extract_frame(self.AIS, frame)
        self.assertEqual(topic, 'EVER GIVEN')
        self.assertEqual(fields['lat'], 36.8)
        self.assertIs(raw['MessageType'], 'PositionReport')

    def test_ais_subscription_confirmation_skipped(self):
        frame = {'MessageType': 'SubscriptionConfirmation', 'MetaData': {}}
        self.assertEqual(serve._ws_extract_frame(self.AIS, frame), (None, None, None))


class SubscriptionSubstitution(unittest.TestCase):
    def test_cred_and_vars_substituted(self):
        ws = {'subscribe': {'APIKey': '$CRED', 'pair': ['$PAIRS']},
              'vars': {'$PAIRS': ['XBT/USD']}}
        out = serve._ws_subscribe_payload(ws, 'sekret')
        self.assertEqual(out, {'APIKey': 'sekret', 'pair': ['XBT/USD']})


class RSSRegistryFlow(unittest.TestCase):
    def setUp(self):
        self.key = serve.get_or_create_agent_key('ben')
        self.headers = {'X-Agent-Key': self.key}

    def tearDown(self):
        with serve._RSS_BUFFER_LOCK:
            serve._RSS_BUFFER.clear()

    def test_propose_approve_remove_end_to_end(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, '_probe_rss_url',
                                        return_value={'ok': True, 'itemCount': 7,
                                                      'sampleTitle': 'First item', 'error': ''}):
            r = c.post('/api/rss/propose', json={
                'agentId': 'ben', 'feed': {'id': 'myfeed', 'name': 'My Feed',
                                           'url': 'https://example.com/rss.xml'}},
                headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(serve._feed_registry_status('rss_feeds', 'myfeed'), 'proposed')
        # Non-director cannot approve.
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='ben'), \
             unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=False):
            r = c.post('/api/rss/approve', json={'feedId': 'myfeed'}, headers=self.headers)
        self.assertEqual(r.status_code, 403)
        # Director approves -> active and live in the registry.
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='director1'), \
             unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=True):
            r = c.post('/api/rss/approve', json={'feedId': 'myfeed'}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(serve._feed_registry_status('rss_feeds', 'myfeed'), 'active')
        self.assertIn('myfeed', serve._RSS_FEEDS)
        # Remove -> dropped from the live registry, row marked removed.
        with _as_player():
            r = c.post('/api/rss/remove', json={'feedId': 'myfeed'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(serve._feed_registry_status('rss_feeds', 'myfeed'), 'removed')
        self.assertNotIn('myfeed', serve._RSS_FEEDS)
        # Management list shows every row with status.
        with _as_player():
            r = c.get('/api/rss/feeds')
        self.assertEqual(r.status_code, 200)
        ids = {f['id']: f['status'] for f in r.json()['feeds']}
        self.assertEqual(ids['myfeed'], 'removed')
        self.assertEqual(ids['bbc_world'], 'active')

    def test_propose_rejects_dead_url_before_approval(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, '_probe_rss_url',
                                        return_value={'ok': False, 'itemCount': 0,
                                                      'sampleTitle': '', 'error': 'connection refused'}):
            r = c.post('/api/rss/propose', json={
                'agentId': 'ben', 'feed': {'id': 'deadfeed', 'url': 'https://example.com/not-a-feed.xml'}},
                headers=self.headers)
        self.assertEqual(r.status_code, 422)
        self.assertIsNone(serve._feed_registry_status('rss_feeds', 'deadfeed'))

    def test_propose_rejects_internal_url(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/rss/propose', json={
            'agentId': 'ben', 'feed': {'id': 'evil', 'url': 'http://localhost/x.xml'}},
            headers=self.headers)
        self.assertEqual(r.status_code, 400)


class WSRegistryFlow(unittest.TestCase):
    def setUp(self):
        self.key = serve.get_or_create_agent_key('ben')
        self.headers = {'X-Agent-Key': self.key}

    def test_propose_approve_spawns_collector_registration(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        spec = {'id': 'myws', 'name': 'My WS',
                'credential': {'type': 'none'},
                'ws': {'url': 'wss://example.com/stream',
                       'subscribe': {'event': 'sub'},
                       'frame_filter': {'data_kind': 'dict', 'topic_path': 't',
                                        'fields': {'v': 'v'}},
                       'buffer': {'kind': 'latest_per_topic'}}}
        r = c.post('/api/ws/propose', json={'agentId': 'ben', 'feed': spec}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(serve._feed_registry_status('ws_feeds', 'myws'), 'proposed')
        with _as_player():
            r = c.post('/api/ws/approve', json={'feedId': 'myws'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('myws', serve._WS_FEEDS)
        with _as_player():
            r = c.get('/api/ws/feeds')
        self.assertEqual(r.status_code, 200)
        self.assertIn('kraken', {f['id'] for f in r.json()['feeds']})

    def test_propose_rejects_internal_ws_url(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        spec = {'id': 'evilws', 'ws': {'url': 'ws://127.0.0.1/stream',
                                       'subscribe': {'a': 'b'},
                                       'frame_filter': {'data_kind': 'dict', 'fields': {'v': 'v'}},
                                       'buffer': {'kind': 'ring'}}}
        r = c.post('/api/ws/propose', json={'agentId': 'ben', 'feed': spec}, headers=self.headers)
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()