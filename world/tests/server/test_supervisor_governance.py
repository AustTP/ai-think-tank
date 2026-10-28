
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import core.governance as governance


def _state():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'director': 'theo', 'isDirector': True},
            {'id': 'ada', 'director': 'nora'},
            {'id': 'ben', 'director': 'nora'},
            {'id': 'sam', 'director': 'nora'},
            {'id': 'scout', 'director': 'nora'},
        ],
        'teams': [
            {'id': 'theo', 'scrumMasterId': 'sam'},
        ],
    }


def test_admin_and_directors_cannot_write_code():
    st = _state()
    assert governance.is_director(st, 'theo')
    assert governance.is_director(st, 'nora')
    assert not governance.may_work_code(st, 'theo')
    assert not governance.may_work_code(st, 'nora')
    assert governance.may_work_code(st, 'ada')


def test_director_may_not_self_edit_but_worker_may():
    st = _state()
    assert not governance.may_self_edit(st, 'nora')
    assert governance.may_self_edit(st, 'ada')


def test_scrum_master_can_grant_entitlements():
    st = _state()
    assert governance.is_scrum_master(st, 'sam')
    assert governance.may_grant_entitlements(st, 'sam')
    assert not governance.may_grant_entitlements(st, 'ada')
    assert governance.may_grant_entitlements(st, 'theo')


def test_change_request_needs_director_and_admin():
    st = _state()
    director, admin = governance.change_request_approvers(st, 'ada')
    assert director == 'nora'
    assert admin == 'theo'
    assert director != admin


def test_supervisor_files_immediate_blocker_bug(monkeypatch):
    import sys
    import types
    state = {
        'tasks': {
            't1': {'id': 't1', 'title': 'Build UI', 'status': 'blocked',
                   'teamId': 'theo', 'taskType': 'code'},
            't2': {'id': 't2', 'title': 'Verify UI', 'status': 'queued',
                   'dependsOn': ['t1']},
        },
        'workQueue': [],
        'agentRoster': [{'id': 'theo', 'isAdmin': True}],
        'lastSupervisorScanAt': 0,
    }
    fake = types.SimpleNamespace(
        on_call_agent=lambda *a, **k: 'ada',
        queue_work=lambda st, items: (
            st.setdefault('workQueue', []).extend(items) or len(st['workQueue'])),
        _task_owner_director=lambda st, task: 'theo',
    )
    monkeypatch.setitem(sys.modules, 'sim', fake)
    assert governance.supervisor_tick(state, now_ms=120000) == 1
    assert state['workQueue'][0]['taskType'] == 'bug'
    assert state['workQueue'][0]['priority'] == 'high'
    assert state['workQueue'][0]['incident'] is True
    assert state['tasks']['t1']['blockerBugId']


def test_supervisor_cadence_does_not_spam(monkeypatch):
    import sys
    import types
    state = {
        'tasks': {'t1': {'id': 't1', 'title': 'Build UI', 'status': 'blocked',
                         'teamId': 'theo'},
                  't2': {'id': 't2', 'status': 'queued', 'dependsOn': ['t1']}},
        'workQueue': [], 'agentRoster': [], 'lastSupervisorScanAt': 0,
    }
    fake = types.SimpleNamespace(
        on_call_agent=lambda *a, **k: 'ada',
        queue_work=lambda st, items: st.setdefault('workQueue', []).extend(items),
        _task_owner_director=lambda st, task: 'theo',
    )
    monkeypatch.setitem(sys.modules, 'sim', fake)
    governance.supervisor_tick(state, now_ms=120000)
    assert governance.supervisor_tick(state, now_ms=150000) == 0


def test_supervision_level_per_project_and_default():
    st = {'directorSupervision': {'nora': {'_default': 2, 'feedback': 5}}}
    assert governance.supervision_level(st, 'nora', 'feedback') == 5
    assert governance.supervision_level(st, 'nora', 'other') == 2
    assert governance.supervision_level(st, 'theo', 'other') == governance.SUPERVISION_DEFAULT


def test_set_supervision_level_validates_and_stores():
    st = {}
    governance.set_supervision_level(st, 'nora', 4, 'feedback')
    assert governance.supervision_level(st, 'nora', 'feedback') == 4
    try:
        governance.set_supervision_level(st, 'nora', 6)
    except ValueError:
        pass
    else:
        raise AssertionError('level 6 must be refused')


def test_level_zero_suppresses_blocker_bug(monkeypatch):
    import sys
    import types
    st = {'tasks': {'t1': {'id': 't1', 'title': 'Build', 'status': 'blocked',
                           'teamId': 'nora'},
                    't2': {'id': 't2', 'status': 'queued', 'dependsOn': ['t1']}},
          'directorSupervision': {'nora': {'_default': 0}},
          'workQueue': [], 'agentRoster': [], 'lastSupervisorScanAt': 0}
    fake = types.SimpleNamespace(
        on_call_agent=lambda *a, **k: 'ada',
        queue_work=lambda s, items: s.setdefault('workQueue', []).extend(items),
        _task_owner_director=lambda s, task: 'nora',
    )
    monkeypatch.setitem(sys.modules, 'sim', fake)
    assert governance.supervisor_tick(st, now_ms=120000) == 0
    assert st['workQueue'] == []


def test_level_five_watches_recent_failure_without_dependents(monkeypatch):
    import sys
    import types
    st = {'tasks': {'t1': {'id': 't1', 'title': 'Build', 'status': 'failed',
                           'teamId': 'nora', 'updatedAt': 110000}},
          'directorSupervision': {'nora': {'_default': 5}},
          'workQueue': [], 'agentRoster': [], 'lastSupervisorScanAt': 0}
    fake = types.SimpleNamespace(
        on_call_agent=lambda *a, **k: 'ada',
        queue_work=lambda s, items: s.setdefault('workQueue', []).extend(items),
        _task_owner_director=lambda s, task: 'nora',
    )
    monkeypatch.setitem(sys.modules, 'sim', fake)
    assert governance.supervisor_tick(st, now_ms=120000) == 1
    assert st['workQueue'][0]['taskType'] == 'bug'


def test_constitution_protected_business_processes_editable(monkeypatch):
    import selfedit
    import serve
    # The actor must be a pure worker: a director is refused earlier by the
    # "directors do not write code" rule, which would mask the protected-path
    # check this test is about. Patch the roster so the test does not depend on
    # the live org chart (supervisors move).
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: {
        'agentRoster': [
            {'id': 'boss', 'isAdmin': True, 'isDirector': True},
            {'id': 'lead', 'isDirector': True, 'director': 'boss'},
            {'id': 'worker', 'director': 'lead'},
        ],
        'agents': {'worker': {}},
    })
    assert selfedit.is_protected('/repo/CONSTITUTION.md')
    assert selfedit.is_protected('/repo/world/constitution.py')
    real, display = selfedit._resolve_path('BUSINESS-PROCESSES.md')
    assert real and display == 'BUSINESS-PROCESSES.md'
    ok, payload = selfedit.propose('worker', 'CONSTITUTION.md', 'x', 'y', 'test')
    assert ok is False and 'PROTECTED' in payload['error']


def test_constitution_rules_are_injected_once():
    import core.constitution as constitution
    body = {'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}]}
    out = constitution.with_rules(body)
    assert out['messages'][0]['role'] == 'system'
    assert constitution.MARKER in out['messages'][0]['content']
    assert constitution.with_rules(out) is out
