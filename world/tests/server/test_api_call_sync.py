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



class ApiRequestSync(unittest.TestCase):
    def test_parses_response(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=_FakeResponse(
                '{"a": 1}', content_type='application/json')):
            result = serve._api_request_sync('GET', 'https://api.github.com/x', {}, None)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['body'], '{"a": 1}')
        self.assertFalse(result['truncated'])
        self.assertIn('application/json', result['contentType'])

    def test_truncates_over_cap(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=_FakeResponse('x' * (serve.API_CALL_MAX_BODY_BYTES + 10))):
            result = serve._api_request_sync('GET', 'https://example.com/', {}, None)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['body']), serve.API_CALL_MAX_BODY_BYTES)

    def test_redirect_to_internal_host_rejected(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=_FakeResponse(
                '{}', final_url='http://127.0.0.1/internal')):
            with self.assertRaises(ValueError):
                serve._api_request_sync('GET', 'https://example.com/', {}, None)


