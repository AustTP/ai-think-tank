"""Coverage-gap tests for change-window parsing, rechecks, vetoes, and admin."""
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402

from _selfedit_support import (  # noqa: E402
    fake_repo, _window_slot, _patch_player_approval
)

NORMAL_STATE = {'agents': {'worker': {}, 'reviewer': {}}}
ADMIN_STATE = {
    'agents': {'worker': {}, 'peer': {}, 'boss': {}},
    'agentRoster': [{'id': 'boss', 'isAdmin': True},
                    {'id': 'worker', 'director': 'boss'},
                    {'id': 'peer'}],
    'governance': {'changeRequestAdminApproval': True},
}


def _arm_row(edit_id='ed-1', path='world/gear.py',
             policy='idle_or_window', agent='worker', status='armed'):
    selfedit._record(edit_id, time.time(), agent, path, 'summary',
                     'before', 'after', status, apply_policy=policy)


def _mk_window(window_id='win-cov', **fields):
    now = time.time()
    selfedit._window_record(window_id, now, 'worker', _window_slot(),
                            '03:10', now - 30, now + 30, 15, 'reason',
                            'approved')
    if fields:
        selfedit._update_window(window_id, **fields)
    return window_id


def _reserve(edit_id, window_id):
    with serve._db() as conn:
        selfedit._ensure_table(conn)
        conn.execute('UPDATE self_edit_log SET reserve_window = ? '
                     'WHERE id = ?', (window_id, edit_id))


def _high_risk_fields():
    return dict(
        title='Trading engine durability', description='Land the reviewed fix',
        change_type='normal', risk_level='high', service_mode='batch',
        file_manifest=['world/gear.py'],
        rollback_plan='Restore the previous bytes.',
        rollback_owner='reviewer', success_criteria='Repo gate green',
        abort_criteria='Any mismatch',
        post_apply_checks='Run the gate',
        on_call='reviewer', acknowledgements=['reviewer'])


# --- part 20: normalizers -------------------------------------------------

def test_cov_normalizers_cover_invalid_inputs():
    assert selfedit._norm_choice('', ('a',), 'fallback') == 'fallback'
    assert selfedit._manifest_paths(None) == (None, None)
    ok, err = selfedit._manifest_paths('not-a-list')
    assert ok is None and 'must be a list' in err
    ok, err = selfedit._manifest_paths([1])
    assert ok is None and 'non-empty path' in err
    assert selfedit._manifest_paths(['a.py', {'path': 'b.py'},
                                     {'path': 'a.py'}])[0] == ['a.py', 'b.py']
    ok, err = selfedit._manifest_paths([f'p{i}.py' for i in range(101)])
    assert ok is None and 'limited to' in err

    assert selfedit._ack_agents(None) == (None, None)
    ok, err = selfedit._ack_agents('nope')
    assert ok is None and 'must be a list' in err
    acks, err = selfedit._ack_agents([{'agent': 'a', 'at': 'bad'}, {'id': 'a'}])
    assert err is None and acks == [{'agent': 'a', 'at': acks[0]['at']}]
    ok, err = selfedit._ack_agents([123])
    assert ok is None and 'non-empty agent' in err
    many = [{'agent': f'a{i}'} for i in range(101)]
    ok, err = selfedit._ack_agents(many)
    assert ok is None and 'too many acknowledgements' in err


