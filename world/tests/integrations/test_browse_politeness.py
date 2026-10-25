"""Tests for the browse politeness layer in serve.py.

Covers per-host pacing, Retry-After retries, challenge stop-and-report, the
block log, and robots.txt parsing. No network and no Docker: the driver and
the robots fetch are replaced with deterministic fakes.

Run: python3 tests/test_browse_politeness.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402


class Pacing(unittest.TestCase):
    def setUp(self):
        with serve._BROWSE_PACE_LOCK:
            serve._BROWSE_PACE.clear()
        self.addCleanup(serve._BROWSE_PACE.clear)

    def test_burst_then_wait(self):
        with unittest.mock.patch.object(serve, '_browse_pace_burst', return_value=1.0), \
                unittest.mock.patch.object(serve, '_browse_pace_rate', return_value=600.0):
            self.assertEqual(serve._browse_pace_wait('a.test'), 0.0)
            waited = serve._browse_pace_wait('a.test')
        self.assertGreater(waited, 0.0)

    def test_hosts_have_separate_buckets(self):
        with unittest.mock.patch.object(serve, '_browse_pace_burst', return_value=1.0), \
                unittest.mock.patch.object(serve, '_browse_pace_rate', return_value=600.0):
            serve._browse_pace_wait('a.test')
            self.assertEqual(serve._browse_pace_wait('b.test'), 0.0)

    def test_empty_host_never_waits(self):
        self.assertEqual(serve._browse_pace_wait(''), 0.0)

    def test_exhausted_budget_raises(self):
        with unittest.mock.patch.object(serve, '_browse_pace_burst', return_value=1.0), \
                unittest.mock.patch.object(serve, '_browse_pace_rate', return_value=1.0):
            serve._browse_pace_wait('a.test')
            with self.assertRaises(TimeoutError):
                serve._browse_pace_wait('a.test', timeout_s=0)


class RobotsParsing(unittest.TestCase):
    TEXT = """
