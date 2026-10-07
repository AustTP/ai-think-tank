"""Persistent code director and mandatory validation before independent review."""
import time


def create_director(state):
    aid = 'code-director'
    if aid in (state.get('agents') or {}):
        return aid
    profile = {'mission': 'Repair and validate implementation work before independent peer review.',
               'instructions': [
                   'Inspect the existing implementation; preserve the author\'s requirements.',
                   'Fix syntax errors, undefined names, imports, type errors, security findings, and failing tests.',
                   'Run the actual quality pipeline and include its output. Never claim a pass without evidence.',
                   'Send validated code to independent reviewers; do not approve your own repairs.',
               ], 'notes': []}
    definition = {'id': aid, 'name': 'Felix', 'role': 'Code Director', 'color': '#3b82f6',
                  'model': 'small', 'isDirector': True, 'codeDirector': True,
                  'villageId': 'main', 'profile': profile, 'approvedCount': 0,
                  'droppedCount': 0, 'weekApprovals': 0, 'mailbox': []}
    state.setdefault('agentRoster', []).append(definition)
    state.setdefault('agents', {})[aid] = {
        **definition, 'x': 510.0, 'y': 272.0, 'dir': 'south', 'offDuty': True,
        'visible': False, 'busy': False, 'task': None, 'inRoom': None,
        'roomX': None, 'roomY': None, 'meetingId': None, 'conversationLog': [],
        'hiredAt': int(time.time()*1000),
    }
    return aid


def queue_check(state, parent, failure=None):
    import sim
    directors = [d['id'] for d in state.get('agentRoster') or []
                 if d.get('codeDirector') and (state.get('agents') or {}).get(d['id'])]
    if not directors or not sim._peer_coding_class(parent):
        return None
    parent.pop('_codeDirectorPassed', None)
    if not parent.get('_peerGate'):
        parent['_peerGate'] = {'approvals': 0, 'approvers': [], 'reviewerIds': [],
                               'cycleCount': 0, 'escalated': False}
    gate = parent['_peerGate']
    if gate.get('escalated'):
        return None
    parent['status'] = 'blocked'
    parent['blockedReason'] = 'code_director_check'
    parent['_codeDirectorPending'] = True
    pending = [q for q in state.get('workQueue') or [] if q.get('reviewOf') == parent['id']
               and q.get('taskType') == 'code_preflight']
    active = [t for t in (state.get('tasks') or {}).values() if t.get('reviewOf') == parent['id']
              and t.get('taskType') == 'code_preflight' and t.get('status') in ('walking', 'working')]
    if not pending and not active:
        sim.queue_work(state, [{
            'title': f'Code check: {parent.get("title")}', 'room': parent.get('room'),
            'taskType': 'code_preflight', 'assignedTo': directors[0],
            'reviewOf': parent['id'], 'reviewAuthorId': parent.get('assignedTo'),
            'projectLabel': parent.get('projectLabel') or parent.get('title'),
            'productId': parent.get('productId'), 'userStory': parent.get('userStory'),
            'acceptanceCriteria': parent.get('acceptanceCriteria'), 'priority': 'high',
            'budgetUsd': parent.get('budgetUsd'),
            'instructions': (f"{parent.get('instructions') or ''}\n\nInspect and repair the existing code "
                             'before peer review. Fix syntax, imports, undefined names, type errors, '
                             'security findings and failing tests. Preserve scope. Run the quality pipeline '
                             f"and report its real output. Latest failure: {failure or parent.get('failNote') or 'none recorded'}"),
        }])
    return gate


def complete_check(state, task, result, now_ms):
    import sim
    parent = (state.get('tasks') or {}).get(task.get('reviewOf'))
    if (not parent or (parent.get('_peerGate') or {}).get('escalated')
            or not result.get('ok') or not result.get('pipelineOk') or not result.get('evidence')):
        return False
    parent['_codeDirectorPassed'] = {'taskId': task['id'], 'atMs': now_ms,
                                     'evidence': result['evidence']}
    parent.pop('_codeDirectorPending', None)
    parent.pop('blockedReason', None)
    sim._enter_peer_review(state, parent, now_ms)
    return True
