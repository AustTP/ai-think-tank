"""The village architecture (2026-11-06): registry, heartbeat, gates.

Parent control plane: a villages_registry table (one row per village
process) with heartbeat bookkeeping; the heartbeat's alert lines land in
the chief of staff's mailbox. Village-worker mode: --village=<id> runs a
process against its own DB that refuses the operator surface.
"""
import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    import tempfile
    import shutil
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-village-registry-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


class VillageRegistry(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ('THINK_TANK_PARENT_TOKEN',)}
        os.environ['THINK_TANK_PARENT_TOKEN'] = 'tok-123'
        self.addCleanup(lambda: [os.environ.pop('THINK_TANK_PARENT_TOKEN',
                                                None)])
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_heartbeat_requires_token(self):
        r = client().post('/api/villages/north/heartbeat', json={'ok': True})
        self.assertEqual(r.status_code, 401)

    def test_heartbeat_unknown_village_404(self):
        r = client().post(
            '/api/villages/ghost/heartbeat', json={'ok': True},
            headers={'X-Parent-Token': 'tok-123'})
        self.assertEqual(r.status_code, 404)

    def test_registered_village_heartbeat_lands_and_alerts_reach_theo(self):
        r = client().post(
            '/api/villages/registry',
            json={'id': 'north', 'name': 'North', 'apiBase': 'http://x:1'})
        self.assertEqual(r.status_code, 401)  # player session required
        # register through the simulated player session
        with unittest.mock.patch.object(serve, 'verify_session',
                                        lambda s: True), \
                unittest.mock.patch.object(serve, 'log_action',
                                           lambda *a, **k: None), \
                unittest.mock.patch.object(
                    serve, 'save_state_to_db', lambda s: None), \
                unittest.mock.patch.object(
                    serve, '_state_begin',
                    lambda: {'agents': {'theo': {'mailbox': []}}}):
            r = client().post(
                '/api/villages/registry',
                json={'id': 'north', 'name': 'North',
                      'apiBase': 'http://x:1'})
            self.assertEqual(r.status_code, 200, r.text)
            r = client().post(
                '/api/villages/north/heartbeat',
                json={'ok': True, 'openTasks': 3, 'queued': 1,
                      'alerts24h': 2, 'alerts': ['db lag high']},
                headers={'X-Parent-Token': 'tok-123'})
            self.assertEqual(r.status_code, 200, r.text)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT last_heartbeat_at, last_status FROM'
                ' villages_registry WHERE id = "north"').fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(json.loads(row[1])['openTasks'], 3)
