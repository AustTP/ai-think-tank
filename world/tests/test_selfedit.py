"""Self-edit pipeline gate tests (Phase 1, world/selfedit.py).

Every test runs against a TEMP repo: serve.ROOT points at a throwaway
world/ directory and serve.DB_PATH at a throwaway database, so the real
world tree and the live think_tank.db are never touched. The reviewer is
the patched serve._jev_quorum_choice_sync -- the same seam selfedit uses.
"""
import sqlite3

import pytest

import selfedit
import serve


@pytest.fixture()
def fake_repo(tmp_path, monkeypatch):
    """A temp repo root with a world/ subdir and an isolated database."""
    world = tmp_path / 'world'
    world.mkdir()
    monkeypatch.setattr(serve, 'ROOT', str(world))
    monkeypatch.setattr(serve, 'DB_PATH', str(tmp_path / 'think_tank.db'))
    serve.init_db()
    return tmp_path, world


def _approve(monkeypatch, decision='approve', confidence=0.9, cost=0.01):
    """Bind the reviewer seam; records every call for assertions."""
    seen = []

    def fake(instructions, question):
        seen.append((instructions, question))
        return decision, confidence, cost

    monkeypatch.setattr(serve, '_jev_quorum_choice_sync', fake)
    return seen


def test_self_edit_is_offered_and_dispatches(monkeypatch):
    names = [t['function']['name'] for t in serve.AGENT_ASK_TOOLS]
    assert 'self_edit' in names and 'self_edit_status' in names
    sent = {}

    def fake_http(method, base, path, body, key, timeout=None):
        sent.update({'method': method, 'path': path, 'body': body,
                     'key': key, 'timeout': timeout})
        return {'status': 'armed', 'id': 'ed-x', 'message': 'edit armed'}

    monkeypatch.setattr(serve, '_http_json', fake_http)
    exec_tool = serve._make_web_tools_executor('worker', 'key-w0rker')
    out = exec_tool('self_edit', {
        'path': 'sim.py', 'find': 'A = 1', 'replace': 'A = 2',
        'summary': 'why'})
    assert sent['path'] == '/api/selfedit/propose'
    assert sent['body']['agentId'] == 'worker'
    assert 'armed' in out
    out = exec_tool('self_edit_status', {})
    assert sent['path'] == '/api/selfedit/status'
    assert sent['body']['limit'] == 15
    assert 'ledger' in out


def test_propose_full_happy_path_arms_candidate(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'gear.py', 'SPEED = 2', 'SPEED = 3', 'engine tuning knob')
    assert ok is True, payload
    assert payload['status'] == 'armed'
    cand = world / '.selfedit_staging' / f"{payload['id']}.cand"
    assert cand.read_text(encoding='utf-8') == 'SPEED = 3\n'
    # arming never mutates the live tree
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 2\n'
    with sqlite3.connect(serve.DB_PATH) as conn:
        row = conn.execute(
            'SELECT status, jev_confidence FROM self_edit_log WHERE id = ?',
            (payload['id'],)).fetchone()
    assert row[0] == 'armed'
    assert row[1] == pytest.approx(0.9)


def test_propose_refuses_protected_files(fake_repo):
    _, world = fake_repo
    (world / 'serve.py').write_text('# kernel\n', encoding='utf-8')
    (world / 'BUSINESS-PROCESSES.md').write_text('rule\n', encoding='utf-8')
    ok, payload = selfedit.propose(
        'worker', 'serve.py', '# kernel', '# x', 'why')
    assert ok is False and 'PROTECTED' in payload['error']
    ok, payload = selfedit.propose(
        'worker', 'world/BUSINESS-PROCESSES.md', 'rule', 'no', 'why')
    assert ok is False and 'PROTECTED' in payload['error']
    assert (world / 'serve.py').read_text(encoding='utf-8') == '# kernel\n'


def test_propose_rejects_escape_and_missing_paths(fake_repo, tmp_path_factory):
    _, world = fake_repo
    outside = tmp_path_factory.mktemp('outside') / 'rogue.py'
    outside.write_text('x = 1\n', encoding='utf-8')
    for bad, needle in (
            ('../evil.py', 'path must be'),
            ('nope.py', 'no such file'),
            (str(outside), 'escapes the repository')):
        ok, payload = selfedit.propose('worker', bad, 'a', 'b', 'why')
        assert ok is False and needle in payload['error'], (bad, payload)


def test_propose_refuses_non_unique_anchor(fake_repo):
    _, world = fake_repo
    (world / 'gear.py').write_text('A = 1\nA = 1\n', encoding='utf-8')
    ok, payload = selfedit.propose(
        'worker', 'gear.py', 'A = 1', 'A = 2', 'why')
    assert ok is False and 'exactly once' in payload['error']


