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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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


class FeedTempAccess(unittest.TestCase):
    """Agent-granted bounded feed windows (the 2026-11-16 operator ask): any
    authenticated agent can switch a registered feed ON for <=24h -- including
    a feed a director removed -- and end the window early, audited, permanent
    status untouched. This is what lets the village re-enable the feeds that
    were disabled for having zero consumers, without operator round-trips."""

    def setUp(self):
        self.key = serve.get_or_create_agent_key('ben')
        self.headers = {'X-Agent-Key': self.key}

    def _set_row_status(self, table, feed_id, status):
        with serve._db() as conn:
            conn.execute(f'UPDATE {table} SET status = ? WHERE id = ?',  # nosec B608 -- fixed internal table
                         (status, feed_id))
        serve._load_ws_feeds()
        serve._load_rss_feeds()

    def test_removed_ws_feed_serves_only_within_window(self):
        self._set_row_status('ws_feeds', 'kraken', 'removed')
        self.assertNotIn('kraken', serve._WS_FEEDS)
        ok, msg = serve.set_ws_feed_temp_window('kraken', 2, 'ben', 'pricing task')
        self.assertTrue(ok, msg)
        self.assertEqual(serve._feed_registry_status('ws_feeds', 'kraken'), 'removed',
                         'a temp window must never touch the permanent status')
        self.assertIn('kraken', serve._WS_FEEDS)
        self.assertEqual(serve._WS_FEEDS['kraken']['temp_granted_by'], 'ben')
        self.assertGreater(serve._WS_FEEDS['kraken']['temp_until_s'], 0)
        ok, msg = serve.set_ws_feed_temp_window('kraken', 0, 'ben')
        self.assertTrue(ok, msg)
        serve._load_ws_feeds()
        self.assertNotIn('kraken', serve._WS_FEEDS)
        self.assertIsNone(serve._WS_FEEDS_LOAD_ERROR)

    def test_expired_window_drops_feed_on_reload(self):
        self._set_row_status('ws_feeds', 'aisstream', 'removed')
        ok, _ = serve.set_ws_feed_temp_window('aisstream', 2, 'ben', 'port watch')
        self.assertTrue(ok)
        self.assertIn('aisstream', serve._WS_FEEDS)
        with serve._db() as conn:
            conn.execute("UPDATE ws_feeds SET temp_until_s = ? WHERE id = 'aisstream'",
                         (0.0,))  # window lapsed
        serve._load_ws_feeds()
        self.assertNotIn('aisstream', serve._WS_FEEDS)
        serve.set_ws_feed_temp_window('aisstream', 0, 'ben')

    def test_hours_bounds(self):
        self.assertFalse(serve.set_ws_feed_temp_window('kraken', 25, 'ben')[0])
        self.assertFalse(serve.set_ws_feed_temp_window('kraken', -1, 'ben')[0])
        self.assertFalse(serve.set_ws_feed_temp_window('kraken', 'soon', 'ben')[0])
        self.assertFalse(serve.set_ws_feed_temp_window('', 2, 'ben')[0])
        self.assertFalse(serve.set_ws_feed_temp_window('no_such_feed', 2, 'ben')[0])

    def test_removed_rss_feed_serves_only_within_window(self):
        self._set_row_status('rss_feeds', 'bbc_world', 'removed')
        self.assertNotIn('bbc_world', serve._RSS_FEEDS)
        ok, _ = serve.set_rss_feed_temp_window('bbc_world', 4, 'ben', 'news brief')
        self.assertTrue(ok)
        self.assertEqual(serve._feed_registry_status('rss_feeds', 'bbc_world'), 'removed')
        self.assertIn('bbc_world', serve._RSS_FEEDS)
        ok, _ = serve.set_rss_feed_temp_window('bbc_world', 0, 'ben')
        self.assertTrue(ok)
        serve._load_rss_feeds()
        self.assertNotIn('bbc_world', serve._RSS_FEEDS)

    def test_active_feed_window_is_inert_on_lapse(self):
        # provider_status feeds must stay serving for the health checks; a
        # lapsed window on an ACTIVE feed changes nothing.
        ok, _ = serve.set_rss_feed_temp_window('provider_status_openai', 1, 'ben')
        self.assertTrue(ok)
        with serve._db() as conn:
            conn.execute("UPDATE rss_feeds SET temp_until_s = 0.0 "
                         "WHERE id = 'provider_status_openai'")
        serve._load_rss_feeds()
        self.assertIn('provider_status_openai', serve._RSS_FEEDS)
        serve.set_rss_feed_temp_window('provider_status_openai', 0, 'ben')

    def test_endpoint_grants_clears_and_audits(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        self._set_row_status('ws_feeds', 'kraken', 'removed')
        with _as_player():
            r = c.post('/api/feeds/access',
                       json={'kind': 'ws', 'feedId': 'kraken', 'hours': 4,
                             'agentId': 'ben', 'purpose': 'quotes for a story'},
                       headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('kraken', serve._WS_FEEDS)
        self.assertEqual(r.json()['permanentStatus'], 'removed')
        with _as_player():
            r = c.post('/api/feeds/access',
                       json={'kind': 'ws', 'feedId': 'kraken', 'hours': 0, 'agentId': 'ben'},
                       headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        serve._load_ws_feeds()
        self.assertNotIn('kraken', serve._WS_FEEDS)
        with serve._db() as conn:
            actions = [row[0] for row in conn.execute(
                "SELECT action FROM action_log WHERE action IN "
                "('feed_access_granted', 'feed_access_cleared')").fetchall()]
        self.assertIn('feed_access_granted', actions)
        self.assertIn('feed_access_cleared', actions)

    def test_endpoint_requires_auth(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, '_require_player_session',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=(False, None)):
            r = c.post('/api/feeds/access',
                       json={'kind': 'ws', 'feedId': 'kraken', 'hours': 2})
        self.assertEqual(r.status_code, 401)

    def test_endpoint_rejects_bad_requests(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        for body in ({'kind': 'ftp', 'feedId': 'kraken', 'hours': 2},
                     {'kind': 'ws', 'feedId': 'no_such', 'hours': 2},
                     {'kind': 'ws', 'feedId': 'kraken', 'hours': 100}):
            with _as_player():
                r = c.post('/api/feeds/access', json=body, headers=self.headers)
            self.assertEqual(r.status_code, 400, body)

    def test_operator_list_shows_temp_columns(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        serve.set_ws_feed_temp_window('kraken', 3, 'ben', 'list view')
        with _as_player():
            r = c.get('/api/ws/feeds')
        self.assertEqual(r.status_code, 200)
        row = {f['id']: f for f in r.json()['feeds']}['kraken']
        self.assertIn('temp_until_s', row)
        self.assertEqual(row['temp_granted_by'], 'ben')
        serve.set_ws_feed_temp_window('kraken', 0, 'ben')


if __name__ == '__main__':
    unittest.main()