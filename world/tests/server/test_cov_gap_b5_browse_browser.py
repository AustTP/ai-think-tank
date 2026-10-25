"""Coverage-gap tests for serve_parts 110, 120 and 130."""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import serve


class _TempMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cov-b5-browse-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patcher = mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'db.sqlite'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            BROWSER_PROFILE_DIR=os.path.join(self.tmp, 'profiles'),
            HTTP_CACHE_DIR=os.path.join(self.tmp, '.http_cache'),
            BROWSE_BLOCK_LOG=os.path.join(self.tmp, 'blocks.jsonl'),
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        serve.init_db()


def _binarycookies_page(page):
    return b'cook' + struct.pack('>I', 1) + struct.pack('>I', len(page)) + page


class SafariCookieTest(_TempMixin, unittest.TestCase):
    def test_parse_bad_header_and_bad_page(self):
        self.assertEqual(serve._parse_safari_binarycookies(b'nope'), [])
        bad_page = b'XXXX' + b'\x00' * 20
        self.assertEqual(
            serve._parse_safari_binarycookies(_binarycookies_page(bad_page)), [])

    def test_parse_truncated_cookie(self):
        page = b'\x00\x00\x01\x00' + struct.pack('<I', 1) + struct.pack('<I', 8)
        self.assertEqual(
            serve._parse_safari_binarycookies(_binarycookies_page(page)), [])

    def test_sync_requires_agent_and_host(self):
        self.assertEqual(serve._sync_safari_cookies('', 'facebook.com'), 0)
        self.assertEqual(serve._sync_safari_cookies('w1', ''), 0)
        with mock.patch.object(serve, '_load_env',
                               return_value={'BROWSER_SAFARI_COOKIE_SYNC': '0'}):
            self.assertEqual(serve._sync_safari_cookies('w1', 'facebook.com'), 0)

    def _env(self):
        return {
            'BROWSER_SAFARI_COOKIE_SYNC': '1',
            'SAFARI_COOKIE_CONSENT': '1',
            'SAFARI_COOKIE_AGENTS': 'w1',
        }

    def test_sync_missing_file(self):
        with mock.patch.object(serve, '_load_env', return_value=self._env()), \
             mock.patch.object(serve, 'SAFARI_COOKIES_PATH',
                               os.path.join(self.tmp, 'missing.binarycookies')):
            self.assertEqual(serve._sync_safari_cookies('w1', 'facebook.com'), 0)

    def test_sync_no_matching_rows(self):
        cookie_file = os.path.join(self.tmp, 'cookies.bin')
        with open(cookie_file, 'wb') as fh:
            fh.write(b'cook')
        rows = [{'domain': 'example.com', 'name': 'a', 'value': 'b',
                 'path': '/', 'expires': -1, 'httpOnly': False,
                 'secure': True, 'sameSite': 'Lax'}]
        with mock.patch.object(serve, '_load_env', return_value=self._env()), \
             mock.patch.object(serve, 'SAFARI_COOKIES_PATH', cookie_file), \
             mock.patch.object(serve, '_parse_safari_binarycookies',
                               return_value=rows):
            self.assertEqual(serve._sync_safari_cookies('w1', 'facebook.com'), 0)

    def test_sync_chmod_failures_reported_as_zero(self):
        cookie_file = os.path.join(self.tmp, 'cookies.bin')
        with open(cookie_file, 'wb') as fh:
            fh.write(b'cook')
        rows = [{'domain': 'facebook.com', 'name': 'a', 'value': 'b',
                 'path': '/', 'expires': -1, 'httpOnly': False,
                 'secure': True, 'sameSite': 'Lax'}]
        with mock.patch.object(serve, '_load_env', return_value=self._env()), \
             mock.patch.object(serve, 'SAFARI_COOKIES_PATH', cookie_file), \
             mock.patch.object(serve, '_parse_safari_binarycookies',
                               return_value=rows), \
             mock.patch.object(serve.os, 'chmod', side_effect=OSError('chmod')), \
             mock.patch.object(serve.os, 'remove', side_effect=OSError('remove')):
            self.assertEqual(serve._sync_safari_cookies('w1', 'facebook.com'), 0)