def test_cov_blackout_parser_and_conflicts():
    assert selfedit._parse_blackout_windows(None) == (None, None)
    ok, err = selfedit._parse_blackout_windows('bad')
    assert ok is None and 'must be a list' in err
    ok, err = selfedit._parse_blackout_windows([3])
    assert ok is None and 'must be an object' in err
    ok, err = selfedit._parse_blackout_windows([{'start': '', 'end': ''}])
    assert ok is None and 'needs start and end' in err
    parsed, err = selfedit._parse_blackout_windows([
        {'start': '2026-01-05T10:00', 'end': '2026-01-05T11:00',
         'label': 'abs'}])
    assert err is None and parsed[0]['kind'] == 'absolute'
    ok, err = selfedit._parse_blackout_windows([
        {'start': '2026-01-05T11:00', 'end': '2026-01-05T10:00'}])
    assert ok is None and 'must end after' in err
    ok, err = selfedit._parse_blackout_windows([
        {'start': 'aa', 'end': 'bb', 'label': 'x'}])
    assert ok is None and 'absolute' in err
    ok, err = selfedit._parse_blackout_windows([
        {'start': '11:00', 'end': '10:00'}])
    assert ok is None and 'must end after' in err
    ok, err = selfedit._parse_blackout_windows([
        {'start': '10:00', 'end': '11:00', 'days': 'x'}])
    assert ok is None and 'days must be' in err
    ok, err = selfedit._parse_blackout_windows([
        {'start': '10:00', 'end': '11:00', 'days': ['x']}])
    assert ok is None and 'days must be' in err
    ok, err = selfedit._parse_blackout_windows([
        {'start': '10:00', 'end': '11:00', 'days': [7]}])
    assert ok is None and '0..6' in err
    ok, err = selfedit._parse_blackout_windows(
        [{'start': '10:00', 'end': '11:00'} for _ in range(51)])
    assert ok is None and 'limited to' in err

    now = time.time()
    moment = datetime.datetime.fromtimestamp(now).replace(second=0,
                                                          microsecond=0)
    abs_start = (moment - datetime.timedelta(minutes=1)).strftime(
        '%Y-%m-%dT%H:%M')
    abs_end = (moment + datetime.timedelta(minutes=1)).strftime(
        '%Y-%m-%dT%H:%M')
    assert selfedit._blackout_conflicts(
        now - 5, now + 5,
        [{'kind': 'absolute', 'start': abs_start, 'end': abs_end,
          'label': 'abs'}]) == ['abs']
    assert selfedit._blackout_conflicts(
        now - 5, now + 5,
        [{'kind': 'absolute', 'start': 'bad', 'end': 'bad'}]) == []
    minute = moment.hour * 60 + moment.minute
    clock_start = f'{max(0, minute - 1) // 60:02d}:{max(0, minute - 1) % 60:02d}'
    clock_end = f'{min(1439, minute + 1) // 60:02d}:{min(1439, minute + 1) % 60:02d}'
    daily = {'kind': 'daily', 'start': clock_start, 'end': clock_end,
             'days': [moment.weekday()], 'label': 'daily'}
    assert selfedit._blackout_conflicts(now - 5, now + 5, [daily]) == ['daily']
    other_day = dict(daily, days=[(moment.weekday() + 1) % 7])
    assert selfedit._blackout_conflicts(now - 5, now + 5, [other_day]) == []
    bad_daily = dict(daily, start='bad', end='bad')
    assert selfedit._blackout_conflicts(now - 5, now + 5, [bad_daily]) == []


def test_cov_revert_kernel_schedules_restart(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'serve.py').write_text('X = 1\n', encoding='utf-8')
    edit_id = 'ed-cov-kernel'
    selfedit._record(edit_id, time.time(), 'worker', 'world/serve.py',
                     'summary', 'before', 'after', 'applied')
    backups = world / '.selfedit_backups'
    backups.mkdir()
    (backups / edit_id).write_text('X = 0\n', encoding='utf-8')
    restarted = []
    monkeypatch.setattr(selfedit, '_schedule_kernel_restart',
                        lambda: restarted.append(True))
    ok, payload = selfedit.revert(edit_id)
    assert ok is True, payload
    assert restarted == [True]
    assert (world / 'serve.py').read_text(encoding='utf-8') == 'X = 0\n'


# --- part 20: request_window validation -----------------------------------

