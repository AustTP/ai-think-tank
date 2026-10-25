"""Throughput telemetry must separate a pipeline failure from a slow finish.

The live alert ("only 28% finished within 1h") was mostly 290 coding attempts
that FAILED the quality pipeline and were marked done to stop re-issue. They
had no completion log, so the metric could only call them unfinished.
"""
import json
import os
import sqlite3
import sys
import time

from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402


def _state():
    return {'agents': {'ben': {'task': 'task-x', 'busy': True, 'inRoom': 'pressoffice'}},
            'tasks': {'task-x': {'id': 'task-x', 'title': 'Fix: a thing',
                                 'status': 'working', 'note': 'execution exited non-zero'}}}


def test_failed_pipeline_release_logs_an_auto_close():
    state = _state()
    with mock.patch('serve.log_action') as log:
        sim._release_agent_after_failure(state, 'ben', state['tasks']['task-x'])
    assert state['tasks']['task-x']['status'] == 'done'
    assert state['agents']['ben']['busy'] is False
    action, payload = log.call_args[0][1], log.call_args[0][2]
    assert action == 'task_auto_closed'
    assert payload['taskId'] == 'task-x'
    assert payload['stage'] == 'content_pipeline'
    assert 'execution exited' in payload['reason']


def test_horizon_metrics_counts_retired_separately_from_unfinished(tmp_path):
    path = str(tmp_path / 'log.db')
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE action_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent_id TEXT,
        action TEXT, details TEXT)''')
    now = int(time.time())
    def add(ts, action, tid):
        conn.execute('INSERT INTO action_log (ts, agent_id, action, details) VALUES (?,?,?,?)',
                     (ts, 'ben', action, json.dumps({'taskId': tid, 'title': tid})))
    add(now - 1000, 'task_assigned', 'done-1')
    add(now - 500, 'task_completed', 'done-1')
    add(now - 900, 'task_assigned', 'retired-1')
    add(now - 800, 'task_auto_closed', 'retired-1')
    add(now - 700, 'task_assigned', 'still-1')
    conn.commit(); conn.close()
    with mock.patch.object(serve, 'DB_PATH', path):
        metr = serve._task_horizon_metrics(now)
    assert metr['assigned'] == 3
    assert metr['completed'] == 1
    assert metr['abandoned'] == 1
    assert metr['fast_completion_rate'] == round(1 / 3, 4)


def test_auto_close_of_an_unassigned_task_is_not_counted(tmp_path):
    path = str(tmp_path / 'log.db')
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE action_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent_id TEXT,
        action TEXT, details TEXT)''')
    now = int(time.time())
    conn.execute('INSERT INTO action_log (ts, agent_id, action, details) VALUES (?,?,?,?)',
                 (now - 100, 'ben', 'task_auto_closed', json.dumps({'taskId': 'old-1'})))
    conn.commit(); conn.close()
    with mock.patch.object(serve, 'DB_PATH', path):
        metr = serve._task_horizon_metrics(now)
    assert metr['abandoned'] == 0


def test_archived_pipeline_failure_without_a_log_counts_as_retired(tmp_path):
    path = str(tmp_path / 'log.db')
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE action_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent_id TEXT,
        action TEXT, details TEXT)''')
    conn.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT, updated_at REAL)')
    conn.execute('CREATE TABLE task_archive (id TEXT PRIMARY KEY, blob TEXT, updated_at REAL)')
    now = int(time.time())
    conn.execute('INSERT INTO action_log (ts, agent_id, action, details) VALUES (?,?,?,?)',
                 (now - 100, 'ben', 'task_assigned', json.dumps({'taskId': 'old-1'})))
    conn.execute('INSERT INTO task_archive (id, blob, updated_at) VALUES (?,?,?)',
                 ('old-1', json.dumps({'id': 'old-1', 'status': 'done',
                                       'note': 'execution exited non-zero (code 2)'}), now))
    conn.commit(); conn.close()
    with mock.patch.object(serve, 'DB_PATH', path):
        metr = serve._task_horizon_metrics(now)
    assert metr['assigned'] == 1
    assert metr['completed'] == 0
    assert metr['abandoned'] == 1
    assert metr['in_flight'] == 0
