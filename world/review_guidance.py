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
    owners = '<option value="">Keep current owner</option>' + ''.join(
        f'<option value="{esc(a)}">{esc(d.get("name") or a)}</option>'
        for a, d in agents.items())
    diagnosis = task.get('reviewDiagnosis') or {}
    recommended = {'formatting': 'retry', 'missing_inputs': 'provide_inputs'}.get(diagnosis.get('category'), 'research')
    options = ''.join(f'<option value="{a}"{" selected" if a == recommended else ""}>{esc(label)}</option>'
                      for a, label in ACTIONS.items())
    failure = diagnosis.get('failure') or task.get('failNote') or task.get('note') or 'No detailed failure recorded.'
    suggested = ('Investigate the recorded blocker against this story\'s own acceptance criteria. '
        'Check whether the review imposed an unrelated requirement. Save a concrete repair or research story '
        'with an owner and testable completion criteria; ask me only for decisions or inputs you cannot research.')
    return f'''<!doctype html><html><meta name="viewport" content="width=device-width">
    <title>Guide a stuck review</title><style>body{{max-width:1100px;margin:30px auto;padding:20px;
    font:16px system-ui}}[hidden]{{display:none!important}}label{{display:block;margin:14px 0}}textarea,input,select{{box-sizing:border-box;
    width:100%;padding:8px}}pre{{white-space:pre-wrap}}button{{padding:12px}}</style>
    <h1>Unblock: {esc(task.get('title') or task['id'])}</h1>
    <p>Task {esc(task['id'])}. No choice bypasses quality checks or peer review.</p>
    <p>Diagnosis: {esc(diagnosis.get('category') or 'needs investigation')}. Suggested next step:
    {esc(ACTIONS[recommended])}.</p>
    <p>Retry queues a repair using your instructions. Investigate assigns recovery to the scrum master and removes this story from Needs attention. Pause saves your instructions without resuming work.</p>
    <h2>What this story must deliver</h2><p>{esc((task.get('reviewContract') or {}).get('acceptanceCriteria') or task.get('acceptanceCriteria') or 'Ask the scrum master to clarify completion criteria.')}</p>
    <h2>Why work stopped</h2><pre>{esc(failure[:2500])}</pre>
    <p>Use <strong>Ask the scrum master to investigate</strong> when the error is unclear or the reviewer asks for the wrong deliverable. Use <strong>Retry</strong> when you know the correction. Use <strong>Supply inputs</strong> for a missing file or decision.</p>
    <form method="post" action="{esc(action_url)}">
    <label>Decision<select name="action">{options}</select></label>
    <p id="actionHelp" role="status"></p>
    <label>Optional instructions (leave blank to use the recorded blocker)<textarea name="guidance" rows="5">{esc(suggested)}</textarea></label>
    <label data-actions="edit_scope">What should count as done?<textarea name="acceptanceCriteria" rows="3">{esc(task.get('acceptanceCriteria') or '')}</textarea></label>
    <label data-actions="edit_scope">Deliverable type<select name="taskType"><option value="">Keep current type</option>
    <option value="research">Research document / plan</option><option value="code">Software implementation</option></select></label>
    <label data-actions="reassign">Who should repair it?<select name="agentId">{owners}</select></label>
    <label data-actions="split">Smaller story titles (one per line)<textarea name="titles" rows="4"></textarea></label>
    <button>Send recovery instructions</button></form>
    <script>
    const explanations={{pause:'Saves your instructions and keeps work paused.',retry:'Queues a repair; quality checks and peer review run again.',provide_inputs:'Passes your supplied inputs to the repair owner and retries.',edit_scope:'Changes the required deliverable, then queues a repair.',reassign:'Queues a repair for the selected owner.',research:'Removes this story from Needs attention and assigns an investigation directly to the scrum master.',split:'Keeps this story paused and creates smaller stories for grooming.',reject:'Stops this story.'}};
    const choice=document.querySelector('[name=action]');
    function updateAction(){{document.getElementById('actionHelp').textContent=explanations[choice.value];document.querySelectorAll('[data-actions]').forEach(el=>el.hidden=el.dataset.actions!==choice.value);}}
    choice.addEventListener('change',updateAction);updateAction();
    </script><p><a href="/">Return to app</a></p></html>'''