def test_cov_request_window_choice_and_manifest_guards(fake_repo, monkeypatch):
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'reviewer',
        change_type='nope')
    assert ok is False and 'changeType' in payload['error']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'reviewer',
        service_mode='nope')
    assert ok is False and 'serviceMode' in payload['error']

    monkeypatch.setattr(serve, 'get_state_from_db', lambda: NORMAL_STATE)
    _arm_row()
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'reviewer',
        file_manifest='not-a-list')
    assert ok is False and 'fileManifest must be a list' in payload['error']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'reviewer',
        acknowledgements='nope')
    assert ok is False and 'acknowledgements must be a list' in payload['error']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'reviewer',
        blackout_windows=[3])
    assert ok is False and 'must be an object' in payload['error']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'reviewer',
        acknowledgements=['ghost'])
    assert ok is False and 'unknown acknowledgement' in payload['error']


def test_cov_request_window_event_duration_cap(fake_repo, monkeypatch):
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: NORMAL_STATE)
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 120, 'why', 'reviewer',
        service_mode='event_driven')
    assert ok is False


def test_cov_request_window_admin_validator_guards(fake_repo, monkeypatch):
    _patch_player_approval(monkeypatch)
    _arm_row()
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: ADMIN_STATE)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'peer')
    assert ok is False and 'must be a director' in payload['error']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'boss')
    assert ok is False and 'other than the admin' in payload['error']


def test_cov_request_window_high_risk_detail_guards(fake_repo, monkeypatch):
    _patch_player_approval(monkeypatch)
    _arm_row(policy='window_only')
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: NORMAL_STATE)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'reviewer',
        risk_level='high', service_mode='batch',
        file_manifest=['world/gear.py'])
    assert ok is False and 'high-risk change requests require' in payload['error']

    fields = _high_risk_fields()
    del fields['acknowledgements']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'why', 'reviewer', **fields)
    assert ok is False and 'at least one acknowledgement' in payload['error']


# --- part 30: fire-time blocker rechecks ----------------------------------

def test_cov_fire_blocker_missing_and_malformed(fake_repo, monkeypatch):
    assert selfedit._window_fire_blockers(None) == ['unknown window']
    blockers = selfedit._window_fire_blockers({'id': 'bad'})
    assert blockers and 'conflict scan failed' in blockers[0]
    _mk_window('win-mal', blackout_windows='not-json')
    monkeypatch.setattr(selfedit, '_window_conflicts', lambda *a, **k: [])
    assert selfedit._window_fire_blockers(
        selfedit._window_row('win-mal')) == []


def test_cov_fire_blocker_blackout_conflict(fake_repo, monkeypatch):
    now = time.time()
    moment = datetime.datetime.fromtimestamp(now).replace(second=0,
                                                          microsecond=0)
    start = (moment - datetime.timedelta(minutes=1)).strftime(
        '%Y-%m-%dT%H:%M')
    end = (moment + datetime.timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M')
    blackouts = [{'kind': 'absolute', 'start': start, 'end': end,
                  'label': 'market'}]
    _mk_window('win-black', blackout_windows=json.dumps(blackouts))
    monkeypatch.setattr(selfedit, '_window_conflicts', lambda *a, **k: [])
    blockers = selfedit._window_fire_blockers(
        selfedit._window_row('win-black'))
    assert any('blackout market' in b for b in blockers)


def test_cov_fire_blocker_roster_and_manifest(fake_repo, monkeypatch):
    monkeypatch.setattr(selfedit, '_window_conflicts', lambda *a, **k: [])
    monkeypatch.setattr(selfedit, '_blackout_conflicts', lambda *a, **k: [])
    # A request-metadata window with no validator.
    _mk_window('win-nov', validator='', risk_level='low')
    blockers = selfedit._window_fire_blockers(selfedit._window_row('win-nov'))
    assert 'no validator on record' in blockers
    assert 'no reserved edits are still armed' in blockers

    # Validator vanished from the roster.
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: NORMAL_STATE)
    _mk_window('win-ghost', validator='ghost', file_manifest='[]',
               risk_level='low')
    blockers = selfedit._window_fire_blockers(
        selfedit._window_row('win-ghost'))
    assert any('no longer on the roster' in b for b in blockers)

    # Admin mode: validator must be a director other than the admin.
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: ADMIN_STATE)
    _mk_window('win-peer', validator='peer', file_manifest='[]',
               risk_level='low')
    blockers = selfedit._window_fire_blockers(
        selfedit._window_row('win-peer'))
    assert any('no longer a director' in b for b in blockers)
    _mk_window('win-boss', validator='boss', file_manifest='[]',
               risk_level='low')
    blockers = selfedit._window_fire_blockers(
        selfedit._window_row('win-boss'))
    assert any('validator is the admin' in b for b in blockers)

    # Reserved rows drive the manifest recheck.
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: NORMAL_STATE)
    _arm_row('ed-manifest', path='world/gear.py')
    _mk_window('win-manifest', validator='reviewer',
               file_manifest='not-json', risk_level='low')
    _reserve('ed-manifest', 'win-manifest')
    blockers = selfedit._window_fire_blockers(
        selfedit._window_row('win-manifest'))
    assert not any('manifest no longer matches' in b for b in blockers)
    selfedit._update_window('win-manifest',
                            file_manifest=json.dumps(['world/other.py']))
    blockers = selfedit._window_fire_blockers(
        selfedit._window_row('win-manifest'))
    assert any('manifest no longer matches' in b for b in blockers)


