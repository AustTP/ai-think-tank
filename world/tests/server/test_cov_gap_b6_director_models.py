"""Coverage-gap tests for serve_parts 180/190/420/430 and live_coverage.

Direct helper tests plus a few TestClient routes. Every patcher redirects the
derived paths into a throwaway temp dir and seeds a fresh DB so nothing
touches the live think_tank.db.
"""
import asyncio
import io
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
import live_coverage as lc  # noqa: E402

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='tt-cov-b6-dir-')
    _MODULE_PATCHER = mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        ESCALATIONS_PATH=os.path.join(_MODULE_TMP_DIR, 'escalations.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _chain_state():
    return {
        'agentRoster': [
            {'id': 'd1', 'name': 'Director', 'isDirector': True},
            {'id': 'w1', 'name': 'Worker', 'director': 'd1'},
            {'id': 'admin', 'name': 'Chief', 'isAdmin': True},
        ],
    }


class DirectorSecondOpinionTest(unittest.TestCase):
    def test_state_read_failure_fails_closed(self):
        with mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db down')):
            self.assertFalse(serve._director_second_opinion(
                'w1', 'browse', 'page', 'http://x', 'p', 0.2))

    def test_reviewer_deny_blocks_and_logs(self):
        with mock.patch.object(serve, 'get_state_from_db', return_value=_chain_state()), \
             mock.patch.object(serve, '_effective_safety_confidence', return_value=0.7), \
             mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('deny', 0.9, 0.0)), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertFalse(serve._director_second_opinion(
                'w1', 'browse', 'page', 'http://x', 'p', 0.2))
        self.assertEqual(log.call_args[0][1], 'jev_chain_denied')

    def test_escalation_failure_is_logged(self):
        with mock.patch.object(serve, 'get_state_from_db', return_value=_chain_state()), \
             mock.patch.object(serve, '_effective_safety_confidence', return_value=0.7), \
             mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('unsure', 0.1, 0.0)), \
             mock.patch.object(serve, 'create_escalation',
                               side_effect=RuntimeError('no esc')), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertFalse(serve._director_second_opinion(
                'w1', 'browse', 'page', 'http://x', 'p', 0.2))
        actions = [call[0][1] for call in log.call_args_list]
        self.assertIn('jev_chain_owner_escalation_failed', actions)

    def test_safety_gate_director_approves(self):
        with mock.patch.object(serve, '_effective_safety_confidence', return_value=0.8), \
             mock.patch.object(serve, '_director_second_opinion', return_value=True), \
             mock.patch.object(serve, 'log_action') as log:
            allowed = serve._jev_safety_gate(
                'w1', 'browse', 'page', 'http://x', 'p', 'allow', 0.2, 0.01, True)
        self.assertTrue(allowed)
        self.assertEqual(log.call_args[0][2]['decision'], 'allowed_by_director')


class LatencyStatsFailOpenTest(unittest.TestCase):
    def test_record_failure_is_swallowed(self):
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('db')):
            serve._record_model_latency('m', 5.0)

    def test_median_read_failure_returns_none(self):
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('db')):
            self.assertIsNone(serve._model_median_latency_ms('m'))

    def test_sample_count_empty_model_is_zero(self):
        self.assertEqual(serve._model_latency_sample_count(''), 0)

    def test_sample_count_read_failure_is_zero(self):
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('db')):
            self.assertEqual(serve._model_latency_sample_count('m'), 0)


class TierFallbackCandidatesTest(unittest.TestCase):
    def test_stops_at_limit(self):
        pool = [{'id': 'a', 'price': 1}, {'id': 'b', 'price': 2},
                {'id': 'c', 'price': 3}, {'id': 'd', 'price': 4}]
        with mock.patch.object(serve, '_best_value_qualifiers_sync',
                               return_value=pool):
            out = serve._tier_fallback_candidates(
                'low', pool, {}, {'id': 'picked'}, limit=3)
        self.assertEqual([m['id'] for m in out], ['a', 'b', 'c'])


