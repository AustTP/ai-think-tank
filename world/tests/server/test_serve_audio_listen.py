"""Live audio listen (2026-11-14, operator direction): ffmpeg capture +
Groq whisper transcription fragment (212_audio_listen). Every function is
exercised against a fake wire and temp dirs -- no ffmpeg, no network, no
real telegram. THINK_TANK_DIR is patched so segment dirs and digest writes
stay inside the temp root.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 -m unittest tests.server.test_serve_audio_listen -v
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402

_TMP = None
_PATCHER = None
_NOW = 1_800_000_000.0


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='think-tank-audio-listen-')
    _PATCHER = unittest.mock.patch.object(serve, 'THINK_TANK_DIR', _TMP)
    _PATCHER.start()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


class AudioListenBase(unittest.TestCase):
    def setUp(self):
        serve._audio_state.update(feeds=[], alerts_last={}, digest_at=0.0,
                                  rate=[], patterns=[])

    def _feed(self, label='x'):
        return {'label': label, 'url': 'https://example.invalid/stream',
                'dir': os.path.join(_TMP, 'audio-listen', label),
                'proc': None, 'backoff': 5.0, 'next_start': 0.0,
                'ring': [], 'pending': []}


class FeedsParseTests(AudioListenBase):
    def test_parses_label_url_csv(self):
        self.assertEqual(
            serve._audio_feeds_parse(' scanner = http://s/1 , cam=https://s/2'),
            [('scanner', 'http://s/1'), ('cam', 'https://s/2')])

    def test_skips_bad_entries(self):
        self.assertEqual(serve._audio_feeds_parse('nourl,=x,a=b c,a=noscheme,'
                                                  'OK=http://y/1'),
                         [('ok', 'http://y/1')])

    def test_empty_is_dormant(self):
        self.assertEqual(serve._audio_feeds_parse(''), [])
        self.assertEqual(serve._audio_feeds_parse(None), [])


class MultipartTests(AudioListenBase):
    def test_fields_and_file_survive_roundtrip(self):
        body = serve._audio_multipart('abc', {'model': 'whisper'},
                                      'seg.wav', b'WAVBYTES')
        text = body.decode('utf-8', errors='replace')
        self.assertIn('name="model"', text)
        self.assertIn('whisper', text)
        self.assertIn('filename="seg.wav"', text)
        self.assertTrue(body.endswith(b'\r\n--abc--\r\n'))
        self.assertIn(b'WAVBYTES', body)


class ReadySegmentsTests(AudioListenBase):
    def test_only_closed_segments_in_order(self):
        feed = self._feed()
        os.makedirs(feed['dir'], exist_ok=True)
        old = os.path.join(feed['dir'], 'x-00001.wav')
        fresh = os.path.join(feed['dir'], 'x-00002.wav')
        junk = os.path.join(feed['dir'], 'notes.txt')
        for path in (old, fresh, junk):
            with open(path, 'wb') as f:
                f.write(b'x')
        os.utime(old, (_NOW - 60, _NOW - 60))
        os.utime(fresh, (_NOW - 2, _NOW - 2))
        self.assertEqual(serve._audio_ready_segments(feed, _NOW), [old])

    def test_missing_dir_is_empty(self):
        self.assertEqual(serve._audio_ready_segments(self._feed(), _NOW), [])

    def test_sweep_drops_only_stale(self):
        feed = self._feed()
        os.makedirs(feed['dir'], exist_ok=True)
        stale = os.path.join(feed['dir'], 'x-00001.wav')
        keep = os.path.join(feed['dir'], 'x-00002.wav')
        for path in (stale, keep):
            with open(path, 'wb') as f:
                f.write(b'x')
        os.utime(stale, (_NOW - 700, _NOW - 700))
        os.utime(keep, (_NOW - 60, _NOW - 60))
        serve._audio_sweep_segments(feed, _NOW)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(keep))


class RateWindowTests(AudioListenBase):
    def test_ten_per_minute_then_blocked(self):
        for _ in range(serve._AUDIO_LISTEN_TRANSCRIBE_PER_MIN):
            self.assertTrue(serve._audio_rate_ok(_NOW))
        self.assertFalse(serve._audio_rate_ok(_NOW))

    def test_window_slides(self):
        for _ in range(serve._AUDIO_LISTEN_TRANSCRIBE_PER_MIN):
            serve._audio_rate_ok(_NOW)
        self.assertTrue(serve._audio_rate_ok(_NOW + 61))


class TranscribeTests(AudioListenBase):
    def _wav(self):
        feed = self._feed()
        os.makedirs(feed['dir'], exist_ok=True)
        path = os.path.join(feed['dir'], 'x-00001.wav')
        with open(path, 'wb') as f:
            f.write(b'RIFFfake')
        return path

    def test_posts_multipart_and_returns_text(self):
        path = self._wav()
        captured = {}

        def fake_post(req, timeout=None):
            captured['auth'] = req.headers.get('Authorization')
            captured['ctype'] = req.headers.get('Content-type')
            captured['body'] = req.data
            return b'  tank said hello  '

        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'GROQ_API_KEY': 'g'}), \
            unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                       fake_post):
            self.assertEqual(serve._audio_transcribe_wav(path),
                             'tank said hello')
        self.assertEqual(captured['auth'], 'Bearer g')
        self.assertIn('boundary=', captured['ctype'] or '')
        self.assertIn(b'name="model"', captured['body'])
        self.assertIn(b'whisper-large-v3-turbo', captured['body'])
        self.assertIn(b'RIFFfake', captured['body'])

    def test_429_retries_once_after_retry_after(self):
        path = self._wav()
        sleeps = []
        serve._audio_transcribe_sleep = sleeps.append
        error = urllib.error.HTTPError('u', 429, 'rate limited',
                                       {'Retry-After': '3'}, None)
        responses = [error, b'second try']
        calls = []

        def fake_post(req, timeout=None):
            calls.append(1)
            out = responses.pop(0)
            if isinstance(out, Exception):
                raise out
            return out

        try:
            with unittest.mock.patch.object(
                    serve, '_load_env',
                    return_value={'GROQ_API_KEY': 'g'}), \
                unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                           fake_post):
                self.assertEqual(serve._audio_transcribe_wav(path),
                                 'second try')
        finally:
            serve._audio_transcribe_sleep = serve.time.sleep
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [3.0])

    def test_429_twice_raises(self):
        path = self._wav()
        serve._audio_transcribe_sleep = lambda s: None
        error = urllib.error.HTTPError('u', 429, 'rate limited',
                                       {'Retry-After': '0'}, None)
        try:
            with unittest.mock.patch.object(
                    serve, '_load_env',
                    return_value={'GROQ_API_KEY': 'g'}), \
                unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                           side_effect=error):
                with self.assertRaises(urllib.error.HTTPError):
                    serve._audio_transcribe_wav(path)
        finally:
            serve._audio_transcribe_sleep = serve.time.sleep

    def test_no_key_raises(self):
        path = self._wav()
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={}):
            with self.assertRaises(RuntimeError):
                serve._audio_transcribe_wav(path)


class AlertScanTests(AudioListenBase):
    def test_match_pushes_then_dedupes_then_other_label_pushes(self):
        serve._audio_state['patterns'] = serve._audio_alert_patterns('fire')
        sent = []
        with unittest.mock.patch.object(serve, '_telegram_send_text_sync',
                                        side_effect=lambda t: sent.append(t)):
            self.assertEqual(
                serve._audio_alert_scan('scanner', 'a fire started', _NOW), 1)
            self.assertEqual(
                serve._audio_alert_scan('scanner', 'fire again', _NOW + 10), 0)
            self.assertEqual(
                serve._audio_alert_scan('cam', 'fire too', _NOW + 20), 1)
            self.assertEqual(
                serve._audio_alert_scan('scanner', 'fire later',
                                        _NOW + 301), 1)
        self.assertEqual(len(sent), 3)
        self.assertIn('[audio:scanner]', sent[0])

    def test_bad_regex_skipped(self):
        self.assertEqual(len(serve._audio_alert_patterns('fire,(unclosed')), 1)


class HandleSegmentTests(AudioListenBase):
    def test_success_transcribes_records_alerts_and_removes(self):
        feed = self._feed()
        os.makedirs(feed['dir'], exist_ok=True)
        path = os.path.join(feed['dir'], 'x-00001.wav')
        with open(path, 'wb') as f:
            f.write(b'RIFFfake')
        serve._audio_state['patterns'] = serve._audio_alert_patterns('fire')
        sent = []
        with unittest.mock.patch.object(serve, '_audio_transcribe_wav',
                                        return_value='hello fire'), \
            unittest.mock.patch.object(serve, '_telegram_send_text_sync',
                                       side_effect=lambda t: sent.append(t)):
            self.assertTrue(serve._audio_handle_segment(feed, path, _NOW))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(feed['ring'], [(_NOW, 'hello fire')])
        self.assertEqual(feed['pending'], [(_NOW, 'hello fire')])
        self.assertEqual(len(sent), 1)

    def test_rate_block_leaves_file(self):
        feed = self._feed()
        os.makedirs(feed['dir'], exist_ok=True)
        path = os.path.join(feed['dir'], 'x-00001.wav')
        with open(path, 'wb') as f:
            f.write(b'RIFFfake')
        for _ in range(serve._AUDIO_LISTEN_TRANSCRIBE_PER_MIN):
            serve._audio_rate_ok(_NOW)
        self.assertFalse(serve._audio_handle_segment(feed, path, _NOW))
        self.assertTrue(os.path.exists(path))

    def test_silent_transcript_removes_file_without_record(self):
        feed = self._feed()
        os.makedirs(feed['dir'], exist_ok=True)
        path = os.path.join(feed['dir'], 'x-00001.wav')
        with open(path, 'wb') as f:
            f.write(b'RIFFfake')
        with unittest.mock.patch.object(serve, '_audio_transcribe_wav',
                                        return_value='   '):
            self.assertTrue(serve._audio_handle_segment(feed, path, _NOW))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(feed['ring'], [])


class DigestTests(AudioListenBase):
    def test_writes_pending_clears_and_skips_silence(self):
        serve._audio_state['feeds'] = [self._feed('x'), self._feed('y')]
        serve._audio_state['feeds'][0]['pending'] = [(_NOW - 60,
                                                      'hello world')]
        path = serve._audio_digest_write(_NOW)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('## x', text)
        self.assertIn('hello world', text)
        self.assertNotIn('## y', text)
        self.assertEqual(serve._audio_state['feeds'][0]['pending'], [])
        self.assertIsNone(serve._audio_digest_write(_NOW + 1))


class LoopGateTests(AudioListenBase):
    def test_dormant_without_feeds(self):
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={}):
            asyncio.run(serve._audio_listen_loop())

    def test_dormant_without_ffmpeg(self):
        feeds_env = {'AUDIO_LISTEN_FEEDS': 'x=http://s/1'}
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value=feeds_env), \
                unittest.mock.patch.object(shutil, 'which',
                                           return_value=None):
            asyncio.run(serve._audio_listen_loop())


if __name__ == '__main__':
    unittest.main()
