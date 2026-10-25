"""Split part of test_serve.py (serve.py behavior tests)."""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse

import asyncio
import openpyxl

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import sim  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402


class SandboxBackups(unittest.TestCase):
    # Real incident this exists to prevent from ever being unrecoverable
    # again: a coding task overwrote a real sandbox file with no way to
    # undo it -- no git repo, no .bak, no OS-level snapshot existed for
    # Rebuilt on local git (weighed
    # against a GitHub remote and against the original full-directory-copy
    # version, which measured at 868KB for 8 snapshots of one real
    # sandbox -- not a problem yet, but linear in sandbox size with no way
    # to store less than a full copy). _restore_sandbox_backup() always
    # resolves the sandbox directory via the real _sandbox_dir_for() (the
    # same thing every other real sandbox endpoint uses), so these tests
    # use a real, uniquely-named test sandbox under the real sandboxes/
    # directory -- same "real path, real cleanup" discipline as the
    # LibraryIngest tests above.
    SANDBOX_ID = 'test-sandbox-backups'

    def setUp(self):
        self.sandbox_dir = serve._sandbox_dir_for(self.SANDBOX_ID)
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>real content</html>')

    def tearDown(self):
        shutil.rmtree(self.sandbox_dir, ignore_errors=True)

    def test_snapshotting_an_empty_directory_is_a_no_op(self):
        empty_dir = tempfile.mkdtemp()
        try:
            serve._snapshot_sandbox('test-sandbox-empty', empty_dir)
            self.assertFalse(os.path.isdir(os.path.join(empty_dir, '.git')), 'nothing real existed yet, so there is nothing to version at all')
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)

    def test_a_real_snapshot_is_listed_and_restorable(self):
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        backups = serve._list_sandbox_backups(self.SANDBOX_ID)
        self.assertEqual(len(backups), 1)

        # Simulate the exact real incident: the file gets destructively overwritten.
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>fabricated replacement, the real content is gone</html>')

        serve._restore_sandbox_backup(self.SANDBOX_ID, backups[0])
        with open(os.path.join(self.sandbox_dir, 'index.html')) as f:
            self.assertEqual(f.read(), '<html>real content</html>', 'the real, pre-overwrite content should be back')

    def test_repeated_snapshots_with_no_real_change_do_not_pile_up_empty_commits(self):
        # Real behavior difference from the old copy-based version, worth
        # locking in: git naturally no-ops a commit when nothing changed,
        # so calling this on every single /api/execute (even ones that
        # touch nothing) doesn't inflate history with empty entries.
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        self.assertEqual(len(serve._list_sandbox_backups(self.SANDBOX_ID)), 1)

    def test_restoring_is_itself_undoable(self):
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        original_backup = serve._list_sandbox_backups(self.SANDBOX_ID)[0]
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('bad overwrite')
        serve._restore_sandbox_backup(self.SANDBOX_ID, original_backup)
        # Restoring should have snapshotted the (bad) pre-restore state,
        # AND the restore itself, as two more real commits.
        backups_after = serve._list_sandbox_backups(self.SANDBOX_ID)
        self.assertEqual(len(backups_after), 3, 'the pre-restore state and the restore itself should both be real, separate commits')

    def test_restoring_an_unknown_stamp_raises_instead_of_silently_doing_nothing(self):
        with self.assertRaises(ValueError):
            serve._restore_sandbox_backup(self.SANDBOX_ID, 'not-a-real-commit-hash')

    def test_history_survives_and_accumulates_across_many_real_changes(self):
        # Real answer to the storage concern this rebuild was FOR: confirm
        # history keeps growing with real content instead of silently
        # capping/dropping old entries the way the copy-based version had
        # to (SANDBOX_BACKUPS_TO_KEEP), since git's per-commit cost is the
        # diff, not a full copy.
        for i in range(15):
            with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
                f.write(f'<html>version {i}</html>')
            serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        self.assertEqual(len(serve._list_sandbox_backups(self.SANDBOX_ID)), 15)


class ActivitySummary(unittest.TestCase):
    # Real fix for confabulated retrospectives: an agent asked to reflect
    # on the think tank with nothing but a role and a vague prompt invented
    # confident-sounding specifics that don't exist in the real code. This
    # grounds that in the same action_log every real action already
    # writes into, so a retrospective prompt can include what an agent
    # actually, verifiably did.
    AGENT_ID = 'test-activity-summary-agent'

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log WHERE agent_id = ?', (self.AGENT_ID,))

    def test_no_history_is_an_empty_summary(self):
        self.assertEqual(serve._activity_summary_for(self.AGENT_ID), {})

    def test_counts_are_grouped_by_action_type(self):
        serve.log_action(self.AGENT_ID, 'curl', {})
        serve.log_action(self.AGENT_ID, 'curl', {})
        serve.log_action(self.AGENT_ID, 'execute', {})
        counts = serve._activity_summary_for(self.AGENT_ID)
        self.assertEqual(counts.get('curl'), 2)
        self.assertEqual(counts.get('execute'), 1)

    def test_another_agents_actions_are_not_counted(self):
        serve.log_action(self.AGENT_ID, 'curl', {})
        serve.log_action('some-other-agent', 'curl', {})
        serve.log_action('some-other-agent', 'curl', {})
        self.assertEqual(serve._activity_summary_for(self.AGENT_ID).get('curl'), 1)


