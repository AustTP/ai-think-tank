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


class DownloadFileSync(unittest.TestCase):
    def test_success_reads_and_returns_raw(self):
        fake = _FakeResp('https://example.com/data.csv', {'Content-Type': 'text/csv'}, 200, b'a,b\n1,2\n')
        with unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_safe_urlopen', return_value=fake):
            final_url, content_type, raw, truncated = serve._download_file_sync('https://example.com/data.csv', 1000)
        self.assertEqual(final_url, 'https://example.com/data.csv')
        self.assertEqual(content_type, 'text/csv')
        self.assertEqual(raw, b'a,b\n1,2\n')
        self.assertFalse(truncated)

    def test_truncates_at_max_bytes(self):
        fake = _FakeResp('https://example.com/big.bin', {'Content-Type': 'application/octet-stream'}, 200, b'x' * 50)
        with unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_safe_urlopen', return_value=fake):
            _final, _ct, raw, truncated = serve._download_file_sync('https://example.com/big.bin', 10)
        self.assertEqual(len(raw), 10)
        self.assertTrue(truncated)

    def test_redirect_to_disallowed_host_raises(self):
        fake = _FakeResp('http://10.0.0.5/steal', {'Content-Type': 'text/html'}, 200, b'x')
        with unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False), \
             unittest.mock.patch.object(serve, '_safe_urlopen', return_value=fake):
            with self.assertRaises(ValueError):
                serve._download_file_sync('https://example.com/ok', 100)



