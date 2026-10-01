"""Tests for the YouTube transcript tool: URL gating (_is_youtube_url),
the VTT/SRT cleaning (_clean_subtitle_file), the local yt-dlp captions
utility (_youtube_transcript), and the serving route
(_youtube_transcript_colab) -- which ALWAYS downloads the audio via the
Apify actor and transcribes it with faster-whisper on the Colab runtime
(every video treated the same; no captions fast-path).

The two real-network boundaries (yt-dlp subprocess, Colab runtime) are
mocked so nothing touches the network. The fallback's budget/availability
gates are exercised deterministically."""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class YouTubeUrlGate(unittest.TestCase):
    def test_accepts_real_youtube_hosts(self):
        for url in (
            'https://youtu.be/NSuMfeTVHqY',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'https://m.youtube.com/watch?v=abc',
            'http://youtu.be/abc?is=mbnmlyheSKQBOUCo',
        ):
            self.assertTrue(serve._is_youtube_url(url), url)

    def test_rejects_non_youtube_hosts(self):
        for url in (
            'https://evil.com/watch?v=dQw4w9WgXcQ',
            'https://youtube.com.evil.net/v',
            'https://notyoutube.com/v',
            'garbage',
            '',
            'ftp://youtu.be/x',
        ):
            self.assertFalse(serve._is_youtube_url(url), url)


class CleanSubtitleFile(unittest.TestCase):
    def _write(self, content):
        d = tempfile.mkdtemp(prefix='yt-clean-test-')
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'sub.vtt')
        with open(path, 'w') as f:
            f.write(content)
        return path

    def test_strips_timing_and_dedupes_growing_captions(self):
        # YouTube aligned captions repeat each growing phrase across cue rows.
        vtt = (
            'WEBVTT\nKind: captions\nLanguage: en\n\n'
            '00:00:00.000 --> 00:00:03.230 align:start position:0%\n'
            'This<00:00:00.520><c> is</c><00:00:00.720><c> a</c><00:00:00.800><c> test</c>\n\n'
            '00:00:03.230 --> 00:00:03.240 align:start position:0%\n'
            'This is a test\n\n'
            '00:00:03.240 --> 00:00:05.350 align:start position:0%\n'
            'This is a test\n'
            'with<00:00:03.600><c> words</c>\n\n'
            '00:00:05.350 --> 00:00:05.360 align:start position:0%\n'
            'with words\n\n'
            '00:00:05.360 --> 00:00:08.470 align:start position:0%\n'
            'with words\n'
            'after<00:00:05.440><c> them</c>\n\n'
            '00:00:08.470 --> 00:00:08.480 align:start position:0%\n'
            'after them\n'
        )
        text = serve._clean_subtitle_file(self._write(vtt))
        self.assertIn('This is a test', text)
        self.assertIn('with words', text)
        self.assertIn('after them', text)
        # No timing rows or tags survive.
        self.assertNotIn('-->', text)
        self.assertNotIn('<00:', text)

    def test_handles_srt_format(self):
        srt = '1\n00:00:00,000 --> 00:00:01,000\nfirst line\n\n2\n00:00:01,000 --> 00:00:02,000\nsecond line\n'
        text = serve._clean_subtitle_file(self._write(srt))
        self.assertEqual(text, 'first line\nsecond line')

    def test_returns_none_on_missing_file(self):
        self.assertIsNone(serve._clean_subtitle_file('/nonexistent/path.vtt'))


class LocalCaptionsPath(unittest.TestCase):
    def test_url_gated_before_subprocess(self):
        with unittest.mock.patch.object(serve, 'subprocess') as sub:
            text, err = serve._youtube_transcript('https://evil.com/x')
            self.assertIsNone(text)
            self.assertIn('Not a YouTube URL', err)
            sub.run.assert_not_called()

    def test_success_returns_cleaned_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Fake the yt-dlp subprocess writing a vtt into the temp dir.
            def fake_run(cmd, **kwargs):
                # cmd[-2] is the -o template; the sub goes next to it.
                tmpl = cmd[-2]
                sub_path = tmpl + '.en.vtt'
                with open(sub_path, 'w') as f:
                    f.write('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello world\n')
                result = unittest.mock.Mock(returncode=0, stderr='', stdout='')
                return result
            with unittest.mock.patch.object(serve, 'subprocess') as sub:
                sub.run.side_effect = fake_run
                with unittest.mock.patch.object(serve, 'tempfile') as tf:
                    tf.TemporaryDirectory.return_value.__enter__.return_value = tmp
                    text, err = serve._youtube_transcript('https://youtu.be/abc')
        self.assertIsNone(err)
        self.assertIn('hello world', text)

    def test_nonzero_exit_surfaces_yt_dlp_reason(self):
        result = unittest.mock.Mock(returncode=1, stderr='ERROR: no subtitles found',
                                    stdout='')
        with unittest.mock.patch.object(serve, 'subprocess') as sub:
            sub.run.return_value = result
            text, err = serve._youtube_transcript('https://youtu.be/abc')
        self.assertIsNone(text)
        self.assertIn('no subtitles found', err)


