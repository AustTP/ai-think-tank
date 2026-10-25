import copy
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import serve
import server.state_updates as state_updates


def states():
    old = {'tasks': {'parent': {'id': 'parent', 'status': 'blocked', 'interventionHistory': []}},
           'workQueue': [], 'agents': {}, 'taskCounter': 1, 'marker': 'old'}
    fresh = copy.deepcopy(old)
    fresh['tasks']['parent'].update(status='repairing', manualAttention=False,
                                    interventionHistory=[{'action': 'retry', 'at': 10}])
    fresh['workQueue'] = [{'title': 'Fix parent', 'reviewOf': 'parent'}]
    return old, fresh


def test_stale_save_preserves_retry_and_queue_but_keeps_unrelated_changes():
    stale, fresh = states()
    stale['marker'] = 'new report'
    merged = state_updates.preserve_guidance(fresh, stale)
    assert merged['tasks']['parent'] == fresh['tasks']['parent']
    assert merged['workQueue'] == fresh['workQueue']
    assert merged['marker'] == 'new report'
    assert stale['tasks']['parent']['status'] == 'blocked'


def test_stale_save_keeps_dispatched_repair_and_worker_assignment():
    stale, fresh = states()
    fresh['workQueue'] = []
    fresh['tasks']['repair'] = {'id': 'repair', 'reviewOf': 'parent', 'status': 'working'}
    fresh['agents']['worker'] = {'busy': True, 'task': 'repair'}
    fresh['taskCounter'] = 2
    merged = state_updates.preserve_guidance(fresh, stale)
    assert merged['tasks']['repair']['status'] == 'working'
    assert merged['agents']['worker']['task'] == 'repair'
    assert merged['taskCounter'] == 2


def test_new_failure_after_retry_can_still_block_the_task():
    _, fresh = states()
    failed = copy.deepcopy(fresh)
    failed['tasks']['parent']['status'] = 'blocked'
    assert state_updates.preserve_guidance(fresh, failed)['tasks']['parent']['status'] == 'blocked'


def test_real_state_save_cannot_erase_accepted_guidance(tmp_path):
    database = tmp_path / 'state.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT, updated_at REAL)')
    stale, fresh = states()
    with patch.object(serve, 'DB_PATH', str(database)), \
         patch.object(serve, 'sync_agent_directories'), \
         patch.object(serve.training_capture, 'sync_outcomes'):
        serve.save_state_to_db(fresh)
        stale['marker'] = 'concurrent save'
        serve.save_state_to_db(stale)
        saved = serve.get_state_from_db()
    assert saved['tasks']['parent']['status'] == 'repairing'
    assert saved['workQueue'] == fresh['workQueue']
    assert saved['marker'] == 'concurrent save'


def test_terminal_archived_task_is_not_resurrected_by_stale_save(tmp_path):
    database = tmp_path / 'state.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT, updated_at REAL)')
    with patch.object(serve, 'DB_PATH', str(database)), \
         patch.object(serve, 'sync_agent_directories'), \
         patch.object(serve.training_capture, 'sync_outcomes'):
        serve.save_state_to_db({'tasks': {'t': {'id': 't', 'status': 'working'}},
                                'workQueue': [], 'agents': {}})
        serve.save_state_to_db({'tasks': {'t': {'id': 't', 'status': 'done', 'note': 'finished'}},
                                'workQueue': [], 'agents': {}})
        serve.save_state_to_db({'tasks': {'t': {'id': 't', 'status': 'working'}},
                                'workQueue': [], 'agents': {}})
        saved = serve.get_state_from_db()
    assert saved['tasks']['t']['status'] == 'done'
