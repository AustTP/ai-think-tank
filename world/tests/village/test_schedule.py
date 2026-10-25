"""Tests for the standing-task scheduler."""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from schedule import (  # noqa: E402
    StandingScheduler,
    StandingTask,
    supervisor_agent_count,
)

MONDAY_1 = datetime(2026, 10, 19)
MONDAY_2 = datetime(2026, 10, 26)


def monday_task(**kwargs):
    base = dict(
        task_id="sweep",
        name="Monday sweep",
        mode="weekly",
        days_of_week={0},
        start_time="08:00",
        end_time="12:00",
    )
    base.update(kwargs)
    return StandingTask(**base)


class TestValidation(unittest.TestCase):
    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            StandingTask(task_id="t", name="t", mode="daily")

    def test_hours_requires_duration(self):
        with self.assertRaises(ValueError):
            StandingTask(task_id="t", name="t", mode="hours")

    def test_weekly_requires_days(self):
        with self.assertRaises(ValueError):
            StandingTask(
                task_id="t",
                name="t",
                mode="weekly",
                start_time="08:00",
                end_time="12:00",
            )

    def test_weekly_rejects_bad_day(self):
        with self.assertRaises(ValueError):
            StandingTask(
                task_id="t",
                name="t",
                mode="weekly",
                days_of_week={7},
                start_time="08:00",
                end_time="12:00",
            )

    def test_monthly_requires_day(self):
        with self.assertRaises(ValueError):
            StandingTask(
                task_id="t", name="t", mode="monthly",
                start_time="08:00", end_time="12:00",
            )

    def test_yearly_requires_date(self):
        with self.assertRaises(ValueError):
            StandingTask(
                task_id="t", name="t", mode="yearly",
                start_time="08:00", end_time="12:00",
            )

    def test_window_requires_times(self):
        with self.assertRaises(ValueError):
            StandingTask(task_id="t", name="t", mode="window")

    def test_invalid_time_format(self):
        with self.assertRaises(ValueError):
            StandingTask(
                task_id="t",
                name="t",
                mode="window",
                start_time="8am",
                end_time="12:00",
            )


class TestIsActive(unittest.TestCase):
    def setUp(self):
        self.scheduler = StandingScheduler()

    def state(self, task):
        return self.scheduler._state(task)

    def test_always(self):
        task = StandingTask(task_id="t", name="t")
        self.assertTrue(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1
            )
        )

    def test_window_inside(self):
        task = StandingTask(
            task_id="t", name="t", mode="window",
            start_time="08:00", end_time="12:00",
        )
        self.assertTrue(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1.replace(hour=9)
            )
        )

    def test_window_outside(self):
        task = StandingTask(
            task_id="t", name="t", mode="window",
            start_time="08:00", end_time="12:00",
        )
        self.assertFalse(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1.replace(hour=13)
            )
        )

    def test_overnight_window_wraps(self):
        task = StandingTask(
            task_id="t", name="t", mode="window",
            start_time="22:00", end_time="02:00",
        )
        self.assertTrue(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1.replace(hour=23)
            )
        )
        self.assertTrue(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1.replace(hour=1)
            )
        )
        self.assertFalse(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1.replace(hour=10)
            )
        )

    def test_weekly_on_day_and_off_day(self):
        task = monday_task()
        self.assertTrue(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1.replace(hour=9)
            )
        )
        self.assertFalse(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1.replace(hour=13)
            )
        )
        self.assertFalse(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_2.replace(hour=9)
                + timedelta(days=1)
            )
        )

    def test_monthly(self):
        task = StandingTask(
            task_id="t", name="t", mode="monthly", day_of_month=15,
            start_time="08:00", end_time="12:00",
        )
        self.assertTrue(
            self.scheduler.is_active(
                task, self.state(task), datetime(2026, 10, 15, 9)
            )
        )
        self.assertFalse(
            self.scheduler.is_active(
                task, self.state(task), datetime(2026, 10, 16, 9)
            )
        )

    def test_yearly(self):
        task = StandingTask(
            task_id="t", name="t", mode="yearly",
            yearly_date=(3, 15),
            start_time="08:00", end_time="12:00",
        )
        self.assertTrue(
            self.scheduler.is_active(
                task, self.state(task), datetime(2026, 3, 15, 9)
            )
        )
        self.assertFalse(
            self.scheduler.is_active(
                task, self.state(task), datetime(2026, 3, 16, 9)
            )
        )

    def test_hours_before_anchor_is_eligible(self):
        task = StandingTask(
            task_id="t", name="t", mode="hours", duration_hours=2.0
        )
        self.assertTrue(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1
            )
        )


