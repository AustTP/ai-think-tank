"""Morning report (serve_parts/426): compile, file, and push. Owner
direction 2026-11-21 -- a nightly heartbeat to Telegram at 07:30 ET that
aggregates runs, autonomy share, plans waiting on the player, spend, and
per-village org state. Unlike the alert digest (425), a morning report is
NOT skipped when the window was quiet: 'quiet night' is itself the result."""

import datetime
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
import serve


class MorningReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think-tank-morning-report-')
        patcher = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self._tmp, 'test.db'),
            THINK_TANK_DIR=self._tmp,
            AGENTS_DIR=os.path.join(self._tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self._tmp, 'library'),
            ESCALATIONS_PATH=os.path.join(self._tmp, 'escalations.json'),
            FAILURES_PATH=os.path.join(self._tmp, 'failures.json'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # plan_approvals does NOT read serve.DB_PATH: it resolves its own
        # database from THINK_TANK_DB (env) and, with no explicit override,
        # routes through the Postgres adapter when THINK_TANK_DB_URL is set.
        # Without this pin the test would query the live org database (and
        # with a fresh adapter connection, race its CREATE TABLE), so every
        # plan-approvals assertion is pinned to a throwaway SQLite file.
        self._plans_db = os.path.join(self._tmp, 'plans.db')
        env_patcher = unittest.mock.patch.dict(
            os.environ, {'THINK_TANK_DB': self._plans_db})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        serve.init_db()

    def _seed_action(self, agent_id, action='tool_call', ts=None, details=None):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details,'
                ' authorized, ts) VALUES (?, ?, ?, 1, ?)',
                (agent_id, action,
                 json.dumps(details or {}), ts or time.time()))

    def test_compile_counts_runs_and_failures(self):
        now = time.time()
        self._seed_action('alpha', 'tool_call', ts=now - 60)
        self._seed_action('alpha', 'tool_call', ts=now - 120)
        self._seed_action('beta', 'failed_work_dropped', ts=now - 180)
        self._seed_action('alpha', 'tool_call', ts=now - 13 * 3600)  # prior window
        text, stats = serve.compile_morning_report(now=now)
        self.assertEqual(stats['runs'], 3)
        self.assertEqual(stats['prevRuns'], 1)
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['deltaPct'], 200)
        self.assertIn('Runs: 3', text)
        self.assertIn('alpha: 2 actions', text)

    def _seed_village_tables(self):
        # villages_agents / villages_teams are created lazily by the
        # heartbeat handlers (194/195), not by init_db -- mirror their DDL.
        with serve._db() as conn:
            conn.execute(
                'CREATE TABLE IF NOT EXISTS villages_agents ('
                ' village_id TEXT NOT NULL, agent_id TEXT NOT NULL,'
                ' name TEXT, role TEXT, room TEXT, updated_at REAL,'
                ' PRIMARY KEY (village_id, agent_id))')
            conn.execute(
                'CREATE TABLE IF NOT EXISTS villages_teams ('
                ' village_id TEXT NOT NULL, team_id TEXT NOT NULL,'
                ' director_id TEXT, updated_at REAL,'
                ' PRIMARY KEY (village_id, team_id))')

    def test_compile_lists_villages(self):
        now = time.time()
        self._seed_village_tables()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO villages_registry (id, name, api_base,'
                ' created_at, last_heartbeat_at, last_status)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                ('v1', 'Corda', 'http://x', now - 100, now - 60, 'ok'))
            conn.execute(
                'INSERT INTO villages_agents (village_id, agent_id, name,'
                ' role, room, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                ('v1', 'a1', 'worker', 'role', 'room', now))
        text, stats = serve.compile_morning_report(now=now)
        self.assertEqual(stats['villages'], 1)
        self.assertEqual(stats['agents'], 1)
        self.assertIn('Corda: 1 agents, live', text)

    def test_compile_marks_quiet_village(self):
        now = time.time()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO villages_registry (id, name, api_base,'
                ' created_at, last_heartbeat_at, last_status)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                ('v2', 'Ghost', 'http://x', now - 100, now - 7200, 'ok'))
        text, stats = serve.compile_morning_report(now=now)
        self.assertEqual(stats['quietVillages'], 1)
        self.assertIn('Ghost: 0 agents, quiet >1h', text)

    def test_compile_counts_pending_plans(self):
        now = time.time()
        from world.core import plan_approvals
        plan_approvals.propose('goal-a', 'route', {'payload': True},
                               'agent-x')
        plan_approvals.propose('goal-b', 'route', {'payload': True},
                               'agent-x')
        text, stats = serve.compile_morning_report(now=now)
        self.assertEqual(stats['pendingPlans'], 2)
        self.assertGreaterEqual(stats['oldestPendingH'], 0.0)
        self.assertIn('Waiting on you: 2', text)

    def test_compile_spend_window_sums_byday(self):
        now = time.time()
        day = datetime.datetime.fromtimestamp(
            now, datetime.timezone.utc).strftime('%Y-%m-%d')
        ledger = {'openrouter': {'byDay': {day: 4.25}}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read',
                                        return_value=ledger), \
                unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 1600):
            text, stats = serve.compile_morning_report(now=now)
        self.assertEqual(stats['spentWindow'], 4.25)
        self.assertEqual(stats['capUsd'], 1600.0)
        self.assertIn('Spent in window: $4.25 (month cap $1600)', text)

    def test_send_files_and_pushes(self):
        now = time.time()
        self._seed_action('alpha', 'tool_call', ts=now - 60)
        with unittest.mock.patch.object(
                serve, '_telegram_send_text_sync', return_value=1) as send:
            rel = serve.send_morning_report(now=now)
        self.assertTrue(rel)
        target = os.path.join(serve.LIBRARY_DIR, rel)
        self.assertTrue(os.path.isfile(target))
        send.assert_called_once()
        self.assertIn('Runs:', send.call_args[0][0])
        with serve._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM action_log "
                "WHERE action='morning_report_sent'").fetchone()
        self.assertEqual(row[0], 1)

    def test_send_returns_none_without_telegram(self):
        with unittest.mock.patch.object(
                serve, '_telegram_send_text_sync', return_value=0):
            rel = serve.send_morning_report()
        self.assertIsNone(rel)

    def test_next_run_delay_is_positive_and_hourly_target(self):
        et = serve._morning_et_now()
        delay = serve._morning_next_run_delay_seconds(
            now_ts=et.timestamp())
        self.assertGreaterEqual(delay, 1.0)
        self.assertLessEqual(delay, 24 * 3600)
        target = et + datetime.timedelta(seconds=delay)
        hour, minute = serve._morning_report_hour_et()
        self.assertEqual(target.hour, hour)
        self.assertEqual(target.minute, minute)
        self.assertEqual(target.second, 0)

    def test_enabled_env_gates(self):
        with unittest.mock.patch.dict(os.environ,
                                      {'MORNING_REPORT_ENABLED': '0'}):
            self.assertFalse(serve._morning_report_enabled())
        with unittest.mock.patch.dict(os.environ,
                                      {'MORNING_REPORT_ENABLED': 'true'}):
            self.assertTrue(serve._morning_report_enabled())
        self.assertTrue(serve._morning_report_enabled())


if __name__ == '__main__':
    unittest.main()


class MorningReportCounterMetrics(MorningReport):
    """Counter-metrics + stall exit (202-sub-note 2>/dev/null)."""

    def setUp(self):
        super().setUp()

    def test_scoreboard_counts_reverts_and_lane_fails(self):
        now = int(time.time())
        self._seed_action('a1', 'change_lane_merged', ts=now - 3600)
        self._seed_action('a1', 'selfedit_revert', ts=now - 3600)
        self._seed_action('a1', 'change_lane_failed', ts=now - 7200)
        with serve._db() as conn:
            scores = serve._morning_scoreboard(conn, 0)
        self.assertEqual(scores['prMerged'], 1)
        self.assertEqual(scores['reverts'], 1)
        self.assertEqual(scores['laneFails'], 1)

    def test_stall_line_renders_when_window_is_empty(self):
        now = int(time.time())
        text, stats = serve.compile_morning_report(now=now)
        self.assertIn('!! STALL', text)
