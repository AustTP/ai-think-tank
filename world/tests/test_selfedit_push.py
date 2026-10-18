"""Change-request push contract tests (two-agent validator -> push to main).

Every test runs against a TEMP repo (the fake_repo pattern from
test_selfedit.py: serve.ROOT/DB_PATH point at throwaways, the JEV quorum
seam is faked) and git is faked at selfedit._git_run, so no real repository
or network is ever touched. The contract under test:

  - a change window applies ONLY its reserved edits (the implementer's
    armed edits at request time),
  - a gate-green window HOLDS for its validator instead of pushing,
  - only the window's validator may approve/deny (selfedit_validate),
  - approval pushes the window's files to origin main and records the
    category (successful / successful_with_issues / unsuccessful) with a
    REQUIRED note whenever there are issues,
  - denial, push failure, unexpected tree dirt, and slot end without a
    decision all roll the window back like a failed gate (nothing pushed).
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selfedit  # noqa: E402
import serve  # noqa: E402
import sqlite3  # noqa: E402

from test_selfedit import (  # noqa: E402,F401
    fake_repo, _approve, _patch_player_approval, _patch_roster, _arm_edit,
    _notify_log, _fake_test_gate, _MARK, _window_slot)


def _fire_requested_window(window_id):
    """A change request is scheduled at least 5 minutes out; for tests,
    retime the approved window into the past so window_tick fires it."""
    now = selfedit.time.time()
    selfedit._update_window(window_id, status='approved',
                            start_ts=now - 60, end_ts=now + 600)
    fired = selfedit.window_tick()
    assert window_id in fired


def _fake_git(monkeypatch, plan=None):
    """Plan: dict step -> (rc, out, err). Default: everything succeeds."""
    calls = []
    plan = plan or {}

    def fake(args):
        step = args[0] if args else '?'
        calls.append(args)
        if step == 'rev-parse':
            return 0, 'abcdef123456\n', ''
        return plan.get(step, (0, '', ''))

    monkeypatch.setattr(selfedit, '_git_run', fake)
    return calls


def _watch_push_outcome(monkeypatch):
    telegrams = []
    monkeypatch.setattr(selfedit, '_notify_player_telegram',
                        lambda subject, body: telegrams.append((subject, body)))
    return telegrams


def _book_window(monkeypatch, fake_repo, implementer='worker',
                 validator='reviewer'):
    """Arm one edit, request the window, approve and fire it. Returns
    (window_id, edit_id, other_id); other_id is always None."""
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    edit_id = _arm_edit(monkeypatch, world, agent=implementer)
    ok, payload = selfedit.request_window(
        implementer, _window_slot(), '03:10', 15, 'ship the tuning change',
        validator)
    assert ok is True, payload
    window_id = payload['id']
    assert selfedit.resolve_window_escalation(payload['escalationId'], True)
    _fire_requested_window(window_id)
    return window_id, edit_id, None


def test_window_applies_only_reserved_edits(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    worker_edit = _arm_edit(monkeypatch, world, agent='worker')
    (world / 'other.py').write_text('B = 1\n', encoding='utf-8')
    ok, payload = selfedit.propose('reviewer', 'world/other.py', 'B = 1',
                                   'B = 2', 'reviewer side work')
    assert ok is True, payload
    other_edit = payload['id']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'ship the tuning change',
        'reviewer')
    assert ok is True, payload
    assert payload['reservedEdits'] == [worker_edit]
    assert selfedit.resolve_window_escalation(payload['escalationId'], True)
    _fire_requested_window(payload['id'])
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    applied = selfedit.apply_pass()
    assert applied == 1
    rows = {r['id']: r['status'] for r in selfedit.status_snapshot()}
    assert rows[worker_edit] == 'applied'
    assert rows[other_edit] == 'armed'  # bystander edit waits for idle path
    assert selfedit._window_field(payload['id'],
                                  'push_status') == 'awaiting_validator'


def test_hold_mails_the_validator(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL TEST FILES PASSED')])
    notes = _notify_log(monkeypatch)
    selfedit.apply_pass()
    assert selfedit._window_field(window_id, 'push_status') \
        == 'awaiting_validator'
    mailed = [a for a, _t in notes]
    assert 'reviewer' in mailed
    text = next(t for a, t in notes if a == 'reviewer')
    assert 'selfedit_validate' in text
    assert window_id in text


def test_only_the_validator_may_decide(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    _notify_log(monkeypatch)
    selfedit.apply_pass()
    ok, payload = selfedit.validate_window('worker', window_id, True, '', False)
    assert ok is False and "only the change request's validator" in payload['error']
    ok, payload = selfedit.validate_window('stranger', window_id, True, '', False)
    assert ok is False
    # nothing moved: the window still holds, the edit is still applied
    assert selfedit._window_field(window_id, 'push_status') \
        == 'awaiting_validator'
    assert selfedit.status_snapshot()[0]['status'] == 'applied'


def test_clean_approval_pushes_to_main(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    _notify_log(monkeypatch)
    selfedit.apply_pass()
    calls = _fake_git(monkeypatch)
    telegrams = _watch_push_outcome(monkeypatch)
    ok, payload = selfedit.validate_window('reviewer', window_id, True,
                                           '', False)
    assert ok is True and payload['status'] == 'successful', payload
    assert payload['sha'] == 'abcdef123456'
    assert calls[0] == ['status', '--porcelain']
    added = next(c for c in calls if c and c[0] == 'add')
    assert 'world/gear.py' in added[1:]
    commit = next(c for c in calls if c and c[0] == 'commit')
    assert f'selfedit: change window {window_id}' in commit[2]
    assert 'Implemented by worker; validated by reviewer.' in commit[2]
    assert ['push', 'origin', 'main'] in calls
    assert selfedit._window_field(window_id, 'push_status') == 'pushed'
    assert selfedit._window_field(window_id, 'push_category') == 'successful'
    assert selfedit._window_field(window_id, 'push_sha') == 'abcdef123456'
    assert any('SUCCESSFUL' in body and 'UNSUCCESSFUL' not in body
               for _s, body in telegrams)
    with sqlite3.connect(serve.DB_PATH) as conn:
        events = [r[0] for r in conn.execute(
            'SELECT details FROM action_log WHERE agent_id = ? '
            "AND action = 'repo_push'", (window_id,))]
    assert events, 'the push must be recorded in the action log'


def test_issues_require_note_then_push(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    _notify_log(monkeypatch)
    selfedit.apply_pass()
    # the window needed in-window remediation before the gate passed
    selfedit._update_window(window_id, remediation_used=1)
    calls = _fake_git(monkeypatch)
    _watch_push_outcome(monkeypatch)
    ok, payload = selfedit.validate_window('reviewer', window_id, True, '',
                                           False)
    assert ok is False and 'note is REQUIRED' in payload['error']
    assert calls == []  # approval without the note must not touch git
    ok, payload = selfedit.validate_window(
        'reviewer', window_id, True,
        'one fix was needed in-window; the result is sound', False)
    assert ok is True, payload
    assert payload['status'] == 'successful_with_issues'
    assert selfedit._window_field(window_id, 'push_category') \
        == 'successful_with_issues'
    assert selfedit._window_field(window_id, 'push_note') \
        == 'one fix was needed in-window; the result is sound'


def test_validator_flagged_issues_also_require_note(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    _notify_log(monkeypatch)
    selfedit.apply_pass()
    calls = _fake_git(monkeypatch)
    telegrams = _watch_push_outcome(monkeypatch)
    ok, payload = selfedit.validate_window('reviewer', window_id, True, '',
                                           True)
    assert ok is False and 'note is REQUIRED' in payload['error']
    assert calls == []
    ok, payload = selfedit.validate_window(
        'reviewer', window_id, True,
        'naming is off but the behavior is right', True)
    assert ok is True and payload['status'] == 'successful_with_issues'
    assert any('SUCCESSFUL WITH ISSUES' in body
               for _s, body in telegrams)


def test_validator_denial_rolls_back(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    window_id, edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    notes = _notify_log(monkeypatch)
    selfedit.apply_pass()
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 3\n'
    calls = _fake_git(monkeypatch)
    telegrams = _watch_push_outcome(monkeypatch)
    ok, payload = selfedit.validate_window('reviewer', window_id, False, '',
                                           False)
    assert ok is True and payload['status'] == 'unsuccessful', payload
    assert calls == []  # a denial never touches git
    assert selfedit._window_field(window_id, 'push_category') \
        == 'unsuccessful'
    assert selfedit._window_field(window_id, 'push_status') == 'failed'
    assert selfedit.status_snapshot()[0]['status'] == 'rolled_back'
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 2\n'
    assert any('UNSUCCESSFUL' in t
               for _s, t in telegrams)


def test_push_failure_rolls_back(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    window_id, edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    _notify_log(monkeypatch)
    selfedit.apply_pass()
    plan = {'push': (1, '', 'fatal: could not push to origin')}
    calls = _fake_git(monkeypatch, plan)
    telegrams = _watch_push_outcome(monkeypatch)
    ok, payload = selfedit.validate_window('reviewer', window_id, True, '',
                                           False)
    assert ok is True and payload['status'] == 'unsuccessful', payload
    assert ['push', 'origin', 'main'] in calls
    assert selfedit._window_field(window_id, 'push_category') \
        == 'unsuccessful'
    assert selfedit.status_snapshot()[0]['status'] == 'rolled_back'
    # local main never sits ahead of origin with unpushed bytes
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 2\n'
    assert any('UNSUCCESSFUL' in t for _s, t in telegrams)


def test_unexpected_tracked_change_blocks_push(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    _notify_log(monkeypatch)
    selfedit.apply_pass()
    plan = {'status': (0, ' M world/stranger.py\n', '')}
    calls = _fake_git(monkeypatch, plan)
    _watch_push_outcome(monkeypatch)
    ok, payload = selfedit.validate_window('reviewer', window_id, True, '',
                                           False)
    assert ok is True and payload['status'] == 'unsuccessful', payload
    assert calls == [  # refuse BEFORE add/commit/push ever run
        ['status', '--porcelain']]
    assert selfedit._window_field(window_id, 'push_category') \
        == 'unsuccessful'
    assert selfedit.status_snapshot()[0]['status'] == 'rolled_back'


def test_slot_end_awaiting_validation_rolls_back(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    window_id, edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    _notify_log(monkeypatch)
    selfedit.apply_pass()
    telegrams = _watch_push_outcome(monkeypatch)
    calls = _fake_git(monkeypatch)
    selfedit._update_window(window_id, end_ts=selfedit.time.time() - 10)
    selfedit.window_tick()
    assert selfedit.windows_snapshot()[0]['status'] == 'rolled_back'
    assert selfedit._window_field(window_id, 'push_category') \
        == 'unsuccessful'
    assert selfedit.status_snapshot()[0]['status'] == 'rolled_back'
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 2\n'
    assert calls == []  # an undecided window never pushes
    assert any('UNSUCCESSFUL' in t for _s, t in telegrams)


def test_validation_wake_exemption_and_headers(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id, _other = _book_window(monkeypatch, fake_repo)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    _notify_log(monkeypatch)
    selfedit.apply_pass()
    key = serve.get_or_create_agent_key('reviewer')

    class _Req:
        def __init__(self, path, headers):
            self.url = type('url', (), {'path': path})()
            self.headers = headers

    base = {'X-Dormancy-Window': window_id, 'X-Dormancy-Agent': 'reviewer',
            'X-Agent-Key': key}
    assert serve._selfedit_validation_exempt(
        _Req('/api/selfedit/validate', dict(base))) is True
    wrong = dict(base)
    wrong['X-Dormancy-Agent'] = 'worker'
    assert serve._selfedit_validation_exempt(
        _Req('/api/selfedit/validate', wrong)) is False
    stray = dict(base)
    stray['X-Dormancy-Window'] = 'win-other'
    assert serve._selfedit_validation_exempt(
        _Req('/api/selfedit/validate', stray)) is False
    assert serve._selfedit_validation_exempt(
        _Req('/api/browse', dict(base))) is False
    bad = dict(base)
    bad['X-Agent-Key'] = 'wrong-key'
    assert serve._selfedit_validation_exempt(
        _Req('/api/selfedit/validate', bad)) is False
    headers = serve._selfedit_validate_headers('reviewer')
    assert headers == {'X-Dormancy-Window': window_id,
                       'X-Dormancy-Agent': 'reviewer'}
    # once the window closes, the exemption closes with it
    selfedit._update_window(window_id, end_ts=selfedit.time.time() - 10)
    selfedit.window_tick()
    assert serve._selfedit_validate_headers('reviewer') is None


def test_schedule_tool_requires_validator_and_forwards_it(monkeypatch):
    names = [t['function']['name'] for t in serve.AGENT_ASK_TOOLS]
    assert 'selfedit_validate' in names
    sched = next(t for t in serve.AGENT_ASK_TOOLS
                 if t['function']['name'] == 'schedule_dormant_apply')
    assert 'validator' in sched['function']['parameters']['required']
    sent = {}

    def fake_http(method, base, path, body, key, timeout=None,
                  extra_headers=None):
        sent.update({'path': path, 'body': body})
        return {'id': 'win-x', 'status': 'pending', 'escalationId': 'esc-1',
                'reservedEdits': ['ed-1'], 'validator': 'reviewer',
                'message': 'window requested'}

    monkeypatch.setattr(serve, '_http_json', fake_http)
    exec_tool = serve._make_web_tools_executor('worker', 'key-w0rker')
    out = exec_tool('schedule_dormant_apply', {
        'date': '2030-01-01', 'start': '03:00',
        'reason': 'ship the tuning change'})
    assert 'requires a validator' in out
    out = exec_tool('schedule_dormant_apply', {
        'date': '2030-01-01', 'start': '03:00',
        'reason': 'ship the tuning change', 'validator': 'reviewer'})
    assert sent['path'] == '/api/selfedit/schedule'
    assert sent['body']['validator'] == 'reviewer'
    assert 'win-x' in out
    out = exec_tool('selfedit_validate', {'windowId': 'win-x',
                                          'approved': True})
    assert sent['path'] == '/api/selfedit/validate'
    assert sent['body']['approved'] is True


def test_unarmed_request_refused_and_nothing_reserved(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nothing armed yet',
        'reviewer')
    assert ok is False and 'no armed edits' in payload['error']
    assert selfedit.windows_snapshot(limit=10) == []


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
