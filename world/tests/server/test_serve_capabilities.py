"""Cluster E line-coverage tests for serve.py.

Covers the library download/ingest/promote/reject/search/passport cluster,
write/read library files, agent file listing/reading, browse + _do_fetch,
allowlist_request, sandbox download/save/curl, curl request sync,
credential/handle management (add_credential, delete_credential,
list_credentials, add_handle, resolve_capability_handle), access_request,
_classify_command, and the room-gate helpers.

Same isolation contract as tests/test_serve.py: every real DB / library /
passport path is redirected into a throwaway temp dir. On top of the standard
boilerplate, a second module patcher also redirects the module-level DERIVED
paths (LIBRARY_ARCHIVE_DIR, LIBRARY_USAGE_PATH, SANDBOXES_DIR,
ESCALATIONS_PATH, BROWSE_TRAIL_PATH) that were computed at import time from the
REAL THINK_TANK_DIR -- without that, list_library/record_library_read/
create_escalation/_sandbox_dir_for would touch the real ~/ai-village-template
tree. Network/process seams (urllib, requests, subprocess, Jev quorum/gate)
are patched per test; library file I/O uses real temp-dir files where that is
simpler and more robust.

Run (isolated coverage file):
  cd /Users/poole86/ai-village-template/world
  COVERAGE_FILE=/tmp/cov_gap_E.coverage python3 -m coverage run --source=serve tests/test_serve_gap_E.py
"""
import asyncio
import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
import urllib.request
import zipfile

import openpyxl

from fastapi.testclient import TestClient
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None
_EXTRA_PATCHER = None
_RATE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER, _EXTRA_PATCHER, _RATE_PATCHER
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
    # The derived paths below were computed at import time from the REAL
    # THINK_TANK_DIR (see the module docstring) -- redirect them too.
    _EXTRA_PATCHER = unittest.mock.patch.multiple(
        serve,
        LIBRARY_ARCHIVE_DIR=os.path.join(_MODULE_TMP_DIR, 'library', 'archive'),
        LIBRARY_USAGE_PATH=os.path.join(_MODULE_TMP_DIR, 'library_usage.json'),
        SANDBOXES_DIR=os.path.join(_MODULE_TMP_DIR, 'sandboxes'),
        ESCALATIONS_PATH=os.path.join(_MODULE_TMP_DIR, 'escalations.json'),
        BROWSE_TRAIL_PATH=os.path.join(_MODULE_TMP_DIR, 'browse_trail.json'),
    )
    _EXTRA_PATCHER.start()
    # Many endpoint tests share the 'ada' agent id, and check_rate_limit is a
    # real 20-calls/60s window that accrues across the WHOLE module run. Patch
    # it open here (check_rate_limit is not itself a cluster-E target); the
    # dedicated 429 tests override it with return_value=False in their own
    # `with` blocks.
    _RATE_PATCHER = unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True)
    _RATE_PATCHER.start()
    # LIBRARY_DIR must exist before any passport chain append -- CredentialVaultEndpoints
    # / CapabilityHandles run early (alphabetical class order) and _append_passport_decision
    # silently swallows the missing-dir write, leaving blocks empty (IndexError).
    os.makedirs(serve.LIBRARY_DIR, exist_ok=True)
    serve.init_db()


def tearDownModule():
    _RATE_PATCHER.stop()
    _EXTRA_PATCHER.stop()
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _uid(prefix):
    return f'{prefix}-{time.time_ns()}'


def _jev(decision='allow', confidence=0.95, trace='trace-gap'):
    return unittest.mock.AsyncMock(return_value=(decision, confidence, 0.001, trace))


