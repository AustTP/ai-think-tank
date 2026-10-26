"""Regression tests for durable task-id minting across a server restart.

The in-memory counter resets on restart while state['tasks'] survives in
SQLite. A bare counter re-minted task-1..task-N over live peer-gated tasks and
destroyed them. _next_task_id must skip every id the durable state already
holds.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


def _task(i):
    return {'id': f'task-{i}', 'title': f'task {i}', 'status': 'needs_review'}


def test_restart_counter_reset_skips_every_live_id():
    state = {'tasks': {f'task-{i}': _task(i) for i in range(1, 13)}}
    holder = [0]  # fresh process: the counter always restarts at zero
    assert sim._next_task_id(state, holder) == 'task-13'
    assert holder == [13]


def test_gap_is_not_backfilled():
    # Ids are monotonic so a retired id (task-2 here) can never be handed to
    # unrelated work later.
    state = {'tasks': {'task-1': _task(1), 'task-3': _task(3)}}
    holder = [0]
    assert sim._next_task_id(state, holder) == 'task-4'
    assert holder == [4]
    assert state['taskIdSeq'] == 4


def test_empty_state_starts_at_one_and_advances():
    state = {}
    holder = [0]
    assert sim._next_task_id(state, holder) == 'task-1'
    assert sim._next_task_id(state, holder) == 'task-2'
    assert holder == [2]
    assert state['taskIdSeq'] == 2


def test_retired_id_is_not_reused_after_restart():
    # The task that owned task-50 is gone from state['tasks'], and the process
    # restarted with holder=0. The durable high-water mark must still prevent
    # unrelated work from inheriting task-50 (the Oct 13 / Oct 26 reuse).
    state = {'tasks': {}, 'taskIdSeq': 50}
    holder = [0]
    assert sim._next_task_id(state, holder) == 'task-51'
    assert state['taskIdSeq'] == 51
