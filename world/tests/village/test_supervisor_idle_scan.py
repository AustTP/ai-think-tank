import sim


def _mk(monkeypatch, delivered):
    monkeypatch.setattr(sim, 'automation_paused', lambda s: False)
    monkeypatch.setattr(sim, '_deliver_supervisor_brief',
                        lambda s, t, n: delivered.append(t) or True)
    monkeypatch.setattr(sim, '_supervisor_attention_reason', lambda *a: '')
    monkeypatch.setattr(sim, 'queue_work', lambda s, q: None)
    return {'teams': [{'id': 't', 'name': 'Team'}], 'sprints': {},
            'agents': {'d': {}}, 'agentRoster': [], '_supervisorCardAt': {}}


def test_hourly_scan_without_sprint_when_workers_active(monkeypatch):
    """Operator floor 2026-11-06: a team with active workers and no sprint
    still gets a director scan (hourly), not silence."""
    delivered = []
    state = _mk(monkeypatch, delivered)
    state['tasks'] = {'task-1': {'teamId': 't', 'status': 'working'}}
    sim._supervisor_brief_step(state, 0, 10 ** 9)
    assert delivered == ['t'], delivered
    # within the hour: no second brief
    sim._supervisor_brief_step(state, 0, 10 ** 9 + 30 * 60 * 1000)
    assert delivered == ['t']
    # after the hour: brief again
    sim._supervisor_brief_step(state, 0, 10 ** 9 + 61 * 60 * 1000)
    assert delivered == ['t', 't']


def test_no_scan_without_sprint_and_without_workers(monkeypatch):
    delivered = []
    state = _mk(monkeypatch, delivered)
    state['tasks'] = {'task-1': {'teamId': 't', 'status': 'done'}}
    sim._supervisor_brief_step(state, 0, 10 ** 9)
    assert delivered == []


def test_sprint_keeps_fast_cadence(monkeypatch):
    delivered = []
    state = _mk(monkeypatch, delivered)
    state['sprints'] = {'s': {'status': 'active', 'teamIds': ['t']}}
    state['tasks'] = {}
    sim._supervisor_brief_step(state, 0, 10 ** 9)
    assert delivered == ['t']
    # 15-min cadence: a second brief after 16 minutes
    sim._supervisor_brief_step(state, 0, 10 ** 9 + 16 * 60 * 1000)
    assert delivered == ['t', 't']