def test_cov_fire_blocker_high_risk_checks(fake_repo, monkeypatch):
    monkeypatch.setattr(selfedit, '_window_conflicts', lambda *a, **k: [])
    monkeypatch.setattr(selfedit, '_blackout_conflicts', lambda *a, **k: [])
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: NORMAL_STATE)
    _arm_row('ed-idle', path='world/gear.py', policy='idle_or_window')
    _mk_window('win-high', validator='reviewer',
               file_manifest=json.dumps(['world/gear.py']),
               risk_level='high', service_mode='idle_ok')
    _reserve('ed-idle', 'win-high')
    blockers = selfedit._window_fire_blockers(
        selfedit._window_row('win-high'))
    assert any('window-only edits required' in b for b in blockers)

    with serve._db() as conn:
        selfedit._ensure_table(conn)
        conn.execute("UPDATE self_edit_log SET apply_policy='window_only' "
                     'WHERE id = ?', ('ed-idle',))
    selfedit._update_window('win-high', pre_apply_result='fail')
    blockers = selfedit._window_fire_blockers(
        selfedit._window_row('win-high'))
    assert 'pre-apply gate did not pass' in blockers


# --- part 30: veto branches ----------------------------------------------

def test_cov_veto_status_branches(fake_repo, monkeypatch):
    monkeypatch.setattr(selfedit, '_notify_agent', lambda *a, **k: None)
    monkeypatch.setattr(selfedit, '_notify_player_telegram', lambda *a, **k: None)
    ok, payload = selfedit.veto_window('win-missing', 'player', 'reason')
    assert ok is False and 'unknown window id' in payload['error']
    _mk_window('win-already', status='vetoed')
    ok, payload = selfedit.veto_window('win-already', 'player', 'reason')
    assert ok is False and 'already vetoed' in payload['error']
    _mk_window('win-deferred', status='deferred_conflict')
    ok, payload = selfedit.veto_window('win-deferred', 'player', 'reason')
    assert ok is True and payload['status'] == 'vetoed'
    _mk_window('win-pushed', status='fired', push_status='pushed')
    ok, payload = selfedit.veto_window('win-pushed', 'player', 'reason')
    assert ok is False and 'already pushed' in payload['error']
    _mk_window('win-odd', status='odd')
    ok, payload = selfedit.veto_window('win-odd', 'player', 'reason')
    assert ok is False and 'nothing to veto' in payload['error']


