"""Self-edit proposal, review, apply, and status tests."""
import sys
import os
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402

from _selfedit_support import (  # noqa: E402
    fake_repo, _approve, _patch_player_approval, _fake_test_gate
)

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
    root, world = fake_repo
    (world / 'serve.py').write_text('# kernel\n', encoding='utf-8')
    (root / 'CONSTITUTION.md').write_text('rule\n', encoding='utf-8')
    # serve.py is the signed kernel, not a protected file: it is accepted as
    # a proposal target and routed through the signature/restart path.
    assert selfedit.is_kernel(world / 'serve.py')
    assert not selfedit.is_player_signature(world / 'serve.py')
    assert not selfedit.is_protected(world / 'serve.py')
    ok, payload = selfedit.propose(
        'worker', 'CONSTITUTION.md', 'rule', 'no', 'why')
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

def test_propose_refuses_movement_physics(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'sim.py').write_text(
        'A = 1\n# BEGIN MOVEMENT PHYSICS\nX = 1\n# END MOVEMENT PHYSICS\n',
        encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'sim.py', 'X = 1', 'X = 2', 'faster walking')
    assert ok is False
    assert 'owner-protected' in payload['error']
    assert (world / 'sim.py').read_text(encoding='utf-8').endswith('# END MOVEMENT PHYSICS\n')


def test_propose_refuses_sim_when_physics_markers_missing(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'sim.py').write_text('X = 1\n', encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'sim.py', 'X = 1', 'X = 2', 'ordinary edit')
    assert ok is False
    assert 'guard markers' in payload['error']


def test_non_python_asset_skips_python_compile(fake_repo, monkeypatch):
    root, world = fake_repo
    (world / 'panel.html').write_text('<html><body>panel</body></html>\n',
                                      encoding='utf-8')
    _approve(monkeypatch)
    ok, payload = selfedit.propose(
        'worker', 'world/panel.html', '<html><body>panel</body></html>',
        '<html><body class="artifact">panel</body></html>',
        'artifact-style pressoffice shell')
    assert ok is True, payload
    assert payload['status'] == 'armed'
