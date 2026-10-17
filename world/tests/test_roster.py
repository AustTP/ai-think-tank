"""Tests for the team and agent roster (ask 11)."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from roster import OFF_DUTY, ON_DUTY, Roster  # noqa: E402


class TestRoster(unittest.TestCase):
    def setUp(self):
        self.roster = Roster()
        self.roster.add_team("alpha")
        self.roster.add_team("beta")
        self.roster.add_agent("a1", "alpha", "worker")
        self.roster.add_agent("a2", "alpha", "worker")
        self.roster.add_agent("b1", "beta", "worker")

    def test_same_team_exception(self):
        self.roster.set_status("a1", OFF_DUTY)
        self.roster.mark_sprint_complete("a1")
        decision = self.roster.route_one_off("task-1", "alpha")
        self.assertEqual(decision.agent_id, "a1")
        self.assertTrue(decision.same_team)
        self.assertFalse(decision.requires_confirmation)

    def test_same_team_requires_sprint_complete(self):
        self.roster.set_status("a1", OFF_DUTY)
        self.roster.set_status("b1", OFF_DUTY)
        decision = self.roster.route_one_off("task-1", "alpha")
        self.assertEqual(decision.agent_id, "b1")
        self.assertFalse(decision.same_team)
        self.assertTrue(decision.requires_confirmation)

    def test_off_duty_other_team_needs_confirmation(self):
        self.roster.set_status("b1", OFF_DUTY)
        decision = self.roster.route_one_off("task-1", "alpha")
        self.assertEqual(decision.agent_id, "b1")
        self.assertEqual(decision.team, "beta")
        self.assertTrue(decision.requires_confirmation)

    def test_no_agent_available(self):
        decision = self.roster.route_one_off("task-1", "alpha")
        self.assertIsNone(decision.agent_id)
        self.assertEqual(decision.reason, "no off duty agent available")

    def test_confirm_lifts_requirement(self):
        self.roster.set_status("b1", OFF_DUTY)
        proposal = self.roster.route_one_off("task-1", "alpha")
        confirmed = self.roster.confirm(proposal)
        self.assertIsNotNone(confirmed)
        self.assertFalse(confirmed.requires_confirmation)
        routed = self.roster.route_one_off("task-1", "alpha")
        self.assertEqual(routed.agent_id, "b1")
        self.assertFalse(routed.requires_confirmation)

    def test_confirm_requires_off_duty(self):
        self.roster.set_status("b1", OFF_DUTY)
        proposal = self.roster.route_one_off("task-1", "alpha")
        self.roster.set_status("b1", ON_DUTY)
        confirmed = self.roster.confirm(proposal)
        self.assertIsNone(confirmed)

    def test_consume_confirmation(self):
        self.roster.set_status("b1", OFF_DUTY)
        proposal = self.roster.route_one_off("task-1", "alpha")
        self.roster.confirm(proposal)
        consumed = self.roster.consume_confirmation("task-1")
        self.assertIsNotNone(consumed)
        again = self.roster.route_one_off("task-1", "alpha")
        self.assertTrue(again.requires_confirmation)

    def test_stale_confirmation_lapses(self):
        self.roster.set_status("b1", OFF_DUTY)
        proposal = self.roster.route_one_off("task-1", "alpha")
        self.roster.confirm(proposal)
        self.roster.set_status("b1", ON_DUTY)
        routed = self.roster.route_one_off("task-1", "alpha")
        self.assertIsNone(routed.agent_id)

    def test_focused_team_not_routed(self):
        self.roster.set_focus("beta", "signals")
        self.roster.set_status("b1", OFF_DUTY)
        decision = self.roster.route_one_off("task-1", "alpha")
        self.assertIsNone(decision.agent_id)

    def test_focused_own_team_has_no_exception(self):
        self.roster.set_focus("alpha", "signals")
        self.roster.set_status("a1", OFF_DUTY)
        self.roster.mark_sprint_complete("a1")
        decision = self.roster.route_one_off("task-1", "alpha")
        self.assertIsNone(decision.agent_id)

    def test_released_team_routed_again(self):
        self.roster.set_focus("beta", "signals")
        self.roster.set_status("b1", OFF_DUTY)
        self.roster.release_team("beta")
        decision = self.roster.route_one_off("task-1", "alpha")
        self.assertEqual(decision.agent_id, "b1")

    def test_do_not_disturb_team_never_routed(self):
        self.roster.set_do_not_disturb("beta", True)
        self.roster.set_status("b1", OFF_DUTY)
        decision = self.roster.route_one_off("task-1", "alpha")
        self.assertIsNone(decision.agent_id)

    def test_wakeable_respects_focus(self):
        self.roster.set_focus("beta", "signals")
        self.roster.set_status("b1", OFF_DUTY)
        self.assertTrue(self.roster.wakeable("b1", "signals"))
        self.assertFalse(self.roster.wakeable("b1", None))
        self.assertFalse(self.roster.wakeable("b1", "cleanup"))

    def test_wakeable_requires_off_duty(self):
        self.assertFalse(self.roster.wakeable("b1", None))
        self.roster.set_status("b1", OFF_DUTY)
        self.assertTrue(self.roster.wakeable("b1", None))

    def test_team_available_counts(self):
        self.assertEqual(len(self.roster.team_available("alpha")), 2)
        self.roster.set_status("a1", OFF_DUTY)
        self.assertEqual(len(self.roster.team_available("alpha")), 1)
        self.roster.mark_sprint_complete("a1")
        self.assertEqual(len(self.roster.team_available("alpha")), 2)

    def test_focused_team_has_no_meeting_capacity(self):
        self.roster.set_focus("beta", "signals")
        self.assertEqual(len(self.roster.team_available("beta")), 0)
