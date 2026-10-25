"""Gap-cluster F coverage tests for serve.py.

Covers the chrome/screenshot helpers, sandbox backups, page probe,
escalations, activity feed, decision/review calibration, weekly review,
health/ops metrics, model-tier and jev endpoints, auth endpoints, _http_json,
_arm_idle_and_run, api_log, post_report, _stage_released_work, execute/pipeline,
the youtube-transcript endpoint, and the youtube transcript helpers.

Same isolation discipline as tests/test_serve.py: module-wide patcher redirects
every real DB / library / passport path into a throwaway temp dir, and a fresh
in-memory DB schema is created in setUpModule.  TestClient is used bare (no
context manager) and serve.get_state_from_db / save_state_to_db / log_action /
verify_session are patched around requests.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
import urllib.error

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve
import sim

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-serve-gap-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class _LoopExit(Exception):
    pass


class _FakePageProbeHarness:
    """Programmable fake for sync_playwright().chromium.launch()."""

    def __init__(self, base_globals=None, probe_error=False, custom_error=False, click_error=None):
        self.base_globals = base_globals or ['a', 'b']
        self.probe_error = probe_error
        self.custom_error = custom_error
        self.click_error = click_error

    def build(self):
        ctx = unittest.mock.MagicMock()
        ctx.__enter__.return_value = ctx
        browser = ctx.chromium.launch.return_value
        base = unittest.mock.MagicMock()
        page = unittest.mock.MagicMock()
        browser.new_page.side_effect = [base, page]
        base.evaluate.return_value = list(self.base_globals)
        page.on = unittest.mock.Mock()
        page.keyboard = unittest.mock.Mock()
        page.keyboard.press = unittest.mock.Mock()
        page.wait_for_timeout = unittest.mock.Mock()

        def _evaluate(expr, arg=None):
            if arg is not None:
                if self.custom_error:
                    raise RuntimeError('custom eval boom')
                return [{'name': 'custom1', 'type': 'boolean', 'value': True}]
            if 'Object.keys(window)' in expr:
                return list(self.base_globals) + ['custom1']
            if self.probe_error:
                raise RuntimeError('probe boom')
            return 'RESULT:' + expr

        page.evaluate.side_effect = _evaluate
        if self.click_error:
            page.click.side_effect = RuntimeError(self.click_error)
        return ctx


class TaskHorizonMetrics(unittest.TestCase):
    """_task_horizon_metrics."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')

    def _insert(self, action, details, ts):
        with serve._db() as conn:
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('eli', action, details, ts))

    def test_metrics_with_errors_and_in_flight_tasks(self):
        now = time.time()
        # Completed tasks (odd count -> median branch for odd n).
        self._insert('task_assigned', json.dumps({'taskId': 't1'}), now - 3600)
        self._insert('task_completed', json.dumps({'taskId': 't1'}), now - 2000)
        self._insert('task_assigned', json.dumps({'taskId': 't3'}), now - 18000)
        self._insert('task_completed', json.dumps({'taskId': 't3'}), now - 100)
        self._insert('task_assigned', json.dumps({'taskId': 't5'}), now - 7200)
        self._insert('task_completed', json.dumps({'taskId': 't5'}), now - 6500)
        # Never completed -> counts against the rates.
        self._insert('task_assigned', json.dumps({'taskId': 't2'}), now - 1000)
        # Corrupt / missing taskId rows skipped entirely.
        self._insert('task_completed', 'not json{{', now - 500)
        self._insert('task_assigned', 'not json{{', now - 500)
        self._insert('task_assigned', json.dumps({}), now - 500)

        m = serve._task_horizon_metrics(now)
        self.assertEqual(m['assigned'], 4)
        self.assertEqual(m['completed'], 3)
        self.assertEqual(m['median_completion_hours'], 0.44)
        self.assertEqual(m['fast_completion_rate'], 0.5)
        self.assertEqual(m['horizon_completion_rate'], 0.75)

    def test_metrics_with_no_assignments(self):
        m = serve._task_horizon_metrics(time.time())
        self.assertEqual(m['assigned'], 0)
        self.assertIsNone(m['median_completion_hours'])
        self.assertIsNone(m['fast_completion_rate'])



