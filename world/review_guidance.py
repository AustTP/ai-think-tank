"""Human interventions for frozen reviews; never substitutes for approval checks."""
import html
import json
import time
import urllib.parse

ACTIONS = {
    'pause': 'Pause while I decide',
    'retry': 'Retry with my instructions',
    'provide_inputs': 'Supply missing inputs and retry',
    'edit_scope': 'Change the scope and acceptance criteria',
    'reassign': 'Assign a different repair owner',
    'research': 'Ask the scrum master to investigate the blocker',
    'split': 'Split into smaller stories',
    'reject': 'Reject / stop this story',
}


def escalation_task_id(escalation, state):
    try:
        task_id = json.loads(escalation.get('note') or '{}').get('taskId')
    except (ValueError, AttributeError):
        task_id = None
    if task_id in (state.get('tasks') or {}):
        return task_id
    matches = [t['id'] for t in (state.get('tasks') or {}).values()
               if t.get('title') and f'"{t["title"]}"' in escalation.get('question', '')
               and (t.get('_peerGate') or {}).get('escalated')]
    return matches[0] if len(matches) == 1 else None


def render_form(task, action_url, agents):
    esc = html.escape
    options = ''.join(f'<option value="{a}">{esc(label)}</option>' for a, label in ACTIONS.items())
    owners = '<option value="">Keep current owner</option>' + ''.join(
        f'<option value="{esc(a)}">{esc(d.get("name") or a)}</option>'
        for a, d in agents.items())
    diagnosis = task.get('reviewDiagnosis') or {}
    recommended = {'formatting': 'retry', 'missing_inputs': 'provide_inputs'}.get(diagnosis.get('category'), 'research')
    return f'''<!doctype html><html><meta name="viewport" content="width=device-width">
    <title>Guide a stuck review</title><style>body{{max-width:800px;margin:30px auto;padding:15px;
    font:16px system-ui}}label{{display:block;margin:14px 0}}textarea,input,select{{box-sizing:border-box;
    width:100%;padding:8px}}pre{{white-space:pre-wrap}}button{{padding:12px}}</style>
    <h1>Guide: {esc(task.get('title') or task['id'])}</h1>
    <p>Task {esc(task['id'])}. No choice bypasses quality checks or peer review.</p>
    <p>Diagnosis: {esc(diagnosis.get('category') or 'needs investigation')}. Suggested next step:
    {esc(ACTIONS[recommended])}.</p>
    <pre>{esc(diagnosis.get('failure') or task.get('failNote') or task.get('note') or '')}</pre>
    <form method="post" action="{esc(action_url)}">
    <label>Decision<select name="action">{options}</select></label>
    <label>Your instructions or missing inputs (required to resume)<textarea name="guidance" rows="5"></textarea></label>
    <label>New acceptance criteria (required for a scope change)<textarea name="acceptanceCriteria" rows="3"></textarea></label>
    <label>Scope change deliverable<select name="taskType"><option value="">Keep current type</option>
    <option value="research">Research document / plan</option><option value="code">Software implementation</option></select></label>
    <label>Repair owner (for reassignment)<select name="agentId">{owners}</select></label>
    <label>Smaller story titles (for splitting; one per line)<textarea name="titles" rows="4"></textarea></label>
    <button>Apply guidance</button></form><p><a href="/">Return to app</a></p></html>'''