class CountDueWorkItems(unittest.TestCase):
    # _count_due_work_items is the one bit of compute_health_snapshot
    # that's a pure function safe to test directly (no real think_tank.db
    # round-trip) -- and the one part most worth pinning down, since a
    # unit mismatch (epoch seconds vs. JS's epoch milliseconds) would
    # fail completely silently: it would just always or never count an
    # item as due, with no error anywhere.
    def test_an_item_with_no_notBefore_is_always_due(self):
        self.assertEqual(serve._count_due_work_items([{'title': 'x'}], now_ms=1000), 1)

    def test_an_item_scheduled_for_the_future_is_not_due(self):
        self.assertEqual(serve._count_due_work_items([{'notBefore': 5000}], now_ms=1000), 0)

    def test_an_item_scheduled_for_the_past_is_due(self):
        self.assertEqual(serve._count_due_work_items([{'notBefore': 500}], now_ms=1000), 1)

    def test_an_item_scheduled_for_exactly_now_is_due(self):
        self.assertEqual(serve._count_due_work_items([{'notBefore': 1000}], now_ms=1000), 1)

    def test_counts_only_the_due_ones_in_a_mixed_queue(self):
        queue = [{'notBefore': 500}, {'notBefore': 5000}, {'title': 'no notBefore at all'}]
        self.assertEqual(serve._count_due_work_items(queue, now_ms=1000), 2)

    def test_an_empty_queue_has_zero_due_items(self):
        self.assertEqual(serve._count_due_work_items([], now_ms=1000), 0)


