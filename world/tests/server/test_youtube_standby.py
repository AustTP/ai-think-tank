# Hermetic tests for the YouTube standby module (operator, 2026-11-23).
# No colab CLI calls, no network: _colab_cli and _colab_session_exists are
# patched at the serve level, exactly the house pattern for the Laya
# standby tests.
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import serve  # noqa: E402


class YoutubeStandby(unittest.TestCase):
    def test_disabled_without_opt_in(self):
        env = {'YOUTUBE_STANDBY_ENABLED': '', 'COLAB_ENABLED': '1',
               'COLAB_CLI_AVAILABLE': True}
        with mock.patch.multiple(serve, _load_env=lambda: env,
                                 COLAB_ENABLED=True, COLAB_CLI_AVAILABLE=True):
            self.assertFalse(serve._youtube_standby_enabled())

    def test_enabled_with_opt_in(self):
        env = {'YOUTUBE_STANDBY_ENABLED': '1', 'COLAB_ENABLED': '1'}
        with mock.patch.multiple(serve, _load_env=lambda: env,
                                 COLAB_ENABLED=True, COLAB_CLI_AVAILABLE=True):
            self.assertTrue(serve._youtube_standby_enabled())

    def test_ready_uses_session_existence(self):
        calls = []

        def fake_exists(session, account='primary'):
            calls.append(session)
            return True

        with mock.patch.object(serve, '_colab_session_exists', fake_exists):
            self.assertTrue(serve._youtube_standby_ready())
        self.assertEqual(calls, [serve.YOUTUBE_SESSION])

    def test_warm_provisions_then_installs(self):
        # ensure_session ok -> one exec that reports ffmpeg present.
        with mock.patch.object(serve, '_colab_standby_ensure_session',
                               return_value=(True, 'ok')) as ens, \
             mock.patch.object(serve, '_colab_cli',
                               return_value=(0, '"ffmpeg": true')) as cli:
            ok, tail = serve._youtube_standby_warm()
        self.assertTrue(ok)
        ens.assert_called_once()
        self.assertEqual(cli.call_args.args[0], 'exec')
        self.assertIn('-s', cli.call_args.args)

    def test_warm_failure_copies_tail(self):
        with mock.patch.object(serve, '_colab_standby_ensure_session',
                               return_value=(False, 'provision failed: quota')):
            ok, tail = serve._youtube_standby_warm()
        self.assertFalse(ok)
        self.assertIn('provision failed', tail)

    def test_status_shape(self):
        with mock.patch.object(serve, '_youtube_standby_ready',
                               return_value=True):
            st = serve._youtube_standby_status()
        self.assertEqual(st['session'], serve.YOUTUBE_SESSION)
        self.assertIn('alive', st)
        self.assertIn('dailyCap', st)


if __name__ == '__main__':
    unittest.main()