class PaceAndRobotsTest(_TempMixin, unittest.TestCase):
    def test_invalid_pace_env_falls_back(self):
        with mock.patch.object(serve, '_load_env',
                               return_value={'BROWSE_PACE_RATE_PER_MIN': 'bad'}):
            self.assertEqual(serve._browse_pace_rate(), 12.0)
        with mock.patch.object(serve, '_load_env',
                               return_value={'BROWSE_PACE_BURST': 'bad'}):
            self.assertEqual(serve._browse_pace_burst(), 3.0)

    def test_record_block_write_failure(self):
        with mock.patch.object(serve.os, 'makedirs',
                               side_effect=OSError('disk')):
            serve._record_browser_block('a1', 'x.test', 'goto',
                                        'https://x.test', {}, 403)

    def test_robots_match_empty_and_regex_error(self):
        self.assertFalse(serve._robots_match('', '/x'))
        with mock.patch.object(serve.re, 'match', side_effect=serve.re.error('bad')):
            self.assertTrue(serve._robots_match('abc', 'abcdef'))

    def test_robots_no_host(self):
        with mock.patch.dict(serve._ROBOTS_CACHE, {}, clear=True):
            self.assertEqual(serve._robots_allows('not a url'), (True, 'no host'))

    def test_robots_http_and_unreachable(self):
        err = urllib.error.HTTPError('https://x.test/robots.txt', 403,
                                     'forbidden', {}, None)
        with mock.patch.dict(serve._ROBOTS_CACHE, {}, clear=True), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            allowed, reason = serve._robots_allows('https://x.test/a')
        self.assertFalse(allowed)
        self.assertIn('403', reason)
        with mock.patch.dict(serve._ROBOTS_CACHE, {}, clear=True), \
             mock.patch.object(serve, '_safe_urlopen',
                               side_effect=RuntimeError('down')):
            allowed, reason = serve._robots_allows('https://y.test/a')
        self.assertFalse(allowed)
        self.assertIn('unreachable', reason)


class PacingActTest(unittest.TestCase):
    def test_waiting_for_operator(self):
        with mock.patch.object(serve.browser_session, 'is_waiting_for_operator',
                               return_value=True):
            parsed, err = serve._browser_act_with_pacing(
                '/tmp', {'op': 'goto', 'url': 'https://x.test'}, 'a1')
        self.assertIsNone(err)
        self.assertTrue(parsed['waitingForOperator'])

    def test_session_error_is_swallowed(self):
        with mock.patch.object(serve.browser_session, 'is_waiting_for_operator',
                               side_effect=serve.browser_session.BrowserSessionError('x')), \
             mock.patch.object(serve, '_browse_pace_wait'), \
             mock.patch.object(serve, '_run_browser_act_sync',
                               return_value=({'ok': True}, None)):
            parsed, err = serve._browser_act_with_pacing(
                '/tmp', {'op': 'goto', 'url': 'https://x.test'}, 'a1')
        self.assertEqual(parsed, {'ok': True})
        self.assertIsNone(err)

    def test_bad_url_uses_empty_host(self):
        with mock.patch.object(serve, '_browse_pace_wait') as pace, \
             mock.patch.object(serve, '_run_browser_act_sync',
                               return_value=({'ok': True}, None)):
            parsed, err = serve._browser_act_with_pacing(
                '/tmp', {'op': 'goto', 'url': 'http://[::1'}, 'a1')
        self.assertEqual(parsed, {'ok': True})
        pace.assert_called_once_with('')

    def test_retry_after_parse_failure(self):
        replies = [({'status': 503, 'retryAfter': 'nope'}, None),
                   ({'status': 200}, None)]
        with mock.patch.object(serve, '_browse_pace_wait'), \
             mock.patch.object(serve, '_run_browser_act_sync',
                               side_effect=replies), \
             mock.patch.object(serve.time, 'sleep') as sleep:
            parsed, err = serve._browser_act_with_pacing(
                '/tmp', {'op': 'goto', 'url': 'https://x.test'}, 'a1')
        self.assertEqual(parsed['status'], 200)
        self.assertIsNone(err)
        sleep.assert_called_once()


