"""Director/chief-of-staff capabilities: read-only live inspection, scoped
delegation, schedule control, and runbooks. Writes stay bounded."""
import os
import sys

from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
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
        'researchTopics': [
            {'id': 't-1', 'topic': 'Mine', 'startUrl': 'https://example.com/a',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'createdBy': 'nora'},
            {'id': 't-2', 'topic': 'Shared', 'startUrl': 'https://example.com/b',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'createdBy': 'watchlist'},
        ],
        'workQueue': [],
        'tasks': {},
    }


def _patch_state(monkeypatch, state):
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: state)
    monkeypatch.setattr(serve, 'save_state_to_db', lambda s: None)
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(serve, '_state_begin', lambda: state)
    monkeypatch.setattr(serve, '_state_abort', lambda: None)


def test_live_read_denies_workers(monkeypatch):
    _patch_state(monkeypatch, _state())
    out = serve.supervisor_live_read('ruth', {'op': 'ls', 'path': 'world'})
    assert 'error' in out


def test_live_read_lists_and_greps_the_live_project(monkeypatch):
    _patch_state(monkeypatch, _state())
    listed = serve.supervisor_live_read('nora', {'op': 'ls', 'path': 'world'})
    assert 'sim.py' in listed['entries']
    found = serve.supervisor_live_read(
        'nora', {'op': 'grep', 'path': 'world', 'pattern': 'def _supervisor_brief'})
    assert any('140__supervisor_brief.py' in m for m in found['matches'])


def test_live_read_refuses_secrets_and_traversal(monkeypatch):
    _patch_state(monkeypatch, _state())
    for args in ({'op': 'read', 'path': '.env'},
                 {'op': 'read', 'path': '.secret_keys/x'},
                 {'op': 'read', 'path': '../../etc/passwd'}):
        out = serve.supervisor_live_read('nora', args)
        assert 'error' in out, args


