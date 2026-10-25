import json
import os
import sqlite3

import integrations.apis as apis
import agents.coding_library as coding_library
import server.confirmations_api as confirmations_api
import agents.python_delivery as python_delivery
import agents.skill_scan as skill_scan
import server.state_updates as state_updates
import agents.work_reuse as work_reuse
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_apis_pixellab_fallback(monkeypatch):
    import serve
    monkeypatch.setattr(serve, '_open_secret', lambda t: None)
    monkeypatch.setattr(serve, '_credential_token', lambda n: 'enc')
    monkeypatch.setattr(serve, '_pixellab_token', lambda: 'px')
    token, token2, err = apis._api_credential_value({'credential': {'type': 'vault', 'name': 'pixellab'}})
    assert (token, token2, err) == ('px', None, None)


def test_skill_scan_duplicate_same_line():
    rep = skill_scan.scan_skill_text('sudo sudo')
    assert len([f for f in rep['findings'] if f['rule'] == 'privilege-escalation']) == 1


def test_python_delivery_context_break(tmp_path):
    (tmp_path / 'a.py').write_text('x = 1\n')
    assert python_delivery.context(tmp_path, limit=1)


def test_state_updates_cycle_with_protected():
    current = {'tasks': {'p': {'id': 'p', 'status': 'done', 'note': 'done'},
                         'c': {'id': 'c', 'reviewOf': 'c'}},
               'workQueue': [], 'agents': {}, 'playerInbox': []}
    incoming = {'tasks': {'p': {'id': 'p', 'status': 'working'},
                          'c': {'id': 'c', 'reviewOf': 'c'}},
                'workQueue': [], 'agents': {}, 'playerInbox': []}
    state_updates.preserve_guidance(current, incoming)


def test_work_reuse_limits_and_oserror(tmp_path, monkeypatch):
    (tmp_path / 'a.py').write_text('x')
    monkeypatch.setattr(work_reuse.os.path, 'getsize', lambda p: 101 * 1024 * 1024)
    assert work_reuse.tree_digest(tmp_path) is None
    monkeypatch.undo()
    monkeypatch.setattr(work_reuse, 'open', lambda *a, **k: (_ for _ in ()).throw(OSError('x')), raising=False)
    assert work_reuse.tree_digest(tmp_path) is None


def test_coding_library_errors(tmp_path):
    lib = tmp_path / 'lib'
    lib.mkdir()
    (lib / 'a.txt').write_text('hello')
    class FakeServe:
        LIBRARY_DIR = str(lib)
        SANDBOXES_DIR = str(tmp_path / 'sandboxes')
        def log_action(self, *a, **k): pass
        def record_library_read(self, *a): pass
        def _library_search_matches(self, q): return []
    s = FakeServe()
    assert 'error' in json.loads(coding_library.execute({'action': 'search', 'query': ' '}, s, 'a', 's'))
    assert 'error' in json.loads(coding_library.execute({'action': 'read', 'path': 'a.txt', 'offset': -1}, s, 'a', 's'))
    assert 'error' in json.loads(coding_library.execute({'action': 'copy', 'path': 'a.txt'}, s, 'a', 's'))
    assert 'error' in json.loads(coding_library.execute({'action': 'nope'}, s, 'a', 's'))
