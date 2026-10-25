"""Tests for browser_session.py: the long-lived, player-viewable browser.

Docker is replaced with a recording fake, so nothing runs and no ports open.

Run: python3 tests/test_browser_session.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import agents.browser_session as browser_session  # noqa: E402


class _DockerRecorder:
    def __init__(self, run_ok=True):
        self.calls = []
        self.run_ok = run_ok
        self.stdin = []

    def __call__(self, args, timeout=60, input_text=None):
        self.calls.append(list(args))
        self.stdin.append(input_text)
        if args and args[0] == 'run':
            if not self.run_ok:
                return types.SimpleNamespace(returncode=1, stdout='', stderr='boom')
            return types.SimpleNamespace(returncode=0, stdout='cid123\n', stderr='')
        if args and args[0] == 'inspect':
            spec = args[2] if len(args) > 2 else ''
            if 'NetworkSettings.Networks' in spec:
                return types.SimpleNamespace(returncode=0, stdout='172.30.0.5\n', stderr='')
            return types.SimpleNamespace(returncode=0, stdout='true\n', stderr='')
        if args and args[0] == 'exec':
            return types.SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr='')
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-session-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        with browser_session._LOCK:
            browser_session._SESSIONS.clear()
        self.addCleanup(browser_session._SESSIONS.clear)
        self.docker = _DockerRecorder()
        self.patches = [
            unittest.mock.patch.object(browser_session, '_docker', self.docker),
            unittest.mock.patch.object(browser_session, '_free_port', return_value=43210),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _start(self, agent='agent-a'):
        return browser_session.start(
            agent,
            sandbox_dir=os.path.join(self.tmp, 'sandbox'),
            profile_dir=os.path.join(self.tmp, 'profile'),
            proxy_url='http://proxy:8080')


class Identity(Base):
    def test_slug_rejects_empty(self):
        with self.assertRaises(browser_session.BrowserSessionError):
            browser_session._slug('')

    def test_slug_sanitizes(self):
        self.assertEqual(browser_session._slug('a/b c'), 'a_b_c')

    def test_container_name(self):
        self.assertEqual(browser_session.container_name('agent-a'), 'tt-browser-agent-a')

    def test_container_script_finds_both_chromium_layouts(self):
        script = browser_session._container_script()
        self.assertIn('chrome-linux*/chrome', script)
        self.assertIn('--remote-debugging-port=9222', script)
        # x11vnc is started after the view network exists, bound to that IP,
        # so the sandbox network never sees the display. websockify lives in
        # the separate viewer container.
        self.assertNotIn('x11vnc', script)
        self.assertNotIn('websockify', script)

    def test_viewer_url_embeds_password(self):
        url = browser_session.viewer_url(1234, 'pw')
        self.assertIn('127.0.0.1:1234', url)
        self.assertIn('password=pw', url)

    def test_viewer_url_without_password(self):
        self.assertNotIn('password', browser_session.viewer_url(1234, ''))


class Start(Base):
    def test_run_command_shape(self):
        session = self._start()
        self.assertEqual(session['port'], 43210)
        self.assertEqual(session['status'], 'starting')
        self.assertFalse(session['takeover'])
        runs = [c for c in self.docker.calls if c and c[0] == 'run']
        self.assertEqual(len(runs), 2)
        browser = next(c for c in runs if 'websockify' not in c)
        viewer = next(c for c in runs if 'websockify' in c)
        self.assertIn('-d', browser)
        self.assertIn(browser_session.SESSION_NETWORK, browser)
        self.assertNotIn('-p', browser)  # internal network cannot publish
        self.assertIn('/browser-profile', ' '.join(browser))
        self.assertIn(os.path.join(self.tmp, 'sandbox') + ':/workspace', ' '.join(browser))
        self.assertTrue(any(a.startswith('VNC_PASS=') for a in browser))
        self.assertTrue(any('http_proxy=http://proxy:8080' == a for a in browser))
        self.assertIn('-p', viewer)
        self.assertIn('127.0.0.1:43210:6080', viewer)
        self.assertIn('websockify', viewer)
        attach = [c for c in self.docker.calls if c[:2] == ['network', 'connect']]
        self.assertEqual(len(attach), 2)
        self.assertTrue(all(browser_session.SESSION_VIEW_NETWORK in c for c in attach))
        vnc = [c for c in self.docker.calls if c[:2] == ['exec', '-d']]
        self.assertEqual(len(vnc), 1)
        self.assertIn('-listen 172.30.0.5', ' '.join(vnc[0]))

    def test_start_is_idempotent_while_running(self):
        first = self._start()
        self.docker.calls.clear()
        again = self._start()
        self.assertEqual(first['token'], again['token'])
        self.assertFalse(any(c and c[0] == 'run' for c in self.docker.calls))

    def test_docker_failure_raises(self):
        self.docker.run_ok = False
        with self.assertRaises(browser_session.BrowserSessionError):
            self._start()


class State(Base):
    def test_get_marks_dead_container_stopped(self):
        self._start()
        with unittest.mock.patch.object(browser_session, '_running', return_value=False):
            info = browser_session.get('agent-a')
        self.assertEqual(info['status'], 'stopped')

    def test_takeover_then_handback(self):
        self._start()
        with unittest.mock.patch.object(browser_session, '_running', return_value=True):
            session = browser_session.takeover('agent-a')
            self.assertTrue(session['takeover'])
            self.assertTrue(browser_session.is_waiting_for_operator('agent-a'))
            session = browser_session.handback('agent-a')
            self.assertFalse(session['takeover'])
            self.assertFalse(browser_session.is_waiting_for_operator('agent-a'))

    def test_takeover_without_session_raises(self):
        with self.assertRaises(browser_session.BrowserSessionError):
            browser_session.takeover('nobody')

    def test_token_lookup(self):
        started = self._start()
        match = browser_session.session_by_token(started['token'])
        self.assertIsNotNone(match)
        self.assertEqual(match['port'], 43210)
        self.assertIn('password', match)
        self.assertIsNone(browser_session.session_by_token('nope'))

    def test_stop_removes_and_clears(self):
        self._start()
        self.assertTrue(browser_session.stop('agent-a'))
        self.assertIsNone(browser_session.get('agent-a'))
        self.assertTrue(any(c[:2] == ['rm', '-f'] for c in self.docker.calls))

    def test_stop_unknown_is_false(self):
        self.assertFalse(browser_session.stop('nobody'))

    def test_idle_session_is_reaped(self):
        self._start()
        with unittest.mock.patch.object(browser_session, 'SESSION_IDLE_STOP_S', 1), \
                unittest.mock.patch.object(browser_session.time, 'time', return_value=1e12):
            browser_session.get('agent-a', touch=False)
            self.assertIsNone(browser_session.get('agent-a', touch=False))

    def test_takeover_blocks_idle_reap(self):
        self._start()
        with unittest.mock.patch.object(browser_session, 'SESSION_IDLE_STOP_S', 1):
            with browser_session._LOCK:
                browser_session._SESSIONS['agent-a']['takeover'] = True
                browser_session._SESSIONS['agent-a']['status'] = 'takeover'
                browser_session._SESSIONS['agent-a']['lastUsed'] = 0
                browser_session._reap_if_idle('agent-a', browser_session._SESSIONS['agent-a'])
            self.assertIn('agent-a', browser_session._SESSIONS)


class Exec(Base):
    def test_exec_pipes_action_on_stdin(self):
        self._start()
        result = browser_session.exec_driver('agent-a', {'op': 'read'})
        self.assertEqual(result.returncode, 0)
        exec_call = next(c for c in self.docker.calls if c[:2] == ['exec', '-i'])
        self.assertIn('-i', exec_call)
        self.assertIn('BROWSER_CDP_URL=' + browser_session.SESSION_CDP_URL, exec_call)
        self.assertIn('/opt/browser_driver.py', exec_call)
        self.assertEqual(self.docker.stdin[-1], '{"op": "read"}')

    def test_exec_failure_wraps(self):
        with unittest.mock.patch.object(
                browser_session, '_docker',
                side_effect=subprocess.SubprocessError('nope')):
            with self.assertRaises(browser_session.BrowserSessionError):
                browser_session.exec_driver('agent-a', {'op': 'read'})


if __name__ == '__main__':
    unittest.main()