class BrowserBlockPayloadTest(_TempMixin, unittest.TestCase):
    def test_invalid_result_url_uses_empty_host(self):
        parsed_result = {'url': 'http://[::1', 'status': 403,
                         'challenge': {'vendor': 'cf'}}
        with mock.patch.object(serve, '_record_browser_block'):
            out = serve._browser_block_payload(
                parsed_result, 'a1', 'goto', 'https://x.test', 'purpose')
        self.assertIn('cf', out['error'])
        self.assertEqual(out['blocked']['host'], '')


class CompactOutputTest(unittest.TestCase):
    def test_signal_compaction_and_truncation(self):
        lines = [f'error {i} ' + 'x' * 70 for i in range(120)]
        lines += [f'plain {i} ' + 'y' * 30 for i in range(120)]
        out = serve._compact_command_output('\n'.join(lines), limit=2000)
        self.assertIn('omitted', out)
        lines = ['error ' + 'x' * 40]
        lines += [f'plain {i} ' + 'y' * 20 for i in range(200)]
        out = serve._compact_command_output('\n'.join(lines), limit=2000)
        self.assertIn('omitted', out)


class InTestProcessTest(unittest.TestCase):
    def test_non_test_argv(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(sys, 'argv', ['/tmp/server.py']):
            self.assertFalse(serve._in_test_process())


class CronSandboxTest(unittest.TestCase):
    def test_requires_local_backend(self):
        with mock.patch.object(serve, 'SANDBOX_EXECUTION', 'digitalocean'):
            out = serve.run_cron_script_sandboxed('scripts/x.py', 5, {})
        self.assertFalse(out['timedOut'])
        self.assertIn('require SANDBOX_EXECUTION=local', out['stderr'])

    def test_local_success_and_env(self):
        result = types.SimpleNamespace(returncode=7, stdout='out', stderr='err')
        with mock.patch.object(serve, 'SANDBOX_EXECUTION', 'local'), \
             mock.patch.object(serve, '_run_docker_sandbox',
                               return_value=result) as run:
            out = serve.run_cron_script_sandboxed(
                'scripts/x.py', 5, {'A': '1', 'B': None, '': 'x'})
        self.assertEqual(out, {'exitCode': 7, 'stdout': 'out',
                               'stderr': 'err', 'timedOut': False})
        self.assertIn('-e', run.call_args[0][0])

    def test_local_timeout(self):
        expired = subprocess.TimeoutExpired('docker', 5, output='out',
                                            stderr='err')
        with mock.patch.object(serve, 'SANDBOX_EXECUTION', 'local'), \
             mock.patch.object(serve, '_run_docker_sandbox',
                               side_effect=expired):
            out = serve.run_cron_script_sandboxed('scripts/x.py', 5, {})
        self.assertTrue(out['timedOut'])


class _Proc:
    def __init__(self, stdout='{"ok": true}', returncode=0, stderr=''):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _Resp:
    def __init__(self, body=b'hello', url='https://x.test/final',
                 headers=None):
        self.body = body
        self.url = url
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self.url

    def read(self, *args):
        return self.body


def _http_error(code, body='err'):
    err = urllib.error.HTTPError('https://x.test', code, 'err', {}, None)
    err.read = lambda: body.encode()
    return err


class BrowserActSyncTest(_TempMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sandbox = os.path.join(self.tmp, 'sandbox')
        os.makedirs(self.sandbox, exist_ok=True)

    def test_session_success(self):
        with mock.patch.object(serve.browser_session, 'get',
                               return_value={'status': 'active'}), \
             mock.patch.object(serve.browser_session, 'session_sandbox_dir',
                               return_value=self.sandbox), \
             mock.patch.object(serve.browser_session, 'exec_driver',
                               return_value=_Proc()), \
             mock.patch.object(serve, '_sync_safari_cookies'), \
             mock.patch.object(serve, '_load_env', return_value={}), \
             mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             mock.patch.object(serve, '_grant_egress_host') as grant, \
             mock.patch.object(serve, '_revoke_egress_host') as revoke:
            parsed, err = serve._run_browser_act_sync(
                self.sandbox, {'op': 'goto', 'url': 'https://x.test'}, 'a1')
        self.assertIsNone(err)
        self.assertTrue(parsed['ok'])
        grant.assert_called_once_with('x.test')
        revoke.assert_called_once_with('x.test')

    def test_session_errors(self):
        with mock.patch.object(serve.browser_session, 'get',
                               return_value={'status': 'active'}), \
             mock.patch.object(serve.browser_session, 'session_sandbox_dir',
                               return_value=self.sandbox), \
             mock.patch.object(serve.browser_session, 'exec_driver',
                               side_effect=serve.browser_session.BrowserSessionError('boom')), \
             mock.patch.object(serve, '_sync_safari_cookies'), \
             mock.patch.object(serve, '_load_env', return_value={}), \
             mock.patch.object(serve, '_is_allowlisted_host', return_value=True):
            parsed, err = serve._run_browser_act_sync(
                self.sandbox, {'op': 'goto', 'url': 'https://x.test'}, 'a1')
        self.assertIsNone(parsed)
        self.assertEqual(err, 'boom')

        with mock.patch.object(serve.browser_session, 'get',
                               return_value={'status': 'active'}), \
             mock.patch.object(serve.browser_session, 'session_sandbox_dir',
                               return_value=self.sandbox), \
             mock.patch.object(serve.browser_session, 'exec_driver',
                               side_effect=subprocess.TimeoutExpired('docker', 1)), \
             mock.patch.object(serve, '_sync_safari_cookies'), \
             mock.patch.object(serve, '_load_env', return_value={}), \
             mock.patch.object(serve, '_is_allowlisted_host', return_value=True):
            parsed, err = serve._run_browser_act_sync(
                self.sandbox, {'op': 'goto', 'url': 'https://x.test'}, 'a1')
        self.assertIn('timed out', err)

    def test_session_invalid_url_has_no_grant(self):
        with mock.patch.object(serve.browser_session, 'get',
                               return_value={'status': 'active'}), \
             mock.patch.object(serve.browser_session, 'session_sandbox_dir',
                               return_value=self.sandbox), \
             mock.patch.object(serve.browser_session, 'exec_driver',
                               return_value=_Proc()), \
             mock.patch.object(serve, '_sync_safari_cookies'), \
             mock.patch.object(serve, '_load_env', return_value={}), \
             mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             mock.patch.object(serve, '_grant_egress_host') as grant:
            parsed, err = serve._run_browser_act_sync(
                self.sandbox, {'op': 'goto', 'url': 'http://[::1'}, 'a1')
        self.assertIsNone(err)
        grant.assert_not_called()

    def _one_shot(self, get):
        with mock.patch.object(serve.browser_session, 'get', **get), \
             mock.patch.object(serve, '_sync_safari_cookies'), \
             mock.patch.object(serve, '_load_env', return_value={}), \
             mock.patch.object(serve, '_is_allowlisted_host', return_value=True), \
             mock.patch.object(serve, '_browser_profile_dir',
                               return_value=os.path.join(self.tmp, 'profile')), \
             mock.patch.object(serve, '_run_docker_sandbox',
                               return_value=_Proc()), \
             mock.patch.object(serve, '_read_browser_act_result',
                               return_value=({'ok': True}, None)):
            return serve._run_browser_act_sync(
                self.sandbox, {'op': 'goto', 'url': 'https://x.test'}, 'a1')

    def test_session_lookup_raises_falls_back(self):
        parsed, err = self._one_shot(
            {'side_effect': serve.browser_session.BrowserSessionError('x')})
        self.assertTrue(parsed['ok'])

    def test_stopped_session_falls_back(self):
        parsed, err = self._one_shot({'return_value': {'status': 'stopped'}})
        self.assertTrue(parsed['ok'])

    def test_chmod_failure_is_ignored(self):
        with mock.patch.object(serve.os, 'chmod', side_effect=OSError('chmod')):
            parsed, err = self._one_shot(
                {'return_value': {'status': 'stopped'}})
        self.assertTrue(parsed['ok'])


class FetchPageSyncTest(_TempMixin, unittest.TestCase):
    def test_fresh_cache_hit(self):
        cached = {'expires': time.time() + 60, 'final_url': 'https://x.test',
                  'content_type': 'text/html', 'body': 'cached',
                  'truncated': False, 'last_modified': 1}
        with mock.patch.object(serve, '_read_http_cache', return_value=cached):
            self.assertEqual(serve._fetch_page_sync('https://x.test')[2], 'cached')

    def test_conditional_headers(self):
        captured = {}

        def make_request(*args, **kwargs):
            captured.update(kwargs.get('headers') or {})
            return mock.MagicMock()

        cached = {'expires': 0, 'etag': 'tag', 'final_url': 'https://x.test',
                  'content_type': 'text/html', 'body': 'b', 'truncated': False,
                  'last_modified': 1}
        with mock.patch.object(serve, '_read_http_cache', return_value=cached), \
             mock.patch.object(serve.urllib.request, 'Request',
                               side_effect=make_request), \
             mock.patch.object(serve, '_safe_urlopen', return_value=_Resp()), \
             mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             mock.patch.object(serve, '_cache_ttl_from_headers',
                               return_value=None):
            serve._fetch_page_sync('https://x.test')
        self.assertEqual(captured.get('If-None-Match'), 'tag')

        cached = dict(cached, etag='', last_modified_header='lm')
        with mock.patch.object(serve, '_read_http_cache', return_value=cached), \
             mock.patch.object(serve.urllib.request, 'Request',
                               side_effect=make_request), \
             mock.patch.object(serve, '_safe_urlopen', return_value=_Resp()), \
             mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             mock.patch.object(serve, '_cache_ttl_from_headers',
                               return_value=None):
            serve._fetch_page_sync('https://x.test')
        self.assertEqual(captured.get('If-Modified-Since'), 'lm')

    def test_not_modified_reuses_cache(self):
        meta_path = os.path.join(self.tmp, 'meta.json')
        with open(meta_path, 'w') as fh:
            fh.write('{}')
        cached = {'expires': 0, 'final_url': 'https://x.test',
                  'content_type': 'text/html', 'body': 'cached',
                  'truncated': False, 'last_modified': 1}
        err = urllib.error.HTTPError('https://x.test', 304, 'nm', {}, None)
        with mock.patch.object(serve, '_read_http_cache', return_value=cached), \
             mock.patch.object(serve, '_http_cache_paths',
                               return_value=(meta_path, meta_path)), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            body = serve._fetch_page_sync('https://x.test')[2]
        self.assertEqual(body, 'cached')

    def test_not_modified_meta_read_oserror_is_swallowed(self):
        cached = {'expires': 0, 'final_url': 'https://x.test',
                  'content_type': 'text/html', 'body': 'cached',
                  'truncated': False, 'last_modified': 1}
        err = urllib.error.HTTPError('https://x.test', 304, 'nm', {}, None)
        missing = os.path.join(self.tmp, 'absent-meta.json')
        with mock.patch.object(serve, '_read_http_cache', return_value=cached), \
             mock.patch.object(serve, '_http_cache_paths',
                               return_value=(missing, missing)), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            body = serve._fetch_page_sync('https://x.test')[2]
        self.assertEqual(body, 'cached')

    def test_other_http_error_propagates(self):
        cached = {'expires': 0, 'etag': 'tag'}
        err = urllib.error.HTTPError('https://x.test', 500, 'boom', {}, None)
        with mock.patch.object(serve, '_read_http_cache', return_value=cached), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                serve._fetch_page_sync('https://x.test')


class OpenRouterPostTest(unittest.TestCase):
    def _patch(self, **kwargs):
        patches = [
            mock.patch.object(serve.constitution, 'with_rules',
                              side_effect=lambda body: body),
            mock.patch.object(serve.training_capture, 'record'),
            mock.patch.object(serve, '_record_model_latency'),
            mock.patch.object(serve, '_queue_tier_recheck_if_availability'),
            mock.patch.object(serve, '_set_credits_pause'),
        ]
        for name, value in kwargs.items():
            patches.append(mock.patch.object(serve, name, **value))
        return patches

    def _run(self, side_effect, **kwargs):
        with mock.patch.object(serve, '_urlopen_with_resilience',
                               side_effect=side_effect), \
             mock.patch.object(serve.constitution, 'with_rules',
                               side_effect=lambda body: body), \
             mock.patch.object(serve.training_capture, 'record'), \
             mock.patch.object(serve, '_record_model_latency'), \
             mock.patch.object(serve, '_queue_tier_recheck_if_availability'), \
             mock.patch.object(serve, '_set_credits_pause') as pause:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                serve._openrouter_chat_post({'model': 'm', 'max_tokens': 100})
        return ctx.exception, pause

    def test_payment_required_marks_pause(self):
        err, pause = self._run([_http_error(402, 'no credits')])
        self.assertEqual(err.code, 402)
        pause.assert_called_once_with(True, source='openrouter_402')

    def test_availability_marker_sets_flag(self):
        err, _ = self._run([_http_error(500, 'model not found')])
        self.assertTrue(getattr(err, 'availability_failure', False))

    def test_provider_invalid_tool_json_sets_flag(self):
        err, _ = self._run([_http_error(
            400, 'upstream error: assistant tool call function.arguments must be valid json')])
        self.assertTrue(getattr(err, 'availability_failure', False))

    def test_retry_payment_required_marks_pause(self):
        err, pause = self._run([
            _http_error(400, 'reasoning is mandatory for this endpoint'),
            _http_error(402, 'no credits')])
        self.assertEqual(err.code, 402)
        self.assertEqual(pause.call_count, 1)

    def test_retry_availability_marker_sets_flag(self):
        err, _ = self._run([
            _http_error(400, 'reasoning is mandatory for this endpoint'),
            _http_error(500, 'model not found')])
        self.assertTrue(getattr(err, 'availability_failure', False))


class PostRawBoundaryTest(unittest.TestCase):
    def _guards(self, **overrides):
        values = {
            'credits_pause_active': False,
            '_think_tank_spend_cap_exceeded': False,
            '_api_key_is_broken': False,
            'is_model_circuit_broken': False,
            '_messages_pass_boundary_check': True,
        }
        values.update(overrides)
        patches = []
        for name, value in values.items():
            patches.append(mock.patch.object(serve, name, return_value=value))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_boundary_failure_refuses_call(self):
        self._guards(_messages_pass_boundary_check=False)
        with self.assertRaises(RuntimeError):
            serve._post_openrouter_raw('m', [{'role': 'user', 'content': 'x'}])

    def test_availability_fallback_returns(self):
        self._guards()
        err = _http_error(500, 'model not found')
        err.availability_failure = True
        with mock.patch.object(serve, '_openrouter_chat_post', side_effect=err), \
             mock.patch.object(serve, 'record_model_result'), \
             mock.patch.object(serve, '_note_api_key_failure'), \
             mock.patch.object(serve, '_try_tier_fallbacks',
                               return_value={'ok': 'fallback'}):
            result = serve._post_openrouter_raw(
                'model-a', [{'role': 'user', 'content': 'x'}])
        self.assertEqual(result, {'ok': 'fallback'})
