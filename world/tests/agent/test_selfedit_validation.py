"""Self-edit oversight, read surface, validation, and push tests."""
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
    assert paths == ['world/BUSINESS-PROCESSES.md', 'world/gear.py',
                     'world/serve.py']
    assert listing['modules'][1]['bytes'] == len('SPEED = 2\n')

    ok, head = selfedit.read_module('gear.py')
    assert ok is True, head
    assert head['path'] == 'world/gear.py'
    assert head['text'] == 'SPEED = 2\n'
    assert head['truncated'] is False
    assert head['totalBytes'] == len('SPEED = 2\n')
    assert head['sha256'] == hashlib.sha256(b'SPEED = 2\n').hexdigest()

    # The kernel is readable (it is signed, not hidden); the gate and the
    # oversight internals stay closed.
    ok, kernel = selfedit.read_module('serve.py')
    assert ok is True and kernel['text'] == '# kernel\n'
    for guarded in (('jev.py', 'outside the readable'),
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
    assert ok is False and 'kernel' in payload['error']
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


def test_validate_window_requires_note_for_denial(fake_repo, monkeypatch):
    _, world = fake_repo
    window_id, _edit_id = _book_and_apply(monkeypatch, world)
    ok, payload = selfedit.validate_window('reviewer', window_id, False, '')
    assert ok is False and 'note is REQUIRED' in payload['error']
    ok, payload = selfedit.validate_window(
        'reviewer', window_id, False, 'the repo gate is red')
    assert ok is True and payload['status'] == 'unsuccessful', payload
    assert selfedit._window_field(window_id, 'push_status') == 'failed'
    assert 'repo gate is red' in (
        selfedit._window_field(window_id, 'push_note') or '')


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
