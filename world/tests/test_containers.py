"""Tests for the container broker (ask 10)."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

import containers  # noqa: E402
import egress  # noqa: E402
from containers import ContainerBroker  # noqa: E402


class TestContainerBroker(unittest.TestCase):
    def setUp(self):
        self.broker = ContainerBroker(
            max_containers=3,
            approved_images=["python:3.12-slim", "node:20"],
            max_per_agent=2,
        )

    def test_provision_runs_container(self):
        result = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.container_id)
        self.assertEqual(self.broker.active("agent-1"), result.container_id)
        self.assertEqual(self.broker.running(), 1)

    def test_unapproved_image_denied(self):
        result = self.broker.provision("agent-1", "evil:latest")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "image not approved")

    def test_global_cap(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        second = self.broker.provision("agent-2", "python:3.12-slim")
        third = self.broker.provision("agent-3", "python:3.12-slim")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertTrue(third.ok)
        fourth = self.broker.provision("agent-4", "python:3.12-slim")
        self.assertFalse(fourth.ok)
        self.assertEqual(fourth.reason, "global container cap reached")

    def test_per_agent_cap(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        second = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        third = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertFalse(third.ok)
        self.assertEqual(third.reason, "per-agent container cap reached")

    def test_shutdown_frees_slot(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertTrue(self.broker.shutdown("agent-1", first.container_id))
        self.assertEqual(self.broker.running(), 0)
        self.assertIsNone(self.broker.active("agent-1"))
        again = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertTrue(again.ok)

    def test_shutdown_rejects_foreign_container(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertFalse(self.broker.shutdown("agent-2", first.container_id))

    def test_shutdown_unknown_container(self):
        self.assertFalse(self.broker.shutdown("agent-1", "ctr-nope"))

    def test_shutdown_twice(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertTrue(self.broker.shutdown("agent-1", first.container_id))
        self.assertFalse(self.broker.shutdown("agent-1", first.container_id))

    def test_switch_between_containers(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        second = self.broker.provision("agent-1", "node:20")
        self.assertTrue(self.broker.switch("agent-1", second.container_id))
        self.assertEqual(self.broker.active("agent-1"), second.container_id)
        self.assertTrue(self.broker.switch("agent-1", first.container_id))
        self.assertEqual(self.broker.active("agent-1"), first.container_id)

    def test_switch_rejects_stopped(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        self.broker.provision("agent-1", "node:20")
        self.broker.shutdown("agent-1", first.container_id)
        self.assertFalse(self.broker.switch("agent-1", first.container_id))

    def test_switch_rejects_foreign(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertFalse(self.broker.switch("agent-2", first.container_id))

    def test_restart_stopped_container(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        self.broker.shutdown("agent-1", first.container_id)
        result = self.broker.restart("agent-1", first.container_id)
        self.assertTrue(result.ok)
        self.assertEqual(self.broker.running(), 1)
        self.assertEqual(self.broker.active("agent-1"), first.container_id)

    def test_restart_respects_global_cap(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        self.broker.provision("agent-2", "python:3.12-slim")
        self.broker.provision("agent-3", "python:3.12-slim")
        self.broker.shutdown("agent-1", first.container_id)
        fourth = self.broker.provision("agent-4", "python:3.12-slim")
        self.assertTrue(fourth.ok)
        result = self.broker.restart("agent-1", first.container_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "global container cap reached")

    def test_restart_respects_per_agent_cap(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        self.broker.provision("agent-1", "node:20")
        self.broker.shutdown("agent-1", first.container_id)
        third = self.broker.provision("agent-1", "python:3.12-slim")
        self.assertTrue(third.ok)
        result = self.broker.restart("agent-1", first.container_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "per-agent container cap reached")

    def test_restart_unknown(self):
        result = self.broker.restart("agent-1", "ctr-nope")
        self.assertFalse(result.ok)

    def test_restart_already_running(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        result = self.broker.restart("agent-1", first.container_id)
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "already running")

    def test_containers_for(self):
        first = self.broker.provision("agent-1", "python:3.12-slim")
        second = self.broker.provision("agent-1", "node:20")
        self.assertEqual(
            self.broker.containers_for("agent-1"),
            [first.container_id, second.container_id],
        )

    def test_egress_profile(self):
        broker = ContainerBroker(
            max_containers=3,
            approved_images=["python:3.12-slim"],
            egress_profiles={
                "trading": ["api.exchange.com"],
                "vpn": ["vpn.mullvad.net"],
            },
        )
        result = broker.provision(
            "agent-1", "python:3.12-slim", egress="trading"
        )
        self.assertTrue(result.ok)
        container = broker._containers[result.container_id]
        self.assertEqual(container.egress, "trading")

    def test_unknown_egress_profile_denied(self):
        broker = ContainerBroker(
            max_containers=3,
            approved_images=["python:3.12-slim"],
            egress_profiles={"trading": ["api.exchange.com"]},
        )
        result = broker.provision(
            "agent-1", "python:3.12-slim", egress="vpn"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unknown egress profile")


class _ScriptedDriver(containers.ContainerDriver):
    """A driver with canned results, so the broker's
    runtime-failure paths run without docker."""

    def __init__(self, create=None, stop=True,
                 start=True):
        self._create = create
        self._stop = stop
        self._start = start
        self._n = 0

    def create(self, agent_id, image, egress_route):
        if self._create is not None:
            return self._create
        self._n += 1
        return "ctr-scripted-%d" % self._n, None

    def stop(self, container_id):
        return self._stop

    def start(self, container_id):
        return self._start


class _FakePlanner(object):
    """An egress planner that records every route ask."""

    def __init__(self):
        self.calls = 0
        self.route = egress.EgressRoute(
            egress.DIRECT, reason=egress.FALLBACK_REASON)

    def route_for(self):
        self.calls += 1
        return self.route


class TestContainerDriverBase(unittest.TestCase):
    def test_abstract_driver_methods_raise(self):
        driver = containers.ContainerDriver()
        with self.assertRaises(NotImplementedError):
            driver.create("ada", "python:3.12-slim", None)
        with self.assertRaises(NotImplementedError):
            driver.stop("ctr-1")
        with self.assertRaises(NotImplementedError):
            driver.start("ctr-1")


class TestBrokerRuntimeFailures(unittest.TestCase):
    def test_provision_driver_create_failure(self):
        broker = ContainerBroker(
            max_containers=3,
            approved_images=["python:3.12-slim"],
            driver=_ScriptedDriver(
                create=(None, "runtime offline")))
        result = broker.provision(
            "agent-1", "python:3.12-slim")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "runtime offline")
        self.assertEqual(broker.running(), 0)

    def test_shutdown_driver_stop_failure(self):
        broker = ContainerBroker(
            max_containers=3,
            approved_images=["python:3.12-slim"],
            driver=_ScriptedDriver(stop=False))
        first = broker.provision(
            "agent-1", "python:3.12-slim")
        self.assertTrue(first.ok)
        self.assertFalse(
            broker.shutdown("agent-1", first.container_id))
        # The stop never happened, so it still runs.
        self.assertEqual(broker.running(), 1)

    def test_restart_driver_start_failure(self):
        broker = ContainerBroker(
            max_containers=3,
            approved_images=["python:3.12-slim"],
            driver=_ScriptedDriver(start=False))
        first = broker.provision(
            "agent-1", "python:3.12-slim")
        broker.shutdown("agent-1", first.container_id)
        result = broker.restart(
            "agent-1", first.container_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason,
                         "driver start failed")
        self.assertEqual(broker.running(), 0)

    def test_planner_routes_every_provision(self):
        planner = _FakePlanner()
        broker = ContainerBroker(
            max_containers=3,
            approved_images=["python:3.12-slim"],
            egress_planner=planner,
            driver=_ScriptedDriver())
        result = broker.provision(
            "agent-1", "python:3.12-slim")
        self.assertTrue(result.ok)
        self.assertEqual(planner.calls, 1)
        container = broker._containers[result.container_id]
        self.assertIs(container.egress_route, planner.route)


if __name__ == '__main__':
    unittest.main()
