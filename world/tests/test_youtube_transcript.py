"""Tests for the YouTube transcript tool: URL gating (_is_youtube_url),
the VTT/SRT cleaning (_clean_subtitle_file), the local yt-dlp captions path
(_youtube_transcript), and the Colab whisper fallback
(_youtube_transcript_colab).

The two real-network boundaries (yt-dlp subprocess, Colab runtime) are
mocked so nothing touches the network. The fallback's budget/availability
gates are exercised deterministically; a video with no captions routes to
the fallback only when the captions path returns a no-captions signal.
"""
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

    def test_success_returns_transcribed_text(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'stdout': 'first line\nsecond line\n',
                                                      'units': 1, 'elapsed_s': 30,
                                                      'session': 'colab'}) as run:
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(err)
        self.assertEqual(text, 'first line\nsecond line')
        # The code sent to Colab must include the URL and whisper imports.
        code = run.call_args.args[1]
        self.assertIn('faster_whisper', code)
        self.assertIn('yt_dlp', code)
        self.assertIn('https://youtu.be/abc', code)

    def test_error_from_colab_surface(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'error': 'runtime not granted'}):
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(text)
        self.assertIn('runtime not granted', err)


class ColabDenyExemption(unittest.TestCase):
    """The Colab whisper fallback is the sanctioned exception to the yt-dlp /
    bulk-media-download deny class (one validated YouTube URL, audio-only,
    transcribed to text), and the exemption must be exactly that narrow:
    agent-facing Colab runs that use yt-dlp stay refused, torrents stay refused
    unconditionally, and every other deny class still fires for the
    transcription path."""

    def test_transcription_passes_the_bulk_media_skip_label(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_gate_urls', return_value=None), \
             unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'stdout': 'hi there\n', 'units': 1,
                                                      'elapsed_s': 30, 'session': 'colab'}) as run:
            text, err = serve._youtube_transcript_colab('https://youtu.be/abc')
        self.assertIsNone(err)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get('skip_deny_labels'), ('bulk media download',))

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