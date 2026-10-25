"""Change-request detail, pre-apply gate, and fire-time revalidation tests."""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402

from _selfedit_support import (  # noqa: E402,F401
    fake_repo, _approve, _patch_player_approval, _patch_roster,
    _window_slot, _fake_test_gate)


def _arm_window_only(monkeypatch, world, agent='worker', path='gear.py'):
    (world / path).write_text('SPEED = 2\n', encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        agent, path, 'SPEED = 2', 'SPEED = 3', 'trading engine tuning',
        window_only=True)
    assert ok is True, payload
    return payload['id']


def _high_risk_fields():
    return dict(
        title='Trading engine durability', description='Land the reviewed fix',
        change_type='normal', risk_level='high', service_mode='batch',
        file_manifest=['world/gear.py'],
        rollback_plan='Restore the previous bytes and reconcile positions.',
        rollback_owner='reviewer', success_criteria='Repo gate green and '
        'positions reconcile', abort_criteria='Any order or balance mismatch',
        post_apply_checks='Run the gate, then reconcile orders and balances',
        on_call='reviewer', acknowledgements=['reviewer'])


def _pass_pre_apply(monkeypatch):
    monkeypatch.setattr(selfedit, '_run_pre_apply_tests',
                        lambda rows: (True, 'PRE-APPLY GREEN'))


def test_high_risk_requires_detail_and_manifest(fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'risky change', 'reviewer',
        risk_level='high', service_mode='batch')
    assert ok is False
    assert 'fileManifest' in payload['error']
    fields = _high_risk_fields()
    fields['file_manifest'] = ['world/other.py']
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'risky change', 'reviewer',
        **fields)
    assert ok is False
    assert 'does not match the reserved edits' in payload['error']


def test_high_risk_requires_window_only_edits(fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _approve(monkeypatch)
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    ok, payload = selfedit.propose('worker', 'gear.py', 'SPEED = 2',
                                   'SPEED = 3', 'ordinary edit')
    assert ok is True, payload
    _pass_pre_apply(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'risky change', 'reviewer',
        **_high_risk_fields())
    assert ok is False
    assert 'window-only edits' in payload['error']


def test_high_risk_pre_apply_gate_and_stored_fields(fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    _pass_pre_apply(monkeypatch)
    fields = _high_risk_fields()
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'risky change', 'reviewer',
        **fields)
    assert ok is True, payload
    assert payload['preApplyResult'] == 'pass'
    row = selfedit.windows_snapshot()[0]
    assert row['title'] == 'Trading engine durability'
    assert row['riskLevel'] == 'high'
    assert row['serviceMode'] == 'batch'
    assert row['preApplyResult'] == 'pass'
    assert row['fileManifest'] == ['world/gear.py']


def test_high_risk_pre_apply_failure_refuses(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'serve.py').write_text('# kernel\n', encoding='utf-8')
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    monkeypatch.setattr(selfedit, '_run_pre_apply_tests',
                        lambda rows: (False, 'FAIL: test_engine'))
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'risky change', 'reviewer',
        **_high_risk_fields())
    assert ok is False
    assert 'pre-apply repo gate failed' in payload['error']
    assert selfedit.windows_snapshot() == []


def test_trading_mode_requires_safe_state_and_blackouts(fake_repo,
                                                       monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    _pass_pre_apply(monkeypatch)
    fields = _high_risk_fields()
    fields.update(risk_level='critical', service_mode='always_on_trading')
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'trading change', 'reviewer',
        **fields)
    assert ok is False
    assert 'safeStatePlan' in payload['error']
    fields.update(safe_state_plan='Cancel resting orders and flatten exposure',
                  restore_plan='Reconcile positions and resume quoting')
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'trading change', 'reviewer',
        **fields)
    assert ok is False
    assert 'blackoutWindows' in payload['error']
    fields['blackout_windows'] = [
        {'start': '03:00', 'end': '04:00', 'label': 'US market open'}]
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'trading change', 'reviewer',
        **fields)
    assert ok is False
    assert 'blackout window' in payload['error']
    fields['blackout_windows'] = [
        {'start': '05:00', 'end': '06:00', 'label': 'outside session'}]
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'trading change', 'reviewer',
        **fields)
    assert ok is True, payload
    assert payload['durationMin'] == 5


