"""A repeated same-root failure stops the fix-card spiral."""
import time

import sim


def _task(tid, title, status='failed', failed_at=None):
    return {'id': tid, 'title': title, 'status': status,
            'failedAt': failed_at or int(time.time() * 1000),
            'teamId': 'dev', 'assignedTo': 'ben'}


def test_two_recent_failures_stop_the_loop():
    now = int(time.time() * 1000)
    state = {'tasks': {
        't0': _task('t0', 'Distill recent think tank knowledge', failed_at=now - 1000),
        't1': _task('t1', 'Fix: Distill recent think tank knowledge', failed_at=now - 500),
    }}
    info = sim._repair_loop_should_stop(state, 'Fix: Distill recent think tank knowledge', now)
    assert info is not None
    assert info['failures'] == 2
    assert info['root'] == 'Distill recent think tank knowledge'


def test_one_failure_does_not_stop_the_loop():
    now = int(time.time() * 1000)
    state = {'tasks': {'t0': _task('t0', 'Distill recent think tank knowledge', failed_at=now)}}
    assert sim._repair_loop_should_stop(state, 'Distill recent think tank knowledge', now) is None


def test_old_failures_fall_outside_the_window():
    now = int(time.time() * 1000)
    state = {'tasks': {
        't0': _task('t0', 'Distill recent think tank knowledge',
                    failed_at=now - sim.REPAIR_LOOP_WINDOW_MS - 1000),
        't1': _task('t1', 'Fix: Distill recent think tank knowledge', failed_at=now),
    }}
    assert sim._repair_loop_should_stop(state, 'Fix: Distill recent think tank knowledge', now) is None


def test_stop_marks_blocked_and_mails_the_director_once():
    now = int(time.time() * 1000)
    task = _task('t1', 'Fix: Distill recent think tank knowledge')
    state = {
        'tasks': {'t1': task},
        'teams': [{'id': 'dev', 'directorId': 'nora'}],
        'agentRoster': [{'id': 'ben', 'director': 'nora'}],
        'agents': {'nora': {'mailbox': []}},
        'workQueue': [],
    }
    info = {'root': 'Distill recent think tank knowledge', 'failures': 2,
            'taskIds': ['t0', 't1']}
    sim._stop_repair_loop(state, task, info, 'ben')
    assert task['status'] == 'blocked'
    assert task['blockedReason'] == 'repair_loop'
    assert 'contract' in task['failNote'].lower()
    assert state['agents']['nora']['mailbox'][0]['kind'] == 'card_repair_loop'
    # A second stop for the same root does not send another note.
    sim._stop_repair_loop(state, task, info, 'ben')
    assert len(state['agents']['nora']['mailbox']) == 1


def test_send_back_stops_minting_a_third_fix(monkeypatch):
    now = int(time.time() * 1000)
    task = _task('t1', 'Fix: Distill recent think tank knowledge', status='working')
    task['note'] = 'missing deliverable'
    state = {
        'tasks': {
            't0': _task('t0', 'Distill recent think tank knowledge', failed_at=now - 1000),
            't1': task,
        },
        'teams': [{'id': 'dev', 'directorId': 'nora'}],
        'agentRoster': [{'id': 'ben', 'director': 'nora'}],
        'agents': {'nora': {'mailbox': []}},
        'workQueue': [],
    }
    queued = []
    monkeypatch.setattr(sim, 'queue_work', lambda st, items: queued.extend(items))
    monkeypatch.setattr(sim, '_notify_card_failure', lambda *a, **k: None)
    monkeypatch.setattr(sim, '_log_governance', lambda *a, **k: None)
    sim._send_back_after_failure(state, task, fail_note='missing deliverable')
    assert queued == [], 'a third fix card must not be minted'
    assert task['status'] == 'blocked'
    assert task['blockedReason'] == 'repair_loop'


def test_ledger_survives_archiving():
    """A failed card is archived (its row leaves state) within minutes, but
    the repair-loop guard must still count it -- the 2026-11-05 pump minted
    ~50 repair children in 20 minutes because archived failures reset the
    counters between generations."""
    st = {}
    t1 = {'id': 'task-1', 'title': 'Fix: Fix disk-full failure on task-12270 staged git write',
          'status': 'failed', 'failedAt': int(time.time() * 1000) - 60_000}
    st['tasks'] = {'task-1': t1}
    sim._record_repair_failure(st, t1, int(time.time() * 1000) - 60_000)
    # The watchdog archives the row: it leaves the live state.
    del st['tasks']['task-1']
    t2 = {'id': 'task-2', 'title': 'Fix: Fix disk-full failure on task-12270 staged git write',
          'status': 'failed', 'failedAt': int(time.time() * 1000)}
    st['tasks']['task-2'] = t2
    sim._record_repair_failure(st, t2, int(time.time() * 1000))
    info = sim._repair_loop_should_stop(
        st, t2['title'], int(time.time() * 1000))
    assert info, 'archived failure must still count toward the stop'
    assert info['failures'] >= 2, info
    assert 'task-1' in info['taskIds'], info
