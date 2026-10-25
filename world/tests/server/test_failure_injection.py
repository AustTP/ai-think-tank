"""Fault-injection checks for the durability and browser paths.

These are the failures that erode trust in a running tank: a commit tool that
raises, a stale in-progress ledger row, a screenshot that fails mid-step, and a
checkpoint callback that raises. None of them may crash the executor or leave a
false success behind.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import content
import serve


class FailureInjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='failure-test-')
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

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_commit_tool_exception_is_returned_and_retried(self):
        calls = []

        def raw(name, args):
            calls.append(name)
            raise RuntimeError('boom')

        key = f'fail-{time.time()}'
        first = serve.idempotent_tool_call('a', key, raw, 'create_calendar_event', {'x': 1})
        second = serve.idempotent_tool_call('a', key, raw, 'create_calendar_event', {'x': 1})
        self.assertIn('boom', first)
        self.assertIn('boom', second)
        self.assertEqual(len(calls), 2)

    def test_stale_pending_external_write_is_taken_over(self):
        with serve._db() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS external_writes '
                         '(kind TEXT, key TEXT, result TEXT, created_at REAL, PRIMARY KEY (kind, key))')
            conn.execute('INSERT OR REPLACE INTO external_writes '
                         '(kind, key, result, created_at) VALUES (?, ?, ?, ?)',
                         ('stale', 'one', '', 0))
        result, error, replayed = serve.external_write_once(
            'stale', 'one', lambda: ({'ok': True}, None), pending_s=1)
        self.assertEqual(result, {'ok': True})
        self.assertIsNone(error)
        self.assertFalse(replayed)

    def test_browser_screenshot_failure_is_a_note_not_a_crash(self):
        def fake_http(method, base, path, body, key, timeout=0):
            if body.get('op') == 'goto':
                return {'allowed': True, 'ok': True, 'url': 'https://example.com',
                        'title': 'Example', 'text': 'Welcome', 'fields': [], 'links': []}
            if body.get('op') == 'screenshot':
                raise RuntimeError('browser died')
            return {'allowed': True, 'ok': True, 'url': 'https://example.com',
                    'text': 'Welcome', 'fields': [], 'links': []}

        with unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http):
            fn = content._make_browser_step_executor('cora', 'key', 'sandbox-cora')
            out = json.loads(fn('browser_step', {
                'op': 'goto', 'url': 'https://example.com',
                'expect': 'Welcome', 'screenshot': True}))
        self.assertTrue(out['verified'])
        self.assertFalse(out['screenshotCaptured'])
        self.assertIn('browser died', out['screenshotNote'])
        self.assertEqual(out['session']['lastUrl'], 'https://example.com')

    def test_checkpoint_callback_failure_does_not_break_the_tool_loop(self):
        responses = [
            {'choices': [{'message': {'tool_calls': [{'id': 'c1', 'function': {
                'name': 'read_library_file', 'arguments': '{}'}}]}}]},
            {'choices': [{'message': {'content': 'done'}}]},
        ]

        def broken(*_args, **_kwargs):
            raise RuntimeError('checkpoint disk full')

        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=responses), \
             unittest.mock.patch.object(serve, '_accrue_spend'):
            reply = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'go'}], [], lambda n, a: 'ok',
                max_iterations=2, on_before_tools=broken, on_after_tools=broken)
        self.assertEqual(reply, 'done')


if __name__ == '__main__':
    unittest.main()