def apply_guidance(state, task_id, data):
    import sim
    if data.get('action') == 'research':
        seen = set()
        while task_id not in seen:
            seen.add(task_id)
            current = (state.get('tasks') or {}).get(task_id) or {}
            parent_id = current.get('investigationOf') or current.get('reviewOf')
            if not parent_id or parent_id not in (state.get('tasks') or {}): break
            task_id = parent_id
        existing = next((q for q in state.get('workQueue',[]) if q.get('investigationOf') == task_id),None)
        active = next((t for t in (state.get('tasks') or {}).values() if t.get('investigationOf') == task_id
                       and t.get('status') in ('walking','working')),None)
        if existing or active:
            return {'ok':True,'taskId':task_id,'action':'research','status':'investigating','alreadyQueued':True}
    task = (state.get('tasks') or {}).get(task_id)
    if not task:
        raise ValueError('Unknown task.')
    if task.get('status') in ('walking', 'working', 'done', 'cancelled', 'abandoned'):
        raise ValueError('This task is not awaiting intervention.')
    action = data.get('action')
    guidance = (data.get('guidance') or '').strip()
    if action not in ACTIONS:
        raise ValueError('Choose a valid action.')
    if not guidance:
        guidance = ('Investigate and resolve this story using its own completion criteria. '
                    'Create concrete repair or research stories and ask questions only for missing human decisions. '
                    'Recorded blocker: ' + str((task.get('reviewDiagnosis') or {}).get('failure') or task.get('failNote') or task.get('note') or 'Inspect the saved deliverable.'))
    criteria = (data.get('acceptanceCriteria') or '').strip()
    if action == 'edit_scope' and not criteria:
        raise ValueError('Provide new acceptance criteria.')
    task_type = data.get('taskType') or task.get('taskType') or 'code'
    if action == 'edit_scope' and task_type not in ('code', 'research'):
        raise ValueError('Choose software implementation or a research document.')
    owner = data.get('agentId') if action == 'reassign' else (task.get('originalAssignedTo') or task.get('assignedTo'))
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
    if action == 'research':
        roster = {d['id']:d for d in state.get('agentRoster',[])}
        tid = task.get('teamId') or (roster.get(task.get('assignedTo')) or {}).get('director')
        team = sim._team_row(state,tid)
        master = (team or {}).get('scrumMasterId')
        if not master:
            master = next((t.get('scrumMasterId') for t in state.get('teams',[]) if t.get('scrumMasterId') in state.get('agents',{})),None)
        if not master or master not in state.get('agents',{}):
            raise ValueError('No scrum master is available to investigate this story.')
    now = int(time.time() * 1000)
    history = {'action': action, 'guidance': guidance, 'at': now,
               'previousGate': dict(task.get('_peerGate') or {})}
    task.setdefault('interventionHistory', []).append(history)
    if action in ('retry', 'provide_inputs', 'edit_scope', 'reassign', 'research'):
        task.pop('completionRecovery', None)
    state['workQueue'] = [it for it in state.get('workQueue', []) if it.get('reviewOf') != task_id]
    task.setdefault('_peerGate', {})['escalated'] = True
    task['manualAttention'] = True
    if action == 'research':
        task.setdefault('originalAssignedTo',task.get('assignedTo'))
        task.update(status='investigating',assignedTo=master,manualAttention=False,blockedReason=None,
                    recoveryOwner=master,note='Scrum master investigation queued; no player guidance required.')
        task['_peerGate']['escalated'] = False
        sim.queue_work(state,[{'title':f'Investigate review blocker: {task.get("title")}',
            'assignedTo':master,'directRoute':True,'taskMasterWork':True,'taskType':'spike',
            'room':'observatory','teamId':(team or {}).get('id') or tid,'budgetMs':120000,
            'investigationOf':task_id,'priority':'high','projectLabel':'Blocker recovery',
            'instructions':guidance+'\nOriginal story: '+task_id+'\nRequired deliverable: '+str(task.get('acceptanceCriteria') or '')
                +'\nSaved Library document: '+str(task.get('libraryPath') or 'none')
                +'\nInvestigate the actual cause, save a repair plan, and use file_issue to create actionable repair or research stories for the team. Do not approve the original story or skip its quality gates.',
            'acceptanceCriteria':'Save an evidence-based diagnosis and create actionable repair/research stories with owners and testable completion criteria, or ask for a specific missing human decision.'}])
    elif action == 'reject':
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
        task.update(assignedTo=owner, status='repairing', manualAttention=False,
                    note='Repair queued for '+str(owner)+' using your retry instructions.')
        for field in ('blockedReason','boardClearedAt','failNote','reviewFailureKind',
                      '_codeDirectorPending','_codeDirectorPassed','recoveryStaffed'):
            task.pop(field, None)
        if task.get('reviewDiagnosis'):
            task.setdefault('reviewDiagnosisHistory', []).append(task.pop('reviewDiagnosis'))
        task['_peerRepairPending'] = True
        if criteria:
            task['acceptanceCriteria'] = criteria
        if action == 'edit_scope':
            task['taskType'] = task_type
            task['room'] = 'observatory' if task_type == 'research' else 'pressoffice'
        task['instructions'] = f'{task.get("instructions") or ""}\n\nHuman guidance: {guidance}'
        if action == 'edit_scope' or criteria:
            import review_quality
            task.pop('reviewContract', None)
            task['reviewContract'] = review_quality.contract_for(task)
            task['reviewContract']['version'] = 1 + len(task.get('interventionHistory') or [])
        task['_peerGate'] = {'cycleCount': 0, 'approvals': 0, 'approvers': [], 'reviewerIds': [], 'enteredMs': 0}
        sim.queue_work(state, [{'title': f'Fix: {task.get("title")}', 'room': task.get('room'),
                               'taskType': task.get('taskType') or 'code', 'assignedTo': owner,
                               'directRoute': True, 'taskMasterWork': True,
                               'reviewOf': task_id, 'reviewAuthorId': owner, 'priority': 'high',
                               'instructions': task['instructions'], 'acceptanceCriteria': task.get('acceptanceCriteria'),
                               'projectLabel': task.get('projectLabel') or task.get('title'),
                               'productId': task.get('productId'), 'userStory': task.get('userStory'),
                               'reviewContract': task.get('reviewContract'),
                               'teamId': task.get('teamId'), 'sprintId': task.get('sprintId'),
                               'budgetUsd': task.get('budgetUsd')}])
    return {'ok': True, 'taskId': task_id, 'action': action, 'status': task['status']}


async def form_data(request):
    if 'application/json' in request.headers.get('content-type', ''):
        return await request.json()
    return {k: v[-1] for k, v in urllib.parse.parse_qs((await request.body()).decode()).items()}
