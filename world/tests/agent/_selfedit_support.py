"""Shared fixtures and helpers for the self-edit test modules."""

import sys
import os
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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
