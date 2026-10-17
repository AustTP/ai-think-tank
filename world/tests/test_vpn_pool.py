"""On-demand VPN exit pool (world/vpn_pool.py).

Covers port allocation and reuse, reserved static-exit ports, the
Mullvad device cap, docker argv construction, failure handling,
and the registry shape that keeps GET /api/vpn-exits and
browse_page's `exit` parameter working for pool-spawned exits.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vpn_location  # noqa: E402
import vpn_pool  # noqa: E402

ACCOUNT = '1234567890123456'


class FakeDocker:
    """Records argv and hands back ids; failures are toggleable."""

    def __init__(self, fail_run=False, fail_rm=False):
        self.run_argv = []
        self.rm_argv = []
        self.fail_run = fail_run
        self.fail_rm = fail_rm
        self.containers = 0

    def run(self, argv):
        self.run_argv.append(list(argv))
        if self.fail_run:
            return None
        self.containers += 1
        return f'ctr-{self.containers:04d}'

    def rm(self, argv):
        self.rm_argv.append(list(argv))
        return not self.fail_rm


class TestVpnExitPool(unittest.TestCase):

    def pool(self, **kwargs):
        fail_run = kwargs.pop('fail_run', False)
        fail_rm = kwargs.pop('fail_rm', False)
        docker = FakeDocker(fail_run=fail_run, fail_rm=fail_rm)
        kwargs.setdefault('docker_run', docker.run)
        kwargs.setdefault('docker_rm', docker.rm)
        kwargs.setdefault('account_number', ACCOUNT)
        return vpn_pool.VpnExitPool(**kwargs), docker

    def test_start_allocates_sequential_ports(self):
        pool, _ = self.pool()
        first, error = pool.start('us-nyc-wg-001')
        self.assertIsNone(error)
        self.assertEqual(first.proxy_port, 18803)
        self.assertEqual(first.proxy, 'http://127.0.0.1:18803')
        second, error = pool.start('se-sto-wg-001')
        self.assertIsNone(error)
        self.assertEqual(second.proxy_port, 18804)

    def test_start_skips_reserved_ports(self):
        pool, _ = self.pool(reserved_ports=(18803, 18804))
        exit_record, error = pool.start('us-nyc-wg-001')
        self.assertIsNone(error)
        self.assertEqual(exit_record.proxy_port, 18805)

    def test_start_reuses_released_port(self):
        pool, _ = self.pool()
        first, _ = pool.start('us-nyc-wg-001')
        self.assertTrue(pool.stop('us-nyc-wg-001'))
        again, error = pool.start('us-nyc-wg-001')
        self.assertIsNone(error)
        self.assertEqual(again.proxy_port, first.proxy_port)

    def test_start_refuses_at_device_cap(self):
        pool, _ = self.pool(max_exits=2)
        pool.start('a-wg-001')
        pool.start('b-wg-001')
        third, error = pool.start('c-wg-001')
        self.assertIsNone(third)
        self.assertIn('device cap', error)
        self.assertEqual(pool.running(), 2)

    def test_start_docker_failure_leaves_port_free(self):
        pool, docker = self.pool(fail_run=True)
        record, error = pool.start('us-nyc-wg-001')
        self.assertIsNone(record)
        self.assertIn('docker run failed', error)
        self.assertEqual(pool.running(), 0)
        docker.fail_run = False
        retry, error = pool.start('us-nyc-wg-001')
        self.assertIsNotNone(retry)
        self.assertIsNone(error)
        self.assertEqual(retry.proxy_port, 18803)

    def test_start_requires_account(self):
        pool, _ = self.pool(account_number='')
        record, error = pool.start('us-nyc-wg-001')
        self.assertIsNone(record)
        self.assertEqual(error, 'Mullvad account required')

    def test_name_defaults_to_relay(self):
        pool, _ = self.pool()
        record, error = pool.start('us-nyc-wg-001')
        self.assertIsNone(error)
        self.assertEqual(record.name, 'us-nyc-wg-001')
        self.assertEqual(pool.names(), ['us-nyc-wg-001'])

    def test_explicit_name_honored_and_duplicate_refused(self):
        pool, _ = self.pool()
        record, error = pool.start('us-nyc-wg-001', name='nyc')
        self.assertIsNone(error)
        self.assertEqual(record.name, 'nyc')
        duplicate, error = pool.start('se-sto-wg-001', name='nyc')
        self.assertIsNone(duplicate)
        self.assertIn('already running', error)

    def test_invalid_name_refused(self):
        pool, _ = self.pool()
        record, error = pool.start('us-nyc-wg-001', name='bad name')
        self.assertIsNone(record)
        self.assertIn('invalid exit name', error)

    def test_empty_relay_refused(self):
        pool, _ = self.pool()
        record, error = pool.start('   ')
        self.assertIsNone(record)
        self.assertEqual(error, 'relay required')

    def test_same_relay_two_names_allowed(self):
        pool, _ = self.pool()
        one, error = pool.start('us-nyc-wg-001', name='nyc-a')
        self.assertIsNone(error)
        two, error = pool.start('us-nyc-wg-001', name='nyc-b')
        self.assertIsNone(error)
        self.assertEqual(pool.running(), 2)

    def test_docker_argv(self):
        pool, docker = self.pool()
        pool.start('us-nyc-wg-001', name='nyc')
        argv = docker.run_argv[0]
        self.assertEqual(argv[0], 'docker')
        self.assertIn('run', argv)
        self.assertIn('-d', argv)
        self.assertIn('--name', argv)
        self.assertIn('nyc', argv)
        self.assertIn('--privileged', argv)
        self.assertIn('MULLVAD_ACCOUNT=' + ACCOUNT, argv)
        self.assertIn('RELAY_HOSTNAME=us-nyc-wg-001', argv)
        self.assertIn('18803:8888', argv)
        self.assertIn('vpn-exit-nyc:/etc/wireguard', argv)
        self.assertIn('vpn-sandbox', argv)

    def test_container_id_recorded(self):
        pool, _ = self.pool()
        record, error = pool.start('us-nyc-wg-001')
        self.assertIsNone(error)
        self.assertEqual(record.container_id, 'ctr-0001')
        self.assertEqual(record.state, 'running')

    def test_stop_unknown_name(self):
        pool, _ = self.pool()
        self.assertFalse(pool.stop('nope'))

    def test_stop_removes_exit_and_frees_port(self):
        pool, docker = self.pool()
        pool.start('us-nyc-wg-001')
        self.assertTrue(pool.stop('us-nyc-wg-001'))
        self.assertEqual(pool.running(), 0)
        self.assertEqual(pool.names(), [])
        self.assertEqual(docker.rm_argv[0],
                         ['docker', 'rm', '-f', 'us-nyc-wg-001'])

    def test_stop_docker_failure_keeps_exit(self):
        pool, docker = self.pool(fail_rm=True)
        pool.start('us-nyc-wg-001')
        self.assertFalse(pool.stop('us-nyc-wg-001'))
        self.assertEqual(pool.running(), 1)
        self.assertIn('us-nyc-wg-001', pool.names())

    def test_stop_all(self):
        pool, _ = self.pool()
        pool.start('a-wg-001')
        pool.start('b-wg-001')
        self.assertEqual(pool.stop_all(), 2)
        self.assertEqual(pool.running(), 0)

    def test_exits_registry_shape_matches_static_exits(self):
        pool, _ = self.pool()
        pool.start('us-nyc-wg-001', name='nyc')
        registry = pool.exits()
        self.assertEqual(registry['nyc']['relay'], 'us-nyc-wg-001')
        described = vpn_location.describe_exit('nyc', registry)
        self.assertEqual(described['relay'], 'us-nyc-wg-001')
        self.assertEqual(described['proxy'], 'http://127.0.0.1:18803')
        # refresh_exit reads the proxy field: a dead proxy yields
        # an honest error rather than an exception
        with mock.patch.object(vpn_location, 'fetch_egress_ip',
                               return_value=None):
            record, error = vpn_location.refresh_exit('nyc', registry)
        self.assertIsNone(record)
        self.assertIn('no egress IP', error)


if __name__ == '__main__':
    unittest.main()
