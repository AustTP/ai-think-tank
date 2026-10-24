import types

import completion_watchdog as cw


def _control(**kw):
    c = {'enabled': True, 'maxRecoveryAttempts': 3, 'maxRecoveriesPerTick': 5,
         'retryBaseMs': 10, 'recoverHistorical': True}
    c.update(kw); return c


def test_replacements_and_backoff(monkeypatch):
    state = {'tasks': {
        'i': {'id': 'i', 'status': 'investigating', 'investigationOf': 'x', 'createdAt': 5},
        'r': {'id': 'r', 'status': 'done', 'investigationOf': 'x', 'libraryPath': 'p', 'createdAt': 6},
    }, 'completionControl': _control(), 'agents': {}}
    cw.step(state, now_ms=10, observe_only=False)
    assert state['tasks']['i']['status'] == 'cancelled'
    # backoff
    state = {'tasks': {'a': {'id': 'a', 'status': 'repairing', 'assignedTo': 'w',
                             'completionRecovery': {'attempts': 1, 'nextAttemptAt': 100}}},
             'agents': {'w': {}}, 'completionControl': _control()}
    cw.step(state, now_ms=10, observe_only=False)
    assert 'backoff' in state['tasks']['a']['nextAction']


def test_investigating_and_format_and_budget(monkeypatch):
    import sim, recovery_staffing
    monkeypatch.setattr(sim, '_queue_peer_repair', lambda *a: None)
    monkeypatch.setattr(recovery_staffing, 'step', lambda s: None)
    monkeypatch.setattr(cw, 'related', lambda s, t: ([], []))
    state = {'tasks': {'a': {'id': 'a', 'status': 'investigating', 'assignedTo': 'w',
                             'completionRecovery': {'attempts': 0}}},
             'agents': {'w': {}}, 'completionControl': _control()}
    cw.step(state, now_ms=10, observe_only=False)
    assert state['tasks']['a']['status'] == 'blocked'
    # format failures
    state = {'tasks': {'a': {'id': 'a', 'status': 'repairing', 'assignedTo': 'w',
                             'reviewFailureKind': 'invalid_response',
                             '_peerGate': {'formatFailures': 3}}},
             'agents': {'w': {}}, 'completionControl': _control()}
    cw.step(state, now_ms=10, observe_only=False)
    assert 'formatting' in state['tasks']['a']['failNote']
    # can_start false
    monkeypatch.setattr(cw, 'can_start', lambda *a: False)
    state = {'tasks': {'a': {'id': 'a', 'status': 'repairing', 'assignedTo': 'w'}},
             'agents': {'w': {}}, 'completionControl': _control()}
    cw.step(state, now_ms=10, observe_only=False)
    assert 'budget' in state['tasks']['a']['failNote'].lower()


def test_can_start_limits():
    state = {'completionControl': {'enabled': True, 'maxConcurrentStories': 1},
             'tasks': {'a': {'id': 'a', 'status': 'working'}}, 'backlogRequests': []}
    assert cw.can_start(state, {'id': 'b'}) is False
    state['completionControl']['enabled'] = False
    assert cw.can_start(state, {'id': 'b'}) is True
    state['backlogRequests'] = [{'id': 'r', 'requestBudgetUsd': 1}]
    state['storyCostTracking'] = {'requests': {'r': {'used': 1}}}
    assert cw.can_start(state, {'id': 'b', 'requestId': 'r'}) is False