class HealthChecks(unittest.TestCase):
    # Tests the pure alert-decision function only (_health_alerts_for_signals),
    # not compute_health_snapshot itself -- that one hits the real,
    # shared think_tank.db, whose action_log/model_tiers content isn't
    # something a test should assume or reset. Built to close the "no
    # monitoring layer -- everything gets caught reactively" gap.
    def _signals(self, **overrides):
        base = {
            'think_tank_active': True,
            'seconds_since_last_save': 2.0,
            'work_queue_size': 0,
            'work_queue_due_size': 0,
            'agents_count': 5,
            'work_items_abandoned_last_24h': 0,
            'login_failures_last_hour': 0,
            'blocked_or_failed_actions_last_hour': {},
            'tool_volume_by_agent_last_15m': {},
            'missing_model_tier_bands': [],
            'jev_decision_attempts_last_hour': 0,
            'jev_decision_failures_last_hour': 0,
            'self_proposed_rejected_last_24h': 0,
            'red_pipeline_rejects_last_24h': 0,
            'red_pipeline_escapes_last_24h': 0,
            'task_assigned_last_24h': 0,
            'task_completed_last_24h': 0,
            'task_median_completion_hours': None,
            'task_fast_completion_rate': None,
            'task_horizon_completion_rate': None,
            'ceremony_actions_last_24h': 0,
            'progress_actions_last_24h': 0,
            'ceremony_to_progress_ratio': None,
            'ceremony_signal': 0.0,
            'progress_signal': 0.0,
            'ceremony_imbalance_score': 0.0,
            'rework_loops_last_24h': 0,
            'looped_tasks_last_24h': 0,
            'loop_cost_usd_last_24h': 0.0,
            'straight_through_rate': None,
            'aging_in_flight_work': 0,
            'open_escalations': 0,
            'bank_over_cap': [],
            'bank_used': 0.0,
            'bank_cap': 0.0,
        }
        base.update(overrides)
        return base

    def test_nominal_state_raises_no_alerts(self):
        self.assertEqual(serve._health_alerts_for_signals(self._signals()), [])

    def test_red_pipeline_rejects_raise_only_info(self):
        # The fail-closed gate catching red content is the gate WORKING --
        # an info signal, never a warning.
        alerts = serve._health_alerts_for_signals(self._signals(
            red_pipeline_rejects_last_24h=6,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'quality_gate')
        self.assertEqual(alerts[0]['severity'], 'info')

    def test_red_pipeline_escape_raises_a_warning(self):
        # ANY red result that escaped to done means the fail-closed invariant
        # broke -- a single escape is a warning, even with zero rejects logged.
        alerts = serve._health_alerts_for_signals(self._signals(
            red_pipeline_escapes_last_24h=1,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'quality_gate')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('ESCAPED', alerts[0]['message'])

    def test_agent_tool_volume_anomaly_raises_behavior_alert(self):
        # An agent firing >threshold spend-inducing tool calls in the window
        # is flagged as a likely runaway loop / tool churn.
        alerts = serve._health_alerts_for_signals(self._signals(
            tool_volume_by_agent_last_15m={'ada': serve.ANOMALY_AGENT_TOOL_THRESHOLD + 5},
        ))
        self.assertTrue(any(a['category'] == 'behavior' for a in alerts))
        self.assertIn('ada', alerts[0]['message'])

    def test_agent_tool_volume_below_threshold_no_alert(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            tool_volume_by_agent_last_15m={'ada': serve.ANOMALY_AGENT_TOOL_THRESHOLD - 1},
        ))
        self.assertFalse(any(a['category'] == 'behavior' for a in alerts))

    def test_a_stale_queue_with_due_items_raises_an_info_alert(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            work_queue_due_size=3, think_tank_active=False, seconds_since_last_save=90.0,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'work_queue')
        self.assertEqual(alerts[0]['severity'], 'info')

    def test_a_nonempty_queue_with_an_active_tab_raises_nothing(self):
        # The queue having items is normal while something's actively
        # draining it -- only "queued AND nothing is running" is a signal.
        alerts = serve._health_alerts_for_signals(self._signals(work_queue_due_size=3, think_tank_active=True))
        self.assertEqual(alerts, [])

    def test_a_stale_queue_of_only_future_scheduled_items_raises_nothing(self):
        # Real gap this closes: a queue can be non-empty (work_queue_size)
        # while nothing in it is actually due yet (work_queue_due_size) --
        # an agent scheduled for later, sitting with no tab open, is
        # working as designed, not stuck.
        alerts = serve._health_alerts_for_signals(self._signals(
            work_queue_size=2, work_queue_due_size=0, think_tank_active=False, seconds_since_last_save=90.0,
        ))
        self.assertEqual(alerts, [])

    def test_abandoned_work_items_raise_a_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(work_items_abandoned_last_24h=2))
        self.assertEqual(len(alerts), 1)
        self.assertIn('2 work item', alerts[0]['message'])
        self.assertEqual(alerts[0]['severity'], 'warning')

    def test_a_handful_of_login_failures_raises_nothing(self):
        self.assertEqual(serve._health_alerts_for_signals(self._signals(login_failures_last_hour=4)), [])

    def test_five_or_more_login_failures_raises_a_security_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(login_failures_last_hour=5))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'security')

    def test_a_handful_of_blocked_calls_raises_nothing(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            blocked_or_failed_actions_last_hour={'browse': 9},
        ))
        self.assertEqual(alerts, [])

    def test_ten_or_more_blocked_calls_for_one_action_raises_a_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            blocked_or_failed_actions_last_hour={'browse': 3, 'curl': 10},
        ))
        self.assertEqual(len(alerts), 1)
        self.assertIn('curl', alerts[0]['message'])

    def test_missing_model_tier_bands_are_named_in_the_alert(self):
        alerts = serve._health_alerts_for_signals(self._signals(missing_model_tier_bands=['coding', 'vision']))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'model_tiers')
        self.assertIn('coding', alerts[0]['message'])
        self.assertIn('vision', alerts[0]['message'])

    def test_jev_health_below_min_attempts_raises_nothing(self):
        # Fewer than the minimum attempts can't establish a failure rate --
        # a quiet think tank with 2 blips out of 3 calls isn't a Jev outage.
        alerts = serve._health_alerts_for_signals(self._signals(
            jev_decision_attempts_last_hour=3, jev_decision_failures_last_hour=3,
        ))
        self.assertEqual(alerts, [])

    def test_jev_health_below_failure_rate_raises_nothing(self):
        # A busy think tank blips a call or two without being down -- the RATE,
        # not the raw count, is the signal.
        alerts = serve._health_alerts_for_signals(self._signals(
            jev_decision_attempts_last_hour=100, jev_decision_failures_last_hour=30,
        ))
        self.assertEqual(alerts, [])

    def test_jev_health_majority_failures_raise_a_warning(self):
        # >=50% of Jev calls failing over the window = the decisions model is
        # likely down, and the colony is running deterministic fallbacks.
        alerts = serve._health_alerts_for_signals(self._signals(
            jev_decision_attempts_last_hour=40, jev_decision_failures_last_hour=30,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'jev')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('30/40', alerts[0]['message'])

    def test_multiple_real_conditions_at_once_all_surface(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            work_items_abandoned_last_24h=1, login_failures_last_hour=6,
        ))
        self.assertEqual({a['category'] for a in alerts}, {'work_queue', 'security'})

    def test_self_proposed_rejections_below_threshold_raise_nothing(self):
        # A little grooming-out is refinement's normal job -- only a real
        # pattern of rejected self-proposals is worth surfacing.
        alerts = serve._health_alerts_for_signals(self._signals(
            self_proposed_rejected_last_24h=serve.SELF_PROPOSED_REJECT_THRESHOLD - 1,
        ))
        self.assertEqual(alerts, [])

    def test_rework_loops_below_volume_threshold_raise_nothing(self):
        # A couple of requeues is normal review churn, not a loop signal.
        alerts = serve._health_alerts_for_signals(self._signals(
            rework_loops_last_24h=serve._REWORK_MIN_LOOPS_FOR_ALERT - 1,
            straight_through_rate=0.2,
        ))
        self.assertEqual(alerts, [])

    def test_rework_loops_with_good_straight_through_raise_nothing(self):
        # Loops with plenty of completed work to show for them aren't debt.
        alerts = serve._health_alerts_for_signals(self._signals(
            rework_loops_last_24h=8, straight_through_rate=0.8,
        ))
        self.assertEqual(alerts, [])

    def test_low_straight_through_rate_raises_rework_warning(self):
        # The review pipeline redoing work instead of finishing it: enough
        # loops AND a straight-through rate below the floor.
        alerts = serve._health_alerts_for_signals(self._signals(
            rework_loops_last_24h=6, looped_tasks_last_24h=2,
            loop_cost_usd_last_24h=3.5, straight_through_rate=0.4,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'rework')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('6 rework loop', alerts[0]['message'])
        self.assertIn('$3.50', alerts[0]['message'])
        self.assertIn('40%', alerts[0]['message'])

    def test_rework_warning_without_spirals_unchanged(self):
        # No spirals in signals -> the rework block emits exactly what it did
        # before the spiral note existed.
        alerts = serve._health_alerts_for_signals(self._signals(
            rework_loops_last_24h=6, looped_tasks_last_24h=2,
            loop_cost_usd_last_24h=3.5, straight_through_rate=0.4,
            fix_spawn_spirals_last_24h=0, fix_spawn_tasks_last_24h=0,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertNotIn('Fix:', alerts[0]['message'])

    def test_fix_retry_spiral_alone_raises_rework_info(self):
        # A "Fix:" spiral with quiet loop metrics: each retry is a fresh task
        # id, so the loop counter never fires -- the spiral detector must.
        alerts = serve._health_alerts_for_signals(self._signals(
            rework_loops_last_24h=0,
            fix_spawn_spirals_last_24h=1, fix_spawn_tasks_last_24h=7,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'rework')
        self.assertEqual(alerts[0]['severity'], 'info')
        self.assertIn('spiral', alerts[0]['message'])
        self.assertIn('7 task id(s)', alerts[0]['message'])

    def test_fix_retry_spiral_mentions_auto_closed_wreckage(self):
        # The churn usually ends in auto-closes ("quality pipeline keeps
        # failing") -- the alert should quantify them when present.
        alerts = serve._health_alerts_for_signals(self._signals(
            rework_loops_last_24h=0,
            fix_spawn_spirals_last_24h=1, fix_spawn_tasks_last_24h=574,
            task_auto_closed_last_24h=82,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertIn('82 task(s) auto-closed', alerts[0]['message'])

    def test_fix_retry_spiral_note_appends_to_rework_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            rework_loops_last_24h=6, looped_tasks_last_24h=2,
            loop_cost_usd_last_24h=3.5, straight_through_rate=0.4,
            fix_spawn_spirals_last_24h=2, fix_spawn_tasks_last_24h=9,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('2 "Fix:" retry spiral(s)', alerts[0]['message'])

    def test_self_proposed_rejection_pattern_raises_coordination_info(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            self_proposed_rejected_last_24h=serve.SELF_PROPOSED_REJECT_THRESHOLD,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'coordination')
        self.assertEqual(alerts[0]['severity'], 'info')
        self.assertIn('rejected', alerts[0]['message'])

    def test_metr_too_few_assignments_raises_nothing(self):
        # Reliability-at-horizon is only judged with enough assigned tasks --
        # a think tank with 3 assignments and 0 fast finishes is just quiet.
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=serve.METR_MIN_ASSIGNED - 1,
            task_fast_completion_rate=0.0, task_median_completion_hours=12.0,
        ))
        self.assertEqual(alerts, [])

    def test_metr_low_fast_completion_rate_raises_throughput_warning(self):
        # Started-but-not-finished is the METR failure mode: few of the tasks
        # assigned in the window completed within the fast horizon.
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=10, task_fast_completion_rate=0.2,
            task_median_completion_hours=8.0, task_horizon_completion_rate=0.4,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'throughput')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('20%', alerts[0]['message'])

    def test_metr_warning_names_retired_pipeline_failures(self):
        # The live incident: fast median for the work that finished, but many
        # assigned cards were retired after a pipeline failure. The message
        # must say that instead of calling the colony slow.
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=10, task_fast_completion_rate=0.2,
            task_median_completion_hours=0.1, task_completed_last_24h=8,
            task_abandoned_last_24h=2,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertIn('2 retired after a pipeline failure', alerts[0]['message'])
        self.assertIn('median for finished work', alerts[0]['message'])

    def test_metr_slow_median_with_ok_fast_rate_raises_throughput_info(self):
        # Fast-enough start-to-finish on some, but the median is unusually
        # long: a milder (info) signal -- work is getting done but slowly.
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=10, task_fast_completion_rate=0.6,
            task_median_completion_hours=serve.METR_SLOW_MEDIAN_HOURS + 2.0,
            task_horizon_completion_rate=0.9,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'throughput')
        self.assertEqual(alerts[0]['severity'], 'info')
        self.assertIn('median', alerts[0]['message'])

    def test_metr_healthy_rates_raise_nothing(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=20, task_fast_completion_rate=0.8,
            task_median_completion_hours=0.5, task_horizon_completion_rate=0.95,
        ))
        self.assertEqual(alerts, [])

    def test_a_little_ceremony_with_no_progress_raises_nothing(self):
        # Below the minimum-volume floor -- a quiet think tank with a couple of
        # escalations and 0 releases isn't a coordination pathology, it's
        # just quiet. The imbalance score is only consulted once the decayed
        # ceremony SIGNAL clears the floor.
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=2, ceremony_signal=2.0, progress_actions_last_24h=0,
        ))
        self.assertEqual(alerts, [])

    def test_fresh_ceremony_with_zero_shipped_work_raises_an_info_alert(self):
        # 8 fresh ceremony actions vs 0 shipped: score log10(9) ~= 0.95 --
        # clear of the INFO floor (0.5) but under the WARNING floor (1.0).
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=8, ceremony_signal=8.0, progress_actions_last_24h=0,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'coordination')
        self.assertEqual(alerts[0]['severity'], 'info')
        self.assertIn('imbalance', alerts[0]['message'])

    def test_the_sep26_restart_storm_shape_raises_a_warning(self):
        # 79 fresh ceremony actions vs 3 shipped (the exact Sep 26 restart
        # storm profile): score log10(80) - log10(4) ~= 1.30 -- WARNING.
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=79, progress_actions_last_24h=3,
            ceremony_signal=79.0, progress_signal=3.0,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'coordination')
        self.assertEqual(alerts[0]['severity'], 'warning')

    def test_the_same_storm_three_days_stale_reads_quiet(self):
        # The Sep 26 storm aged to 3 days = ~6 half-lives, every
        # one of the 79 actions decayed ~64x => signal 1.2, under the 3.0
        # floor. Nothing still in the last 24h's COUNT for the message. The
        # old count-ratio would've screamed forever; the decayed scalar
        # fades with its cause.
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=0, progress_actions_last_24h=0,
            ceremony_signal=1.2, progress_signal=0.05,
        ))
        self.assertEqual(alerts, [])

    def test_heavy_ceremony_matched_by_real_progress_raises_nothing(self):
        # Lots of review activity is fine when shipped work keeps pace --
        # the imbalance score (log10(13) - log10(11) ~= 0.07), not the raw
        # count, is the signal.
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=12, progress_actions_last_24h=6,
            ceremony_signal=12.0, progress_signal=10.0,
        ))
        self.assertEqual(alerts, [])

    def test_aging_in_flight_work_raises_a_warning(self):
        # Admin gap 1: wedged in-flight work the stale-work sweep hasn't
        # re-planned yet is a digest-worthy warning, not a silent wait.
        alerts = serve._health_alerts_for_signals(self._signals(aging_in_flight_work=2))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'work_queue')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('wedged', alerts[0]['message'])

    def test_no_aging_work_raises_nothing(self):
        self.assertEqual(serve._health_alerts_for_signals(self._signals(aging_in_flight_work=0)), [])

    def test_a_single_open_escalation_raises_nothing(self):
        # One pending escalation is normal incident response -- only a stack
        # of unresolved products is a backing-up signal.
        alerts = serve._health_alerts_for_signals(self._signals(open_escalations=1))
        self.assertEqual(alerts, [])

    def test_two_open_escalations_raise_a_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(open_escalations=2))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'escalations')
        self.assertEqual(alerts[0]['severity'], 'warning')

    def test_bank_over_cap_raises_a_warning_naming_the_services(self):
        # Spend is real money -- the director teller's warning surfaced to the
        # admin digest, not just a bank-room readout a director opens on purpose.
        alerts = serve._health_alerts_for_signals(self._signals(
            bank_over_cap=['openrouter'], bank_used=12.0, bank_cap=10.0,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'bank')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('openrouter', alerts[0]['message'])
        self.assertIn('12.00', alerts[0]['message'])

    def test_bank_within_cap_raises_nothing(self):
        self.assertEqual(serve._health_alerts_for_signals(self._signals(
            bank_over_cap=[], bank_used=4.0, bank_cap=100.0,
        )), [])

    # --- OpenRouter live-deposit spend alerts -----------------------------
    # Alerts fire on the LIVE account funds (percent of total_credits
    # actually spent), not on the monthly plan. $10 deposited / $8 spent =
    # 80% used -> warning; $9 spent = 90% -> critical. The monthly budget
    # ($50 expected) is separate gauge context and never triggers these.
    def test_openrouter_80pct_of_deposit_spent_raises_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            openrouter_credits={'totalCredits': 10.0, 'totalUsage': 8.0, 'remaining': 2.0},
            openrouter_api_key_set=True,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'budget')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('80%', alerts[0]['message'])
        self.assertIn('8.00', alerts[0]['message'])
        self.assertIn('10.00', alerts[0]['message'])

    def test_openrouter_90pct_of_deposit_spent_raises_critical(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            openrouter_credits={'totalCredits': 10.0, 'totalUsage': 9.0, 'remaining': 1.0},
            openrouter_api_key_set=True,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['severity'], 'critical')
        self.assertIn('90%', alerts[0]['message'])

    def test_openrouter_below_80pct_spent_raises_nothing(self):
        self.assertEqual(serve._health_alerts_for_signals(self._signals(
            openrouter_credits={'totalCredits': 10.0, 'totalUsage': 7.9, 'remaining': 2.1},
            openrouter_api_key_set=True,
        )), [])

    def test_openrouter_no_deposit_skips_the_divide(self):
        # Never-funded account: totalCredits == 0 must not divide by zero or
        # fire a percentage alert.
        self.assertEqual(serve._health_alerts_for_signals(self._signals(
            openrouter_credits={'totalCredits': 0.0, 'totalUsage': 0.0, 'remaining': 0.0},
            openrouter_api_key_set=True,
        )), [])

    def test_openrouter_monthly_budget_does_not_trigger_the_alert(self):
        # The $50 monthly plan is planning context only -- a large plan with a
        # healthy live deposit raises nothing.
        self.assertEqual(serve._health_alerts_for_signals(self._signals(
            openrouter_credits={'totalCredits': 10.0, 'totalUsage': 1.0, 'remaining': 9.0},
            openrouter_monthly_budget_usd=50.0,
            openrouter_api_key_set=True,
        )), [])

    def test_openrouter_probe_unavailable_with_key_raises_info(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            openrouter_credits=None,
            openrouter_api_key_set=True,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['severity'], 'info')
        self.assertIn('probe unavailable', alerts[0]['message'])


