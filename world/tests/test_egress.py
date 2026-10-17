"""The always-on VPN egress planner (world/egress.py):
EgressRoute.container_proxy's host rewrite for use
from inside a container, and ContainerEgress.route_for's
round-robin over exit positions with the recorded
direct fallback when the registry is empty.

Pure logic over a positions dict -- no docker, no
serve import, so every test is hermetic.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import egress  # noqa: E402


class _Positions(object):
    """A positions callable the test can re-point,
    mimicking serve's _vpn_exit_registry lookups."""

    def __init__(self, positions=None):
        self.positions = positions or {}

    def __call__(self):
        return self.positions


class ContainerProxy(unittest.TestCase):
    def test_vpn_proxy_rewrites_loopback(self):
        route = egress.EgressRoute(
            egress.VPN, exit_name='exit-a',
            proxy='http://127.0.0.1:18801')
        self.assertEqual(
            route.container_proxy(),
            'http://host.docker.internal:18801')

    def test_vpn_proxy_without_port(self):
        route = egress.EgressRoute(
            egress.VPN, proxy='socks5://127.0.0.1')
        self.assertEqual(
            route.container_proxy(),
            'socks5://host.docker.internal')

    def test_vpn_without_proxy_is_none(self):
        route = egress.EgressRoute(egress.VPN,
                                   exit_name='exit-a')
        self.assertIsNone(route.container_proxy())

    def test_direct_route_is_never_proxied(self):
        route = egress.EgressRoute(
            egress.DIRECT,
            proxy='http://127.0.0.1:18801',
            reason=egress.FALLBACK_REASON)
        self.assertIsNone(route.container_proxy())


class RouteFor(unittest.TestCase):
    def test_empty_registry_falls_back_to_direct(self):
        planner = egress.ContainerEgress(
            _Positions({}))
        route = planner.route_for()
        self.assertEqual(route.kind, egress.DIRECT)
        self.assertEqual(route.reason,
                         egress.FALLBACK_REASON)
        self.assertIsNone(route.exit_name)
        self.assertIsNone(route.proxy)

    def test_none_registry_falls_back_to_direct(self):
        planner = egress.ContainerEgress(
            _Positions(None))
        route = planner.route_for()
        self.assertEqual(route.kind, egress.DIRECT)
        self.assertEqual(route.reason,
                         egress.FALLBACK_REASON)

    def test_single_exit_is_vpn(self):
        planner = egress.ContainerEgress(_Positions({
            'exit-a': {
                'proxy': 'http://127.0.0.1:18801',
                'proxyPort': 18801,
                'relay': 'se-relay-01',
            },
        }))
        route = planner.route_for()
        self.assertEqual(route.kind, egress.VPN)
        self.assertEqual(route.exit_name, 'exit-a')
        self.assertEqual(route.proxy,
                         'http://127.0.0.1:18801')
        self.assertEqual(route.proxy_port, 18801)
        self.assertEqual(route.relay, 'se-relay-01')
        self.assertEqual(route.reason, 'via exit exit-a')

    def test_round_robin_is_sorted_and_stable(self):
        planner = egress.ContainerEgress(_Positions({
            'exit-c': {'proxy': 'http://127.0.0.1:1'},
            'exit-a': {'proxy': 'http://127.0.0.1:2'},
            'exit-b': {'proxy': 'http://127.0.0.1:3'},
        }))
        names = [planner.route_for().exit_name
                 for _ in range(6)]
        self.assertEqual(names, ['exit-a', 'exit-b',
                                 'exit-c', 'exit-a',
                                 'exit-b', 'exit-c'])

    def test_proxy_port_derived_from_url(self):
        planner = egress.ContainerEgress(_Positions({
            'exit-a': {
                'proxy': 'http://127.0.0.1:18801',
            },
        }))
        route = planner.route_for()
        self.assertEqual(route.proxy_port, 18801)

    def test_explicit_proxy_port_wins(self):
        planner = egress.ContainerEgress(_Positions({
            'exit-a': {
                'proxy': 'http://127.0.0.1:18801',
                'proxyPort': 9999,
            },
        }))
        route = planner.route_for()
        self.assertEqual(route.proxy_port, 9999)

    def test_planner_is_stateless_across_registries(self):
        positions = _Positions(
            {'exit-a': {'proxy': 'http://127.0.0.1:1'}})
        planner = egress.ContainerEgress(positions)
        self.assertEqual(planner.route_for().exit_name,
                         'exit-a')
        # An exit that stopped existing is never handed
        # out: the next plan sees the empty registry.
        positions.positions = {}
        route = planner.route_for()
        self.assertEqual(route.kind, egress.DIRECT)
        self.assertEqual(route.reason,
                         egress.FALLBACK_REASON)


if __name__ == '__main__':
    unittest.main()
