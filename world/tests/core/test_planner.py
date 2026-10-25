"""Tests for the supervisor meeting planner (asks 8 and 9)."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from core.planner import PlannerSubmission, SupervisorMeeting  # noqa: E402
from village.roster import OFF_DUTY, Roster  # noqa: E402


class TestSupervisorMeeting(unittest.TestCase):
    def setUp(self):
        self.roster = Roster()
        self.roster.add_team("trading")
        self.roster.add_team("research")
        self.roster.add_agent("t1", "trading", "director")
        self.roster.add_agent("t2", "trading", "worker")
        self.roster.add_agent("r1", "research", "worker")
        self.meeting = SupervisorMeeting(
            self.roster,
            specialties={
                "trading": ["market", "ticker", "trade"],
                "research": ["paper", "study", "report"],
            },
            stories_per_agent=1,
        )

    def test_breakdown_creates_brief(self):
        submission = PlannerSubmission(
            title="Weekly review",
            goal="Review markets",
            items=["check BTC", "check ETH"],
        )
        brief, stories = self.meeting.breakdown(submission)
        self.assertEqual(brief.role, "supervisor")
        self.assertEqual(brief.total_items, 2)
        self.assertFalse(brief.scope_confirmation_required)
        self.assertEqual(len(stories), 1)

    def test_breakdown_flags_large_scope(self):
        submission = PlannerSubmission(
            title="Big",
            goal="g",
            items=[f"item {n}" for n in range(101)],
        )
        brief, _ = self.meeting.breakdown(submission)
        self.assertTrue(brief.scope_confirmation_required)

    def test_refine_batches_items(self):
        submission = PlannerSubmission(
            title="Weekly review",
            goal="Review markets",
            items=["check BTC market", "study market report"],
        )
        brief, stories = self.meeting.breakdown(submission)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].item_ids, ["w-0001", "w-0002"])

    def test_refine_respects_batch_size(self):
        submission = PlannerSubmission(
            title="Weekly review",
            goal="g",
            items=["a", "b", "c"],
        )
        self.meeting.batch_size = 2
        _, stories = self.meeting.breakdown(submission)
        self.assertEqual(len(stories), 2)
        self.assertEqual(len(stories[0].item_ids), 2)
        self.assertEqual(len(stories[1].item_ids), 1)

    def test_assign_matches_specialties(self):
        submission = PlannerSubmission(
            title="Mixed",
            goal="g",
            items=["trade BTC", "write paper"],
        )
        self.meeting.batch_size = 1
        _, stories = self.meeting.breakdown(submission)
        assignments = self.meeting.assign(stories)
        by_team = {a.team: a.story_ids for a in assignments}
        self.assertEqual(by_team["trading"], [1])
        self.assertEqual(by_team["research"], [2])

    def test_assign_respects_capacity(self):
        submission = PlannerSubmission(
            title="Overflow",
            goal="g",
            items=[f"trade {n}" for n in range(5)],
        )
        self.meeting.batch_size = 1
        _, stories = self.meeting.breakdown(submission)
        assignments = self.meeting.assign(stories)
        trading = next(a for a in assignments if a.team == "trading")
        self.assertEqual(len(trading.story_ids), 2)

    def test_unmatched_work_goes_to_largest_capacity(self):
        submission = PlannerSubmission(
            title="Misc",
            goal="g",
            items=["do chores"],
        )
        self.meeting.batch_size = 1
        _, stories = self.meeting.breakdown(submission)
        assignments = self.meeting.assign(stories)
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].team, "trading")

    def test_focused_team_gets_no_work(self):
        self.roster.set_focus("trading", "signals")
        submission = PlannerSubmission(
            title="Misc",
            goal="g",
            items=["trade BTC", "write paper"],
        )
        self.meeting.batch_size = 1
        _, stories = self.meeting.breakdown(submission)
        assignments = self.meeting.assign(stories)
        teams = {a.team for a in assignments}
        self.assertEqual(teams, {"research"})

    def test_no_capacity_drops_work(self):
        for agent_id in ("t1", "t2", "r1"):
            self.roster.set_status(agent_id, OFF_DUTY)
        submission = PlannerSubmission(
            title="Misc",
            goal="g",
            items=["trade BTC"],
        )
        self.meeting.batch_size = 1
        _, stories = self.meeting.breakdown(submission)
        assignments = self.meeting.assign(stories)
        self.assertEqual(assignments, [])


if __name__ == '__main__':
    unittest.main()