class AgingInFlightWork(unittest.TestCase):
    """Admin gap 1: the health snapshot's mirror of sim's stale-work sweep --
    counts non-bug tasks wedged past their budget (walking forever, or still
    working long after workUntil). Pure read over a state blob."""

    def _task(self, status, opened=None, work_until=None, **over):
        base = {'id': 'task-1', 'status': status, 'taskType': 'code',
                'title': 'Fix the weather', 'room': 'pressoffice',
                'createdAt': opened or 0, 'openedAt': opened or 0}
        if work_until is not None:
            base['workUntil'] = work_until
        base.update(over)
        return base

    def test_working_task_long_past_work_until_is_aging(self):
        now_ms = 1_725_000_000_000
        task = self._task('working', opened=now_ms - 1000,
                          work_until=now_ms / 1000 - sim.STALE_WORK_BUDGET_GRACE_S - 60)
        self.assertEqual(serve._aging_in_flight_work({'tasks': {'t1': task}}, now_ms), 1)

    def test_working_task_within_budget_is_not_aging(self):
        now_ms = 1_725_000_000_000
        task = self._task('working', opened=now_ms - 1000,
                          work_until=now_ms / 1000 + 5)
        self.assertEqual(serve._aging_in_flight_work({'tasks': {'t1': task}}, now_ms), 0)

    def test_walking_task_older_than_ceiling_is_aging(self):
        now_ms = 1_725_000_000_000
        task = self._task('walking', opened=now_ms - sim.STALE_WORK_TIMEOUT_MS - 1)
        self.assertEqual(serve._aging_in_flight_work({'tasks': {'t1': task}}, now_ms), 1)

    def test_fresh_walking_task_is_not_aging(self):
        now_ms = 1_725_000_000_000
        task = self._task('walking', opened=now_ms - 1000)
        self.assertEqual(serve._aging_in_flight_work({'tasks': {'t1': task}}, now_ms), 0)

    def test_bugs_shadows_and_reviews_never_count_as_aging(self):
        now_ms = 1_725_000_000_000
        stale = now_ms - sim.STALE_WORK_TIMEOUT_MS - 1
        bugs = self._task('walking', opened=stale, taskType='bug')
        shadow = self._task('working', opened=stale,
                            work_until=now_ms / 1000 - sim.STALE_WORK_BUDGET_GRACE_S - 60,
                            shadow=True)
        review = self._task('working', opened=stale, reviewOf='task-0',
                            work_until=now_ms / 1000 - sim.STALE_WORK_BUDGET_GRACE_S - 60)
        self.assertEqual(serve._aging_in_flight_work(
            {'tasks': {'b': bugs, 's': shadow, 'r': review}}, now_ms), 0)

    def test_missing_or_empty_tasks_dict_counts_zero(self):
        self.assertEqual(serve._aging_in_flight_work({}, 1_725_000_000_000), 0)
        self.assertEqual(serve._aging_in_flight_work({'tasks': {}}, 1_725_000_000_000), 0)

    def test_paused_time_is_excluded_from_walking_age(self):
        now_ms = 1_725_000_000_000
        opened = now_ms - sim.STALE_WORK_TIMEOUT_MS - 60_000
        task = self._task('walking', opened=opened)
        data = {'tasks': {'t1': task},
                '_simPauseWindows': [[opened + 1000, now_ms - 1000]]}
        self.assertEqual(serve._aging_in_flight_work(data, now_ms), 0)

    def test_paused_time_gives_back_working_budget(self):
        now_ms = 1_725_000_000_000
        task = self._task('working', opened=now_ms - 1000,
                          work_until=now_ms / 1000 - sim.STALE_WORK_BUDGET_GRACE_S - 600)
        data = {'tasks': {'t1': task},
                '_simPauseWindows': [[now_ms - 7_200_000, now_ms - 1_000]]}
        self.assertEqual(serve._aging_in_flight_work(data, now_ms), 0)


