"""Self-edit pipeline gate tests (Phase 1, world/selfedit.py).

Every test runs against a TEMP repo: serve.ROOT points at a throwaway
world/ directory and serve.DB_PATH at a throwaway database, so the real
world tree and the live think_tank.db are never touched. The reviewer is
the patched serve._jev_quorum_choice_sync -- the same seam selfedit uses.
"""
import sys
import os
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selfedit  # noqa: E402
import serve  # noqa: E402


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

    def fake_http(method, base, path, body, key, timeout=None,
                  extra_headers=None):
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


def test_propose_refuses_config_and_hostile_importers(fake_repo,
                                                      monkeypatch):
    # Fresh state of the binding contract (Phase 2, operator decision
    # 2026-10-18): config.py is now EDITABLE -- its four top-level importers
    # are tolerated because the apply chain reloads sublow/content/sim and
    # refreshes serve's import block. Unregistered importers of an ordinary
    # module are still refused.
    _, world = fake_repo
    (world / 'config.py').write_text('X = 1\n', encoding='utf-8')
    (world / 'content.py').write_text('from config import X\n',
                                      encoding='utf-8')
    (world / 'sim.py').write_text('from config import bind_policy\n',
                                  encoding='utf-8')
    (world / 'sublow.py').write_text('from config import bind_policy\n',
                                     encoding='utf-8')
    (world / 'serve.py').write_text('from config import X\n',
                                    encoding='utf-8')
    assert selfedit.TOLERATED_IMPORTERS['config'] == {
        'content.py', 'sim.py', 'sublow.py', 'serve.py'}
    assert selfedit.RELOAD_IMPORTERS_AFTER['config'] == (
        'sublow', 'content', 'sim')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'config.py', 'X = 1', 'X = 2', 'why')
    assert ok is True and payload['status'] == 'armed', payload
    # Same refusal stays for an unregistered importer of an ordinary module.
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
    # jev.py is a Tier B oversight file, so a passing quorum+validator run
    # parks the edit at pending_signature instead of arming directly.
    _patch_player_approval(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'jev.py', 'FLOOR = 1.0', 'FLOOR = 2.0',
        'raise the risk floor')
    assert ok is True and payload['status'] == 'pending_signature', payload
    assert selfedit.resolve_oversight_signature('esc-x12345', True)
    cand = world / '.selfedit_staging' / f"{payload['id']}.cand"
    assert cand.read_text(encoding='utf-8') == 'FLOOR = 2.0\n'


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
    _fake_test_gate(monkeypatch, [(True, 'ALL TEST FILES PASSED')])
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


def _patch_roster(monkeypatch):
    # Deterministic roster for the two-agent validator check (the seed
    # roster is env-dependent in the real install).
    monkeypatch.setattr(serve, 'get_state_from_db',
                        lambda: {'agents': {'worker': {}, 'reviewer': {}}})


