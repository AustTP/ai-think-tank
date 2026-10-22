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


def test_director_generates_avatars_for_own_team_only(monkeypatch, tmp_path):
    state = _state()
    state['agents']['ruth'] = {'name': 'Ruth', 'role': 'Assistant'}
    state['agents']['cora'] = {'name': 'Cora', 'role': 'Assistant'}
    _patch_state(monkeypatch, state)
    avatar_dir = tmp_path / 'avatars'
    monkeypatch.setattr(serve, '_avatar_dir', lambda aid: str(avatar_dir))
    monkeypatch.setattr(serve, '_avatar_set_complete', lambda aid: False)
    monkeypatch.setattr(serve, '_pixellab_credits_remaining', lambda: 5.0)
    monkeypatch.setattr(serve, '_think_tank_spend_cap_exceeded', lambda: False)
    monkeypatch.setattr(serve, '_agent_spend_cap_exceeded', lambda aid: False)

    def fake_ensure(aid, name, role, is_director):
        avatar_dir.mkdir(parents=True, exist_ok=True)
        for orientation in ('south', 'north', 'east', 'west'):
            (avatar_dir / f'{orientation}.png').write_bytes(b'png')
        return True

    monkeypatch.setattr(serve, '_ensure_agent_avatar', fake_ensure)
    out = serve.generate_agent_avatar('nora', 'ruth')
    assert out['ok'] is True, out
    assert sorted(out['files']) == ['east.png', 'north.png', 'south.png', 'west.png']
    # A director cannot target another team's worker.
    out = serve.generate_agent_avatar('nora', 'cora')
    assert out['ok'] is False and 'not on your team' in out['error']
    # The chief can target any non-admin agent.
    out = serve.generate_agent_avatar('theo', 'cora')
    assert out['ok'] is True, out
    # But not the chief's own default sprite.
    out = serve.generate_agent_avatar('theo', 'theo')
    assert out['ok'] is False


def test_generate_agent_avatar_respects_the_credit_floor(monkeypatch, tmp_path):
    state = _state()
    state['agents']['ruth'] = {'name': 'Ruth'}
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_avatar_dir', lambda aid: str(tmp_path))
    monkeypatch.setattr(serve, '_avatar_set_complete', lambda aid: False)
    monkeypatch.setattr(serve, '_think_tank_spend_cap_exceeded', lambda: False)
    monkeypatch.setattr(serve, '_agent_spend_cap_exceeded', lambda aid: False)
    monkeypatch.setattr(serve, '_pixellab_credits_remaining', lambda: 0.10)
    out = serve.generate_agent_avatar('nora', 'ruth')
    assert out['ok'] is False and 'floor' in out['error']


def test_avatar_queue_assigns_missing_worker_to_its_director(monkeypatch):
    state = _state()
    state['agents']['ruth'] = {'name': 'Ruth'}
    state['agents']['cora'] = {'name': 'Cora'}
    state['_avatarTaskAt'] = {}
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_state_begin', lambda: state)
    monkeypatch.setattr(serve, '_state_abort', lambda: None)
    monkeypatch.setattr(serve, '_pixellab_configured', lambda: True)
    monkeypatch.setattr(serve, '_avatar_generation_available', lambda: True)
    monkeypatch.setattr(serve, '_think_tank_spend_cap_exceeded', lambda: False)
    monkeypatch.setattr(serve, '_avatar_set_complete', lambda aid: False)
    monkeypatch.setattr(serve, 'AVATAR_TASK_BATCH_MAX', 10)
    n = serve._queue_avatar_tasks()
    assert n > 0
    queued = [q for q in state['workQueue'] if str(q.get('title', '')).startswith('[AVATAR]')]
    ruth = next(q for q in queued if 'ruth' in q['title'])
    assert ruth['assignedTo'] == 'nora' and ruth['teamId'] == 'nora'
    cora = next(q for q in queued if 'cora' in q['title'])
    assert cora['assignedTo'] == 'ben'
    # The per-agent cooldown prevents a duplicate turn on the next pass.
    assert serve._queue_avatar_tasks() == 0