def test_propose_refuses_config_and_hostile_importers(fake_repo):
    # config.py is refused NATURALLY (no code carve-out): its top-level
    # importers are not in TOLERATED_IMPORTERS, and v1 cannot reload them.
    _, world = fake_repo
    (world / 'config.py').write_text('X = 1\n', encoding='utf-8')
    (world / 'content.py').write_text('from config import X\n',
                                      encoding='utf-8')
    ok, payload = selfedit.propose(
        'worker', 'config.py', 'X = 1', 'X = 2', 'why')
    assert ok is False and 'top-level imported by' in payload['error']
    assert 'content.py' in payload['error']
    # Same refusal for an unregistered importer of an ordinary module.
    (world / 'subx.py').write_text('F = 1\n', encoding='utf-8')
    (world / 'user.py').write_text('import subx\n', encoding='utf-8')
    ok, payload = selfedit.propose(
        'worker', 'subx.py', 'F = 1', 'F = 2', 'why')
    assert ok is False and 'user.py' in payload['error']


def test_propose_tolerates_serve_rebind_for_jev(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'jev.py').write_text('FLOOR = 1.0\n', encoding='utf-8')
    (world / 'serve.py').write_text('from jev import FLOOR\n',
                                    encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'jev.py', 'FLOOR = 1.0', 'FLOOR = 2.0',
        'raise the risk floor')
    assert ok is True and payload['status'] == 'armed', payload