def test_window_only_edit_is_not_applied_on_idle(fake_repo, monkeypatch):
    _, world = fake_repo
    edit_id = _arm_window_only(monkeypatch, world)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    applied = selfedit.apply_pass()
    assert applied == 0
    with sqlite3.connect(serve.DB_PATH) as conn:
        status = conn.execute('SELECT status FROM self_edit_log WHERE id = ?',
                             (edit_id,)).fetchone()[0]
    assert status == 'armed'


def test_fire_time_conflict_defers_and_releases(fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    _pass_pre_apply(monkeypatch)
    monkeypatch.setattr(selfedit, '_notify_player_telegram',
                        lambda subject, body: None)
    notes = []
    monkeypatch.setattr(selfedit, '_notify_agent',
                        lambda agent, text, about='': notes.append(agent))
    fields = _high_risk_fields()
    fields['risk_level'] = 'medium'
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'normal change', 'reviewer',
        **fields)
    assert ok is True, payload
    assert selfedit.resolve_window_escalation(payload['escalationId'], True)
    now = selfedit.time.time()
    # A new approved window appears after the request and overlaps the slot.
    selfedit._window_record('win-blocker', now, 'other', _window_slot(),
                            '03:10', now + 100, now + 400, 5, 'later work',
                            'approved')
    selfedit._update_window(payload['id'], status='approved',
                            start_ts=now - 60, end_ts=now + 600)
    assert selfedit.window_tick() == []
    row = next(r for r in selfedit.windows_snapshot()
               if r['id'] == payload['id'])
    assert row['status'] == 'deferred_conflict'
    assert 'win-blocker' in (row['deferredReason'] or '')
    with sqlite3.connect(serve.DB_PATH) as conn:
        reserved = conn.execute(
            'SELECT reserve_window FROM self_edit_log WHERE id = ?',
            (payload['reservedEdits'][0],)).fetchone()[0]
    assert reserved is None
    assert 'worker' in notes


def test_schedule_tool_forwards_detail_fields(monkeypatch):
    sent = {}

    def fake_http(method, base, path, body, key, timeout=None,
                  extra_headers=None):
        sent.update({'path': path, 'body': body})
        return {'id': 'win-x', 'status': 'approved', 'reservedEdits': ['ed-1'],
                'validator': 'reviewer', 'message': 'window approved'}

    monkeypatch.setattr(serve, '_http_json', fake_http)
    exec_tool = serve._make_web_tools_executor('worker', 'key-w0rker')
    exec_tool('schedule_dormant_apply', {
        'date': '2030-01-01', 'start': '03:00', 'reason': 'ship it',
        'validator': 'reviewer', 'riskLevel': 'high',
        'serviceMode': 'always_on_trading',
        'fileManifest': ['world/gear.py'],
        'rollbackPlan': 'restore bytes', 'rollbackOwner': 'reviewer',
        'successCriteria': 'gate green', 'abortCriteria': 'mismatch',
        'postApplyChecks': 'reconcile', 'onCall': 'reviewer',
        'acknowledgements': ['reviewer'],
        'safeStatePlan': 'flatten', 'restorePlan': 'resume',
        'blackoutWindows': [{'start': '05:00', 'end': '06:00'}],
        'affectedServices': ['trading']})
    assert sent['path'] == '/api/selfedit/schedule'
    body = sent['body']
    assert body['riskLevel'] == 'high'
    assert body['serviceMode'] == 'always_on_trading'
    assert body['fileManifest'] == ['world/gear.py']
    assert body['safeStatePlan'] == 'flatten'
    assert body['blackoutWindows'][0]['start'] == '05:00'


def test_invalid_risk_level_cannot_fall_back_to_low(fake_repo, monkeypatch):
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'typo risk', 'reviewer',
        risk_level='hgih')
    assert ok is False
    assert 'riskLevel' in payload['error']


def test_veto_before_fire_releases_reservations(fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    _pass_pre_apply(monkeypatch)
    monkeypatch.setattr(selfedit, '_notify_agent',
                        lambda agent, text, about='': None)
    monkeypatch.setattr(selfedit, '_notify_player_telegram',
                        lambda subject, body: None)
    fields = _high_risk_fields()
    fields['risk_level'] = 'medium'
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'normal change', 'reviewer',
        **fields)
    assert ok is True, payload
    ok, result = selfedit.veto_window(payload['id'], 'player', 'not now')
    assert ok is True, result
    assert result['status'] == 'vetoed'
    row = next(r for r in selfedit.windows_snapshot()
               if r['id'] == payload['id'])
    assert row['status'] == 'vetoed'
    assert row['vetoedBy'] == 'player'
    assert row['vetoReason'] == 'not now'
    with sqlite3.connect(serve.DB_PATH) as conn:
        reserved = conn.execute(
            'SELECT reserve_window FROM self_edit_log WHERE id = ?',
            (payload['reservedEdits'][0],)).fetchone()[0]
    assert reserved is None