def apply_guidance(state, task_id, data):
    import sim
    task = (state.get('tasks') or {}).get(task_id)
    if not task:
        raise ValueError('Unknown task.')
    if task.get('status') in ('walking', 'working', 'done', 'cancelled', 'abandoned'):
        raise ValueError('This task is not awaiting intervention.')
    action = data.get('action')
    guidance = (data.get('guidance') or '').strip()
    if action not in ACTIONS:
        raise ValueError('Choose a valid action.')
    if action not in ('pause', 'reject') and not guidance:
        raise ValueError('Provide instructions so the next attempt can make progress.')
    criteria = (data.get('acceptanceCriteria') or '').strip()
    if action == 'edit_scope' and not criteria:
        raise ValueError('Provide new acceptance criteria.')
    task_type = data.get('taskType') or task.get('taskType') or 'code'
    if action == 'edit_scope' and task_type not in ('code', 'research'):
        raise ValueError('Choose software implementation or a research document.')
    owner = data.get('agentId') if action == 'reassign' else task.get('assignedTo')
    if action in ('retry', 'provide_inputs', 'edit_scope', 'reassign'):
        if owner not in (state.get('agents') or {}):
            raise ValueError('Choose an existing repair owner.')
        masters = {t.get('scrumMasterId') for t in state.get('teams', [])}
        if owner in masters or any(d.get('id') == owner and d.get('isAdmin')
                                  for d in state.get('agentRoster', [])):
            raise ValueError('Choose a worker rather than the admin or scrum master.')
    titles = [t.strip() for t in (data.get('titles') or '').splitlines() if t.strip()]
    if action == 'split' and not 2 <= len(titles) <= 12:
        raise ValueError('Provide between two and twelve smaller story titles.')
    now = int(time.time() * 1000)
    history = {'action': action, 'guidance': guidance, 'at': now,
               'previousGate': dict(task.get('_peerGate') or {})}
    task.setdefault('interventionHistory', []).append(history)
    state['workQueue'] = [it for it in state.get('workQueue', []) if it.get('reviewOf') != task_id]
    task.setdefault('_peerGate', {})['escalated'] = True
    task['manualAttention'] = True
    if action == 'reject':
        task['status'] = 'cancelled'
    elif action in ('pause', 'research', 'split'):
        task['status'] = 'cancelled' if action == 'split' else 'blocked'
        task['blockedReason'] = 'awaiting_human_guidance' if action == 'pause' else 'review_diagnosis_required'
        if action != 'pause':
            roster = {d['id']: d for d in state.get('agentRoster', [])}
            tid = task.get('teamId') or (roster.get(task.get('assignedTo')) or {}).get('director')
            team = sim._team_row(state, tid)
            titles = titles if action == 'split' else [f'Investigate review blocker: {task.get("title")}']
            for title in titles:
                req = sim.file_work_request(state, (team or {}).get('scrumMasterId') or 'theo',
                                            title, 'observatory' if action == 'research' else task.get('room'),
                                            reason=f'{guidance}\nOriginal task: {task_id}. {task.get("failNote") or ""}')
                if req:
                    req.update(teamId=tid, taskType='spike' if action == 'research' else task.get('taskType') or 'code',
                               sourceTaskId=task_id, acceptanceCriteria=criteria or task.get('acceptanceCriteria'))
            sim.kick_refinement_now(state, tid, now)
    else:
        task.update(assignedTo=owner, status='failed', manualAttention=False)
        task.pop('blockedReason', None)
        if criteria:
            task['acceptanceCriteria'] = criteria
        if action == 'edit_scope':
            task['taskType'] = task_type
            task['room'] = 'observatory' if task_type == 'research' else 'pressoffice'
        task['instructions'] = f'{task.get("instructions") or ""}\n\nHuman guidance: {guidance}'
        task['_peerGate'] = {'cycleCount': 0, 'approvals': 0, 'approvers': [], 'reviewerIds': [], 'enteredMs': 0}
        sim.queue_work(state, [{'title': f'Fix: {task.get("title")}', 'room': task.get('room'),
                               'taskType': task.get('taskType') or 'code', 'assignedTo': owner,
                               'reviewOf': task_id, 'reviewAuthorId': owner, 'priority': 'high',
                               'instructions': task['instructions'], 'acceptanceCriteria': task.get('acceptanceCriteria'),
                               'teamId': task.get('teamId'), 'sprintId': task.get('sprintId'),
                               'budgetUsd': task.get('budgetUsd')}])
    return {'ok': True, 'taskId': task_id, 'action': action, 'status': task['status']}


async def form_data(request):
    if 'application/json' in request.headers.get('content-type', ''):
        return await request.json()
    return {k: v[-1] for k, v in urllib.parse.parse_qs((await request.body()).decode()).items()}
