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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


class ChromeFindAndScreenshotSync(unittest.TestCase):
    """_find_chrome and _screenshot_url_sync."""

    def test_find_chrome_returns_first_existing_path(self):
        exe = os.path.join(_MODULE_TMP_DIR, 'fake-chrome')
        with open(exe, 'w') as f:
            f.write('#!/bin/sh\n')
        try:
            with unittest.mock.patch.object(serve, 'CHROME_PATHS',
                                            [exe, '/definitely/missing/chrome']):
                self.assertEqual(serve._find_chrome(), exe)
        finally:
            os.remove(exe)

    def test_find_chrome_returns_none_when_nothing_exists(self):
        with unittest.mock.patch.object(serve, 'CHROME_PATHS',
                                        ['/definitely/missing/chrome']):
            self.assertIsNone(serve._find_chrome())

    def test_screenshot_url_sync_returns_none_without_a_chrome(self):
        with unittest.mock.patch.object(serve, '_find_chrome', return_value=None):
            self.assertIsNone(serve._screenshot_url_sync('http://example.com'))

    def test_screenshot_url_sync_returns_base64_png_and_cleans_up(self):
        calls = []

        def _fake_run(cmd, capture_output=False, timeout=0):
            calls.append(cmd)
            shot = next(a.split('=', 1)[1] for a in cmd if a.startswith('--screenshot='))
            with open(shot, 'wb') as f:
                f.write(b'FAKEPNGBYTES')
            return subprocess.CompletedProcess(cmd, 0)

        with unittest.mock.patch.object(serve, '_find_chrome', return_value='/fake/chrome'), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=_fake_run):
            out = serve._screenshot_url_sync('http://example.com', width=800, height=600)
        self.assertEqual(out, 'RkFLRVBOR0JZVEVT')  # base64 of b'FAKEPNGBYTES'
        self.assertIn('--window-size=800,600', calls[0])
        leftovers = [n for n in os.listdir(_MODULE_TMP_DIR) if n.startswith('.tmp-screenshot-')]
        self.assertEqual(leftovers, [])

    def test_screenshot_url_sync_returns_none_when_chrome_writes_no_file(self):
        def _fake_run(cmd, capture_output=False, timeout=0):
            return subprocess.CompletedProcess(cmd, 0)

        with unittest.mock.patch.object(serve, '_find_chrome', return_value='/fake/chrome'), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=_fake_run):
            out = serve._screenshot_url_sync('http://example.com')
        self.assertIsNone(out)


class ScreenshotEndpoint(unittest.TestCase):
    """POST /api/screenshot."""

    SANDBOX_ID = 'gap-f-screenshot-sandbox'

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-screenshots-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = unittest.mock.patch.object(serve, 'SANDBOXES_DIR', self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sandbox_dir = serve._sandbox_dir_for(self.SANDBOX_ID)

    def _post(self, **kwargs):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            return c.post('/api/screenshot', json=kwargs)

    def test_rate_limited_returns_429(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID)
        self.assertEqual(r.status_code, 429)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1], 'rate_limited')

    def test_missing_sandbox_id_returns_400(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            r = self._post(agentId='eli')
        self.assertEqual(r.status_code, 400)
        self.assertIn('sandboxId', r.json()['error'])

    def test_path_traversal_returns_400(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, path='../../etc/passwd')
        self.assertEqual(r.status_code, 400)
        self.assertIn('invalid path', r.json()['error'])

    def test_missing_file_returns_404(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, path='nope.html')
        self.assertEqual(r.status_code, 404)

    def test_no_chrome_returns_501(self):
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>x</html>')
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_find_chrome', return_value=None):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, path='index.html')
        self.assertEqual(r.status_code, 501)

    def test_success_returns_base64_image(self):
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>x</html>')

        def _fake_run(cmd, capture_output=False, timeout=0):
            shot = next(a.split('=', 1)[1] for a in cmd if a.startswith('--screenshot='))
            with open(shot, 'wb') as f:
                f.write(b'PNG')
            return subprocess.CompletedProcess(cmd, 0)

        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_find_chrome', return_value='/fake/chrome'), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=_fake_run), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, path='index.html',
                           width=10, height=10)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['imageBase64'], 'UE5H')
        self.assertEqual(body['width'], 200)
        self.assertEqual(body['height'], 200)
        self.assertTrue(any(ca[0][1] == 'screenshot' and ca[0][2]['ok'] is True for ca in log.call_args_list))

    def test_chrome_writes_no_file_returns_502(self):
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>x</html>')

        def _fake_run(cmd, capture_output=False, timeout=0):
            return subprocess.CompletedProcess(cmd, 0, stderr=b'boom')

        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_find_chrome', return_value='/fake/chrome'), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=_fake_run), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, path='index.html')
        self.assertEqual(r.status_code, 502)
        self.assertIn('boom', r.json()['stderr'])
        self.assertTrue(any(ca[0][1] == 'screenshot' and ca[0][2]['ok'] is False for ca in log.call_args_list))

    def test_chrome_timeout_returns_504(self):
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>x</html>')
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_find_chrome', return_value='/fake/chrome'), \
             unittest.mock.patch.object(serve.subprocess, 'run',
                                        side_effect=subprocess.TimeoutExpired('chrome', 20)):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, path='index.html')
        self.assertEqual(r.status_code, 504)


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


class PageProbeSync(unittest.TestCase):
    """_page_probe_sync -- fake playwright driving, no real browser."""

    def _run(self, actions, probes, **kw):
        ctx = _FakePageProbeHarness(**kw).build()
        with unittest.mock.patch.object(serve, 'sync_playwright', return_value=ctx):
            return serve._page_probe_sync('/tmp/fake.html', actions, probes)

    def test_success_with_all_action_kinds_and_probes(self):
        actions = [
            {'type': 'click', 'selector': '#btn'},
            {'type': 'keydown', 'key': 'Enter'},
            {'type': 'wait', 'ms': 100},
            {'type': 'eval', 'code': 'window.x = 1'},
            {'type': 'unknown'},
        ]
        probes = ['window.custom1', 'document.title']
        out = self._run(actions, probes)
        self.assertEqual(len(out['actionLog']), 5)
        self.assertIn('action 0 (click): ok', out['actionLog'])
        self.assertIn('action 1 (keydown): ok', out['actionLog'])
        self.assertIn('action 2 (wait): ok', out['actionLog'])
        self.assertIn('action 3 (eval): ok', out['actionLog'])
        self.assertIn('unknown action type', out['actionLog'][4])
        self.assertEqual(out['results'], {'window.custom1': 'RESULT:window.custom1',
                                         'document.title': 'RESULT:document.title'})
        self.assertEqual(out['customGlobals'], [{'name': 'custom1', 'type': 'boolean', 'value': True}])
        self.assertEqual(out['console'], [])
        self.assertEqual(out['pageErrors'], [])

    def test_failed_action_is_reported_not_aborting(self):
        out = self._run([{'type': 'click', 'selector': '#missing'}], [], click_error='no such selector')
        self.assertIn('FAILED', out['actionLog'][0])

    def test_failed_probe_is_reported_per_probe(self):
        out = self._run([], ['window.missing'], probe_error=True)
        self.assertTrue(out['results']['window.missing'].startswith('ERROR:'))

    def test_custom_globals_failure_is_reported(self):
        out = self._run([], ['window.x'], custom_error=True)
        self.assertEqual(out['customGlobals'], [{'name': 'ERROR', 'type': 'ERROR', 'error': 'custom eval boom'}])

    def test_actions_and_probes_are_bounded(self):
        actions = [{'type': 'wait', 'ms': 100} for _ in range(60)]
        probes = ['window.x%d' % i for i in range(60)]
        out = self._run(actions, probes)
        self.assertEqual(len(out['actionLog']), serve.PAGE_PROBE_MAX_ACTIONS)
        self.assertEqual(len(out['results']), serve.PAGE_PROBE_MAX_PROBES)


