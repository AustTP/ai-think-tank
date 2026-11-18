"""Blender render lane: gate (trusted + installed), wrapper guardrails
(forced AFTER the agent's scene code), output confinement under
library/renders/<agent>/, timeout handling, and ask-tool dispatch. All
subprocess behavior is mocked -- CI has no blender binary."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def _parts_path(name):
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))),
        'server', 'serve_parts', name)


class Gate(unittest.TestCase):
    def test_untrusted_source_refuses_with_fix(self):
        import serve as bl
        with mock.patch.object(bl, 'source_is_trusted', return_value=False):
            binary, err = bl._blender_gate()
        self.assertIsNone(binary)
        self.assertIn('POST /api/trusted-codebase', err)
        self.assertIn('blender/blender', err)

    def test_uninstalled_binary_refuses_with_fix(self):
        import serve as bl
        with mock.patch.object(bl, 'source_is_trusted', return_value=True), \
             mock.patch.object(bl.shutil, 'which', return_value=None):
            binary, err = bl._blender_gate()
        self.assertIsNone(binary)
        self.assertIn('apt-get install blender', err)

    def test_trusted_and_installed_passes_binary_through(self):
        import serve as bl
        with mock.patch.object(bl, 'source_is_trusted', return_value=True), \
             mock.patch.object(bl.shutil, 'which', return_value='/usr/bin/blender'):
            binary, err = bl._blender_gate()
        self.assertEqual(binary, '/usr/bin/blender')
        self.assertIsNone(err)


class OutputName(unittest.TestCase):
    def test_strips_traversal_and_paths(self):
        import serve as bl
        # basename first: the traversal collapses to the last component
        self.assertEqual(bl._safe_render_name('../../etc/passwd'), 'passwd')
        self.assertEqual(bl._safe_render_name('my scene!!'), 'my-scene')

    def test_empty_falls_back_and_caps_length(self):
        import serve as bl
        self.assertEqual(bl._safe_render_name(''), 'render')
        self.assertEqual(len(bl._safe_render_name('x' * 500)), 64)


class Wrapper(unittest.TestCase):
    def test_guardrails_forced_after_user_code(self):
        import serve as bl
        got = bl._build_wrapper('bpy.ops.mesh.primitive_monkey_add()',
                                '/out/render.png', 64, 640, 640)
        user_at = got.index('primitive_monkey_add')
        for marker in ("engine = 'CYCLES'", "device = 'CPU'",
                       'use_denoising = False', "'/out/render.png'",
                       'write_still=True'):
            self.assertIn(marker, got)
            self.assertGreater(got.index(marker), user_at,
                               f'{marker} must be forced after user code')
        self.assertIn('BLENDER_TANK_RENDER_DONE', got)

    def test_samples_and_res_clamped(self):
        import serve as bl
        got = bl._build_wrapper('pass', '/out.png', 256, 1920, 1920)
        self.assertIn('min(int(scene.cycles.samples or 1), 256)', got)
        self.assertIn('min(int(scene.render.resolution_x or 1920), 1920)',
                      got)
        self.assertIn('min(int(scene.render.resolution_y or 1920), 1920)',
                      got)


class RenderRun(unittest.TestCase):
    def _run(self, args=None, returncode=0, stdout='BLENDER_TANK_RENDER_DONE',
             stderr=''):
        import serve as bl
        args = args or {}

        proc = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            captured['timeout'] = kwargs.get('timeout')
            with open(cmd[3], encoding='utf-8') as fh:
                captured['wrapper'] = fh.read()
            return proc

        real_exists = os.path.exists

        def fake_exists(path):
            return path.endswith('.png') or real_exists(path)

        with mock.patch.object(bl, '_blender_gate',
                               return_value=('/usr/bin/blender', None)), \
             mock.patch.object(bl.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(bl.os.path, 'exists', fake_exists), \
             mock.patch.object(bl.os.path, 'getsize', return_value=4242), \
             mock.patch.object(bl, 'LIBRARY_DIR', '/tmp/fake-library'):
            ok, out = bl._blender_render('ada', args)
        return ok, out, captured

    def test_happy_path_renders_into_agent_renders_dir(self):
        ok, out, captured = self._run({'script': 'pass', 'output': 'suzanne'})
        self.assertTrue(ok, out)
        self.assertTrue(captured['cmd'][0].endswith('blender'))
        self.assertEqual(captured['cmd'][1:3], ['-b', '--python'])
        self.assertIn(f"library{os.sep}renders{os.sep}ada{os.sep}",
                      captured['wrapper'].replace('/', os.sep))
        self.assertIn("renders/ada/", out['file'].replace(os.sep, '/'))

    def test_missing_script_refused(self):
        ok, out, _ = self._run({})
        self.assertFalse(ok)
        self.assertIn('script is required', out['error'])

    def test_script_size_capped(self):
        ok, out, _ = self._run({'script': 'x' * 9000})
        self.assertFalse(ok)
        self.assertIn('too long', out['error'])

    def test_numeric_args_clamped(self):
        ok, out, captured = self._run({'script': 'pass', 'samples': 99999,
                                       'resX': 99999, 'resY': 0,
                                       'timeoutS': 10 ** 9})
        self.assertTrue(ok, out)
        self.assertEqual(out['samples'], 256)
        self.assertEqual(out['res'][0], 1920)
        # resY=0 is falsy, so the default kicks in (documented semantics)
        self.assertEqual(out['res'][1], 640)
        self.assertLessEqual(captured['timeout'], 900.0)

    def test_malformed_numeric_refused(self):
        ok, out, _ = self._run({'script': 'pass', 'samples': 'lots'})
        self.assertFalse(ok)
        self.assertIn('malformed', out['error'])

    def test_timeout_kills_and_reports(self):
        import serve as bl
        with mock.patch.object(bl, '_blender_gate',
                               return_value=('/usr/bin/blender', None)), \
             mock.patch.object(bl.subprocess, 'run',
                               side_effect=bl.subprocess.
                               TimeoutExpired(cmd='blender', timeout=300)), \
             mock.patch.object(bl, 'LIBRARY_DIR', '/tmp/fake-library'):
            ok, out = bl._blender_render('ada', {'script': 'pass'})
        self.assertFalse(ok)
        self.assertIn('exceeded 300s', out['error'])

    def test_failure_carries_stderr_tail(self):
        ok, out, _ = self._run(args={'script': 'pass'}, returncode=1,
                               stdout='', stderr='boom\nbang')
        self.assertFalse(ok)
        self.assertEqual(out['rc'], 1)
        self.assertIn('boom', out['tail'])

    def test_log_action_called_with_outcome(self):
        import serve as bl
        real_exists = os.path.exists

        def fake_exists(path):
            return path.endswith('.png') or real_exists(path)

        with mock.patch.object(bl, '_blender_gate',
                               return_value=('/usr/bin/blender', None)), \
             mock.patch.object(bl.subprocess, 'run',
                               return_value=mock.Mock(returncode=0,
                                                      stdout='BLENDER_TANK'
                                                             '_RENDER_DONE',
                                                      stderr='')), \
             mock.patch.object(bl.os.path, 'exists', fake_exists), \
             mock.patch.object(bl.os.path, 'getsize', return_value=4242), \
             mock.patch.object(bl, 'LIBRARY_DIR', '/tmp/fake-library'), \
             mock.patch.object(bl, 'log_action') as la:
            ok, _ = bl._blender_render('ada', {'script': 'pass'})
        self.assertTrue(ok)
        self.assertEqual(la.call_args[0][1], 'blender_render')


class Wiring(unittest.TestCase):
    def test_dispatch_and_tools_list(self):
        src250 = open(_parts_path('250_ask_core.py'), encoding='utf-8').read()
        self.assertIn("name == 'blender_render'", src250)
        self.assertIn('_blender_render_tool(pick, args)', src250)
        self.assertIn('_BLENDER_TOOL', src250)

    def test_endpoint_is_operator_gated(self):
        src = open(_parts_path('475_blender_render.py'),
                   encoding='utf-8').read()
        self.assertIn("'/api/blender/render'", src)
        self.assertIn('_require_player_or_admin(request)', src)
        self.assertIn("_BLENDER_TRUSTED_REPO = 'blender/blender'", src)
        self.assertIn('use_denoising = False', src,
                      'Ubuntu build has no OIDN; default must be off')


if __name__ == '__main__':
    unittest.main()
