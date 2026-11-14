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
import time
import unittest
import unittest.mock
import urllib.error
import uuid

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


class ColabOverflowTests(AudioListenBase):
    """The Colab overflow leg (operator, 2026-11-14): segments the Groq leg
    cannot take queue for a batched faster-whisper job on the standby CPU
    session -- borrowed only when it already exists, never provisioned."""

    def setUp(self):
        super().setUp()
        serve._audio_state.update(colab=[], colab_pending=set(),
                                  colab_next=0.0, colab_enabled=True,
                                  colab_ok=None)

    def tearDown(self):
        serve._audio_state.update(colab=[], colab_pending=set(),
                                  colab_next=0.0, colab_enabled=False,
                                  colab_ok=None)

    def _wav(self, label='x', name=None):
        feed = self._feed(label)
        os.makedirs(feed['dir'], exist_ok=True)
        name = name or '%s.wav' % uuid.uuid4().hex
        path = os.path.join(feed['dir'], name)
        with open(path, 'wb') as f:
            f.write(b'RIFFfake')
        return feed, path

    def test_enqueue_requires_enabled_leg(self):
        serve._audio_state['colab_enabled'] = False
        feed, path = self._wav()
        self.assertFalse(serve._audio_colab_enqueue(feed, path))
        serve._audio_state['colab_enabled'] = True
        self.assertTrue(serve._audio_colab_enqueue(feed, path))
        self.assertEqual(serve._audio_state['colab'],
                         [(feed['label'], path)])
        # Already claimed: idempotent True, no duplicate entry.
        self.assertTrue(serve._audio_colab_enqueue(feed, path))
        self.assertEqual(len(serve._audio_state['colab']), 1)

    def test_enqueue_honors_queue_cap(self):
        serve._audio_state['colab_enabled'] = True
        feed, path = self._wav()
        serve._audio_state['colab'] = [
            ('x', '/tmp/%d.wav' % i)
            for i in range(serve._AUDIO_COLAB_QUEUE_MAX)]
        self.assertFalse(serve._audio_colab_enqueue(feed, path))
        self.assertEqual(len(serve._audio_state['colab']),
                         serve._AUDIO_COLAB_QUEUE_MAX)

    def test_rate_refusal_enqueues_when_leg_enabled(self):
        feed, path = self._wav()
        serve._audio_state['rate'] = [time.time()] * \
            serve._AUDIO_LISTEN_TRANSCRIBE_PER_MIN
        self.assertFalse(serve._audio_handle_segment(feed, path, time.time()))
        self.assertEqual(serve._audio_state['colab'],
                         [(feed['label'], path)])
        self.assertTrue(os.path.exists(path), 'file stays on disk')

    def test_groq_failure_enqueues_when_leg_enabled(self):
        feed, path = self._wav()
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={}), \
                unittest.mock.patch.object(serve, '_audio_rate_ok',
                                           return_value=True):
            self.assertFalse(
                serve._audio_handle_segment(feed, path, time.time()))
        self.assertEqual(serve._audio_state['colab'],
                         [(feed['label'], path)])

    def test_pending_paths_are_never_redispatched(self):
        _, path = self._wav()
        serve._audio_state['colab_pending'].add(path)
        feed = self._feed()
        # The leg owning the path is invisible to a re-enqueue attempt.
        self.assertTrue(serve._audio_colab_enqueue(feed, path))
        self.assertEqual(serve._audio_state['colab'], [])

    def test_tick_skips_without_a_live_cpu_session(self):
        feed, path = self._wav()
        serve._audio_state['colab'] = [(feed['label'], path)]
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
                unittest.mock.patch.object(serve, '_colab_session_exists',
                                           return_value=False):
            serve._audio_colab_tick(time.time())
        self.assertEqual(serve._audio_state['colab'],
                         [(feed['label'], path)],
                         'the batch stays queued for a later tick')
        self.assertIs(serve._audio_state['colab_ok'], False)

    def test_tick_harness_failure_keeps_batch_queued(self):
        feed, path = self._wav()
        serve._audio_state['colab'] = [(feed['label'], path)]
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
                unittest.mock.patch.object(serve, '_colab_session_exists',
                                           return_value=True), \
                unittest.mock.patch.object(
                    serve, '_audio_colab_transcribe_batch',
                    side_effect=RuntimeError('colab exec failed: x')):
            serve._audio_colab_tick(time.time())
        self.assertEqual(serve._audio_state['colab'],
                         [(feed['label'], path)])
        self.assertTrue(os.path.exists(path), 'no file is consumed')
        self.assertIsNot(serve._audio_state['colab_next'], 0.0,
                         'a failure arms the backoff')

    def test_tick_records_results_and_consumes_the_batch(self):
        feed, path = self._wav(label='x', name='seg.wav')
        other = self._feed('x')
        serve._audio_state['feeds'] = [feed]
        serve._audio_state['colab'] = [(feed['label'], path)]
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
                unittest.mock.patch.object(serve, '_colab_session_exists',
                                           return_value=True), \
                unittest.mock.patch.object(
                    serve, '_audio_colab_transcribe_batch',
                    return_value={path: ' tank said hello  '}):
            serve._audio_colab_tick(time.time())
        self.assertEqual(serve._audio_state['colab'], [])
        self.assertNotIn(path, serve._audio_state['colab_pending'])
        self.assertFalse(os.path.exists(path), 'the segment is consumed')
        self.assertEqual(other['ring'], [])
        self.assertEqual(feed['ring'][-1][1], 'tank said hello')
        self.assertEqual(feed['pending'][-1][1], 'tank said hello')

    def test_transcribe_batch_uploads_tars_and_parses_result(self):
        feed, path = self._wav(label='cam', name='seg1.wav')
        batch = [(feed['label'], path)]
        calls = []

        def fake_cli(*args, **kwargs):
            calls.append(list(args))
            if args[0] == 'upload':
                # The tar exists at upload time: prove the arc names here.
                import tarfile as _tf
                arc = serve._audio_colab_arcname('cam', path)
                with _tf.open(args[3]) as t:
                    assert t.getnames() == [arc], t.getnames()
                return 0, 'ok'
            if args[0] == 'exec':
                arc = serve._audio_colab_arcname('cam', path)
                return 0, ('noise\n%s{"%s": " hello from colab "}\ntrailer'
                           % (serve._AUDIO_COLAB_RESULT_TAG, arc))
            return 1, 'unexpected call'

        with unittest.mock.patch.object(serve, '_colab_cli', fake_cli):
            results = serve._audio_colab_transcribe_batch(batch)
        self.assertEqual(results, {path: ' hello from colab '})
        upload_call = [c for c in calls if c[0] == 'upload'][0]
        self.assertEqual(upload_call[1], '-s')
        self.assertIn('think-tank-cpu', upload_call)


if __name__ == '__main__':
    unittest.main()
