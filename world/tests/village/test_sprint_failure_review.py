"""Sprint failure review: at close the owning director root-causes every card
that failed and files an on-call bug card when the cause is a real defect.

Pure state tests -- no model call and no database. The decider is injected and
governance logging is silenced.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402

TEAM = 'team-a'
DIRECTOR = 'nora'
ADMIN = 'theo'


def _state():
    return {
        'agents': {
            DIRECTOR: {'busy': False, 'offDuty': False},
            ADMIN: {'busy': False, 'offDuty': False},
            'ben': {'busy': False, 'offDuty': False},
            'cora': {'busy': True, 'offDuty': False},
        },
        'agentRoster': [
            {'id': DIRECTOR, 'name': 'Nora', 'isDirector': True},
            {'id': ADMIN, 'name': 'Theo', 'isAdmin': True},
            {'id': 'ben', 'name': 'Ben', 'director': DIRECTOR},
            {'id': 'cora', 'name': 'Cora', 'director': DIRECTOR},
        ],
        'teams': [{'id': TEAM, 'directorId': DIRECTOR,
                   'members': ['ben', 'cora']}],
        'sprints': {'s1': {'id': 's1', 'name': 'Sprint 1', 'teamIds': [TEAM],
                           'items': [['Card A', 'pressoffice'],
                                     ['Card B', 'pressoffice']]}},
        'tasks': {
            't1': {'id': 't1', 'title': 'Card A', 'room': 'pressoffice',
                   'status': 'failed', 'sprintId': 's1', 'assignedTo': 'ben',
                   'failNote': 'KeyError: value', 'failedAt': 1},
            't2': {'id': 't2', 'title': 'Card B', 'room': 'pressoffice',
                   'status': 'failed', 'sprintId': 's1', 'assignedTo': 'ben',
                   'failNote': 'provider timed out', '_infraFailure': True,
                   'failedAt': 2},
            't3': {'id': 't3', 'title': 'Card C', 'room': 'pressoffice',
                   'status': 'done', 'sprintId': 's1'},
        },
        'issues': {},
        'issueCounters': {},
        'backlogRequests': [],
        'workQueue': [],
        '_oncallOrder': {TEAM: ['ben', 'cora']},
        'pendingFailureReviews': {
            's1': {'at': 0, 'sprintId': 's1', 'teamIds': [TEAM]},
        },
    }


def _silence_log(monkeypatch):
    monkeypatch.setattr(sim, '_log_governance', lambda *a, **k: None)


def _decider(mapping):
    def decide(state, instructions, sprint_id, failures):
        return {'findings': [
            {'taskId': f['id'], 'cause': mapping.get(f['id'], 'unknown'),
             'why': 'because'}
            for f in failures]}
    return decide


def test_sprint_failures_only_failed_cards_from_the_sprint(monkeypatch):
    state = _state()
    _silence_log(monkeypatch)
    failures = sim._sprint_failures(state, 's1')
    assert [f['id'] for f in failures] == ['t1', 't2']
    # A card already reviewed for this sprint is not reviewed twice.
    state['tasks']['t1']['_failureReviewSprint'] = 's1'
    assert [f['id'] for f in sim._sprint_failures(state, 's1')] == ['t2']


def test_review_files_a_bug_and_assigns_the_oncall(monkeypatch):
    state = _state()
    _silence_log(monkeypatch)
    sim._failure_review_step(state, 0, 1000,
                             decider=_decider({'t1': 'bug', 't2': 'infra'}))
    assert 's1' not in state['pendingFailureReviews']
    review = state['failureReviews']['s1']
    assert review['directorId'] == DIRECTOR
    assert review['filedBugs'] == ['TEAMA-1']
    issue = state['issues']['TEAMA-1']
    assert issue['type'] == 'bug'
    assert issue['title'] == 'Bug: Card A'
    assert issue['rootCause'] == 'because'
    assert issue['failureOf'] == 't1'
    assert issue['sprintId'] == 's1'
    assert issue['fixAssignee'] in ('ben', 'cora')
    assert len(state['workQueue']) == 1
    card = state['workQueue'][0]
    assert card['issueKey'] == 'TEAMA-1'
    assert card['incident'] is True and card['directRoute'] is True
    assert card['teamId'] == TEAM
    # The request was placed by the director, so refinement must not re-groom it.
    assert state['backlogRequests'][0]['status'] == 'accepted'
    # Both failures were stamped, including the infra one that earned no card.
    assert state['tasks']['t1']['_failureReviewSprint'] == 's1'
    assert state['tasks']['t2']['_failureReview']['cause'] == 'infra'


def test_process_and_unknown_failures_get_no_card(monkeypatch):
    state = _state()
    _silence_log(monkeypatch)
    sim._failure_review_step(state, 0, 1000,
                             decider=_decider({'t1': 'process', 't2': 'unknown'}))
    assert state['failureReviews']['s1']['filedBugs'] == []
    assert state['issues'] == {}
    assert state['workQueue'] == []


def test_review_waits_while_the_director_is_busy(monkeypatch):
    state = _state()
    _silence_log(monkeypatch)
    state['agents'][DIRECTOR]['busy'] = True
    sim._failure_review_step(state, 0, 1000, decider=_decider({}))
    assert 's1' in state['pendingFailureReviews']
    assert 'failureReviews' not in state or 's1' not in state.get('failureReviews', {})
    # The review runs once a director is free again.
    state['agents'][DIRECTOR]['busy'] = False
    sim._failure_review_step(state, 0, 1001,
                             decider=_decider({'t1': 'bug', 't2': 'infra'}))
    assert 's1' not in state['pendingFailureReviews']


def test_rerunning_the_same_sprint_review_files_nothing_new(monkeypatch):
    state = _state()
    _silence_log(monkeypatch)
    sim._failure_review_step(state, 0, 1000,
                             decider=_decider({'t1': 'bug', 't2': 'infra'}))
    count = len(state['workQueue'])
    state['pendingFailureReviews']['s1'] = {'at': 0, 'sprintId': 's1',
                                            'teamIds': [TEAM]}
    sim._failure_review_step(state, 0, 2000,
                             decider=_decider({'t1': 'bug', 't2': 'infra'}))
    assert len(state['workQueue']) == count
    assert len(state['issues']) == 1


def test_no_failures_records_an_empty_review_without_a_model_call(monkeypatch):
    state = _state()
    _silence_log(monkeypatch)
    state['tasks'] = {'t3': state['tasks']['t3']}
    called = []

    def decider(*args, **kwargs):
        called.append(True)
        return {}

    sim._failure_review_step(state, 0, 1000, decider=decider)
    assert called == []
    assert state['failureReviews']['s1']['failures'] == []
    assert 's1' not in state['pendingFailureReviews']


def test_sprint_close_queues_the_failure_review(monkeypatch):
    state = _state()
    state.pop('pendingFailureReviews')
    for name in ('_rotate_served_oncalls', '_end_loans_for_team',
                 '_pull_backlog_for_team', 'kick_refinement_now',
                 '_team_health_review'):
        monkeypatch.setattr(sim, name, lambda *a, **k: None, raising=False)
    sim._on_sprint_closed(state, {'id': 's1', 'teamIds': [TEAM]}, 5000)
    queued = state['pendingFailureReviews']['s1']
    assert queued['at'] == 5000 + sim.FAILURE_REVIEW_DELAY_MS
    assert queued['teamIds'] == [TEAM]


def test_failure_cause_normalization():
    cases = {
        'Bug': 'bug', 'regression in parser': 'bug',
        'provider timeout': 'infra', 'network 503': 'infra',
        'unclear spec': 'process', 'no idea': 'unknown', '': 'unknown',
    }
    for raw, expected in cases.items():
        assert sim._normalize_failure_cause(raw) == expected
