"""Tests for the verified browser step and its durable session checkpoint."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import content
import serve


class BrowserStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='browser-step-test-')
        self.patcher = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            SELF_BASE_URL='http://127.0.0.1:9',
            _mint_work_trust_nonce=lambda: '',
        )
        self.patcher.start()
        content._BROWSER_SESSIONS_READY = False
        self.pages = {
            'goto': {'allowed': True, 'ok': True, 'url': 'https://example.com',
                     'title': 'Example', 'text': 'Welcome to Example',
                     'fields': [], 'links': []},
            'click': {'allowed': True, 'ok': True, 'url': 'https://example.com/done',
                      'title': 'Done', 'text': 'Done',
                      'fields': [], 'links': []},
            'read': {'allowed': True, 'ok': True, 'url': 'https://example.com/done',
                     'title': 'Done', 'text': 'Done', 'fields': [], 'links': []},
            'screenshot': {'allowed': True, 'ok': True, 'screenshotBase64': 'abc'},
        }

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args):
        calls = []

        def fake_http(method, base, path, body, key, timeout=0):
            calls.append(body)
            return dict(self.pages.get(body.get('op'), self.pages['read']))

        with unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http):
            fn = content._make_browser_step_executor('cora', 'key', 'sandbox-cora')
            result = json.loads(fn('browser_step', args))
        return result, calls

    def test_verified_step_and_session_history(self):
        result, calls = self._run({'op': 'goto', 'url': 'https://example.com',
                                    'expect': 'Welcome', 'screenshot': True})
        self.assertTrue(result['verified'])
        self.assertTrue(result['screenshotCaptured'])
        self.assertEqual(len(result['screenshotSha256']), 64)
        self.assertIn('Welcome to Example', result['text'])
        session = result['session']
        self.assertEqual(session['lastUrl'], 'https://example.com')
        self.assertEqual(len(session['history']), 1)
        second, _ = self._run({'op': 'goto', 'url': 'https://example.com',
                               'expect': 'Missing'})
        self.assertFalse(second['verified'])
        self.assertEqual(len(second['session']['history']), 2)
        self.assertEqual(second['session']['lastVerified'], False)

    def test_session_op_reads_without_touching_the_browser(self):
        self._run({'op': 'goto', 'url': 'https://example.com', 'expect': 'Welcome'})
        result, calls = self._run({'op': 'session'})
        self.assertEqual(calls, [])
        self.assertEqual(result['session']['lastUrl'], 'https://example.com')

    def test_blocked_step_is_reported_not_verified(self):
        self.pages['goto'] = {'allowed': False, 'reason': 'host not allowlisted'}
        result, _ = self._run({'op': 'goto', 'url': 'https://evil.example'})
        self.assertFalse(result['ok'])
        self.assertTrue(result['blocked'])
        self.assertIn('allowlisted', result['reason'])

    def test_challenge_page_is_surfaced_with_vendor(self):
        self.pages['goto'] = {'allowed': True, 'ok': False,
                              'blocked': {'vendor': 'cloudflare', 'host': 'example.com',
                                          'status': 403, 'kind': 'challenge'},
                              'error': 'blocked by cloudflare challenge',
                              'hint': 'do not retry', 'url': 'https://example.com'}
        result, _ = self._run({'op': 'goto', 'url': 'https://example.com'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['blocked']['vendor'], 'cloudflare')
        self.assertIn('do not retry', result['hint'])

    def test_operator_takeover_is_surfaced(self):
        self.pages['goto'] = {'allowed': True, 'ok': False,
                              'waitingForOperator': True,
                              'error': 'operator has taken over',
                              'hint': 'read again after handback',
                              'url': 'https://example.com'}
        result, _ = self._run({'op': 'goto', 'url': 'https://example.com'})
        self.assertTrue(result['waitingForOperator'])
        self.assertIn('handback', result['hint'])


if __name__ == '__main__':
    unittest.main()


class BrowserStepVision(BrowserStep):
    def test_visual_expectation_uses_the_vision_tier(self):
        with unittest.mock.patch.object(
                content, '_vision_review_base64',
                return_value={'ok': True, 'reply': 'YES the page shows Done'}):
            result, calls = self._run({'op': 'click', 'selector': '#done',
                                        'expectVisual': 'a Done confirmation'})
        self.assertTrue(result['visualVerified'])
        self.assertEqual(result['checks']['visual'], True)
        self.assertTrue(result['screenshotCaptured'])
        self.assertIn('YES', result['visualEvidence'])
        self.assertTrue(any(c.get('op') == 'screenshot' for c in calls))

    def test_visual_failure_is_unknown_not_false_evidence(self):
        with unittest.mock.patch.object(
                content, '_vision_review_base64',
                return_value={'ok': False, 'note': 'vision tier unavailable'}):
            result, _ = self._run({'op': 'click', 'selector': '#done',
                                   'expectVisual': 'a Done confirmation'})
        self.assertIsNone(result['visualVerified'])
        self.assertIn('vision tier unavailable', result['visionNote'])
