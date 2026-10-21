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
