"""Generic api_call: the registry-gated /api/api-call path for external
services (config, not code). Covers the registry loader and host/path lookup,
credential resolution and auth injection, the raw request helper (including
the redirect re-check), and the endpoint's gate order -- read-only for
unregistered hosts, path/method enforcement and credential injection for
registered hosts, Jev gating, and page/spend accrual.

Hermetic: the endpoint's only IO is log_action (DB) and the model gate, so
this suite patches those along with the network fetch. No real outbound call
or DB write happens in these tests.
"""
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-api-call-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        API_CALL_ENABLED=True,
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class _FakeResponse:
    def __init__(self, body, status=200, content_type='application/json', final_url=None):
        self._body = body
        self.status = status
        self.headers = {'content-type': content_type}
        self._final_url = final_url or 'https://example.com/x'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._final_url

    def read(self, n=-1):
        data = self._body if isinstance(self._body, bytes) else self._body.encode()
        return data[:n] if n != -1 else data
_VALID_SPEC = {
    'id': 'my_service',
    'name': 'My Service',
    'base_url': 'https://api.myservice.example.com',
    'credential': {'type': 'env', 'key': 'MY_SERVICE_TOKEN'},
    'auth': {'type': 'header_bearer'},
    'methods': ['GET', 'POST'],
    'path_rules': [{'prefix': '/', 'methods': ['GET', 'POST']}],
    'spend': {'kind': 'none'},
}



class Workflow(unittest.TestCase):
    """The config-driven submit/poll/finalize engine, with spend accrued only
    on a successful terminal."""

    def _hf_spec(self):
        return {
            'id': 'higgsfield', 'name': 'Higgsfield', 'base_url': 'https://api.higgsfield.ai',
            'workflow': {
                'submit': {'path_template': '/{endpoint}', 'method': 'POST', 'id_field': 'request_id'},
                'estimate': {'path_template': '/estimate/{endpoint}', 'method': 'POST', 'usd_field': 'usd'},
                'poll': {'path_template': '/requests/{request_id}/status', 'method': 'GET',
                         'interval_s': 0.01, 'timeout_s': 5,
                         'terminal': ['completed', 'failed'], 'success': ['completed']},
                'spend': {'kind': 'estimate_first', 'service': 'higgsfield'},
            },
            'spend': {'kind': 'none'},
        }

    def _run(self, poll_status, usd=0.094, with_usd=True):
        responses = [
            {'body': json.dumps({'usd': usd}), 'contentType': 'application/json', 'status': 200},
            {'body': json.dumps({'request_id': 'r1'}), 'contentType': 'application/json', 'status': 200},
            {'body': json.dumps({'status': poll_status, 'request_id': 'r1',
                                 'images': [{'url': 'https://cdn/u.jpg'}]}),
             'contentType': 'application/json', 'status': 200},
        ]
        calls = []
        def fake_req(method, url, headers, body):
            calls.append(url)
            return responses[len(calls) - 1]
        spec = self._hf_spec()
        with unittest.mock.patch.object(serve, '_api_request_sync', side_effect=fake_req), \
             unittest.mock.patch.object(serve, '_accrue_spend') as acc:
            out = serve._api_execute_workflow(spec, spec['spend'], 'https://api.higgsfield.ai',
                                              'POST', {}, {'prompt': 'x'}, {'endpoint': 'higgsfield-ai/soul/v2/standard'})
        return out, calls, acc

    def test_estimate_accrued_on_success(self):
        out, calls, acc = self._run('completed')
        self.assertEqual(out['status'], 'completed')
        self.assertEqual(out['usd'], 0.094)
        self.assertEqual(out['ids'], {'endpoint': 'higgsfield-ai/soul/v2/standard', 'request_id': 'r1'})
        acc.assert_called_once_with('higgsfield', 0.094, agent_id=None)

    def test_no_charge_on_failed_terminal(self):
        out, calls, acc = self._run('failed')
        self.assertEqual(out['status'], 'failed')
        acc.assert_not_called()

    def test_workflow_submits_estimate_then_polls(self):
        out, calls, acc = self._run('completed')
        self.assertIn('https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard', calls)
        self.assertIn('https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard', calls)
        self.assertIn('https://api.higgsfield.ai/requests/r1/status', calls)