def test_delegate_requires_own_team_and_respects_cap(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    ok, payload = serve.supervisor_delegate(
        'nora', 'Check the parser', 'Read the parser and prove whether it handles empty input.',
        assignee='cora')
    assert ok is False and 'team' in payload['error']
    ok, payload = serve.supervisor_delegate(
        'nora', 'Check the parser', 'Read the parser and prove whether it handles empty input.',
        assignee='ruth', budget_usd=0.1)
    assert ok is True, payload
    assert state['workQueue'][0]['title'].startswith('[DELEGATE]')
    assert state['workQueue'][0]['assignedTo'] == 'ruth'


def test_delegate_is_capped(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    for i in range(serve.DELEGATE_MAX_OPEN):
        state['tasks'][f'task-{i}'] = {
            'id': f'task-{i}', 'title': f'[DELEGATE] open {i}', 'teamId': 'nora',
            'assignedTo': 'ruth', 'status': 'working'}
    ok, payload = serve.supervisor_delegate(
        'nora', 'One more', 'Check one more thing on the team and report back.')
    assert ok is False and 'open delegations' in payload['error']


def test_schedules_list_and_ownership(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    listed = serve.supervisor_schedules('nora', {'op': 'list'})
    assert [t['id'] for t in listed['topics']] == ['t-1', 't-2']
    denied = serve.supervisor_schedules('nora', {'op': 'delete', 'id': 't-2'})
    assert 'created' in denied['error']
    ok = serve.supervisor_schedules('nora', {'op': 'delete', 'id': 't-1'})
    assert ok.get('ok') is True
    assert [t['id'] for t in state['researchTopics']] == ['t-2']
    # The chief of staff may delete any schedule.
    ok = serve.supervisor_schedules('theo', {'op': 'delete', 'id': 't-2'})
    assert ok.get('ok') is True


def test_schedules_create_marks_the_creator(monkeypatch):
    state = _state()
    state['researchTopics'] = []
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve.config, 'note_watchlist_topic', lambda r: None)
    out = serve.supervisor_schedules('nora', {
        'op': 'create', 'topic': 'Watch', 'startUrl': 'https://example.com/w',
        'cadenceMs': 7200000})
    assert out.get('ok') is True
    assert state['researchTopics'][0]['createdBy'] == 'nora'


def test_runbook_lists_and_loads():
    run = content._make_supervisor_runbook_executor()
    listed = run('runbook', {})
    assert 'supervisor-sprint-check' in listed
    loaded = run('runbook', {'name': 'supervisor-sprint-check'})
    assert 'supervisor_report' in loaded and '1.' in loaded


def test_admin_report_covers_all_teams(monkeypatch):
    state = _state()
    state['tasks'] = {'t1': {'id': 't1', 'title': 'Nora card', 'status': 'working',
                             'teamId': 'nora', 'assignedTo': 'ruth', 'updatedAt': 1},
                      't2': {'id': 't2', 'title': 'Ben card', 'status': 'working',
                             'teamId': 'ben', 'assignedTo': 'cora', 'updatedAt': 1}}
    _patch_state(monkeypatch, state)
    text = content._make_supervisor_report_executor('theo')('supervisor_report', {})
    assert 'Nora card' in text and 'Ben card' in text


def test_admin_delegate_requires_and_accepts_a_team(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    ok, payload = serve.supervisor_delegate(
        'theo', 'Check the parser', 'Read the parser and prove whether it handles empty input.')
    assert ok is False and 'name a team' in payload['error']
    ok, payload = serve.supervisor_delegate(
        'theo', 'Check the parser', 'Read the parser and prove whether it handles empty input.',
        team='nora')
    assert ok is True, payload
    assert state['workQueue'][0]['assignedTo'] in ('ruth', None)


def test_brief_step_queues_a_cross_team_card_for_the_admin(monkeypatch):
    import sim
    state = _state()
    state['sprints'] = {'s1': {'id': 's1', 'status': 'active', 'teamIds': ['nora', 'ben']}}
    state['tasks'] = {'t1': {'id': 't1', 'title': 'Broke', 'status': 'failed',
                             'teamId': 'nora', 'assignedTo': 'ruth', 'updatedAt': 1}}
    state['_supervisorBriefAt'] = {}
    state['_supervisorCardAt'] = {}
    sim._supervisor_brief_step(state, 0, 1000)
    cross = [q for q in state['workQueue'] if str(q.get('title')).startswith('[SUPERVISE] Cross-team')]
    assert len(cross) == 1
    assert cross[0]['assignedTo'] == 'theo'


def test_admin_direct_backlog_files_a_high_priority_issue_and_mails_the_director(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    ok, payload = serve.supervisor_backlog_directive(
        'theo', 'nora', 'Investigate the failing parser',
        'The parser failed twice on the same input; card the fix and prove it handles empty input.',
        task_type='task')
    assert ok is True, payload
    key = payload['issueKey']
    assert state['issues'][key]['teamId'] == 'nora'
    req = next(r for r in state['backlogRequests'] if r.get('issueKey') == key)
    assert req['origin'] == 'chief_of_staff'
    assert req['priority'] == 2  # high
    assert req['directiveBy'] == 'theo'
    mail = state['agents']['nora'].get('mailbox') or []
    assert mail and mail[0]['kind'] == 'chief_directive'


def test_direct_backlog_is_chief_of_staff_only(monkeypatch):
    _patch_state(monkeypatch, _state())
    ok, payload = serve.supervisor_backlog_directive(
        'nora', 'ben', 'Investigate the parser',
        'The parser failed twice on the same input and needs a card now.')
    assert ok is False and 'chief of staff' in payload['error']


def test_admin_routine_sweep_does_not_fire_without_an_attention_reason(monkeypatch):
    import sim
    state = _state()
    state['sprints'] = {'s1': {'id': 's1', 'status': 'active',
                               'teamIds': ['nora', 'ben']}}
    state['tasks'] = {}
    state['workQueue'] = []
    state['_supervisorBriefAt'] = {}
    state['_supervisorCardAt'] = {}
    sim._supervisor_brief_step(state, 0, 1000)
    assert state['workQueue'] == []
    # Idle is not a reason to mint a card on a later cadence either.
    sim._supervisor_brief_step(state, 0, 1000 + sim.CHIEF_MONITOR_INTERVAL_MS + 1)
    assert state['workQueue'] == []