class LibraryDownloadEndpoint(unittest.TestCase):
    def _post(self, body, **patches):
        return serve.verify_agent_key, body

    def test_disabled_browsing_returns_403(self):
        with unittest.mock.patch.object(serve, 'BROWSING_ENABLED', False), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/library/download', json={'url': 'http://x', 'filename': 'x'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('disabled', r.json()['error'])

    def test_missing_api_key_returns_500(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/library/download', json={'url': 'http://x', 'filename': 'x'})
        self.assertEqual(r.status_code, 500)

    def test_missing_url_or_filename_returns_400(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True):
            r = _client().post('/api/library/download', json={'url': '', 'filename': ''})
        self.assertEqual(r.status_code, 400)
        self.assertIn('url and filename are required', r.json()['error'])

    def test_rate_limited_returns_429(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/library/download', json={'url': 'http://x', 'filename': 'x', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 429)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1], 'rate_limited')

    def test_unsafe_filename_returns_400(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True):
            r = _client().post('/api/library/download', json={'url': 'http://x', 'filename': '///', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('safe characters', r.json()['error'])

    def test_non_http_scheme_is_blocked(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/library/download', json={'url': 'ftp://example.com/a.csv', 'filename': 'a.csv', 'agentId': 'ada', 'purpose': 'data'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'invalid or non-http(s) scheme')

    def test_private_host_is_blocked(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/library/download', json={'url': 'http://internal.example/a.csv', 'filename': 'a.csv', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertEqual(log.call_args[0][2]['reason'], 'private/internal host')

    def test_jev_denial_blocks(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=False):
            r = _client().post('/api/library/download', json={'url': 'http://example.com/a.csv', 'filename': 'a.csv', 'agentId': 'ada', 'purpose': 'ref'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])

    def test_fetch_failure_reports_ok_false(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_download_file_sync', side_effect=ValueError('boom')), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/library/download', json={'url': 'http://example.com/a.csv', 'filename': 'a.csv', 'agentId': 'ada', 'purpose': 'ref'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['allowed'])
        self.assertFalse(r.json()['ok'])
        self.assertEqual(log.call_args[0][2]['reason'], 'boom')

    def test_personal_download_success_writes_file(self):
        agent = _uid('dl-agent')
        body = {'url': 'https://example.com/data.csv', 'filename': '../data.csv', 'agentId': agent, 'purpose': 'a dataset'}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev(trace='trace-dl')), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_download_file_sync',
                                        return_value=('https://example.com/data.csv', 'text/csv', b'a,b\n1,2\n', False)), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/library/download', json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['allowed'])
        self.assertTrue(r.json()['ok'])
        rel = r.json()['path']
        self.assertTrue(rel.startswith(f'pending_review/downloads/{agent}/'))
        full = os.path.join(serve.LIBRARY_DIR, rel)
        self.assertTrue(os.path.isfile(full), 'the download must land on disk')
        with open(full, 'rb') as f:
            self.assertEqual(f.read(), b'a,b\n1,2\n')
        self.assertEqual(log.call_args[0][2]['decision'], 'allowed')

    def test_shared_scope_routes_to_shared_pending(self):
        agent = _uid('dl-agent')
        body = {'url': 'https://example.com/data.csv', 'filename': 'data.csv', 'agentId': agent, 'scope': 'shared', 'purpose': 'team data'}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_download_file_sync',
                                        return_value=('https://example.com/data.csv', 'text/csv', b'x', False)):
            r = _client().post('/api/library/download', json=body)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['path'].startswith('pending_review/shared/'))

    def test_bogus_scope_defaults_to_personal(self):
        agent = _uid('dl-agent')
        body = {'url': 'https://example.com/data.csv', 'filename': 'data.csv', 'agentId': agent, 'scope': 'bogus', 'purpose': 'x'}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_download_file_sync',
                                        return_value=('https://example.com/data.csv', 'text/csv', b'x', False)):
            r = _client().post('/api/library/download', json=body)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['path'].startswith('pending_review/downloads/'))

    def test_traversal_agent_id_is_rejected(self):
        body = {'url': 'https://example.com/data.csv', 'filename': 'data.csv', 'agentId': '../../..', 'purpose': 'x'}
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision', _jev()), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', return_value=True), \
             unittest.mock.patch.object(serve, '_download_file_sync',
                                        return_value=('https://example.com/data.csv', 'text/csv', b'x', False)):
            r = _client().post('/api/library/download', json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn('invalid destination', r.json()['error'])



class PassportLedger(unittest.TestCase):
    def setUp(self):
        if os.path.exists(serve.PASSPORT_PATH):
            os.remove(serve.PASSPORT_PATH)

    def test_load_missing_file_is_fresh_chain(self):
        self.assertEqual(serve._load_passport(), {'version': 1, 'count': 0, 'head': None, 'blocks': []})

    def test_load_corrupt_file_is_fresh_chain(self):
        os.makedirs(os.path.dirname(serve.PASSPORT_PATH), exist_ok=True)
        with open(serve.PASSPORT_PATH, 'w') as f:
            f.write('not json{{{')
        self.assertEqual(serve._load_passport(), {'version': 1, 'count': 0, 'head': None, 'blocks': []})

    def test_load_backfills_missing_keys(self):
        os.makedirs(os.path.dirname(serve.PASSPORT_PATH), exist_ok=True)
        with open(serve.PASSPORT_PATH, 'w') as f:
            json.dump({'blocks': [{'index': 1}]}, f)
        data = serve._load_passport()
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['count'], 0)
        self.assertIsNone(data['head'])
        self.assertEqual(len(data['blocks']), 1)

    def test_append_skips_missing_file(self):
        self.assertIsNone(serve._append_passport('does/not/exist.md', 'ada', 'player'))

    def test_append_chains_blocks_and_writes_ledger(self):
        path = _write_lib('pending_review/downloads/ada/notes.md', 'real content')
        block = serve._append_passport('pending_review/downloads/ada/notes.md', 'ada', 'player')
        self.assertIsNotNone(block)
        self.assertEqual(block['index'], 1)
        self.assertEqual(block['path'], 'pending_review/downloads/ada/notes.md')
        self.assertEqual(block['sha256'], serve._sha256_file(path))
        self.assertEqual(block['owner'], 'ada')
        self.assertEqual(block['promotedBy'], 'player')
        self.assertIsNone(block['prev'])
        data = serve._load_passport()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['head'], block['sha256'])
        self.assertEqual(len(data['blocks']), 1)

        second = serve._append_passport('pending_review/downloads/ada/notes.md', 'ada', 'player')
        self.assertEqual(second['index'], 2)
        self.assertEqual(second['prev'], block['sha256'], 'each block links back to the previous head')

    def test_append_failure_returns_none(self):
        with unittest.mock.patch.object(serve, '_safe_library_path', side_effect=RuntimeError('boom')):
            self.assertIsNone(serve._append_passport('shared/x.md', 'ada', 'player'))

    def test_library_passport_endpoint_returns_ledger(self):
        _write_lib('shared/known.md', 'known content')
        serve._append_passport('shared/known.md', 'ada', 'player')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/library/passport')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(len(body['blocks']), 1)