class SelfeditWindowingTest(unittest.TestCase):
    def _req(self, path='/api/selfedit/fix', headers=None):
        return types.SimpleNamespace(
            url=types.SimpleNamespace(path=path), headers=headers or {})

    def test_remediation_exception_fails_closed(self):
        with mock.patch.object(serve._selfedit, 'remediation_window',
                               side_effect=RuntimeError('x')):
            self.assertFalse(serve._selfedit_remediation_exempt(
                self._req()))

    def test_fix_headers_exception_returns_none(self):
        with mock.patch.object(serve._selfedit, 'remediation_window',
                               side_effect=RuntimeError('x')):
            self.assertIsNone(serve._selfedit_fix_headers('a1'))

    def test_validation_exception_fails_closed(self):
        with mock.patch.object(serve._selfedit, 'validation_window',
                               side_effect=RuntimeError('x')):
            self.assertFalse(serve._selfedit_validation_exempt(
                self._req()))

    def test_validation_wrong_validator_fails_closed(self):
        headers = {'X-Dormancy-Window': 'w1', 'X-Dormancy-Agent': 'a1',
                   'X-Agent-Key': 'k'}
        with mock.patch.object(serve._selfedit, 'validation_window',
                               return_value='w1'), \
             mock.patch.object(serve._selfedit, 'window_validator',
                               return_value='someone-else'), \
             mock.patch.object(serve, 'verify_agent_key', return_value=True):
            self.assertFalse(serve._selfedit_validation_exempt(
                self._req(headers=headers)))

    def test_validate_headers_exception_returns_none(self):
        with mock.patch.object(serve._selfedit, 'validation_window',
                               side_effect=RuntimeError('x')):
            self.assertIsNone(serve._selfedit_validate_headers('a1'))

    def test_middleware_skips_wake_for_exempt_traffic(self):
        from fastapi.testclient import TestClient
        with mock.patch.object(serve, '_dormant', return_value=True), \
             mock.patch.object(serve, '_should_wake', return_value=True), \
             mock.patch.object(serve, '_selfedit_remediation_exempt',
                               return_value=True), \
             mock.patch.object(serve, '_set_dormant') as setd:
            c = TestClient(serve.app)
            r = c.get('/index.html')
        self.assertEqual(r.status_code, 200)
        setd.assert_not_called()


class TaskFixSpiralsTest(unittest.TestCase):
    def test_bad_json_and_empty_root_are_skipped(self):
        now = time.time()
        with serve._db() as conn:
            conn.execute(
                "INSERT INTO action_log (ts, agent_id, action, details) VALUES (?, ?, ?, ?)",
                (now, 'x', 'task_assigned', 'not-json'))
            conn.execute(
                "INSERT INTO action_log (ts, agent_id, action, details) VALUES (?, ?, ?, ?)",
                (now, 'x', 'task_assigned',
                 json.dumps({'title': 'Fix: (agent)', 'taskId': 't-empty'})))
        out = serve._task_fix_spirals(now + 1)
        self.assertEqual(out['spirals'], 0)


class HttpJsonTest(unittest.TestCase):
    def _resp(self):
        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'
        return _R()

    def test_extra_headers_are_sent(self):
        captured = {}

        def _make_request(url, data=None, method=None, headers=None):
            captured.update(headers or {})
            return mock.MagicMock()

        with mock.patch.object(serve.urllib.request, 'Request',
                               side_effect=_make_request), \
             mock.patch.object(serve.urllib.request, 'urlopen',
                               return_value=self._resp()), \
             mock.patch.object(serve.runtime_limits, 'observe'):
            out = serve._http_json('GET', 'http://x', '/api/state',
                                   extra_headers={'X-Dormancy-Window': 'w1'})
        self.assertEqual(out, {'ok': True})
        self.assertEqual(captured.get('X-Dormancy-Window'), 'w1')

    def test_credits_paused_raises(self):
        body = io.BytesIO(b'{"creditsPaused": true, "error": "no credits"}')
        err = urllib.error.HTTPError('http://x', 402, 'pay', {}, body)
        with mock.patch.object(serve.urllib.request, 'urlopen',
                               side_effect=err), \
             mock.patch.object(serve.runtime_limits, 'observe'):
            with self.assertRaises(serve.OpenRouterCreditsPaused):
                serve._http_json('GET', 'http://x', '/api/state')


