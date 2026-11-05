"""Tests for backstage rooms (191) and the autosave-merge anchor (200).

Hermetic: state read/write are mocked, auth uses real agent-key rows.
Pins the owner directive: agents can enter a blank room by state
transition (no walk), never appear in rooms the player sees, are not
blocked by movement physics, and busy agents may not teleport. The
merge anchor keeps a server-written backstage presence alive across
client autosaves, and releases only through exit.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None
_KEY = 'backstage-test-key'
_STATE = {}


def _save(state):
    _STATE.clear()
    _STATE.update(state)


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-backstage-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        verify_session=lambda sess: False,
        check_rate_limit=lambda aid: True,
        get_state_from_db=lambda *a, **k: dict(_STATE),
        save_state_to_db=_save,
    )
    _PATCHER.start()
    serve.init_db()
    with serve._db() as conn:
        for agent, secret in (('marcus', _KEY), ('ruth', 'ruth-key'),
                              ('clara', 'clara-key')):
            conn.execute('INSERT INTO agent_keys (agent_id, secret_key, '
                         'created_at) VALUES (?, ?, ?)',
                         (agent, secret, 0.0))
    _STATE.update({'agents': {
        'marcus': {'id': 'marcus', 'name': 'Marcus', 'inRoom': None},
        'clara': {'id': 'clara', 'name': 'Clara', 'inRoom': None,
                  'task': 'task-1', 'busy': True},
        'ruth': {'id': 'ruth', 'name': 'Ruth', 'inRoom': 'library',
                 'path': [[1, 2], [3, 4]]},
    }})


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


class BackstageEnterExit(unittest.TestCase):
    def test_requires_auth(self):
        r = client().post('/api/backstage/enter', json={})
        self.assertEqual(r.status_code, 401)
        r = client().get('/api/backstage/who')
        self.assertEqual(r.status_code, 401)

    def test_enter_is_a_teleport_not_a_walk(self):
        r = client().post('/api/backstage/enter', json={},
                          headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 200)
        rec = _STATE['agents']['marcus']
        self.assertEqual(rec['inRoom'], 'backstage')
        self.assertNotIn('path', {k: v for k, v in rec.items() if v})

    def test_enter_carryover_walk_state_cleared(self):
        r = client().post('/api/backstage/enter', json={},
                          headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 200)
        # Now ruth: she is mid-walk (path present, not busy on a task).
        def self_as_ruth(key):
            return 'ruth' if key == 'ruth-key' else None
        with unittest.mock.patch.object(serve, '_agent_id_for_key',
                                        self_as_ruth), \
                unittest.mock.patch.object(serve, 'verify_agent_key',
                                           lambda a, k: k == 'ruth-key'):
            r2 = client().post('/api/backstage/enter', json={},
                               headers={'X-Agent-Key': 'ruth-key'})
        self.assertEqual(r2.status_code, 200)
        rec = _STATE['agents']['ruth']
        self.assertEqual(rec['inRoom'], 'backstage')
        self.assertIsNone(rec.get('path'))

    def test_busy_agent_refused(self):
        # marcus is backstage from the previous test; try clara (busy).
        def self_as_clara(key):
            return 'clara' if key == 'clara-key' else None
        with unittest.mock.patch.object(serve, '_agent_id_for_key',
                                        self_as_clara), \
                unittest.mock.patch.object(serve, 'verify_agent_key',
                                           lambda a, k: k == 'clara-key'):
            r = client().post('/api/backstage/enter', json={},
                              headers={'X-Agent-Key': 'clara-key'})
        self.assertEqual(r.status_code, 409)
        self.assertIn('mid-task', r.json()['error'])
        self.assertEqual(_STATE['agents']['clara']['inRoom'], None)

    def test_exit_releases(self):
        r = client().post('/api/backstage/exit', json={},
                          headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(_STATE['agents']['marcus']['inRoom'])

    def test_exit_when_not_backstage_is_clean(self):
        r = client().post('/api/backstage/exit', json={},
                          headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get('note'))

    def test_who_lists_only_backstage(self):
        # ruth was left backstage by an earlier test in this class; the
        # contract is: exactly the agents whose inRoom is backstage.
        _STATE['agents']['ruth']['inRoom'] = None
        _STATE['agents']['marcus']['inRoom'] = 'backstage'
        try:
            r = client().get('/api/backstage/who',
                             headers={'X-Agent-Key': _KEY})
            self.assertEqual(r.status_code, 200)
            ids = [a['id'] for a in r.json()['agents']]
            self.assertEqual(ids, ['marcus'])
        finally:
            _STATE['agents']['marcus']['inRoom'] = None


class AgentsBeyondBoundaries(unittest.TestCase):
    """Owner directive 2026-11-05: agents do not have the player's physical
    boundaries. The room-tied tool gates (curl/weatherstation, sandbox
    download, sandbox save) answer True for any agent from anywhere; the
    security layers above them (Jev, allowlist, caps, temp-access) are
    unchanged and tested at their own level."""

    def test_weatherstation_gate_lifted_for_agents(self):
        self.assertTrue(serve._agent_is_in_weatherstation('marcus', None))
        self.assertTrue(serve._agent_is_in_weatherstation(
            'marcus', 'library'))

    def test_sandbox_room_gate_lifted_for_agents(self):
        self.assertTrue(serve._agent_is_in_sandbox_room('marcus', None))
        self.assertTrue(serve._agent_is_in_sandbox_room('ruth', 'bank'))

    def test_player_still_passes_without_room(self):
        self.assertTrue(serve._agent_is_in_weatherstation('player', None))
        self.assertTrue(serve._agent_is_in_sandbox_room('player', None))


class BackstageMergeAnchor(unittest.TestCase):
    def test_server_backstage_anchors_over_client_stale(self):
        server = {'id': 'marcus', 'inRoom': 'backstage', 'x': 5, 'y': 6}
        client_agent = {'id': 'marcus', 'inRoom': 'library', 'x': 1}
        out = serve._merge_server_owned_agent(server, client_agent)
        self.assertEqual(out['inRoom'], 'backstage')
        self.assertEqual(out['x'], 5)  # spatial ownership unchanged

    def test_non_backstage_inroom_stays_client_owned(self):
        server = {'id': 'marcus', 'inRoom': None, 'x': 5}
        client_agent = {'id': 'marcus', 'inRoom': 'library', 'x': 1}
        out = serve._merge_server_owned_agent(server, client_agent)
        self.assertEqual(out['inRoom'], 'library')

    def test_client_confirming_backstage_stays(self):
        server = {'id': 'marcus', 'inRoom': 'backstage'}
        client_agent = {'id': 'marcus', 'inRoom': 'backstage'}
        out = serve._merge_server_owned_agent(server, client_agent)
        self.assertEqual(out['inRoom'], 'backstage')


if __name__ == '__main__':
    unittest.main()
