"""Supervisor tools: a scoped read-only report and one role-checked budget
top-up. The supervisor never gets database or code write access."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import sim  # noqa: E402
import content  # noqa: E402


def _state():
    return {
        'agents': {'nora': {}, 'ben': {}, 'theo': {}, 'ruth': {}, 'cora': {}},
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'name': 'Nora', 'isDirector': True},
            {'id': 'ben', 'name': 'Ben', 'isDirector': True, 'director': 'theo'},
            {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'},
            {'id': 'cora', 'name': 'Cora', 'director': 'ben'},
        ],
        'teams': [
            {'id': 'nora', 'directorId': 'nora', 'members': ['ruth']},
            {'id': 'ben', 'directorId': 'ben', 'members': ['cora']},
        ],
        'sprints': {'s1': {'id': 's1', 'name': 'S1', 'status': 'active',
                           'teamIds': ['nora']}},
        'tasks': {
            't1': {'id': 't1', 'title': 'Own card', 'status': 'failed',
                   'teamId': 'nora', 'assignedTo': 'ruth', 'budgetUsd': 0.5,
                   'ownSpendUsd': 0.5, 'failNote': 'budget exhausted',
                   '_budgetExhaustedNotified': True, 'updatedAt': 100},
            't2': {'id': 't2', 'title': 'Other card', 'status': 'working',
                   'teamId': 'ben', 'assignedTo': 'cora', 'budgetUsd': 0.5,
                   'ownSpendUsd': 0.1, 'updatedAt': 100},
        },
        'workQueue': [],
        '_supervisorBriefAt': {},
        '_supervisorCardAt': {},
    }


def _patch_state(monkeypatch, state):
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: state)
    monkeypatch.setattr(serve, 'save_state_to_db', lambda s: None)
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)


def test_supervisor_report_is_scoped(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    run = content._make_supervisor_report_executor('nora')
    text = run('supervisor_report', {})
    assert 'Own card' in text and 'Other card' not in text
    # A worker gets no report at all.
    assert 'do not supervise' in content._make_supervisor_report_executor('ruth')(
        'supervisor_report', {})


def test_grant_requires_a_supervisor(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    ok, payload = serve.supervisor_grant_budget('ruth', 't2', 1.0, 'need more')
    assert ok is False and 'supervisor' in payload['error']


def test_grant_is_team_scoped(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    ok, payload = serve.supervisor_grant_budget('nora', 't2', 1.0, 'need more')
    assert ok is False and 'another team' in payload['error']


def test_grant_requires_a_real_reason_and_a_higher_ceiling(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    ok, payload = serve.supervisor_grant_budget('nora', 't1', 1.0, 'more')
    assert ok is False and 'reason' in payload['error']
    ok, payload = serve.supervisor_grant_budget('nora', 't1', 0.4, 'needs a bit more')
    assert ok is False and 'exceed' in payload['error']
    ok, payload = serve.supervisor_grant_budget('nora', 't1', 99.0, 'needs a lot more')
    assert ok is False and 'between' in payload['error']


def test_grant_reopens_a_budget_failed_card(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    ok, payload = serve.supervisor_grant_budget(
        'nora', 't1', 0.9, 'the first attempt landed the parser; the last test needs another pass')
    assert ok is True, payload
    assert payload['reopened'] is True
    assert state['tasks']['t1']['budgetUsd'] == 0.9
    assert state['tasks']['t1']['budgetGrants'][0]['by'] == 'nora'
    assert state['workQueue'] and state['workQueue'][0]['budgetUsd'] == 0.9
    assert state['workQueue'][0]['assignedTo'] == 'ruth'


def test_brief_step_queues_one_supervise_card_on_attention(monkeypatch):
    state = _state()
    sim._supervisor_brief_step(state, 0, 1000)
    cards = [q for q in state['workQueue']
             if q.get('taskType') == 'supervise' and q.get('teamId') == 'nora']
    assert len(cards) == 1
    assert cards[0]['assignedTo'] == 'nora' and cards[0]['teamId'] == 'nora'
    # A second pass inside the cadence does not stack a duplicate.
    sim._supervisor_brief_step(state, 0, 1000 + 60_000)
    cards = [q for q in state['workQueue']
             if q.get('taskType') == 'supervise' and q.get('teamId') == 'nora']
    assert len(cards) == 1


def test_db_records_is_chief_of_staff_only(monkeypatch):
    _patch_state(monkeypatch, _state())
    out = serve.supervisor_db_records('nora', {'op': 'tables'})
    assert 'error' in out and 'chief of staff' in out['error']


def test_db_records_crud_is_filtered_and_audited(monkeypatch, tmp_path):
    import sqlite3
    db = tmp_path / 'records.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)')
    conn.execute("INSERT INTO records (name, n) VALUES ('a', 1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(serve, 'DB_PATH', str(db))
    _patch_state(monkeypatch, _state())
    calls = []
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: calls.append(a))

    listed = serve.supervisor_db_records('theo', {'op': 'tables'})
    assert [t['table'] for t in listed['tables']] == ['records']
    rows = serve.supervisor_db_records('theo', {'op': 'select', 'table': 'records'})
    assert rows['rows'] == [{'id': 1, 'name': 'a', 'n': 1}]

    out = serve.supervisor_db_records('theo', {
        'op': 'insert', 'table': 'records', 'values': {'name': 'b', 'n': 2}})
    assert out['inserted'] == 1
    out = serve.supervisor_db_records('theo', {
        'op': 'update', 'table': 'records',
        'values': {'n': 9}, 'where': {'name': 'b'}})
    assert out['updated'] == 1
    rows = serve.supervisor_db_records('theo', {
        'op': 'select', 'table': 'records', 'where': {'name': 'b'}})
    assert rows['rows'][0]['n'] == 9
    out = serve.supervisor_db_records('theo', {
        'op': 'delete', 'table': 'records', 'where': {'name': 'b'}})
    assert out['deleted'] == 1
    assert serve.supervisor_db_records('theo', {
        'op': 'select', 'table': 'records'})['rows'] == [{'id': 1, 'name': 'a', 'n': 1}]
    assert calls, 'every write must be audit-logged'


def test_db_records_refuses_secret_tables_and_unfiltered_writes(monkeypatch):
    monkeypatch.setattr(serve, 'DB_PATH', ':memory:')
    _patch_state(monkeypatch, _state())
    out = serve.supervisor_db_records('theo', {'op': 'select', 'table': 'agent_keys'})
    assert 'error' in out and 'not exposed' in out['error']
    out = serve.supervisor_db_records('theo', {
        'op': 'update', 'table': 'records', 'values': {'n': 1}})
    assert 'error' in out


def test_state_records_is_chief_of_staff_only(monkeypatch):
    _patch_state(monkeypatch, _state())
    out = serve.supervisor_state_records('nora', {'op': 'collections'})
    assert 'error' in out and 'chief of staff' in out['error']


def test_state_records_reads_and_patches_a_task_record(monkeypatch):
    state = _state()
    state['tasks'] = {'t1': {'id': 't1', 'status': 'walking', 'note': 'old'}}
    _patch_state(monkeypatch, state)
    got = serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/t1/status'})
    assert got['value'] == 'walking'
    out = serve.supervisor_state_records(
        'theo', {'op': 'set', 'path': 'tasks/t1/status', 'value': 'failed'})
    assert out['ok'] is True
    assert state['tasks']['t1']['status'] == 'failed'
    out = serve.supervisor_state_records(
        'theo', {'op': 'delete', 'path': 'tasks/t1/note'})
    assert out['ok'] is True
    assert 'note' not in state['tasks']['t1']


def test_state_records_refuses_roster_and_internal_keys(monkeypatch):
    _patch_state(monkeypatch, _state())
    for path in ('agents/ruth/busy', 'agentRoster/0/name', '_onboard/stage'):
        out = serve.supervisor_state_records(
            'theo', {'op': 'set', 'path': path, 'value': True})
        assert 'error' in out, path