def _arm_edit(monkeypatch, world, agent='worker', path='gear.py'):
    """Arm one reviewed edit for `agent` so a change request has something
    to reserve."""
    (world / path).write_text('SPEED = 2\n', encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(agent, path, 'SPEED = 2', 'SPEED = 3',
                                   'engine tuning knob')
    assert ok is True, payload
    return payload['id']


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


# ---------------------------------------------------------------------------
# CHANGE-WINDOW semantics: post-apply tests, in-window remediation, and the
# automatic end-of-window rollback (operator decision, 2026-10-18).
# ---------------------------------------------------------------------------

_MARK = '# marker module\nMARKER_VALUE = 1\n'


def _fake_test_gate(monkeypatch, sequence):
    """Replace _run_post_apply_tests with a scripted source: sequence is a
    list of (ok, tail) consumed per run."""
    runs = iter(sequence)
    monkeypatch.setattr(
        selfedit, '_run_post_apply_tests',
        lambda: next(runs))


def _notify_log(monkeypatch):
    notes = []
    monkeypatch.setattr(
        selfedit, '_notify_agent',
        lambda agent_id, text, about='': notes.append((agent_id, text)))
    return notes


def _fire_window(start_ts, duration_min=15):
    selfedit._window_record('win-live', selfedit.time.time(), 'worker',
                            _window_slot(), '03:10', start_ts,
                            start_ts + duration_min * 60, duration_min,
                            'reason', 'approved')
    fired = selfedit.window_tick()
    assert 'win-live' in fired
    return 'win-live'


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


def test_oversight_signature_flow(fake_repo, monkeypatch):
    # Tier B (audit 2026-10-18): oversight-enforcement files pass quorum and
    # validation but arm only after the player's emailed signature.
    _, world = fake_repo
    (world / 'jev.py').write_text('FLOOR = 1.0\n', encoding='utf-8')
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)

    ok, payload = selfedit.propose(
        'worker', 'jev.py', 'FLOOR = 1.0', 'FLOOR = 2.0', 'raise the floor')
    assert ok is True, payload
    assert payload['status'] == 'pending_signature'
    assert payload['escalationId'] == 'esc-x12345'
    cand = world / '.selfedit_staging' / f"{payload['id']}.cand"
    assert cand.read_text(encoding='utf-8') == 'FLOOR = 2.0\n'
    assert (world / 'jev.py').read_text(encoding='utf-8') == 'FLOOR = 1.0\n'
    with sqlite3.connect(serve.DB_PATH) as conn:
        row = conn.execute(
            'SELECT status, signature_esc FROM self_edit_log WHERE id = ?',
            (payload['id'],)).fetchone()
    assert row[0] == 'pending_signature' and row[1] == 'esc-x12345'
    # unknown ids and stale ids never mutate anything
    assert not selfedit.resolve_oversight_signature('esc-missing', True)
    assert selfedit.resolve_oversight_signature('esc-x12345', True)
    with sqlite3.connect(serve.DB_PATH) as conn:
        status = conn.execute(
            'SELECT status FROM self_edit_log WHERE id = ?',
            (payload['id'],)).fetchone()[0]
    assert status == 'armed'
    assert not selfedit.resolve_oversight_signature('esc-x12345', True)

    # a second signature request can be denied outright (the first edit was
    # armed, not applied, so the live anchor is still the original line)
    ok2, payload2 = selfedit.propose(
        'worker', 'jev.py', 'FLOOR = 1.0', 'FLOOR = 3.0', 'lower again')
    assert ok2 is True and payload2['status'] == 'pending_signature'
    assert selfedit.resolve_oversight_signature('esc-x12345', False)
    with sqlite3.connect(serve.DB_PATH) as conn:
        row2 = conn.execute(
            'SELECT status, note FROM self_edit_log WHERE id = ?',
            (payload2['id'],)).fetchone()
    assert row2[0] == 'refused'
    assert 'player signature denied' in row2[1]
    assert (world / 'jev.py').read_text(encoding='utf-8') == 'FLOOR = 1.0\n'


def test_read_module_surface(fake_repo):
    # Tier A/B stays closed for reads too: the read surface is deliberately
    # smaller than the write surface.
    import hashlib
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    (world / 'serve.py').write_text('# kernel\n', encoding='utf-8')
    (world / 'jev.py').write_text('FLOOR = 1.0\n', encoding='utf-8')
    (world / 'conftest.py').write_text('# gate\n', encoding='utf-8')
    (world / 'BUSINESS-PROCESSES.md').write_text('rule\n', encoding='utf-8')

    ok, listing = selfedit.read_module('')
    assert ok is True
    paths = [m['path'] for m in listing['modules']]
    assert paths == ['world/gear.py']
    assert listing['modules'][0]['bytes'] == len('SPEED = 2\n')

    ok, head = selfedit.read_module('gear.py')
    assert ok is True, head
    assert head['path'] == 'world/gear.py'
    assert head['text'] == 'SPEED = 2\n'
    assert head['truncated'] is False
    assert head['totalBytes'] == len('SPEED = 2\n')
    assert head['sha256'] == hashlib.sha256(b'SPEED = 2\n').hexdigest()

    for guarded in (('serve.py', 'outside the readable'),
                    ('world/BUSINESS-PROCESSES.md', 'outside the readable'),
                    ('jev.py', 'outside the readable'),
                    ('conftest.py', 'outside the readable')):
        ok, payload = selfedit.read_module(guarded[0])
        assert ok is False and guarded[1] in payload['error'], (
            guarded, payload)


