"""On-demand iMessage fetcher (scripts/imessage_fetch.py).

The Mac-side half of the receive-only SMS inbox: reads NEW incoming iMessages
from a chat.db stand-in and forwards each to the webhook. Covers the watermark
(first run never floods the inbox), dedupe by guid, the iMessage-only default,
nanosecond date normalization, and that a failed forward leaves the watermark
put so the message is retried on the next sweep.

Hermetic: temp sqlite with the chat.db shape the script queries; the webhook
POST is mocked. No Messages.app, no network, no real chat.db (that needs Full
Disk Access and never appears in CI).
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'scripts')
sys.path.insert(0, _SCRIPTS)

import imessage_fetch as fetch  # noqa: E402


class _FetchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='imessage-fetch-')
        self.db = os.path.join(self.tmp, 'chat.db')
        self.state_path = os.path.join(self.tmp, 'state.json')
        self._state_cm = unittest.mock.patch.object(fetch, 'STATE_PATH', self.state_path)
        self._state_cm.start()
        self.addCleanup(self._state_cm.stop)
        conn = sqlite3.connect(self.db)
        conn.executescript('''
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY, guid TEXT, date REAL,
                handle_id INTEGER, is_from_me INTEGER, text TEXT, service TEXT);
        ''')
        conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15550001')")
        conn.commit()
        conn.close()
        self.now = time.time() - fetch.APPLE_EPOCH_OFFSET

    def _add(self, guid, apple_date, text, is_from_me=0, service='iMessage', handle_id=1):
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO message (guid, date, handle_id, is_from_me, text, service) '
                     'VALUES (?, ?, ?, ?, ?, ?)',
                     (guid, apple_date, handle_id, is_from_me, text, service))
        conn.commit()
        conn.close()

    def _state(self, watermark=None):
        return {'appleWatermark': self.now if watermark is None else watermark, 'sentGuids': []}


class DateNormalization(_FetchCase):
    def test_seconds_and_nanoseconds(self):
        self.assertEqual(fetch._norm_apple(123.5), 123.5)
        self.assertAlmostEqual(fetch._to_unix(0), fetch.APPLE_EPOCH_OFFSET)
        # Realistic magnitudes: ~8e8 seconds vs ~8e17 nanoseconds since 2001.
        # (Anything under the 1e12 cutoff is read as seconds.)
        sec, ns = 8.1e8, 8.1e8 * 1e9
        self.assertEqual(fetch._norm_apple(ns), sec)
        self.assertAlmostEqual(fetch._to_unix(sec), sec + fetch.APPLE_EPOCH_OFFSET)
        self.assertAlmostEqual(fetch._to_unix(ns), sec + fetch.APPLE_EPOCH_OFFSET)


class FirstRun(_FetchCase):
    def test_missing_state_initializes_to_now_and_forwards_nothing(self):
        state = fetch._load_state(self.state_path)
        self.assertIsNone(state)
        with unittest.mock.patch.object(fetch, '_forward') as fwd:
            state = {'appleWatermark': fetch._apple_now(), 'sentGuids': []}
            fetch._save_state(self.state_path, state)
            n = fetch._sweep(self.db, state, 'http://x', 's', False, False)
        self.assertEqual(n, 0)
        fwd.assert_not_called()


class Sweep(_FetchCase):
    def test_new_incoming_imessage_forwarded_once(self):
        self._add('g1', self.now + 10, 'Your code is 123456.')
        state = self._state()
        with unittest.mock.patch.object(fetch, '_forward', return_value={'messageId': 7}) as fwd:
            self.assertEqual(fetch._sweep(self.db, state, 'http://x', 's', False, False), 1)
            self.assertEqual(fetch._sweep(self.db, state, 'http://x', 's', False, False), 0)
        fwd.assert_called_once_with('http://x', 's', '+15550001', 'Your code is 123456.')
        self.assertEqual(state['sentGuids'], ['g1'])
        self.assertEqual(state['appleWatermark'], self.now + 10)

    def test_outgoing_and_empty_bodies_skipped(self):
        self._add('g1', self.now + 10, 'my own text', is_from_me=1)
        self._add('g2', self.now + 11, '   ')
        self._add('g3', self.now + 12, 'real one')
        state = self._state()
        with unittest.mock.patch.object(fetch, '_forward', return_value={'messageId': 1}) as fwd:
            self.assertEqual(fetch._sweep(self.db, state, 'http://x', 's', False, False), 1)
        self.assertEqual(fwd.call_args[0][3], 'real one')

    def test_sms_relay_only_with_flag(self):
        self._add('g1', self.now + 10, 'carrier text', service='SMS')
        state = self._state()
        with unittest.mock.patch.object(fetch, '_forward', return_value={'messageId': 1}) as fwd:
            self.assertEqual(fetch._sweep(self.db, state, 'http://x', 's', False, False), 0)
            self.assertEqual(fetch._sweep(self.db, state, 'http://x', 's', True, False), 1)
        fwd.assert_called_once()

    def test_failed_forward_keeps_watermark_for_retry(self):
        self._add('g1', self.now + 10, 'Your code is 999888.')
        state = self._state()
        with unittest.mock.patch.object(fetch, '_forward', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                fetch._sweep(self.db, state, 'http://x', 's', False, False)
        self.assertEqual(state['appleWatermark'], self.now)
        with unittest.mock.patch.object(fetch, '_forward', return_value={'messageId': 2}) as fwd:
            self.assertEqual(fetch._sweep(self.db, state, 'http://x', 's', False, False), 1)
        fwd.assert_called_once()

    def test_dry_run_reports_but_never_posts(self):
        self._add('g1', self.now + 10, 'Your code is 111222.')
        state = self._state()
        with unittest.mock.patch.object(fetch, '_forward') as fwd:
            self.assertEqual(fetch._sweep(self.db, state, 'http://x', 's', False, True), 0)
        fwd.assert_not_called()
        self.assertEqual(state['appleWatermark'], self.now)

    def test_nanosecond_dbs_do_not_leak_history(self):
        # Big Sur+ stores message.date as nanoseconds. A seconds-based
        # watermark must be scaled before the SQL comparison or every
        # historical message would beat it.
        old, new = self.now - 100000, self.now + 10
        self._add('g-old', old * 1e9, 'ancient history')
        self._add('g-new', new * 1e9, 'Your code is 777000.')
        state = self._state()
        with unittest.mock.patch.object(fetch, '_forward', return_value={'messageId': 9}) as fwd:
            self.assertEqual(fetch._sweep(self.db, state, 'http://x', 's', False, False), 1)
        fwd.assert_called_once_with('http://x', 's', '+15550001', 'Your code is 777000.')
        self.assertEqual(state['appleWatermark'], new)


class Main(_FetchCase):
    def test_main_end_to_end_with_mocked_webhook(self):
        self._add('g1', self.now + 10, 'Your code is 444555.')
        env = {'SMS_FORWARD_SECRET': 'sec', 'IMESSAGE_CHAT_DB': self.db,
               'IMESSAGE_STATE_FILE': self.state_path, 'IMESSAGE_FORWARD_URL': 'http://web'}
        with unittest.mock.patch.object(fetch, '_load_env', return_value=env), \
                unittest.mock.patch.object(fetch, '_forward', return_value={'messageId': 3}) as fwd, \
                unittest.mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(fetch.main([]), 0)
        fwd.assert_called_once_with('http://web', 'sec', '+15550001', 'Your code is 444555.')

    def test_main_requires_secret(self):
        with unittest.mock.patch.object(fetch, '_load_env', return_value={}):
            self.assertEqual(fetch.main([]), 2)


if __name__ == '__main__':
    unittest.main()