def test_review_rejection_leaves_nothing_behind(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    _approve(monkeypatch, decision='reject', confidence=0.8)
    ok, payload = selfedit.propose(
        'worker', 'gear.py', 'SPEED = 2', 'import os  # sabotage', 'why')
    assert ok is False
    assert payload['error'].startswith('the edits reviewer said')
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 2\n'
    assert not (world / '.selfedit_staging').exists()
    with sqlite3.connect(serve.DB_PATH) as conn:
        status = conn.execute(
            'SELECT status FROM self_edit_log').fetchone()[0]
    assert status == 'rejected'


def test_review_transport_failure_fails_closed(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')

    def boom(instructions, question):
        raise RuntimeError('judge transport down')

    monkeypatch.setattr(serve, '_jev_quorum_choice_sync', boom)
    ok, payload = selfedit.propose(
        'worker', 'gear.py', 'SPEED = 2', 'SPEED = 3', 'why')
    assert ok is False
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 2\n'


def test_validator_rejection_on_broken_import(fake_repo, monkeypatch):
    # The candidate compiles (the syntax pre-check is happy) but explodes at
    # import time; only the subprocess quarantine can catch this.
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'gear.py', 'SPEED = 2', 'raise RuntimeError("boom")',
        'why')
    assert ok is False and payload['status'] == 'validator-rejected'
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 2\n'
    # a validator-rejected edit never leaves a persisted candidate behind
    assert not any((world / '.selfedit_staging').glob('*.cand'))


def test_apply_pass_dormant_gate_and_revert_round_trip(
        fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'gear.py', 'SPEED = 2', 'SPEED = 3', 'engine tuning knob')
    edit_id = payload['id']
    target = world / 'gear.py'
    monkeypatch.setattr(serve, '_dormant', lambda: False)
    assert selfedit.apply_pass() == 0
    assert target.read_text(encoding='utf-8') == 'SPEED = 2\n'
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    assert selfedit.apply_pass() == 1
    assert target.read_text(encoding='utf-8') == 'SPEED = 3\n'
    with sqlite3.connect(serve.DB_PATH) as conn:
        status = conn.execute(
            'SELECT status FROM self_edit_log WHERE id = ?',
            (edit_id,)).fetchone()[0]
    assert status == 'applied'
    backup = world / '.selfedit_backups' / edit_id
    assert backup.read_text(encoding='utf-8') == 'SPEED = 2\n'
    ok, payload = selfedit.revert(edit_id, by='player')
    assert ok is True, payload
    assert target.read_text(encoding='utf-8') == 'SPEED = 2\n'
    with sqlite3.connect(serve.DB_PATH) as conn:
        status = conn.execute(
            'SELECT status FROM self_edit_log WHERE id = ?',
            (edit_id,)).fetchone()[0]
    assert status == 'reverted'


def test_apply_conflict_when_file_drifts_after_arming(
        fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    _approve(monkeypatch)
    _, payload = selfedit.propose(
        'worker', 'gear.py', 'SPEED = 2', 'SPEED = 3', 'why')
    (world / 'gear.py').write_text('SPEED = 9\n', encoding='utf-8')
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    assert selfedit.apply_pass() == 0
    with sqlite3.connect(serve.DB_PATH) as conn:
        status = conn.execute(
            'SELECT status FROM self_edit_log WHERE id = ?',
            (payload['id'],)).fetchone()[0]
    assert status == 'conflict'
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 9\n'


def test_status_snapshot_shape_and_bounds(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    _approve(monkeypatch)
    selfedit.propose('worker', 'gear.py', 'SPEED = 2', 'SPEED = 3', 'why')
    rows = selfedit.status_snapshot(limit=25)
    assert len(rows) == 1
    row = rows[0]
    assert row['agent'] == 'worker'
    assert row['path'] == 'world/gear.py'
    assert row['status'] in ('armed', 'applied', 'conflict', 'reverted')
    assert selfedit.status_snapshot(limit=0) == []


# ---------------------------------------------------------------------------
# One-shot dormancy windows (the change-request process for applying edits).
# ---------------------------------------------------------------------------

def _window_slot(days_ahead=1):
    import datetime
    day = datetime.date.today() + datetime.timedelta(days=days_ahead)
    return day.strftime('%Y-%m-%d')


def _patch_player_approval(monkeypatch):
    # Stand in for notify.create_escalation so no email/telegram machinery
    # runs; the test only asserts the linkage that selfedit persists.
    monkeypatch.setattr(
        serve, 'create_escalation',
        lambda kind, question, on_approve_note='', what_checked='',
        look_first='', **_kw: 'esc-' + (on_approve_note[:0] or 'x12345'))


def test_schedule_window_happy_path_pends_escalation(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nightly maintenance')
    assert ok is True, payload
    assert payload['status'] == 'pending'
    assert payload['escalationId'].startswith('esc-x')
    rows = selfedit.windows_snapshot()
    assert len(rows) == 1
    row = rows[0]
    assert row['agent'] == 'worker'
    assert row['date'] == _window_slot()
    assert row['start'] == '03:10'
    assert row['durationMin'] == 15
    assert row['status'] == 'pending'
    assert row['escalationId'] == payload['escalationId']


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


def test_schedule_window_time_and_range_validation(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.request_window('worker', '2020-01-01', '03:00',
                                          15, 'why')
    assert ok is False and 'at least 5 minutes' in payload['error']
    ok, payload = selfedit.request_window('worker', 'bad-day', '03:00',
                                          15, 'why')
    assert ok is False and 'YYYY-MM-DD' in payload['error']
    ok, payload = selfedit.request_window('worker', _window_slot(), '25:99',
                                          15, 'why')
    assert ok is False and 'HH:MM' in payload['error']
    ok, payload = selfedit.request_window('worker', _window_slot(31),
                                          '03:00', 15, 'why')
    assert ok is False and '30 days' in payload['error']
    ok, payload = selfedit.request_window('worker', _window_slot(), '03:00',
                                          None, '')
    assert ok is False and 'reason is required' in payload['error']
    assert selfedit.windows_snapshot(limit=10) == []


def test_schedule_window_supervisor_refusal(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch, decision='reject', confidence=0.7)
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'vague pause please')
    assert ok is False and payload['status'] == 'refused'
    assert 'supervisor review said' in payload['error']
    assert selfedit.windows_snapshot(limit=10) == []


def test_resolve_and_cancel_lifecycle(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'nightly maintenance')
    window_id, esc_id = payload['id'], payload['escalationId']
    assert selfedit.resolve_window_escalation(esc_id, True) is True
    assert selfedit.resolve_window_escalation(esc_id, True) is False
    assert selfedit.windows_snapshot()[0]['status'] == 'approved'
    ok, payload = selfedit.cancel_window(window_id, by='player')
    assert ok is True and payload['status'] == 'cancelled'
    ok, payload = selfedit.cancel_window('win-nope', by='player')
    assert ok is False and 'unknown window id' in payload['error']


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
    assert ok is False and 'fired' in payload['error']


def test_dormancy_tools_surface_and_executor_forwarding(monkeypatch):
    names = [t['function']['name'] for t in serve.AGENT_ASK_TOOLS]
    assert 'schedule_dormant_apply' in names
    sent = []

    def fake_http(method, base, path, body, key, timeout=None):
        sent.append({'method': method, 'path': path, 'body': body,
                     'key': key, 'timeout': timeout})
        return {'id': 'win-y', 'status': 'pending', 'escalationId': 'esc-z',
                'message': 'window requested.'}

    monkeypatch.setattr(serve, '_http_json', fake_http)
    exec_tool = serve._make_web_tools_executor('worker', 'key-w0rker')
    out = exec_tool('schedule_dormant_apply', {
        'date': _window_slot(), 'start': '03:10', 'durationMin': 25,
        'reason': 'nightly maintenance'})
    assert sent[0]['path'] == '/api/selfedit/schedule'
    assert sent[0]['body']['agentId'] == 'worker'
    assert sent[0]['body']['durationMin'] == 25
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
        'reason': 'nightly maintenance'})
    assert 'Engineering ritual block' in out