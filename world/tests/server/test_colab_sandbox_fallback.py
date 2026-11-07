"""Tests for the Docker-down -> Colab fallback (175_colab_sandbox_fallback).

Guarantees under test:
1. _docker_unavailable recognizes dead-daemon / missing-binary shapes and
   never mistakes a normal non-zero command exit for a Docker failure.
2. The fallback REFUSES bug-bounty / offensive-security commands on the
   player's Google account (the whole point of the operator's rule).
3. With Colab disabled or the CLI missing, the fallback returns an honest
   no-command-run error instead of pretending anything executed.
4. A permitted small command runs end-to-end against a mocked colab CLI:
   workspace up, bash result back, changed files applied, ranOn=colab.
"""
import os
import sys
import tarfile
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve


class TestDockerUnavailable(unittest.TestCase):
    def test_daemon_down_detected(self):
        self.assertTrue(serve._docker_unavailable(
            125, 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock'))

    def test_normal_command_failure_not_detected(self):
        self.assertFalse(serve._docker_unavailable(
            1, 'bash: line 1: ./run.sh: No such file or directory'))
        self.assertFalse(serve._docker_unavailable(2, 'pytest: command not found'))

    def test_missing_binary_marker(self):
        self.assertTrue(serve._docker_unavailable(None, 'error while creating shim: no such file or directory'))


class TestFallbackRefusesAccountRiskWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='colab-fb-')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self._patches = [
            unittest.mock.patch.object(serve, 'COLAB_ENABLED', True),
            unittest.mock.patch.object(serve, '_colab_compute_run', lambda *a, **k: {}),
            unittest.mock.patch.object(serve, '_colab_cli', lambda *a, **k: (0, '')),
            unittest.mock.patch.object(serve, '_colab_compute_provision',
                                       lambda *a, **k: (True, 'ok')),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_bug_bounty_scan_refused(self):
        out = serve._run_on_colab_sandbox_sync(
            'tester', self.tmp, 'nmap -sV -p- target.example.com', 'bug bounty recon', 60)
        self.assertIsNone(out.get('exitCode'))
        self.assertIn('off-limits', out['stderr'])
        self.assertNotIn('ranOn', out)

    def test_exploit_tooling_refused(self):
        out = serve._run_on_colab_sandbox_sync(
            'tester', self.tmp, 'msfconsole -q -x "use exploit/multi/handler"',
            'verify a cve', 60)
        self.assertIn('off-limits', out['stderr'])

    def test_torrent_refused(self):
        out = serve._run_on_colab_sandbox_sync(
            'tester', self.tmp, 'transmission-cli big.tgz', 'fetch dataset', 60)
        self.assertIn('off-limits', out['stderr'])


class TestFallbackDisabledIsHonest(unittest.TestCase):
    def test_colab_disabled_no_command_run(self):
        tmp = tempfile.mkdtemp(prefix='colab-fb-')
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', False):
            out = serve._run_on_colab_sandbox_sync(
                'tester', tmp, 'echo hi', 'probe', 60)
        self.assertIsNone(out.get('exitCode'))
        self.assertIn('No command was run anywhere', out['stderr'])


class TestFallbackEndToEnd(unittest.TestCase):
    def test_permitted_command_runs_and_files_come_home(self):
        tmp = tempfile.mkdtemp(prefix='colab-fb-')
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        with open(os.path.join(tmp, 'hello.txt'), 'w') as f:
            f.write('workspace marker\n')

        calls = {'exec_input': None}

        def fake_cli(*args, **kwargs):
            sub = args[0]
            if sub == 'upload':
                calls['rt'] = tempfile.mkdtemp(prefix='colab-fb-rt-')
                import shutil as sh
                sh.copy(args[3], os.path.join(calls['rt'], '__fallback_in.tar.gz'))
                return 0, 'ok'
            if sub == 'exec':
                calls['exec_input'] = kwargs.get('input') or ''
                import subprocess as sp
                code = calls['exec_input']
                # the runtime lives at /content on Colab; repoint to a temp dir
                rt = calls['rt']
                code = code.replace("'/content/workspace'", repr(os.path.join(rt, 'workspace')))
                code = code.replace("'/content/__fallback_in.tar.gz'",
                                    repr(os.path.join(rt, '__fallback_in.tar.gz')))
                code = code.replace("'/content/__fallback_out.tar.gz'",
                                    repr(os.path.join(rt, '__fallback_out.tar.gz')))
                for line in calls['exec_input'].splitlines():
                    if line.startswith("os.environ['tank_fb_command']"):
                        os.environ['tank_fb_command'] = eval(
                            line.split('=', 1)[1].strip())
                os.makedirs(os.path.join(rt, 'workspace'), exist_ok=True)
                proc = sp.run([sys.executable, '-c', code],
                              capture_output=True, text=True,
                              cwd=rt, timeout=60)
                calls['stdout'] = proc.stdout
                return 0, proc.stdout + '\n__COLAB_DONE__'
            if sub == 'download':
                rt = calls['rt']
                with open(args[4], 'wb') as dst, \
                        open(os.path.join(rt, '__fallback_out.tar.gz'), 'rb') as src:
                    dst.write(src.read())
                return 0, 'ok'
            if sub == 'stop':
                return 0, 'ok'
            return 0, ''

        def fake_provision(session, kind='cpu'):
            return True, 'ok'

        def fake_exec_json_line_helper():  # pragma: no cover
            return None

        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
                unittest.mock.patch.object(serve, '_colab_cli', fake_cli), \
                unittest.mock.patch.object(serve, '_colab_compute_provision', fake_provision):
            out = serve._run_on_colab_sandbox_sync(
                'tester', tmp, 'echo colab-ran && echo shipped > new_file.txt',
                'probe the fallback', 60)
        self.assertEqual(out.get('ranOn'), 'colab')
        self.assertEqual(out.get('exitCode'), 0)
        self.assertIn('colab-ran', out.get('stdout') or '')
        self.assertIn('Colab', out.get('colabNote') or '')
        self.assertTrue(os.path.exists(os.path.join(tmp, 'new_file.txt')),
                        'changed file did not come home')


if __name__ == '__main__':
    unittest.main()
