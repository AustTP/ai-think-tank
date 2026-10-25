"""Self-edit remediation, rollback, and in-window test-gate behavior."""
import sys
import os
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402

from _selfedit_support import (  # noqa: E402
    fake_repo, _approve, _MARK, _fake_test_gate, _notify_log, _fire_window, _window_slot
)

def test_idle_path_failed_tests_rollback_and_save(monkeypatch, fake_repo):
    _, world = fake_repo
    _approve(monkeypatch)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    (world / 'marker.py').write_text(_MARK)
    notes = _notify_log(monkeypatch)
    ok, payload = selfedit.propose('worker', 'world/marker.py', 'MARKER_VALUE = 1',
                                   'MARKER_VALUE = 2', 'bump the marker')
    assert ok is True, payload
    _fake_test_gate(monkeypatch, [(False, 'ssl contract failed at the shipyard')])
    applied = selfedit.apply_pass()
    assert applied == 1
    row = selfedit.status_snapshot()[0]
    assert row['status'] == 'rolled_back'
    assert (world / 'marker.py').read_text() == _MARK
    saved = world / '.selfedit_failed' / 'no-window'
    saved_files = list(saved.glob('*.py'))
    assert saved_files, 'rejected candidate must survive under another name'
    assert any(notes and 'REVERTED' in t for _a, t in notes)
    monkeypatch.setattr(serve, '_dormant', lambda: False)
    selfedit.apply_pass()


def test_window_remediation_fix_recheck_recover(monkeypatch, fake_repo):
    _, world = fake_repo
    _approve(monkeypatch)
    now = selfedit.time.time()
    dormancy = []
    monkeypatch.setattr(serve, '_set_dormant', lambda v: dormancy.append(v))
    monkeypatch.setattr(serve, '_dormant', lambda: False)
    (world / 'marker.py').write_text(_MARK)
    _notify_log(monkeypatch)
    ok, payload = selfedit.propose('worker', 'world/marker.py', 'MARKER_VALUE = 1',
                                   'MARKER_VALUE = 2', 'bump the marker')
    assert ok is True, payload
    window_id = _fire_window(now - 60)
    # two-agent contract: the window applies only its reserved edits
    with sqlite3.connect(serve.DB_PATH) as conn:
        conn.execute('UPDATE self_edit_log SET reserve_window = ? '
                     'WHERE id = ?', (window_id, payload['id']))
    assert dormancy == [True]
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(False, 'boom: marker tests broken')])
    selfedit.apply_pass()
    assert selfedit.status_snapshot()[0]['status'] == 'failed'
    assert selfedit.remediation_window() == window_id
    ok, payload = selfedit.submit_remediation_fix(
        'worker', 'world/marker.py', 'MARKER_VALUE = 2', 'MARKER_VALUE = 3',
        'the shipped bump broke assertions; smaller bump')
    assert ok is True, payload
    assert (world / 'marker.py').read_text() == '# marker module\nMARKER_VALUE = 3\n'
    _fake_test_gate(monkeypatch, [(True, 'ALL TEST FILES PASSED')])
    ok, payload = selfedit.run_remediation_checks('worker')
    assert payload.get('tests') == 'pass', payload
    rows = selfedit.status_snapshot()
    assert all(r['status'] == 'applied' for r in rows
               if r['path'] == 'world/marker.py')
    assert selfedit.remediation_window() is None
    # NEW contract: gate green now holds the window for its validator; the
    # validator must approve (with a note -- remediation was used) before
    # the push, and only a pushed window closes as done.
    assert selfedit._window_field(window_id, 'push_status') \
        == 'awaiting_validator'
    selfedit._update_window(window_id, validator='reviewer')
    git_calls = []

    def fake_git(args):
        git_calls.append(args)
        if args and args[0] == 'rev-parse':
            return 0, 'abcdef123456\n', ''
        return 0, '', ''

    monkeypatch.setattr(selfedit, '_git_run', fake_git)
    telegrams = []
    monkeypatch.setattr(selfedit, '_notify_player_telegram',
                        lambda subject, body: telegrams.append((subject, body)))
    ok, payload = selfedit.validate_window('reviewer', window_id, True, '', False)
    assert ok is False and 'note is REQUIRED' in payload['error'], payload
    assert git_calls == []  # nothing pushed before the note exists
    ok, payload = selfedit.validate_window(
        'reviewer', window_id, True,
        'in-window fixes recovered the failed assert', False)
    assert ok is True and payload['status'] == 'successful_with_issues', payload
    assert selfedit._window_field(window_id, 'push_sha') == 'abcdef123456'
    commit = next(c for c in git_calls if c and c[0] == 'commit')
    assert 'Implemented by worker; validated by reviewer.' in commit[2]
    assert any('SUCCESSFUL WITH ISSUES' in body for _s, body in telegrams)
    # a healthy ending: no rollback file trash, window closes as done
    selfedit._update_window(window_id, end_ts=selfedit.time.time() - 10)
    selfedit.window_tick()
    windows = selfedit.windows_snapshot()
    assert windows[0]['status'] == 'done'
    assert not (world / '.selfedit_failed').exists()


