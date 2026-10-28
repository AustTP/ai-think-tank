"""Self-edit dormancy window scheduling, lifecycle, and tick tests."""
import sys
import os
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402

from _selfedit_support import (  # noqa: E402
    fake_repo, _approve, _window_slot, _patch_player_approval, _patch_roster, _arm_edit
)

def test_schedule_window_happy_path_pends_escalation(fake_repo, monkeypatch):
    _, world = fake_repo
    edit_id = _arm_edit(monkeypatch, world)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nightly maintenance',
        'reviewer')
    assert ok is True, payload
    assert payload['status'] == 'pending'
    assert payload['escalationId'].startswith('esc-x')
    assert payload['validator'] == 'reviewer'
    assert payload['reservedEdits'] == [edit_id]
    with sqlite3.connect(serve.DB_PATH) as conn:
        reserved = conn.execute(
            'SELECT reserve_window FROM self_edit_log WHERE id = ?',
            (edit_id,)).fetchone()[0]
    assert reserved == payload['id']
    rows = selfedit.windows_snapshot()
    assert len(rows) == 1
    row = rows[0]
    assert row['agent'] == 'worker'
    assert row['date'] == _window_slot()
    assert row['start'] == '03:10'
    assert row['durationMin'] == 15
    assert row['status'] == 'pending'
    assert row['escalationId'] == payload['escalationId']
    assert row['validator'] == 'reviewer'


def test_window_row_precedes_escalation_and_is_findable_by_note(fake_repo, monkeypatch):
    _, world = fake_repo
    _arm_edit(monkeypatch, world)
    _patch_roster(monkeypatch)
    # Use the real create_escalation so the durable record + log are written,
    # but keep the escalations file inside the fake repo. Without this patch
    # the test wrote real pending escalations to the live tank's JSON.
    monkeypatch.setattr(serve, 'ESCALATIONS_PATH',
                        str(world / 'escalations.json'))
    # Only the outbound channels are stubbed.
    monkeypatch.setattr(serve, '_send_escalation_email_sync',
                        lambda *a, **k: False)
    monkeypatch.setattr(serve, 'send_player_telegram_sync',
                        lambda *a, **k: False)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '05:10', 15, 'nightly maintenance unique-%s' % os.getpid(),
        'reviewer')
    assert ok is True, payload
    esc_id = payload['escalationId']
    esc = serve._load_escalations()[esc_id]
    assert esc['note']
    # Simulate the old failure: the escalation exists but the link column was
    # never written. The fallback must still find the window by its id.
    with sqlite3.connect(serve.DB_PATH) as conn:
        conn.execute('UPDATE dormancy_windows SET escalation_id = NULL '
                     'WHERE id = ?', (esc['note'],))
    assert serve._dormancy_window_start(esc_id, esc) is not None
    with sqlite3.connect(serve.DB_PATH) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'escalation_created' "
            "AND details LIKE ?", ('%' + esc_id + '%',)).fetchone()[0]
    assert n == 1