def test_cov_veto_after_fire_with_failed_rows(fake_repo, monkeypatch):
    monkeypatch.setattr(selfedit, '_notify_agent', lambda *a, **k: None)
    monkeypatch.setattr(selfedit, '_notify_player_telegram', lambda *a, **k: None)
    _mk_window('win-failed', status='fired', push_status=None)
    _arm_row('ed-failed', status='failed')
    selfedit._update('ed-failed', window_id='win-failed')
    ok, payload = selfedit.veto_window('win-failed', 'player', 'stop')
    assert ok is True, payload
    note = selfedit._window_field('win-failed', 'note')
    assert 'failed edit(s) kept' in note


def test_cov_snapshot_and_telegram_decode_errors(fake_repo, monkeypatch):
    _mk_window('win-bad-json', file_manifest='not-json',
               acknowledgements='[bad', blackout_windows='also-bad')
    row = next(r for r in selfedit.windows_snapshot(limit=10)
               if r['id'] == 'win-bad-json')
    assert row['fileManifest'] == []
    assert row['acknowledgements'] == []
    sent = []
    monkeypatch.setattr(selfedit, '_notify_player_telegram',
                        lambda subject, body: sent.append(subject))
    selfedit._telegram_change_request_created('win-bad-json')
    assert sent == ['[AI Think Tank] Change request created']
    ok, payload = selfedit.veto_window_by_link('win-bad-json', '')
    assert ok is False and 'expired veto link' in payload['error']


def test_cov_agent_display_name_falls_back_on_state_error(monkeypatch):
    def boom():
        raise RuntimeError('state unavailable')

    monkeypatch.setattr(serve, 'get_state_from_db', boom)
    assert selfedit._agent_display_name('worker') == 'worker'
    assert selfedit._agent_display_name('') == '?'
    assert selfedit._agent_display_name('player') == 'player'


# --- part 40: admin approval and validator fallback ----------------------

def test_cov_admin_approve_guards(fake_repo, monkeypatch):
    ok, payload = selfedit.admin_approve_window('anyone', 'win-x', True)
    assert ok is False and 'only the admin' in payload['error']
    monkeypatch.setattr(serve, '_is_admin', lambda state, agent: True)
    ok, payload = selfedit.admin_approve_window('boss', 'win-x', True)
    assert ok is False and 'unknown window id' in payload['error']
    _mk_window('win-pending', status='pending')
    ok, payload = selfedit.admin_approve_window('boss', 'win-pending', True)
    assert ok is False and 'nothing is waiting' in payload['error']


def test_cov_admin_denial_requires_note_and_rolls_back(fake_repo, monkeypatch):
    monkeypatch.setattr(serve, '_is_admin', lambda state, agent: True)
    monkeypatch.setattr(selfedit, '_notify_agent', lambda *a, **k: None)
    monkeypatch.setattr(selfedit, '_notify_player_telegram', lambda *a, **k: None)
    _mk_window('win-admin', status='fired', push_status='awaiting_validator')
    ok, payload = selfedit.admin_approve_window('boss', 'win-admin', False, '')
    assert ok is False and 'note is REQUIRED' in payload['error']
    ok, payload = selfedit.admin_approve_window('boss', 'win-admin', False,
                                                'not safe')
    assert ok is True and payload['status'] == 'unsuccessful'
    assert selfedit._window_field('win-admin', 'admin_status') == 'denied'
    assert selfedit._window_field('win-admin', 'push_status') == 'failed'


def test_cov_admin_approve_waits_for_director(fake_repo, monkeypatch):
    monkeypatch.setattr(serve, '_is_admin', lambda state, agent: True)
    _mk_window('win-director', status='fired',
               push_status='awaiting_validator', validator_status='pending')
    ok, payload = selfedit.admin_approve_window('boss', 'win-director', True,
                                                'looks good')
    assert ok is True and payload['status'] == 'awaiting_director'


def test_cov_validate_admin_fallback(fake_repo, monkeypatch):
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: ADMIN_STATE)
    _mk_window('win-fallback', status='fired',
               push_status='awaiting_validator', validator='reviewer',
               validator_status='pending')
    ok, payload = selfedit.validate_window('boss', 'win-fallback', True,
                                           'looks good')
    assert ok is True and payload['status'] == 'awaiting_director'
