import copy
import json
import os
import time

import migrations
import policies
import python_delivery
import skill_prompts
import skill_scan
import state_updates
import story_tracking
import structured_edits
import url_evidence
import work_reuse


def test_structured_edits_valid_command():
    out = structured_edits.command(json.dumps({'edits': [
        {'path': 'a.py', 'old': None, 'new': 'x = 1\n'}]}))
    assert out.startswith('python -c') and 'base64.b64decode' in out


def test_url_evidence_observe_shapes():
    d = url_evidence.DiscoveredURLs({'a': ['see https://example.com/x,'], 'b': ('http://e.com/y',)})
    assert d.allows('https://example.com/x')
    assert not d.allows('ftp://example.com/x')


def test_state_updates_more_branches():
    # non-dict incoming tasks -> returned unchanged
    assert state_updates.preserve_guidance({}, {'tasks': []}) == {'tasks': []}
    # task absent from incoming -> skipped
    state_updates.preserve_guidance({'tasks': {'a': {'id': 'a'}}}, {'tasks': {}})
    # protected parent through reviewOf
    current = {'tasks': {'p': {'id': 'p', 'status': 'done', 'note': 'x'},
                         'c': {'id': 'c', 'reviewOf': 'p'}},
               'workQueue': [], 'agents': {}, 'playerInbox': []}
    incoming = {'tasks': {'p': {'id': 'p', 'status': 'working'},
                          'c': {'id': 'c', 'reviewOf': 'p'}},
                'workQueue': [], 'agents': {}, 'playerInbox': []}
    out = state_updates.preserve_guidance(current, incoming)
    assert out['tasks']['c']['reviewOf'] == 'p'
    # cycle in belongs
    cyc = {'tasks': {'c': {'id': 'c', 'reviewOf': 'c'}}}
    state_updates.preserve_guidance(cyc, {'tasks': {'c': {'id': 'c', 'reviewOf': 'c'}},
                                          'workQueue': [], 'agents': {}, 'playerInbox': []})


def test_skill_scan_duplicate_and_format():
    assert skill_scan.scan_skill_text('')['summary'] == 'No static skill-safety findings.'
    rep = skill_scan.scan_skill_text('sudo\nsudo')
    assert skill_scan.format_findings(rep)
    assert skill_scan.format_findings({'findings': []}) == ''


def test_skill_prompt_unreadable_and_empty(tmp_path, monkeypatch):
    assert skill_prompts.load_skill_prompt('missing', 'fb', skills_dir=tmp_path) == 'fb'
    (tmp_path / 'x.md').write_text('## Key facts\nx')
    monkeypatch.setattr(skill_prompts.Path, 'read_text', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    assert skill_prompts.load_skill('x', 'fb', skills_dir=tmp_path) == 'fb'


def test_python_delivery_required_and_errors(tmp_path):
    assert python_delivery.required({'title': 'write python'}) is True
    assert python_delivery.required({'title': 'design'}) is False
    empty = tmp_path / 'e'
    empty.mkdir()
    assert len(python_delivery.check(empty)) == 2


def test_work_reuse_more(tmp_path):
    assert work_reuse.validation_lock('s') is work_reuse.validation_lock('s')
    (tmp_path / '.coverage').write_text('x')
    (tmp_path / 'a.py').write_text('x')
    assert work_reuse.tree_digest(tmp_path)
    work_reuse.save_check('k', {'ok': True})
    work_reuse.save_check('k', {'ok': True})
    assert work_reuse.get_check('k')['cached'] is True
    assert 'human guidance' in work_reuse.compact({'instructions': 'Story: x\n\nHuman guidance: y'}).lower()
    for i in range(130):
        work_reuse.doc_save(f'd{i}', {'x': i})
    assert work_reuse.doc_get('d129')['x'] == 129


def test_story_tracking_costs_refresh_exhausted_status_closed():
    state = {'tasks': {'s': {'id': 's', 'budgetUsd': 1, 'assignedTo': 'a'},
                       't': {'id': 't', 'reviewOf': 's', 'assignedTo': 'a'}},
             'teams': [{'id': 'team', 'directorId': 'a', 'scrumMasterId': 'm'}],
             'agentRoster': [{'id': 'a', 'director': 'team'}],
             'backlogRequests': [{'id': 'r', 'scrumMasterId': 'm', 'requestBudgetUsd': 2}],
             'workQueue': [{'id': 'q'}]}
    ledger = {'__task__/t': {'used': 2, 'calls': 3}, '__task__/request/r': {'used': 1, 'calls': 2}}
    view = story_tracking.refresh(state, ledger)
    assert view['stories']['s']['used'] == 2
    assert story_tracking.exhausted(state, ledger, 't') is True
    assert story_tracking.exhausted(state, ledger, 'request/r') is False
    assert story_tracking.budget_status(state, ledger, 't')['scope'] in ('request', 'story')
    assert story_tracking.closed({'tasks': {'s': {'id': 's', 'status': 'cancelled'}}}, 's') is True


def test_policies_route_and_retry():
    assert policies.choose_route('code', 'code')['band'] == 'coding'
    assert policies.choose_route('status ping')['band'] == 'low'
    assert policies.choose_route('x', best_effort=True)['best_of'] == 3
    assert policies.choose_route('plan', allow_high=True)['band'] == 'mid'
    assert policies.policy_mode() in ('shadow', 'apply')
    assert policies.apply_enabled() in (True, False)
