"""Automated tests for the per-agent avatar generation feature.

Covers the pure helpers (_avatar_sex, _avatar_description,
_avatar_orientation_name, _pixellab_avatar_available), the generator's
availability gate + success path (_ensure_agent_avatar), and the
/api/avatar/<id>/<orientation>.png serving route.

Same hermetic pattern as test_serve.py: a module-level patch redirects
AGENTS_DIR/DB to a temp dir so nothing touches a real agent directory or a
live DB.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-avatar-test-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class AvatarHelpers(unittest.TestCase):
    def test_avatar_sex_by_first_name(self):
        self.assertEqual(serve._avatar_sex('Marcus'), 'male')
        self.assertEqual(serve._avatar_sex('Priya'), 'female')
        self.assertEqual(serve._avatar_sex('Theo'), 'male')
        self.assertEqual(serve._avatar_sex('Nora'), 'female')
        self.assertEqual(serve._avatar_sex('Xylophone'), 'neutral')

    def test_avatar_description_role_clothing_and_gender(self):
        director = serve._avatar_description('Theo', 'Director', is_director=True)
        self.assertIn('male', director)
        self.assertIn('suit', director)
        worker = serve._avatar_description('Priya', 'Analyst', is_director=False)
        self.assertIn('female', worker)
        self.assertIn('vest', worker)
        self.assertNotIn('suit', worker)

    def test_avatar_orientation_name_maps_aliases(self):
        self.assertEqual(serve._avatar_orientation_name('front'), 'south')
        self.assertEqual(serve._avatar_orientation_name('south'), 'south')
        self.assertEqual(serve._avatar_orientation_name('back'), 'north')
        self.assertEqual(serve._avatar_orientation_name('side_flip'), 'west')
        self.assertEqual(serve._avatar_orientation_name('side'), 'east')
        self.assertEqual(serve._avatar_orientation_name('bogus key'), 'bogus_key')

    def test_avatar_available_false_when_not_configured(self):
        # No pixellab credential in the test vault -> fails closed.
        self.assertFalse(serve._pixellab_avatar_available())


class AvatarGeneration(unittest.TestCase):
    def test_ensure_skips_when_unavailable(self):
        agent_dir = os.path.join(serve.AGENTS_DIR, 'marcus')
        with unittest.mock.patch.object(serve, '_pixellab_avatar_available', return_value=False):
            result = serve._ensure_agent_avatar('marcus', 'Marcus', 'Analyst', is_director=False)
        self.assertFalse(result)
        self.assertFalse(os.path.isdir(agent_dir), 'must not write avatars when PixelLab unavailable')

    def test_ensure_writes_rotations_on_success(self):
        fake_char = {
            'rotation_urls': {
                'front': 'https://cdn.example/front.png',
                'back': 'https://cdn.example/back.png',
                'side': 'https://cdn.example/side.png',
                'side_flip': 'https://cdn.example/side_flip.png',
            }
        }
        png = b'\x89PNG\r\n\x1a\nfakepixels'

        class FakeResp:
            def read(self):
                return png
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=30):  # noqa: ARG001
            return FakeResp()

        with unittest.mock.patch.object(serve, '_pixellab_avatar_available', return_value=True), \
             unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': True, 'data': fake_char, 'ids': {'character_id': 'c1'}}), \
             unittest.mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            result = serve._ensure_agent_avatar('priya', 'Priya', 'Analyst', is_director=False)
        self.assertTrue(result)
        av = os.path.join(serve.AGENTS_DIR, 'priya', 'avatars')
        self.assertTrue(os.path.isdir(av))
        for orient in ('south', 'north', 'east', 'west'):
            self.assertEqual(os.path.getsize(os.path.join(av, orient + '.png')), len(png))

    def test_ensure_returns_false_on_api_failure(self):
        with unittest.mock.patch.object(serve, '_pixellab_avatar_available', return_value=True), \
             unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': False, 'reason': 'credential unavailable'}):
            result = serve._ensure_agent_avatar('omar', 'Omar', 'Analyst', is_director=False)
        self.assertFalse(result)


class AvatarRoute(unittest.TestCase):
    def test_route_serves_written_orientation(self):
        av = os.path.join(serve.AGENTS_DIR, 'nadia', 'avatars')
        os.makedirs(av, exist_ok=True)
        with open(os.path.join(av, 'south.png'), 'wb') as f:
            f.write(b'\x89PNG\x00\x00\x00\x00')
        client = TestClient(serve.app)
        r = client.get('/api/avatar/nadia/south.png')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get('content-type'), 'image/png')

    def test_route_404_for_missing_and_unknown_agent(self):
        client = TestClient(serve.app)
        self.assertEqual(client.get('/api/avatar/nobody/south.png').status_code, 404)
        self.assertEqual(client.get('/api/avatar/nadia/east.png').status_code, 404)

    def test_route_404_for_non_orientation_filename(self):
        client = TestClient(serve.app)
        self.assertEqual(client.get('/api/avatar/nadia/secret.png').status_code, 404)
        self.assertEqual(client.get('/api/avatar/nadia/..%2Fagent.json').status_code, 404)


if __name__ == '__main__':
    unittest.main()
