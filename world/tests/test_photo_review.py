"""On-demand photo review: path scoping, prompt modes, and fail-soft shape."""

import unittest
from unittest import mock

import serve
import apple_photos


class PhotoReviewPathTest(unittest.TestCase):
    def test_default_and_general_share_the_root(self):
        self.assertEqual(serve._photo_review_rel(''), 'photo-reviews')
        self.assertEqual(serve._photo_review_rel('general'), 'photo-reviews')
        self.assertEqual(serve._photo_review_rel(None), 'photo-reviews')

    def test_project_is_sanitized_into_a_subdirectory(self):
        self.assertEqual(serve._photo_review_rel('Store Front'), 'photo-reviews/Store_Front')
        self.assertEqual(serve._photo_review_rel('../etc'), 'photo-reviews/etc')


class PhotoReviewPromptTest(unittest.TestCase):
    def setUp(self):
        self.result = {'choices': [{'message': {'content': 'SUBJECT: a cat\nTEXT: none'}}]}
        self.patches = [
            mock.patch.object(serve, '_vision_tier_slug', return_value='vision/test'),
            mock.patch.object(serve, '_call_openrouter_sync', return_value=self.result),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_general_prompt_is_grounded_and_sectioned(self):
        out = serve._photo_review_from_image('AAAA', 'photo 1', context='whiteboard')
        self.assertTrue(out['ok'])
        messages = serve._call_openrouter_sync.call_args[0][1]
        prompt = messages[0]['content'][0]['text']
        for needle in ('SUBJECT:', 'DETAILS:', 'TEXT:', 'IDEAS:',
                       'untrusted data', 'whiteboard'):
            self.assertIn(needle, prompt)
        self.assertTrue(messages[0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_design_mode_delegates_to_the_existing_brief(self):
        with mock.patch.object(serve, '_design_brief_from_image',
                               return_value={'ok': True, 'brief': 'PALETTE: #fff'}) as brief:
            out = serve._photo_review_from_image('AAAA', 'photo 1', mode='design')
        brief.assert_called_once()
        self.assertEqual(out['brief'], 'PALETTE: #fff')

    def test_missing_vision_tier_fails_soft(self):
        with mock.patch.object(serve, '_vision_tier_slug', return_value=None):
            out = serve._photo_review_from_image('AAAA', 'photo 1')
        self.assertFalse(out['ok'])
        self.assertIn('vision model tier', out['note'])

    def test_empty_reply_fails_soft(self):
        with mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [{'message': {'content': ''}}]}):
            out = serve._photo_review_from_image('AAAA', 'photo 1')
        self.assertFalse(out['ok'])
        self.assertIn('no reply', out['note'])


class ApplePhotosLibraryTest(unittest.TestCase):
    def setUp(self):
        import sqlite3, tempfile, os
        self.tmp = tempfile.mkdtemp(prefix='tt-photos-lib-')
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'database'))
        os.makedirs(os.path.join(self.tmp, 'originals', 'AB'))
        with open(os.path.join(self.tmp, 'originals', 'AB', 'local.JPG'), 'wb') as f:
            f.write(b'\xff\xd8\xff' + b'x' * 100)
        db = sqlite3.connect(os.path.join(self.tmp, 'database', 'Photos.sqlite'))
        db.execute('CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)')
        db.execute('''CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT,
            ZFILENAME TEXT, ZDIRECTORY TEXT, ZFAVORITE INTEGER, ZDATECREATED REAL,
            ZADDEDDATE REAL, ZKIND INTEGER, ZTRASHEDSTATE INTEGER, ZHIDDEN INTEGER)''')
        db.execute('CREATE TABLE Z_30ASSETS (Z_30ALBUMS INTEGER, Z_3ASSETS INTEGER)')
        db.execute("INSERT INTO ZGENERICALBUM VALUES (7, 'Tagged')")
        rows = [
            (1, 'U1', 'local.JPG', 'AB', 0, 0, 300, 0, 0, 0),
            (2, 'U2', 'cloud.JPG', 'CD', 0, 0, 200, 0, 0, 0),
            (3, 'U3', 'video.MOV', 'AB', 0, 0, 400, 1, 0, 0),
            (4, 'U4', 'trashed.JPG', 'AB', 0, 0, 500, 0, 1, 0),
            (5, 'U5', 'hidden.JPG', 'AB', 0, 0, 600, 0, 0, 1),
        ]
        db.executemany('INSERT INTO ZASSET VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
        db.executemany('INSERT INTO Z_30ASSETS VALUES (7, ?)', [(1,), (2,), (3,), (4,), (5,)])
        db.commit(); db.close()
        self.patch = mock.patch.object(apple_photos, 'library_root', return_value=self.tmp)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_only_photos_in_the_album_are_returned_local_flag_is_exact(self):
        out = apple_photos.album_assets('Tagged')
        self.assertEqual([a['uuid'] for a in out], ['U1', 'U2'])
        self.assertTrue(out[0]['local'])
        self.assertFalse(out[1]['local'])

    def test_list_albums(self):
        self.assertEqual(apple_photos.list_albums(), ['Tagged'])

    def test_album_lookup_is_case_and_space_insensitive(self):
        self.assertEqual([a['uuid'] for a in apple_photos.album_assets('tagged')],
                         ['U1', 'U2'])
        self.assertEqual([a['uuid'] for a in apple_photos.album_assets('  TaGgEd ')],
                         ['U1', 'U2'])
        self.assertEqual(apple_photos.album_assets('missing'), [])


class PhotoReviewAuthTest(unittest.TestCase):
    def _request(self, key=None):
        import types
        headers = {'X-Photo-Key': key} if key is not None else {}
        return types.SimpleNamespace(headers=headers)

    def test_photo_key_is_accepted(self):
        with mock.patch.object(serve, '_operator_gate', return_value=(False, None)), \
             mock.patch.object(serve, '_load_env', return_value={'PHOTO_REVIEW_KEY': 'sekret'}):
            self.assertEqual(serve._photo_review_auth(self._request('sekret')),
                             (True, 'player'))
            self.assertEqual(serve._photo_review_auth(self._request('wrong')),
                             (False, None))

    def test_agent_director_is_refused_even_with_a_session(self):
        with mock.patch.object(serve, '_operator_gate', return_value=(True, 'nora')):
            self.assertEqual(serve._photo_review_auth(self._request()),
                             (False, None))

    def test_missing_env_key_disables_the_shortcut_credential(self):
        with mock.patch.object(serve, '_operator_gate', return_value=(False, None)), \
             mock.patch.object(serve, '_load_env', return_value={}):
            self.assertEqual(serve._photo_review_auth(self._request('anything')),
                             (False, None))

    def test_manifest_roundtrip(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix='tt-photo-manifest-')
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        with mock.patch.object(serve, 'LIBRARY_DIR', tmp):
            self.assertTrue(serve._save_photo_manifest({'Tagged::U1': '2026-10-18'}))
            self.assertEqual(serve._load_photo_manifest(), {'Tagged::U1': '2026-10-18'})


class PhotoKeyRotationTest(unittest.TestCase):
    def test_rotation_is_player_only(self):
        import asyncio, json as _json
        with mock.patch.object(serve, '_operator_gate', return_value=(True, 'nora')):
            self.assertEqual(asyncio.run(serve.photo_review_key_rotate(None)).status_code, 403)
        with mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
             mock.patch.object(serve, '_set_env_keys') as set_env, \
             mock.patch.object(serve, '_reload_env_config'), \
             mock.patch.object(serve, 'log_action'):
            r = asyncio.run(serve.photo_review_key_rotate(None))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(_json.loads(r.body).get('key'))
        set_env.assert_called_once()


class PhotoRetentionTest(unittest.TestCase):
    def test_prune_removes_only_files_older_than_the_window(self):
        import os, tempfile, time
        tmp = tempfile.mkdtemp(prefix='tt-photo-retention-')
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        root = os.path.join(tmp, 'photo-reviews', 'photos')
        os.makedirs(root)
        old = os.path.join(root, 'old.jpg')
        new = os.path.join(root, 'new.txt')
        manifest = os.path.join(tmp, 'photo-reviews', '.reviewed.json')
        for path in (old, new, manifest):
            with open(path, 'w') as f:
                f.write('x')
        os.utime(old, (time.time() - 90 * 86400,) * 2)
        with mock.patch.object(serve, 'LIBRARY_DIR', tmp), \
             mock.patch.object(serve, 'PHOTO_REVIEW_RETENTION_DAYS', 30), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(serve._prune_photo_reviews_sync(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(manifest))


class PhotoWatchTest(unittest.TestCase):
    def test_needs_review_semantics(self):
        needs = serve._photo_needs_review
        self.assertTrue(needs({}, 'a::1'))
        self.assertFalse(needs({'a::1': {'status': 'baseline'}}, 'a::1'))
        self.assertFalse(needs({'a::1': {'status': 'done'}}, 'a::1'))
        self.assertFalse(needs({'a::1': '2026-10-18T00:00:00Z'}, 'a::1'))
        self.assertTrue(needs({'a::1': {'status': 'error', 'attempts': 2}}, 'a::1'))
        self.assertFalse(needs({'a::1': {'status': 'error', 'attempts': 3}}, 'a::1'))
        self.assertTrue(needs({'a::1': {'status': 'waiting-local'}}, 'a::1'))

    def _asset(self, uuid='ABCDEF12-0000-0000-0000-000000000000'):
        return {'uuid': uuid, 'filename': 'x.jpg', 'date': '2026-10-18T10:00:00+00:00',
                'favorite': False, 'local': True, 'localPath': '/tmp/original.jpg'}

    def test_baseline_marks_without_reviewing(self):
        manifest = {}
        with mock.patch.object(serve._apple_photos, 'album_assets',
                               return_value=[self._asset('U1'), self._asset('U2')]), \
             mock.patch.object(serve, '_load_photo_manifest', side_effect=lambda: dict(manifest)), \
             mock.patch.object(serve, '_save_photo_manifest',
                               side_effect=lambda d: manifest.update(d) or True):
            self.assertEqual(serve._photo_baseline_album('Tagged'), 2)
        self.assertEqual(manifest['Tagged::U1']['status'], 'baseline')
        self.assertFalse(serve._photo_needs_review(manifest, 'Tagged::U1'))

    def _run_batch(self, assets, attempts=1,
                   review=None):
        import tempfile, os
        tmp = tempfile.mkdtemp(prefix='tt-photo-batch-')
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        jpeg = os.path.join(tmp, 'photo.jpg')
        with open(jpeg, 'wb') as fh:
            fh.write(b'\xff\xd8\xff' + b'x' * 50)
        manifest = {}
        patches = [
            mock.patch.object(serve, 'LIBRARY_DIR', tmp),
            mock.patch.object(serve, 'log_action'),
            mock.patch.object(serve, '_photo_review_rel', return_value='photo-reviews/photos'),
            mock.patch.object(serve._apple_photos, 'normalize_to_jpeg',
                              return_value=(jpeg, None)),
            mock.patch.object(serve, '_load_photo_manifest', side_effect=lambda: dict(manifest)),
            mock.patch.object(serve, '_save_photo_manifest',
                              side_effect=lambda d: manifest.update(d) or True),
            mock.patch.object(serve, '_photo_review_from_image',
                              return_value=review or {'ok': True, 'brief': 'SUBJECT: x'}),
        ]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        out = None
        for _ in range(attempts):
            out = serve._review_album_batch_sync(
                'Tagged', 'photos', 'general', '', 3, 'player', assets=assets)
        return manifest, out

    def test_success_marks_done_and_writes_brief(self):
        manifest, out = self._run_batch([self._asset()])
        self.assertEqual(out['reviewedCount'], 1)
        self.assertEqual(manifest['Tagged::ABCDEF12-0000-0000-0000-000000000000']['status'], 'done')
        self.assertIn('briefPath', out['reviewed'][0])

    def test_failure_stops_after_three_attempts(self):
        manifest, out = self._run_batch(
            [self._asset()], attempts=3, review={'ok': False, 'note': 'boom'})
        key = 'Tagged::ABCDEF12-0000-0000-0000-000000000000'
        self.assertEqual(manifest[key]['status'], 'error')
        self.assertEqual(manifest[key]['attempts'], 3)
        self.assertFalse(serve._photo_needs_review(manifest, key))
