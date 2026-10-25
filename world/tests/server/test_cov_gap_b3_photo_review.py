"""Coverage-gap tests for the PPTX/ingest part's photo-review surface.

Only new tests. Photos, model calls, and config storage are patched fakes; files
stay under the module temp LIBRARY_DIR.
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
from test_serve_helpers import (  # noqa: E402
    setUpModule as _base_setup, tearDownModule as _base_teardown)


def setUpModule():
    _base_setup()
    os.makedirs(serve.LIBRARY_DIR, exist_ok=True)


def tearDownModule():
    _base_teardown()


def _asset(uuid='u1', local=True, filename='photo.jpg', date='2026-01-02'):
    return {'uuid': uuid, 'local': local,
            'localPath': f'/tmp/{uuid}.heic',
            'filename': filename, 'date': date}


class PhotoReviewFromImageGapTests(unittest.TestCase):
    def test_vision_exception(self):
        with mock.patch.object(serve, '_vision_tier_slug',
                               return_value='vision'), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               side_effect=RuntimeError('model down')):
            out = serve._photo_review_from_image('abc', 'label')
        self.assertFalse(out['ok'])
        self.assertIn('model down', out['note'])

    def test_reply_parse_exception_and_empty(self):
        with mock.patch.object(serve, '_vision_tier_slug',
                               return_value='vision'), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               return_value=[]):
            out = serve._photo_review_from_image('abc', 'label')
        self.assertFalse(out['ok'])
        self.assertIn('no reply', out['note'])


class PhotoAuthAndManifestGapTests(unittest.TestCase):
    def test_auth_player_path(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')):
            self.assertEqual((True, 'player'), serve._photo_review_auth(req))

    def test_manifest_missing_path(self):
        with mock.patch.object(serve, '_photo_manifest_path', return_value=''):
            self.assertEqual({}, serve._load_photo_manifest())

    def test_manifest_bad_json(self):
        directory = os.path.join(serve.LIBRARY_DIR, 'photo-reviews')
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, '.reviewed.json'), 'w') as fh:
            fh.write('{not json')
        self.assertEqual({}, serve._load_photo_manifest())

    def test_save_manifest_missing_path(self):
        with mock.patch.object(serve, '_photo_manifest_path', return_value=''):
            self.assertFalse(serve._save_photo_manifest({}))


class PhotoNeedsReviewGapTests(unittest.TestCase):
    def test_unknown_dict_status_needs_review(self):
        self.assertTrue(serve._photo_needs_review(
            {'k': {'status': 'something-else'}}, 'k'))
        self.assertFalse(serve._photo_needs_review(
            {'k': 'non-dict'}, 'k'))


class PhotoWatchConfigGapTests(unittest.TestCase):
    def test_config_parsing(self):
        with mock.patch.object(serve, '_get_setting', return_value=None):
            self.assertEqual({'enabled': False},
                             serve._photo_watch_config())
        with mock.patch.object(serve, '_get_setting',
                               return_value='{"enabled": true, "album": "A"}'):
            self.assertEqual({'enabled': True, 'album': 'A'},
                             serve._photo_watch_config())
        with mock.patch.object(serve, '_get_setting', return_value='[1, 2]'):
            self.assertEqual({'enabled': False},
                             serve._photo_watch_config())
        with mock.patch.object(serve, '_get_setting', return_value='{bad'):
            self.assertEqual({'enabled': False},
                             serve._photo_watch_config())

    def test_save_config(self):
        seen = {}
        with mock.patch.object(serve, '_set_setting',
                               side_effect=lambda k, v: seen.update(
                                   {'key': k, 'value': v})):
            cfg = serve._photo_save_watch_config({'enabled': True})
        self.assertEqual({'enabled': True}, cfg)
        self.assertEqual(serve.PHOTO_WATCH_SETTING, seen['key'])
        self.assertEqual({'enabled': True}, json.loads(seen['value']))


class PrunePhotoReviewsGapTests(unittest.TestCase):
    def test_missing_root_returns_zero(self):
        with mock.patch.object(serve, '_safe_library_path', return_value=''):
            self.assertEqual(0, serve._prune_photo_reviews_sync())

    def test_oserror_is_skipped(self):
        root = os.path.join(serve.LIBRARY_DIR, 'photo-reviews')
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, 'old.jpg')
        with open(path, 'wb') as fh:
            fh.write(b'x')
        with mock.patch('os.path.getmtime', side_effect=OSError('gone')):
            self.assertEqual(0, serve._prune_photo_reviews_sync())

    def test_log_action_failure_is_ignored(self):
        root = os.path.join(serve.LIBRARY_DIR, 'photo-reviews')
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, 'old2.jpg')
        with open(path, 'wb') as fh:
            fh.write(b'x')
        old = 1
        os.utime(path, (old, old))
        with mock.patch.object(serve, 'log_action',
                               side_effect=RuntimeError('db down')):
            self.assertGreaterEqual(serve._prune_photo_reviews_sync(), 1)


class ReviewAlbumBatchGapTests(unittest.TestCase):
    def test_lock_busy(self):
        class BusyLock:
            def acquire(self, blocking=False):
                return False

            def release(self):
                raise AssertionError('a busy lock must not be released')

        with mock.patch.object(serve, '_PHOTO_REVIEW_LOCK', BusyLock()):
            out = serve._review_album_batch_sync(
                'A', 'proj', 'general', '', 5, 'ada', assets=[_asset()])
        self.assertTrue(out['busy'])
        self.assertFalse(out['ok'])

    def test_no_pending_assets(self):
        with mock.patch.object(
                serve, '_load_photo_manifest',
                return_value={'A::u1': {'status': 'baseline'}}):
            out = serve._review_album_batch_sync(
                'A', 'proj', 'general', '', 5, 'ada', assets=[_asset()])
        self.assertTrue(out['ok'])
        self.assertEqual(0, out['pending'])

    def test_invalid_photo_directory(self):
        with mock.patch.object(serve, '_load_photo_manifest',
                               return_value={}), \
             mock.patch.object(serve, '_safe_library_path',
                               return_value=''):
            out = serve._review_album_batch_sync(
                'A', 'proj', 'general', '', 5, 'ada', assets=[_asset()])
        self.assertFalse(out['ok'])
        self.assertIn('invalid photo-reviews path', out['error'])

    def test_non_local_asset_waits(self):
        with mock.patch.object(serve, '_load_photo_manifest',
                               return_value={}):
            out = serve._review_album_batch_sync(
                'A', 'proj', 'general', '', 5, 'ada',
                assets=[_asset(local=False)])
        self.assertTrue(out['ok'])
        self.assertEqual(0, out['reviewedCount'])
        self.assertIn('not downloaded', out['reviewed'][0]['error'])

    def test_invalid_output_paths(self):
        root = os.path.join(serve.LIBRARY_DIR, 'photo-reviews')
        os.makedirs(root, exist_ok=True)
        jpeg = os.path.join(root, 'tmp.jpg')
        with open(jpeg, 'wb') as fh:
            fh.write(b'jpeg')

        def safe(rel):
            if rel.endswith('.jpg') and not rel.endswith('tmp.jpg'):
                return ''
            return os.path.join(serve.LIBRARY_DIR, rel)

        with mock.patch.object(serve, '_load_photo_manifest',
                               return_value={}), \
             mock.patch.object(serve, '_safe_library_path',
                               side_effect=safe), \
             mock.patch.object(serve._apple_photos, 'normalize_to_jpeg',
                               return_value=(jpeg, None)), \
             mock.patch.object(serve, '_photo_review_from_image',
                               return_value={'ok': True, 'brief': 'b'}):
            out = serve._review_album_batch_sync(
                'A', 'proj', 'general', '', 5, 'ada', assets=[_asset()])
        self.assertTrue(out['ok'])
        self.assertIn('invalid output path', out['reviewed'][0]['error'])

    def test_exception_records_error_and_cleans_tmp(self):
        tmp = tempfile.mkdtemp(prefix='photo-tmp-')
        with mock.patch.object(serve, '_load_photo_manifest',
                               return_value={}), \
             mock.patch.object(serve._apple_photos, 'normalize_to_jpeg',
                               return_value=(os.path.join(tmp, 'gone.jpg'),
                                             tmp)):
            out = serve._review_album_batch_sync(
                'A', 'proj', 'general', '', 5, 'ada', assets=[_asset()])
        self.assertTrue(out['ok'])
        self.assertEqual(0, out['reviewedCount'])
        self.assertIn('gone.jpg', out['reviewed'][0]['error'])
        self.assertFalse(os.path.exists(tmp))


class PhotoWatchTickGapTests(unittest.TestCase):
    def test_disabled(self):
        with mock.patch.object(serve, '_photo_watch_config',
                               return_value={'enabled': False}):
            self.assertIsNone(serve._photo_watch_tick_sync())

    def test_tick_error_is_recorded(self):
        saved = {}
        with mock.patch.object(serve, '_photo_watch_config',
                               return_value={'enabled': True,
                                             'album': 'A'}), \
             mock.patch.object(serve, '_review_album_batch_sync',
                               side_effect=RuntimeError('photos down')), \
             mock.patch.object(serve, '_photo_save_watch_config',
                               side_effect=lambda cfg: saved.update(cfg)):
            self.assertIsNone(serve._photo_watch_tick_sync())
        self.assertIn('photos down', saved['lastError'])

    def test_tick_success_updates_counts(self):
        saved = {}
        with mock.patch.object(serve, '_photo_watch_config',
                               return_value={'enabled': True, 'album': 'A',
                                             'reviewedCount': 1}), \
             mock.patch.object(serve, '_review_album_batch_sync',
                               return_value={'ok': True, 'pending': 4,
                                             'reviewedCount': 2}), \
             mock.patch.object(serve, '_photo_save_watch_config',
                               side_effect=lambda cfg: saved.update(cfg)):
            out = serve._photo_watch_tick_sync()
        self.assertEqual(4, out['pending'])
        self.assertEqual(3, saved['reviewedCount'])
        self.assertEqual(4, saved['pending'])
        self.assertIn('lastReviewedAt', saved)


class PhotoWatchLoopGapTests(unittest.TestCase):
    def test_loop_tick_failure_is_contained(self):
        class StopLoop(Exception):
            pass

        async def fake_sleep(_seconds):
            fake_sleep.calls += 1
            if fake_sleep.calls > 1:
                raise StopLoop

        async def fake_to_thread(fn):
            raise RuntimeError('tick failed')

        fake_sleep.calls = 0
        with mock.patch.object(serve.asyncio, 'sleep',
                               side_effect=fake_sleep), \
             mock.patch.object(serve.asyncio, 'to_thread',
                               side_effect=fake_to_thread):
            with self.assertRaises(StopLoop):
                serve.asyncio.run(serve._photo_album_watch_loop())
        self.assertEqual(2, fake_sleep.calls)


class PhotoAlbumsEndpointGapTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(serve.app)
        self.client.cookies.set(serve.SESSION_COOKIE_NAME,
                                serve.create_session())

    def test_unauthorized(self):
        with mock.patch.object(serve, '_photo_review_auth',
                               return_value=(False, None)):
            resp = self.client.get('/api/photo-review/albums')
        self.assertEqual(401, resp.status_code)

    def test_success(self):
        with mock.patch.object(serve, '_photo_review_auth',
                               return_value=(True, 'player')), \
             mock.patch.object(serve._apple_photos, 'list_albums',
                               return_value=['A', 'B']):
            resp = self.client.get('/api/photo-review/albums')
        self.assertEqual(200, resp.status_code)
        self.assertEqual(['A', 'B'], resp.json()['albums'])

    def test_missing_library_and_generic_error(self):
        with mock.patch.object(serve, '_photo_review_auth',
                               return_value=(True, 'player')), \
             mock.patch.object(serve._apple_photos, 'list_albums',
                               side_effect=FileNotFoundError('no photos')):
            resp = self.client.get('/api/photo-review/albums')
        self.assertEqual(503, resp.status_code)
        with mock.patch.object(serve, '_photo_review_auth',
                               return_value=(True, 'player')), \
             mock.patch.object(serve._apple_photos, 'list_albums',
                               side_effect=ValueError('bad db')):
            resp = self.client.get('/api/photo-review/albums')
        self.assertEqual(502, resp.status_code)


if __name__ == '__main__':
    unittest.main()
