# Operator card management (2026-11-20): direct edit/cancel of mission-control
# task cards from the player's session. The tank's own lifecycle (repair loop,
# completion gate) stays the owner of status transitions; these endpoints are
# the operator's surgical tools for the two cases the machine cannot do on its
# own -- amending instructions mid-flight (e.g. pointing a batch review at a
# shared rubric) and killing a wedged/stale card that would otherwise churn
# repair generations forever. Both are @_state_writer guarded and
# player-session only, exactly like the /api/intent/issues surface.
import json as _json
import time as _time

_CARD_TERMINAL = ('done', 'cancelled', 'abandoned', 'quarantined')


def _find_task(state, task_id):
    """Locate a card anywhere in the task forest: root-level tasks map, plus
    any nested tasks dict on a parent card (pieces of a story/program)."""
    tasks = state.get('tasks') or {}
    if task_id in tasks:
        return tasks[task_id], None
    for parent_id, parent in tasks.items():
        nested = parent.get('tasks')
        if isinstance(nested, dict) and task_id in nested:
            return nested[task_id], parent_id
    return None, None


def _append_card_event(task, kind, detail, actor):
    hist = task.setdefault('history', [])
    if not isinstance(hist, list):
        task['history'] = hist = []
    hist.append({
        'ts': int(_time.time() * 1000),
        'kind': kind,
        'by': actor,
        'detail': (detail or '')[:400],
    })


@app.post('/api/tasks/{task_id}/edit')
@_state_writer
async def edit_task_card(task_id: str, request: Request):
    """Edit an operator-owned field set on a live card: instructions, title,
    priority, deliverable, budgetUsd, approvedTools. Everything else is
    machine-owned and refused. Fields absent from the body are left untouched.
    A note is appended to the card's history; if a worker holds the card, the
    change lands on the next step read (the worker re-reads its instructions
    per step; nothing needs to be pushed)."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    task, parent_id = _find_task(state, task_id)
    if not task:
        return JSONResponse({'error': f'unknown task: {task_id}'}, status_code=404)
    if (task.get('status') or '') in _CARD_TERMINAL:
        return JSONResponse(
            {'error': f'task {task_id} is terminal ({task.get("status")}); nothing to edit'},
            status_code=409)
    allowed = {'instructions': 'instructions', 'title': 'title',
               'priority': 'priority', 'deliverable': 'deliverable',
               'budgetUsd': 'budgetUsd', 'approvedTools': 'approvedTools'}
    changes = {}
    for field in ('instructions', 'title', 'deliverable'):
        if field in body:
            value = body[field]
            if value is not None and not isinstance(value, str):
                return JSONResponse({'error': f'{field} must be a string'}, status_code=400)
            task[field] = (value or '').strip()
            changes[field] = f'{len(task[field])} chars'
    if 'priority' in body:
        try:
            task['priority'] = int(body['priority'])
            changes['priority'] = task['priority']
        except (TypeError, ValueError):
            return JSONResponse({'error': 'priority must be an integer'}, status_code=400)
    if 'budgetUsd' in body:
        try:
            task['budgetUsd'] = round(float(body['budgetUsd']), 2)
            changes['budgetUsd'] = task['budgetUsd']
        except (TypeError, ValueError):
            return JSONResponse({'error': 'budgetUsd must be a number'}, status_code=400)
    if 'approvedTools' in body:
        tools = body['approvedTools']
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            return JSONResponse({'error': 'approvedTools must be a list of strings'}, status_code=400)
        task['approvedTools'] = tools
        changes['approvedTools'] = tools
    if not changes:
        return JSONResponse({'error': 'no editable field in body '
                             f'(editable: {", ".join(sorted(allowed))})'}, status_code=400)
    task['updatedAt'] = int(_time.time() * 1000)
    task['operatorEdited'] = True
    _append_card_event(task, 'operator_edit',
                       '; '.join(f'{k}: {v}' for k, v in changes.items()), actor)
    save_state_to_db(state)
    log_action('player', 'task_card_edit', {'taskId': task_id, 'changes': changes,
                                            'parentTaskId': parent_id}, authorized=True)
    return JSONResponse({'ok': True, 'taskId': task_id, 'parentTaskId': parent_id,
                         'changes': changes, 'status': task.get('status')})


@app.post('/api/tasks/{task_id}/cancel')
@_state_writer
async def cancel_task_card(task_id: str, request: Request):
    """Cancel a live card: status -> 'cancelled', deliveryState -> 'cancelled',
    a history event, and an explicit cancelledBy/cancelledReason provenance
    pair (the machine paths leave those unset, so an operator cancel is always
    distinguishable from a lifecycle cancel). Cancelled cards are terminal:
    neither the repair machine nor the completion gate re-cards them."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = (body.get('reason') or '').strip()[:300] or 'operator cancelled'
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    task, parent_id = _find_task(state, task_id)
    if not task:
        return JSONResponse({'error': f'unknown task: {task_id}'}, status_code=404)
    if (task.get('status') or '') in _CARD_TERMINAL:
        return JSONResponse(
            {'error': f'task {task_id} is already terminal ({task.get("status")})'},
            status_code=409)
    now_ms = int(_time.time() * 1000)
    task.update(
        status='cancelled',
        deliveryState='cancelled',
        closedAt=now_ms,
        cancelledBy=actor,
        cancelledReason=reason,
        note=reason,
    )
    for sweep in ('_contentInFlight', '_peerRepairPending'):
        task.pop(sweep, None)
    _append_card_event(task, 'operator_cancel', reason, actor)
    save_state_to_db(state)
    log_action('player', 'task_card_cancel', {'taskId': task_id, 'reason': reason,
                                              'parentTaskId': parent_id}, authorized=True)
    return JSONResponse({'ok': True, 'taskId': task_id, 'parentTaskId': parent_id,
                         'status': task.get('status'), 'reason': reason})