class HealthDigestMarkdown(unittest.TestCase):
    """Admin gap 1: the periodic health digest is a readable markdown combining
    alerts + Bank + aging work + escalations. Pure text assembly -- testable
    with a synthetic snapshot, no DB or files."""

    def _snapshot(self, **over):
        base = {
            'checked_at': 1_725_000_000,
            'alerts': [{'category': 'bank', 'severity': 'warning',
                        'message': 'over-cap spend on: openrouter'}],
            'bank_used': 12.0, 'bank_cap': 10.0, 'bank_over_cap': ['openrouter'],
            'aging_in_flight_work': 1, 'open_escalations': 0,
        }
        base.update(over)
        return base

    def test_digest_mentions_alerts_bank_aging_and_escalations(self):
        md = serve._health_digest_markdown(self._snapshot())
        self.assertIn('# Think Tank Health Digest', md)
        self.assertIn('1 active alert(s)', md)
        self.assertIn('over-cap', md)
        self.assertIn('Bank:', md)
        self.assertIn('12.00', md)
        self.assertIn('Aging in-flight work:', md)
        self.assertIn('Open escalations:', md)

    def test_digest_no_alerts_and_clean_bank_reads_quiet(self):
        md = serve._health_digest_markdown(self._snapshot(
            alerts=[], bank_used=4.0, bank_cap=100.0, bank_over_cap=[],
            aging_in_flight_work=0, open_escalations=0,
        ))
        self.assertIn('No active alerts.', md)
        self.assertNotIn('OVER CAP', md)
        self.assertIn('0 non-bug task(s)', md)

    def test_digest_names_over_cap_services(self):
        md = serve._health_digest_markdown(self._snapshot())
        self.assertIn('OVER CAP on openrouter', md)


