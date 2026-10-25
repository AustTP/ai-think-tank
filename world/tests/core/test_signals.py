"""Tests for the signal registry (ask 7)."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from signals import Scenario, SignalWaker, WakePlan  # noqa: E402


def matches(data, condition):
    return condition in data


class TestSignalWaker(unittest.TestCase):
    def setUp(self):
        self.waker = SignalWaker(evaluator=matches)
        self.plan = WakePlan(
            plan_id="plan-1",
            agent_id="trader-1",
            team="trading",
            subject="BTC",
            task="signals",
            scenarios=[
                Scenario(
                    scenario_id="add",
                    name="add on dip",
                    condition="price<60000",
                    action="add",
                    exit_strategy="sell at 65000",
                ),
                Scenario(
                    scenario_id="close",
                    name="close on spike",
                    condition="price>70000",
                    action="close_all",
                    exit_strategy="market sell",
                    rearm=True,
                ),
            ],
        )
        self.waker.arm(self.plan)

    def test_matching_scenario_fires(self):
        events = self.waker.evaluate("BTC", "price<60000")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.agent_id, "trader-1")
        self.assertEqual(event.team, "trading")
        self.assertEqual(event.scenario_id, "add")
        self.assertEqual(event.action, "add")
        self.assertEqual(event.exit_strategy, "sell at 65000")
        self.assertEqual(event.task, "signals")

    def test_other_subject_does_not_fire(self):
        events = self.waker.evaluate("ETH", "price<60000")
        self.assertEqual(events, [])

    def test_fired_scenario_disarms(self):
        self.waker.evaluate("BTC", "price<60000")
        events = self.waker.evaluate("BTC", "price<60000")
        self.assertEqual(events, [])

    def test_rearm_scenario_stays_armed(self):
        self.waker.evaluate("BTC", "price>70000")
        events = self.waker.evaluate("BTC", "price>70000")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].scenario_id, "close")

    def test_multiple_scenarios_fire(self):
        events = self.waker.evaluate(
            "BTC", "price<60000 and price>70000"
        )
        self.assertEqual(len(events), 2)

    def test_disarm_plan(self):
        self.waker.disarm("plan-1")
        events = self.waker.evaluate("BTC", "anything")
        self.assertEqual(events, [])

    def test_disarm_unknown_plan(self):
        self.assertFalse(self.waker.disarm("nope"))

    def test_wakeable_gates_wake(self):
        waker = SignalWaker(
            evaluator=matches,
            wakeable=lambda agent_id, work: (
                agent_id == "trader-1" and work == "signals"
            ),
        )
        waker.arm(self.plan)
        events = waker.evaluate("BTC", "price<60000")
        self.assertEqual(len(events), 1)
        self.plan.task = "cleanup"
        events = waker.evaluate("BTC", "price<60000")
        self.assertEqual(events, [])

    def test_active_when_gates_plan(self):
        self.plan.active_when = "positions>0"
        self.plan.scenarios[0].rearm = True
        events = self.waker.evaluate(
            "BTC", "price<60000 positions>0"
        )
        self.assertEqual(len(events), 1)
        events = self.waker.evaluate(
            "BTC", "price<60000 positions=0"
        )
        self.assertEqual(events, [])
        events = self.waker.evaluate(
            "BTC", "price<60000 positions>0"
        )
        self.assertEqual(len(events), 1)

    def test_active_when_keeps_scenario_armed(self):
        self.plan.active_when = "positions>0"
        self.waker.evaluate("BTC", "positions=0")
        events = self.waker.evaluate(
            "BTC", "price<60000 positions>0"
        )
        self.assertEqual(len(events), 1)


if __name__ == '__main__':
    unittest.main()