class IngestPdf(unittest.TestCase):
    def test_missing_pypdf_returns_clear_error(self):
        with unittest.mock.patch.object(serve, 'pypdf', None):
            text, err = serve._ingest_pdf_to_text('/x/whatever.pdf')
        self.assertIsNone(text)
        self.assertIn('pypdf is not installed', err)

    def test_success_extracts_pages(self):
        class FakePage:
            def extract_text(self):
                return 'page body'
        class FakeReader:
            pages = [FakePage(), FakePage()]
        with unittest.mock.patch.object(serve, 'pypdf', types.SimpleNamespace(PdfReader=lambda p: FakeReader())):
            text, err = serve._ingest_pdf_to_text('/x/doc.pdf')
        self.assertIsNone(err)
        self.assertIn('--- page 1 ---', text)
        self.assertIn('page body', text)
        self.assertIn('--- page 2 ---', text)

    def test_more_than_200_pages_is_capped(self):
        class FakePage:
            def extract_text(self):
                return 'p'
        class FakeReader:
            pages = [FakePage() for _ in range(201)]
        with unittest.mock.patch.object(serve, 'pypdf', types.SimpleNamespace(PdfReader=lambda p: FakeReader())):
            text, err = serve._ingest_pdf_to_text('/x/big.pdf')
        self.assertIsNone(err)
        self.assertIn('1 more pages not extracted', text)

    def test_extraction_exception_is_reported(self):
        with unittest.mock.patch.object(serve, 'pypdf', types.SimpleNamespace(PdfReader=lambda p: (_ for _ in ()).throw(ValueError('corrupt pdf')))):
            text, err = serve._ingest_pdf_to_text('/x/bad.pdf')
        self.assertIsNone(text)
        self.assertIn('corrupt pdf', err)