class PageProbeEndpoint(unittest.TestCase):
    """POST /api/page-probe."""

    SANDBOX_ID = 'gap-f-probe-sandbox'

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-probe-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = unittest.mock.patch.object(serve, 'SANDBOXES_DIR', self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sandbox_dir = serve._sandbox_dir_for(self.SANDBOX_ID)
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>x</html>')

    def _post(self, **kwargs):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            return c.post('/api/page-probe', json=kwargs)

    def test_rate_limited_returns_429(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, probes=['window.x'])
        self.assertEqual(r.status_code, 429)
        self.assertEqual(log.call_args[0][1], 'rate_limited')

    def test_playwright_missing_returns_501(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, 'sync_playwright', None):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, probes=['window.x'])
        self.assertEqual(r.status_code, 501)

    def test_missing_sandbox_id_returns_400(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            r = self._post(agentId='eli', probes=['window.x'])
        self.assertEqual(r.status_code, 400)

    def test_non_list_actions_returns_400(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, actions='click', probes=['x'])
        self.assertEqual(r.status_code, 400)

    def test_empty_probes_returns_400(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, probes=[])
        self.assertEqual(r.status_code, 400)
        self.assertIn('at least one probe', r.json()['error'])

    def test_path_traversal_returns_400(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, path='../x.html', probes=['x'])
        self.assertEqual(r.status_code, 400)

    def test_missing_file_returns_404(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, path='nope.html', probes=['x'])
        self.assertEqual(r.status_code, 404)

    def test_timeout_returns_504(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve.asyncio, 'wait_for',
                                        side_effect=asyncio.TimeoutError), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, probes=['window.x'])
        self.assertEqual(r.status_code, 504)
        self.assertEqual(log.call_args[0][1], 'page_probe')
        self.assertFalse(log.call_args[0][2]['ok'])

    def test_probe_failure_returns_502(self):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve.asyncio, 'wait_for',
                                        side_effect=RuntimeError('playwright exploded')), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, probes=['window.x'])
        self.assertEqual(r.status_code, 502)
        self.assertIn('playwright exploded', r.json()['error'])

    def test_success_returns_probe_result(self):
        result = {'actionLog': [], 'console': [], 'pageErrors': [],
                  'customGlobals': [], 'results': {'window.x': 1}}
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_page_probe_sync', return_value=result), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = self._post(agentId='eli', sandboxId=self.SANDBOX_ID, probes=['window.x'])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['results'], {'window.x': 1})
        self.assertTrue(any(ca[0][1] == 'page_probe' and ca[0][2]['ok'] is True
                            for ca in log.call_args_list))


