"""Scrum-master workflow memory.

A request that finishes cleanly is evidence of a repeatable process. The
scrum master's sweep turns that evidence into a card template, promotes it
after a second clean run, and lets any team's scrum master reuse it. Cards
that fail a template demote it. Nothing here calls a model.
"""
import hashlib
import re

WORKFLOW_TEMPLATE_CADENCE_MS = 6 * 60 * 60 * 1000
_WORKFLOW_STOP = {
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'under',
    'over', 'after', 'before', 'about', 'your', 'their', 'our', 'you',
    'are', 'was', 'were', 'will', 'can', 'not', 'use', 'using', 'least',
    'one', 'two', 'three', 'word', 'words', 'please', 'make', 'sure',
}
_WORKFLOW_INTENT = {
    'research', 'write', 'brief', 'report', 'memo', 'summary', 'cited',
    'sources', 'source', 'recommendation', 'save', 'gather', 'verify',
    'review', 'audit', 'analyze', 'analysis', 'compare', 'plan', 'fix',
    'bug', 'test', 'implement', 'build', 'add', 'refactor', 'migrate',
    'deploy', 'lint', 'flake8',
}
_ROLE_LABELS = {
    'gather': 'Gather sources',
    'write': 'Write the deliverable',
    'save': 'Save the deliverable',
    'verify': 'Verify the deliverable',
    'contingency': 'Reserve contingency',
    'deliverable': 'Deliver the requested artifact',
    'step': 'Complete the step',
}


def _workflow_tokens(text):
    words = re.findall(r'[a-z0-9]+', str(text or '').lower())
    return {w for w in words if len(w) >= 3 and w not in _WORKFLOW_STOP}


def _workflow_role(title, task_type):
    text = str(title or '').lower()
    if 'gather' in text or 'source' in text or 'research' in text:
        return 'gather'
    if 'save' in text or 'library' in text:
        return 'save'
    if 'verify' in text or 'review' in text or 'check' in text:
        return 'verify'
    if 'write' in text or 'draft' in text:
        return 'write'
    if 'contingency' in text or 'reserve' in text:
        return 'contingency'
    return 'deliverable' if task_type == 'report' else 'step'


_WORKFLOW_CORE = {
    'research', 'brief', 'report', 'memo', 'summary', 'cited', 'sources',
    'recommendation', 'fix', 'bug', 'test', 'implement', 'build', 'add',
    'refactor', 'migrate', 'deploy', 'lint',
}


def _workflow_template_id(tokens, steps):
    # The id must survive a topic change and optional details: "brief on
    # repair loops" and "brief on retry storms" are the same workflow, whether
    # or not the goal repeats "save" or "cited". Keep the artifact kind and
    # the core intent words; drop topic words and optional save wording.
    if tokens & {'brief', 'report', 'memo', 'summary', 'writeup'}:
        kind = 'report'
    elif 'research' in tokens:
        kind = 'research'
    elif tokens & {'fix', 'bug', 'test', 'implement', 'build', 'add', 'refactor',
                   'migrate', 'deploy', 'lint'}:
        kind = 'code'
    else:
        kind = 'general'
    core = sorted(set(tokens) & _WORKFLOW_CORE)
    key = kind + '|' + ','.join(core) + '||' + '|'.join(
        f"{s.get('role')}:{s.get('type')}" for s in steps)
    return 'wft-' + hashlib.sha256(key.encode()).hexdigest()[:16]


def _workflow_steps(tasks):
    ordered = sorted(
        tasks,
        key=lambda t: (t.get('sequence') if isinstance(t.get('sequence'), int) else 999,
                       t.get('createdAt') or 0))
    steps = []
    for task in ordered:
        if task.get('status') != 'done':
            continue
        task_type = task.get('taskType') or 'code'
        # Treat a spike/research card as report-shaped for role detection too,
        # so a written deliverable gets the same role whichever lane ran it.
        role_type = 'report' if task_type in ('spike', 'research') else task_type
        role = _workflow_role(task.get('title'), role_type)
        if role in ('deliverable', 'write', 'save') and task_type in ('spike', 'research'):
            # A written deliverable that happened to run through the spike
            # lane is the same workflow as one that ran through the report
            # lane. Normalize the signature so they accumulate, not split.
            task_type = 'report'
        steps.append({
            'role': role,
            'title': _ROLE_LABELS.get(role, role.title()),
            'type': task_type,
            'acceptanceCriteria': task.get('acceptanceCriteria') or '',
            'sizeEstimate': task.get('sizeEstimate') or 'M',
            'never': task.get('never') or None,
            'budgetUsd': float(task.get('budgetUsd') or 0),
            'sourceTaskId': task.get('id'),
        })
    total = sum(step['budgetUsd'] for step in steps) or 0.0
    for step in steps:
        step['budgetShare'] = (step['budgetUsd'] / total) if total else (1.0 / len(steps))
    return steps