def test_config_reload_chain_and_serve_refresh(monkeypatch, fake_repo):
    # Phase 2 (operator decision 2026-10-18): after a config.py apply, the
    # chain reloads sublow/content/sim whose `from config import` bindings
    # re-execute, then refreshes serve's ten config-import names. The reload
    # itself never runs except while dormant; this test spies on it.
    _, world = fake_repo
    names = ('ALLOW_WATCHLIST_WRITE', 'BROWSE_BLOCK_CATEGORIES',
             'PLAIN_WRITING_BAN_LIST', 'RESEARCH_WATCHLIST', 'ROOT',
             'bind_policy', 'get_grants_config', 'get_operator_policy',
             'note_watchlist_topic', 'reload_policy_config')
    reloaded = []

    def fake_reload(mod):
        reloaded.append(mod.__name__)
        return mod

    monkeypatch.setattr('importlib.reload', fake_reload)
    config_stub = type('StubModule', (), {'__name__': 'config'})()
    for name in names:
        setattr(config_stub, name, 'sentinel-' + name)
    for name in ('sublow', 'content', 'sim'):
        monkeypatch.setitem(
            sys.modules, name, type('StubModule', (), {'__name__': name})())
    monkeypatch.setitem(sys.modules, 'config', config_stub)
    saved = {name: getattr(serve, name) for name in names}
    try:
        (world / 'config.py').write_text('X = 1\n', encoding='utf-8')
        selfedit._reload(str(world / 'config.py'))
        assert reloaded == ['config', 'sublow', 'content', 'sim']
        for name in names:
            assert getattr(serve, name) == 'sentinel-' + name
    finally:
        # Plain setattr, not monkeypatch: monkeypatch.setattr would register
        # the sentinel value as its own undo target and re-leak it into
        # later tests in the same process.
        for name in names:
            setattr(serve, name, saved[name])


def test_propose_rejects_required_guards(fake_repo):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    ok, payload = selfedit.propose('worker', 'gear.py', '', 'A = 2', 'why')
    assert ok is False and 'find text is required' in payload['error']
    ok, payload = selfedit.propose('worker', 'gear.py', 'SPEED = 2',
                                   'x' * (selfedit.MAX_REPLACE_LEN + 1), 'why')
    assert ok is False and 'replacement too long' in payload['error']
    ok, payload = selfedit.propose('worker', 'gear.py', 'SPEED = 2',
                                   'SPEED = 2', 'why')
    assert ok is False and 'changes nothing' in payload['error']
    ok, payload = selfedit.propose('worker', 'gear.py', 'SPEED = 2',
                                   'SPEED = 3', '')
    assert ok is False and 'a summary is required' in payload['error']
    ok, payload = selfedit.propose('worker', '', 'A', 'B', 'why')
    assert ok is False and payload['error'] == 'path is required'


def test_propose_rejects_unreadable_too_large_and_noncompiling(fake_repo):
    _, world = fake_repo
    (world / 'bin.py').write_bytes(b'\x80\x81\x82')
    ok, payload = selfedit.propose('worker', 'bin.py', 'SPEED = 2',
                                   'SPEED = 3', 'why')
    assert ok is False and 'cannot read target' in payload['error']
    (world / 'big.py').write_text(
        'SPEED = 2\n' + 'x' * (4 * 1024 * 1024), encoding='utf-8')
    ok, payload = selfedit.propose('worker', 'big.py', 'SPEED = 2',
                                   'SPEED = 3', 'why')
    assert ok is False and 'target file too large' in payload['error']
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    ok, payload = selfedit.propose('worker', 'gear.py', 'SPEED = 2',
                                   'SPEED = (', 'why')
    assert ok is False and 'candidate does not compile' in payload['error']


def test_importers_scan_skips_unreadable_entries_and_blank_lines(fake_repo):
    _, world = fake_repo
    (world / 'junkd.py').mkdir()
    (world / 'notes.py').write_text(
        '# comment\n\nimport blip\n', encoding='utf-8')
    (world / 'blip.py').write_text('SPEED = 2\n', encoding='utf-8')
    assert selfedit._importers_of('blip') == ['notes.py']


