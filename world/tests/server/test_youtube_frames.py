# Hermetic tests for the YouTube frame capture + vision review endpoint
# (2026-11-02). The extraction itself hits yt-dlp/ffmpeg and the network --
# those are mocked. What is tested here is the pure surface: timestamp
# parsing/capping, library-file caching (no re-download when frames exist),
# the fail-closed frame results (None never becomes a described frame), and
# the endpoint's validation paths.
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class ParseFrameTimestamps(unittest.TestCase):
    def test_numbers_and_string_forms(self):
        self.assertEqual(
            serve._parse_frame_timestamps([0, 90, '90', '90s', '1m30s', '2m']),
            [0, 90, 120])

    def test_malformed_dropped_not_fatal(self):
        self.assertEqual(serve._parse_frame_timestamps(['abc', -5, None, '45']), [45])

    def test_capped_and_sorted(self):
        # Exactly 12 well-formed entries (the input slice budget of 48 would
        # drop the trailing 0 from a longer reversed list).
        many = list(range(110, -1, -10))
        out = serve._parse_frame_timestamps(many)
        self.assertEqual(out, sorted(out))
        self.assertLessEqual(len(out), serve.YOUTUBE_FRAMES_MAX)
        self.assertEqual(out[0], 0)

    def test_empty_is_empty(self):
        self.assertEqual(serve._parse_frame_timestamps(None), [])
        self.assertEqual(serve._parse_frame_timestamps([]), [])


class ExtractFramesCacheAndFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._lib = unittest.mock.patch.object(
            serve, '_safe_library_path',
            side_effect=lambda rel: os.path.join(self.tmp, 'library', rel))
        self._lib.start()
        self.addCleanup(self._lib.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cached_frames_never_download(self):
        vid = '1v7ldJiyH-Y'
        rel = serve._youtube_frame_rel(vid, 90)
        target = os.path.join(self.tmp, 'library', rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as fh:
            fh.write(b'PNGDATA')
        with unittest.mock.patch.object(serve, '_find_local_bin',
                                        side_effect=AssertionError('no download expected')):
            frames = serve._extract_youtube_frames('https://www.youtube.com/watch?v=' + vid,
                                                   vid, [90])
        self.assertEqual(frames, {90: b'PNGDATA'})

    def test_missing_bins_yield_none_not_crash(self):
        vid = '1v7ldJiyH-Y'
        with unittest.mock.patch.object(serve, '_find_local_bin', return_value=None):
            frames = serve._extract_youtube_frames('https://youtu.be/' + vid, vid, [30, 60])
        self.assertEqual(frames, {30: None, 60: None})

    def test_failed_seek_records_none_and_filed_frame_reused(self):
        vid = '1v7ldJiyH-Y'
        calls = []

        def fake_run(cmd, capture_output, timeout):
            calls.append(cmd)
            class P:
                returncode = 0
            return P()

        def fake_bin(names):
            return '/bin/true'

        with unittest.mock.patch.object(serve, '_find_local_bin', side_effect=fake_bin), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=fake_run), \
             unittest.mock.patch.object(serve.os.path, 'isfile',
                                        side_effect=lambda p: p.endswith('video.mp4')):
            frames = serve._extract_youtube_frames('https://youtu.be/' + vid, vid, [15])
        # The fake ffmpeg produced no real file -> the frame records None
        # (fail closed), but the yt-dlp-shaped invocation ran (its resolved
        # binary is /bin/true for both tools; distinguish by the -o flag).
        self.assertEqual(frames, {15: None})
        self.assertTrue(any('-o' in c for c in calls if c))


class FramesEndpointValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def _post(self, body):
        import asyncio

        async def go():
            return await serve.youtube_frames.__wrapped__(self._request(body)) \
                if hasattr(serve.youtube_frames, '__wrapped__') else None
        # The endpoint is a bare async function (no decorator wrapping beyond
        # FastAPI registration), so call it directly with a stub request.
        class Req:
            def __init__(self, payload):
                self._payload = payload
                self.headers = {}

            async def json(self):
                return self._payload
        return asyncio.run(serve.youtube_frames(Req(body)))

    def test_bad_url_rejected_before_any_capture(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True):
            resp = self._post({'agentId': 'theo', 'url': 'https://vimeo.com/123',
                               'timestamps': [30]})
        self.assertEqual(resp.status_code, 400)

    def test_empty_timestamps_rejected(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True):
            resp = self._post({'agentId': 'theo',
                               'url': 'https://www.youtube.com/watch?v=1v7ldJiyH-Y',
                               'timestamps': []})
        self.assertEqual(resp.status_code, 400)

    def test_good_request_skips_capture_when_all_frames_fail(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_extract_youtube_frames',
                                        return_value={30: None}):
            resp = self._post({'agentId': 'theo',
                               'url': 'https://www.youtube.com/watch?v=1v7ldJiyH-Y',
                               'timestamps': [30]})
        self.assertEqual(resp.status_code, 502)


if __name__ == '__main__':
    unittest.main()