class ColabFallback(unittest.TestCase):
    """The serving route: ALWAYS downloads the audio via the Apify actor on
    the Colab runtime and transcribes it with faster-whisper there."""

    def test_returns_error_when_colab_disabled(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', False):
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(text)
        self.assertIn('Colab is disabled', err)

    def test_budget_exceeded_refused(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=True), \
             unittest.mock.patch.object(serve, '_colab_compute_run') as run:
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(text)
        self.assertIn('Colab usage cap reached', err)
        run.assert_not_called()

    def test_missing_apify_key_fails_fast(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, 'APIFY_API_KEY', ''), \
             unittest.mock.patch.object(serve, '_colab_compute_run') as run:
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(text)
        self.assertIn('APIFY_API_KEY is not configured', err)
        run.assert_not_called()

    def test_success_returns_transcribed_text(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'test-key'), \
             unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'stdout': 'first line\nsecond line\n__TRANSCRIPT_END__\n',
                                                      'units': 1, 'elapsed_s': 30,
                                                      'session': 'colab'}) as run:
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(err)
        self.assertEqual(text, 'first line\nsecond line')
        # The code sent to Colab must include the Apify actor URL + whisper
        # imports, must NOT reference yt-dlp or a cookies file, and the key
        # must travel via the env dict, not the code text.
        code = run.call_args.args[1]
        self.assertIn('faster_whisper', code)
        self.assertIn('IHDsaLO64Wge9wSWx', code)
        self.assertIn('https://youtu.be/abc', code)
        self.assertIn('__TRANSCRIPT_END__', code)
        self.assertNotIn('yt_dlp', code)
        self.assertNotIn('cookiefile', code)
        self.assertNotIn('test-key', code)
        self.assertEqual(run.call_args.kwargs.get('env'), {'APIFY_API_KEY': 'test-key'})
        self.assertEqual(run.call_args.kwargs.get('packages'), ['faster-whisper', 'av==13.1.0'])

    def test_no_end_marker_is_an_honest_failure(self):
        # A run that dies partway (traceback instead of a marked transcript)
        # must NEVER be returned as text -- fail closed.
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'test-key'), \
             unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'stdout': 'Traceback (most recent call last):\n'
                                                               'ExtractorError: Sign in to confirm',
                                                      'units': 1, 'elapsed_s': 30, 'session': 'colab'}):
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(text)
        self.assertIn('no end marker', err)

    def test_explicit_error_marker_is_an_honest_failure(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'test-key'), \
             unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'stdout': '__ERROR__: actor start failed: boom',
                                                      'units': 1, 'elapsed_s': 30, 'session': 'colab'}):
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(text)
        self.assertIn('could not download or transcribe', err)
        self.assertIn('boom', err)

    def test_error_from_colab_surface(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'test-key'), \
             unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'error': 'runtime not granted'}):
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(text)
        self.assertIn('runtime not granted', err)


class YouTubeVideoId(unittest.TestCase):
    def test_extracts_id_from_watch_url(self):
        self.assertEqual(serve._youtube_video_id('https://www.youtube.com/watch?v=dQw4w9WgXcQ'),
                         'dQw4w9WgXcQ')

    def test_extracts_id_from_youtu_be(self):
        self.assertEqual(serve._youtube_video_id('https://youtu.be/dQw4w9WgXcQ'),
                         'dQw4w9WgXcQ')

    def test_extracts_id_from_shorts(self):
        self.assertEqual(serve._youtube_video_id('https://www.youtube.com/shorts/abcdefghijk'),
                         'abcdefghijk')

    def test_rejects_non_youtube(self):
        self.assertIsNone(serve._youtube_video_id('https://evil.com/watch?v=dQw4w9WgXcQ'))


class FileYoutubeTranscript(unittest.TestCase):
    """Every successful extraction is filed into the shared Library's media/
    transcripts/ tree (YOUTUBE_TRANSCRIPTS_DIR) so ANY agent can read it via
    /api/library/file. A write failure must never fail the request."""

    def test_writes_transcript_into_shared_transcripts_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = os.path.join(tmp, 'library')
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', lib), \
                 unittest.mock.patch.object(serve, 'YOUTUBE_TRANSCRIPTS_DIR',
                                            os.path.join(lib, 'media', 'transcripts')):
                rel = serve._file_youtube_transcript('https://youtu.be/dQw4w9WgXcQ',
                                                     'hello world', 'captions')
            self.assertEqual(rel, os.path.join('media', 'transcripts', 'dQw4w9WgXcQ.txt'))
            target = os.path.join(lib, 'media', 'transcripts', 'dQw4w9WgXcQ.txt')
            self.assertTrue(os.path.isfile(target))
            content = open(target).read()
            self.assertIn('hello world', content)
            self.assertIn('dQw4w9WgXcQ', content)
            self.assertIn('captions', content)

    def test_updates_same_file_in_place_on_refetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = os.path.join(tmp, 'library')
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', lib), \
                 unittest.mock.patch.object(serve, 'YOUTUBE_TRANSCRIPTS_DIR',
                                            os.path.join(lib, 'media', 'transcripts')):
                serve._file_youtube_transcript('https://youtu.be/dQw4w9WgXcQ', 'first', 'captions')
                serve._file_youtube_transcript('https://youtu.be/dQw4w9WgXcQ', 'second', 'colab-whisper')
            target = os.path.join(lib, 'media', 'transcripts', 'dQw4w9WgXcQ.txt')
            self.assertEqual(open(target).read().count('first'), 0)
            self.assertIn('second', open(target).read())

    def test_invalid_url_is_a_silent_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = os.path.join(tmp, 'library')
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', lib), \
                 unittest.mock.patch.object(serve, 'YOUTUBE_TRANSCRIPTS_DIR',
                                            os.path.join(lib, 'media', 'transcripts')):
                rel = serve._file_youtube_transcript('https://evil.com/x', 'text', 'captions')
            self.assertIsNone(rel)
            self.assertEqual(os.listdir(lib) if os.path.exists(lib) else [], [])