class _FakeResp:
    """Minimal urllib response stand-in: context manager with geturl() /
    read(n) / headers / status, enough for _download_file_sync and
    _curl_request_sync."""

    def __init__(self, url, headers, status, data):
        self._url = url
        self.headers = headers
        self.status = status
        self._data = data

    def geturl(self):
        return self._url

    def read(self, n=-1):
        if n is None or n < 0:
            return self._data
        return self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _write_lib(rel_path, content, mtime=None):
    full = os.path.join(serve.LIBRARY_DIR, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w') as f:
        f.write(content)
    if mtime is not None:
        os.utime(full, (mtime, mtime))
    return full


def _client():
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return c


class CurlEndpoint(unittest.TestCase):
    def _post(self, body, **patches):
        return _client().post('/api/curl', json=body)

    def _ok_patches(self, agent):
        return [
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True),
            unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True),
            unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()),
            unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True),
        ]

    def test_disabled_browsing_returns_403(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'BROWSING_ENABLED', False):
            r = _client().post('/api/curl', json={'url': 'http://x', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 403)

    def test_missing_key_returns_500(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None):
            r = _client().post('/api/curl', json={'url': 'http://x', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 500)

    def test_rate_limited_returns_429(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/curl', json={'url': 'http://x', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(log.call_args[0][1], 'rate_limited')

    def test_not_in_weatherstation_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=False), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/curl', json={'url': 'http://x', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'not in the Weather Station and no active temporary grant')

    def test_missing_url_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False):
            r = _client().post('/api/curl', json={'agentId': 'ada'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('url is required', r.json()['error'])

    def test_invalid_method_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False):
            r = _client().post('/api/curl', json={'url': 'http://x', 'agentId': 'ada', 'method': 'TRACE'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('method must be one of', r.json()['error'])

    def test_invalid_scheme_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/curl', json={'url': 'ftp://x', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'invalid or non-http(s) scheme')

    def test_private_host_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/curl', json={'url': 'http://localhost/x', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'private/internal host')

    def test_jev_denial_blocks(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=False):
            r = _client().post('/api/curl', json={'url': 'http://example.com/x', 'agentId': 'ada', 'purpose': 'api'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])

    def test_invalid_capability_handle_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, 'resolve_capability_handle', return_value=None), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/curl', json={'url': 'http://example.com/x', 'agentId': 'ada', 'capabilityHandle': 'bad'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'capability handle invalid, expired, or out of scope')

    def test_fetch_failure_is_reported(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_curl_request_sync', side_effect=ConnectionError('refused')), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/curl', json={'url': 'http://example.com/x', 'agentId': 'ada', 'purpose': 'api'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        self.assertIn('refused', r.json()['error'])
        self.assertEqual(log.call_args[0][2]['decision'], 'allowed_but_failed')

    def test_success_without_handle(self):
        result = {'status': 200, 'finalUrl': 'https://example.com/x', 'headers': {'Content-Type': 'text/plain'}, 'body': 'hello', 'truncated': False}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_curl_request_sync', return_value=result):
            r = _client().post('/api/curl', json={'url': 'http://example.com/x', 'agentId': 'ada', 'purpose': 'api'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['allowed'])
        self.assertEqual(r.json()['body'], 'hello')

    def test_success_with_handle_redacts_secret(self):
        result = {'status': 200, 'finalUrl': 'https://api.example.com/x',
                  'headers': {'Content-Type': 'application/json', 'X-Echo': 'TOP-SECRET'},
                  'body': '{"token": "TOP-SECRET"}', 'truncated': False}
        grant = {'credential_name': 'api', 'purpose': 'scoped use', 'service': 'svc', 'secret': 'TOP-SECRET'}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_weatherstation', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, 'resolve_capability_handle', return_value=grant), \
             unittest.mock.patch.object(serve, '_curl_request_sync', return_value=result), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/curl', json={'url': 'http://api.example.com/x', 'agentId': 'ada', 'capabilityHandle': 'h1'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn('TOP-SECRET', r.text, 'the injected credential must never reach the caller')
        self.assertIn('[REDACTED]', r.json()['body'])
        self.assertEqual(log.call_args[0][2]['decision'], 'allowed')



class SandboxDownloadEndpoint(unittest.TestCase):
    def _post(self, body, **patches):
        return _client().post('/api/sandbox-download', json=body)

    def _ok_patches(self):
        return [
            unittest.mock.patch.object(serve, 'verify_session', return_value=True),
            unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'),
            unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True),
            unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True),
            unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False),
            unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True),
            unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()),
            unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True),
        ]

    def test_disabled_returns_403(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'BROWSING_ENABLED', False):
            r = _client().post('/api/sandbox-download', json={'url': 'http://x'})
        self.assertEqual(r.status_code, 403)

    def test_missing_key_returns_500(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None):
            r = _client().post('/api/sandbox-download', json={'url': 'http://x'})
        self.assertEqual(r.status_code, 500)

    def test_rate_limited_returns_429(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-download', json={'url': 'http://x', 'agentId': 'ada', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(log.call_args[0][1], 'rate_limited')

    def test_not_in_sandbox_room_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=False), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-download', json={'url': 'http://x', 'agentId': 'ada', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['decision'], 'blocked')

    def test_missing_fields_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False):
            r = _client().post('/api/sandbox-download', json={'url': 'http://x', 'agentId': 'ada', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('url, filename, and sandboxId are required', r.json()['error'])

    def test_bad_filename_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False):
            r = _client().post('/api/sandbox-download', json={'url': 'http://x', 'agentId': 'ada', 'filename': '///', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 400)

    def test_invalid_scheme_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-download', json={'url': 'ftp://x', 'agentId': 'ada', 'filename': 'x.bin', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'invalid or non-http(s) scheme')

    def test_private_host_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-download', json={'url': 'http://localhost/x', 'agentId': 'ada', 'filename': 'x.bin', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'private/internal host')

    def test_jev_denial_blocks(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=False):
            r = _client().post('/api/sandbox-download', json={'url': 'http://example.com/x', 'agentId': 'ada', 'filename': 'x.bin', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])

    def test_fetch_failure_is_reported(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_download_file_sync', side_effect=ValueError('dl failed')):
            r = _client().post('/api/sandbox-download', json={'url': 'http://example.com/x', 'agentId': 'ada', 'filename': 'x.bin', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        self.assertFalse(r.json()['ok'])
        self.assertIn('dl failed', r.json()['reason'])

    def test_success_writes_into_sandbox(self):
        sandbox_id = _uid('sbox')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev(trace='trace-sd')), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_download_file_sync',
                                        return_value=('https://example.com/data.csv', 'text/csv', b'csvdata', False)), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-download', json={'url': 'http://example.com/data.csv', 'agentId': 'ada', 'filename': 'data.csv', 'sandboxId': sandbox_id})
        self.assertEqual(r.status_code, 200, r.text)
        resp = r.json()
        self.assertTrue(resp['allowed'])
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['path'], 'downloads/data.csv')
        sandbox_dir = serve._sandbox_dir_for(sandbox_id)
        with open(os.path.join(sandbox_dir, 'downloads', 'data.csv'), 'rb') as f:
            self.assertEqual(f.read(), b'csvdata')
        self.assertEqual(log.call_args[0][2]['decision'], 'allowed')



class SandboxSavePageEndpoint(unittest.TestCase):
    def test_disabled_returns_403(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'BROWSING_ENABLED', False):
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://x'})
        self.assertEqual(r.status_code, 403)

    def test_missing_key_returns_500(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None):
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://x'})
        self.assertEqual(r.status_code, 500)

    def test_rate_limited_returns_429(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://x', 'agentId': 'ada', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(log.call_args[0][1], 'rate_limited')

    def test_not_in_room_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=False), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://x', 'agentId': 'ada', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['decision'], 'blocked')

    def test_missing_fields_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False):
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://x', 'agentId': 'ada', 'sandboxId': 's1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('url, filename, sandboxId, and content are required', r.json()['error'])

    def test_bad_filename_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False):
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://x', 'agentId': 'ada', 'filename': '///', 'sandboxId': 's1', 'content': 'c'})
        self.assertEqual(r.status_code, 400)

    def test_invalid_scheme_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-save-page', json={'url': 'ftp://x', 'agentId': 'ada', 'filename': 'p.html', 'sandboxId': 's1', 'content': 'c'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'invalid or non-http(s) scheme')

    def test_private_host_is_blocked(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://localhost/x', 'agentId': 'ada', 'filename': 'p.html', 'sandboxId': 's1', 'content': 'c'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'private/internal host')

    def test_jev_denial_blocks(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=False):
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://example.com/x', 'agentId': 'ada', 'filename': 'p.html', 'sandboxId': 's1', 'content': 'c'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])

    def test_success_writes_and_truncates(self):
        sandbox_id = _uid('sbox')
        content = 'x' * (serve.SANDBOX_SAVE_MAX_BYTES + 50)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev(trace='trace-ssp')), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://example.com/page', 'agentId': 'ada', 'filename': 'page.html', 'sandboxId': sandbox_id, 'content': content})
        self.assertEqual(r.status_code, 200, r.text)
        resp = r.json()
        self.assertTrue(resp['allowed'])
        self.assertTrue(resp['ok'])
        self.assertTrue(resp['truncated'])
        sandbox_dir = serve._sandbox_dir_for(sandbox_id)
        with open(os.path.join(sandbox_dir, 'downloads', 'page.html'), 'rb') as f:
            self.assertEqual(len(f.read()), serve.SANDBOX_SAVE_MAX_BYTES)
        self.assertEqual(log.call_args[0][2]['decision'], 'allowed')

    def test_success_short_content_not_truncated(self):
        sandbox_id = _uid('sbox')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_agent_is_in_sandbox_room', return_value=True), \
             unittest.mock.patch.object(serve, '_has_active_temp_access', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True):
            r = _client().post('/api/sandbox-save-page', json={'url': 'http://example.com/page', 'agentId': 'ada', 'filename': 'page.html', 'sandboxId': sandbox_id, 'content': 'short page text'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()['truncated'])



class AccessRequestEndpoint(unittest.TestCase):
    def _post(self, body, **patches):
        return _client().post('/api/access/request', json=body)

    def test_rate_limited_returns_429(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/access/request', json={'agentId': 'ada', 'capability': 'curl', 'reason': 'x'})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(log.call_args[0][1], 'rate_limited')

    def test_invalid_capability_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/access/request', json={'agentId': 'ada', 'capability': 'nope', 'reason': 'x'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('capability must be one of', r.json()['error'])

    def test_missing_reason_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/access/request', json={'agentId': 'ada', 'capability': 'curl'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('reason is required', r.json()['error'])

    def test_task_id_mismatch_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value={'agents': {'ada': {'task': 'other-task'}}}):
            r = _client().post('/api/access/request', json={'agentId': 'ada', 'capability': 'curl', 'reason': 'x', 'taskId': 't1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('currently working on', r.json()['error'])

    def test_missing_key_returns_500(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None):
            r = _client().post('/api/access/request', json={'agentId': 'ada', 'capability': 'curl', 'reason': 'x'})
        self.assertEqual(r.status_code, 500)

    def test_jev_denial_denies(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/access/request', json={'agentId': 'ada', 'capability': 'curl', 'reason': 'legit need', 'supervisorId': 'faye'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['approved'])
        self.assertEqual(log.call_args[0][1], 'access_request')

    def test_success_grants_temporary_access(self):
        agent = _uid('acc')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev(trace='trace-ar')), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/access/request', json={'agentId': agent, 'capability': 'curl', 'reason': 'need to check a weather API', 'supervisorId': 'faye'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['approved'])
        self.assertFalse(body['untilStoryComplete'])
        self.assertGreater(body['expiresAt'], time.time())
        self.assertTrue(serve._has_active_temp_access(agent, 'curl'))
        self.assertEqual(log.call_args[0][2]['decision'], 'approved')

    def test_success_story_scoped_grant(self):
        agent = _uid('acc')
        state = {'agents': {agent: {'task': 't1'}}}
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True):
            r = _client().post('/api/access/request', json={'agentId': agent, 'capability': 'curl', 'reason': 'for the story', 'supervisorId': 'faye', 'taskId': 't1'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['approved'])
        self.assertTrue(body['untilStoryComplete'])
        self.assertIsNone(body['expiresAt'])
        self.assertTrue(serve._has_active_temp_access(agent, 'curl'))



class ClassifyCommand(unittest.TestCase):
    def test_missing_key_fails_closed(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None):
            allowed, reason = asyncio.run(serve._classify_command('echo hi', 'test', 'ada'))
        self.assertFalse(allowed)
        self.assertEqual(reason, 'OPENROUTER_API_KEY not set')

    def test_confident_allow_passes(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True):
            allowed, reason = asyncio.run(serve._classify_command('echo hi', 'runs a test', 'ada'))
        self.assertTrue(allowed)
        self.assertEqual(reason, 'allow')

    def test_block_blocks(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev(decision='block', confidence=0.99)), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=False):
            allowed, reason = asyncio.run(serve._classify_command('rm -rf /', 'cleanup', 'ada'))
        self.assertFalse(allowed)
        self.assertEqual(reason, 'block')

    def test_host_home_path_blocks_before_the_model(self):
        # Host paths are refused deterministically so the classifier cannot
        # allow a read that the sandbox can never satisfy, or block a
        # container-local /tmp path it wrongly reads as the host's (task-389).
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision',
                                        side_effect=AssertionError('model should not run')):
            allowed, reason = asyncio.run(serve._classify_command(
                'cat ' + os.path.expanduser('~') + '/secret.txt', 'read a file', 'ada'))
        self.assertFalse(allowed)
        self.assertIn('host filesystem', reason)
        # The refusal names the sanctioned alternative so a high-ranking
        # agent (the chief of staff reaching for the production database for
        # a supervision report) is pointed at the real tool, not just a wall.
        self.assertIn('state_records tool with op=stats', reason)



class CredentialVaultEndpoints(unittest.TestCase):
    def setUp(self):
        if os.path.exists(serve.PASSPORT_PATH):
            os.remove(serve.PASSPORT_PATH)

    def test_list_credentials_blocks_agents(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True):
            r = _client().get('/api/keys/credentials', headers={'X-Agent-Key': 'k'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('player-only', r.json()['error'])

    def test_list_credentials_returns_entries(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_list_credentials',
                                        return_value=[{'name': 'api', 'service': 'svc'}]):
            r = _client().get('/api/keys/credentials')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['credentials'], [{'name': 'api', 'service': 'svc'}])

    def test_add_credential_blocks_agents(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True):
            r = _client().post('/api/keys/credentials', json={'name': 'x', 'value': 'v'}, headers={'X-Agent-Key': 'k'})
        self.assertEqual(r.status_code, 403)

    def test_add_credential_missing_fields_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/keys/credentials', json={'name': 'x'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('required', r.json()['error'])

    def test_add_credential_bad_name_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/keys/credentials', json={'name': 'bad name!', 'value': 'v'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('must be [a-zA-Z0-9_-]', r.json()['error'])

    def test_add_credential_success_stores_and_chains(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_store_credential') as store, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/keys/credentials', json={'name': 'api', 'service': 'my-svc', 'value': 'secret-value'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['credential'], {'name': 'api', 'service': 'my-svc', 'kind': 'token'})
        store.assert_called_once_with('api', 'my-svc', {'kind': 'token', 'token': 'secret-value'},
                                      status='active', proposed_by='player')
        self.assertEqual(log.call_args[0][1], 'credential_stored')
        data = serve._load_passport()
        self.assertEqual(data['blocks'][0]['kind'], 'credential_stored')

    def test_delete_credential_blocks_agents(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True):
            r = _client().delete('/api/keys/credentials/api', headers={'X-Agent-Key': 'k'})
        self.assertEqual(r.status_code, 403)

    def test_delete_credential_success(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_delete_credential') as delete, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().delete('/api/keys/credentials/api')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])
        delete.assert_called_once_with('api')
        self.assertEqual(log.call_args[0][1], 'credential_deleted')
        data = serve._load_passport()
        self.assertEqual(data['blocks'][0]['kind'], 'credential_deleted')



class CapabilityHandles(unittest.TestCase):
    def setUp(self):
        if os.path.exists(serve.PASSPORT_PATH):
            os.remove(serve.PASSPORT_PATH)

    def _post_handle(self, body, **patches):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            return _client().post('/api/keys/handles', json=body)

    def test_requires_session(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            r = _client().post('/api/keys/handles', json={})
        self.assertEqual(r.status_code, 401)

    def test_missing_fields_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/keys/handles', json={'agentId': 'ada'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('agentId, credentialName, and purpose are required', r.json()['error'])

    def test_string_allowed_methods_is_parsed(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'mint_capability_handle',
                                        return_value=('handle-1', None)) as mint:
            r = _client().post('/api/keys/handles', json={'agentId': 'ada', 'credentialName': 'api', 'purpose': 'p', 'allowedMethods': 'GET, POST'})
        self.assertEqual(r.status_code, 200, r.text)
        args = mint.call_args[0]
        self.assertEqual(args[4], ['GET', 'POST'])

    def test_string_allowed_hosts_is_parsed(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'mint_capability_handle',
                                        return_value=('handle-1', None)) as mint:
            r = _client().post('/api/keys/handles', json={'agentId': 'ada', 'credentialName': 'api', 'purpose': 'p', 'allowedHosts': 'api.a.com,api.b.com'})
        self.assertEqual(r.status_code, 200, r.text)
        args = mint.call_args[0]
        self.assertEqual(args[3], ['api.a.com', 'api.b.com'])

    def test_wildcard_host_stays_star(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'mint_capability_handle',
                                        return_value=('handle-1', None)) as mint:
            r = _client().post('/api/keys/handles', json={'agentId': 'ada', 'credentialName': 'api', 'purpose': 'p', 'allowedHosts': '*'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(mint.call_args[0][3], ['*'])

    def test_unknown_credential_refusal_is_404(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'mint_capability_handle', return_value=(None, 'unknown credential')):
            r = _client().post('/api/keys/handles', json={'agentId': 'ada', 'credentialName': 'ghost', 'purpose': 'p'})
        self.assertEqual(r.status_code, 404)
        self.assertIn('unknown credential', r.json()['error'])

    def test_other_refusal_is_403(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'mint_capability_handle', return_value=(None, 'over cap')):
            r = _client().post('/api/keys/handles', json={'agentId': 'ada', 'credentialName': 'api', 'purpose': 'p'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error'], 'over cap')

    def test_success_mints_handle(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'mint_capability_handle',
                                        return_value=('minted-nonce', None)) as mint, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/keys/handles', json={'agentId': 'ada', 'credentialName': 'api', 'purpose': 'scoped use', 'ttlSec': 600})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['handle'], 'minted-nonce')
        self.assertEqual(mint.call_args[0][1], 'api')
        self.assertEqual(log.call_args[0][1], 'handle_minted')
        data = serve._load_passport()
        self.assertEqual(data['blocks'][0]['kind'], 'handle_minted')

    def _insert_cred(self, name, service='svc'):
        with serve._db() as conn:
            conn.execute('INSERT OR REPLACE INTO external_credentials (name, service, encrypted_value, created_at) VALUES (?, ?, ?, ?)',
                         (name, service, 'encrypted-token', time.time()))

    def _insert_handle(self, handle, agent_id, cred='api', hosts='["api.example.com"]', methods='["GET"]', expires_at=None):
        with serve._db() as conn:
            conn.execute('INSERT INTO capability_handles (handle, agent_id, credential_name, purpose, allowed_hosts, allowed_methods, granted_by, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (handle, agent_id, cred, 'purpose', hosts, methods, 'player', time.time() + 3600 if expires_at is None else expires_at, time.time()))

    def test_resolve_empty_inputs_is_none(self):
        self.assertIsNone(serve.resolve_capability_handle('', 'h', 'GET', 'http://x'))

    def test_resolve_no_row_is_none(self):
        self.assertIsNone(serve.resolve_capability_handle('ada', 'no-such-handle', 'GET', 'http://api.example.com'))

    def test_resolve_digitalocean_disabled_is_none(self):
        self._insert_cred('digitalocean')
        self._insert_handle('do-handle', 'ada', cred='digitalocean')
        with unittest.mock.patch.object(serve, '_digitalocean_enabled', return_value=False):
            self.assertIsNone(serve.resolve_capability_handle('ada', 'do-handle', 'GET', 'http://api.example.com'))

    def test_resolve_bad_json_scope_is_none(self):
        self._insert_cred('api')
        self._insert_handle('bad-json-handle', 'ada', hosts='not-json')
        self.assertIsNone(serve.resolve_capability_handle('ada', 'bad-json-handle', 'GET', 'http://api.example.com'))

    def test_resolve_expired_handle_is_none_and_deleted(self):
        self._insert_cred('api')
        self._insert_handle('expired-handle', 'ada', expires_at=time.time() - 100)
        self.assertIsNone(serve.resolve_capability_handle('ada', 'expired-handle', 'GET', 'http://api.example.com'))
        with serve._db() as conn:
            row = conn.execute('SELECT 1 FROM capability_handles WHERE handle = ?', ('expired-handle',)).fetchone()
        self.assertIsNone(row, 'an expired handle is deleted on resolve')

    def test_resolve_url_without_host_is_none(self):
        self._insert_cred('api')
        self._insert_handle('no-host-handle', 'ada')
        self.assertIsNone(serve.resolve_capability_handle('ada', 'no-host-handle', 'GET', 'not-a-url'))

    def test_resolve_host_out_of_scope_is_none(self):
        self._insert_cred('api')
        self._insert_handle('host-handle', 'ada')
        self.assertIsNone(serve.resolve_capability_handle('ada', 'host-handle', 'GET', 'http://evil.example.net'))

    def test_resolve_method_out_of_scope_is_none(self):
        self._insert_cred('api')
        self._insert_handle('method-handle', 'ada', methods='["GET"]')
        self.assertIsNone(serve.resolve_capability_handle('ada', 'method-handle', 'POST', 'http://api.example.com'))

    def test_resolve_missing_credential_row_is_none(self):
        self._insert_handle('no-cred-handle', 'ada', cred='ghost-cred')
        self.assertIsNone(serve.resolve_capability_handle('ada', 'no-cred-handle', 'GET', 'http://api.example.com'))

    def test_resolve_secret_unreadable_is_none(self):
        self._insert_cred('api')
        self._insert_handle('secret-handle', 'ada')
        with unittest.mock.patch.object(serve, '_open_secret', return_value=None):
            self.assertIsNone(serve.resolve_capability_handle('ada', 'secret-handle', 'GET', 'http://api.example.com'))

    def test_resolve_success_returns_scoped_headers(self):
        self._insert_cred('api', service='github')
        self._insert_handle('ok-handle', 'ada')
        with unittest.mock.patch.object(serve, '_open_secret', return_value='decrypted-token'):
            result = serve.resolve_capability_handle('ada', 'ok-handle', 'GET', 'http://api.example.com/repos')
        self.assertEqual(result['credential_name'], 'api')
        self.assertEqual(result['service'], 'github')
        self.assertEqual(result['secret'], 'decrypted-token')
        self.assertEqual(result['purpose'], 'purpose')

    def test_resolve_wildcard_host_success(self):
        self._insert_cred('api')
        self._insert_handle('wild-handle', 'ada', hosts='"*"')
        with unittest.mock.patch.object(serve, '_open_secret', return_value='t'):
            result = serve.resolve_capability_handle('ada', 'wild-handle', 'GET', 'http://anything.example.com')
        self.assertIsNotNone(result)
