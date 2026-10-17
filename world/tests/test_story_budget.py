"""Tests for the story budget ledger (ask 9).

The reference test file also covers a BudgetMonitor that
pushes alerts through a keyed policy API. That monitor is
not ported (see story_budget.py), so its tests are not
included here.
"""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from planner import PlannerSubmission, SupervisorMeeting  # noqa: E402
from roster import Roster  # noqa: E402
from story_budget import BudgetLedger  # noqa: E402


class TestBudgetLedger(unittest.TestCase):
    def setUp(self):
        self.ledger = BudgetLedger()
        self.ledger.open_line(1, 60.0, 4)

    def test_record_spend_under_budget(self):
        alert = self.ledger.record_spend(1, 30.0, 2)
        self.assertIsNone(alert)
        line = self.ledger.line(1)
        self.assertEqual(line.spent_minutes, 30.0)
        self.assertFalse(line.exceeded)

    def test_overrun_creates_alert(self):
        alert = self.ledger.record_spend(1, 61.0, 3)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.story_id, 1)
        self.assertEqual(alert.completed_items, 3)
        self.assertEqual(alert.total_items, 4)
        self.assertEqual(len(self.ledger.alerts()), 1)

    def test_no_repeat_alert_while_over(self):
        self.ledger.record_spend(1, 61.0, 3)
        alert = self.ledger.record_spend(1, 10.0, 4)
        self.assertIsNone(alert)

    def test_review_grant_extends_budget(self):
        self.ledger.record_spend(1, 61.0, 3)
        review = self.ledger.review(1, "scope grew", "grant", 30.0)
        self.assertEqual(review.decision, "grant")
        line = self.ledger.line(1)
        self.assertEqual(line.budget_minutes, 90.0)
        self.assertFalse(line.closed)

    def test_review_stop_closes_line(self):
        self.ledger.record_spend(1, 61.0, 3)
        review = self.ledger.review(1, "low value", "stop")
        self.assertEqual(review.decision, "stop")
        line = self.ledger.line(1)
        self.assertTrue(line.closed)
        alert = self.ledger.record_spend(1, 5.0, 4)
        self.assertIsNone(alert)
        self.assertEqual(line.spent_minutes, 61.0)

    def test_review_unknown_story(self):
        review = self.ledger.review(99, "nope", "stop")
        self.assertIsNone(review)

    def test_overrun_factor(self):
        self.ledger.record_spend(1, 90.0, 4)
        self.ledger.review(1, "underestimated", "stop")
        self.assertEqual(self.ledger.overrun_factor(), 1.5)

    def test_overrun_factor_empty(self):
        self.assertEqual(self.ledger.overrun_factor(), 0.0)


class TestMeetingBudgets(unittest.TestCase):
    def setUp(self):
        self.roster = Roster()
        self.roster.add_team("research")
        self.roster.add_agent("r1", "research", "worker")
        self.ledger = BudgetLedger()
        self.meeting = SupervisorMeeting(
            self.roster,
            specialties={"research": ["paper"]},
            stories_per_agent=1,
            ledger=self.ledger,
        )

    def test_meeting_opens_budget_lines(self):
        submission = PlannerSubmission(
            title="Papers",
            goal="g",
            items=["write paper"],
            durations={"write paper": 60.0},
        )
        self.meeting.batch_size = 1
        _, stories = self.meeting.breakdown(submission)
        assignments = self.meeting.assign(stories)
        self.assertEqual(len(assignments), 1)
        line = self.ledger.line(stories[0].story_id)
        self.assertIsNotNone(line)
        self.assertEqual(line.budget_minutes, 60.0)
        self.assertEqual(line.total_items, 1)

    def test_meeting_scales_by_history(self):
        self.ledger.open_line(99, 100.0, 1)
        self.ledger.record_spend(99, 150.0, 1)
        self.ledger.review(99, "underestimated", "stop")
        submission = PlannerSubmission(
            title="Papers",
            goal="g",
            items=["write paper"],
            durations={"write paper": 60.0},
        )
        self.meeting.batch_size = 1
        _, stories = self.meeting.breakdown(submission)
        self.meeting.assign(stories)
        line = self.ledger.line(stories[0].story_id)
        self.assertEqual(line.budget_minutes, 150.0)
