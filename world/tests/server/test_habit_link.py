"""Habit Link (199): the paired browser extension posts metadata-only
browsing events (host, path, title, dwell, scroll, clicks) with a shared
token; the server aggregates per host/day and can surface allowlist
candidates for hosts the owner personally uses. Three off switches: env
HABIT_LINK_ENABLED=0, the live settings toggle, and an unset
HABIT_LINK_TOKEN (fail closed). No network, no extension, no real vault."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    import tempfile
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-habit-link-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        check_rate_limit=lambda aid: True,
        _load_env=lambda: {'HABIT_LINK_TOKEN': 'tok-123'},
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


AUTH = {'X-Habit-Link-Token': 'tok-123'}


def _event(url='https://news.example.com/daily/story', **over):
    ev = {'url': url, 'title': 'Daily news', 'dwellMs': 30000,
          'scrollFrac': 0.5, 'clicks': 2, 'ts': serve.time.time()}
    ev.update(over)
    return ev


class HabitLinkBase(unittest.TestCase):
    def setUp(self):
        for name, val in (('log_action', lambda *a, **k: None),):
            p = mock.patch.object(serve, name, val)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        with serve._db() as conn:
            for table in ('habit_link_days', 'habit_link_seen_hosts'):
                conn.execute(f'DELETE FROM {table}')
        serve._set_setting(serve._HABIT_LINK_ENABLED_SETTING, '1')


class HabitLinkIngest(HabitLinkBase):
    def test_unconfigured_token_fails_closed_501(self):
        with mock.patch.object(serve, '_load_env', lambda: {}):
            r = client().post('/api/habit-link', json={'events': [_event()]})
        self.assertEqual(501, r.status_code)

    def test_disabled_at_server_403(self):
        serve._set_setting(serve._HABIT_LINK_ENABLED_SETTING, '0')
        r = client().post('/api/habit-link', json={'events': [_event()]},
                          headers=AUTH)
        self.assertEqual(403, r.status_code)

    def test_env_hard_off_beats_live_toggle(self):
        serve._set_setting(serve._HABIT_LINK_ENABLED_SETTING, '1')
        env = {'HABIT_LINK_TOKEN': 'tok-123', 'HABIT_LINK_ENABLED': '0'}
        with mock.patch.object(serve, '_load_env', lambda: env):
            r = client().post('/api/habit-link', json={'events': [_event()]},
                              headers=AUTH)
        self.assertEqual(403, r.status_code)

    def test_wrong_token_401(self):
        r = client().post('/api/habit-link',
                          json={'events': [_event()]},
                          headers={'X-Habit-Link-Token': 'nope'})
        self.assertEqual(401, r.status_code)
        r = client().post('/api/habit-link', json={'events': [_event()]})
        self.assertEqual(401, r.status_code)

    def test_events_aggregate_per_host_per_day(self):
        r = client().post('/api/habit-link',
                          json={'events': [_event(), _event(),
                                           _event('https://other.example.org/x')]},
                          headers=AUTH)
        self.assertEqual(200, r.status_code)
        self.assertEqual({'ok': True, 'stored': 2}, r.json())
        with serve._db() as conn:
            rows = dict(conn.execute(
                'SELECT host, hits FROM habit_link_days').fetchall())
        self.assertEqual({'news.example.com': 2, 'other.example.org': 1}, rows)

    def test_query_strings_are_stripped_before_storage(self):
        client().post('/api/habit-link',
                      json={'events': [_event(
                          'https://news.example.com/daily/story?token=SECRET&q=private')]},
                      headers=AUTH)
        with serve._db() as conn:
            rows = conn.execute(
                'SELECT host, day FROM habit_link_days').fetchall()
        self.assertEqual([('news.example.com', rows[0][1])], rows)  # no path column stores the query

    def test_non_http_and_junk_events_dropped(self):
        r = client().post('/api/habit-link',
                          json={'events': [
                              _event('ftp://x.example.com/f'),
                              _event('file:///etc/passwd'),
                              'garbage',
                              None,
                              _event(url='not a url'),
                          ]},
                      headers=AUTH)
        self.assertEqual(200, r.status_code)
        self.assertEqual({'ok': True, 'stored': 0}, r.json())

    def test_numbers_clamped(self):
        client().post('/api/habit-link',
                      json={'events': [_event(dwellMs=10**12, clicks=999,
                                              scrollFrac=42)]},
                      headers=AUTH)
        with serve._db() as conn:
            dwell, clicks, scroll_sum = conn.execute(
                'SELECT dwell_ms, clicks, scroll_sum FROM habit_link_days').fetchone()
        self.assertEqual(60 * 60 * 1000, dwell)
        self.assertEqual(100, clicks)
        self.assertEqual(1.0, scroll_sum)


class HabitLinkSummaryAndToggle(HabitLinkBase):
    def test_summary_operator_only(self):
        r = client().get('/api/habit-link/summary')
        self.assertEqual(401, r.status_code)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')):
            r = client().get('/api/habit-link/summary')
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertTrue(body['enabled'])
        self.assertEqual([], body['hosts'])

    def test_toggle_off_is_visible_in_summary_and_blocks_ingest(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')):
            r = client().post('/api/habit-link/toggle', json={'enabled': False})
        self.assertEqual(200, r.status_code)
        self.assertEqual(403, client().post(
            '/api/habit-link', json={'events': [_event()]}, headers=AUTH).status_code)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')):
            body = client().get('/api/habit-link/summary').json()
        self.assertFalse(body['enabled'])
        self.assertTrue(body['liveEnabled'] is False)
        # and back on
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')):
            client().post('/api/habit-link/toggle', json={'enabled': True})
        self.assertEqual(200, client().post(
            '/api/habit-link', json={'events': [_event()]},
            headers=AUTH).status_code)

    def test_status_endpoint_token_gated(self):
        self.assertEqual(401, client().get('/api/habit-link/status').status_code)
        self.assertEqual(200, client().get('/api/habit-link/status',
                                           headers=AUTH).status_code)


class HabitLinkAllowlistCandidate(HabitLinkBase):
    def _seed_host(self, host='news.example.com', days=3, hits_per_day=4):
        with serve._db() as conn:
            for d in range(days):
                conn.execute(
                    'INSERT INTO habit_link_days (host, day, hits, dwell_ms, '
                    'scroll_sum, scroll_n, clicks, last_ts) '
                    'VALUES (?, date(\'now\', ?), ?, 1000, 2.0, 2, 1, ?)',
                    (host, f'-{d} days', hits_per_day, serve.time.time()))
            conn.execute('INSERT INTO habit_link_seen_hosts (host, '
                         'first_seen_at) VALUES (?, ?)',
                         (host, serve.time.time()))

    def test_candidate_escalates_once(self):
        self._seed_host()
        with mock.patch.object(serve, '_effective_allowlist_domains',
                               return_value=set()), \
             mock.patch.object(serve, 'create_escalation',
                               return_value='esc1') as esc:
            serve._habit_link_maybe_allowlist_candidate('news.example.com')
            self.assertEqual(1, esc.call_count)
            serve._habit_link_maybe_allowlist_candidate('news.example.com')
            self.assertEqual(1, esc.call_count)  # once per host, ever
        self.assertIn('news.example.com', esc.call_args.args[1])

    def test_below_threshold_is_silent(self):
        self._seed_host(days=1, hits_per_day=3)
        with mock.patch.object(serve, '_effective_allowlist_domains',
                               return_value=set()), \
             mock.patch.object(serve, 'create_escalation') as esc:
            serve._habit_link_maybe_allowlist_candidate('news.example.com')
            self.assertEqual(0, esc.call_count)

    def test_already_allowed_host_never_escalates(self):
        self._seed_host()
        with mock.patch.object(serve, '_effective_allowlist_domains',
                               return_value={'news.example.com'}), \
             mock.patch.object(serve, 'create_escalation') as esc:
            serve._habit_link_maybe_allowlist_candidate('news.example.com')
            self.assertEqual(0, esc.call_count)