def _workflow_scrum_master(state, team_id):
    for team in (state.get('teams') or []):
        if team_id in (team.get('id'), team.get('directorId')):
            return team.get('scrumMasterId') or team.get('directorId')
    for entry in (state.get('agentRoster') or []):
        if entry.get('director') == team_id:
            return entry.get('id')
    return team_id


def _workflow_template_name(goal):
    text = ' '.join(str(goal or '').split())
    return text[:90] or 'Untitled workflow'


def _workflow_log(state, agent_id, action, details):
    try:
        _log_governance(state, agent_id, action, details)
    except Exception:
        pass


def _workflow_record_success(state, req, tasks, team_id, now_ms):
    goal = req.get('goal') or req.get('title') or ''
    tokens = _workflow_tokens(goal)
    single_goal = globals().get('_single_deliverable_goal')
    if callable(single_goal) and single_goal(goal):
        # The learned process is one deliverable card, even if history has a
        # messy gather/write/save/verify chain behind it. This keeps the old
        # broken shape from becoming the template.
        total = sum(float(t.get('budgetUsd') or 0) for t in tasks) or 1.0
        steps = [{
            'role': 'deliverable',
            'title': _ROLE_LABELS['deliverable'],
            'type': 'report',
            'acceptanceCriteria': 'One deliverable at the requested path.',
            'sizeEstimate': 'M',
            'never': None,
            'budgetUsd': total,
            'budgetShare': 1.0,
            'sourceTaskId': (tasks[0].get('id') if tasks else None),
        }]
    else:
        steps = _workflow_steps(tasks)
    if not tokens or not steps:
        return
    templates = state.setdefault('workflowTemplates', {})
    template_id = _workflow_template_id(tokens, steps)
    template = templates.get(template_id)
    if template is None:
        template = {
            'id': template_id,
            'name': _workflow_template_name(goal),
            'trigger': str(goal)[:400],
            'tokens': sorted(tokens),
            'steps': steps,
            'ownerTeamId': team_id,
            'createdBy': _workflow_scrum_master(state, team_id),
            'status': 'candidate',
            'successCount': 1,
            'failureCount': 0,
            'crossTeamSuccess': 0,
            'useCount': 0,
            'sourceRequests': [req.get('id')],
            'sourceTeams': [team_id],
            'usingTeams': [team_id],
            'createdAt': now_ms,
            'updatedAt': now_ms,
        }
        templates[template_id] = template
        req['workflowTemplateId'] = template_id
        _workflow_log(state, template['createdBy'], 'workflow_template_learned',
                      {'templateId': template_id, 'teamId': team_id,
                       'steps': len(steps), 'title': template['name'][:120]})
    else:
        template['successCount'] = int(template.get('successCount') or 0) + 1
        template['updatedAt'] = now_ms
        if req.get('id') not in (template.get('sourceRequests') or []):
            template.setdefault('sourceRequests', []).append(req.get('id'))
        if team_id != template.get('ownerTeamId'):
            template['crossTeamSuccess'] = int(template.get('crossTeamSuccess') or 0) + 1
            if team_id not in (template.get('usingTeams') or []):
                template.setdefault('usingTeams', []).append(team_id)
    req['_workflowSuccessCounted'] = True
    _workflow_promote(state, template, now_ms)


def _workflow_record_failure(state, req, now_ms):
    template_id = req.get('workflowTemplateId')
    if not template_id or req.get('_workflowFailureCounted'):
        return
    template = (state.get('workflowTemplates') or {}).get(template_id)
    if not template:
        return
    template['failureCount'] = int(template.get('failureCount') or 0) + 1
    template['lastFailureAt'] = now_ms
    template['updatedAt'] = now_ms
    req['_workflowFailureCounted'] = True
    _workflow_log(state, template.get('createdBy'), 'workflow_template_failed',
                  {'templateId': template_id, 'failures': template['failureCount'],
                   'requestId': req.get('id')})
    _workflow_promote(state, template, now_ms)