def test_window_end_unresolved_rolls_back(monkeypatch, fake_repo):
    _, world = fake_repo
    _approve(monkeypatch)
    now = selfedit.time.time()
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    (world / 'marker.py').write_text(_MARK)
    notes = _notify_log(monkeypatch)
    ok, payload = selfedit.propose('worker', 'world/marker.py', 'MARKER_VALUE = 1',
                                   'MARKER_VALUE = 2', 'bump the marker')
    window_id = _fire_window(now - 60)
    # two-agent contract: the window applies only its reserved edits
    with sqlite3.connect(serve.DB_PATH) as conn:
        conn.execute('UPDATE self_edit_log SET reserve_window = ? '
                     'WHERE id = ?', (window_id, payload['id']))
    _fake_test_gate(monkeypatch, [(False, 'still failing')])
    selfedit.apply_pass()
    # nobody fixes in time: the slot ends
    selfedit._update_window(window_id, end_ts=selfedit.time.time() - 10)
    selfedit.window_tick()
    row = selfedit.status_snapshot()[0]
    assert row['status'] == 'rolled_back'
    assert (world / 'marker.py').read_text() == _MARK
    saved = world / '.selfedit_failed' / window_id
    assert list(saved.glob('*.py')), 'candidate must survive by another name'
    windows = selfedit.windows_snapshot()
    assert windows[0]['status'] == 'rolled_back'
    assert any('REVERTED' in t and 'schedule a new change window' in t
               for _a, t in notes)


def test_remediation_gates_and_wake_exemption(monkeypatch, fake_repo):
    _, world = fake_repo
    ok, payload = selfedit.submit_remediation_fix(
        'worker', 'world/marker.py', 'A', 'B', 'why')
    assert ok is False and 'no remediation session' in payload['error']
    ok, payload = selfedit.run_remediation_checks('worker')
    assert ok is False and 'no remediation session' in payload['error']
    # open one for real to check the middleware boundary reply
    now = selfedit.time.time()
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    window_id = _fire_window(now - 60)
    # a failed row makes the session open
    key = serve.get_or_create_agent_key('worker')
    selfedit._record('ed-fail', now, 'worker', 'world/marker.py', 'why',
                     'x' * 64, 'y' * 64, 'failed')
    selfedit._update('ed-fail', window_id=window_id, status='failed')

    class _Req:
        class url:
            path = '/api/selfedit/fix/edit'
        headers = {'X-Dormancy-Window': window_id,
                   'X-Dormancy-Agent': 'worker', 'X-Agent-Key': key}

    assert serve._selfedit_remediation_exempt(_Req()) is True
    _Req.headers['X-Dormancy-Window'] = 'win-other'
    assert serve._selfedit_remediation_exempt(_Req()) is False
    _Req.headers['X-Dormancy-Window'] = window_id
    _Req.headers['X-Agent-Key'] = 'wrong-key'
    assert serve._selfedit_remediation_exempt(_Req()) is False
    _Req.url.path = '/api/browse'
    _Req.headers['X-Agent-Key'] = key
    assert serve._selfedit_remediation_exempt(_Req()) is False


def test_remediation_executor_tools_attach_slot_headers(monkeypatch, fake_repo):
    _, world = fake_repo
    now = selfedit.time.time()
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    (world / 'marker.py').write_text(_MARK)
    key = serve.get_or_create_agent_key('worker')
    selfedit._record('ed-fail', now, 'worker', 'world/marker.py', 'why',
                     'x' * 64, 'y' * 64, 'failed')
    selfedit._update('ed-fail', window_id='win-live-0', status='failed')
    selfedit._window_record('win-live-0', now, 'worker', _window_slot(),
                            '03:10', now - 60, now + 600, 15, 'r', 'fired')
    captured = []

    def fake_http(method, base, path, body, key, timeout=None, extra_headers=None):
        captured.append({'path': path, 'headers': extra_headers})
        if 'fix/edit' in path:
            return {'id': 'fix-x', 'status': 'applied', 'window': 'win-live-0'}
        return {'ok': True, 'tests': 'pass'}

    monkeypatch.setattr(serve, '_http_json', fake_http)
    exec_tool = serve._make_web_tools_executor('worker', key)
    out = exec_tool('selfedit_fix', {
        'path': 'world/marker.py', 'find': 'MARKER_VALUE = 2',
        'replace': 'MARKER_VALUE = 3', 'summary': 'correction'})
    assert captured[-1]['headers']['X-Dormancy-Window'] == 'win-live-0'
    assert 'fix-x' in out and 'selfedit_recheck' in out
    out = exec_tool('selfedit_recheck', {})
    assert captured[-1]['headers']['X-Dormancy-Agent'] == 'worker'
    assert 'PASSES' in out


def test_propose_refuses_test_gate_files(fake_repo):
    # Tier A (audit 2026-10-18): the repo's own test gate may never be
    # edited -- rewriting it would rewrite the acceptance bar itself.
    _, world = fake_repo
    (world / 'conftest.py').write_text('# gate helper\n', encoding='utf-8')
    ok, payload = selfedit.propose(
        'worker', 'conftest.py', '# gate helper', '# rigged', 'why')
    assert ok is False and 'test gate' in payload['error'], payload
    assert (world / 'conftest.py').read_text(encoding='utf-8') \
        == '# gate helper\n'
    (world / 'tests').mkdir()
    (world / 'tests' / 'run_all.sh').write_text('# gate\n', encoding='utf-8')
    assert selfedit.is_gate_file(str(world / 'tests' / 'run_all.sh'))
