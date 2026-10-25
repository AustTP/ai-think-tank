"""Gap-cluster F coverage tests for serve.py.

Covers the chrome/screenshot helpers, sandbox backups, page probe,
escalations, activity feed, decision/review calibration, weekly review,
health/ops metrics, model-tier and jev endpoints, auth endpoints, _http_json,
_arm_idle_and_run, api_log, post_report, _stage_released_work, execute/pipeline,
the youtube-transcript endpoint, and the youtube transcript helpers.

Same isolation discipline as tests/test_serve.py: module-wide patcher redirects
every real DB / library / passport path into a throwaway temp dir, and a fresh
in-memory DB schema is created in setUpModule.  TestClient is used bare (no
context manager) and serve.get_state_from_db / save_state_to_db / log_action /
verify_session are patched around requests.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
import urllib.error

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve
import sim

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-serve-gap-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class _LoopExit(Exception):
    pass


class _FakePageProbeHarness:
    """Programmable fake for sync_playwright().chromium.launch()."""

    def __init__(self, base_globals=None, probe_error=False, custom_error=False, click_error=None):
        self.base_globals = base_globals or ['a', 'b']
        self.probe_error = probe_error
        self.custom_error = custom_error
        self.click_error = click_error

    def build(self):
        ctx = unittest.mock.MagicMock()
        ctx.__enter__.return_value = ctx
        browser = ctx.chromium.launch.return_value
        base = unittest.mock.MagicMock()
        page = unittest.mock.MagicMock()
        browser.new_page.side_effect = [base, page]
        base.evaluate.return_value = list(self.base_globals)
        page.on = unittest.mock.Mock()
        page.keyboard = unittest.mock.Mock()
        page.keyboard.press = unittest.mock.Mock()
        page.wait_for_timeout = unittest.mock.Mock()

        def _evaluate(expr, arg=None):
            if arg is not None:
                if self.custom_error:
                    raise RuntimeError('custom eval boom')
                return [{'name': 'custom1', 'type': 'boolean', 'value': True}]
            if 'Object.keys(window)' in expr:
                return list(self.base_globals) + ['custom1']
            if self.probe_error:
                raise RuntimeError('probe boom')
            return 'RESULT:' + expr

        page.evaluate.side_effect = _evaluate
        if self.click_error:
            page.click.side_effect = RuntimeError(self.click_error)
        return ctx


class PipelineEndpoint(unittest.TestCase):
    """POST /api/pipeline."""

    RESULT = {'exitCode': 0, 'stdout': 'ok', 'stderr': '', 'timedOut': False}
    FAIL = {'exitCode': 2, 'stdout': '', 'stderr': 'boom', 'timedOut': False}

    def setUp(self):
        self.sbx = tempfile.mkdtemp(prefix='gap-f-sandbox-')
        self.addCleanup(shutil.rmtree, self.sbx, ignore_errors=True)

    def _post(self, body, classify=(True, 'allow'), rate=True, exec_enabled=True,
              results=None):
        patchers = [
            unittest.mock.patch.object(serve, 'EXECUTION_ENABLED', exec_enabled),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=rate),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=None),
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, 'log_action'),
            unittest.mock.patch.object(serve, '_classify_command',
                                       new=unittest.mock.AsyncMock(return_value=classify)),
            unittest.mock.patch.object(serve, '_snapshot_sandbox',
                                       new=unittest.mock.AsyncMock()),
            unittest.mock.patch.object(serve, 'create_escalation', return_value='esc-1'),
            unittest.mock.patch.object(serve, 'sync_prototypes'),
            unittest.mock.patch.object(serve, '_sandbox_dir_for', return_value=self.sbx),
        ]
        seq = results if results is not None else [self.RESULT, self.RESULT]
        patchers.append(unittest.mock.patch.object(serve, '_run_in_sandbox_sync',
                                                   side_effect=seq))
        for p in patchers:
            p.start()
        try:
            c = TestClient(serve.app)
            return c.post('/api/pipeline', json=body)
        finally:
            for p in patchers:
                p.stop()

    def _steps(self, names=('clone', 'test')):
        return [{'name': n, 'command': f'run {n}'} for n in names]

    def test_disabled_is_403(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()}, exec_enabled=False)
        self.assertEqual(r.status_code, 403)

    def test_rate_limited_is_429(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()}, rate=False)
        self.assertEqual(r.status_code, 429)

    def test_missing_steps_is_400(self):
        r = self._post({'agentId': 'eli'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('steps is required', r.json()['error'])

    def test_all_steps_succeed(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body['failedStep'])
        self.assertEqual(len(body['results']), 2)
        self.assertTrue(all(x['allowed'] for x in body['results']))

    def test_blocked_step_stops_pipeline(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()},
                       classify=(False, 'blocked: network access'))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['failedStep'], 'clone')
        self.assertFalse(body['results'][0]['allowed'])

    def test_nonzero_exit_stops_pipeline(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()},
                       results=[self.RESULT, self.FAIL])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['failedStep'], 'test')
        self.assertEqual(body['results'][1]['exitCode'], 2)



class YoutubeTranscriptEndpoint(unittest.TestCase):
    """POST /api/youtube-transcript. Free local captions first; Colab whisper
    only when captions are unavailable."""

    URL = 'https://www.youtube.com/watch?v=abc123def45'

    def _post(self, body, captions_return=None, captions_error=None,
              colab_return=None, colab_error=None, rate=True, file_return=None,
              cached_return=None):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=rate), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=None), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_cached_youtube_transcript',
                                        return_value=cached_return) as cached, \
             unittest.mock.patch.object(serve, '_youtube_transcript',
                                        return_value=(captions_return, captions_error)) as caps, \
             unittest.mock.patch.object(serve, '_youtube_transcript_colab',
                                        return_value=(colab_return, colab_error)) as colab, \
             unittest.mock.patch.object(serve, '_file_youtube_transcript',
                                        return_value=file_return) as filed:
            c = TestClient(serve.app)
            r = c.post('/api/youtube-transcript', json=body)
        return r, log, caps, colab, filed

    def test_rate_limited_is_429(self):
        r, _log, _caps, _colab, _filed = self._post(
            {'agentId': 'eli', 'url': self.URL}, rate=False)
        self.assertEqual(r.status_code, 429)

    def test_cached_transcript_skips_both_fetches(self):
        r, log, caps, colab, filed = self._post(
            {'agentId': 'eli', 'url': self.URL},
            cached_return=('cached text', 'media/transcripts/abc123def45.txt'))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['transcript'], 'cached text')
        self.assertEqual(body['source'], 'cache')
        self.assertTrue(body['cached'])
        caps.assert_not_called()
        colab.assert_not_called()
        filed.assert_not_called()
        self.assertEqual(log.call_args[0][2]['decision'], 'cache')

    def test_invalid_url_is_400(self):
        r, _log, _caps, _colab, _filed = self._post(
            {'agentId': 'eli', 'url': 'https://evil.com/x'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('youtube.com', r.json()['error'])

    def test_captions_first_skips_colab_and_files(self):
        r, log, caps, colab, filed = self._post(
            {'agentId': 'eli', 'url': self.URL}, captions_return='caption text',
            file_return='media/transcripts/abc123def45.txt')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['transcript'], 'caption text')
        self.assertEqual(body['source'], 'yt-dlp-captions')
        self.assertEqual(body['filed'], 'media/transcripts/abc123def45.txt')
        caps.assert_called_once_with(self.URL, 'en')
        colab.assert_not_called()
        called_with = [c for c in filed.call_args_list]
        self.assertEqual(called_with[0].args[2], 'yt-dlp-captions')

    def test_falls_back_to_colab_when_no_captions(self):
        r, log, caps, colab, filed = self._post(
            {'agentId': 'eli', 'url': self.URL}, captions_error='no captions',
            colab_return='whisper text')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['source'], 'colab-whisper')
        colab.assert_called_once_with(self.URL, 'en')

    def test_both_routes_fail_is_422(self):
        r, log, _caps, _colab, _filed = self._post(
            {'agentId': 'eli', 'url': self.URL}, captions_error='no captions',
            colab_error='no captions')
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()['error'], 'no captions')
        self.assertEqual(log.call_args[0][2]['decision'], 'failed')

    def test_success_returns_transcript(self):
        r, log, _caps, colab, filed = self._post(
            {'agentId': 'eli', 'url': self.URL}, captions_error='none',
            colab_return='hello world',
            file_return='media/transcripts/abc123def45.txt')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['transcript'], 'hello world')
        self.assertEqual(body['filed'], 'media/transcripts/abc123def45.txt')
        colab.assert_called_once_with(self.URL, 'en')
        self.assertEqual(log.call_args[0][2]['decision'], 'ok')



class YoutubeHelpers(unittest.TestCase):
    """_is_youtube_url / _youtube_video_id / _file_youtube_transcript /
    _clean_subtitle_file."""

    URL = 'https://www.youtube.com/watch?v=abc123def45'

    def test_is_youtube_url_rejects_non_http(self):
        self.assertFalse(serve._is_youtube_url('youtube.com/watch?v=abc'))
        self.assertFalse(serve._is_youtube_url(None))

    def test_is_youtube_url_accepts_youtube(self):
        self.assertTrue(serve._is_youtube_url('https://youtu.be/abc123def45'))

    def test_is_youtube_url_urlparse_exception(self):
        with unittest.mock.patch.object(serve.urllib.parse, 'urlparse',
                                        side_effect=ValueError('bad url')):
            self.assertFalse(serve._is_youtube_url(self.URL))

    def test_video_id_urlparse_exception_returns_none(self):
        real = serve.urllib.parse.urlparse
        with unittest.mock.patch.object(serve.urllib.parse, 'urlparse',
                                        side_effect=[real(self.URL), ValueError('bad url')]):
            self.assertIsNone(serve._youtube_video_id(self.URL))

    def test_video_id_short_v_param_returns_none(self):
        self.assertIsNone(serve._youtube_video_id('https://www.youtube.com/watch?v=abc'))

    def test_file_transcript_writes_to_library(self):
        rel = serve._file_youtube_transcript(self.URL, 'hello', 'colab-whisper')
        self.assertEqual(rel, 'media/transcripts/abc123def45.txt')
        target = os.path.join(serve.LIBRARY_DIR, 'media', 'transcripts',
                              'abc123def45.txt')
        self.assertTrue(os.path.isfile(target))
        with open(target) as f:
            self.assertIn('hello', f.read())

    def test_cached_transcript_reads_and_strips_header(self):
        serve._file_youtube_transcript(self.URL, 'hello cached body', 'captions')
        body, rel = serve._cached_youtube_transcript(self.URL)
        self.assertEqual(body, 'hello cached body')
        self.assertEqual(rel, 'media/transcripts/abc123def45.txt')

    def test_file_transcript_unsafe_library_path_returns_none(self):
        with unittest.mock.patch.object(serve, '_safe_library_path',
                                        return_value=None):
            rel = serve._file_youtube_transcript(self.URL, 'hello', 'x')
        self.assertIsNone(rel)

    def test_file_transcript_write_failure_returns_none(self):
        with unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=OSError('no space')):
            rel = serve._file_youtube_transcript(self.URL, 'hello', 'x')
        self.assertIsNone(rel)

    def _write_subs(self, content):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-clean-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = os.path.join(self.tmp, 'subs.vtt')
        with open(p, 'w') as f:
            f.write(content)
        return p

    def test_clean_strips_timestamps_and_tags(self):
        p = self._write_subs(
            'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<i></i>\n'
            '00:00:02.000 --> 00:00:03.000\nHello world\n')
        out = serve._clean_subtitle_file(p)
        self.assertEqual(out, 'WEBVTT\nHello world')

    def test_clean_unreadable_file_returns_none(self):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-clean-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        out = serve._clean_subtitle_file(os.path.join(self.tmp, 'missing.vtt'))
        self.assertIsNone(out)



class YoutubeTranscriptLocal(unittest.TestCase):
    """_youtube_transcript (local yt-dlp path)."""

    URL = 'https://www.youtube.com/watch?v=abc123def45'

    def _run(self, subs=None, exc=None):
        def fake_run(cmd, **kwargs):
            tmpl = cmd[cmd.index('-o') + 1]
            if subs is not None:
                with open(f'{tmpl}.en.vtt', 'w') as f:
                    f.write(subs)
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
        side = exc if exc is not None else fake_run
        with unittest.mock.patch.object(serve.subprocess, 'run', side_effect=side):
            return serve._youtube_transcript(self.URL)

    def test_no_subtitle_files_is_error(self):
        text, error = self._run(subs=None)
        self.assertIsNone(text)
        self.assertIn('no subtitle files', error)

    def test_clean_produces_no_text_is_error(self):
        subs = ('00:00:01.000 --> 00:00:02.000\n<i></i>\n'
                '00:00:02.000 --> 00:00:03.000\n<i></i>\n')
        text, error = self._run(subs=subs)
        self.assertIsNone(text)
        self.assertIn('no readable text', error)

    def test_segments_truncated_to_max(self):
        subs = '\n'.join(f'Segment {i}' for i in range(1000))
        text, error = self._run(subs=subs)
        self.assertIsNone(error)
        self.assertEqual(text.count('\n'), 799)
        self.assertNotIn('Segment 800', text)

    def test_chars_truncated_to_max(self):
        subs = '\n'.join(f'{i:03d}' + 'x' * 117 for i in range(500))
        text, error = self._run(subs=subs)
        self.assertIsNone(error)
        self.assertEqual(len(text), 50000)

    def test_timeout_is_error(self):
        text, error = self._run(exc=subprocess.TimeoutExpired('yt-dlp', 90))
        self.assertIsNone(text)
        self.assertIn('timed out', error)

    def test_ytdlp_missing_is_error(self):
        text, error = self._run(exc=FileNotFoundError())
        self.assertIsNone(text)
        self.assertIn('yt-dlp is not installed', error)

    def test_generic_failure_is_error(self):
        text, error = self._run(exc=ValueError('boom'))
        self.assertIsNone(text)
        self.assertIn('fetch failed: boom', error)



class YoutubeTranscriptColab(unittest.TestCase):
    """_youtube_transcript_colab edge branches."""

    URL = 'https://www.youtube.com/watch?v=abc123def45'

    def _call(self, url=None, colab_return=None, colab_exc=None, enabled=True,
              cli=True, key='test-key', budget=False):
        patchers = [
            unittest.mock.patch.object(serve, 'COLAB_ENABLED', enabled),
            unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', cli),
            unittest.mock.patch.object(serve, 'APIFY_API_KEY', key),
            unittest.mock.patch.object(serve, '_colab_budget_exceeded',
                                       return_value=budget),
        ]
        if colab_return is not None:
            patchers.append(unittest.mock.patch.object(serve, '_colab_compute_run',
                                                       return_value=colab_return))
        if colab_exc is not None:
            patchers.append(unittest.mock.patch.object(serve, '_colab_compute_run',
                                                       side_effect=colab_exc))
        for p in patchers:
            p.start()
        try:
            return serve._youtube_transcript_colab(url or self.URL)
        finally:
            for p in patchers:
                p.stop()

    def test_not_youtube_url_is_error(self):
        text, error = serve._youtube_transcript_colab('https://evil.com/x')
        self.assertIsNone(text)
        self.assertIn('Not a YouTube URL', error)

    def test_colab_disabled_is_error(self):
        text, error = self._call(enabled=False)
        self.assertIsNone(text)
        self.assertIn('Colab is disabled', error)

    def test_cli_unavailable_is_error(self):
        text, error = self._call(cli=False)
        self.assertIsNone(text)
        self.assertIn('Colab CLI is not installed', error)

    def test_compute_run_crash_is_error(self):
        text, error = self._call(colab_exc=RuntimeError('boom'))
        self.assertIsNone(text)
        self.assertIn('run crashed: boom', error)

    def test_non_dict_response_is_error(self):
        text, error = self._call(colab_return='oops')
        self.assertIsNone(text)
        self.assertIn('Unexpected Colab transcription response', error)

    def test_no_text_is_error(self):
        text, error = self._call(colab_return={'stdout': '__TRANSCRIPT_END__'})
        self.assertIsNone(text)
        self.assertIn('returned no text', error)

    def test_chars_truncated_to_max(self):
        stdout = ('x' * 100 + '\n') * 600 + '__TRANSCRIPT_END__\n'
        text, error = self._call(colab_return={'stdout': stdout})
        self.assertIsNone(error)
        self.assertEqual(len(text), 50000)



class SandboxRestoreAdminGate(unittest.TestCase):
    """POST /api/sandbox-backups/restore widened to player session OR admin."""

    def test_admin_agent_may_restore_any_sandbox(self):
        state = {'agentRoster': [{'id': 'boss', 'isAdmin': True}], 'agents': {}}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='boss'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_restore_sandbox_backup') as restore:
            c = TestClient(serve.app)
            r = c.post('/api/sandbox-backups/restore',
                       json={'agentId': 'worker1', 'sandboxId': 'sbx', 'stamp': 'abc'})
        self.assertEqual(r.status_code, 200, r.text)
        restore.assert_called_once_with('sbx', 'abc')

    def test_ordinary_agent_may_not_restore(self):
        state = {'agentRoster': [{'id': 'worker1', 'isAdmin': False}], 'agents': {}}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='worker1'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state):
            c = TestClient(serve.app)
            r = c.post('/api/sandbox-backups/restore',
                       json={'agentId': 'player', 'sandboxId': 'sbx', 'stamp': 'abc'})
        self.assertEqual(r.status_code, 401)