def test_veto_after_fire_rolls_back(fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    _pass_pre_apply(monkeypatch)
    monkeypatch.setattr(selfedit, '_notify_agent',
                        lambda agent, text, about='': None)
    monkeypatch.setattr(selfedit, '_notify_player_telegram',
                        lambda subject, body: None)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    fields = _high_risk_fields()
    fields['risk_level'] = 'medium'
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'normal change', 'reviewer',
        **fields)
    assert ok is True, payload
    assert selfedit.resolve_window_escalation(payload['escalationId'], True)
    now = selfedit.time.time()
    selfedit._update_window(payload['id'], status='approved',
                            start_ts=now - 60, end_ts=now + 600)
    assert payload['id'] in selfedit.window_tick()
    assert selfedit.apply_pass() == 1
    ok, result = selfedit.veto_window(payload['id'], 'player',
                                      'roll it back now')
    assert ok is True, result
    assert result['status'] == 'vetoed'
    row = next(r for r in selfedit.windows_snapshot()
               if r['id'] == payload['id'])
    assert row['status'] == 'vetoed'
    assert row['pushStatus'] == 'failed'
    with sqlite3.connect(serve.DB_PATH) as conn:
        status = conn.execute('SELECT status FROM self_edit_log WHERE id = ?',
                             (payload['reservedEdits'][0],)).fetchone()[0]
    assert status == 'rolled_back'


def test_veto_requires_reason(fake_repo, monkeypatch):
    _patch_roster(monkeypatch)
    ok, payload = selfedit.veto_window('win-x', 'player', '')
    assert ok is False
    assert 'reason' in payload['error']


def test_veto_tool_is_offered_and_forwards(monkeypatch):
    names = [t['function']['name'] for t in serve.AGENT_ASK_TOOLS]
    assert 'veto_change_request' in names
    sent = {}

    def fake_http(method, base, path, body, key, timeout=None,
                  extra_headers=None):
        sent.update({'path': path, 'body': body})
        return {'ok': True, 'status': 'vetoed', 'windowId': 'win-x',
                'reason': 'stop'}

    monkeypatch.setattr(serve, '_http_json', fake_http)
    exec_tool = serve._make_web_tools_executor('theo', 'key-theo')
    out = exec_tool('veto_change_request', {'windowId': 'win-x',
                                            'reason': 'stop'})
    assert sent['path'] == '/api/selfedit/window/veto'
    assert sent['body']['windowId'] == 'win-x'
    assert sent['body']['reason'] == 'stop'
    assert 'vetoed' in out


def test_creation_telegram_has_details_and_veto_link(fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    monkeypatch.setattr(selfedit, '_notify_agent',
                        lambda agent, text, about='': None)
    sent = {}
    monkeypatch.setattr(selfedit, '_notify_player_telegram',
                        lambda subject, body: sent.update(subject=subject,
                                                          body=body))
    fields = _high_risk_fields()
    fields['risk_level'] = 'medium'
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'normal change', 'reviewer',
        notify_created=True, **fields)
    assert ok is True, payload
    assert sent['subject'] == '[AI Think Tank] Change request created'
    assert payload['id'] in sent['body']
    assert 'Veto this change request' in sent['body']
    token = selfedit._window_row(payload['id'])['veto_token']
    assert token in sent['body']


def test_veto_link_rejects_bad_token_and_accepts_real_one(fake_repo, monkeypatch):
    _, world = fake_repo
    _patch_roster(monkeypatch)
    _patch_player_approval(monkeypatch)
    _arm_window_only(monkeypatch, world)
    monkeypatch.setattr(selfedit, '_notify_agent',
                        lambda agent, text, about='': None)
    monkeypatch.setattr(selfedit, '_notify_player_telegram',
                        lambda subject, body: None)
    fields = _high_risk_fields()
    fields['risk_level'] = 'medium'
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'normal change', 'reviewer',
        **fields)
    assert ok is True, payload
    ok, result = selfedit.veto_window_by_link(payload['id'], 'wrong-token')
    assert ok is False and 'invalid or expired' in result['error']
    token = selfedit._window_row(payload['id'])['veto_token']
    ok, result = selfedit.veto_window_by_link(payload['id'], token)
    assert ok is True and result['status'] == 'vetoed'