def test_update_without_fields_is_a_noop():
    selfedit._update('ed-nonexistent')


def test_read_module_listing_skips_nonfiles_and_unknown_path_errors(fake_repo):
    _, world = fake_repo
    (world / 'dirdemo.py').mkdir()
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    ok, payload = selfedit.read_module('')
    assert ok is True
    assert [m['path'] for m in payload['modules']] == ['world/gear.py']
    ok, payload = selfedit.read_module('world/ghost.py')
    assert ok is False and payload['error'] == 'no such file'


def test_read_module_binary_and_truncation(fake_repo):
    _, world = fake_repo
    (world / 'bin2.py').write_bytes(b'\x80\x81\x82')
    ok, payload = selfedit.read_module('bin2.py')
    assert ok is False and 'cannot read target' in payload['error']
    (world / 'longhead.py').write_text(
        'x' * (selfedit.READ_TEXT_CAP + 1), encoding='utf-8')
    ok, payload = selfedit.read_module('longhead.py')
    assert ok is True
    assert len(payload['text']) == selfedit.READ_TEXT_CAP
    assert payload['truncated'] is True


def _book_and_apply(monkeypatch, world, post_tests=(True, 'ALL GREEN'),
                    arm_twice=False):
    """Arm an edit (twice when arm_twice, so the second conflicts by sha),
    book+approve+fire a change window, then apply it with the given
    post-apply test result. Returns (window_id, first_edit_id)."""
    now = selfedit.time.time()
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    edit_id = _arm_edit(monkeypatch, world)
    if arm_twice:
        _arm_edit(monkeypatch, world)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'ship the tuning change',
        'reviewer')
    assert ok is True, payload
    assert selfedit.resolve_window_escalation(payload['escalationId'], True)
    selfedit._update_window(payload['id'], status='approved',
                            start_ts=now - 60, end_ts=now + 600)
    assert payload['id'] in selfedit.window_tick()
    monkeypatch.setattr(selfedit, '_run_post_apply_tests',
                        lambda: post_tests)
    selfedit.apply_pass()
    return payload['id'], edit_id


def _open_remediation(monkeypatch, world):
    """Book and apply with a FAILING post-apply test run so the remediation
    session opens."""
    window_id, edit_id = _book_and_apply(
        monkeypatch, world, post_tests=(False, 'REPO TESTS FAILED'))
    assert selfedit._get_row(edit_id)[1] == 'failed'
    return window_id, edit_id


