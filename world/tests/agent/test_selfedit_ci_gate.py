"""CI gate for change requests (2026-11-07 operator direction).

A pushed window is not successful until the repo's CI run on the pushed
sha is green: a red CI reverts the push (main never sits red), an
unverifiable CI is recorded as such after the wait window. gh and git are
faked -- no network, no real repository.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402

from _selfedit_support import (  # noqa: E402,F1
    fake_repo, _approve, _patch_player_approval, _patch_roster, _arm_edit,
    _notify_log, _fake_test_gate, _window_slot)


def _fire_requested_window(window_id):
    now = selfedit.time.time()
    selfedit._update_window(window_id, status='approved',
                            start_ts=now - 60, end_ts=now + 600)
    fired = selfedit.window_tick()
    assert window_id in fired


def _pushed_window(monkeypatch, fake_repo, implementer='worker',
                   validator='reviewer'):
    """Book a window, apply it behind a green gate, and validate it so the
    push path runs -- with git faked all the way through. Returns the id."""
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
    monkeypatch.setattr(serve, '_set_dormant', lambda v: None)
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    _fake_test_gate(monkeypatch, [(True, 'ALL GREEN')])
    monkeypatch.setattr(selfedit, '_git_run',
                        lambda args: (0, 'abcdef123456\n', '')
                        if args[0] == 'rev-parse' else (0, '', ''))
    applied = selfedit.apply_pass()
    assert applied == 1
    selfedit.validate_window(validator, window_id, True, 'looks right')
    return window_id


def test_pushed_window_awaits_ci(fake_repo, monkeypatch):
    wid = _pushed_window(monkeypatch, fake_repo)
    assert selfedit._window_field(wid, 'push_status') == 'awaiting_ci'


def test_green_ci_completes_the_change_request(fake_repo, monkeypatch):
    wid = _pushed_window(monkeypatch, fake_repo)
    monkeypatch.setattr(selfedit, '_gh_run_json', lambda sha: [
        {'databaseId': 1, 'status': 'completed', 'conclusion': 'success'}])
    selfedit._ci_sweep_pushed_windows()
    assert selfedit._window_field(wid, 'push_status') == 'pushed'


def test_red_ci_reverts_the_push(fake_repo, monkeypatch):
    wid = _pushed_window(monkeypatch, fake_repo)
    calls = []
    monkeypatch.setattr(selfedit, '_gh_run_json', lambda sha: [
        {'databaseId': 1, 'status': 'completed', 'conclusion': 'failure'}])

    def fake_git(args):
        calls.append(args)
        return (0, '', '')

    monkeypatch.setattr(selfedit, '_git_run', fake_git)
    selfedit._ci_sweep_pushed_windows()
    assert selfedit._window_field(wid, 'push_status') == 'failed'
    assert selfedit._window_field(wid, 'push_category') == 'unsuccessful'
    assert 'CI failed' in (selfedit._window_field(wid, 'push_note') or '')
    assert calls[0][:2] == ['revert', '--no-edit'], calls
    assert calls[-1][:2] == ['push', 'origin'], calls


def test_unverifiable_ci_times_out_to_a_recorded_note(fake_repo, monkeypatch):
    wid = _pushed_window(monkeypatch, fake_repo)
    monkeypatch.setattr(selfedit, '_gh_run_json', lambda sha: None)
    selfedit._update_window(
        wid, pushed_at=selfedit.time.time() - selfedit._CI_WAIT_TIMEOUT_S - 1)
    selfedit._ci_sweep_pushed_windows()
    assert selfedit._window_field(wid, 'push_status') == 'pushed'
    assert 'CI unverified' in (selfedit._window_field(wid, 'push_note') or '')


def test_pending_ci_keeps_waiting(fake_repo, monkeypatch):
    wid = _pushed_window(monkeypatch, fake_repo)
    monkeypatch.setattr(selfedit, '_gh_run_json', lambda sha: [
        {'databaseId': 1, 'status': 'in_progress', 'conclusion': None}])
    selfedit._ci_sweep_pushed_windows()
    assert selfedit._window_field(wid, 'push_status') == 'awaiting_ci'