class JevModelGetTest(unittest.TestCase):
    def _req(self, cookies=None):
        return types.SimpleNamespace(cookies=cookies or {})

    def test_serve_pulse_without_session_shows_login(self):
        with mock.patch.object(serve, 'verify_session', return_value=False):
            resp = asyncio.run(serve.serve_pulse(self._req()))
        self.assertIn('text/html', resp.headers.get('content-type', 'text/html'))

    def test_pulse_overview_requires_player(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = asyncio.run(serve.api_pulse_overview(self._req()))
        self.assertEqual(resp.status_code, 401)

    def test_project_board_requires_player(self):
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = asyncio.run(serve.api_project_board(self._req()))
        self.assertEqual(resp.status_code, 401)

    def test_project_board_bad_limit_defaults(self):
        import content
        req = types.SimpleNamespace(
            cookies={}, query_params={'limit': 'not-a-number'})
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(content, 'project_board',
                               return_value=json.dumps({'cards': []})) as board:
            resp = asyncio.run(serve.api_project_board(req))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(board.call_args[0][0]['limit'], 40)

    def test_lazy_content_reexport(self):
        import content
        name = '_run_spike_content'
        serve.__dict__.pop(name, None)
        self.assertIs(serve.__getattr__(name), getattr(content, name))
        with self.assertRaises(AttributeError):
            serve.__getattr__('definitely_not_a_reexport')


class ContentReexportTailTest(unittest.TestCase):
    """Exercise the shipped lazy-re-export block in 430_jev_model_get.py.

    The loader (world/serve.py) repeats this block after exec'ing the parts, so
    the live module resolves through serve.py's copy. This test compiles the
    part's own tail with padding that preserves its real line numbers, so the
    shipped function body itself is verified rather than only its duplicate.
    """

    def test_tail_lazy_reexport_contract(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        part = os.path.join(root, 'server', 'serve_parts',
                            '430_jev_model_get.py')
        with open(part, encoding='utf-8') as fh:
            lines = fh.read().splitlines()
        start = next(i for i, line in enumerate(lines)
                     if line.startswith('_CONTENT_REEXPORTS = {'))
        src = '\n' * start + '\n'.join(lines[start:]) + '\n'
        namespace = {'__name__': 'serve_tail_probe'}
        exec(compile(src, part, 'exec'), namespace)
        import content
        self.assertIs(namespace['__getattr__']('_run_spike_content'),
                      content._run_spike_content)
        with self.assertRaises(AttributeError):
            namespace['__getattr__']('nope_not_reexported')


class LiveCoverageDeadlineTest(unittest.TestCase):
    def setUp(self):
        lc._RUNS.clear()
        lc._CONTROL.clear()

    def test_top_of_loop_usd_cap_after_error_continue(self):
        def _extract(url, path, timeout):
            open(path, 'wb').write(b'jpeg')
            return path

        def _vision(img, prompt, max_tokens):
            return json.dumps({'observation': 'seen', 'changes': 'none',
                               'entities': [], 'confidence': 0.9}), 0.01

        tmp = tempfile.mkdtemp(prefix='lc-usd-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(lc, '_append_observation',
                               side_effect=RuntimeError('boom')):
            snap = lc.start('https://example.com', max_frames=10, interval_s=2,
                            max_seconds=30, max_usd=0.005,
                            extract_frame=_extract, vision_call=_vision,
                            library_dir=tmp)
            done = lc.wait(snap['id'], 20)
        self.assertEqual(done['stopReason'], 'max_usd')

    def test_bottom_of_loop_deadline_ends_run(self):
        def _extract(url, path, timeout):
            open(path, 'wb').write(b'jpeg')
            return path

        def _slow_vision(img, prompt, max_tokens):
            time.sleep(1.2)
            return json.dumps({'observation': 'seen', 'changes': 'none',
                               'entities': [], 'confidence': 0.9}), 0.01

        tmp = tempfile.mkdtemp(prefix='lc-deadline-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        snap = lc.start('https://example.com', max_frames=10, interval_s=2,
                        max_seconds=1, max_usd=1,
                        extract_frame=_extract, vision_call=_slow_vision,
                        library_dir=tmp)
        done = lc.wait(snap['id'], 15)
        self.assertEqual(done['stopReason'], 'max_seconds')


if __name__ == '__main__':
    unittest.main()