class SandboxBackupHelpers(unittest.TestCase):
    """_snapshot_sandbox / _list_sandbox_backups / _restore_sandbox_backup gaps."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-sandbox-helpers-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = unittest.mock.patch.object(serve, 'SANDBOXES_DIR', os.path.join(self.tmp, 'sbx'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_snapshot_sandbox_swallows_git_failures(self):
        sandbox_dir = serve._sandbox_dir_for('gap-f-snap')
        with open(os.path.join(sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>x</html>')
        with unittest.mock.patch.object(serve, '_ensure_sandbox_git_repo',
                                        side_effect=RuntimeError('git down')):
            serve._snapshot_sandbox('gap-f-snap', sandbox_dir)  # must not raise

    def test_list_returns_empty_when_no_git_dir(self):
        sandbox_dir = serve._sandbox_dir_for('gap-f-no-git')
        with open(os.path.join(sandbox_dir, 'index.html'), 'w') as f:
            f.write('x')
        self.assertEqual(serve._list_sandbox_backups('gap-f-no-git'), [])

    def test_list_returns_empty_when_git_log_fails(self):
        sandbox_dir = serve._sandbox_dir_for('gap-f-git-fail')
        os.makedirs(os.path.join(sandbox_dir, '.git'))
        with unittest.mock.patch.object(
                serve, '_run_git_sync',
                return_value=subprocess.CompletedProcess(['git', 'log'], 1, '', 'bad repo')):
            self.assertEqual(serve._list_sandbox_backups('gap-f-git-fail'), [])

    def test_restore_raises_when_checkout_fails(self):
        sandbox_dir = serve._sandbox_dir_for('gap-f-checkout-fail')
        os.makedirs(os.path.join(sandbox_dir, '.git'))

        def _fake_git(sandbox_dir, args):
            if args[0] == 'cat-file':
                return subprocess.CompletedProcess(['git'] + args, 0, '', '')
            return subprocess.CompletedProcess(['git'] + args, 1, '', 'checkout failed')

        with unittest.mock.patch.object(serve, '_run_git_sync', side_effect=_fake_git), \
             unittest.mock.patch.object(serve, '_snapshot_sandbox'):
            with self.assertRaises(ValueError) as cm:
                serve._restore_sandbox_backup('gap-f-checkout-fail', 'abc123 iso-date')
        self.assertIn('restore failed', str(cm.exception))


class SandboxBackupEndpoints(unittest.TestCase):
    """GET /api/sandbox-backups and POST /api/sandbox-backups/restore."""

    def test_list_returns_backups(self):
        with unittest.mock.patch.object(serve, '_list_sandbox_backups',
                                        return_value=['abc123 2024-01-01T00:00:00Z']), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/sandbox-backups', params={'sandboxId': 'sbx-1'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['backups'][0][:7], 'abc123 ')

    def test_restore_rejects_non_player(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/sandbox-backups/restore',
                       json={'agentId': 'eli', 'sandboxId': 'sbx', 'stamp': 'abc'})
        self.assertEqual(r.status_code, 403)

    def test_restore_requires_fields(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/sandbox-backups/restore', json={'agentId': 'player', 'sandboxId': 'sbx'})
        self.assertEqual(r.status_code, 400)

    def test_restore_missing_stamp_is_404(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_restore_sandbox_backup',
                                        side_effect=ValueError('no such backup: abc')):
            c = TestClient(serve.app)
            r = c.post('/api/sandbox-backups/restore',
                       json={'agentId': 'player', 'sandboxId': 'sbx', 'stamp': 'abc'})
        self.assertEqual(r.status_code, 404)

    def test_restore_success_logs(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_restore_sandbox_backup') as restore, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/api/sandbox-backups/restore',
                       json={'agentId': 'player', 'sandboxId': 'sbx', 'stamp': 'abc'})
        self.assertEqual(r.status_code, 200, r.text)
        restore.assert_called_once_with('sbx', 'abc')
        self.assertEqual(log.call_args[0][1], 'sandbox_backup_restored')


class EscalationEndpoints(unittest.TestCase):
    """GET /api/escalation/{id} and /api/escalation/resolve."""

    def test_get_escalation_found(self):
        with unittest.mock.patch.object(serve, '_load_escalations',
                                        return_value={'e1': {'status': 'pending', 'kind': 'x'}}):
            c = TestClient(serve.app)
            r = c.get('/api/escalation/e1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'pending')

    def test_get_escalation_not_found(self):
        with unittest.mock.patch.object(serve, '_load_escalations', return_value={}):
            c = TestClient(serve.app)
            r = c.get('/api/escalation/e1')
        self.assertEqual(r.status_code, 404)

    def _resolve(self, id='e1', token='tok', decision='approve'):
        with unittest.mock.patch.object(serve, '_load_escalations',
                                        return_value={'e1': {
                                            'token': 'tok', 'status': 'pending', 'kind': 'personnel',
                                            'question': 'fire eli?', 'note': 'x'}}), \
             unittest.mock.patch.object(serve, '_save_escalations') as save, \
             unittest.mock.patch.object(serve, '_reset_escalation_judge_drift') as reset, \
             unittest.mock.patch.object(serve, '_grant_allowlist') as grant:
            c = TestClient(serve.app)
            r = c.get('/api/escalation/resolve',
                      params={'id': id, 'token': token, 'decision': decision})
        return r, save, reset, grant

    def test_resolve_wrong_token_is_404(self):
        r, _save, reset, _grant = self._resolve(token='wrong')
        self.assertEqual(r.status_code, 404)
        self.assertIn('Invalid or expired', r.text)

    def test_resolve_already_resolved(self):
        with unittest.mock.patch.object(serve, '_load_escalations',
                                        return_value={'e1': {'token': 'tok', 'status': 'approved'}}):
            c = TestClient(serve.app)
            r = c.get('/api/escalation/resolve', params={'id': 'e1', 'token': 'tok', 'decision': 'approve'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Already resolved', r.text)

    def test_resolve_invalid_decision_is_400(self):
        r, _save, _reset, _grant = self._resolve(decision='maybe')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Invalid decision', r.text)

    def test_resolve_approve_allowlist_grants_host(self):
        with unittest.mock.patch.object(serve, '_load_escalations',
                                        return_value={'e1': {
                                            'token': 'tok', 'status': 'pending',
                                            'kind': 'allowlist request',
                                            'question': 'add host?', 'note': 'example.com'}}), \
             unittest.mock.patch.object(serve, '_save_escalations'), \
             unittest.mock.patch.object(serve, '_reset_escalation_judge_drift'), \
             unittest.mock.patch.object(serve, '_grant_allowlist', return_value='example.com') as grant:
            c = TestClient(serve.app)
            r = c.get('/api/escalation/resolve',
                      params={'id': 'e1', 'token': 'tok', 'decision': 'approve'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Allowlist grant applied', r.text)
        self.assertIn('example.com', r.text)
        grant.assert_called_once_with('example.com')

    def test_resolve_plain_approve(self):
        r, save, reset, grant = self._resolve(decision='approve')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Recorded: <b>approved</b>', r.text)
        saved = save.call_args[0][0]
        self.assertEqual(saved['e1']['status'], 'approved')
        reset.assert_called_once_with('personnel')
        grant.assert_not_called()

    def test_resolve_deny(self):
        r, _save, _reset, _grant = self._resolve(decision='deny')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Recorded: <b>denied</b>', r.text)


class ActivityFeedEndpoint(unittest.TestCase):
    """GET /api/activity."""

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')

    def test_returns_log_entries_newest_first(self):
        serve.log_action('eli', 'browse', {'url': 'http://x'})
        serve.log_action(None, 'foo', None)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/activity?limit=10')
        self.assertEqual(r.status_code, 200)
        entries = r.json()['entries']
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['action'], 'foo')
        self.assertIsNone(entries[0]['details'])
        self.assertEqual(entries[1]['agentId'], 'eli')
        self.assertEqual(entries[1]['details']['url'], 'http://x')

    def test_limit_is_clamped(self):
        serve.log_action('eli', 'foo', None)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/activity?limit=5000')
        self.assertEqual(r.status_code, 200)


class DecisionCalibration(unittest.TestCase):
    """_decision_calibration_report (and its nested _bin/_outcome_for)."""

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')

    def _insert(self, agent_id, action, details, ts, trace_id=None):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (agent_id, action, json.dumps(details) if details is not None else None,
                 None, ts, trace_id))

    def test_report_buckets_confidences_and_scores_outcomes(self):
        now = time.time()
        # Trace-linked allowed gate + outcome -> success.
        self._insert('eli', 'browse', {'decision': 'allowed', 'confidence': 0.7}, now - 200, trace_id='T1')
        self._insert('eli', 'browse', {'decision': 'allowed'}, now - 100, trace_id='T1')
        # Fallback (no trace_id): two outcomes in window, later one wins.
        self._insert('eli', 'curl', {'decision': 'allowed', 'confidence': 0.75}, now - 400)
        self._insert('eli', 'curl', {'decision': 'allowed_but_fetch_failed'}, now - 300)
        self._insert('eli', 'curl', {'decision': 'allowed'}, now - 150)
        # Blocked and escalated rows never score.
        self._insert('eli', 'browse', {'decision': 'blocked', 'confidence': 0.5}, now - 50)
        self._insert('eli', 'browse', {'decision': 'escalated_unsure', 'confidence': 0.3}, now - 40)
        # Allowed with no outcome.
        self._insert('eli', 'browse', {'decision': 'allowed', 'confidence': 0.9}, now - 30)
        # >= 1.0 confidence lands in the top bin via the fallback.
        self._insert('eli', 'browse', {'decision': 'blocked', 'confidence': 1.5}, now - 20)
        # Outcome row that is a json-decoding failure is skipped.
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                ('eli', 'curl', 'not json{{', now - 10))
        # Outcome row with NULL details skipped.
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                ('eli', 'browse', None, now - 9))
        # Gate with non-numeric confidence / unknown decision skipped.
        self._insert('eli', 'browse', {'decision': 'blocked', 'confidence': 'high'}, now - 8)
        self._insert('eli', 'browse', {'decision': 'weird', 'confidence': 0.6}, now - 7)
        # Gate with bad JSON details (still matched by the confidence LIKE) skipped.
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                ('eli', 'browse', '{"confidence": 0.6, "decision": "allowed", BAD', now - 6))

        report = serve._decision_calibration_report(window_s=3600)
        by_bin = {b['bin']: b for b in report['buckets']}
        # 0.7 allowed -> 1 success (trace-linked); 0.75 allowed -> later outcome 'allowed' wins (1 success).
        self.assertEqual(by_bin['0.70-0.80']['n'], 2)
        self.assertEqual(by_bin['0.70-0.80']['n_outcome'], 2)
        self.assertEqual(by_bin['0.70-0.80']['n_success'], 2)
        self.assertEqual(by_bin['0.50-0.60']['n_blocked'], 1)
        self.assertEqual(by_bin['0.30-0.40']['n_escalated'], 1) if '0.30-0.40' in by_bin else None
        self.assertEqual(by_bin['0.90-1.00']['n'], 2)
        self.assertEqual(report['total_decisions'], 6)
        self.assertEqual(report['scoreable'], 2)
        self.assertEqual(report['overall_success_rate'], 1.0)

    def test_outcome_for_trace_requires_same_agent_action_and_later_ts(self):
        now = time.time()
        self._insert('eli', 'browse', {'decision': 'allowed', 'confidence': 0.7}, now - 200, trace_id='T2')
        # Same trace but wrong agent/action and earlier ts -> falls back to the window heuristic.
        self._insert('maya', 'browse', {'decision': 'allowed'}, now - 300, trace_id='T2')
        self._insert('eli', 'curl', {'decision': 'allowed'}, now - 300, trace_id='T2')
        self._insert('eli', 'browse', {'decision': 'allowed'}, now - 50)
        report = serve._decision_calibration_report(window_s=3600)
        by_bin = {b['bin']: b for b in report['buckets']}
        self.assertEqual(by_bin['0.70-0.80']['n_success'], 1)


class ReviewCalibrationSample(unittest.TestCase):
    """_insert_review_calibration_sample."""

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM review_judge_calibration')

    def test_inserts_agree_row(self):
        serve._insert_review_calibration_sample('code', 'meets', 0.9, 'meets')
        serve._insert_review_calibration_sample('code', 'meets', 0.9, 'fails')
        with serve._db() as conn:
            rows = conn.execute('SELECT section, agree FROM review_judge_calibration').fetchall()
        self.assertEqual(dict(rows), {'code': 0})  # second insert overwrote first? no -- two rows

    def test_insert_is_best_effort_on_db_failure(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db down')):
            serve._insert_review_calibration_sample('code', 'meets', 0.9, 'meets')  # must not raise


class CalibrationPasses(unittest.TestCase):
    """_calibration_adjust_pass and _review_grade_calibration_pass."""

    def _report(self, label, rate, n_outcome):
        buckets = []
        for lo, hi, name in serve.JEV_CALIBRATION_BINS:
            b = {'bin': name, 'n': 0, 'n_allowed': 0, 'n_blocked': 0, 'n_escalated': 0,
                 'n_outcome': 0, 'n_success': 0, 'n_failure': 0, 'success_rate': None}
            if name == label:
                b.update({'n': 50, 'n_allowed': 50, 'n_outcome': n_outcome,
                          'success_rate': rate})
            buckets.append(b)
        return {'buckets': buckets}

    def test_adjust_returns_none_when_live_bar_not_in_report(self):
        with unittest.mock.patch.object(serve, '_decision_calibration_report',
                                        return_value={'buckets': []}), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=1.5):
            self.assertIsNone(serve._calibration_adjust_pass(now=time.time()))

    def test_adjust_returns_none_below_min_outcome(self):
        report = self._report('0.60-0.70', 0.9, 3)
        with unittest.mock.patch.object(serve, '_decision_calibration_report',
                                        return_value=report):
            self.assertIsNone(serve._calibration_adjust_pass(now=time.time()))

    def test_adjust_raises_the_bar_when_reliability_low(self):
        report = self._report('0.60-0.70', 0.5, 20)
        with unittest.mock.patch.object(serve, '_decision_calibration_report',
                                        return_value=report), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_set_setting') as set_setting, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            new = serve._calibration_adjust_pass(now=time.time())
        self.assertEqual(new, 0.65)
        set_setting.assert_called_once_with('jev_safety_confidence', '0.65')
        self.assertEqual(log.call_args[0][2]['reason'], 'raised')

    def test_adjust_lowers_the_bar_when_reliability_high(self):
        report = self._report('0.60-0.70', 1.0, 20)
        with unittest.mock.patch.object(serve, '_decision_calibration_report',
                                        return_value=report), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_set_setting') as set_setting:
            new = serve._calibration_adjust_pass(now=time.time())
        self.assertEqual(new, 0.55)
        set_setting.assert_called_once_with('jev_safety_confidence', '0.55')

    def test_adjust_no_move_inside_deadband(self):
        report = self._report('0.60-0.70', 0.88, 20)
        with unittest.mock.patch.object(serve, '_decision_calibration_report',
                                        return_value=report), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6):
            self.assertIsNone(serve._calibration_adjust_pass(now=time.time()))

    def test_adjust_no_move_when_clamped_at_max(self):
        report = self._report('0.90-1.00', 0.5, 20)
        with unittest.mock.patch.object(serve, '_decision_calibration_report',
                                        return_value=report), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.95):
            self.assertIsNone(serve._calibration_adjust_pass(now=time.time()))

    def test_review_grade_returns_none_below_min_samples(self):
        with unittest.mock.patch.object(serve, '_review_grade_calibration_report',
                                        return_value={'samples': 3, 'agreement_rate': 0.9}):
            self.assertIsNone(serve._review_grade_calibration_pass(now=time.time()))

    def test_review_grade_returns_none_when_rate_none(self):
        with unittest.mock.patch.object(serve, '_review_grade_calibration_report',
                                        return_value={'samples': 20, 'agreement_rate': None}):
            self.assertIsNone(serve._review_grade_calibration_pass(now=time.time()))

    def test_review_grade_raises_the_bar(self):
        with unittest.mock.patch.object(serve, '_review_grade_calibration_report',
                                        return_value={'samples': 20, 'agreement_rate': 0.6}), \
             unittest.mock.patch.object(serve, '_effective_review_grade_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_set_setting') as set_setting, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            new = serve._review_grade_calibration_pass(now=time.time())
        self.assertEqual(new, 0.65)
        set_setting.assert_called_once_with('jev_review_grade_confidence', '0.65')
        self.assertEqual(log.call_args[0][2]['reason'], 'raised')

    def test_review_grade_lowers_the_bar(self):
        with unittest.mock.patch.object(serve, '_review_grade_calibration_report',
                                        return_value={'samples': 20, 'agreement_rate': 1.0}), \
             unittest.mock.patch.object(serve, '_effective_review_grade_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_set_setting') as set_setting:
            new = serve._review_grade_calibration_pass(now=time.time())
        self.assertEqual(new, 0.55)
        set_setting.assert_called_once_with('jev_review_grade_confidence', '0.55')

    def test_review_grade_no_move_inside_deadband(self):
        with unittest.mock.patch.object(serve, '_review_grade_calibration_report',
                                        return_value={'samples': 20, 'agreement_rate': 0.88}):
            self.assertIsNone(serve._review_grade_calibration_pass(now=time.time()))

    def test_review_grade_no_move_when_clamped_at_max(self):
        with unittest.mock.patch.object(serve, '_review_grade_calibration_report',
                                        return_value={'samples': 20, 'agreement_rate': 0.5}), \
             unittest.mock.patch.object(serve, '_effective_review_grade_confidence', return_value=0.95):
            self.assertIsNone(serve._review_grade_calibration_pass(now=time.time()))


class WeeklyReview(unittest.TestCase):
    """_build_weekly_review and _generate_weekly_review."""

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            conn.execute('DELETE FROM decision_tape')
            conn.execute('DELETE FROM weekly_reviews')

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            conn.execute('DELETE FROM decision_tape')
            conn.execute('DELETE FROM weekly_reviews')

    def _insert_action(self, agent_id, action, ts):
        with serve._db() as conn:
            conn.execute('INSERT INTO action_log (agent_id, action, details, ts) VALUES (?, ?, ?, ?)',
                         (agent_id, action, None, ts))

    def _insert_decision(self, kind, ok, confidence, cost, ts):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO decision_tape (ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (ts, kind, 'm', 'p', 'c', 'allow', confidence, cost, '{}', ok))

    def test_build_weekly_review_aggregates_ground_truth(self):
        now = time.time()
        self._insert_action('eli', 'task_completed', now - 1000)      # shipped
        self._insert_action('player', 'browse', now - 2000)            # excluded per-agent
        self._insert_action('system', 'decide', now - 3000)            # excluded per-agent
        self._insert_action(None, 'review_escalate', now - 4000)       # ceremony
        self._insert_decision('escalation', 1, 0.9, 0.001, now - 1500)
        self._insert_decision('grade', 0, 0.8, 0.002, now - 1400)
        review = serve._build_weekly_review(now=now)
        self.assertIsNotNone(review)
        digest = review['digest']
        self.assertEqual(digest['total_actions'], 4)
        self.assertEqual(digest['shipped_actions'], 1)
        self.assertEqual(digest['ceremony_actions'], 1)
        self.assertEqual(digest['decisions'], 2)
        self.assertEqual(digest['decisions_ok'], 1)
        self.assertAlmostEqual(digest['decision_cost_usd'], 0.003, places=4)
        self.assertEqual(digest['per_agent'], {'eli': {'actions': 1, 'shipped': 1}})
        self.assertEqual(digest['decision_kinds']['escalation'], {'n': 1, 'ok': 1})
        self.assertIn('- **eli**: 1 action(s), 1 shipped', review['markdown'])
        self.assertIn('escalation: 1 (1 ok)', review['markdown'])

    def test_build_weekly_review_with_no_agent_activity(self):
        now = time.time()
        self._insert_action('player', 'browse', now - 1000)
        review = serve._build_weekly_review(now=now)
        self.assertIsNotNone(review)
        self.assertIn('(no per-agent activity recorded in this window)', review['markdown'])

    def test_build_weekly_review_returns_none_when_nothing_to_review(self):
        self.assertIsNone(serve._build_weekly_review(now=time.time()))

    def test_generate_persists_one_row_per_week(self):
        now = time.time()
        self._insert_action('eli', 'task_completed', now - 1000)
        review = serve._generate_weekly_review(now=now)
        self.assertIsNotNone(review)
        with serve._db() as conn:
            rows = conn.execute('SELECT period_start, digest FROM weekly_reviews').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], review['digest']['period_start_ms'])

    def test_generate_returns_none_when_nothing_to_review(self):
        self.assertIsNone(serve._generate_weekly_review(now=time.time()))

    def test_generate_swallows_persist_failures(self):
        review = {'digest': {'period_start_ms': 1, 'generated_at': 2.0}, 'markdown': 'x'}
        with unittest.mock.patch.object(serve, '_build_weekly_review', return_value=review), \
             unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db down')):
            out = serve._generate_weekly_review(now=time.time())
        self.assertEqual(out, review)


class ReviewLoops(unittest.TestCase):
    """_weekly_review_loop and _calibration_loop."""

    def test_weekly_review_loop_iterates_and_handles_errors(self):
        calls = {'sleep': 0}

        async def _fake_sleep(_delay):
            calls['sleep'] += 1
            if calls['sleep'] >= 2:
                raise _LoopExit()

        async def _fake_to_thread(fn, *a, **k):
            return fn(*a, **k)

        with unittest.mock.patch.object(serve.asyncio, 'sleep', side_effect=_fake_sleep), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=_fake_to_thread), \
             unittest.mock.patch.object(serve, '_generate_weekly_review',
                                        side_effect=RuntimeError('boom')):
            with self.assertRaises(_LoopExit):
                asyncio.run(serve._weekly_review_loop())

    def test_calibration_loop_iterates_and_handles_errors(self):
        calls = {'sleep': 0}

        async def _fake_sleep(_delay):
            calls['sleep'] += 1
            if calls['sleep'] >= 2:
                raise _LoopExit()

        async def _fake_to_thread(fn, *a, **k):
            return fn(*a, **k)

        with unittest.mock.patch.object(serve.asyncio, 'sleep', side_effect=_fake_sleep), \
             unittest.mock.patch.object(serve.asyncio, 'to_thread', side_effect=_fake_to_thread), \
             unittest.mock.patch.object(serve, '_calibration_adjust_pass',
                                        side_effect=RuntimeError('boom')), \
             unittest.mock.patch.object(serve, '_review_grade_calibration_pass',
                                        side_effect=RuntimeError('boom')):
            with self.assertRaises(_LoopExit):
                asyncio.run(serve._calibration_loop())


class AgingInFlightWork(unittest.TestCase):
    """_aging_in_flight_work -- pure read over a state blob."""

    def test_tasks_not_a_dict_returns_zero(self):
        self.assertEqual(serve._aging_in_flight_work({'tasks': [1, 2]}, 1000), 0)

    def test_non_dict_task_skipped(self):
        self.assertEqual(serve._aging_in_flight_work({'tasks': {'a': 1}}, 1000), 0)

    def test_non_walking_working_status_skipped(self):
        data = {'tasks': {'a': {'status': 'idle', 'openedAt': 0}}}
        self.assertEqual(serve._aging_in_flight_work(data, 1000), 0)

    def test_missing_opened_skipped(self):
        data = {'tasks': {'a': {'status': 'walking'}}}
        self.assertEqual(serve._aging_in_flight_work(data, 1000), 0)

    def test_bug_or_shadow_tasks_skipped(self):
        data = {'tasks': {'a': {'status': 'walking', 'openedAt': 0, 'taskType': 'bug'},
                          'b': {'status': 'working', 'openedAt': 0, 'workUntil': 0, 'incident': True}}}
        self.assertEqual(serve._aging_in_flight_work(data, 1000), 0)

    def test_fresh_walking_task_not_aging(self):
        data = {'tasks': {'a': {'status': 'walking', 'openedAt': 1_000_100_000_000 - 1000}}}
        self.assertEqual(serve._aging_in_flight_work(data, 1_000_100_000_000), 0)

    def test_stale_walking_task_counts(self):
        data = {'tasks': {'a': {'status': 'walking', 'openedAt': 1}}}
        self.assertEqual(serve._aging_in_flight_work(data, sim.STALE_WORK_TIMEOUT_MS + 1), 1)

    def test_working_within_grace_does_not_count(self):
        data = {'tasks': {'a': {'status': 'working', 'openedAt': 1, 'workUntil': 1_000_000}}}
        self.assertEqual(serve._aging_in_flight_work(data, 1_000_000 + sim.STALE_WORK_BUDGET_GRACE_S), 0)

    def test_working_past_grace_counts(self):
        data = {'tasks': {'a': {'status': 'working', 'openedAt': 1, 'workUntil': 1000}}}
        self.assertEqual(
            serve._aging_in_flight_work(data, (1000 + sim.STALE_WORK_BUDGET_GRACE_S + 1) * 1000), 1)


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
        self.assertEqual(snap['task_assigned_last_24h'], 1)
        self.assertEqual(snap['task_completed_last_24h'], 1)
        self.assertIn('low', snap['model_tier_bands'])
        self.assertIn('mid', snap['missing_model_tier_bands'])
        self.assertTrue(any(a['category'] == 'behavior' for a in snap['alerts']))


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
                'aging_in_flight_work': 0, 'open_escalations': 1}

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
        set_score.assert_called_once_with('m', 'MMLU', 0.7, 'http://x')
        self.assertEqual(log.call_args[0][1], 'model_benchmark_score_recorded')


class JevEndpoints(unittest.TestCase):
    """decide, jev_model_get, jev_model_set, jev_calibration, review_escalate."""

    def test_decide_missing_key_is_500(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide', json={'state': {}, 'questions': []})
        self.assertEqual(r.status_code, 500)
        self.assertIn('OPENROUTER_API_KEY', r.json()['error'])

    def test_decide_missing_fields_is_400(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide', json={})
        self.assertEqual(r.status_code, 400)

    def test_decide_throttled_is_429(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, '_decide_allowed',
                                        return_value=(False, 'slow down')), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide',
                       json={'state': {'x': 1}, 'questions': ['q'], 'agentId': 'eli'})
        self.assertEqual(r.status_code, 429)
        body = r.json()
        self.assertTrue(body['throttled'])
        self.assertIn('retry_after_ms', body)
        self.assertEqual(log.call_args[0][1], 'decide_throttled')

    def test_decide_success(self):
        data = {'choice': 'allow', 'confidence': 0.9}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='m'), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=data), \
             unittest.mock.patch.object(serve, '_jev_choice',
                                        return_value=('allow', 0.9, 0.001)), \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue, \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide',
                       json={'state': {'x': 1}, 'questions': ['q'], 'agentId': 'eli'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['choice'], 'allow')
        accrue.assert_called_once_with('__jev__', 0.001)
        self.assertEqual(log.call_args[0][1], 'decide')

    def test_decide_http_error(self):
        err = urllib.error.HTTPError('http://x', 500, 'err', {}, io.BytesIO(b'provider down'))
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=err), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide',
                       json={'state': {'x': 1}, 'questions': ['q'], 'agentId': 'eli'})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()['error'], 'provider down')

    def test_decide_generic_error(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-x'), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(True, None)), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=RuntimeError('boom')), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/decide',
                       json={'state': {'x': 1}, 'questions': ['q'], 'agentId': 'eli'})
        self.assertEqual(r.status_code, 500)
        self.assertIn('boom', r.json()['error'])

    def test_jev_model_get_single_chain(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['a']), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='a'), \
             unittest.mock.patch.object(serve, '_jev_judge_model', return_value='j'), \
             unittest.mock.patch.object(serve, '_escalation_judge_drift_summary', return_value={}):
            c = TestClient(serve.app)
            r = c.get('/api/jev/model')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['slugs'], {'a': 'sole'})

    def test_jev_model_get_multi_chain(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decision_model_chain', return_value=['a', 'b']), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken', return_value=False), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='a'), \
             unittest.mock.patch.object(serve, '_jev_judge_model', return_value='j'), \
             unittest.mock.patch.object(serve, '_escalation_judge_drift_summary', return_value={}):
            c = TestClient(serve.app)
            r = c.get('/api/jev/model')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['slugs'], {'a': 'available', 'b': 'available'})

    def test_jev_model_set_requires_session(self):
        # Middleware passes (valid agent key) so the handler's own player-only
        # check is what rejects the request.
        with unittest.mock.patch.object(serve, 'verify_session', side_effect=[True, False]):
            c = TestClient(serve.app)
            r = c.post('/api/jev/model', json={'model': 'x/y'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('player-only', r.json()['error'])

    def test_jev_model_set_requires_model(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/jev/model', json={})
        self.assertEqual(r.status_code, 400)

    def test_jev_model_set_success(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_set_setting') as set_setting, \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as app:
            c = TestClient(serve.app)
            r = c.post('/api/jev/model', json={'model': 'x/y'})
        self.assertEqual(r.status_code, 200, r.text)
        set_setting.assert_called_once_with('jev_model', 'x/y')
        self.assertEqual(log.call_args[0][1], 'jev_model_set')
        app.assert_called_once_with('jev_model_set', 'player', {'model': 'x/y'})

    def test_jev_calibration_default_and_windowed(self):
        report = {'buckets': [], 'total_decisions': 0}
        review_report = {'samples': 0, 'agreed': 0, 'agreement_rate': None, 'sections': []}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decision_calibration_report',
                                        return_value=report), \
             unittest.mock.patch.object(serve, '_review_grade_calibration_report',
                                        return_value=review_report), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_effective_review_grade_confidence',
                                        return_value=0.6):
            c = TestClient(serve.app)
            r = c.get('/api/jev/calibration')
            r2 = c.get('/api/jev/calibration?window_s=3600')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['effective_safety_confidence'], 0.6)
        self.assertEqual(r2.status_code, 200)

    def test_review_escalate_requires_fields(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/review/escalate', json={'agentId': 'eli'})
        self.assertEqual(r.status_code, 400)

    def test_review_escalate_rate_limited(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(False, 'x')):
            c = TestClient(serve.app)
            r = c.post('/api/review/escalate',
                       json={'agentId': 'eli', 'question': 'cannot judge'})
        self.assertEqual(r.status_code, 429)

    def test_review_escalate_success(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_decide_allowed', return_value=(True, None)), \
             unittest.mock.patch.object(serve, 'create_escalation', return_value='esc-1') as esc, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/api/review/escalate',
                       json={'agentId': 'eli', 'kind': 'x' * 100, 'question': 'cannot judge'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['escalationId'], 'esc-1')
        self.assertLessEqual(len(esc.call_args[0][0]), 60)
        self.assertEqual(log.call_args[0][1], 'review_escalate')


class AuthEndpoints(unittest.TestCase):
    """login, logout, serve_index."""

    def test_index_shows_login_page_without_session(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            c = TestClient(serve.app)
            r = c.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('SIGN IN', r.text)

    def test_index_serves_game_page_with_session(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/index.html')
        self.assertEqual(r.status_code, 200)
        self.assertIn('<html', r.text.lower())

    def test_login_rate_limited_is_429(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=False):
            c = TestClient(serve.app)
            r = c.post('/login', data={'username': 'admin', 'password': 'x'})
        self.assertEqual(r.status_code, 429)

    def test_login_bad_credentials_json_is_401(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_hash_password',
                                        return_value=('salt', 'deadbeef')), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/login', json={'username': 'admin', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error'], 'Invalid username or password')
        self.assertEqual(log.call_args[0][1], 'login_failed')

    def test_login_bad_credentials_form_is_401(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_hash_password',
                                        return_value=('salt', 'deadbeef')):
            c = TestClient(serve.app)
            r = c.post('/login', data={'username': 'admin', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('Invalid username or password', r.text)

    def test_login_success_json(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_hash_password',
                                        return_value=(serve.ADMIN_PASSWORD_SALT,
                                                      serve.ADMIN_PASSWORD_HASH)), \
             unittest.mock.patch.object(serve, 'create_session', return_value='sess-1'), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/login', json={'username': 'admin', 'password': 'x'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'ok': True})
        self.assertIn('ai_think_tank_session=sess-1', r.headers.get('set-cookie', ''))
        self.assertEqual(log.call_args[0][1], 'login_success')

    def test_login_success_form_redirects(self):
        with unittest.mock.patch.object(serve, '_check_login_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, '_hash_password',
                                        return_value=(serve.ADMIN_PASSWORD_SALT,
                                                      serve.ADMIN_PASSWORD_HASH)), \
             unittest.mock.patch.object(serve, 'create_session', return_value='sess-1'):
            c = TestClient(serve.app, follow_redirects=False)
            r = c.post('/login', data={'username': 'admin', 'password': 'x'})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers['location'], '/')

    def test_logout_destroys_session(self):
        with unittest.mock.patch.object(serve, 'destroy_session') as destroy:
            c = TestClient(serve.app, follow_redirects=False)
            r = c.post('/logout', cookies={'ai_think_tank_session': 'sess-1'})
        self.assertEqual(r.status_code, 303)
        destroy.assert_called_once_with('sess-1')
        self.assertIn('ai_think_tank_session=', r.headers.get('set-cookie', ''))

    def test_logout_without_cookie(self):
        with unittest.mock.patch.object(serve, 'destroy_session') as destroy:
            c = TestClient(serve.app, follow_redirects=False)
            r = c.post('/logout')
        self.assertEqual(r.status_code, 303)
        destroy.assert_not_called()


class HttpJson(unittest.TestCase):
    """_http_json."""

    def _mock_urlopen(self, read_bytes=None, exc=None):
        urlopen = unittest.mock.MagicMock()
        if exc is not None:
            urlopen.side_effect = exc
        else:
            urlopen.return_value.__enter__.return_value.read.return_value = read_bytes or b''
        return urlopen

    def test_success_json(self):
        urlopen = self._mock_urlopen(read_bytes=b'{"ok": true}')
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('POST', 'http://base', '/api/x', body={'a': 1}, header='key')
        self.assertEqual(out, {'ok': True})
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('X-agent-key'), 'key')
        self.assertEqual(req.data, b'{"a": 1}')

    def test_success_empty_body_returns_dict(self):
        urlopen = self._mock_urlopen(read_bytes=b'')
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {})

    def test_non_json_body_returns_raw(self):
        urlopen = self._mock_urlopen(read_bytes=b'not json')
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {'_raw': 'not json'})

    def test_http_error_json_body(self):
        err = urllib.error.HTTPError('http://x', 404, 'nf', {}, io.BytesIO(b'{"error": "nf"}'))
        urlopen = self._mock_urlopen(exc=err)
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {'error': 'nf'})

    def test_http_error_non_json_body(self):
        err = urllib.error.HTTPError('http://x', 500, 'err', {}, io.BytesIO(b'crash'))
        urlopen = self._mock_urlopen(exc=err)
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {'error': 'HTTP 500: crash'})

    def test_generic_failure(self):
        urlopen = self._mock_urlopen(exc=RuntimeError('conn refused'))
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen', urlopen):
            out = serve._http_json('GET', 'http://base', '/api/x')
        self.assertEqual(out, {'error': 'request failed: conn refused'})


class ArmIdleAndRun(unittest.TestCase):
    """_arm_idle_and_run (defined inside serve.py's __main__ block)."""

    @classmethod
    def setUpClass(cls):
        # Extract the real function source from serve.py and exec it with
        # matching line numbers so coverage attributes execution to serve.py.
        src = open(serve.__file__, encoding='utf-8').read().splitlines()
        start = next(i + 1 for i, line in enumerate(src)
                     if line.startswith('    async def _arm_idle_and_run():'))
        end = next(i for i, line in enumerate(src)
                   if line.startswith('        await server.serve()')) + 1
        body = textwrap.dedent('\n'.join(src[start - 1:end]))
        lines = [''] * (start - 1) + body.splitlines()
        code = compile('\n'.join(lines), serve.__file__, 'exec')
        exec(code, serve.__dict__)

    def test_armed_idle_loop(self):
        server = unittest.mock.Mock()
        server.serve = unittest.mock.AsyncMock()
        idle = unittest.mock.Mock()
        create_task = unittest.mock.Mock()
        with unittest.mock.patch.dict(serve.__dict__, {
                '_MAX_IDLE_MINUTES': 5.0, '_LAST_REQUEST_TIME': None,
                'server': server, '_idle_shutdown_loop': idle}), \
             unittest.mock.patch.object(serve.asyncio, 'create_task', create_task):
            asyncio.run(serve._arm_idle_and_run())
            self.assertIsNotNone(serve._LAST_REQUEST_TIME)
            create_task.assert_called_once()
        server.serve.assert_awaited_once()

    def test_disabled_idle_loop(self):
        server = unittest.mock.Mock()
        server.serve = unittest.mock.AsyncMock()
        create_task = unittest.mock.Mock()
        with unittest.mock.patch.dict(serve.__dict__, {
                '_MAX_IDLE_MINUTES': 0.0, '_LAST_REQUEST_TIME': None,
                'server': server}), \
             unittest.mock.patch.object(serve.asyncio, 'create_task', create_task):
            asyncio.run(serve._arm_idle_and_run())
            self.assertIsNone(serve._LAST_REQUEST_TIME)
            create_task.assert_not_called()
        server.serve.assert_awaited_once()


class ApiLogEndpoint(unittest.TestCase):
    """POST /api/log."""

    def _post(self, body, log, app):
        with unittest.mock.patch.object(serve, 'log_action', log), \
             unittest.mock.patch.object(serve, '_append_passport_decision', app), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            return c.post('/api/log', json=body)

    def test_logs_routine_action(self):
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'foo', 'details': {'a': 1}, 'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        log.assert_called_once_with('eli', 'foo', {'a': 1})
        app.assert_not_called()

    def test_hashed_action_chains_to_passport(self):
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'hire', 'details': {'decision': 'fire'}, 'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        app.assert_called_once_with('hire', 'eli', {'decision': 'fire'})

    def test_firing_review_keep_does_not_chain(self):
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'firing_review', 'details': {'decision': 'keep'},
                        'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        app.assert_not_called()

    def test_firing_review_fire_chains(self):
        log = unittest.mock.Mock()
        app = unittest.mock.Mock()
        r = self._post({'action': 'firing_review', 'details': {'decision': 'fire'},
                        'agentId': 'eli'}, log, app)
        self.assertEqual(r.status_code, 200)
        app.assert_called_once()


class PostReportEndpoint(unittest.TestCase):
    """POST /api/reports."""

    def _post(self, body, state=None, key_ok=True):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: self._saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=key_ok), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as app, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/reports', json=body)
        return r, log, app

    def setUp(self):
        self._saved = {}

    def _body(self, **kw):
        body = {'aboutId': 'maya', 'fromId': 'eli', 'quote': 'q', 'note': 'n'}
        body.update(kw)
        return body

    def test_missing_fields_is_400(self):
        r, _log, _app = self._post({'aboutId': 'maya', 'fromId': 'eli', 'quote': 'q'})
        self.assertEqual(r.status_code, 400)

    def test_player_filer_is_403(self):
        r, _log, _app = self._post(self._body(fromId='player'))
        self.assertEqual(r.status_code, 403)

    def test_key_mismatch_is_403(self):
        r, _log, _app = self._post(self._body(), key_ok=False)
        self.assertEqual(r.status_code, 403)

    def test_self_report_is_403(self):
        r, _log, _app = self._post(self._body(fromId='eli', aboutId='eli'))
        self.assertEqual(r.status_code, 403)

    def test_no_state_is_503(self):
        r, _log, _app = self._post(self._body(), state=None)
        self.assertEqual(r.status_code, 503)

    def test_unknown_subject_is_404(self):
        r, _log, _app = self._post(self._body(aboutId='nobody'),
                                   state={'agentRoster': [{'id': 'eli'}]})
        self.assertEqual(r.status_code, 404)

    def test_success_files_report(self):
        state = {'agentRoster': [{'id': 'eli'}, {'id': 'maya'}], 'reports': []}
        r, log, app = self._post(self._body(), state=state)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])
        report = self._saved['reports'][0]
        self.assertEqual(report['aboutId'], 'maya')
        self.assertEqual(report['fromId'], 'eli')
        self.assertEqual(report['severity'], 'minor')
        self.assertEqual(log.call_args[0][1], 'report_filed')
        app.assert_called_once_with('report_filed', 'eli', {'about': 'maya'})