def test_avatar_queue_stays_idle_without_pixellab(monkeypatch):
    _patch_state(monkeypatch, _state())
    monkeypatch.setattr(serve, '_pixellab_configured', lambda: False)
    assert serve._queue_avatar_tasks() == 0


def test_avatar_directory_is_the_agent_own_directory():
    import os
    expected = os.path.join(serve.AGENTS_DIR, 'ruth', 'avatars')
    assert serve._avatar_dir('ruth') == expected


def test_pixellab_token_falls_back_to_the_env_file(monkeypatch):
    monkeypatch.setattr(serve, '_open_secret', lambda token: None)
    monkeypatch.setattr(serve, '_credential_token', lambda name: 'vault-token')
    monkeypatch.setattr(serve, '_load_env', lambda: {'PIXELLAB_API_KEY': 'env-key'})
    assert serve._pixellab_token() == 'env-key'


def test_supervise_card_pins_its_director_and_keeps_the_team(monkeypatch):
    import sim
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'name': 'Nora', 'isDirector': True},
            {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'},
        ],
        'agents': {
            'theo': {'id': 'theo', 'name': 'Theo', 'x': 0, 'y': 0, 'busy': False,
                     'visible': True, 'offDuty': False, 'inRoom': None},
            'nora': {'id': 'nora', 'name': 'Nora', 'x': 0, 'y': 0, 'busy': False,
                     'visible': True, 'offDuty': False, 'inRoom': None},
            'ruth': {'id': 'ruth', 'name': 'Ruth', 'x': 0, 'y': 0, 'busy': False,
                     'visible': True, 'offDuty': False, 'inRoom': None},
        },
        'teams': [{'id': 'nora', 'directorId': 'nora', 'members': ['ruth']}],
        'tasks': {}, 'workQueue': [],
    }
    grid, doors = sim._load_outdoor_geometry()
    door = doors['pressoffice']
    for a in state['agents'].values():
        a['x'] = door['x'] + door['w'] / 2
        a['y'] = door['y'] + door['h'] + 4
    item = {'title': '[SUPERVISE] check', 'room': 'pressoffice',
            'taskType': 'supervise', 'assignedTo': 'nora', 'teamId': 'nora',
            'instructions': 'check the team',
            'sourcePaths': ['media/transcripts/v.txt'],
            'reportPath': 'archive/report.md'}
    task = sim._assign_due_item(state, item, True, grid, doors, 1000,
                                task_id_holder={0: 0})
    assert task is not None
    assert task['assignedTo'] == 'nora'
    assert task['teamId'] == 'nora'
    assert task['sourcePaths'] == ['media/transcripts/v.txt']
    assert task['reportPath'] == 'archive/report.md'
    # A busy director makes the supervisor card wait; it must never fall
    # through to a worker who has no supervisor scope.
    state['agents']['nora']['task'] = task['id']
    item2 = dict(item, title='[SUPERVISE] check 2')
    assert sim._assign_due_item(state, item2, True, grid, doors, 2000,
                                task_id_holder={0: 1}) is sim._ASSIGN_WAIT
    assert all(t.get('assignedTo') != 'ruth' for t in state['tasks'].values())


def test_supervisor_tool_loop_has_a_heartbeat(monkeypatch):
    import content
    import sim
    state = _state()
    state['agents']['nora'] = {'busy': False, 'mailbox': []}
    state['tasks'] = {}
    task = {'id': 'sup-hb', 'title': 'Check the team', 'teamId': 'nora',
            'instructions': 'Check the team.'}
    called = {}

    def fake_loop(model, messages, tools, execute_tool, **kwargs):
        called['report'] = execute_tool('supervisor_report', {})
        return 'done', []

    monkeypatch.setattr(serve, '_call_agent_tool_loop', fake_loop)
    monkeypatch.setattr(serve, '_mid_tier_slug', lambda: 'mid')
    monkeypatch.setattr(serve, 'get_or_create_agent_key', lambda aid: 'k')
    monkeypatch.setattr(serve, '_avatar_set_complete', lambda aid: True)
    monkeypatch.setattr(sim, '_store_content_result', lambda *a, **k: None)
    content._run_supervisor_content(state, 'nora', task)
    assert 'report' in called
