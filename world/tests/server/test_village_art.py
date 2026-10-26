"""Village art: quote, exact geometry, dry-run, and the director card.

Hermetic: DB and ROOT redirected into a temp copy; PixelLab is mocked.
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_REAL_WORLD = os.path.join(_REPO, 'world')
_TMP = None
_PATCHER = None
_ONE_PX = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8'
           '/5+hHgAHggJ/PzWvWQAAAABJRU5ErkJggg==')


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='tt-village-art-')
    os.makedirs(os.path.join(_TMP, 'world', 'assets'))
    for name in ('collision_grid.json', 'door_triggers.json'):
        shutil.copy(os.path.join(_REAL_WORLD, name), os.path.join(_TMP, 'world', name))
    shutil.copy(os.path.join(_REAL_WORLD, 'assets', 'think_tank_background.png'),
                os.path.join(_TMP, 'world', 'assets'))
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP, 'test.db'),
        THINK_TANK_DIR=_TMP,
        AGENTS_DIR=os.path.join(_TMP, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP, 'library'),
        PASSPORT_PATH=os.path.join(_TMP, 'library', '.passport.json'),
        ROOT=os.path.join(_TMP, 'world'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _state():
    return {
        'agents': {'theo': {'id': 'theo', 'name': 'Theo', 'busy': False, 'offDuty': False},
                   'nora': {'id': 'nora', 'name': 'Nora', 'busy': False, 'offDuty': False},
                   'ruth': {'id': 'ruth', 'name': 'Ruth', 'busy': False, 'offDuty': False}},
        'agentRoster': [{'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
                        {'id': 'nora', 'name': 'Nora', 'isDirector': True},
                        {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'}],
        'teams': [{'id': 'nora', 'directorId': 'nora', 'members': ['ruth']}],
        'tasks': {}, 'workQueue': [], 'backlogRequests': [], 'sim': {},
    }


class VillageArt(unittest.TestCase):
    def setUp(self):
        serve.save_state_to_db(_state())
        self.world = serve.ROOT
        for name in ('assets/think_tank_south.png', 'collision_grid_south.json',
                     'door_triggers_south.json', 'scenes.json'):
            path = os.path.join(self.world, name)
            if os.path.exists(path):
                os.remove(path)

    def _quote_patches(self):
        return (
            unittest.mock.patch.object(serve, '_pixellab_credits_remaining', return_value=6.44),
            unittest.mock.patch.object(serve, '_spend_cap_month_view',
                                       return_value={'cap': 50.0, 'usage': 20.0, 'exceeded': False}),
        )

    def test_quote_only_returns_the_endpoint_price(self):
        p1, p2 = self._quote_patches()
        with p1, p2:
            r = serve.generate_village_map('theo', 'south', 'warm stone', quote_only=True)
        self.assertTrue(r['ok'])
        self.assertTrue(r['quoteOnly'])
        self.assertAlmostEqual(r['quote']['estimate']['usd'], 0.0132)
        self.assertFalse(os.path.exists(os.path.join(self.world, 'assets', 'think_tank_south.png')))

    def test_dry_run_writes_geometry_and_scene_without_spend(self):
        p1, p2 = self._quote_patches()
        with p1, p2, unittest.mock.patch.object(serve, '_pixellab_call') as call:
            r = serve.generate_village_map('theo', 'south', 'sunlit', dry_run=True)
        self.assertTrue(r['ok'], r)
        call.assert_not_called()
        self.assertEqual(r['costUsd'], 0.0)
        with open(os.path.join(self.world, 'collision_grid_south.json')) as fh:
            grid = json.load(fh)
        with open(os.path.join(self.world, 'door_triggers_south.json')) as fh:
            doors = json.load(fh)
        self.assertEqual((grid['cols'], grid['rows']), (86, 48))
        self.assertNotIn('south_outlet', doors)
        self.assertIn('pressoffice', doors)
        with open(os.path.join(self.world, 'scenes.json')) as fh:
            scenes = json.load(fh)
        self.assertEqual(scenes['south']['doorsSprite'], '/door_triggers_south.json')

    def test_paid_generation_saves_a_native_png_and_charges_the_real_cost(self):
        p1, p2 = self._quote_patches()
        with p1, p2, \
             unittest.mock.patch.object(serve, '_pixellab_call',
                                        return_value=({'image': {'base64': _ONE_PX},
                                                       'usage': {'usd': 0.0121}}, None)), \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue:
            r = serve.generate_village_map('theo', 'south', 'sunlit', force=True)
        self.assertTrue(r['ok'], r)
        self.assertAlmostEqual(r['costUsd'], 0.0121)
        accrue.assert_called_once_with('pixellab', 0.0121, agent_id='theo')
        from PIL import Image
        with Image.open(os.path.join(self.world, 'assets', 'think_tank_south.png')) as img:
            self.assertEqual(img.size, (688, 384))

    def test_cap_too_low_refuses_before_any_call(self):
        p1, p2 = self._quote_patches()
        with p1, p2, unittest.mock.patch.object(serve, '_pixellab_call') as call:
            r = serve.generate_village_map('theo', 'south', 'x', max_usd=0.001)
        self.assertFalse(r['ok'])
        self.assertIn('exceeds', r['error'])
        call.assert_not_called()

    def test_non_director_is_refused(self):
        r = serve.generate_village_map('ruth', 'south', 'x', quote_only=True)
        self.assertFalse(r['ok'])
        self.assertIn('director', r['error'])

    def test_main_and_winter_are_refused(self):
        for name in ('main', 'winter'):
            r = serve.generate_village_map('theo', name, 'x', quote_only=True)
            self.assertFalse(r['ok'])
            self.assertIn('built-in', r['error'])


class VillageArtEndpoint(unittest.TestCase):
    def setUp(self):
        serve.save_state_to_db(_state())
        self.key = serve.get_or_create_agent_key('theo')
        self.client = TestClient(serve.app)

    def _headers(self):
        return {'X-Agent-Key': self.key}

    def test_post_queues_a_supervise_card_for_the_team_director(self):
        p1, p2 = VillageArt._quote_patches(self)
        with unittest.mock.patch.object(serve, '_operator_gate', return_value=(True, 'theo')), p1, p2:
            r = self.client.post('/api/villages/south/art', headers=self._headers(),
                                 json={'theme': 'sunlit', 'teamId': 'nora', 'maxUsd': 0.05})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['queued'])
        self.assertEqual(body['assignedTo'], 'nora')
        state = serve.get_state_from_db()
        card = state['workQueue'][0]
        self.assertEqual(card['taskType'], 'supervise')
        self.assertEqual(card['assignedTo'], 'nora')
        self.assertIn('generate_village_map', card['instructions'])

    def test_sync_generation_runs_inline(self):
        p1, p2 = VillageArt._quote_patches(self)
        with unittest.mock.patch.object(serve, '_operator_gate', return_value=(True, 'theo')), \
             p1, p2, unittest.mock.patch.object(serve, '_pixellab_call',
                                                return_value=({'image': {'base64': _ONE_PX},
                                                               'usage': {'usd': 0.0121}}, None)), \
             unittest.mock.patch.object(serve, '_accrue_spend'):
            r = self.client.post('/api/villages/south/art', headers=self._headers(),
                                 json={'theme': 'sunlit', 'sync': True, 'force': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])


if __name__ == '__main__':
    unittest.main()
