"""Scrum-master workflow memory: learn, promote, reuse, demote."""
import sim


def _base_state():
    return {
        'agentRoster': [
            {'id': 'sm', 'name': 'Sam'},
            {'id': 'nora', 'name': 'Nora', 'isDirector': True},
        ],
        'teams': [
            {'id': 'nora', 'directorId': 'nora', 'scrumMasterId': 'sm'},
        ],
        'agents': {'sm': {'id': 'sm'}, 'nora': {'id': 'nora'}},
        'backlogRequests': [],
        'tasks': {},
        'workQueue': [],
    }


def _request(rid, goal, team='nora', status='accepted'):
    return {'id': rid, 'goal': goal, 'title': goal, 'teamId': team,
            'status': status, 'filedBy': 'theo'}


def _task(tid, rid, title='Deliver the requested artifact', status='done',
          task_type='report', budget=1.0):
    return {'id': tid, 'requestId': rid, 'title': title, 'status': status,
            'taskType': task_type, 'sequence': 0, 'budgetUsd': budget,
            'createdAt': 1}


def _sweep(state):
    sim._workflow_template_sweep(state, 1_000_000, archived={})


def _learned_state():
    state = _base_state()
    goal = ('Research and write a 300-word cited brief on repair-card loops. '
            'Use at least three sources. End with one recommendation. '
            'Save it under admin/repair-loop-brief.md.')
    state['backlogRequests'] = [_request('r1', goal)]
    state['tasks'] = {'t1': _task('t1', 'r1')}
    _sweep(state)
    return state


def test_first_success_is_a_candidate():
    state = _learned_state()
    templates = list(state['workflowTemplates'].values())
    assert len(templates) == 1
    assert templates[0]['status'] == 'candidate'
    assert templates[0]['successCount'] == 1
    assert templates[0]['steps'][0]['type'] == 'report'


def test_second_success_promotes_to_active():
    state = _learned_state()
    goal2 = ('Research and write a 300-word cited brief on retry storms. '
             'Use at least three sources. End with one recommendation. '
             'Save it under admin/retry-brief.md.')
    state['backlogRequests'].append(_request('r2', goal2))
    state['tasks']['t2'] = _task('t2', 'r2')
    _sweep(state)
    template = next(iter(state['workflowTemplates'].values()))
    assert template['status'] == 'active'
    assert template['successCount'] == 2


def test_single_deliverable_goal_ignores_a_historical_multicard_shape():
    state = _base_state()
    goal = ('Research and write a 300-word cited brief on repair-card loops. '
            'Use at least three sources. End with one recommendation. '
            'Save it under admin/repair-loop-brief.md.')
    state['backlogRequests'] = [_request('r1', goal)]
    state['tasks'] = {
        't1': _task('t1', 'r1', title='Gather sources'),
        't2': _task('t2', 'r1', title='Write the brief'),
        't3': _task('t3', 'r1', title='Save the brief'),
        't4': _task('t4', 'r1', title='Review: Save the brief'),
        't5': _task('t5', 'r1', title='Fix: Save the brief'),
    }
    _sweep(state)
    template = next(iter(state['workflowTemplates'].values()))
    assert len(template['steps']) == 1
    assert template['steps'][0]['type'] == 'report'


def test_spike_and_report_deliverables_share_a_template():
    state = _learned_state()
    goal2 = ('Research and write a 300-word cited brief on retry storms. '
             'Use at least three sources. End with one recommendation.')
    state['backlogRequests'].append(_request('r2', goal2))
    state['tasks']['t2'] = _task('t2', 'r2', task_type='spike')
    _sweep(state)
    templates = list(state['workflowTemplates'].values())
    assert len(templates) == 1
    assert templates[0]['status'] == 'active'
    assert templates[0]['steps'][0]['type'] == 'report'