class TestSchedulerFlow(unittest.TestCase):
    def setUp(self):
        self.scheduler = StandingScheduler()

    def test_always_starts_and_continues(self):
        task = StandingTask(task_id="t", name="t", agent_count=2)
        first = self.scheduler.tick(task, MONDAY_1)
        self.assertEqual(first.action, "start")
        self.assertEqual(first.agent_count, 2)
        self.assertEqual(first.lead_role, "director")
        later = self.scheduler.tick(task, MONDAY_1 + timedelta(hours=3))
        self.assertEqual(later.action, "continue")

    def test_hours_mode_is_one_off(self):
        task = StandingTask(
            task_id="t", name="t", mode="hours",
            duration_hours=2.0, agent_count=1,
        )
        start = self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.assertEqual(start.action, "start")
        mid = self.scheduler.tick(task, MONDAY_1.replace(hour=9))
        self.assertEqual(mid.action, "continue")
        stop = self.scheduler.tick(
            task, MONDAY_1.replace(hour=10, minute=1)
        )
        self.assertEqual(stop.action, "stop")
        state = self.scheduler.states["t"]
        self.assertTrue(state.finished)
        self.assertEqual(state.finish_reason, "duration elapsed")
        after = self.scheduler.tick(task, MONDAY_2)
        self.assertEqual(after.action, "idle")
        self.assertIn("finished", after.reason)

    def test_weekly_runs_until_occurrence_cap(self):
        task = monday_task(max_occurrences=2, agent_count=2)
        self.assertEqual(
            self.scheduler.tick(task, MONDAY_1.replace(hour=8)).action,
            "start",
        )
        self.assertEqual(
            self.scheduler.tick(task, MONDAY_1.replace(hour=10)).action,
            "continue",
        )
        stop = self.scheduler.tick(task, MONDAY_1.replace(hour=12, minute=30))
        self.assertEqual(stop.action, "stop")
        self.assertEqual(
            self.scheduler.tick(task, MONDAY_1.replace(hour=13)).action,
            "idle",
        )
        self.assertEqual(
            self.scheduler.tick(task, MONDAY_2.replace(hour=8)).action,
            "start",
        )
        stop2 = self.scheduler.tick(task, MONDAY_2.replace(hour=12, minute=30))
        self.assertEqual(stop2.action, "stop")
        state = self.scheduler.states["sweep"]
        self.assertTrue(state.finished)
        self.assertEqual(state.occurrences, 2)
        self.assertEqual(state.finish_reason, "occurrence cap reached")
        final = self.scheduler.tick(task, MONDAY_2 + timedelta(days=7, hours=8))
        self.assertEqual(final.action, "idle")

    def test_idle_outside_window(self):
        task = monday_task()
        result = self.scheduler.tick(task, MONDAY_1.replace(hour=7))
        self.assertEqual(result.action, "idle")
        self.assertEqual(result.reason, "outside active window")

    def test_consecutive_hours_cap(self):
        task = monday_task(max_consecutive_hours=4.0)
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.scheduler.tick(task, MONDAY_1.replace(hour=11))
        stop = self.scheduler.tick(task, MONDAY_1.replace(hour=12))
        self.assertEqual(stop.action, "stop")
        state = self.scheduler.states["sweep"]
        self.assertTrue(state.finished)
        self.assertEqual(state.finish_reason, "consecutive hours reached")

    def test_consecutive_resets_after_gap(self):
        task = monday_task(
            max_consecutive_hours=8.0,
            consecutive_reset_after=timedelta(days=3),
        )
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.scheduler.tick(task, MONDAY_1.replace(hour=12, minute=30))
        self.scheduler.tick(task, datetime(2026, 10, 22, 13))
        self.scheduler.tick(task, MONDAY_2.replace(hour=8))
        self.scheduler.tick(task, MONDAY_2.replace(hour=12, minute=30))
        state = self.scheduler.states["sweep"]
        self.assertFalse(state.finished)
        self.assertAlmostEqual(state.consecutive_hours, 4.5)

    def test_consecutive_accumulates_across_weeks(self):
        task = monday_task(
            max_consecutive_hours=8.0,
            consecutive_reset_after=timedelta(days=8),
        )
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.scheduler.tick(task, MONDAY_1.replace(hour=12))
        self.scheduler.tick(task, MONDAY_1.replace(hour=12, minute=30))
        self.scheduler.tick(task, MONDAY_2.replace(hour=8))
        self.scheduler.tick(task, MONDAY_2.replace(hour=11))
        stop = self.scheduler.tick(task, MONDAY_2.replace(hour=12))
        self.assertEqual(stop.action, "stop")
        state = self.scheduler.states["sweep"]
        self.assertTrue(state.finished)
        self.assertEqual(state.finish_reason, "consecutive hours reached")

    def test_total_hours_cap(self):
        task = monday_task(max_total_hours=4.0)
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        stop = self.scheduler.tick(task, MONDAY_1.replace(hour=12))
        self.assertEqual(stop.action, "stop")
        state = self.scheduler.states["sweep"]
        self.assertTrue(state.finished)
        self.assertEqual(state.finish_reason, "total hours reached")

    def test_supervisor_decides_agent_count(self):
        task = monday_task(agent_count=None)
        self.assertEqual(supervisor_agent_count(task), 3)
        result = self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.assertEqual(result.agent_count, 3)
        self.assertEqual(result.lead_role, "director")

    def test_large_crew_gets_scrum_master(self):
        task = monday_task(agent_count=5)
        result = self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.assertEqual(result.lead_role, "scrum master")

    def test_custom_agent_count_policy(self):
        scheduler = StandingScheduler(agent_count_policy=lambda t: 7)
        task = monday_task(agent_count=None)
        result = scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.assertEqual(result.agent_count, 7)

    def test_tick_all(self):
        tasks = [
            monday_task(task_id="a"),
            StandingTask(task_id="b", name="b"),
        ]
        results = self.scheduler.tick_all(tasks, MONDAY_1.replace(hour=8))
        self.assertEqual(set(results), {"a", "b"})
        self.assertEqual(results["a"].action, "start")
        self.assertEqual(results["b"].action, "start")

    def test_tick_reports_finished_flag(self):
        task = monday_task(max_occurrences=1, agent_count=2)
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        stop = self.scheduler.tick(task, MONDAY_1.replace(hour=12, minute=30))
        self.assertEqual(stop.action, "stop")
        self.assertTrue(stop.finished)

    def test_natural_completion_early(self):
        task = monday_task()
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        result = self.scheduler.complete(task, MONDAY_1.replace(hour=10))
        self.assertEqual(result.action, "stop")
        self.assertEqual(result.reason, "work complete")
        self.assertFalse(result.finished)
        state = self.scheduler.states["sweep"]
        self.assertEqual(state.occurrences, 1)
        self.assertAlmostEqual(state.total_hours, 2.0)
        self.assertAlmostEqual(state.consecutive_hours, 2.0)
        self.assertFalse(state.finished)
        self.assertEqual(
            self.scheduler.tick(task, MONDAY_1.replace(hour=11)).action,
            "idle",
        )

    def test_natural_completion_next_week(self):
        task = monday_task()
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.scheduler.complete(task, MONDAY_1.replace(hour=11))
        self.assertEqual(
            self.scheduler.tick(task, MONDAY_1.replace(hour=13)).action,
            "idle",
        )
        self.assertEqual(
            self.scheduler.tick(task, MONDAY_2.replace(hour=8)).action,
            "start",
        )

    def test_no_restart_within_completed_window(self):
        task = monday_task()
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.scheduler.complete(task, MONDAY_1.replace(hour=10))
        result = self.scheduler.tick(task, MONDAY_1.replace(hour=11))
        self.assertEqual(result.action, "idle")
        self.assertEqual(result.reason, "period complete")

    def test_always_mode_restarts_after_completion(self):
        task = StandingTask(task_id="t", name="t", agent_count=1)
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.scheduler.complete(task, MONDAY_1.replace(hour=9))
        result = self.scheduler.tick(task, MONDAY_1.replace(hour=10))
        self.assertEqual(result.action, "start")

    def test_natural_completion_hits_occurrence_cap(self):
        task = monday_task(max_occurrences=1)
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        result = self.scheduler.complete(task, MONDAY_1.replace(hour=10))
        self.assertEqual(result.action, "stop")
        self.assertTrue(result.finished)
        state = self.scheduler.states["sweep"]
        self.assertEqual(state.finish_reason, "occurrence cap reached")

    def test_hours_mode_natural_completion(self):
        task = StandingTask(
            task_id="t", name="t", mode="hours", duration_hours=4.0
        )
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        result = self.scheduler.complete(task, MONDAY_1.replace(hour=10))
        self.assertEqual(result.reason, "work complete")
        state = self.scheduler.states["t"]
        self.assertTrue(state.finished)
        self.assertEqual(state.finish_reason, "work complete")
        self.assertAlmostEqual(state.total_hours, 2.0)

    def test_complete_when_not_running(self):
        task = monday_task()
        result = self.scheduler.complete(task, MONDAY_1.replace(hour=9))
        self.assertEqual(result.action, "idle")
        self.assertEqual(result.reason, "not running")

    def test_complete_after_finished(self):
        task = monday_task(max_occurrences=1)
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.scheduler.complete(task, MONDAY_1.replace(hour=9))
        result = self.scheduler.complete(task, MONDAY_1.replace(hour=10))
        self.assertEqual(result.action, "idle")
        self.assertIn("finished", result.reason)

    def test_never_ending_weekly_task(self):
        task = monday_task()
        for week in range(1, 53):
            monday = MONDAY_1 + timedelta(weeks=week)
            self.scheduler.tick(task, monday.replace(hour=8))
            self.scheduler.complete(
                task, monday.replace(hour=12, minute=30)
            )
            self.scheduler.tick(task, monday.replace(hour=13))
        state = self.scheduler.states["sweep"]
        self.assertFalse(state.finished)
        self.assertEqual(state.occurrences, 52)

    def test_abort_cancels_running_period(self):
        task = monday_task()
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.assertTrue(self.scheduler.abort(task))
        state = self.scheduler.states["sweep"]
        self.assertFalse(state.running)
        self.assertEqual(state.occurrences, 0)
        self.assertEqual(
            self.scheduler.tick(task, MONDAY_1.replace(hour=9)).action,
            "start",
        )

    def test_abort_resets_hours_anchor(self):
        task = StandingTask(
            task_id="t", name="t", mode="hours", duration_hours=2.0
        )
        self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.scheduler.abort(task)
        self.scheduler.tick(task, MONDAY_1.replace(hour=9))
        stop = self.scheduler.tick(task, MONDAY_1.replace(hour=11))
        self.assertEqual(stop.action, "stop")
        state = self.scheduler.states["t"]
        self.assertEqual(state.finish_reason, "duration elapsed")
        self.assertAlmostEqual(state.total_hours, 2.0)

    def test_abort_when_not_running(self):
        task = monday_task()
        self.assertFalse(self.scheduler.abort(task))


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.scheduler = StandingScheduler()

    def state(self, task):
        return self.scheduler._state(task)

    def test_unknown_mode_is_never_active(self):
        task = monday_task()
        task.mode = "bogus"
        self.assertFalse(
            self.scheduler.is_active(
                task, self.state(task), MONDAY_1.replace(hour=9)
            )
        )

    def test_running_without_start_time_uses_now(self):
        task = monday_task()
        state = self.state(task)
        state.running = True
        result = self.scheduler.tick(task, MONDAY_1.replace(hour=9))
        self.assertEqual(result.action, "continue")
        self.assertAlmostEqual(state.consecutive_hours, 0.0)

    def test_occurrence_cap_checked_at_tick(self):
        task = monday_task(max_occurrences=1)
        self.state(task).occurrences = 1
        result = self.scheduler.tick(task, MONDAY_1.replace(hour=8))
        self.assertEqual(result.action, "idle")
        self.assertIn("finished", result.reason)
        state = self.scheduler.states["sweep"]
        self.assertTrue(state.finished)
        self.assertEqual(state.finish_reason, "occurrence cap reached")

    def test_end_period_without_start_time(self):
        task = monday_task()
        state = self.state(task)
        state.running = True
        self.scheduler._end_period(
            task, state, MONDAY_1.replace(hour=9))
        self.assertEqual(state.occurrences, 1)
        self.assertAlmostEqual(state.total_hours, 0.0)
        self.assertAlmostEqual(state.consecutive_hours, 0.0)
        self.assertFalse(state.running)


if __name__ == "__main__":
    unittest.main()
