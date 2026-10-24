import types
import governance as g


def test_roles_and_approvers():
    assert g._roster(None) == []
    assert g._entry({}, 'x') is None
    state = {'agentRoster': [
        {'id': 'admin', 'isAdmin': True},
        {'id': 'd', 'director': 'admin', 'scrumMaster': True},
        {'id': 'w', 'director': 'd'},
    ], 'teams': [{'scrumMasterId': 'd'}]}
    assert g.is_admin(state, 'admin') and not g.is_admin(state, 'x')
    assert g.direct_reports(state, 'd') == ['w']
    assert g.is_director(state, 'd') and not g.is_director(state, '')
    assert g.is_scrum_master(state, 'd') and not g.is_scrum_master(state, '')
    assert not g.may_work_code(state, 'd') and g.may_work_code(state, 'w')
    assert g.may_grant_entitlements(state, 'd')
    assert g.admin_id(state) == 'admin'
    assert g.change_request_approvers(state, 'w') == ('d', 'admin')
    assert g.change_request_approvers({'agentRoster': [{'id': 'd', 'isAdmin': True, 'director': 'd'}]}, 'd') == (None, 'd')


def test_supervision_and_project():
    assert g.project_key(None) == '_default'
    assert g.project_key({'projectId': 1}) == '1'
    assert g.project_key({'teamId': 't'}) == 't'
    assert g.supervision_level({}, 'd') == 3
    assert g.supervision_level({'directorSupervision': {'d': {'p': 99}}}, 'd', 'p') == 5
    assert g.supervision_level({'directorSupervision': {'d': {'p': 'bad'}}}, 'd', 'p') == 3
    try:
        g.set_supervision_level({}, 'd', 'x')
    except ValueError:
        pass
    try:
        g.set_supervision_level({}, 'd', 9)
    except ValueError:
        pass
    state = {}
    assert g.set_supervision_level(state, 'd', 2, 'p')['p'] == 2
    assert g._task_map({'tasks': [{'id': 'a'}, 'x']}) == {'a': {'id': 'a'}}
    assert g._task_map({}) == {}


def test_supervisor_tick(monkeypatch):
    state = {'tasks': {'a': {'id': 'a', 'status': 'failed', 'updatedAt': 95000,
                             'title': 'broken', 'teamId': 't', 'taskType': 'code'}},
             'agentRoster': [{'id': 'd'}], 'directorSupervision': {'t': {'_default': 4}},
             'automation': {'mode': 'run'}, 'lastSupervisorScanAt': 0}
    fake = types.ModuleType('sim')
    fake._task_owner_director = lambda s, t: 'd'
    fake.on_call_agent = lambda *a: 'w'
    fake.queue_work = lambda s, q: s.setdefault('workQueue', []).extend(q)
    monkeypatch.setitem(__import__('sys').modules, 'sim', fake)
    assert g.supervisor_tick(state, now_ms=100000) == 1
    assert state['tasks']['a']['blockerBugId']
    assert g.supervisor_tick(state, now_ms=100000) == 0
    # mode drain and cooldown
    state['automation']['mode'] = 'drain'
    assert g.supervisor_tick(state, now_ms=200000) == 0
    # already queued
    state['automation']['mode'] = 'run'; state['lastSupervisorScanAt'] = 0
    state['tasks']['a'].pop('blockerBugId')
    state['workQueue'] = [{'title': 'Blocker: broken', 'status': 'queued'}]
    assert g.supervisor_tick(state, now_ms=300000) == 0
    # no on-call
    state['workQueue'] = []
    fake.on_call_agent = lambda *a: None
    assert g.supervisor_tick(state, now_ms=400000) == 0
    # levels suppress
    state['lastSupervisorScanAt'] = 0
    state['directorSupervision']['t']['_default'] = 0
    assert g.supervisor_tick(state, now_ms=500000) == 0
    assert g.admin_change_request_enabled({'governance': {'changeRequestAdminApproval': True}})


def test_governance_more(monkeypatch):
    assert g.is_scrum_master({'agentRoster': [{'id': 's', 'isScrumMaster': True}]}, 's')
    assert g.may_self_edit({}, 'w')
    assert g.admin_id({}) is None
    assert g.project_key({'projectId': '', 'projectLabel': '', 'teamId': ''}) == '_default'
    state = {'tasks': {
        'done': {'id': 'done', 'status': 'done'},
        'b': {'id': 'b', 'status': 'blocked'},
        'c': {'id': 'c', 'status': 'working', 'dependsOn': 'b'},
        'd': {'id': 'd', 'status': 'working', 'dependsOn': ['b']},
        'bug': {'id': 'bug', 'status': 'failed', 'taskType': 'bug'},
        'rev': {'id': 'rev', 'status': 'failed', 'reviewOf': 'x'},
        'known': {'id': 'known', 'status': 'failed', 'blockerBugId': 'x'},
    }}
    assert g._blocking_task_ids(state) == {'b': 2}
    # import failure
    monkeypatch.setitem(__import__('sys').modules, 'sim', None)
    assert g.supervisor_tick({'tasks': {'a': {'id': 'a', 'status': 'failed'}},
                              'automation': {'mode': 'run'}}, now_ms=1) == 0
