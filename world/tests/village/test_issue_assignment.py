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


def test_recorded_noop_satisfies_completion_evidence():
    assert sim._completion_has_evidence({}, {
        'taskType': 'code', 'noop': True, 'note': 'No feeds configured yet.'})
    assert not sim._completion_has_evidence({}, {
        'taskType': 'code', 'note': 'No feeds configured yet.'})


def test_distill_and_skill_review_notes_are_completion_evidence():
    # Distill/skill-review cards are queued as taskType 'code' but their
    # deliverable is the wiki / the review decision, not a Library file.
    assert sim._completion_has_evidence({}, {
        'taskType': 'code', 'distill': True, 'note': 'Merged 4 findings into the wiki.'})
    assert sim._completion_has_evidence({}, {
        'taskType': 'code', 'skillReview': True, 'note': '1 promoted, 0 rejected'})
    assert not sim._completion_has_evidence({}, {
        'taskType': 'code', 'distill': True, 'note': ''})


def test_scoped_issue_card_does_not_get_global_room_focus(monkeypatch):
    state = {
        'agents': {'ada': {'id': 'ada', 'offDuty': True, 'busy': False,
                           'task': None, 'pairWith': None}},
        'agentRoster': [{'id': 'ada'}],
        'tasks': {},
        'sim': {'rr': {}},
        'teams': [],
        'consensusRelay': {'consensus': 'Focus next work on observatory (weak trailing grade).'},
    }
    pick = {'title': 'Review the X articles', 'room': 'pressoffice',
            'issueKey': 'BEN-6', 'playerAuthored': True,
            'taskType': 'spike', 'priority': 1,
            'instructions': 'Save the briefing from library/x-articles.'}
    captured = {}
    monkeypatch.setattr(sim, '_eligible_candidates', lambda s, include: ['ada'])
    monkeypatch.setattr(sim, '_one_off_routing_applies', lambda p: False)
    monkeypatch.setattr(sim, '_feature_affinity', lambda s, a, p: 0)
    monkeypatch.setattr(sim, '_description_affinity', lambda s, a, p: 0)
    monkeypatch.setattr(sim, 'appear_from_outskirts', lambda *a: None)
    monkeypatch.setattr(sim.policies, 'apply_enabled', lambda: False)
    def fake_assign(*args, **kwargs):
        captured['instructions'] = args[4]
        return {'id': 'task-x'}
    monkeypatch.setattr(sim, 'assign_task', fake_assign)
    got = sim._assign_due_item(state, pick, True, None,
                               {'pressoffice': {}}, 1_000_000)
    assert got == {'id': 'task-x'}
    assert 'observatory' not in captured['instructions']
    assert 'library/x-articles' in captured['instructions']


def test_senior_director_can_take_watchdog_research(monkeypatch):
    state = {
        'agents': {'nora': {'id': 'nora', 'offDuty': False, 'busy': False,
                            'task': None, 'pairWith': None}},
        'agentRoster': [{'id': 'nora', 'name': 'Nora', 'isDirector': True}],
        'tasks': {}, 'sim': {'rr': {}}, 'teams': [],
    }
    pick = {'title': 'Investigate sim-loop watchdog alert', 'room': None,
            'taskType': 'research', 'assignedTo': 'nora',
            '_mailResume': True, 'goal': 'sim-watchdog',
            'instructions': 'Find the root cause.'}
    seen = {}

    def fake_assign(*args, **kwargs):
        seen['agent'] = args[1]
        seen['taskType'] = args[6].get('taskType') if len(args) > 6 else None
        return {'id': 'task-alert'}

    monkeypatch.setattr(sim, '_eligible_candidates', lambda s, include: ['nora'])
    monkeypatch.setattr(sim, '_resolve_assignment_room', lambda p: 'pressoffice')
    monkeypatch.setattr(sim, 'assign_task', fake_assign)
    got = sim._assign_due_item(state, pick, True, None,
                               {'pressoffice': {}}, 1_000_000)
    assert got == {'id': 'task-alert'}
    assert seen['agent'] == 'nora'
    assert seen['taskType'] == 'research'
