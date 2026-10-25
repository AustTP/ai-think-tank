import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


def test_unpinned_issue_card_uses_round_robin(monkeypatch):
    state = {
        'agents': {'ada': {'id': 'ada', 'offDuty': True, 'busy': False,
                           'task': None, 'pairWith': None}},
        'agentRoster': [{'id': 'ada'}],
        'tasks': {},
        'sim': {'rr': {}},
        'teams': [],
    }
    pick = {'title': 'Categorize X articles', 'room': 'pressoffice',
            'issueKey': 'BEN-5', 'taskType': 'spike', 'priority': 1}
    monkeypatch.setattr(sim, '_eligible_candidates', lambda s, include: ['ada'])
    monkeypatch.setattr(sim, '_one_off_routing_applies', lambda p: False)
    monkeypatch.setattr(sim, '_feature_affinity', lambda s, a, p: 0)
    monkeypatch.setattr(sim, '_description_affinity', lambda s, a, p: 0)
    monkeypatch.setattr(sim, 'appear_from_outskirts', lambda *a: None)
    monkeypatch.setattr(sim.policies, 'apply_enabled', lambda: False)
    monkeypatch.setattr(sim, 'assign_task', lambda *a, **k: {'id': 'task-x'})
    got = sim._assign_due_item(state, pick, True, None,
                               {'pressoffice': {}}, 1_000_000)
    assert got == {'id': 'task-x'}


def test_report_path_satisfies_completion_evidence():
    assert sim._completion_has_evidence({}, {
        'taskType': 'report', 'reportPath': 'x-articles/REVIEWED.md',
        'note': 'Wrote the report.'})
    assert not sim._completion_has_evidence({}, {
        'taskType': 'report', 'note': 'Wrote the report.'})