def test_validator_rejects_compileall_and_flake8_failures(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')

    def fake_run(args, **kw):
        name = kw.get('step') or (args[2] if len(args) > 2 else '?')
        if name == 'compileall':
            return type('D', (), {'returncode': 1, 'stdout': 'compile boom\n',
                                  'stderr': ''})()
        if name == 'flake8':
            return type('D', (), {'returncode': 1, 'stdout': 'flake boom\n',
                                  'stderr': ''})()
        return type('D', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()

    monkeypatch.setattr(selfedit.subprocess, 'run', fake_run)
    ok, payload = selfedit._validate('ed-x', str(world / 'gear.py'),
                                     'SPEED = 3\n')
    assert ok is False and 'quarantine validation failed' in payload['error']
    assert 'compile boom' in payload['detail']
    assert 'flake boom' in payload['detail']


def test_validator_handles_timeout_and_generic_failure(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')

    def timeout(*args, **kw):
        raise selfedit.subprocess.TimeoutExpired('cmd', 1)

    monkeypatch.setattr(selfedit.subprocess, 'run', timeout)
    ok, payload = selfedit._validate('ed-y', str(world / 'gear.py'),
                                     'SPEED = 3\n')
    assert ok is False and 'timed out' in payload['detail']

    def broken(*args, **kw):
        raise RuntimeError('cannot spawn validator')

    monkeypatch.setattr(selfedit.subprocess, 'run', broken)
    ok, payload = selfedit._validate('ed-z', str(world / 'gear.py'),
                                     'SPEED = 3\n')
    assert ok is False and 'validator failure' in payload['detail']


def test_apply_pass_without_armed_rows_is_a_noop(fake_repo, monkeypatch):
    fake_repo
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    assert selfedit.apply_pass() == 0


def test_apply_pass_survives_an_edit_that_raises(fake_repo, monkeypatch):
    _, world = fake_repo
    edit_id = _arm_edit(monkeypatch, world)
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)

    def boom(*args, **kw):
        raise RuntimeError('apply blew up')

    monkeypatch.setattr(selfedit, '_apply_one', boom)
    assert selfedit.apply_pass() == 0
    assert selfedit._get_row(edit_id)[1] == 'failed'


def test_apply_pass_with_all_reserved_edits_failed_marks_window_unsuccessful(
        fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    _arm_edit(monkeypatch, world)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'ship the tuning change',
        'reviewer')
    assert ok is True, payload
    assert selfedit.resolve_window_escalation(payload['escalationId'], True)
    now = selfedit.time.time()
    selfedit._update_window(payload['id'], status='approved',
                            start_ts=now - 60, end_ts=now + 600)
    assert payload['id'] in selfedit.window_tick()
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)

    def boom(*args, **kw):
        raise RuntimeError('reserved edit blew up')

    monkeypatch.setattr(selfedit, '_apply_one', boom)
    telegrams = []
    monkeypatch.setattr(selfedit, '_telegram_change_request',
                        lambda w, c, n: telegrams.append((w, c)))
    assert selfedit.apply_pass() == 0
    assert selfedit._window_field(payload['id'], 'push_status') == 'failed'
    assert selfedit._window_field(payload['id'], 'push_category') \
        == 'unsuccessful'
    assert telegrams and telegrams[0][1] == 'unsuccessful'


def test_post_apply_tests_pass_fail_timeout_and_start_failure(
        fake_repo, monkeypatch):
    fake_repo
    cases = [
        ([sys.executable, '-c', 'print("ALL GREEN")'], True),
        ([sys.executable, '-c', 'import sys; sys.exit(3)'], False),
    ]
    for cmd, expect_ok in cases:
        monkeypatch.setattr(selfedit, 'POST_APPLY_TEST_CMD', cmd)
        ok, _tail = selfedit._run_post_apply_tests()
        assert ok is expect_ok
    monkeypatch.setattr(selfedit, 'POST_APPLY_TEST_CMD',
                        [sys.executable, '-c', 'print("ALL GREEN")'])

    def timeout(*args, **kw):
        raise selfedit.subprocess.TimeoutExpired('cmd', 1)

    monkeypatch.setattr(selfedit.subprocess, 'run', timeout)
    ok, tail = selfedit._run_post_apply_tests()
    assert ok is False and 'timed out' in tail

    def broken(*args, **kw):
        raise RuntimeError('cannot spawn gate')

    monkeypatch.setattr(selfedit.subprocess, 'run', broken)
    ok, tail = selfedit._run_post_apply_tests()
    assert ok is False and 'could not start' in tail


def test_rollback_skips_unknown_ids(fake_repo):
    fake_repo
    assert selfedit._rollback_rows(['ed-nosuch'], 'reason', 'no-window') == []


def test_rollback_failure_marks_row_failed(fake_repo, monkeypatch):
    _, world = fake_repo
    edit_id = _arm_edit(monkeypatch, world)
    backups = world / '.selfedit_backups'
    backups.mkdir(exist_ok=True)
    (backups / edit_id).write_text('SPEED = 2\n', encoding='utf-8')
    (backups / f'{edit_id}.cand').write_text('SPEED = 3\n', encoding='utf-8')
    monkeypatch.setattr(selfedit, '_notify_agent', lambda *a, **k: None)

    def boom_reload(*args, **kw):
        raise RuntimeError('reload broke')

    monkeypatch.setattr(selfedit, '_reload', boom_reload)
    saved = selfedit._rollback_rows([edit_id], 'reason', 'no-window')
    assert saved and saved[0].endswith('gear.py')
    assert selfedit._get_row(edit_id)[1] == 'failed'


def test_remediation_recheck_reports_failing_tests(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id = _open_remediation(monkeypatch, world)
    monkeypatch.setattr(selfedit, '_run_post_apply_tests',
                        lambda: (False, 'STILL FAILING'))
    ok, payload = selfedit.run_remediation_checks('worker')
    assert ok is True and payload['tests'] == 'fail'
    assert 'STILL FAILING' in payload['tail']


def test_apply_marks_conflict_when_target_missing(fake_repo, monkeypatch):
    _, world = fake_repo
    edit_id = _arm_edit(monkeypatch, world)
    (world / 'gear.py').unlink()
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    assert selfedit.apply_pass() == 0
    assert selfedit._get_row(edit_id)[1] == 'conflict'


def test_submit_fix_guards_and_splices(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id = _open_remediation(monkeypatch, world)
    ok, payload = selfedit.submit_remediation_fix('worker', 'gear.py', '',
                                                  'B', 'why')
    assert ok is False and 'find text is required' in payload['error']
    ok, payload = selfedit.submit_remediation_fix(
        'worker', 'gear.py', 'SPEED = 3',
        'x' * (selfedit.MAX_REPLACE_LEN + 1), 'why')
    assert ok is False and 'replacement too long' in payload['error']
    ok, payload = selfedit.submit_remediation_fix('worker', 'gear.py',
                                                  'SPEED = 3', 'SPEED = 3',
                                                  'why')
    assert ok is False and 'changes nothing' in payload['error']
    ok, payload = selfedit.submit_remediation_fix('worker', 'gear.py',
                                                  'SPEED = 3', 'SPEED = 4', '')
    assert ok is False and 'a summary is required' in payload['error']
    ok, payload = selfedit.submit_remediation_fix('worker', 'ghost.py',
                                                  'A', 'B', 'why')
    assert ok is False and payload['error'] == 'no such file'
    (world / 'serve.py').write_text('SPEED = 2\n', encoding='utf-8')
    ok, payload = selfedit.submit_remediation_fix('worker', 'serve.py',
                                                  'A', 'B', 'why')
    assert ok is False and 'PROTECTED' in payload['error']
    (world / 'bin3.py').write_bytes(b'\x80\x81')
    ok, payload = selfedit.submit_remediation_fix('worker', 'bin3.py',
                                                  'A', 'B', 'why')
    assert ok is False and 'cannot read target' in payload['error']
    (world / 'twin.py').write_text('A = 1\nA = 2\n', encoding='utf-8')
    ok, payload = selfedit.submit_remediation_fix('worker', 'twin.py',
                                                  'A =', 'B =', 'why')
    assert ok is False and 'appears 2 times' in payload['error']
    ok, payload = selfedit.submit_remediation_fix('worker', 'gear.py',
                                                  'NOMATCH', 'B', 'why')
    assert ok is False and 'appears 0 times' in payload['error']
    ok, payload = selfedit.submit_remediation_fix('worker', 'gear.py',
                                                  'SPEED = 3', 'SPEED = (',
                                                  'why')
    assert ok is False and 'fix does not compile' in payload['error']
    ok, payload = selfedit.submit_remediation_fix('worker', 'gear.py',
                                                  'SPEED = 3', 'SPEED = 4',
                                                  'why')
    assert ok is True and payload['status'] == 'applied'
    assert payload['window'] == window_id


def test_revert_one_from_backup_cases(fake_repo, monkeypatch):
    _, world = fake_repo
    assert selfedit._revert_one_from_backup('ed-nosuch') is False
    edit_id = _arm_edit(monkeypatch, world)
    assert selfedit._revert_one_from_backup(edit_id) is False
    backups = world / '.selfedit_backups'
    backups.mkdir(exist_ok=True)
    (backups / edit_id).write_text('SPEED = 2\n', encoding='utf-8')
    monkeypatch.setattr(selfedit, '_notify_agent', lambda *a, **k: None)
    assert selfedit._revert_one_from_backup(edit_id) is True
    assert (world / 'gear.py').read_text(encoding='utf-8') == 'SPEED = 2\n'

    def boom_reload(*args, **kw):
        raise RuntimeError('reload broke')

    monkeypatch.setattr(selfedit, '_reload', boom_reload)
    assert selfedit._revert_one_from_backup(edit_id) is True


def test_reload_of_jev_refreshes_serve_exports(fake_repo):
    fake_repo
    before = serve.JEV_SAFETY_CONFIDENCE
    selfedit._reload('nonexistent/jev.py')
    selfedit._refresh_serve_jev_exports()
    assert serve.JEV_SAFETY_CONFIDENCE == before


def test_revert_guards(fake_repo, monkeypatch):
    _, world = fake_repo
    ok, payload = selfedit.revert('ed-unknown-id')
    assert ok is False and payload['error'] == 'unknown edit id'
    edit_id = _arm_edit(monkeypatch, world)
    ok, payload = selfedit.revert(edit_id)
    assert ok is False and 'not applied' in payload['error']
    selfedit._update(edit_id, status='applied')
    ok, payload = selfedit.revert(edit_id)
    assert ok is False and 'backup missing' in payload['error']


def test_update_window_without_fields_is_a_noop():
    selfedit._update_window('win-nonexistent')


def test_notify_agent_survives_state_read_failure(fake_repo, monkeypatch):
    fake_repo

    def boom():
        raise RuntimeError('state read failed')

    monkeypatch.setattr(serve, 'get_state_from_db', boom)
    assert selfedit._notify_agent('worker', 'hello') is False


def test_telegram_push_survives_sender_failure(fake_repo, monkeypatch):
    fake_repo
    import notify

    def boom(*args, **kw):
        raise RuntimeError('telegram unreachable')

    monkeypatch.setattr(notify, 'send_player_telegram_sync', boom)
    assert selfedit._notify_player_telegram('subj', 'body') is False

    def log_boom(*args, **kw):
        raise RuntimeError('log write failed')

    monkeypatch.setattr(serve, 'log_action', log_boom)
    assert selfedit._notify_player_telegram('subj', 'body') is False


def test_git_run_real_timeout_and_start_failure(fake_repo, monkeypatch):
    _, world = fake_repo
    rc, out, err = selfedit._git_run(['status', '--porcelain'])
    assert rc in (0, 128)

    def timeout(*args, **kw):
        raise selfedit.subprocess.TimeoutExpired('cmd', 1)

    monkeypatch.setattr(selfedit.subprocess, 'run', timeout)
    rc, out, err = selfedit._git_run(['status'])
    assert rc == 124 and 'timed out' in err

    def broken(*args, **kw):
        raise RuntimeError('cannot spawn git')

    monkeypatch.setattr(selfedit.subprocess, 'run', broken)
    rc, out, err = selfedit._git_run(['status'])
    assert rc == 1 and 'could not start' in err


def test_standing_conflicts_skip_bad_declarations(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    _arm_edit(monkeypatch, world)
    roster_state = serve.get_state_from_db() or {}
    roster_state['standingTasks'] = [
        'not-a-dict',
        {'taskId': 'st-bad', 'daysOfWeek': 5},
        {'taskId': 'st-harmless', 'name': 'Harmless block', 'mode': 'window',
         'startTime': '10:00', 'endTime': '11:00'}]
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: roster_state)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'ship the tuning change',
        'reviewer')
    assert ok is True, payload


def test_request_window_rejects_bad_start_and_coerces_duration(
        fake_repo, monkeypatch):
    fake_repo
    _approve(monkeypatch)
    ok, payload = selfedit.request_window('worker', '1000-01-01', '03:10',
                                          15, 'why', 'reviewer')
    assert ok is False and 'invalid window start' in payload['error']
    ok, payload = selfedit.request_window('worker', _window_slot(), '03:10',
                                          'garbage', 'why', 'reviewer')
    assert ok is False and 'invalid window start' not in payload['error']


def test_resolve_window_escalation_unknown_and_denial(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    edit_id = _arm_edit(monkeypatch, world)
    assert selfedit.resolve_window_escalation('esc-nosuch', True) is False
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'ship the tuning change',
        'reviewer')
    assert ok is True, payload
    assert selfedit.resolve_window_escalation(payload['escalationId'],
                                              False) is True
    assert selfedit._window_field(payload['id'], 'status') == 'refused'
    assert selfedit._get_row(edit_id)[1] == 'armed'


def test_window_tick_keeps_holding_slot_open(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, edit_id = _book_and_apply(monkeypatch, world)
    assert selfedit._window_field(window_id, 'status') == 'fired'
    assert selfedit.window_tick() == []


def test_hold_for_validator_ignores_not_fired_window(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    _arm_edit(monkeypatch, world)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'ship the tuning change',
        'reviewer')
    assert ok is True, payload
    assert selfedit.resolve_window_escalation(payload['escalationId'], True)
    selfedit._hold_for_validator(payload['id'], remediation_used=False,
                                 tail='t')
    assert selfedit._window_field(payload['id'], 'push_status') is None


def test_remediation_recovery_holds_for_validator_with_note(
        fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, edit_id = _open_remediation(monkeypatch, world)
    monkeypatch.setattr(selfedit, '_run_post_apply_tests',
                        lambda: (True, 'GREEN NOW'))
    ok, payload = selfedit.run_remediation_checks('worker')
    assert ok is True and payload['tests'] == 'pass'
    assert selfedit._get_row(edit_id)[1] == 'applied'
    assert selfedit._window_field(window_id, 'push_status') \
        == 'awaiting_validator'


def _push_git_plan(monkeypatch, plan):
    def fake(args):
        step = args[0] if args else '?'
        if step == 'rev-parse':
            return 0, 'abcdef123456\n', ''
        return plan.get(step, (0, '', ''))

    monkeypatch.setattr(selfedit, '_git_run', fake)


def test_push_window_unknown_window_and_status_failure(fake_repo, monkeypatch):
    _, world = fake_repo
    ok, payload = selfedit._push_window('win-nosuch', 'successful', '')
    assert ok is True and payload['status'] == 'unsuccessful'
    window_id, _edit_id = _book_and_apply(monkeypatch, world)
    _push_git_plan(monkeypatch, {'status': (1, '', 'status broke')})
    ok, payload = selfedit._push_window(window_id, 'successful', '')
    assert ok is True and payload['status'] == 'unsuccessful'
    assert selfedit._window_field(window_id, 'push_status') == 'failed'
    assert 'git status failed' in payload['message']


def test_push_window_ignores_noise_and_reads_renames(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id = _book_and_apply(monkeypatch, world)
    status_out = (' M world/gear.py\n'
                  '\n'
                  '?? world/notes.py\n'
                  ' R  world/gear-old.py -> world/gear.py\n')
    _push_git_plan(monkeypatch, {'status': (0, status_out, '')})
    ok, payload = selfedit._push_window(window_id, 'successful', '')
    assert ok is True and payload['status'] == 'successful'
    assert payload['sha'] == 'abcdef123456'


def test_validate_window_unknown_and_not_holding(fake_repo, monkeypatch):
    _, world = fake_repo
    _approve(monkeypatch)
    _patch_player_approval(monkeypatch)
    _patch_roster(monkeypatch)
    ok, payload = selfedit.validate_window('reviewer', 'win-nosuch', True, '')
    assert ok is False and payload['error'] == 'unknown window id'
    _arm_edit(monkeypatch, world)
    ok, payload = selfedit.request_window(
        'worker', _window_slot(), '03:10', 15, 'ship the tuning change',
        'reviewer')
    assert ok is True, payload
    assert selfedit.resolve_window_escalation(payload['escalationId'], True)
    ok, payload = selfedit.validate_window('reviewer', payload['id'], True,
                                           '')
    assert ok is False and 'nothing is waiting for validation' \
        in payload['error']


def test_validate_window_after_slot_ended_refuses(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id = _book_and_apply(monkeypatch, world)
    selfedit._update_window(window_id, end_ts=selfedit.time.time() - 10)
    ok, payload = selfedit.validate_window('reviewer', window_id, True, '')
    assert ok is False and 'slot has ended' in payload['error']


def test_validate_window_requires_note_for_unlanded_edits(
        fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id = _book_and_apply(monkeypatch, world, arm_twice=True)
    ok, payload = selfedit.validate_window('reviewer', window_id, True, '')
    assert ok is False and 'note is REQUIRED' in payload['error']
    assert 'did not apply' in payload['error']
    status_out = ' M world/gear.py\n'
    _push_git_plan(monkeypatch, {'status': (0, status_out, '')})
    ok, payload = selfedit.validate_window('reviewer', window_id, True,
                                           'tuning needs review', issues=True)
    assert ok is True and payload['status'] == 'successful_with_issues', \
        payload


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
