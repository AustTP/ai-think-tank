import completion_watchdog as cw


def test_helpers():
    assert cw.budget_only_stops({'approvalPreset': 'always', 'budgetOnlyStops': True})
    task = {'id': 't'}
    cw.block(task, 'reason', 1)
    assert task['status'] == 'blocked'
    cw.terminalize(task, 'done', 2)
    assert task['status'] == 'cancelled'


def test_step_observe_and_empty():
    cw.step({})
    state = {'tasks': {'a': {'id': 'a', 'status': 'working'}}}
    cw.observe(state)
    assert state['tasks']['a']['deliveryState'] == 'actively_working'


def test_blocked_exhausted_and_terminal(monkeypatch):
    state = {'tasks': {'a': {'id': 'a', 'status': 'blocked',
                             'blockedReason': 'completion_recovery',
                             'blockedAt': 1, 'completionRecovery': {'attempts': 3}}}}
    cw.step(state, now_ms=2, observe_only=False)
    assert state['tasks']['a']['status'] == 'cancelled'


def test_failed_repair_running(monkeypatch):
    import sim
    state = {'tasks': {
        'p': {'id': 'p', 'status': 'failed', '_peerRepairPending': True,
              'completionControl': {}},
        'c': {'id': 'c', 'reviewOf': 'p', 'status': 'working', 'taskType': 'code'},
    }}
    cw.step(state, now_ms=1, observe_only=False)
    assert state['tasks']['p']['status'] == 'repairing'


def test_investigation_completed_and_replacements():
    state = {'tasks': {
        'i': {'id': 'i', 'status': 'investigating', 'investigationOf': 'x',
              'createdAt': 1},
        'c': {'id': 'c', 'investigationOf': 'i', 'status': 'done',
              'libraryPath': 'r', 'taskType': 'spike', 'createdAt': 2},
        'old': {'id': 'old', 'status': 'working', 'investigationOf': 'x', 'createdAt': 0},
        'new': {'id': 'new', 'status': 'done', 'investigationOf': 'x',
                'libraryPath': 'r2', 'createdAt': 3},
    }}
    cw.step(state, now_ms=1, observe_only=False)
    assert state['tasks']['i']['status'] in ('done', 'cancelled')


def test_validation_branch(monkeypatch):
    import sys, types
    fake = types.ModuleType('code_director')
    fake.validation_progress = lambda s, t: ('state', 'next')
    monkeypatch.setitem(sys.modules, 'code_director', fake)
    state = {'tasks': {'a': {'id': 'a', 'status': 'working'}}}
    cw.step(state, now_ms=1)
    assert state['tasks']['a']['deliveryState'] == 'state'


def test_recovery_owner_missing_and_budget():
    state = {'tasks': {'a': {'id': 'a', 'status': 'repairing', 'assignedTo': 'ghost'}}}
    cw.step(state, now_ms=1, observe_only=False)
    assert state['tasks']['a']['blockedReason'] == 'completion_recovery'
    state = {'tasks': {'a': {'id': 'a', 'status': 'repairing', 'assignedTo': 'x',
                             'budgetExhausted': True}}, 'agents': {'x': {}}}
    cw.step(state, now_ms=1, observe_only=False)
    assert 'budget' in state['tasks']['a']['failNote'].lower()


def test_missing_inputs_and_invalid_response(monkeypatch):
    import sim
    state = {'tasks': {'a': {'id': 'a', 'status': 'repairing', 'assignedTo': 'x',
                             'reviewFailureKind': 'missing_inputs'}}, 'agents': {'x': {}}}
    cw.step(state, now_ms=1, observe_only=False)
    assert state['tasks']['a']['status'] == 'blocked'
    monkeypatch.setattr(sim, '_enter_peer_review', lambda *a, **k: None)
    state = {'tasks': {'a': {'id': 'a', 'status': 'repairing', 'assignedTo': 'x',
                             'reviewFailureKind': 'invalid_response'}}, 'agents': {'x': {}}}
    cw.step(state, now_ms=1, observe_only=False)
    assert state['tasks']['a']['status'] in ('needs_review', 'blocked')


def test_repair_queue_and_block(monkeypatch):
    import sim
    monkeypatch.setattr(sim, '_queue_peer_repair', lambda *a, **k: None)
    state = {'tasks': {'a': {'id': 'a', 'status': 'repairing', 'assignedTo': 'x'}},
             'agents': {'x': {}}}
    cw.step(state, now_ms=1, observe_only=False)
    assert state['tasks']['a']['status'] in ('repairing', 'blocked')


def test_can_start_and_spike_repair():
    assert cw.can_start({'tasks': {}, 'completionControl': {'enabled': False}}, {})
    assert not cw.can_start({'tasks': {'p': {'id': 'p', 'budgetExhausted': True}}}, {'reviewOf': 'p'})
    assert cw.complete_spike_repair({'taskType': 'spike'}, {'ok': True, 'libraryPath': 'x', 'note': 'n'}, 1)
    assert not cw.complete_spike_repair({'taskType': 'spike', 'manualAttention': True},
                                        {'ok': True, 'libraryPath': 'x', 'note': 'n'}, 1)