def test_schedule_window_refused_by_conflicts(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    state = serve.get_state_from_db() or {}
    state['standingTasks'] = [{
        'taskId': 'st-eng-ritual', 'name': 'Engineering ritual block',
        'mode': 'window', 'startTime': '03:00', 'endTime': '04:05'}]
    serve.save_state_to_db(state)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nightly maintenance')
    assert ok is False
    assert 'collides with already scheduled items' in payload['error']
    assert any(c.get('name') == 'Engineering ritual block'
               for c in payload['conflicts'])
    # another dormancy window also blocks the same slot
    start_ts = selfedit.time.mktime(
        selfedit.time.strptime(f"{_window_slot()} 03:12", '%Y-%m-%d %H:%M'))
    selfedit._window_record('win-pending', selfedit.time.time(), 'other',
                            _window_slot(), '03:12', start_ts,
                            start_ts + 15 * 60, 15, 'their reason', 'pending')
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:20', 15, 'nightly maintenance 2')
    assert ok is False
    assert any(c.get('kind') == 'dormancy window'
               for c in payload['conflicts'])
    assert selfedit.windows_snapshot(limit=10)[-1]['agent'] == 'other'


def test_emergency_lane_shorter_lead_time(fake_repo, monkeypatch):
    """The emergency lane: changeType 'emergency' may start under 24h out
    (never under 1h), at the same slot a normal change is refused. The lane
    is loud -- the window record lands and the telegram announce fires even
    without notify_created -- while the 7-day cap stays for everyone."""
    import datetime
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_edit(monkeypatch, world)
    announced = []
    monkeypatch.setattr(selfedit, '_telegram_change_request_created',
                        lambda wid: announced.append(wid))
    target = datetime.datetime.now() + datetime.timedelta(hours=2)
    date = target.strftime('%Y-%m-%d')
    start = target.strftime('%H:%M')
    # normal change at the same near-term slot: refused
    ok, payload = selfedit.request_window('worker', date, start, 15, 'why')
    assert ok is False and 'at least 24 hours' in payload['error']
    # emergency at the same slot: accepted, announced, recorded
    ok, payload = selfedit.request_window('worker', date, start, 15, 'why',
                                          validator='reviewer',
                                          change_type='emergency')
    assert ok is True, payload
    assert announced == [payload['id']]
    assert any(w['agent'] == 'worker' for w in selfedit.windows_snapshot(limit=10))
    # emergency still has a floor: 30 minutes out is refused
    soon = datetime.datetime.now() + datetime.timedelta(minutes=30)
    ok, payload = selfedit.request_window('worker', soon.strftime('%Y-%m-%d'),
                                          soon.strftime('%H:%M'), 15, 'why',
                                          validator='reviewer',
                                          change_type='emergency')
    assert ok is False and 'at least 1 hour' in payload['error']
    # and the 7-day cap applies to the lane too
    ok, payload = selfedit.request_window('worker', _window_slot(31),
                                          '03:00', 15, 'why',
                                          validator='reviewer',
                                          change_type='emergency')
    assert ok is False and '7 days' in payload['error']


def test_schedule_window_time_and_range_validation(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.request_window('worker', '2020-01-01', '03:00',
                                          15, 'why')
    assert ok is False and 'at least 24 hours' in payload['error']
    ok, payload = selfedit.request_window('worker', 'bad-day', '03:00',
                                          15, 'why')
    assert ok is False and 'YYYY-MM-DD' in payload['error']
    ok, payload = selfedit.request_window('worker', _window_slot(), '25:99',
                                          15, 'why')
    assert ok is False and 'HH:MM' in payload['error']
    ok, payload = selfedit.request_window('worker', _window_slot(31),
                                          '03:00', 15, 'why')
    assert ok is False and '7 days' in payload['error']
    ok, payload = selfedit.request_window('worker', _window_slot(), '03:00',
                                          None, '')
    assert ok is False and 'reason is required' in payload['error']
    assert selfedit.windows_snapshot(limit=10) == []


def test_schedule_window_supervisor_refusal(fake_repo, monkeypatch):
    _, world = fake_repo
    _arm_edit(monkeypatch, world)
    _approve(monkeypatch, decision='reject', confidence=0.7)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'vague pause please',
        'reviewer')
    assert ok is False and payload['status'] == 'refused'
    assert 'supervisor review said' in payload['error']
    assert selfedit.windows_snapshot(limit=10) == []


def test_resolve_and_cancel_lifecycle(fake_repo, monkeypatch):
    _, world = fake_repo
    edit_id = _arm_edit(monkeypatch, world)
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nightly maintenance',
        'reviewer')
    window_id, esc_id = payload['id'], payload['escalationId']
    assert selfedit.resolve_window_escalation(esc_id, True) is True
    assert selfedit.resolve_window_escalation(esc_id, True) is False
    assert selfedit.windows_snapshot()[0]['status'] == 'approved'
    # the reservation survives approval (the window still fires later)
    with sqlite3.connect(serve.DB_PATH) as conn:
        reserved = conn.execute(
            'SELECT reserve_window FROM self_edit_log WHERE id = ?',
            (edit_id,)).fetchone()[0]
    assert reserved == window_id
    ok, payload = selfedit.cancel_window(window_id, by='player')
    assert ok is True and payload['status'] == 'cancelled'
    # cancellation releases the reservation back to the idle pool
    with sqlite3.connect(serve.DB_PATH) as conn:
        reserved = conn.execute(
            'SELECT reserve_window FROM self_edit_log WHERE id = ?',
            (edit_id,)).fetchone()[0]
    assert reserved is None
    ok, payload = selfedit.cancel_window('win-nope', by='player')
    assert ok is False and 'unknown window id' in payload['error']


