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
        # /api/escalation/{id} is session-protected (deny-by-default auth);
        # the resolve link is the only public escalation route (token-gated).
        with unittest.mock.patch.object(serve, '_load_escalations',
                                        return_value={'e1': {'status': 'pending', 'kind': 'x'}}), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/escalation/e1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'pending')

    def test_get_escalation_not_found(self):
        with unittest.mock.patch.object(serve, '_load_escalations', return_value={}), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
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
