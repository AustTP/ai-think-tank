"""Adaptive suppression: a repeatedly failing card pattern stops minting."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


def _failed(tid, now, issue=None, title=None, status='failed'):
    task = {'id': tid, 'status': status, 'failedAt': now,
            'assignedTo': 'ada', 'title': title or ('Fix: ' + tid)}
    if issue:
        task['issueKey'] = issue
    return task


def test_two_failures_do_not_suppress():
    now = int(time.time() * 1000)
    state = {'tasks': {'task-1': _failed('task-1', now, issue='IRIS-1'),
                       'task-2': _failed('task-2', now - 1000, issue='IRIS-1')}}
    assert sim._card_pattern_suppressed(
        state, {'issueKey': 'IRIS-1', 'title': 'x'}, now) is None


def test_three_failures_suppress_the_issue():
    now = int(time.time() * 1000)
    state = {'tasks': {
        'task-1': _failed('task-1', now, issue='IRIS-1'),
        'task-2': _failed('task-2', now - 1000, issue='IRIS-1'),
        'task-3': _failed('task-3', now - 2000, issue='IRIS-1', status='blocked'),
    }}
    info = sim._card_pattern_suppressed(
        state, {'issueKey': 'IRIS-1', 'title': 'x'}, now)
    assert info is not None
    assert info['key'] == 'issue:IRIS-1'
    assert info['failures'] == 3


def test_new_issue_key_is_not_suppressed():
    now = int(time.time() * 1000)
    state = {'tasks': {
        'task-1': _failed('task-1', now, issue='IRIS-1'),
        'task-2': _failed('task-2', now, issue='IRIS-1'),
        'task-3': _failed('task-3', now, issue='IRIS-1'),
    }}
    assert sim._card_pattern_suppressed(
        state, {'issueKey': 'IRIS-2', 'title': 'x'}, now) is None


def test_title_fallback_strips_fix_prefixes():
    now = int(time.time() * 1000)
    state = {'tasks': {
        'task-1': _failed('task-1', now, title='Fix: Fix: Act on mail'),
        'task-2': _failed('task-2', now, title='Fix: Act on mail'),
        'task-3': _failed('task-3', now, title='Act on mail (retry 2)'),
    }}
    info = sim._card_pattern_suppressed(
        state, {'title': 'Fix: Act on mail'}, now)
    assert info is not None
    assert info['key'] == 'title:Act on mail'


def test_stop_card_pattern_alerts_once_per_window(monkeypatch):
    import serve
    calls = []
    monkeypatch.setattr(serve, 'send_player_telegram_sync',
                        lambda subject, body: calls.append((subject, body)) or True)
    monkeypatch.setattr(sim, '_log_governance', lambda *a, **k: None)
    monkeypatch.setattr(sim, '_append_mailbox', lambda *a, **k: None)
    now = int(time.time() * 1000)
    state = {'tasks': {
        'task-1': {'id': 'task-1', 'status': 'failed', 'failedAt': now,
                   'assignedTo': 'ada', 'issueKey': 'IRIS-1',
                   'title': 'Reset cards'},
    }, 'agents': {'ada': {}}}
    info = {'key': 'issue:IRIS-1', 'failures': 3, 'taskIds': ['task-1']}
    sim._stop_card_pattern(state, state['tasks']['task-1'], info, 'ada')
    sim._stop_card_pattern(
        state, {'id': 'task-2', 'assignedTo': 'ada', 'issueKey': 'IRIS-1',
                'title': 'Reset cards again'}, info, 'ada')
    assert len(calls) == 1
    assert 'Card pattern suppressed' in calls[0][0]
