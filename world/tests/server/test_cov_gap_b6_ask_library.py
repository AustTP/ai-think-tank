"""Coverage-gap tests for serve_parts 250, 290 and 410.

The ask-lane tool dispatch is driven through the real _ask_core with the model
tool loop replaced by a scripted fake; the library download and task-cause
helpers are exercised directly. No real network, model or browser calls.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import unittest.mock as mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402
import sim  # noqa: E402
import content  # noqa: E402
import agents.pulse as pulse  # noqa: E402

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='tt-cov-b6-ask-')
    _MODULE_PATCHER = mock.patch.multiple(
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


def _roster_state():
    return {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'agent-1', 'name': 'Theo', 'role': 'admin', 'isAdmin': True},
            {'id': 'agent-2', 'name': 'Nora', 'role': 'director', 'isDirector': True},
        ],
        'agents': {
            'agent-1': {'name': 'Theo', 'busy': False, 'offDuty': False,
                        'role': 'admin', 'profile': {'mission': 'run it'}},
            'agent-2': {'name': 'Nora', 'busy': False, 'offDuty': False,
                        'role': 'director', 'profile': {'mission': 'lead'}},
        },
        'teams': [{'id': 'team-1', 'name': 'Alpha', 'directorId': 'agent-2'}],
        'workQueue': [],
    }


def _loop_fake(script):
    def fake(model, messages, tools, execute_tool, max_iterations, max_tokens,
             **kwargs):
        for name, args in script:
            execute_tool(name, args)
        return 'final answer'
    return fake


def _run_ask(state, script=None, **kwargs):
    patches = [
        mock.patch.object(sim, '_eligible_candidates', return_value=['agent-2']),
        mock.patch.object(serve, '_resolve_model_tier', return_value='m'),
        mock.patch.object(serve, 'get_or_create_agent_key', return_value='k'),
        mock.patch.object(serve, '_call_agent_tool_loop',
                          _loop_fake(script or [])),
        mock.patch.object(serve, 'idempotent_tool_call',
                          side_effect=lambda aid, tid, raw, name, args:
                          raw(name, args)),
    ]
    for p in patches:
        p.start()
    try:
        return asyncio.run(serve._ask_core(state, 'question'))
    finally:
        for p in reversed(patches):
            p.stop()


class AskCoreToolDispatchTest(unittest.TestCase):
    def test_approved_tools_string_is_normalized(self):
        state = _roster_state()
        with mock.patch.object(sim, '_eligible_candidates',
                               return_value=['agent-2']), \
             mock.patch.object(serve, '_resolve_model_tier', return_value='m'), \
             mock.patch.object(serve, 'get_or_create_agent_key',
                               return_value='k'), \
             mock.patch.object(serve, '_call_agent_tool_loop',
                               _loop_fake([])):
            result = asyncio.run(serve._ask_core(
                state, 'question', approved_tools='team_status'))
        self.assertEqual(result['reply'], 'final answer')

    def test_browser_step_tool(self):
        state = _roster_state()
        step = mock.MagicMock(return_value='STEP-RESULT')
        with mock.patch.object(serve, '_make_browser_step_executor',
                               create=True):
            pass
        with mock.patch.object(content, '_make_browser_step_executor',
                               return_value=step):
            result = _run_ask(state, [('browser_step', {'url': 'http://x'})])
        self.assertEqual(result['reply'], 'final answer')
        self.assertIn('browser_step', result['tools'])

    def test_agent_readiness_tool(self):
        state = _roster_state()
        with mock.patch.object(content, 'audit_agent_readiness',
                               return_value='READY') as readiness:
            result = _run_ask(state, [('audit_agent_readiness', {})])
        self.assertEqual(result['reply'], 'final answer')
        readiness.assert_called_once()

    def test_project_board_tool(self):
        state = _roster_state()
        with mock.patch.object(content, 'project_board',
                               return_value='BOARD') as board:
            result = _run_ask(state, [('project_board', {'team': 'x'})])
        self.assertEqual(result['reply'], 'final answer')
        board.assert_called_once()

    def test_pulse_tool(self):
        state = _roster_state()
        with mock.patch.object(pulse, 'execute_pulse_tool',
                               return_value='PULSE') as execute:
            result = _run_ask(state, [('read_pulse', {})])
        self.assertEqual(result['reply'], 'final answer')
        execute.assert_called_once()

    def test_content_import_failure_is_swallowed(self):
        state = _roster_state()
        saved = content._PROJECT_BOARD_TOOL
        del content._PROJECT_BOARD_TOOL
        try:
            result = _run_ask(state)
        finally:
            content._PROJECT_BOARD_TOOL = saved
        self.assertEqual(result['reply'], 'final answer')


class BoundaryCheckListContentTest(unittest.TestCase):
    def test_list_content_with_bad_text_is_rejected(self):
        with mock.patch.object(serve, 'verify_external_boundaries',
                               return_value=False):
            self.assertFalse(serve._messages_pass_boundary_check(
                [{'role': 'user', 'content': [{'type': 'text', 'text': 'x'}]}]))

    def test_list_content_with_good_text_passes(self):
        with mock.patch.object(serve, 'verify_external_boundaries',
                               return_value=True):
            self.assertTrue(serve._messages_pass_boundary_check(
                [{'role': 'user', 'content': [{'type': 'text', 'text': 'x'}]}]))


class _DownloadResp:
    def __init__(self, body=b'abc'):
        self._body = body
        self.headers = {'Content-Type': 'text/plain', 'ETag': '"e1"',
                        'Last-Modified': 'Wed, 21 Oct 2015 07:28:00 GMT'}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return 'https://example.com/f'

    def read(self, n):
        return self._body


class DownloadFileSyncTest(unittest.TestCase):
    def test_success_writes_cache_and_returns_bytes(self):
        with mock.patch.object(serve, '_read_http_cache_bytes',
                               return_value=None), \
             mock.patch.object(serve, '_safe_urlopen',
                               return_value=_DownloadResp(b'hello')), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_cache_ttl_from_headers',
                               return_value=60), \
             mock.patch.object(serve, '_write_http_cache_bytes') as write:
            final, ctype, raw, truncated = serve._download_file_sync(
                'https://example.com/f', 100)
        self.assertEqual((final, ctype, raw, truncated),
                         ('https://example.com/f', 'text/plain', b'hello', False))
        write.assert_called_once()

    def test_truncation_flag(self):
        with mock.patch.object(serve, '_read_http_cache_bytes',
                               return_value=None), \
             mock.patch.object(serve, '_safe_urlopen',
                               return_value=_DownloadResp(b'0123456789')), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_cache_ttl_from_headers',
                               return_value=None):
            _final, _ctype, raw, truncated = serve._download_file_sync(
                'https://example.com/f', 4)
        self.assertTrue(truncated)
        self.assertEqual(raw, b'0123')

    def test_conditional_etag_header(self):
        captured = {}

        def _make_request(url, headers=None, method=None):
            captured.update(headers or {})
            return mock.MagicMock()

        cached = {'expires': 0, 'etag': 'tag', 'last_modified_header': ''}
        err = urllib.error.HTTPError('https://example.com/f', 304, 'nm', {}, None)
        with mock.patch.object(serve, '_read_http_cache_bytes',
                               return_value=cached), \
             mock.patch.object(serve.urllib.request, 'Request',
                               side_effect=_make_request), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            out = serve._download_file_sync('https://example.com/f', 10)
        self.assertEqual(captured.get('If-None-Match'), 'tag')
        self.assertEqual(out[2], b'')

    def test_non_304_http_error_propagates(self):
        err = urllib.error.HTTPError('https://example.com/f', 500, 'boom', {}, None)
        with mock.patch.object(serve, '_read_http_cache_bytes',
                               return_value=None), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                serve._download_file_sync('https://example.com/f', 10)

    def test_conditional_last_modified_header(self):
        captured = {}

        def _make_request(url, headers=None, method=None):
            captured.update(headers or {})
            return mock.MagicMock()

        cached = {'expires': 0, 'etag': '',
                  'last_modified_header': 'Wed, 21 Oct 2015 07:28:00 GMT',
                  'raw': b'cached', 'final_url': 'https://example.com/f',
                  'content_type': 'text/plain', 'truncated': False}
        err = urllib.error.HTTPError('https://example.com/f', 304, 'nm', {}, None)
        with mock.patch.object(serve, '_read_http_cache_bytes',
                               return_value=cached), \
             mock.patch.object(serve.urllib.request, 'Request',
                               side_effect=_make_request), \
             mock.patch.object(serve, '_safe_urlopen', side_effect=err):
            out = serve._download_file_sync('https://example.com/f', 10)
        self.assertEqual(captured.get('If-Modified-Since'),
                         'Wed, 21 Oct 2015 07:28:00 GMT')
        self.assertEqual(out[2], b'cached')


def _signals(**overrides):
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


class ThroughputCauseBreakdownTest(unittest.TestCase):
    def test_cancelled_failed_and_in_flight_causes(self):
        alerts = serve._health_alerts_for_signals(_signals(
            task_assigned_last_24h=serve.METR_MIN_ASSIGNED,
            task_fast_completion_rate=0.0,
            task_median_completion_hours=2.0,
            task_cancelled_last_24h=2,
            task_failed_last_24h=3,
            task_in_flight_last_24h=4,
        ))
        message = next(a['message'] for a in alerts
                       if a['category'] == 'throughput')
        self.assertIn('2 cancelled', message)
        self.assertIn('3 failed', message)
        self.assertIn('4 still in flight', message)


class UnfinishedTaskCausesTest(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM kv_state WHERE id = 1')
            conn.execute('DELETE FROM task_archive')
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
                ('{not valid json', time.time()))
            conn.execute(
                'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                ('t-cancel', json.dumps({'status': 'cancelled'}), time.time()))
            conn.execute(
                'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                ('t-fail', json.dumps({'status': 'failed'}), time.time()))
            conn.execute(
                'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                ('t-broken', '{not valid json', time.time()))
            conn.execute(
                'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                ('t-ignored', json.dumps({'status': 'failed'}), time.time()))

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM kv_state WHERE id = 1')
            conn.execute('DELETE FROM task_archive')

    def test_classifies_terminal_states(self):
        out = serve._unfinished_task_causes(
            ['t-cancel', 't-fail', 't-broken', 't-live'])
        self.assertEqual(out['cancelled'], 1)
        self.assertEqual(out['failed'], 1)
        self.assertEqual(out['in_flight'], 2)
        self.assertEqual(out['retired'], 0)


if __name__ == '__main__':
    unittest.main()
