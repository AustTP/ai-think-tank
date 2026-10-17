"""Agent-authenticated container broker endpoints (ask 8).

containers.py is pure and already tested on its own. These
tests cover the WIRING in serve: the /api/containers routes
that let an agent (authenticated by its own X-Agent-Key) list
its containers and provision new ones through the in-memory
broker -- the policy boundary that bounds how many containers
run at once (a global cap and a per-agent cap), which images
may be provisioned, and which egress profiles a provision may
name. The broker is patched with a fresh instance per test so
no docker is ever run.

DB-isolated (setUpModule below, the test_vpn_exit_endpoints
pattern) since every route calls log_action, a real DB write.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import containers  # noqa: E402
import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-containers-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _client():
    from starlette.testclient import TestClient
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return c


def _broker(**kwargs):
    options = {
        'max_containers': 8,
        'approved_images': ['python:3.12-slim', 'alpine:3.20'],
        'max_per_agent': 3,
        'egress_profiles': {'direct': [], 'vpn': []},
    }
    options.update(kwargs)
    return containers.ContainerBroker(**options)


def _as_agent(agent_id='ada', broker=None, key_ok=True):
    patches = [
        unittest.mock.patch.object(
            serve, '_resolve_requester', return_value=agent_id),
        unittest.mock.patch.object(
            serve, 'verify_agent_key',
            return_value=True if key_ok else False),
    ]
    if broker is not None:
        patches.append(
            unittest.mock.patch.object(serve, '_CONTAINER_BROKER', broker))
    return patches


class ContainersList(unittest.TestCase):
    def test_an_unauthenticated_request_is_a_401(self):
        with unittest.mock.patch.object(
                serve, '_resolve_requester', return_value=None):
            response = _client().get('/api/containers')
        self.assertEqual(response.status_code, 401)

    def test_the_player_session_gets_no_containers(self):
        with unittest.mock.patch.object(
                serve, '_resolve_requester', return_value='player'):
            response = _client().get('/api/containers')
        self.assertEqual(response.status_code, 401)

    def test_a_key_mismatch_is_a_403(self):
        patches = _as_agent(key_ok=False)
        with patches[0], patches[1]:
            response = _client().get('/api/containers')
        self.assertEqual(response.status_code, 403)

    def test_list_carries_the_running_counts(self):
        broker = _broker()
        broker.provision('ada', 'python:3.12-slim')
        patches = _as_agent(broker=broker)
        with patches[0], patches[1], patches[2]:
            response = _client().get('/api/containers')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['containers']), 1)
        self.assertEqual(body['containers'][0]['image'],
                         'python:3.12-slim')
        self.assertEqual(body['running'], 1)
        self.assertEqual(body['runningForMe'], 1)


class ContainersCreate(unittest.TestCase):
    def _create(self, body, agent_id='ada', broker=None, key_ok=True):
        patches = _as_agent(agent_id=agent_id, broker=broker, key_ok=key_ok)
        with patches[0], patches[1], patches[2]:
            return _client().post('/api/containers', json=body)

    def test_provision_runs_through_the_broker(self):
        broker = _broker()
        response = self._create({'image': 'python:3.12-slim'},
                                broker=broker)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['container']['agentId'], 'ada')
        self.assertEqual(body['container']['image'], 'python:3.12-slim')
        self.assertEqual(body['running'], 1)
        self.assertEqual(broker.running(), 1)

    def test_an_unapproved_image_is_a_409(self):
        broker = _broker()
        response = self._create({'image': 'ubuntu:latest'}, broker=broker)
        self.assertEqual(response.status_code, 409)
        self.assertIn('image', response.json()['error'])
        self.assertEqual(broker.running(), 0)

    def test_the_per_agent_cap_is_a_409(self):
        broker = _broker(max_per_agent=1)
        first = self._create({'image': 'python:3.12-slim'}, broker=broker)
        self.assertEqual(first.status_code, 200)
        second = self._create({'image': 'alpine:3.20'}, broker=broker)
        self.assertEqual(second.status_code, 409)
        self.assertIn('per-agent', second.json()['error'])

    def test_the_global_cap_is_a_409(self):
        broker = _broker(max_containers=1)
        first = self._create({'image': 'python:3.12-slim'},
                             agent_id='ada', broker=broker)
        self.assertEqual(first.status_code, 200)
        second = self._create({'image': 'alpine:3.20'},
                              agent_id='bee', broker=broker)
        self.assertEqual(second.status_code, 409)

    def test_a_missing_image_is_a_400(self):
        response = self._create({}, broker=_broker())
        self.assertEqual(response.status_code, 400)


class ContainersLifecycle(unittest.TestCase):
    def _call(self, method, container_id, broker=None, agent_id='ada'):
        patches = _as_agent(agent_id=agent_id, broker=broker)
        with patches[0], patches[1], patches[2]:
            return _client().post(
                f'/api/containers/{container_id}/{method}')

    def test_shutdown_stops_an_own_container(self):
        broker = _broker()
        result = broker.provision('ada', 'python:3.12-slim')
        response = self._call('shutdown', result.container_id, broker=broker)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {'ok': True,
                          'containerId': result.container_id})
        self.assertEqual(broker.running(), 0)

    def test_shutdown_of_a_foreign_container_is_a_409(self):
        broker = _broker()
        result = broker.provision('bee', 'python:3.12-slim')
        response = self._call('shutdown', result.container_id, broker=broker)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(broker.running(), 1)

    def test_restart_restarts_an_own_container(self):
        broker = _broker()
        result = broker.provision('ada', 'python:3.12-slim')
        response = self._call('restart', result.container_id, broker=broker)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(broker.running(), 1)

    def test_restart_of_an_unknown_container_is_a_409(self):
        response = self._call('restart', 'ghost', broker=_broker())
        self.assertEqual(response.status_code, 409)

    def test_switch_of_an_unknown_container_is_a_409(self):
        response = self._call('switch', 'ghost', broker=_broker())
        self.assertEqual(response.status_code, 409)


if __name__ == '__main__':
    unittest.main()