def _workflow_promote(state, template, now_ms):
    successes = int(template.get('successCount') or 0) + int(template.get('crossTeamSuccess') or 0)
    failures = int(template.get('failureCount') or 0)
    if template.get('status') == 'candidate' and successes >= 2:
        template['status'] = 'active'
        template['promotedAt'] = now_ms
        _workflow_log(state, template.get('createdBy'), 'workflow_template_promoted',
                      {'templateId': template.get('id'), 'successes': successes,
                       'steps': len(template.get('steps') or [])})
    elif (template.get('status') in ('candidate', 'active')
          and failures >= 2 and failures >= successes):
        template['status'] = 'retired'
        template['retiredAt'] = now_ms
        _workflow_log(state, template.get('createdBy'), 'workflow_template_retired',
                      {'templateId': template.get('id'), 'failures': failures,
                       'successes': successes})


def _workflow_request_tasks(state, request_id, archived):
    tasks = {}
    for task in (state.get('tasks') or {}).values():
        if task.get('requestId') == request_id:
            tasks[task.get('id')] = task
    for task in (archived or {}).values():
        if task.get('requestId') == request_id and task.get('id') not in tasks:
            tasks[task.get('id')] = task
    return list(tasks.values())


def _workflow_template_sweep(state, now_ms, archived=None):
    """Learn from completed requests, update template successes/failures."""
    if archived is None:
        try:
            import serve as _serve
            archived = _serve._archived_tasks()
        except Exception:
            archived = {}
    for req in (state.get('backlogRequests') or []):
        if req.get('status') == 'cancelled':
            continue
        tasks = _workflow_request_tasks(state, req.get('id'), archived)
        if not tasks:
            continue
        queued = any(q.get('requestId') == req.get('id') for q in (state.get('workQueue') or []))
        failed = any(t.get('status') in ('failed', 'abandoned', 'cancelled') for t in tasks)
        if failed and not queued:
            _workflow_record_failure(state, req, now_ms)
            continue
        if queued or any(t.get('status') != 'done' for t in tasks):
            continue
        if req.get('_workflowSuccessCounted'):
            continue
        _workflow_record_success(state, req, tasks, req.get('teamId'), now_ms)


def _workflow_match_template(state, goal, team_id):
    goal_tokens = _workflow_tokens(goal)
    if len(goal_tokens) < 3:
        return None
    best = None
    best_key = None
    for template in (state.get('workflowTemplates') or {}).values():
        if template.get('status') != 'active':
            continue
        tokens = set(template.get('tokens') or [])
        overlap = len(goal_tokens & tokens)
        if overlap < 3:
            continue
        score = overlap / max(1, min(len(goal_tokens), len(tokens)))
        if score < 0.5:
            continue
        key = (round(score, 3), 1 if template.get('ownerTeamId') == team_id else 0,
               int(template.get('successCount') or 0) + int(template.get('crossTeamSuccess') or 0))
        if best_key is None or key > best_key:
            best, best_key = template, key
    return best


def _workflow_template_items(template, goal, constraints):
    path = None
    requested = globals().get('_requested_library_path')
    if callable(requested):
        path = requested(goal)
    total_budget = (constraints or {}).get('budgetUsd')
    total_budget = float(total_budget) if isinstance(total_budget, (int, float)) else 0.0
    steps = template.get('steps') or []
    items = []
    for step in steps:
        single = len(steps) == 1
        title = str(goal or '')[:120] if single else (
            f"{step.get('title') or 'Step'}: {str(goal or '')[:90]}")
        items.append({
            'title': title,
            'type': step.get('type') or 'code',
            'acceptanceCriteria': step.get('acceptanceCriteria') or '',
            'sizeEstimate': step.get('sizeEstimate') or 'M',
            'never': step.get('never') or None,
            'budgetUsd': (round(total_budget * float(step.get('budgetShare') or 0), 4)
                          if total_budget else None),
            'reportPath': path if (step.get('type') == 'report'
                                   or step.get('role') in ('save', 'deliverable')) else None,
        })
    return items