def test_match_is_topic_tolerant_but_not_for_code():
    state = _learned_state()
    state['backlogRequests'].append(_request(
        'r2', 'Research and write a 300-word cited brief on retry storms. '
        'Use at least three sources. End with one recommendation. '
        'Save it under admin/retry-brief.md.'))
    state['tasks']['t2'] = _task('t2', 'r2')
    _sweep(state)
    new_goal = ('Research and write a 300-word cited brief on agent memory. '
                'Use at least three sources. End with one recommendation. '
                'Save it under admin/memory-brief.md.')
    assert sim._workflow_match_template(state, new_goal, 'nora') is not None
    assert sim._workflow_match_template(state, 'Fix the parser bug', 'nora') is None


def test_breakdown_reuses_an_active_template_without_the_model():
    state = _learned_state()
    state['backlogRequests'].append(_request(
        'r2', 'Research and write a 300-word cited brief on retry storms. '
        'Use at least three sources. End with one recommendation.'))
    state['tasks']['t2'] = _task('t2', 'r2')
    _sweep(state)
    goal = ('Research and write a 300-word cited brief on agent memory. '
            'Use at least three sources. End with one recommendation. '
            'Save it under admin/memory-brief.md.')
    state['backlogRequests'].append(_request('r3', goal, status='pending'))
    pending = {'scrumMasterId': 'sm', 'teamId': 'nora',
               'reqIds': ['r3'], 'people': {}}
    def decider(*args):
        raise AssertionError('the model must not run when a template matches')
    sim._resolve_breakdown(state, pending, 2_000_000, decider=decider)
    card = next(q for q in state['workQueue'] if q.get('requestId') == 'r3')
    assert card['taskType'] == 'report'
    assert card['sequence'] == 0
    assert card['reportPath'] == 'admin/memory-brief.md'
    assert card['templateId'].startswith('wft-')
    req = next(r for r in state['backlogRequests'] if r['id'] == 'r3')
    assert req['workflowTemplateId'] == card['templateId']


def test_cross_team_reuse_is_counted():
    state = _learned_state()
    state['backlogRequests'].append(_request(
        'r2', 'Research and write a 300-word cited brief on retry storms. '
        'Use at least three sources. End with one recommendation.'))
    state['tasks']['t2'] = _task('t2', 'r2')
    _sweep(state)
    state['teams'].append({'id': 'dax', 'directorId': 'dax', 'scrumMasterId': 'dax'})
    state['agentRoster'].append({'id': 'dax', 'name': 'Dax', 'isDirector': True})
    state['agents']['dax'] = {'id': 'dax'}
    goal = ('Research and write a 300-word cited brief on agent memory. '
            'Use at least three sources. End with one recommendation. '
            'Save it under admin/memory-brief.md.')
    state['backlogRequests'].append(_request('r3', goal, team='dax', status='pending'))
    sim._resolve_breakdown(state, {'scrumMasterId': 'dax', 'teamId': 'dax',
                                   'reqIds': ['r3'], 'people': {}}, 2_000_000,
                           decider=lambda *a: None)
    state['workQueue'] = []  # assignment popped the card
    state['tasks']['t3'] = _task('t3', 'r3')
    state['tasks']['t3']['teamId'] = 'dax'
    _sweep(state)
    template = next(iter(state['workflowTemplates'].values()))
    assert template['crossTeamSuccess'] == 1
    assert 'dax' in template['usingTeams']


def test_two_failures_retire_a_template():
    state = _learned_state()
    template = next(iter(state['workflowTemplates'].values()))
    template['status'] = 'active'
    template['successCount'] = 2
    tid = template['id']
    state['backlogRequests'].append(_request('r4', 'Goal four'))
    state['backlogRequests'][-1]['workflowTemplateId'] = tid
    state['tasks']['t4'] = _task('t4', 'r4', status='failed')
    _sweep(state)
    assert template['failureCount'] == 1
    state['backlogRequests'].append(_request('r5', 'Goal five'))
    state['backlogRequests'][-1]['workflowTemplateId'] = tid
    state['tasks']['t5'] = _task('t5', 'r5', status='failed')
    _sweep(state)
    assert template['status'] == 'retired'