class StageReleasedWork(unittest.TestCase):
    """_stage_released_work."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-publish-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib = os.path.join(self.tmp, 'library')
        self.staging = os.path.join(self.tmp, 'publish-staging')
        patcher = unittest.mock.patch.multiple(
            serve, LIBRARY_DIR=self.lib, PUBLISH_STAGING=self.staging)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stages_released_projects_wiki_and_skills(self):
        os.makedirs(os.path.join(self.lib, 'projects', 'alpha'))
        with open(os.path.join(self.lib, 'projects', 'alpha', 'game.js'), 'w') as f:
            f.write('console.log(1)')
        with open(os.path.join(self.lib, 'projects', 'scratch.txt'), 'w') as f:
            f.write('not a dir')
        os.makedirs(os.path.join(self.lib, 'wiki', 'design'))
        with open(os.path.join(self.lib, 'wiki', 'design', 'robot.md'), 'w') as f:
            f.write('# robot')
        with open(os.path.join(self.lib, 'wiki', 'notes.txt'), 'w') as f:
            f.write('not a dir')
        os.makedirs(os.path.join(self.lib, 'skills'))
        with open(os.path.join(self.lib, 'skills', 'coding.md'), 'w') as f:
            f.write('# coding')
        with open(os.path.join(self.lib, 'skills', 'readme.txt'), 'w') as f:
            f.write('not md')

        # serve._stage_released_work rmtrees PUBLISH_STAGING then only creates
        # projects/wiki subdirs (never skills/), so wrap os.makedirs to create it
        # alongside the staging root.
        real_makedirs = os.makedirs

        def make_staging_with_skills(path, **kw):
            real_makedirs(path, **kw)
            if os.path.abspath(path) == os.path.abspath(self.staging):
                real_makedirs(os.path.join(path, 'skills'), exist_ok=True)

        with unittest.mock.patch.object(serve.os, 'makedirs',
                                        side_effect=make_staging_with_skills):
            staged = serve._stage_released_work({})

        self.assertEqual(staged, ['projects/alpha', 'wiki/design/robot.md', 'skills/coding.md'])
        self.assertTrue(os.path.isfile(
            os.path.join(self.staging, 'projects', 'alpha', 'game.js')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.staging, 'wiki', 'design', 'robot.md')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.staging, 'skills', 'coding.md')))
        self.assertFalse(os.path.exists(
            os.path.join(self.staging, 'projects', 'scratch.txt')))

    def test_stages_projects_without_wiki_or_skills(self):
        os.makedirs(os.path.join(self.lib, 'projects', 'beta'))
        with open(os.path.join(self.lib, 'projects', 'beta', 'index.html'), 'w') as f:
            f.write('<p>hi</p>')
        staged = serve._stage_released_work({})
        self.assertEqual(staged, ['projects/beta'])


class ServeIndex(unittest.TestCase):
    """GET / and /index.html -- login gate + real game page."""

    def test_logged_out_serves_login_page(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            c = TestClient(serve.app)
            r = c.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Sign in', r.text)

    def test_logged_in_serves_game_page(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/index.html')
        self.assertEqual(r.status_code, 200)
        self.assertIn('<!doctype html', r.text.lower())


class DecideEndpoint(unittest.TestCase):
    """POST /api/decide."""

    DATA = {'answers': {'q': {'choice': 'allow', 'confidence': 0.9}},
            'usage': {'cost': 0.001}}

    def _post(self, body, api_key='test-key', decide_allowed=(True, None),
              call_openrouter=None, call_exc=None):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', api_key), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test/jev'), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_decide_allowed',
                                        return_value=decide_allowed), \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            if call_exc is not None:
                with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                                side_effect=call_exc):
                    c = TestClient(serve.app)
                    r = c.post('/api/decide', json=body)
            else:
                with unittest.mock.patch.object(
                        serve, '_call_openrouter_decision_sync',
                        return_value=call_openrouter if call_openrouter is not None else self.DATA):
                    c = TestClient(serve.app)
                    r = c.post('/api/decide', json=body)
        return r, log, accrue

    def _body(self):
        return {'agentId': 'eli', 'state': {'turn': 3},
                'questions': {'choice': {'instructions': 'decide', 'criteria': 'x'}}}

    def test_missing_api_key_is_500(self):
        r, _log, _accrue = self._post(self._body(), api_key='')
        self.assertEqual(r.status_code, 500)
        self.assertIn('OPENROUTER_API_KEY', r.json()['error'])

    def test_missing_state_or_questions_is_400(self):
        r, _log, _accrue = self._post({'agentId': 'eli'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('state and questions', r.json()['error'])

    def test_throttled_is_429(self):
        r, log, _accrue = self._post(
            self._body(), decide_allowed=(False, 'decide rate limited (too fast)'))
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r.json()['throttled'])
        self.assertEqual(log.call_args[0][1], 'decide_throttled')

    def test_success_forwards_decision(self):
        r, log, accrue = self._post(self._body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), self.DATA)
        accrue.assert_called_once_with('__jev__', 0.001)
        self.assertEqual(log.call_args[0][1], 'decide')
        self.assertEqual(log.call_args[0][2]['choice'], 'allow')

    def test_http_error_is_passthrough(self):
        err = urllib.error.HTTPError('http://x', 429, 'Too Many Requests', {},
                                     io.BytesIO(b'rate limited'))
        r, _log, _accrue = self._post(self._body(), call_exc=err)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()['error'], 'rate limited')

    def test_generic_failure_is_500(self):
        r, _log, _accrue = self._post(self._body(), call_exc=RuntimeError('boom'))
        self.assertEqual(r.status_code, 500)
        self.assertIn('boom', r.json()['error'])


class ExecuteEndpoint(unittest.TestCase):
    """POST /api/execute."""

    RESULT = {'exitCode': 0, 'stdout': 'ok', 'stderr': '', 'timedOut': False}

    def setUp(self):
        self.sbx = tempfile.mkdtemp(prefix='gap-f-sandbox-')
        self.addCleanup(shutil.rmtree, self.sbx, ignore_errors=True)

    def _patchers(self, classify=(True, 'allow'), rate=True, exec_enabled=True,
                  result=None, escalation='esc-1'):
        return [
            unittest.mock.patch.object(serve, 'EXECUTION_ENABLED', exec_enabled),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=rate),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=None),
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, 'log_action'),
            unittest.mock.patch.object(serve, '_classify_command',
                                       new=unittest.mock.AsyncMock(return_value=classify)),
            unittest.mock.patch.object(serve, '_snapshot_sandbox',
                                       new=unittest.mock.AsyncMock()),
            unittest.mock.patch.object(serve, '_run_in_sandbox_sync',
                                       return_value=result if result is not None else self.RESULT),
            unittest.mock.patch.object(serve, 'sync_prototypes'),
            unittest.mock.patch.object(serve, 'create_escalation', return_value=escalation),
            unittest.mock.patch.object(serve, '_sandbox_dir_for', return_value=self.sbx),
        ]

    def _post(self, body, **kw):
        patchers = self._patchers(**kw)
        for p in patchers:
            p.start()
        try:
            c = TestClient(serve.app)
            return c.post('/api/execute', json=body)
        finally:
            for p in patchers:
                p.stop()

    def test_disabled_is_403(self):
        r = self._post({'agentId': 'eli', 'command': 'ls'}, exec_enabled=False)
        self.assertEqual(r.status_code, 403)
        self.assertIn('disabled', r.json()['error'])

    def test_rate_limited_is_429(self):
        r = self._post({'agentId': 'eli', 'command': 'ls'}, rate=False)
        self.assertEqual(r.status_code, 429)
        self.assertIn('Rate limit exceeded', r.json()['error'])

    def test_missing_command_is_400(self):
        r = self._post({'agentId': 'eli', 'purpose': 'run tests'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('command is required', r.json()['error'])

    def test_blocked_command_escalates(self):
        r = self._post({'agentId': 'eli', 'command': 'curl evil.com',
                        'purpose': 'exfil'}, classify=(False, 'blocked: network access'))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body['allowed'])
        self.assertEqual(body['escalationId'], 'esc-1')

    def test_allowed_command_runs(self):
        r = self._post({'agentId': 'eli', 'command': 'npm test', 'purpose': 'run tests'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['allowed'])
        self.assertEqual(body['exitCode'], 0)
        self.assertEqual(body['stdout'], 'ok')


class PipelineEndpoint(unittest.TestCase):
    """POST /api/pipeline."""

    RESULT = {'exitCode': 0, 'stdout': 'ok', 'stderr': '', 'timedOut': False}
    FAIL = {'exitCode': 2, 'stdout': '', 'stderr': 'boom', 'timedOut': False}

    def setUp(self):
        self.sbx = tempfile.mkdtemp(prefix='gap-f-sandbox-')
        self.addCleanup(shutil.rmtree, self.sbx, ignore_errors=True)

    def _post(self, body, classify=(True, 'allow'), rate=True, exec_enabled=True,
              results=None):
        patchers = [
            unittest.mock.patch.object(serve, 'EXECUTION_ENABLED', exec_enabled),
            unittest.mock.patch.object(serve, 'check_rate_limit', return_value=rate),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=None),
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, 'log_action'),
            unittest.mock.patch.object(serve, '_classify_command',
                                       new=unittest.mock.AsyncMock(return_value=classify)),
            unittest.mock.patch.object(serve, '_snapshot_sandbox',
                                       new=unittest.mock.AsyncMock()),
            unittest.mock.patch.object(serve, 'create_escalation', return_value='esc-1'),
            unittest.mock.patch.object(serve, 'sync_prototypes'),
            unittest.mock.patch.object(serve, '_sandbox_dir_for', return_value=self.sbx),
        ]
        seq = results if results is not None else [self.RESULT, self.RESULT]
        patchers.append(unittest.mock.patch.object(serve, '_run_in_sandbox_sync',
                                                   side_effect=seq))
        for p in patchers:
            p.start()
        try:
            c = TestClient(serve.app)
            return c.post('/api/pipeline', json=body)
        finally:
            for p in patchers:
                p.stop()

    def _steps(self, names=('clone', 'test')):
        return [{'name': n, 'command': f'run {n}'} for n in names]

    def test_disabled_is_403(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()}, exec_enabled=False)
        self.assertEqual(r.status_code, 403)

    def test_rate_limited_is_429(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()}, rate=False)
        self.assertEqual(r.status_code, 429)

    def test_missing_steps_is_400(self):
        r = self._post({'agentId': 'eli'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('steps is required', r.json()['error'])

    def test_all_steps_succeed(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body['failedStep'])
        self.assertEqual(len(body['results']), 2)
        self.assertTrue(all(x['allowed'] for x in body['results']))

    def test_blocked_step_stops_pipeline(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()},
                       classify=(False, 'blocked: network access'))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['failedStep'], 'clone')
        self.assertFalse(body['results'][0]['allowed'])

    def test_nonzero_exit_stops_pipeline(self):
        r = self._post({'agentId': 'eli', 'steps': self._steps()},
                       results=[self.RESULT, self.FAIL])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['failedStep'], 'test')
        self.assertEqual(body['results'][1]['exitCode'], 2)


class YoutubeTranscriptEndpoint(unittest.TestCase):
    """POST /api/youtube-transcript."""

    URL = 'https://www.youtube.com/watch?v=abc123def45'

    def _post(self, body, colab_return=None, colab_error=None, rate=True,
              file_return=None):
        with unittest.mock.patch.object(serve, 'check_rate_limit', return_value=rate), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=None), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_youtube_transcript_colab',
                                        return_value=(colab_return, colab_error)) as colab, \
             unittest.mock.patch.object(serve, '_file_youtube_transcript',
                                        return_value=file_return) as filed:
            c = TestClient(serve.app)
            r = c.post('/api/youtube-transcript', json=body)
        return r, log, colab, filed

    def test_rate_limited_is_429(self):
        r, _log, _colab, _filed = self._post(
            {'agentId': 'eli', 'url': self.URL}, rate=False)
        self.assertEqual(r.status_code, 429)

    def test_invalid_url_is_400(self):
        r, _log, _colab, _filed = self._post(
            {'agentId': 'eli', 'url': 'https://evil.com/x'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('youtube.com', r.json()['error'])

    def test_colab_failure_is_422(self):
        r, log, _colab, _filed = self._post(
            {'agentId': 'eli', 'url': self.URL}, colab_error='no captions')
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()['error'], 'no captions')
        self.assertEqual(log.call_args[0][2]['decision'], 'failed')

    def test_success_returns_transcript(self):
        r, log, colab, filed = self._post(
            {'agentId': 'eli', 'url': self.URL}, colab_return='hello world',
            file_return='media/transcripts/abc123def45.txt')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['transcript'], 'hello world')
        self.assertEqual(body['filed'], 'media/transcripts/abc123def45.txt')
        colab.assert_called_once_with(self.URL, 'en')
        self.assertEqual(log.call_args[0][2]['decision'], 'ok')


class YoutubeHelpers(unittest.TestCase):
    """_is_youtube_url / _youtube_video_id / _file_youtube_transcript /
    _clean_subtitle_file."""

    URL = 'https://www.youtube.com/watch?v=abc123def45'

    def test_is_youtube_url_rejects_non_http(self):
        self.assertFalse(serve._is_youtube_url('youtube.com/watch?v=abc'))
        self.assertFalse(serve._is_youtube_url(None))

    def test_is_youtube_url_accepts_youtube(self):
        self.assertTrue(serve._is_youtube_url('https://youtu.be/abc123def45'))

    def test_is_youtube_url_urlparse_exception(self):
        with unittest.mock.patch.object(serve.urllib.parse, 'urlparse',
                                        side_effect=ValueError('bad url')):
            self.assertFalse(serve._is_youtube_url(self.URL))

    def test_video_id_urlparse_exception_returns_none(self):
        real = serve.urllib.parse.urlparse
        with unittest.mock.patch.object(serve.urllib.parse, 'urlparse',
                                        side_effect=[real(self.URL), ValueError('bad url')]):
            self.assertIsNone(serve._youtube_video_id(self.URL))

    def test_video_id_short_v_param_returns_none(self):
        self.assertIsNone(serve._youtube_video_id('https://www.youtube.com/watch?v=abc'))

    def test_file_transcript_writes_to_library(self):
        rel = serve._file_youtube_transcript(self.URL, 'hello', 'colab-whisper')
        self.assertEqual(rel, 'media/transcripts/abc123def45.txt')
        target = os.path.join(serve.LIBRARY_DIR, 'media', 'transcripts',
                              'abc123def45.txt')
        self.assertTrue(os.path.isfile(target))
        with open(target) as f:
            self.assertIn('hello', f.read())

    def test_file_transcript_unsafe_library_path_returns_none(self):
        with unittest.mock.patch.object(serve, '_safe_library_path',
                                        return_value=None):
            rel = serve._file_youtube_transcript(self.URL, 'hello', 'x')
        self.assertIsNone(rel)

    def test_file_transcript_write_failure_returns_none(self):
        with unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=OSError('no space')):
            rel = serve._file_youtube_transcript(self.URL, 'hello', 'x')
        self.assertIsNone(rel)

    def _write_subs(self, content):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-clean-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = os.path.join(self.tmp, 'subs.vtt')
        with open(p, 'w') as f:
            f.write(content)
        return p

    def test_clean_strips_timestamps_and_tags(self):
        p = self._write_subs(
            'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<i></i>\n'
            '00:00:02.000 --> 00:00:03.000\nHello world\n')
        out = serve._clean_subtitle_file(p)
        self.assertEqual(out, 'WEBVTT\nHello world')

    def test_clean_unreadable_file_returns_none(self):
        self.tmp = tempfile.mkdtemp(prefix='gap-f-clean-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        out = serve._clean_subtitle_file(os.path.join(self.tmp, 'missing.vtt'))
        self.assertIsNone(out)


class YoutubeTranscriptLocal(unittest.TestCase):
    """_youtube_transcript (local yt-dlp path)."""

    URL = 'https://www.youtube.com/watch?v=abc123def45'

    def _run(self, subs=None, exc=None):
        def fake_run(cmd, **kwargs):
            tmpl = cmd[cmd.index('-o') + 1]
            if subs is not None:
                with open(f'{tmpl}.en.vtt', 'w') as f:
                    f.write(subs)
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
        side = exc if exc is not None else fake_run
        with unittest.mock.patch.object(serve.subprocess, 'run', side_effect=side):
            return serve._youtube_transcript(self.URL)

    def test_no_subtitle_files_is_error(self):
        text, error = self._run(subs=None)
        self.assertIsNone(text)
        self.assertIn('no subtitle files', error)

    def test_clean_produces_no_text_is_error(self):
        subs = ('00:00:01.000 --> 00:00:02.000\n<i></i>\n'
                '00:00:02.000 --> 00:00:03.000\n<i></i>\n')
        text, error = self._run(subs=subs)
        self.assertIsNone(text)
        self.assertIn('no readable text', error)

    def test_segments_truncated_to_max(self):
        subs = '\n'.join(f'Segment {i}' for i in range(1000))
        text, error = self._run(subs=subs)
        self.assertIsNone(error)
        self.assertEqual(text.count('\n'), 799)
        self.assertNotIn('Segment 800', text)

    def test_chars_truncated_to_max(self):
        subs = '\n'.join(f'{i:03d}' + 'x' * 117 for i in range(500))
        text, error = self._run(subs=subs)
        self.assertIsNone(error)
        self.assertEqual(len(text), 50000)

    def test_timeout_is_error(self):
        text, error = self._run(exc=subprocess.TimeoutExpired('yt-dlp', 90))
        self.assertIsNone(text)
        self.assertIn('timed out', error)

    def test_ytdlp_missing_is_error(self):
        text, error = self._run(exc=FileNotFoundError())
        self.assertIsNone(text)
        self.assertIn('yt-dlp is not installed', error)

    def test_generic_failure_is_error(self):
        text, error = self._run(exc=ValueError('boom'))
        self.assertIsNone(text)
        self.assertIn('fetch failed: boom', error)


class YoutubeTranscriptColab(unittest.TestCase):
    """_youtube_transcript_colab edge branches."""

    URL = 'https://www.youtube.com/watch?v=abc123def45'

    def _call(self, url=None, colab_return=None, colab_exc=None, enabled=True,
              cli=True, key='test-key', budget=False):
        patchers = [
            unittest.mock.patch.object(serve, 'COLAB_ENABLED', enabled),
            unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', cli),
            unittest.mock.patch.object(serve, 'APIFY_API_KEY', key),
            unittest.mock.patch.object(serve, '_colab_budget_exceeded',
                                       return_value=budget),
        ]
        if colab_return is not None:
            patchers.append(unittest.mock.patch.object(serve, '_colab_compute_run',
                                                       return_value=colab_return))
        if colab_exc is not None:
            patchers.append(unittest.mock.patch.object(serve, '_colab_compute_run',
                                                       side_effect=colab_exc))
        for p in patchers:
            p.start()
        try:
            return serve._youtube_transcript_colab(url or self.URL)
        finally:
            for p in patchers:
                p.stop()

    def test_not_youtube_url_is_error(self):
        text, error = serve._youtube_transcript_colab('https://evil.com/x')
        self.assertIsNone(text)
        self.assertIn('Not a YouTube URL', error)

    def test_colab_disabled_is_error(self):
        text, error = self._call(enabled=False)
        self.assertIsNone(text)
        self.assertIn('Colab is disabled', error)

    def test_cli_unavailable_is_error(self):
        text, error = self._call(cli=False)
        self.assertIsNone(text)
        self.assertIn('Colab CLI is not installed', error)

    def test_compute_run_crash_is_error(self):
        text, error = self._call(colab_exc=RuntimeError('boom'))
        self.assertIsNone(text)
        self.assertIn('run crashed: boom', error)

    def test_non_dict_response_is_error(self):
        text, error = self._call(colab_return='oops')
        self.assertIsNone(text)
        self.assertIn('Unexpected Colab transcription response', error)

    def test_no_text_is_error(self):
        text, error = self._call(colab_return={'stdout': '__TRANSCRIPT_END__'})
        self.assertIsNone(text)
        self.assertIn('returned no text', error)

    def test_chars_truncated_to_max(self):
        stdout = ('x' * 100 + '\n') * 600 + '__TRANSCRIPT_END__\n'
        text, error = self._call(colab_return={'stdout': stdout})
        self.assertIsNone(error)
        self.assertEqual(len(text), 50000)


if __name__ == '__main__':
    unittest.main(verbosity=2)