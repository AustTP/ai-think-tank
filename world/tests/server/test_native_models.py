# Tests for native-model tiers (398): route table, gating, sync executor
# contract -- all Colab contact mocked; no network, no real units.
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class NativeRoutes(unittest.TestCase):
    def test_route_table_shape(self):
        routes = serve._NATIVE_ROUTES
        for key in ('tts', 'whisper-s', 'blip-cap'):
            r = routes[key]
            self.assertTrue(r.get('label') and r.get('repos') and r.get('pip'))
            self.assertIn(r['kind'], ('tts', 'asr', 'caption'))
        self.assertIsNone(serve._native_route('nope'))

    def test_job_code_covers_kinds(self):
        from collections import Counter
        kinds = {r['kind'] for r in serve._NATIVE_ROUTES.values()}
        self.assertEqual(kinds, {'tts', 'asr', 'caption'})
        for kind in kinds:
            code = serve._native_job_code(kind)
            self.assertIn('RESULT:', code)
            self.assertIn('__COLAB_DONE__', code)


class NativeRunSync(unittest.TestCase):
    def setUp(self):
        self.saved = (serve._native_enabled(), serve.COLAB_CLI_AVAILABLE,
                      serve.COLAB_ENABLED)
        lock_patcher = mock.patch.object(serve.time, 'sleep', lambda s: None)
        lock_patcher.start()
        self.addCleanup(lock_patcher.stop)

    def tearDown(self):
        serve.COLAB_CLI_AVAILABLE = self.saved[1]
        serve.COLAB_ENABLED = self.saved[2]

    def _mock_colab(self, exec_out="RESULT:{\"ok\": true}\n__COLAB_DONE__\n"):
        def cli_side(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('cmd')
            if cmd == 'upload':
                return (0, 'ok')
            return (0, exec_out)
        return mock.patch.object(serve, '_colab_cli', side_effect=cli_side)

    def test_disabled_refuses(self):
        with mock.patch.object(serve, '_native_enabled', return_value=False):
            ok, res = serve._native_model_run_sync('tts', ['hello'])
        self.assertFalse(ok)
        self.assertIn('disabled', res['error'])

    def test_no_cli_refuses(self):
        with mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False):
            ok, res = serve._native_model_run_sync('tts', ['hello'])
        self.assertFalse(ok)


class NativeRunSuccessPath(unittest.TestCase):
    def setUp(self):
        self.saved_enabled = serve._native_enabled()
        sleep_patcher = mock.patch.object(serve.time, 'sleep', lambda s: None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_full_happy_path(self):
        exec_out = ("RESULT:{\"ok\": true, \"texts\": [{\"file\": \"a\", "
                    "\"text\": \"hi\"}]}\n__COLAB_DONE__\n")
        def cli_side(*args, **kwargs):
            cmd = args[0] if args else ''
            if cmd == 'upload':
                return (0, 'ok')
            return (0, exec_out)
        with mock.patch.object(serve, '_colab_cli', side_effect=cli_side), \
                mock.patch.object(serve, '_colab_pick_account',
                                  return_value='primary'), \
                mock.patch.object(serve, '_colab_compute_provision',
                                  return_value=(True, '')), \
                mock.patch.object(serve, '_accrue_colab_units'), \
                mock.patch.object(serve, '_native_enabled',
                                  return_value=True):
            ok, res = serve._native_model_run_sync('whisper-s', ['a.wav'])
        self.assertTrue(ok)
        self.assertEqual(res['texts'][0]['text'], 'hi')

    def test_unknown_route_refuses(self):
        ok, res = serve._native_model_run_sync('quantum', ['x'])
        self.assertFalse(ok)
        self.assertIn('unknown native route', res['error'])

    def test_empty_payload_refuses(self):
        ok, res = serve._native_model_run_sync('tts', [])
        self.assertFalse(ok)
        self.assertIn('no payload', res['error'])