class ColabSessionExists(unittest.TestCase):
    """The colab CLI exits 0 even for a missing session (it prints 'Session 'X'
    not found.' to stderr) -- the existence check must read the output, not
    just the exit code, or provision skips `colab new` and exec fails against a
    phantom session."""

    def test_missing_session_prints_not_found_is_not_existing(self):
        with unittest.mock.patch.object(serve, '_colab_cli',
                                        return_value=(0, "[colab] Session 'think-tank-gpu' not found.")):
            self.assertFalse(serve._colab_session_exists('think-tank-gpu'))

    def test_no_active_sessions_is_not_existing(self):
        with unittest.mock.patch.object(serve, '_colab_cli',
                                        return_value=(0, '[colab] No active sessions found on server.')):
            self.assertFalse(serve._colab_session_exists('think-tank-gpu'))

    def test_real_session_is_existing(self):
        with unittest.mock.patch.object(serve, '_colab_cli',
                                        return_value=(0, '[think-tank-gpu] gpu-t4-s-abc | Status: IDLE')):
            self.assertTrue(serve._colab_session_exists('think-tank-gpu'))

    def test_nonzero_exit_is_not_existing(self):
        with unittest.mock.patch.object(serve, '_colab_cli',
                                        return_value=(1, 'some error')):
            self.assertFalse(serve._colab_session_exists('think-tank-gpu'))


class ColabDenyExemption(unittest.TestCase):
    """The transcription route no longer needs any deny-class exemption: it
    downloads via the Apify actor on the runtime (no yt-dlp), so agent-facing
    Colab runs that use yt-dlp stay refused, torrents stay refused
    unconditionally, and every other deny class still fires."""

    def test_transcription_needs_no_skip_label_and_uses_apify(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'test-key'), \
             unittest.mock.patch.object(serve, '_colab_gate_urls', return_value=None), \
             unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'stdout': 'hi there\n__TRANSCRIPT_END__\n', 'units': 1,
                                                      'elapsed_s': 30, 'session': 'colab'}) as run:
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(err)
        kwargs = run.call_args.kwargs
        self.assertNotIn('skip_deny_labels', kwargs)
        self.assertEqual(kwargs.get('packages'), ['faster-whisper', 'av==13.1.0'])
        self.assertEqual(kwargs.get('env'), {'APIFY_API_KEY': 'test-key'})
        self.assertNotIn('yt_dlp', run.call_args.args[1])

    def test_agent_facing_run_with_yt_dlp_is_still_refused(self):
        code = "import yt_dlp\nydl_opts = {}\n"
        result = serve._colab_compute_run('ada', code, 'download some videos',
                                          ['yt-dlp'], 60, runtimes=1, kind='cpu')
        self.assertIn('refusing to run', result['error'])
        self.assertIn('bulk media download', result['error'])

    def test_torrents_still_refused_even_with_skip_label(self):
        code = "import deezloader\nDeezerLoader()\n"
        with unittest.mock.patch.object(serve, '_colab_gate_urls', return_value=None):
            result = serve._colab_compute_run('ada', code, 'transcribe a YouTube video that has no captions',
                                              [], 60, runtimes=1, kind='cpu',
                                              skip_deny_labels=('bulk media download',))
        self.assertIn('refusing to run', result['error'])
        self.assertIn('bulk media / torrents', result['error'])

    def test_other_deny_classes_still_fire_for_transcription_path(self):
        # Mining / drive / exfil must not be unlocked by the yt-dlp skip.
        for code in ('import xmrig\n', 'from google.colab import drive\n', 'requests.post("https://transfer.sh/x")\n'):
            with unittest.mock.patch.object(serve, '_colab_gate_urls', return_value=None):
                result = serve._colab_compute_run('ada', code, 'transcribe a YouTube video that has no captions',
                                                  [], 60, runtimes=1, kind='cpu',
                                                  skip_deny_labels=('bulk media download',))
            self.assertIn('refusing to run', result['error'], code)


if __name__ == '__main__':
    unittest.main()