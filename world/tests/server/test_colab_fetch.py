# Hermetic tests for the Colab fetch lane (391) and its wiring into the
# /api/browse fallback chain (320). The lane is a rare-day IP escape: only
# 403/429 from the plain path trigger it, failure is swallowed back into
# the original error, and every Colab gate still applies. Everything that
# shells out to the colab CLI is mocked.
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class ColabFetchGates(unittest.TestCase):
    def test_disabled_by_flag(self):
        with unittest.mock.patch.object(serve, '_COLAB_FETCH_ENABLED', False):
            ok, payload = serve._colab_fetch_sync('https://example.com')
        self.assertFalse(ok)
        self.assertIn('disabled', payload['error'])

    def test_cli_missing(self):
        with unittest.mock.patch.object(serve, '_COLAB_FETCH_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False):
            ok, payload = serve._colab_fetch_sync('https://example.com')
        self.assertFalse(ok)
        self.assertIn('colab CLI', payload['error'])

    def test_budget_cap_blocks(self):
        with unittest.mock.patch.object(serve, '_COLAB_FETCH_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded',
                                        return_value=True):
            ok, payload = serve._colab_fetch_sync('https://example.com')
        self.assertFalse(ok)
        self.assertIn('cap', payload['error'])

    def test_rate_limit_between_attempts(self):
        with unittest.mock.patch.object(serve, '_COLAB_FETCH_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_COLAB_FETCH_LAST_AT',
                                        serve.time.time()):
            ok, payload = serve._colab_fetch_sync('https://example.com')
        self.assertFalse(ok)
        self.assertIn('rate-limited', payload['error'])


class ColabFetchRun(unittest.TestCase):
    def _run(self, cli_handler):
        def noop_provision(session=None, kind='cpu', account='primary'):
            return True, 'ok'

        with unittest.mock.patch.object(serve, '_COLAB_FETCH_ENABLED', True), \
                unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
                unittest.mock.patch.object(serve, '_colab_budget_exceeded',
                                           return_value=False), \
                unittest.mock.patch.object(serve, '_COLAB_FETCH_LAST_AT', 0.0), \
                unittest.mock.patch.object(serve, '_colab_compute_provision',
                                           side_effect=noop_provision), \
                unittest.mock.patch.object(serve, '_colab_cli',
                                           side_effect=cli_handler), \
                unittest.mock.patch.object(serve, '_accrue_colab_units'), \
                unittest.mock.patch.object(serve, 'log_action') as log:
            ok, payload = serve._colab_fetch_sync('https://example.com/page',
                                                  'testing the lane')
        return ok, payload, log

    def test_happy_path_parses_header_and_body(self):
        def handler(*args, timeout=120, input=None, account=None):
            if args[0] == 'exec':
                self.assertIn('COLAB_FETCH_URL=https://example.com/page',
                              ' '.join(args))
                self.assertIn('import os', input)
                out = ('200\ntext/html; charset=utf-8\n'
                       'https://example.com/page-final\nhello page body\n'
                       '__COLAB_DONE__')
                return 0, out
            return 0, 'ok'
        ok, payload, log = self._run(handler)
        self.assertTrue(ok)
        self.assertEqual(payload['body'], 'hello page body')
        self.assertEqual(payload['finalUrl'], 'https://example.com/page-final')
        self.assertIn('html', payload['contentType'])
        self.assertEqual(log.call_args.args[1], 'colab_fetch_run')

    def test_remote_refusal_reports_error(self):
        def handler(*args, timeout=120, input=None, account=None):
            if args[0] == 'exec':
                return 0, 'FETCHERR HTTP Error 403: Forbidden\n__COLAB_DONE__'
            return 0, 'ok'
        ok, payload, log = self._run(handler)
        self.assertFalse(ok)
        self.assertIn('remote fetch refused', payload['error'])

    def test_exec_failure_fails_closed(self):
        def handler(*args, timeout=120, input=None, account=None):
            if args[0] == 'exec':
                return 1, 'boom'
            return 0, 'ok'
        ok, payload, log = self._run(handler)
        self.assertFalse(ok)
        self.assertIn('Colab fetch failed', payload['error'])

    def test_unparseable_header_fails_closed(self):
        def handler(*args, timeout=120, input=None, account=None):
            if args[0] == 'exec':
                return 0, 'junk\n__COLAB_DONE__'
            return 0, 'ok'
        ok, payload, log = self._run(handler)
        self.assertFalse(ok)
        self.assertIn('unparseable', payload['error'])

    def test_provision_failure_fails_closed(self):
        def bad_provision(session=None, kind='cpu', account='primary'):
            return False, 'no quota'

        with unittest.mock.patch.object(serve, '_COLAB_FETCH_ENABLED', True), \
                unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
                unittest.mock.patch.object(serve, '_colab_budget_exceeded',
                                           return_value=False), \
                unittest.mock.patch.object(serve, '_COLAB_FETCH_LAST_AT', 0.0), \
                unittest.mock.patch.object(serve, '_colab_compute_provision',
                                           side_effect=bad_provision), \
                unittest.mock.patch.object(serve, '_accrue_colab_units'), \
                unittest.mock.patch.object(serve, 'log_action'):
            ok, payload = serve._colab_fetch_sync('https://example.com')
        self.assertFalse(ok)
        self.assertIn('could not provision', payload['error'])


if __name__ == '__main__':
    unittest.main()