def test_request_window_two_agent_contract(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    edit_id = _arm_edit(monkeypatch, world)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nightly maintenance')
    assert ok is False and 'a validator is required' in payload['error']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nightly maintenance',
        'worker')
    assert ok is False and 'different agent' in payload['error']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nightly maintenance',
        'ghost')
    assert ok is False and 'not a known agent' in payload['error']
    # a rostered validator with no armed edits of the implementer's
    monkeypatch.setattr(serve, 'get_state_from_db',
                        lambda: {'agents': {'idle': {}, 'reviewer': {}}})
    ok, payload = selfedit.request_window(
        'idle', _window_slot(), '03:10', 15, 'nightly maintenance',
        'reviewer')
    assert ok is False and 'no armed edits' in payload['error']
    assert selfedit.windows_snapshot(limit=10) == []
    with sqlite3.connect(serve.DB_PATH) as conn:
        reserved = conn.execute(
            'SELECT reserve_window FROM self_edit_log WHERE id = ?',
            (edit_id,)).fetchone()[0]
    assert reserved is None  # refused requests leave the edit unreserved


def test_window_tick_fires_forces_dormancy_and_marks_missed(
        fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_player_approval(monkeypatch)
    now = selfedit.time.time()
    # due now: start passed, end still in the future
    selfedit._window_record('win-due', now, 'worker', _window_slot(), '03:10',
                            now - 60, now + 600, 15, 'reason', 'approved')
    # gone: both edges already passed while the server was down
    selfedit._window_record('win-gone', now, 'worker', _window_slot(), '03:10',
                            now - 6000, now - 5000, 15, 'reason', 'approved')
    dormancy = []

    def fake_set(value):
        dormancy.append(value)

    monkeypatch.setattr(serve, '_set_dormant', fake_set)
    monkeypatch.setattr(serve, '_dormant', lambda: False)
    fired = selfedit.window_tick()
    assert fired == ['win-due']
    assert dormancy == [True]
    statuses = {r['id']: r['status'] for r in selfedit.windows_snapshot(limit=10)}
    assert statuses['win-due'] == 'fired'
    assert statuses['win-gone'] == 'missed'
    # the fired window is past its end: it must not count as a live hold
    selfedit._update_window('win-due', end_ts=now - 100)
    monkeypatch.setattr(serve, '_dormant', lambda: False)
    assert selfedit.window_tick() == []
    assert dormancy == [True]
    ok, payload = selfedit.cancel_window('win-due', by='player')
    assert ok is False  # a finished window cannot be cancelled


def test_dormancy_tools_surface_and_executor_forwarding(monkeypatch):
    names = [t['function']['name'] for t in serve.AGENT_ASK_TOOLS]
    assert 'schedule_dormant_apply' in names
    sent = []

    def fake_http(method, base, path, body, key, timeout=None,
                  extra_headers=None):
        sent.append({'method': method, 'path': path, 'body': body,
                     'key': key, 'timeout': timeout})
        return {'id': 'win-y', 'status': 'pending', 'escalationId': 'esc-z',
                'message': 'window requested.'}

    monkeypatch.setattr(serve, '_http_json', fake_http)
    exec_tool = serve._make_web_tools_executor('worker', 'key-w0rker')
    out = exec_tool('schedule_dormant_apply', {
        'date': _window_slot(), 'start': '03:10', 'durationMin': 25,
        'reason': 'nightly maintenance'})
    assert 'requires a validator' in out
    out = exec_tool('schedule_dormant_apply', {
        'date': _window_slot(), 'start': '03:10', 'durationMin': 25,
        'reason': 'nightly maintenance', 'validator': 'reviewer'})
    assert sent[0]['path'] == '/api/selfedit/schedule'
    assert sent[0]['body']['agentId'] == 'worker'
    assert sent[0]['body']['durationMin'] == 25
    assert sent[0]['body']['validator'] == 'reviewer'
    assert 'win-y' in out and 'pending' in out
    # conflict-shaped refusals render their list
    monkeypatch.setattr(
        serve, '_http_json',
        lambda *a, **k: {'error': 'collides with already scheduled items '
                                  '-- pick another time',
                         'conflicts': [
                             {'kind': 'standing task',
                              'name': 'Engineering ritual block'}]})
    out = exec_tool('schedule_dormant_apply', {
        'date': _window_slot(), 'start': '03:10',
        'reason': 'nightly maintenance', 'validator': 'reviewer'})
    assert 'Engineering ritual block' in out
