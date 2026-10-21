"""Supervisor brief: each director gets a scoped, state-only view of their own
team's sprint, budget, and process failures, plus where to look.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402

DB = os.path.abspath(os.path.join(os.path.dirname(sim.__file__), '..', 'think_tank.db'))


def _state():
    return {
        'agents': {
            'nora': {'busy': False, 'mailbox': []},
            'ben': {'busy': False, 'mailbox': []},
            'ruth': {'busy': False},
            'clara': {'busy': False},
        },
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'name': 'Nora', 'isDirector': True},
            {'id': 'ben', 'name': 'Ben', 'isDirector': True, 'director': 'theo'},
            {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'},
            {'id': 'clara', 'name': 'Clara', 'director': 'ben'},
        ],
        'teams': [
            {'id': 'nora', 'directorId': 'nora', 'members': ['ruth']},
            {'id': 'ben', 'directorId': 'ben', 'members': ['clara']},
        ],
        'sprints': {
            's1': {'id': 's1', 'name': 'Sprint 1', 'goal': 'ship',
                   'status': 'active', 'teamIds': ['nora']},
        },
        'tasks': {
            't1': {'id': 't1', 'title': 'Own team card', 'status': 'working',
                   'teamId': 'nora', 'assignedTo': 'ruth', 'ownSpendUsd': 0.4,
                   'budgetUsd': 0.5, 'updatedAt': 100},
            't2': {'id': 't2', 'title': 'Other team card', 'status': 'working',
                   'teamId': 'ben', 'assignedTo': 'clara', 'ownSpendUsd': 0.1,
                   'budgetUsd': 0.5, 'updatedAt': 100},
            't3': {'id': 't3', 'title': 'Broke', 'status': 'failed',
                   'teamId': 'nora', 'assignedTo': 'ruth',
                   'failNote': 'boom', 'updatedAt': 100},
        },
        '_supervisorBriefAt': {},
    }


def test_brief_is_scoped_to_the_team():
    brief = sim._supervisor_brief(_state(), 'nora', 1000)
    text = brief['text']
    assert 'Own team card' in text and 'Broke' in text
    assert 'Other team card' not in text
    assert 'boom' in text
    assert 'spend $0.4000 / budget $0.50' in text
    assert DB in text
    assert "json_extract(value,'$.teamId')='nora'" in text
    assert 'Scope: this team only' in text


def test_brief_lists_workers_missing_avatar_images(monkeypatch):
    import serve
    monkeypatch.setattr(serve, '_avatar_set_complete', lambda aid: False)
    text = sim._supervisor_brief(_state(), 'nora', 1000)['text']
    assert 'Avatar images missing: ruth' in text
    assert 'generate_agent_avatar' in text


def test_brief_names_no_sprint_when_none_is_active():
    state = _state()
    state['sprints']['s1']['status'] = 'closed'
    text = sim._supervisor_brief(state, 'nora', 1000)['text']
    assert 'Sprint: none active for this team.' in text


def test_brief_step_mails_once_per_interval():
    state = _state()
    sim._supervisor_brief_step(state, 0, 1000)
    mailbox = state['agents']['nora']['mailbox']
    assert len(mailbox) == 1 and mailbox[0]['kind'] == 'supervisor_brief'
    # Inside the interval: no second copy.
    sim._supervisor_brief_step(state, 0, 1000 + 60_000)
    assert len(state['agents']['nora']['mailbox']) == 1
    # After the interval: a fresh brief.
    sim._supervisor_brief_step(
        state, 0, 1000 + sim.SUPERVISOR_BRIEF_INTERVAL_MS + 1)
    assert len(state['agents']['nora']['mailbox']) == 2


def test_brief_step_skips_teams_without_an_active_sprint():
    state = _state()
    sim._supervisor_brief_step(state, 0, 1000)
    assert state['agents']['ben']['mailbox'] == []
