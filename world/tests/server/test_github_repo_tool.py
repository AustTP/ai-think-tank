"""github_repo tool: the ephemeral repo-review workspace.

The player's ask (2026-11-01): agents reviewing any GitHub repo get the
files, then the files are deleted when they are finished. Tests stub the
one subprocess seam (no network, no git) and exercise the lifecycle:
open -> list/read -> close deletes the checkout; TTL expiry deletes
behind the agent's back; URL/ref validation rejects injection attempts.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._root = serve._repo_ws_root()
        self._old = serve.THINK_TANK_DIR
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='repo-ws-test-')
        serve.THINK_TANK_DIR = self._tmp
        self.addCleanup(self._cleanup)
        # A fake "clone" that materializes a tiny repo.
        self._clone_calls = []

        def fake_clone(argv, timeout):
            self._clone_calls.append(argv)
            dest = argv[-1]
            os.makedirs(os.path.join(dest, 'src'), exist_ok=True)
            with open(os.path.join(dest, 'README.md'), 'w') as handle:
                handle.write('# fixture\n')
            with open(os.path.join(dest, 'src', 'main.py'), 'w') as handle:
                handle.write('print("hi")\n')
            class _P:
                returncode = 0
                stderr = b''
            return _P()

        self._patch = mock.patch.object(serve, '_git_clone', side_effect=fake_clone)
        self._patch.start()

    def _cleanup(self):
        import shutil
        serve.THINK_TANK_DIR = self._old
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _open(self, agent='dana'):
        return serve._github_repo_tool(agent, {
            'action': 'open', 'url': 'https://github.com/acme/widget'})

    def _ws_id(self, reply):
        self.assertTrue(reply.startswith('Workspace '), reply)
        return reply.split()[1]


class OpenLifecycle(_Base):
    def test_open_lists_files_and_records_budget(self):
        reply = self._open()
        self.assertIn('2 files', reply)
        self.assertIn('README.md', reply)
        self.assertIn('src/main.py', reply)
        self.assertIn('auto-delete in 24h', reply)
        argv = self._clone_calls[0]
        self.assertIn('--depth', argv)
        self.assertEqual(argv[-2].startswith('https://github.com/acme/widget'), True)

    def test_read_returns_file_text(self):
        ws = self._ws_id(self._open())
        out = serve._github_repo_tool('dana', {'action': 'read', 'workspace': ws,
                                               'path': 'src/main.py'})
        self.assertEqual(out, 'print("hi")\n')

    def test_read_rejects_containment_escape(self):
        ws = self._ws_id(self._open())
        out = serve._github_repo_tool('dana', {'action': 'read', 'workspace': ws,
                                               'path': '../../escape.txt'})
        self.assertTrue(out.startswith('Denied'), out)

    def test_list_rejects_containment_escape(self):
        ws = self._ws_id(self._open())
        out = serve._github_repo_tool('dana', {'action': 'list', 'workspace': ws,
                                               'path': '../..'})
        self.assertTrue(out.startswith('Denied'), out)

    def test_close_deletes_everything(self):
        ws = self._ws_id(self._open())
        d, sidecar = serve._repo_ws_paths(ws)
        self.assertTrue(os.path.isdir(d))
        out = serve._github_repo_tool('dana', {'action': 'close', 'workspace': ws})
        self.assertIn('Deleted', out)
        self.assertFalse(os.path.exists(d))
        self.assertFalse(os.path.exists(sidecar))
        out2 = serve._github_repo_tool('dana', {'action': 'read', 'workspace': ws,
                                                'path': 'README.md'})
        self.assertTrue(out2.startswith('Unknown workspace'), out2)

    def test_workspaces_are_private_to_the_opener(self):
        ws = self._ws_id(self._open())
        out = serve._github_repo_tool('ruth', {'action': 'read', 'workspace': ws,
                                               'path': 'README.md'})
        self.assertTrue(out.startswith('Denied'), out)


class Validation(_Base):
    def test_only_github_https_urls(self):
        for bad in ('http://github.com/acme/widget', 'https://evil.test/a/b',
                    'file:///etc', 'https://github.com/acme',
                    'ssh://git@github.com/acme/widget'):
            out = serve._github_repo_tool('dana', {'action': 'open', 'url': bad})
            self.assertTrue(out.startswith('Deny'), f'{bad}: {out}')
        self.assertEqual(self._clone_calls, [])

    def test_ref_cannot_inject_options(self):
        out = serve._github_repo_tool('dana', {
            'action': 'open', 'url': 'https://github.com/acme/widget',
            'ref': '--upload-pack=evil'})
        self.assertTrue(out.startswith('Deny'), out)

    def test_git_failure_is_a_reason_and_cleans_up(self):
        class _P:
            returncode = 128
            stderr = b"fatal: repository 'https://github.com/acme/widget/' not found"
        with mock.patch.object(serve, '_git_clone', return_value=_P()):
            out = serve._github_repo_tool('dana', {
                'action': 'open', 'url': 'https://github.com/acme/widget'})
        self.assertTrue(out.startswith('Clone failed'), out)
        self.assertEqual(os.listdir(serve._repo_ws_root()), [])

    def test_overbudget_clone_is_deleted(self):
        def fat_clone(argv, timeout):
            dest = argv[-1]
            os.makedirs(dest, exist_ok=True)
            with open(os.path.join(dest, 'blob.bin'), 'wb') as handle:
                handle.write(b'0' * (serve._GITHUB_REPO_MAX_BYTES + 1))
            class _P:
                returncode = 0
                stderr = b''
            return _P()
        with mock.patch.object(serve, '_git_clone', side_effect=fat_clone):
            out = serve._github_repo_tool('dana', {
                'action': 'open', 'url': 'https://github.com/acme/widget'})
        self.assertTrue(out.startswith('Deny'), out)
        self.assertEqual(os.listdir(serve._repo_ws_root()), [])


class TtlSweep(_Base):
    def test_expired_workspace_is_swept(self):
        ws = self._ws_id(self._open())
        d, sidecar = serve._repo_ws_paths(ws)
        record = json.load(open(sidecar))
        record['expiresAt'] = time.time() - 1
        json.dump(record, open(sidecar, 'w'))
        self.assertEqual(serve._expire_repo_workspaces(), 1)
        self.assertFalse(os.path.exists(d))
        self.assertFalse(os.path.exists(sidecar))

    def test_live_workspace_survives_the_sweep(self):
        ws = self._ws_id(self._open())
        self.assertEqual(serve._expire_repo_workspaces(), 0)
        self.assertTrue(os.path.isdir(serve._repo_ws_paths(ws)[0]))

    def test_missing_dir_is_reported_gone(self):
        ws = self._ws_id(self._open())
        import shutil
        shutil.rmtree(serve._repo_ws_paths(ws)[0])
        out = serve._github_repo_tool('dana', {'action': 'list', 'workspace': ws})
        self.assertIn('gone', out)


if __name__ == '__main__':
    unittest.main()