class HealthSnapshot(unittest.TestCase):
    """compute_health_snapshot."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM kv_state')
            conn.execute('DELETE FROM action_log')
            conn.execute('DELETE FROM decision_tape')
            conn.execute('DELETE FROM model_tiers')
            conn.execute('DELETE FROM review_judge_calibration')

    def test_db_failure_returns_critical_alert(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db unreachable')):
            snap = serve.compute_health_snapshot()
        self.assertFalse(snap['db_ok'])
        self.assertEqual(snap['alerts'][0]['severity'], 'critical')

    def test_empty_state_uses_defaults(self):
        snap = serve.compute_health_snapshot()
        self.assertTrue(snap['db_ok'])
        self.assertFalse(snap['think_tank_active'])
        self.assertEqual(snap['work_queue_size'], 0)
        self.assertEqual(snap['agents_count'], 0)
        self.assertEqual(snap['bank_over_cap'], [])

    def test_full_snapshot_with_live_state(self):
        now = time.time()
        blob = {
            'workQueue': [{'title': 'due'}, {'notBefore': 10 ** 18}],
            'agents': {'a': {}, 'b': {}},
            'tasks': {},
            '_pendingEscalation': True,
            '_escalatedProducts': {'p1': {}},
            'products': {},
        }
        with serve._db() as conn:
            conn.execute('INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
                         (json.dumps(blob), now))
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('a', 'work_item_abandoned', None, now - 100))
            for _ in range(6):
                conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                             ('a', 'login_failed', None, now - 100))
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('a', 'browse', json.dumps({'decision': 'blocked'}), now - 100))
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('a', 'execute', json.dumps({'decision': 'allowed_but_fetch_failed'}), now - 100))
            for _ in range(3):
                conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                             ('a', 'review_escalate', None, now - 100))
            for _ in range(2):
                conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                             ('a', 'task_completed', None, now - 100))
            for _ in range(61):
                conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                             ('eli', 'browse', None, now - 10))
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('eli', 'refinement_carryaway',
                          json.dumps({'selfProposedRejected': True}), now - 100))
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('eli', 'quality_gate_reject', None, now - 100))
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('eli', 'quality_gate_reject', json.dumps({'redPipelineEscaped': True}),
                          now - 100))
            # Grader calibration: Jev verdict vs mechanical pipeline anchor.
            # meets/meets + meets/fails + fails/fails -> agreement 2/3,
            # kappa 0.4 (see test_judge_gate_calibration for the math).
            for row in [('repo', 'meets', 0.9, 'meets', 1),
                        ('repo', 'meets', 0.8, 'fails', 0),
                        ('opening', 'fails', 0.9, 'fails', 1)]:
                conn.execute(
                    'INSERT INTO review_judge_calibration (ts, section, judge_verdict, judge_confidence, anchor_verdict, agree) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (now - 100, row[0], row[1], row[2], row[3], row[4]))
            for ok in (1, 0, 0):
                conn.execute(
                    'INSERT INTO decision_tape (ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (now - 100, 'escalation', 'm', 'p', 'c', 'allow', 0.9, 0.001, '{}', ok))
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, chosen_at) '
                'VALUES (?, ?, ?, ?, ?)',
                ('low', 's', 'S', 0.1, now))
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('eli', 'task_assigned', json.dumps({'taskId': 't1'}), now - 3600))
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         ('eli', 'task_completed', json.dumps({'taskId': 't1'}), now - 2000))

        bank_view = {
            'a': {'service': 'a', 'over': True, 'used': 3.0, 'cap': 2.0},
            'b': {'service': 'b', 'over': False, 'used': 1.0, 'cap': 5.0},
        }
        with unittest.mock.patch.object(serve, '_bank_budget_view', return_value=bank_view), \
             unittest.mock.patch.object(serve, '_dormant', return_value=False):
            snap = serve.compute_health_snapshot()
        self.assertTrue(snap['db_ok'])
        self.assertTrue(snap['think_tank_active'])
        self.assertEqual(snap['work_queue_due_size'], 1)
        self.assertEqual(snap['agents_count'], 2)
        self.assertEqual(snap['open_escalations'], 2)
        self.assertEqual(snap['bank_over_cap'], ['a'])
        self.assertEqual(snap['bank_used'], 4.0)
        self.assertEqual(snap['bank_cap'], 7.0)
        self.assertEqual(snap['work_items_abandoned_last_24h'], 1)
        self.assertEqual(snap['login_failures_last_hour'], 6)
        self.assertEqual(snap['blocked_or_failed_actions_last_hour'], {'browse': 1, 'execute': 1})
        self.assertEqual(snap['tool_volume_by_agent_last_15m']['eli'], 61)
        self.assertEqual(snap['jev_decision_attempts_last_hour'], 3)
        self.assertEqual(snap['jev_decision_failures_last_hour'], 2)
        self.assertEqual(snap['self_proposed_rejected_last_24h'], 1)
        self.assertEqual(snap['red_pipeline_rejects_last_24h'], 2)
        self.assertEqual(snap['red_pipeline_escapes_last_24h'], 1)
        self.assertEqual(snap['ceremony_actions_last_24h'], 3)
        self.assertEqual(snap['progress_actions_last_24h'], 3)
        # The 3 review_escalate rows are rework loops; with 3 progress actions
        # the straight-through rate is 50%. No taskId/title in their details,
        # so no ledger spend attaches to them.
        self.assertEqual(snap['rework_loops_last_24h'], 3)
        self.assertEqual(snap['looped_tasks_last_24h'], 0)
        self.assertAlmostEqual(snap['loop_cost_usd_last_24h'], 0.0)
        self.assertAlmostEqual(snap['straight_through_rate'], 0.5)
        self.assertEqual(snap['review_grader_samples'], 3)
        self.assertAlmostEqual(snap['review_grader_agreement_rate'], 0.6667, places=3)
        self.assertAlmostEqual(snap['review_grader_kappa'], 0.4, places=3)
        self.assertEqual(snap['task_assigned_last_24h'], 1)
        self.assertEqual(snap['task_completed_last_24h'], 1)
        self.assertIn('low', snap['model_tier_bands'])
        self.assertIn('mid', snap['missing_model_tier_bands'])
        self.assertTrue(any(a['category'] == 'behavior' for a in snap['alerts']))

    def test_rework_metrics_prices_distinct_looped_tasks_from_ledger(self):
        # 4 requeue rows for the same task = 4 loops, 1 distinct task, and the
        # task's per-task ledger bucket counts once toward the loop cost.
        now = time.time()
        with serve._db() as conn:
            for _ in range(4):
                conn.execute(
                    'INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                    ('a', 'task_review_requeued',
                     json.dumps({'taskId': 't9', 'title': 'Loop me'}), now - 100))
        ledger = {'__task__/t9': {'used': 2.5, 'calls': 3}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            m = serve._rework_metrics(now)
        self.assertEqual(m['loops'], 4)
        self.assertEqual(m['looped_tasks'], 1)
        self.assertAlmostEqual(m['loop_cost_usd'], 2.5)

    def test_rework_metrics_falls_back_to_title_key(self):
        # Some rework rows carry only the title; the ledger's title-keyed
        # bucket (the old per-task keying) still prices the loop.
        now = time.time()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                ('a', 'story_vetoed', json.dumps({'title': '[STORY] Some story'}), now - 100))
        ledger = {'[STORY] Some story': {'used': 1.25, 'calls': 5}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            m = serve._rework_metrics(now)
        self.assertEqual(m['loops'], 1)
        self.assertEqual(m['looped_tasks'], 1)
        self.assertAlmostEqual(m['loop_cost_usd'], 1.25)

    def test_rework_metrics_outside_window_is_ignored(self):
        now = time.time()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                ('a', 'review_escalate', json.dumps({'taskId': 'old'}), now - serve._REWORK_WINDOW_S - 10))
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={'__task__/old': {'used': 9.0}}):
            m = serve._rework_metrics(now)
        self.assertEqual(m['loops'], 0)
        self.assertEqual(m['looped_tasks'], 0)
        self.assertEqual(m['loop_cost_usd'], 0.0)



class HealthAlertsPersist(unittest.TestCase):
    """_persist_new_health_alerts."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM health_alerts')

    def test_empty_alerts_returns_empty(self):
        self.assertEqual(serve._persist_new_health_alerts([]), [])

    def test_new_alerts_persisted_and_deduped(self):
        alerts = [{'category': 'jev', 'severity': 'warning', 'message': 'm1'}]
        persisted = serve._persist_new_health_alerts(alerts)
        self.assertEqual(persisted, alerts)
        with serve._db() as conn:
            rows = conn.execute('SELECT category, severity FROM health_alerts').fetchall()
        self.assertEqual(rows, [('jev', 'warning')])
        # Same (category, severity) within the window -> deduped.
        self.assertEqual(serve._persist_new_health_alerts(alerts), [])



