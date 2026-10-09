"""Durable story lineage, accountable scrum ownership and cost rollups."""
import math

LINKS = ('reviewOf', 'investigationOf', 'recoveryOf')


def budget(value):
    if isinstance(value, str):
        value = value.strip()
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError('Budget must be a positive dollar amount or blank.')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError('Budget must be a positive dollar amount or blank.') from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError('Budget must be a positive dollar amount or blank.')
    return number


def root(state, task):
    seen = set()
    while task and task.get('id') not in seen:
        seen.add(task.get('id'))
        pid = next((task.get(k) for k in LINKS if task.get(k)), None)
        parent = (state.get('tasks') or {}).get(pid)
        if not parent:
            break
        task = parent
    return task or {}


def stamp(state, task):
    parent = root(state, task)
    request_id = parent.get('workOrderId') or task.get('workOrderId') or (parent.get('reviewContract') or {}).get('parentWorkOrderId')
    request = next((r for r in state.get('backlogRequests') or [] if r.get('id') == request_id), {})
    roster = {r.get('id'): r for r in state.get('agentRoster') or []}
    team_id = parent.get('teamId') or request.get('teamId') or roster.get(parent.get('assignedTo'), {}).get('director')
    team = next((t for t in state.get('teams') or [] if t.get('id') == team_id or t.get('directorId') == team_id), {})
    master = parent.get('scrumMasterId') or request.get('scrumMasterId') or team.get('scrumMasterId')
    if not master:
        master = next((t.get('scrumMasterId') for t in state.get('teams') or [] if t.get('scrumMasterId')), None)
    task.update(rootStoryId=parent.get('id') or task.get('rootStoryId'), scrumMasterId=master,
                requestId=request_id)
    if request_id:
        task['workOrderId'] = request_id
    if parent.get('workOrderStoryId'):
        task['workOrderStoryId'] = parent['workOrderStoryId']
    return task


def costs(state, ledger):
    stories, requests, masters = {}, {}, {}
    for task in (state.get('tasks') or {}).values():
        stamp(state, task)
        entry = ledger.get('__task__/' + task['id']) or {}
        used, calls = float(entry.get('used') or 0), int(entry.get('calls') or 0)
        task.update(ownSpendUsd=used, ownSpendCalls=calls)
        for mapping, key in ((stories, task['rootStoryId']), (requests, task.get('requestId')), (masters, task.get('scrumMasterId'))):
            if key:
                row = mapping.setdefault(key, {'used': 0.0, 'calls': 0})
                row['used'] += used
                row['calls'] += calls
    for request in state.get('backlogRequests') or []:
        entry = ledger.get('__task__/request/' + request['id']) or {}
        row = requests.setdefault(request['id'], {'used': 0.0, 'calls': 0})
        row['used'] += float(entry.get('used') or 0)
        row['calls'] += int(entry.get('calls') or 0)
        master = request.get('scrumMasterId')
        if master:
            owner = masters.setdefault(master, {'used': 0.0, 'calls': 0})
            owner['used'] += float(entry.get('used') or 0)
            owner['calls'] += int(entry.get('calls') or 0)
    return {'stories': stories, 'requests': requests, 'scrumMasters': masters}


def refresh(state, ledger):
    view = costs(state, ledger)
    for task in (state.get('tasks') or {}).values():
        task['storySpendUsd'] = view['stories'].get(task['rootStoryId'], {}).get('used', 0.0)
    state['storyCostTracking'] = view
    for item in state.get('workQueue') or []:
        stamp(state, item)
    return view


def exhausted(state, ledger, task_id):
    task = (state.get('tasks') or {}).get(task_id)
    request_id = task_id[len('request/'):] if task_id.startswith('request/') else None
    if task:
        stamp(state, task)
        request_id = task.get('requestId')
    request = next((r for r in state.get('backlogRequests') or [] if r.get('id') == request_id), None)
    cap = request.get('requestBudgetUsd') if request else None
    if cap is None:
        return False
    return costs(state, ledger)['requests'].get(request_id, {}).get('used', 0.0) >= cap


def budget_status(state, ledger, task_id):
    task = (state.get('tasks') or {}).get(task_id)
    request_id = task_id[len('request/'):] if task_id.startswith('request/') else None
    if task:
        stamp(state, task)
        request_id = task.get('requestId')
    request = next((r for r in state.get('backlogRequests') or [] if r['id'] == request_id), {})
    if request.get('requestBudgetUsd') is not None:
        usage = costs(state, ledger)['requests'].get(request_id, {'used':0.0,'calls':0})
        return {'scope':'request', 'id':request_id, 'budgetUsd':request['requestBudgetUsd'], **usage}
    entry = ledger.get('__task__/' + task_id) or {}
    return {'scope':'task', 'id':task_id, 'budgetUsd':(task or {}).get('budgetUsd'),
            'used':float(entry.get('used') or 0), 'calls':int(entry.get('calls') or 0)}


def closed(state, task_id):
    task = (state.get('tasks') or {}).get(task_id)
    return bool(task and (task.get('status') in ('cancelled','abandoned') or root(state,task).get('playerAcceptance')))