class CoordinationImbalanceScalar(unittest.TestCase):
    """Direct tests of the Reddit-Hot-style decayed signals + imbalance score
    The whole point of the scalar vs the old 24h COUNT ratio is
    that a burst of ceremony is loud while FRESH and fades once it's stale --
    the Sep 26 restart storm (79 escalations, 3 shipped, within an hour)
    should have read loud on day 1 and gone quiet a few days later."""

    NOW = 1_800_000_000.0  # pinned epoch for reproducible decay math

    def test_fresh_events_sum_to_their_count(self):
        self.assertEqual(serve._decayed_signal([self.NOW, self.NOW], self.NOW), 2.0)

    def test_half_life_halves_the_weight(self):
        one_half_life = serve._IMBALANCE_HALF_LIFE_S
        fresh = serve._decayed_signal([self.NOW], self.NOW)
        aged = serve._decayed_signal([self.NOW], self.NOW + one_half_life)
        self.assertAlmostEqual(aged, fresh / 2, places=4)

    def test_two_day_old_events_are_about_one_sixteenth_weight(self):
        two_days = 2 * 86400
        fresh = serve._decayed_signal([self.NOW], self.NOW)
        aged = serve._decayed_signal([self.NOW], self.NOW + two_days)
        self.assertAlmostEqual(aged / fresh, 1 / 16, places=3)

    def test_events_beyond_the_horizon_are_excluded(self):
        beyond = serve._IMBALANCE_HORIZON_S + 3600
        self.assertEqual(serve._decayed_signal([self.NOW - beyond], self.NOW), 0.0)

    def test_balanced_activity_scores_zero(self):
        self.assertAlmostEqual(serve._imbalance_score(50.0, 50.0), 0.0, places=9)
        self.assertAlmostEqual(serve._imbalance_score(0.0, 0.0), 0.0, places=9)

    def test_scores_are_log_compressed_not_count_ratios(self):
        # log10(80) - log10(4) ~= 1.30: a 26x count ratio reads as a single
        # digit score, and the same RELATIVE imbalance at lower volume is
        # smaller -- a 26x gap of 2-vs-0.08 events is noise.
        self.assertAlmostEqual(serve._imbalance_score(79.0, 3.0), 1.30, places=2)
        self.assertLess(serve._imbalance_score(2.0, 2.0 / 26), 0.5)

    def test_decay_monotonically_lowers_the_score_toward_quiet(self):
        # Two days of decay drops the storm's score from 1.30 (warning) to
        # 0.70 (below the warning floor); it reaches true silence once the
        # ceremony SIGNAL itself decays under the 3.0 floor (~3 days for the
        # 79-event storm -- asserted via the snapshot test).
        fresh = serve._imbalance_score(79.0, 3.0)
        aged = serve._imbalance_score(79.0 / 16, 3.0 / 16)  # two days stale
        self.assertGreaterEqual(fresh, serve._COORDINATION_WARNING_SCORE)
        self.assertLess(aged, serve._COORDINATION_WARNING_SCORE)
        self.assertLess(aged, fresh)

    def test_snapshot_pairs_timestamps_with_counts(self):
        # End-to-end through compute_health_snapshot: 5 FRESH ceremony rows
        # (today) vs 0 shipped must produce a coordination alert; the same
        # rows aged 3 days must not.
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            now = time.time()
            fresh_action = serve._CEREMONY_ACTIONS[0]
            for i in range(5):
                conn.execute(
                    'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
                    'VALUES (?, ?, ?, 1, ?, NULL)',
                    ('ben', fresh_action, '{}', now - i * 60),
                )
        snap = serve.compute_health_snapshot()
        self.assertGreaterEqual(snap['ceremony_imbalance_score'], serve._COORDINATION_INFO_SCORE)
        self.assertTrue(any(a['category'] == 'coordination' for a in snap['alerts']))

        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            for i in range(5):
                conn.execute(
                    'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
                    'VALUES (?, ?, ?, 1, ?, NULL)',
                    ('ben', fresh_action, '{}', now - 3 * 86400 - i * 3600),
                )
        snap = serve.compute_health_snapshot()
        self.assertLess(snap['ceremony_signal'], serve._COORDINATION_MIN_CEREMONY_SIGNAL)
        self.assertFalse(any(a['category'] == 'coordination' for a in snap['alerts']))