class HealthDigest(unittest.TestCase):
    """_write_health_digest."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM settings')
        self.admin_dir = os.path.join(_MODULE_TMP_DIR, 'library', 'admin')

    def tearDown(self):
        shutil.rmtree(self.admin_dir, ignore_errors=True)

    def _snapshot(self):
        return {'checked_at': time.time(), 'alerts': [{'category': 'jev', 'severity': 'warning',
                                                       'message': 'x'}],
                'bank_used': 1.0, 'bank_cap': 5.0, 'bank_over_cap': [],
                'aging_in_flight_work': 0, 'open_escalations': 1,
                'review_grader_samples': 3, 'review_grader_agreement_rate': 0.6667,
                'review_grader_kappa': 0.4}

    def test_skips_when_digest_recently_written(self):
        with serve._db() as conn:
            conn.execute('INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)',
                         (serve._DIGEST_STAMP_KEY, str(time.time()), time.time()))
        serve._write_health_digest(self._snapshot())
        self.assertFalse(os.path.isdir(self.admin_dir))

    def test_writes_digest_file_and_stamp(self):
        serve._write_health_digest(self._snapshot())
        files = os.listdir(self.admin_dir)
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.admin_dir, files[0])) as f:
            content = f.read()
        self.assertIn('Think Tank Health Digest', content)
        self.assertIn('Open escalations:** 1', content)
        self.assertIn('**Rework:**', content)
        self.assertIn('**Grader:** 3 anchored review grade(s), agreement 67%, kappa 0.40', content)
        with serve._db() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key = ?',
                               (serve._DIGEST_STAMP_KEY,)).fetchone()
        self.assertIsNotNone(row)

    def test_write_failure_is_swallowed(self):
        with unittest.mock.patch.object(serve, '_health_digest_markdown',
                                        side_effect=RuntimeError('boom')):
            serve._write_health_digest(self._snapshot())  # must not raise
        with serve._db() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key = ?',
                               (serve._DIGEST_STAMP_KEY,)).fetchone()
        self.assertIsNotNone(row)

    def test_prune_keeps_newest_digests(self):
        os.makedirs(self.admin_dir, exist_ok=True)
        for i in range(serve._DIGEST_KEEP + 5):
            with open(os.path.join(self.admin_dir, f'health-{1000 + i}.md'),
                      'w') as f:
                f.write('x')
        serve._prune_old_health_digests()
        files = sorted(os.listdir(self.admin_dir))
        self.assertEqual(len(files), serve._DIGEST_KEEP)
        self.assertEqual(files[0], f'health-{1005}.md')
        self.assertEqual(files[-1], f'health-{1000 + serve._DIGEST_KEEP + 4}.md')

    def test_corrupt_stamp_self_heals_and_writes(self):
        # CAST('garbage' AS REAL) = 0.0 <= now - INTERVAL, so a
        # corrupt stamp must be treated as stale and repaired.
        with serve._db() as conn:
            conn.execute('INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)',
                         (serve._DIGEST_STAMP_KEY, 'garbage', time.time()))
        serve._write_health_digest(self._snapshot())
        self.assertEqual(len(os.listdir(self.admin_dir)), 1)
        with serve._db() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key = ?',
                               (serve._DIGEST_STAMP_KEY,)).fetchone()
        self.assertAlmostEqual(float(row[0]), time.time(), delta=60)



class HealthEndpoints(unittest.TestCase):
    """/api/health, /api/sim/status, /api/sim/agents, /api/health/alerts, /api/activity/summary."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            conn.execute('DELETE FROM health_alerts')

    def test_health_returns_snapshot(self):
        snapshot = {'checked_at': time.time(), 'db_ok': True, 'alerts': []}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'compute_health_snapshot', return_value=snapshot):
            c = TestClient(serve.app)
            r = c.get('/api/health')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['db_ok'])

    def test_sim_status_error_path(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        side_effect=RuntimeError('boom')):
            c = TestClient(serve.app)
            r = c.get('/api/sim/status')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['running'])
        self.assertIn('boom', r.json()['error'])

    def test_sim_status_live_state(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'tick': 7}), \
             unittest.mock.patch.object(sim._engine, 'status',
                                        return_value={'running': True, 'tick': 7}):
            c = TestClient(serve.app)
            r = c.get('/api/sim/status')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['running'])

    def test_sim_agents_error_path(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        side_effect=RuntimeError('boom')):
            c = TestClient(serve.app)
            r = c.get('/api/sim/agents')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['owner'], 'unknown')

    def test_sim_agents_empty_state(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None):
            c = TestClient(serve.app)
            r = c.get('/api/sim/agents')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'owner': 'client', 'tick': 0, 'agents': {}})

    def test_sim_agents_live_state(self):
        state = {'sim': {'owner': 'server', 'tick': 3, 'agents': {'eli': {'x': 1}}}}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state):
            c = TestClient(serve.app)
            r = c.get('/api/sim/agents')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['owner'], 'server')
        self.assertEqual(r.json()['tick'], 3)
        self.assertEqual(r.json()['agents'], {'eli': {'x': 1}})

    def test_health_alerts_endpoint(self):
        with serve._db() as conn:
            conn.execute('INSERT INTO health_alerts (category, severity, message, ts) VALUES (?, ?, ?, ?)',
                         ('jev', 'warning', 'm1', 100.0))
            conn.execute('INSERT INTO health_alerts (category, severity, message, ts) VALUES (?, ?, ?, ?)',
                         ('db', 'critical', 'm2', 200.0))
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/health/alerts?limit=50')
        self.assertEqual(r.status_code, 200)
        alerts = r.json()['alerts']
        self.assertEqual([a['category'] for a in alerts], ['db', 'jev'])

    def test_activity_summary(self):
        serve.log_action('eli', 'curl', {})
        serve.log_action('eli', 'curl', {})
        serve.log_action('eli', 'execute', {})
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/activity/summary', params={'agentId': 'eli'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['agentId'], 'eli')
        self.assertEqual(r.json()['counts']['curl'], 2)
        self.assertEqual(r.json()['counts']['execute'], 1)