User-agent: *
Disallow: /private
Allow: /private/public
Disallow: /tmp$
"""

    def test_wildcard_group(self):
        rules = serve._robots_rules(self.TEXT, serve._ROBOTS_UA)
        self.assertFalse(serve._robots_path_allowed(rules, '/private/x'))
        self.assertTrue(serve._robots_path_allowed(rules, '/private/public/x'))
        self.assertTrue(serve._robots_path_allowed(rules, '/public'))
        self.assertFalse(serve._robots_path_allowed(rules, '/tmp'))
        self.assertTrue(serve._robots_path_allowed(rules, '/tmp/x'))

    def test_specific_agent_beats_wildcard(self):
        text = ("User-agent: *\nDisallow: /\n\n"
                "User-agent: AIThinkTankAgent\nDisallow: /nope\n")
        rules = serve._robots_rules(text, serve._ROBOTS_UA)
        self.assertTrue(serve._robots_path_allowed(rules, '/ok'))
        self.assertFalse(serve._robots_path_allowed(rules, '/nope'))

    def test_allow_wins_equal_length_tie(self):
        rules = [(False, '/x'), (True, '/x')]
        self.assertTrue(serve._robots_path_allowed(list(rules), '/x'))


class RobotsFetch(unittest.TestCase):
    def setUp(self):
        serve._ROBOTS_CACHE.clear()
        self.addCleanup(serve._ROBOTS_CACHE.clear)

    def _everywhere_disallowed(self):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=None):
                return b'User-agent: *\nDisallow: /\n'
        return _Resp()

    def test_disallow_is_refused(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen',
                                        return_value=self._everywhere_disallowed()):
            allowed, reason = serve._robots_allows('https://site.test/a')
        self.assertFalse(allowed)
        self.assertIn('disallows', reason)

    def test_missing_file_allows(self):
        err = urllib.error.HTTPError('https://site.test/robots.txt', 404, 'gone', {}, None)
        with unittest.mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            allowed, reason = serve._robots_allows('https://site.test/a')
        self.assertTrue(allowed)
        self.assertIn('no robots.txt', reason)

    def test_server_error_refuses(self):
        err = urllib.error.HTTPError('https://site.test/robots.txt', 503, 'down', {}, None)
        with unittest.mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            allowed, reason = serve._robots_allows('https://site.test/a')
        self.assertFalse(allowed)
        self.assertIn('unavailable', reason)

    def test_result_is_cached_per_host(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen',
                                        return_value=self._everywhere_disallowed()) as fetch:
            serve._robots_allows('https://site.test/a')
            serve._robots_allows('https://site.test/b')
        self.assertEqual(fetch.call_count, 1)


class BlockLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-blocks-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, 'blocks.jsonl')
        self.patcher = unittest.mock.patch.object(serve, 'BROWSE_BLOCK_LOG', self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_writes_jsonl(self):
        serve._record_browser_block('agent-a', 'site.test', 'goto',
                                    'https://site.test/x',
                                    {'vendor': 'cloudflare', 'marker': 'cf'}, 403, 'check')
        with open(self.path) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['vendor'], 'cloudflare')
        self.assertEqual(rows[0]['status'], 403)
        self.assertEqual(rows[0]['agentId'], 'agent-a')

    def test_rotates_when_large(self):
        with open(self.path, 'w') as f:
            f.write('x' * (serve.BROWSE_BLOCK_LOG_MAX_BYTES + 1))
        serve._record_browser_block('a', 'h', 'goto', 'u', {}, None)
        self.assertTrue(os.path.isfile(self.path + '.1'))
        self.assertTrue(os.path.isfile(self.path))


class PacedBrowserAct(unittest.TestCase):
    def setUp(self):
        with serve._BROWSE_PACE_LOCK:
            serve._BROWSE_PACE.clear()
        self.sleeps = []
        self.patches = [
            unittest.mock.patch.object(serve, '_browse_pace_wait', return_value=0.0),
            unittest.mock.patch.object(serve.time, 'sleep',
                                       side_effect=self.sleeps.append),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_blocked_page_is_not_retried(self):
        calls = []

        def fake_run(sandbox_dir, action, agent_id=None):
            calls.append(action)
            return {'ok': False, 'blocked': True, 'status': 403,
                    'challenge': {'vendor': 'cloudflare'}}, None

        with unittest.mock.patch.object(serve, '_run_browser_act_sync', side_effect=fake_run):
            parsed, err = serve._browser_act_with_pacing('/tmp', {'op': 'goto', 'url': 'https://s.test'})
        self.assertIsNone(err)
        self.assertTrue(parsed['blocked'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_transient_status_is_retried(self):
        results = iter([
            ({'ok': True, 'status': 503}, None),
            ({'ok': True, 'status': 200}, None),
        ])

        with unittest.mock.patch.object(serve, '_run_browser_act_sync',
                                        side_effect=lambda *a, **k: next(results)):
            parsed, err = serve._browser_act_with_pacing('/tmp', {'op': 'read', 'url': 'https://s.test'})
        self.assertIsNone(err)
        self.assertEqual(parsed['status'], 200)
        self.assertEqual(parsed['retryCount'], 1)
        self.assertEqual(len(self.sleeps), 1)

    def test_retry_after_is_honored(self):
        results = iter([
            ({'ok': True, 'status': 429, 'retryAfter': '4'}, None),
            ({'ok': True, 'status': 200}, None),
        ])

        with unittest.mock.patch.object(serve, '_run_browser_act_sync',
                                        side_effect=lambda *a, **k: next(results)):
            serve._browser_act_with_pacing('/tmp', {'op': 'read', 'url': 'https://s.test'})
        self.assertEqual(self.sleeps, [4.0])

    def test_client_error_is_not_retried(self):
        calls = []

        def fake_run(*a, **k):
            calls.append(1)
            return {'ok': True, 'status': 404}, None

        with unittest.mock.patch.object(serve, '_run_browser_act_sync', side_effect=fake_run):
            parsed, _ = serve._browser_act_with_pacing('/tmp', {'op': 'read', 'url': 'https://s.test'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(parsed['status'], 404)

    def test_pacing_timeout_is_reported(self):
        with unittest.mock.patch.object(serve, '_browse_pace_wait',
                                        side_effect=TimeoutError('too fast')):
            parsed, err = serve._browser_act_with_pacing('/tmp', {'op': 'goto', 'url': 'https://s.test'})
        self.assertIsNone(parsed)
        self.assertIn('too fast', err)


class BlockPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-blocks-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patcher = unittest.mock.patch.object(
            serve, 'BROWSE_BLOCK_LOG', os.path.join(self.tmp, 'blocks.jsonl'))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_sets_blocked_and_error(self):
        parsed = {'url': 'https://site.test/x', 'status': 403,
                  'challenge': {'vendor': 'akamai', 'marker': 'reference block'}}
        out = serve._browser_block_payload(parsed, 'agent-a', 'goto',
                                           'https://site.test/x', 'research')
        self.assertFalse(out['ok'])
        self.assertEqual(out['blocked']['vendor'], 'akamai')
        self.assertEqual(out['blocked']['host'], 'site.test')
        self.assertIn('do not retry', out['hint'].lower())
        self.assertTrue(os.path.isfile(serve.BROWSE_BLOCK_LOG))


if __name__ == '__main__':
    unittest.main()