class IngestXlsx(unittest.TestCase):
    def test_missing_openpyxl_returns_clear_error(self):
        with unittest.mock.patch.object(serve, 'openpyxl', None):
            text, err = serve._ingest_xlsx_to_text('/x/whatever.xlsx')
        self.assertIsNone(text)
        self.assertIn('openpyxl is not installed', err)

    def test_success_extracts_sheets_and_rows(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'book.xlsx')
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = 'Data'
            ws.append(['a', 'b'])
            ws.append([1, 2])
            wb.save(path)
            text, err = serve._ingest_xlsx_to_text(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNone(err)
        self.assertIn('--- sheet: Data ---', text)
        self.assertIn('a, b', text)
        self.assertIn('1, 2', text)

    def test_over_2000_rows_is_truncated(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'wide.xlsx')
            wb = openpyxl.Workbook()
            ws = wb.active
            for i in range(2001):
                ws.append([i])
            wb.save(path)
            text, err = serve._ingest_xlsx_to_text(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNone(err)
        self.assertIn('truncated at 2000 rows', text)

    def test_load_exception_is_reported(self):
        with unittest.mock.patch.object(serve.openpyxl, 'load_workbook', side_effect=Exception('bad workbook')):
            text, err = serve._ingest_xlsx_to_text('/x/book.xlsx')
        self.assertIsNone(text)
        self.assertIn('bad workbook', err)



class IngestDocx(unittest.TestCase):
    def test_missing_docx_returns_clear_error(self):
        with unittest.mock.patch.object(serve, '_docx', None):
            text, err = serve._ingest_docx_to_text('/x/whatever.docx')
        self.assertIsNone(text)
        self.assertIn('python-docx is not installed', err)

    def test_success_extracts_paragraphs_and_tables(self):
        import docx as _docx
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'memo.docx')
            d = _docx.Document()
            d.add_paragraph('The decision memo.')
            t = d.add_table(rows=1, cols=2)
            t.rows[0].cells[0].text = 'column one'
            t.rows[0].cells[1].text = 'column two'
            d.save(path)
            text, err = serve._ingest_docx_to_text(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNone(err)
        self.assertIn('The decision memo.', text)
        self.assertIn('column one | column two', text)

    def test_load_exception_is_reported(self):
        with unittest.mock.patch.object(serve, '_docx', types.SimpleNamespace(Document=lambda p: (_ for _ in ()).throw(Exception('bad docx')))):
            text, err = serve._ingest_docx_to_text('/x/memo.docx')
        self.assertIsNone(text)
        self.assertIn('bad docx', err)



class IngestPptx(unittest.TestCase):
    def test_missing_pptx_returns_clear_error(self):
        with unittest.mock.patch.object(serve, '_pptx', None):
            text, err = serve._ingest_pptx_to_text('/x/whatever.pptx')
        self.assertIsNone(text)
        self.assertIn('python-pptx is not installed', err)

    def test_success_extracts_slide_text(self):
        from pptx import Presentation as _Pres
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'deck.pptx')
            prs = _Pres()
            slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
            tb = slide.shapes.add_textbox(100, 100, 400, 60)
            tb.text = 'Q3 pricing update'
            prs.save(path)
            text, err = serve._ingest_pptx_to_text(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNone(err)
        self.assertIn('--- slide 1 ---', text)
        self.assertIn('Q3 pricing update', text)

    def test_load_exception_is_reported(self):
        with unittest.mock.patch.object(serve, '_pptx', types.SimpleNamespace(Presentation=lambda p: (_ for _ in ()).throw(Exception('bad pptx')))):
            text, err = serve._ingest_pptx_to_text('/x/deck.pptx')
        self.assertIsNone(text)
        self.assertIn('bad pptx', err)



class IngestWordPowerPointDispatch(unittest.TestCase):
    def test_docx_lands_as_md_in_library(self):
        import docx as _docx
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, 'memo.docx')
            d = _docx.Document()
            d.add_paragraph('prose')
            d.save(src)
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', os.path.join(tmp, 'library')), \
                 unittest.mock.patch.object(serve, '_redact_secrets', side_effect=lambda s: s):
                results = []
                serve._ingest_one_file(src, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(results[0]['ok'], True)
        self.assertTrue(results[0]['path'].endswith('memo.docx.md'))

    def test_pptx_lands_as_md_in_library(self):
        from pptx import Presentation as _Pres
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, 'deck.pptx')
            prs = _Pres()
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            tb = slide.shapes.add_textbox(100, 100, 400, 60)
            tb.text = 'slide prose'
            prs.save(src)
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', os.path.join(tmp, 'library')), \
                 unittest.mock.patch.object(serve, '_redact_secrets', side_effect=lambda s: s):
                results = []
                serve._ingest_one_file(src, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(results[0]['ok'], True)
        self.assertTrue(results[0]['path'].endswith('deck.pptx.md'))



class IngestWriteText(unittest.TestCase):
    def test_invalid_destination_reports_failure(self):
        results = []
        serve._write_ingested_text('../../escape.md', 'content', results, 'name.md')
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertEqual(results[0]['note'], 'invalid destination path')

    def test_valid_destination_writes_file(self):
        results = []
        serve._write_ingested_text('ingested/notes/name.md', 'hello world', results, 'name.md')
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['ok'])
        self.assertEqual(results[0]['path'], 'ingested/notes/name.md')
        with open(os.path.join(serve.LIBRARY_DIR, 'ingested/notes/name.md')) as f:
            self.assertEqual(f.read(), 'hello world')



class IngestOneFile(unittest.TestCase):
    def test_getsize_failure_is_reported(self):
        results = []
        serve._ingest_one_file('/nonexistent/nope.pdf', 'ingested', results)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])

    def test_oversized_file_is_skipped(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'big.txt')
            with open(path, 'w') as f:
                f.write('x' * 20)
            results = []
            with unittest.mock.patch.object(serve, 'INGEST_MAX_FILE_BYTES', 10):
                serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertIn('exceeds the', results[0]['note'])

    def test_pdf_success_writes_markdown(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'doc.pdf')
            with open(path, 'wb') as f:
                f.write(b'%PDF-1.4 fake')
            results = []
            with unittest.mock.patch.object(serve, '_ingest_pdf_to_text', return_value=('extracted pdf text', None)):
                serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['ok'])
        self.assertEqual(results[0]['path'], 'ingested/doc.pdf.md')
        with open(os.path.join(serve.LIBRARY_DIR, 'ingested/doc.pdf.md')) as f:
            self.assertIn('extracted pdf text', f.read())

    def test_pdf_extraction_error_is_reported(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'bad.pdf')
            with open(path, 'wb') as f:
                f.write(b'garbage')
            results = []
            with unittest.mock.patch.object(serve, '_ingest_pdf_to_text', return_value=(None, 'PDF extraction failed: nope')):
                serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertIn('PDF extraction failed: nope', results[0]['note'])

    def test_xlsx_success_writes_markdown(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'data.xlsx')
            with open(path, 'wb') as f:
                f.write(b'fake xlsx')
            results = []
            with unittest.mock.patch.object(serve, '_ingest_xlsx_to_text', return_value=('sheet rows', None)):
                serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['ok'])
        self.assertEqual(results[0]['path'], 'ingested/data.xlsx.md')

    def test_xlsx_extraction_error_is_reported(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'bad.xlsx')
            with open(path, 'wb') as f:
                f.write(b'garbage')
            results = []
            with unittest.mock.patch.object(serve, '_ingest_xlsx_to_text', return_value=(None, 'Excel extraction failed: nope')):
                serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertIn('Excel extraction failed: nope', results[0]['note'])

    def test_text_file_success(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'note.txt')
            with open(path, 'w') as f:
                f.write('plain text content')
            results = []
            serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['ok'])
        with open(os.path.join(serve.LIBRARY_DIR, 'ingested/note.txt')) as f:
            self.assertEqual(f.read(), 'plain text content')

    def test_text_read_error_is_reported(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'note.txt')
            with open(path, 'w') as f:
                f.write('x')
            results = []
            real_open = builtins.open

            def fake_open(p, *a, **k):
                if p == path:
                    raise PermissionError('denied')
                return real_open(p, *a, **k)
            with unittest.mock.patch.object(builtins, 'open', side_effect=fake_open):
                serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertIn('denied', results[0]['note'])

    def test_image_success_is_copied(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'photo.png')
            with open(path, 'wb') as f:
                f.write(b'\x89PNG fake image bytes')
            results = []
            serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['ok'])
        self.assertIn('vision review', results[0]['note'])
        with open(os.path.join(serve.LIBRARY_DIR, 'ingested/photo.png'), 'rb') as f:
            self.assertEqual(f.read(), b'\x89PNG fake image bytes')

    def test_image_read_error_is_reported(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'photo.png')
            with open(path, 'wb') as f:
                f.write(b'png')
            results = []
            real_open = builtins.open

            def fake_open(p, *a, **k):
                if p == path:
                    raise PermissionError('no read')
                return real_open(p, *a, **k)
            with unittest.mock.patch.object(builtins, 'open', side_effect=fake_open):
                serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertIn('no read', results[0]['note'])

    def test_image_invalid_destination_is_reported(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'photo.png')
            with open(path, 'wb') as f:
                f.write(b'png')
            results = []
            serve._ingest_one_file(path, '../../escape', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertEqual(results[0]['note'], 'invalid destination path')

    def test_unsupported_extension_is_skipped(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'archive.rar')
            with open(path, 'wb') as f:
                f.write(b'rar')
            results = []
            serve._ingest_one_file(path, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertIn('unsupported file type', results[0]['note'])



class IngestWalk(unittest.TestCase):
    def test_skips_ds_store_and_ingests_real_files(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, '.DS_Store'), 'w') as f:
                f.write('junk')
            os.makedirs(os.path.join(tmp, 'sub'))
            with open(os.path.join(tmp, 'sub', 'a.txt'), 'w') as f:
                f.write('a')
            results = []
            serve._ingest_walk(tmp, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['ok'])
        self.assertEqual(results[0]['path'], 'ingested/sub/a.txt')

    def test_cap_reached_notes_skipped_files(self):
        tmp = tempfile.mkdtemp()
        try:
            for n in ('a.txt', 'b.txt'):
                with open(os.path.join(tmp, n), 'w') as f:
                    f.write(n)
            results = []
            with unittest.mock.patch.object(serve, 'INGEST_MAX_FILES', 1):
                serve._ingest_walk(tmp, 'ingested', results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(results), 2)
        self.assertTrue(any(r['ok'] for r in results))
        self.assertTrue(any(not r['ok'] and 'cap of 1 files reached' in r['note'] for r in results))



class IngestLibraryEndpoint(unittest.TestCase):
    def test_agent_cannot_ingest(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/library/ingest', json={'sourcePath': '/tmp', 'agentId': 'ada'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('player-only', r.json()['error'])

    def test_missing_source_path_returns_400(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/library/ingest', json={})
        self.assertEqual(r.status_code, 400)

    def test_nonexistent_source_returns_404(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            r = _client().post('/api/library/ingest', json={'sourcePath': '/nonexistent/nowhere'})
        self.assertEqual(r.status_code, 404)

    def test_single_file_ingest(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'note.md')
            with open(path, 'w') as f:
                f.write('# a real note')
            with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
                 unittest.mock.patch.object(serve, 'log_action') as log:
                r = _client().post('/api/library/ingest', json={'sourcePath': path, 'destPath': 'ingested/notes'})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['okCount'], 1)
        self.assertEqual(body['totalCount'], 1)
        self.assertTrue(os.path.isfile(os.path.join(serve.LIBRARY_DIR, 'ingested/notes/note.md')))
        self.assertEqual(log.call_args[0][1], 'library_ingest')

    def test_directory_walk_ingest(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, 'src', 'sub'))
            for rel in ('src/a.txt', 'src/sub/b.txt'):
                with open(os.path.join(tmp, rel), 'w') as f:
                    f.write(rel)
            with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
                r = _client().post('/api/library/ingest', json={'sourcePath': os.path.join(tmp, 'src')})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['okCount'], 2)

    def test_zip_ingest_extracts_and_walks(self):
        tmp = tempfile.mkdtemp()
        try:
            zpath = os.path.join(tmp, 'bundle.zip')
            with zipfile.ZipFile(zpath, 'w') as zf:
                zf.writestr('one.txt', 'one')
                zf.writestr('sub/two.txt', 'two')
            with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
                r = _client().post('/api/library/ingest', json={'sourcePath': zpath})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['okCount'], 2)
        self.assertTrue(os.path.isfile(os.path.join(serve.LIBRARY_DIR, 'ingested/bundle.zip/one.txt')))

    def test_zip_bomb_guard_refuses(self):
        tmp = tempfile.mkdtemp()
        try:
            zpath = os.path.join(tmp, 'bomb.zip')
            with open(zpath, 'wb') as f:
                f.write(b'not really a zip')
            class FakeZF:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def infolist(self):
                    return [types.SimpleNamespace(file_size=serve.INGEST_MAX_FILE_BYTES * 11)]
                def extractall(self, dest):
                    raise AssertionError('must never extract')
            with unittest.mock.patch.object(serve.zipfile, 'ZipFile', return_value=FakeZF()), \
                 unittest.mock.patch.object(serve, 'verify_session', return_value=True):
                r = _client().post('/api/library/ingest', json={'sourcePath': zpath})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn('exceed the ingest cap', r.json()['error'])

    def test_bad_zip_returns_400(self):
        tmp = tempfile.mkdtemp()
        try:
            zpath = os.path.join(tmp, 'notzip.zip')
            with open(zpath, 'wb') as f:
                f.write(b'garbage bytes, not a zip at all')
            with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
                r = _client().post('/api/library/ingest', json={'sourcePath': zpath})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn('not a valid zip', r.json()['error'])