class CatalogBenchmarkIngest(unittest.TestCase):
    """_ingest_catalog_benchmarks auto-fills model_benchmark_scores from the
    OpenRouter catalog's own Artificial Analysis capability indices on every
    sync, so the benchmark table stays fresh as models emerge and their scores
    update -- and dies with a model that leaves the catalog via the existing
    removal purge. Player-entered scores under other benchmark names (MMLU,
    SWE-bench, HLE, MMMU) are separate rows and must be untouched."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_benchmark_scores')
            conn.execute('DELETE FROM model_catalog')

    def _model(self, mid='bench/sample', aa=None, design=None):
        m = {'id': mid, 'pricing': {'prompt': 0.0000001, 'completion': 0.0000001}}
        if aa is not None:
            m['benchmarks'] = {'artificial_analysis': aa}
            if design is not None:
                m['benchmarks']['design_arena'] = design
        elif design is not None:
            m['benchmarks'] = {'design_arena': design}
        return m

    def test_ingest_writes_aa_indices_with_model_page_citation(self):
        written = serve._ingest_catalog_benchmarks([
            self._model('vendor/model-a', {'intelligence_index': 34.3,
                                           'coding_index': 69.1,
                                           'agentic_index': 41}),
        ])
        self.assertEqual(written, 3)
        with serve._db() as conn:
            rows = conn.execute(
                'SELECT model_id, benchmark, score, source_url, evaluator FROM model_benchmark_scores '
                'ORDER BY benchmark').fetchall()
        self.assertEqual(rows, [
            ('vendor/model-a', 'agentic_index', 41.0, 'https://openrouter.ai/vendor/model-a', 'artificial_analysis'),
            ('vendor/model-a', 'coding_index', 69.1, 'https://openrouter.ai/vendor/model-a', 'artificial_analysis'),
            ('vendor/model-a', 'intelligence_index', 34.3, 'https://openrouter.ai/vendor/model-a', 'artificial_analysis'),
        ])

    def test_ingest_skips_models_without_aa_data(self):
        self.assertEqual(serve._ingest_catalog_benchmarks([self._model()]), 0)
        self.assertEqual(serve._ingest_catalog_benchmarks([self._model(design={'website': [{'elo': 1242}]})]), 0)
        with serve._db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM model_benchmark_scores').fetchone()[0], 0)

    def test_ingest_upserts_on_rerun(self):
        self.assertEqual(serve._ingest_catalog_benchmarks([
            self._model('vendor/model-a', {'intelligence_index': 30.0})]), 1)
        self.assertEqual(serve._ingest_catalog_benchmarks([
            self._model('vendor/model-a', {'intelligence_index': 44.0})]), 1)
        with serve._db() as conn:
            row = conn.execute('SELECT score FROM model_benchmark_scores '
                               'WHERE model_id=? AND benchmark=?',
                               ('vendor/model-a', 'intelligence_index')).fetchone()
        self.assertEqual(row[0], 44.0)

    def test_band_benchmarks_use_catalog_metrics(self):
        # The bands now score against the catalog's own Artificial Analysis
        # indices; vision keeps player-entered MMMU (the catalog carries no
        # image-comprehension metric to back it). Each entry is an ordered
        # metric CHAIN -- primary first, the HF Open LLM MMLU-Pro fallback
        # second for low/mid so open-weights models AA never measured can
        # still compete when the primary pool is empty.
        self.assertEqual(serve.BAND_BENCHMARK['low'], ('intelligence_index', 'MMLU-Pro'))
        self.assertEqual(serve.BAND_BENCHMARK['sublow'], ('intelligence_index', 'MMLU-Pro'))
        self.assertEqual(serve.BAND_BENCHMARK['mid'], ('intelligence_index', 'MMLU-Pro'))
        self.assertEqual(serve.BAND_BENCHMARK['high'], ('agentic_index',))
        self.assertEqual(serve.BAND_BENCHMARK['coding'], ('coding_index',))
        self.assertEqual(serve.BAND_BENCHMARK['vision'], ('MMMU',))

    def test_sync_model_catalog_ingests_then_carries_into_catalog_row(self):
        models = [self._model('vendor/model-a', {'intelligence_index': 34.3})]
        purged = serve._sync_model_catalog(models)
        self.assertEqual(purged, 0)
        with serve._db() as conn:
            row = conn.execute('SELECT scores FROM model_catalog WHERE model_id=?',
                               ('vendor/model-a',)).fetchone()
        self.assertEqual(json.loads(row[0]),
                         {'intelligence_index': {'score': 34.3, 'evaluator': 'artificial_analysis'}})

    def test_sync_purges_models_gone_from_catalog_with_their_scores(self):
        # Regression: the purge's params were swapped (grace floor went into
        # the NOT IN list, a model id into the last_seen comparison, so
        # `number < text` matched EVERY row and a refresh wiped the whole
        # catalog). A model absent from the fresh snapshot for longer than
        # one refresh interval must be purged from BOTH tables; a present
        # model must survive untouched.
        stale = time.time() - serve.MODEL_TIER_REFRESH_INTERVAL_S - 1
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_catalog (model_id, name, prompt_price, completion_price, price_per_m, image_capable, first_seen, last_seen) '
                'VALUES (?,?,?,?,?,?,?,?)',
                ('gone/model', 'Gone', 1e-7, 1e-7, 0.0002, 0, stale, stale))
            conn.execute(
                'INSERT INTO model_benchmark_scores (model_id, benchmark, score, source_url, checked_at) '
                'VALUES (?,?,?,?,?)', ('gone/model', 'intelligence_index', 30.0, 'http://x', stale))
        purged = serve._sync_model_catalog([self._model('vendor/model-a', {'intelligence_index': 34.3})])
        self.assertEqual(purged, 1)
        with serve._db() as conn:
            self.assertIsNone(conn.execute('SELECT model_id FROM model_catalog WHERE model_id=?',
                                           ('gone/model',)).fetchone())
            self.assertIsNone(conn.execute('SELECT model_id FROM model_benchmark_scores WHERE model_id=?',
                                           ('gone/model',)).fetchone())
            self.assertIsNotNone(conn.execute('SELECT model_id FROM model_catalog WHERE model_id=?',
                                              ('vendor/model-a',)).fetchone())
            self.assertIsNotNone(conn.execute('SELECT model_id FROM model_benchmark_scores WHERE model_id=?',
                                              ('vendor/model-a',)).fetchone())



class BenchmarkDataSources(unittest.TestCase):
    """The data_sources registry: operator PUT/GET endpoints, hot application
    into the AA leaderboard and HF Open LLM backfills, and the evaluator
    column that records WHO measured each score."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM data_sources')
            conn.execute('DELETE FROM model_benchmark_scores')
            conn.execute('DELETE FROM model_catalog')

    def tearDown(self):
        # Leave no configured source behind: a later test's _sync_model_catalog
        # must never attempt a real network fetch.
        with serve._db() as conn:
            conn.execute('DELETE FROM data_sources')

    @staticmethod
    def _model(mid, hf_id=None, price='0.0000001'):
        m = {'id': mid, 'name': mid.replace('/', ': ').title(),
             'pricing': {'prompt': price, 'completion': price}}
        if hf_id:
            m['hugging_face_id'] = hf_id
        return m

    def _set(self, **sources):
        with serve._db() as conn:
            for key, value in sources.items():
                conn.execute(
                    'INSERT INTO data_sources (key, value, updated_by, updated_at) VALUES (?,?,?,?)',
                    (key, value, 'player', time.time()))

    def test_get_put_require_operator(self):
        c = TestClient(serve.app)
        self.assertEqual(c.get('/api/data-sources').status_code, 401)
        self.assertEqual(c.put('/api/data-sources', json={'sources': {}}).status_code, 401)

    def test_put_round_trips_and_is_read_hot(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.put('/api/data-sources', json={'sources': {
                'aa_leaderboard_url': 'https://aa.test/leaderboard',
                'hf_contents_parquet_url': 'https://hf.test/parquet',
            }})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['updatedBy'], 'player')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/data-sources')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['knownKeys'], list(serve.DATA_SOURCE_KEYS))
        self.assertEqual(body['sources']['aa_leaderboard_url']['value'], 'https://aa.test/leaderboard')
        self.assertEqual(body['sources']['aa_leaderboard_url']['updatedBy'], 'player')
        # The consumer reads the new value on the very next call -- no restart.
        self.assertEqual(serve._get_data_source('aa_leaderboard_url'), 'https://aa.test/leaderboard')

    def test_put_rejects_unknown_key_and_bad_url(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.put('/api/data-sources', json={'sources': {'aa_leaderboard_url': 'x'}})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn('http(s)', r.json()['error'])
            r = c.put('/api/data-sources', json={'sources': {'bogus_key': 'https://x.test'}})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn('unknown data source key', r.json()['error'])
            r = c.put('/api/data-sources', json={'sources': 'not-a-dict'})
            self.assertEqual(r.status_code, 400, r.text)

    def test_put_is_attributed_in_action_log(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.put('/api/data-sources', json={'sources': {'aa_leaderboard_url': 'https://aa.test/lb'}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(log.call_args[0][0], 'player')
        self.assertEqual(log.call_args[0][1], 'data_sources_updated')
        self.assertIn('aa_leaderboard_url', log.call_args[0][2]['sources'])

    def test_aa_ingest_skips_when_unconfigured(self):
        with unittest.mock.patch('urllib.request.urlopen', side_effect=AssertionError('must not fetch')):
            self.assertEqual(serve._ingest_aa_leaderboard([self._model('m/m')]), 0)

    def test_aa_ingest_uses_configured_url_and_evaluator(self):
        self._set(aa_leaderboard_url='https://aa.test/leaderboard')
        html = ('<table><tr><td>MiMo-V2.6-Pro</td><td>1M</td><td>Xiaomi</td>'
                '<td>46</td><td>$0.13</td></tr></table>')
        resp = unittest.mock.MagicMock()
        resp.read.return_value = html.encode()
        resp.__enter__.return_value = resp
        with unittest.mock.patch('urllib.request.urlopen', return_value=resp):
            added = serve._ingest_aa_leaderboard([self._model('xiaomi/mimo-v2.6-pro', 'MiMo-V2.6-Pro')])
        self.assertEqual(added, 1)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT model_id, benchmark, score, source_url, evaluator FROM model_benchmark_scores'
            ).fetchone()
        self.assertEqual(row, ('xiaomi/mimo-v2.6-pro', 'intelligence_index', 46.0,
                               'https://aa.test/leaderboard', 'artificial_analysis'))

    def test_aa_backfill_never_overwrites_catalog_value(self):
        self._set(aa_leaderboard_url='https://aa.test/leaderboard')
        serve.set_model_benchmark_score('xiaomi/mimo-v2.6-pro', 'intelligence_index', 37.9,
                                        'https://openrouter.ai/xiaomi/mimo-v2.6-pro', 'artificial_analysis')
        html = ('<tr><td>MiMo-V2.6-Pro</td><td>1M</td><td>Xiaomi</td><td>46</td><td>$0.13</td></tr>')
        resp = unittest.mock.MagicMock()
        resp.read.return_value = html.encode()
        resp.__enter__.return_value = resp
        with unittest.mock.patch('urllib.request.urlopen', return_value=resp):
            added = serve._ingest_aa_leaderboard([self._model('xiaomi/mimo-v2.6-pro', 'MiMo-V2.6-Pro')])
        self.assertEqual(added, 0)
        with serve._db() as conn:
            score = conn.execute(
                'SELECT score FROM model_benchmark_scores WHERE model_id=? AND benchmark=?',
                ('xiaomi/mimo-v2.6-pro', 'intelligence_index')).fetchone()[0]
        self.assertEqual(score, 37.9)

    def test_hf_ingest_skips_when_unconfigured(self):
        with unittest.mock.patch('urllib.request.urlopen', side_effect=AssertionError('must not fetch')):
            self.assertEqual(serve._ingest_hf_open_llm_fallback([self._model('m/m')]), 0)

    def test_hf_ingest_uses_configured_url_and_evaluator(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest('pyarrow not installed')
        self._set(hf_contents_parquet_url='https://hf.test/parquet',
                  hf_contents_source_url='https://hf.test/contents')
        table = pa.table({'fullname': ['mistralai/Mistral-Nemo-Instruct-2407'], 'MMLU-PRO': [27.97]})
        buf = io.BytesIO()
        pq.write_table(table, buf)
        parquet_bytes = buf.getvalue()
        calls = [{'parquet_files': [{'url': 'https://hf.test/data.parquet'}]}, parquet_bytes]
        def _resp(payload):
            resp = unittest.mock.MagicMock()
            resp.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            resp.__enter__.return_value = resp
            return resp
        with unittest.mock.patch('urllib.request.urlopen',
                                 side_effect=[_resp(calls[0]), _resp(calls[1])]):
            added = serve._ingest_hf_open_llm_fallback(
                [self._model('mistralai/mistral-nemo', hf_id='mistralai/Mistral-Nemo-Instruct-2407')])
        self.assertEqual(added, 1)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT model_id, benchmark, score, source_url, evaluator FROM model_benchmark_scores'
            ).fetchone()
        self.assertEqual(row, ('mistralai/mistral-nemo', 'MMLU-Pro', 27.97,
                               'https://hf.test/contents', 'huggingface_open_llm'))

    def test_hf_fallback_never_clobbers_existing_row(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest('pyarrow not installed')
        self._set(hf_contents_parquet_url='https://hf.test/parquet')
        # A player-entered MMLU-Pro row exists for this model.
        serve.set_model_benchmark_score('mistralai/mistral-nemo', 'MMLU-Pro', 68.4,
                                        'http://player-research', 'player')
        table = pa.table({'fullname': ['mistralai/Mistral-Nemo-Instruct-2407'], 'MMLU-PRO': [27.97]})
        buf = io.BytesIO()
        pq.write_table(table, buf)
        parquet_bytes = buf.getvalue()
        calls = [{'parquet_files': [{'url': 'https://hf.test/data.parquet'}]}, parquet_bytes]
        def _resp(payload):
            resp = unittest.mock.MagicMock()
            resp.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            resp.__enter__.return_value = resp
            return resp
        with unittest.mock.patch('urllib.request.urlopen',
                                 side_effect=[_resp(calls[0]), _resp(calls[1])]):
            added = serve._ingest_hf_open_llm_fallback(
                [self._model('mistralai/mistral-nemo', hf_id='mistralai/Mistral-Nemo-Instruct-2407')])
        self.assertEqual(added, 0)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT score, evaluator FROM model_benchmark_scores WHERE model_id=? AND benchmark=?',
                ('mistralai/mistral-nemo', 'MMLU-Pro')).fetchone()
        self.assertEqual(row, (68.4, 'player'))

    def test_tokenizer_splits_on_dashes_dots_slashes(self):
        # Regression: a hub id like 'meta-llama/llama-3.1-70b-instruct' must
        # tokenize to {meta, llama, 31, 70b, instruct} -- the old removal-based
        # normalizer glued dashes/dots and produced '3170binstruct', so the HF
        # name fallback never matched base open-weights models.
        self.assertEqual(
            serve._benchmark_name_tokens('meta-llama/llama-3.1-70b-instruct'),
            {'meta', 'llama', '3', '1', '70b', 'instruct'})
        self.assertEqual(
            serve._benchmark_name_tokens('OpenAI: GPT-6 Luna Pro'),
            {'openai', 'gpt', '6', 'luna', 'pro'})
        self.assertEqual(serve._benchmark_name_tokens(''), set())

    def test_hf_name_match_without_hugging_face_id(self):
        # A catalog model with NO hugging_face_id still gets its HF fallback
        # score via the normalized name-token match (the shortest hub id whose
        # tokens contain the catalog name's tokens wins -- the closest base).
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest('pyarrow not installed')
        self._set(hf_contents_parquet_url='https://hf.test/parquet')
        table = pa.table({'fullname': ['meta-llama/llama-3.1-70b-instruct'], 'MMLU-PRO': [47.88]})
        buf = io.BytesIO()
        pq.write_table(table, buf)
        parquet_bytes = buf.getvalue()
        calls = [{'parquet_files': [{'url': 'https://hf.test/data.parquet'}]}, parquet_bytes]
        def _resp(payload):
            resp = unittest.mock.MagicMock()
            resp.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            resp.__enter__.return_value = resp
            return resp
        with unittest.mock.patch('urllib.request.urlopen',
                                 side_effect=[_resp(calls[0]), _resp(calls[1])]):
            added = serve._ingest_hf_open_llm_fallback(
                [self._model('meta-llama/llama-3.1-70b-instruct')])
        self.assertEqual(added, 1)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT score, evaluator FROM model_benchmark_scores WHERE model_id=? AND benchmark=?',
                ('meta-llama/llama-3.1-70b-instruct', 'MMLU-Pro')).fetchone()
        self.assertEqual(row, (47.88, 'huggingface_open_llm'))



class ModelTierEndpoints(unittest.TestCase):
    """model_tiers, model_tiers_refresh, model_benchmark_scores get/post."""

    def test_model_tiers_returns_cached(self):
        cached = {'low': {'slug': 's', 'name': 'S', 'price': 0.1}}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_cached_model_tiers', return_value=cached):
            c = TestClient(serve.app)
            r = c.get('/api/model-tiers')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), cached)

    def test_model_tiers_refreshes_when_cold(self):
        fresh = {'low': {'id': 's', 'name': 'S', 'price': 0.1}}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_cached_model_tiers', return_value=None), \
             unittest.mock.patch.object(serve, 'refresh_model_tiers',
                                        new=unittest.mock.AsyncMock(return_value=fresh)):
            c = TestClient(serve.app)
            r = c.get('/api/model-tiers')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['low']['slug'], 's')

    def test_model_tiers_refresh_failure_is_502(self):
        async def _boom():
            raise RuntimeError('no catalog')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_cached_model_tiers', return_value=None), \
             unittest.mock.patch.object(serve, 'refresh_model_tiers', side_effect=_boom):
            c = TestClient(serve.app)
            r = c.get('/api/model-tiers')
        self.assertEqual(r.status_code, 502)
        self.assertIn('no catalog', r.json()['error'])

    def test_model_tiers_refresh_post(self):
        fresh = {'low': {'id': 's', 'name': 'S', 'price': 0.1}}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'refresh_model_tiers',
                                        new=unittest.mock.AsyncMock(return_value=fresh)):
            c = TestClient(serve.app)
            r = c.post('/api/model-tiers/refresh')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['low']['slug'], 's')

    def test_model_tiers_refresh_post_failure_is_502(self):
        async def _boom():
            raise RuntimeError('nope')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'refresh_model_tiers', side_effect=_boom):
            c = TestClient(serve.app)
            r = c.post('/api/model-tiers/refresh')
        self.assertEqual(r.status_code, 502)

    def test_model_tiers_refresh_rejects_worker_key(self):
        # The refresh is an OPERATOR control (player session or a
        # director/admin agent): a worker's key is refused 401 before
        # any catalog walk runs.
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='worker'), \
             unittest.mock.patch.object(serve, '_is_director_or_admin', return_value=False):
            c = TestClient(serve.app)
            r = c.post('/api/model-tiers/refresh')
        self.assertEqual(r.status_code, 401)
        self.assertIn('Unauthorized', r.json()['error'])

    def test_model_tiers_refresh_rate_limited_is_429(self):
        refresh = unittest.mock.AsyncMock()
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'refresh_model_tiers', new=refresh):
            c = TestClient(serve.app)
            r = c.post('/api/model-tiers/refresh')
        self.assertEqual(r.status_code, 429)
        refresh.assert_not_awaited()

    def test_benchmark_scores_get(self):
        scores = {'s': {'MMLU': 0.7}}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_model_benchmark_scores', return_value=scores):
            c = TestClient(serve.app)
            r = c.get('/api/model-benchmark-scores')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['scores'], scores)

    def test_benchmark_scores_post_rejects_agent(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/model-benchmark-scores',
                       json={'agentId': 'eli', 'modelId': 'm', 'benchmark': 'MMLU', 'score': 0.7})
        self.assertEqual(r.status_code, 403)

    def test_benchmark_scores_post_requires_fields(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/model-benchmark-scores', json={'agentId': 'player', 'modelId': 'm'})
            self.assertEqual(r.status_code, 400)
            r = c.post('/api/model-benchmark-scores',
                       json={'agentId': 'player', 'modelId': 'm', 'benchmark': 'MMLU', 'score': 'high'})
            self.assertEqual(r.status_code, 400)

    def test_benchmark_scores_post_success(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'set_model_benchmark_score') as set_score, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/api/model-benchmark-scores',
                       json={'agentId': 'player', 'modelId': 'm', 'benchmark': 'MMLU',
                             'score': 0.7, 'sourceUrl': 'http://x'})
        self.assertEqual(r.status_code, 200, r.text)
        set_score.assert_called_once_with('m', 'MMLU', 0.7, 'http://x', 'player')
        self.assertEqual(log.call_args[0][1], 'model_benchmark_score_recorded')

    def test_benchmark_scores_post_records_explicit_evaluator(self):
        # The evaluator column records WHO measured a score -- the player can
        # label their own hand-entered research, distinct from the automated
        # 'artificial_analysis' / 'huggingface_open_llm' rows.
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'set_model_benchmark_score') as set_score, \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/model-benchmark-scores',
                       json={'agentId': 'player', 'modelId': 'm', 'benchmark': 'MMMU',
                             'score': 0.6, 'sourceUrl': 'http://x', 'evaluator': 'player'})
        self.assertEqual(r.status_code, 200, r.text)
        set_score.assert_called_once_with('m', 'MMMU', 0.6, 'http://x', 'player')
