"""Admin bulk-close for runaway card families (2026-11-05).

The re-card machine spawned 167 repair/park/cleanup cards around work that
was ALREADY DONE (the disk-full fix, the weatherstation strip, the CI
re-card chain). Every one of them fails the honest deliverable gate, each
failure repairs into another "Fix:"-prefixed card, and the churn keeps
paging the player. Directors close cards by hand one at a time -- too slow
for a population this size.

This endpoint is the operator's executive instrument, deliberately narrow:

  * auth: the tank admin (agent key with isAdmin) or the player session;
  * the CALLER supplies explicit title regex patterns -- the endpoint has
    no built-in notion of what is "stale";
  * only non-terminal tasks and queued work items are touched; terminal
    rows are left alone;
  * every close is logged individually with the matched pattern and the
    caller-supplied reason, so the board's history shows why each card
    died and who closed it.
"""
from fastapi import Request
from fastapi.responses import JSONResponse


def _is_tank_admin(request):
    from serve import _agent_id_for_key, verify_agent_key, get_state_from_db
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    if agent_id and verify_agent_key(agent_id, key) is True:
        try:
            state = get_state_from_db(archive_merge='none') or {}
        except Exception:
            return None
        for d in state.get('agentRoster') or []:
            if d.get('id') == agent_id and d.get('isAdmin'):
                return agent_id
    return None


@app.post('/api/admin/close-stale-cards')
@_state_writer
async def admin_close_stale_cards(request: Request):
    from serve import (app, _state_writer, verify_session, SESSION_COOKIE_NAME,
                       save_state_to_db, log_action, check_rate_limit)
    actor = _is_tank_admin(request)
    if not actor and not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- tank admin or the '
                             'player only'}, status_code=401)
    if actor and not check_rate_limit(actor):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    patterns = body.get('patterns') or []
    if not isinstance(patterns, list) or not patterns:
        return JSONResponse({'error': 'patterns (list of regex strings) '
                             'required -- the endpoint closes nothing on its '
                             'own judgment'}, status_code=400)
    reason = str(body.get('reason') or '').strip()[:300] or (
        'bulk-closed by operator: the underlying work is already done')
    import re as _re
    compiled = []
    for p in patterns:
        try:
            compiled.append((p, _re.compile(str(p), _re.I)))
        except _re.error:
            return JSONResponse({'error': f'bad regex: {p}'}, status_code=400)

    def _matches(text):
        text = str(text or '')
        return next((pat for pat, rx in compiled if rx.search(text)), None)

    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    now_ms = __import__('time').time() * 1000
    terminal = ('done', 'cancelled', 'abandoned')
    closed_tasks, skipped_artifact = [], []
    for task_id, task in list((state.get('tasks') or {}).items()):
        if not isinstance(task, dict) or task.get('status') in terminal:
            continue
        matched = _matches(task.get('title')) or _matches(
            task.get('instructions'))
        if not matched:
            continue
        # A card that already produced a real artifact is evidence of work
        # worth keeping even if the pattern matches; leave it for a human.
        if (task.get('libraryPath') or task.get('codePublished')
                or task.get('writtenPy')):
            skipped_artifact.append(task_id)
            continue
        task.update(status='cancelled', closedAt=now_ms,
                    statusReason='admin_bulk_close', deliveryState='cancelled',
                    manualAttention=False, note=reason,
                    nextAction='Closed by the operator: the underlying work '
                               'is already done.')
        task.pop('_peerRepairPending', None)
        task.pop('_peerGate', None)
        closed_tasks.append(task_id)
        log_action(actor, 'admin_bulk_closed_card',
                   {'taskId': task_id, 'pattern': matched,
                    'title': str(task.get('title'))[:120],
                    'reason': reason}, authorized=True)
    closed_queue = []
    kept_queue = []
    for item in state.get('workQueue') or []:
        if isinstance(item, dict):
            matched = _matches(item.get('title')) or _matches(
                item.get('instructions'))
            if matched and not (item.get('libraryPath')
                                or item.get('codePublished')):
                item['status'] = 'cancelled'
                item['closedAt'] = now_ms
                item['closedReason'] = reason
                closed_queue.append(str(item.get('id') or item.get('title'))[:60])
                continue
        kept_queue.append(item)
    state['workQueue'] = kept_queue
    save_state_to_db(state)
    log_action(actor, 'admin_bulk_close_complete',
               {'closedTasks': len(closed_tasks), 'closedQueue': len(closed_queue),
                'skippedWithArtifact': len(skipped_artifact),
                'reason': reason}, authorized=True)
    return JSONResponse({'ok': True, 'closedTasks': len(closed_tasks),
                         'closedQueue': len(closed_queue),
                         'skippedWithArtifact': skipped_artifact[:20],
                         'sample': closed_tasks[:20]})
