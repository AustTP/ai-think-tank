"""The docker SDK container driver
(world/containers.py, ask 9 wiring): a VPN
egress route becomes the container's proxy
environment (rewritten to host.docker.internal)
plus the extra_hosts host-gateway entry, a
direct or absent route stays clean, and a
runtime failure fails the provision instead of
raising. The docker client is faked, so no
daemon is needed.
"""

import os
import unittest
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import agents.containers as containers  # noqa: E402
import core.egress as egress  # noqa: E402


class _FakeContainer(object):
    def __init__(self, container_id):
        self.id = container_id
        self.stopped_with = None
        self.started = False

    def stop(self, timeout=None):
        self.stopped_with = timeout

    def start(self):
        self.started = True


class _FakeContainers(object):
    def __init__(self, fail_on_run=None):
        self.by_id = {}
        self.run_kwargs = None
        self.fail_on_run = fail_on_run

    def run(self, image, **kwargs):
        if self.fail_on_run is not None:
            raise self.fail_on_run
        self.run_image = image
        self.run_kwargs = kwargs
        container = _FakeContainer('cid-%s' % image)
        self.by_id[container.id] = container
        return container

    def get(self, container_id):
        if container_id not in self.by_id:
            raise KeyError(container_id)
        return self.by_id[container_id]


class _FakeClient(object):
    def __init__(self, fail_on_run=None):
        self.containers = _FakeContainers(
            fail_on_run=fail_on_run)


try:
    import docker  # noqa: F401 -- presence check only
    _DOCKER_SDK = True
except ImportError:
    # The docker SDK is optional runtime infra (the in-memory driver is the
    # default); these white-box tests of the DockerDriver mock it and cannot
    # run without it. CI has no docker SDK -- skip rather than fail.
    _DOCKER_SDK = False


@unittest.skipUnless(_DOCKER_SDK, "docker SDK not installed")
class DockerDriverCreate(unittest.TestCase):
    def setUp(self):
        self.client = _FakeClient()
        self.driver = containers.DockerDriver(
            client=self.client)

    def test_vpn_route_injects_proxy_environment(self):
        route = egress.EgressRoute(
            egress.VPN, exit_name='exit-a',
            proxy='http://127.0.0.1:18801')
        container_id, error = self.driver.create(
            'ada', 'python:3.11', route)
        self.assertIsNone(error)
        self.assertEqual(container_id,
                         'cid-python:3.11')
        kwargs = self.client.containers.run_kwargs
        self.assertEqual(
            kwargs['environment'],
            {'HTTP_PROXY':
             'http://host.docker.internal:18801',
             'HTTPS_PROXY':
             'http://host.docker.internal:18801'})
        self.assertEqual(kwargs['extra_hosts'],
                         {'host.docker.internal':
                          'host-gateway'})
        self.assertEqual(kwargs['labels'],
                         {'agent': 'ada'})
        self.assertEqual(
            kwargs['command'],
            containers.DockerDriver.SLEEP_COMMAND)
        self.assertTrue(kwargs['detach'])
        self.assertEqual(
            self.client.containers.run_image,
            'python:3.11')

    def test_direct_route_stays_clean(self):
        route = egress.EgressRoute(
            egress.DIRECT,
            reason=egress.FALLBACK_REASON)
        container_id, error = self.driver.create(
            'ada', 'python:3.11', route)
        self.assertIsNone(error)
        kwargs = self.client.containers.run_kwargs
        self.assertIsNone(kwargs['environment'])
        self.assertIsNone(kwargs['extra_hosts'])
        self.assertEqual(kwargs['labels'],
                         {'agent': 'ada'})

    def test_absent_route_stays_clean(self):
        container_id, error = self.driver.create(
            'ada', 'python:3.11', None)
        self.assertIsNone(error)
        kwargs = self.client.containers.run_kwargs
        self.assertIsNone(kwargs['environment'])
        self.assertIsNone(kwargs['extra_hosts'])

    def test_vpn_route_without_proxy_still_maps_host(
            self):
        route = egress.EgressRoute(egress.VPN)
        self.driver.create('ada', 'python:3.11',
                                route)
        kwargs = self.client.containers.run_kwargs
        self.assertIsNone(kwargs['environment'])
        self.assertEqual(kwargs['extra_hosts'],
                         {'host.docker.internal':
                          'host-gateway'})

    def test_runtime_failure_is_a_denial(self):
        driver = containers.DockerDriver(
            client=_FakeClient(
                fail_on_run=RuntimeError(
                    'no such image')))
        container_id, error = driver.create(
            'ada', 'python:3.11', None)
        self.assertIsNone(container_id)
        self.assertEqual(
            error,
            'docker create failed: no such image')


@unittest.skipUnless(_DOCKER_SDK, "docker SDK not installed")
class DockerDriverLifecycle(unittest.TestCase):
    def setUp(self):
        self.client = _FakeClient()
        self.driver = containers.DockerDriver(
            client=self.client)
        self.container_id, _ = self.driver.create(
            'ada', 'python:3.11', None)

    def test_stop_stops_with_timeout(self):
        self.assertTrue(self.driver.stop(
            self.container_id))
        container = self.client.containers.by_id[
            self.container_id]
        self.assertEqual(container.stopped_with, 10)

    def test_stop_unknown_id_is_false(self):
        self.assertFalse(self.driver.stop('gone'))

    def test_start_starts_a_stopped_container(self):
        self.assertTrue(self.driver.start(
            self.container_id))
        self.assertTrue(
            self.client.containers.by_id[
                self.container_id].started)

    def test_start_unknown_id_is_false(self):
        self.assertFalse(self.driver.start('gone'))

    def test_client_is_injected(self):
        self.assertIs(self.driver._connect(),
                      self.client)


if __name__ == '__main__':
    unittest.main()