class TaskHorizonMetrics(unittest.TestCase):
    # Direct test of _task_horizon_metrics -- the METR reliability-at-horizon
    # pairing -- against the module's redirected DB with explicit timestamps
    # (log_action stamps time.time() itself, so insert rows directly).

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')

    def _insert(self, action, task_id, ts):
        import json as _json
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
                'VALUES (?, ?, ?, 1, ?, NULL)',
                ('ben', action, _json.dumps({'taskId': task_id}), ts),
            )

    def test_pairs_assigned_to_completed_and_computes_rates_and_median(self):
        now = 1_000_000.0
        # task-a: assigned 5k s ago, completed 4k s later (over the 1h fast
        # horizon, inside the 24h horizon).
        self._insert('task_assigned', 'task-a', now - 5000)
        self._insert('task_completed', 'task-a', now - 1000)
        # task-b: assigned 2k s ago, completed 1k s later (fast).
        self._insert('task_assigned', 'task-b', now - 2000)
        self._insert('task_completed', 'task-b', now - 1000)
        # task-c: assigned but never completed -- must count AGAINST the rates.
        self._insert('task_assigned', 'task-c', now - 500)
        m = serve._task_horizon_metrics(now)
        self.assertEqual(m['assigned'], 3)
        self.assertEqual(m['completed'], 2)
        self.assertAlmostEqual(m['fast_completion_rate'], 1 / 3, places=4)
        self.assertAlmostEqual(m['horizon_completion_rate'], 2 / 3, places=4)
        # Median of [1000, 4000] seconds = 2500s -> 0.69h (rounded to 2 dp).
        self.assertAlmostEqual(m['median_completion_hours'], 0.69, places=2)

    def test_no_completions_leaves_rates_at_zero_or_none(self):
        now = 1_000_000.0
        self._insert('task_assigned', 'task-a', now - 1000)
        m = serve._task_horizon_metrics(now)
        self.assertEqual(m['assigned'], 1)
        self.assertEqual(m['completed'], 0)
        self.assertIsNone(m['median_completion_hours'])
        self.assertEqual(m['fast_completion_rate'], 0.0)
        self.assertEqual(m['horizon_completion_rate'], 0.0)

    def test_no_rows_yields_empty_metrics(self):
        m = serve._task_horizon_metrics(1_000_000.0)
        self.assertEqual(m['assigned'], 0)
        self.assertIsNone(m['fast_completion_rate'])

    def test_standing_card_echoes_pair_each_completion_with_its_epoch(self):
        # The incident shape: one standing card ("Maintain tooling") is
        # re-assigned every duty cycle and completes between cycles. The old
        # join kept "first completion wins", so every later assignment paired
        # with the PAST completion and the median went (-13.5)h-negative. The
        # card is one distinct task and its evidenced cycles are all fast.
        now = 1_000_000.0
        self._insert('task_assigned', 'duty', now - 7000)
        self._insert('task_completed', 'duty', now - 6900)
        self._insert('task_assigned', 'duty', now - 4000)
        self._insert('task_completed', 'duty', now - 500)
        self._insert('task_assigned', 'duty', now - 100)   # still in flight
        m = serve._task_horizon_metrics(now)
        self.assertEqual(m['assigned'], 1)
        self.assertEqual(m['completed'], 1)
        self.assertEqual(m['fast_completion_rate'], 1.0)
        # Pairs of [100, 3500] -> median 1800s -> 0.5h, never negative.
        self.assertAlmostEqual(m['median_completion_hours'], 0.5, places=2)

    def test_completion_older_than_the_assignment_is_skipped(self):
        # Exact production bug regression: a completion that predates the
        # (windowed) assignment must be skipped, not paired into a negative
        # duration -- and the task still counts against the fast rate
        # because its in-window epoch shows no completion.
        now = 1_000_000.0
        self._insert('task_completed', 'z', now - 8000)
        self._insert('task_assigned', 'z', now - 2000)
        m = serve._task_horizon_metrics(now)
        self.assertEqual(m['assigned'], 1)
        self.assertEqual(m['completed'], 0)
        self.assertEqual(m['fast_completion_rate'], 0.0)
        self.assertIsNone(m['median_completion_hours'])

    def test_fix_spiral_counts_distinct_task_ids_for_same_root_title(self):
        row = ('INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
               'VALUES (?, ?, ?, 1, ?, NULL)')
        now = time.time()
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            for i in range(3):
                conn.execute(row, ('sue', 'task_assigned',
                                   json.dumps({'taskId': 'fix-%d' % i,
                                               'title': 'Fix: Act on mail (trudy)'}), now - 100 - i))
            # one legit non-fix task must not join the spiral
            conn.execute(row, ('sue', 'task_assigned',
                               json.dumps({'taskId': 'ok-1', 'title': 'Act on mail (trudy)'}),
                               now - 90))
        s = serve._task_fix_spirals(now)
        self.assertEqual(s['spirals'], 1)
        self.assertEqual(s['tasks'], 3)
        # a single "Fix:" card is not a spiral
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            conn.execute(row, ('sue', 'task_assigned',
                               json.dumps({'taskId': 'fix-a', 'title': 'Fix: Act on mail (trudy)'}),
                               now - 100))
        self.assertEqual(serve._task_fix_spirals(now)['spirals'], 0)
