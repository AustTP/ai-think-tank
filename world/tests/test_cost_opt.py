import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class CommandCompactionTest(unittest.TestCase):
    def test_short_output_is_unchanged(self):
        self.assertEqual(serve._compact_command_output('ok'), 'ok')

    def test_long_output_keeps_head_and_tail(self):
        text = 'HEAD' + ('x' * 50000) + 'TAIL'
        out = serve._compact_command_output(text)
        self.assertLessEqual(len(out), serve.COMMAND_CONTEXT_MAX + 80)
        self.assertTrue(out.startswith('HEAD'))
        self.assertTrue(out.endswith('TAIL'))
        self.assertIn('chars omitted', out)


class HttpCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='http-cache-test-')
        self.old = serve.HTTP_CACHE_DIR
        serve.HTTP_CACHE_DIR = self.tmp

    def tearDown(self):
        serve.HTTP_CACHE_DIR = self.old

    def test_round_trip_and_expiry(self):
        serve._write_http_cache('https://example.test/a', None,
                                'https://example.test/a', 'text/html',
                                '<h1>Hello</h1>', False, '"etag-1"', 123, 3600)
        got = serve._read_http_cache('https://example.test/a')
        self.assertEqual(got['body'], '<h1>Hello</h1>')
        self.assertEqual(got['etag'], '"etag-1"')
        self.assertEqual(got['last_modified'], 123)

    def test_no_store_is_not_cached(self):
        self.assertIsNone(serve._cache_ttl_from_headers({'Cache-Control': 'no-store'}))
        self.assertEqual(serve._cache_ttl_from_headers({'Cache-Control': 'no-cache'}), 0)
        self.assertEqual(serve._cache_ttl_from_headers({'Cache-Control': 'max-age=60'}), 60)


class CostReportTest(unittest.TestCase):
    def test_per_card_summary(self):
        import sqlite3
        import cost_report
        tmp = tempfile.mkdtemp(prefix='cost-report-test-')
        db = os.path.join(tmp, 'think_tank.db')
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE action_log (ts REAL, action TEXT, details TEXT)')
        con.execute('INSERT INTO action_log VALUES (?,?,?)',
                    (time.time(), 'chat', json.dumps({'cost': 0.2, 'taskId': 'task-1', 'title': 'A'})))
        con.execute('INSERT INTO action_log VALUES (?,?,?)',
                    (time.time(), 'chat', json.dumps({'cost': 0.1, 'taskId': 'task-1'})))
        con.execute('INSERT INTO action_log VALUES (?,?,?)',
                    (time.time(), 'task_completed', json.dumps({'taskId': 'task-1'})))
        con.commit(); con.close()
        r = cost_report.summarize(db, 1)
        self.assertEqual(r['completedCards'], 1)
        self.assertAlmostEqual(r['totalCost'], 0.3)
        self.assertAlmostEqual(r['cards'][0]['cost'], 0.3)
