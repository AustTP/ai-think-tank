"""Task replay (2026-11-04, from the @dankornas review's one recommendation,
itself inspired by agentplay's session-replay idea): a read-only endpoint that
renders ONE task's ordered trail -- model calls, tool executions,
classifications, refusals, watchdog actions -- so an operator can answer
"what did the agent actually do?" in one request instead of hand-querying
action_log. The evidence already exists in action_log rows whose `details`
JSON carries the taskId; this endpoint just orders and shapes it. Related
child cards (reviewOf/investigationOf pointing at the task) are listed with
one-line status so the operator can replay a repair chain without guessing
ids."""
import asyncio
import json

from fastapi import Request
from fastapi.responses import JSONResponse

_TRAIL_LIMIT = 500


def _task_replay_rows(conn, task_id):
    """Ordered action_log rows for one task. Most actions stamp the id inside
    their JSON `details` (taskId key); a few older actions embedded it in
    plain text. Match both, oldest-first, bounded. Malformed JSON rows still
    match via the LIKE fallback -- a partial trail beats none. Portable SQL:
    the jsonb cast only runs on postgres (db_backend maps the same call
    surface onto sqlite, where the LIKE match alone covers it)."""
    like = f'%"{task_id}"%'
    import db_backend as _dbm
    if _dbm.is_postgres():
        rows = conn.execute(
            "SELECT ts, agent_id, action, details FROM action_log "
            "WHERE details::jsonb->>'taskId' = ? OR details LIKE ? "
            "ORDER BY ts ASC LIMIT ?",
            (task_id, like, _TRAIL_LIMIT),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, agent_id, action, details FROM action_log "
            "WHERE details LIKE ? ORDER BY ts ASC LIMIT ?",
            (like, _TRAIL_LIMIT),
        ).fetchall()
    trail = []
    for ts, agent_id, action, details in rows:
        try:
            parsed = json.loads(details) if details else None
        except Exception:
            parsed = details
        trail.append({'ts': ts, 'agent': agent_id, 'action': action,
                      'details': parsed})
    return trail


def _child_cards(state, task_id):
    """One-line status for every card whose reviewOf/investigationOf points
    at this task -- the repair/review chain an operator replays with it."""
    children = []
    for cid, task in (state.get('tasks') or {}).items():
        if not isinstance(task, dict):
            continue
        if task.get('reviewOf') == task_id or task.get('investigationOf') == task_id:
            children.append({'id': cid, 'title': task.get('title'),
                             'status': task.get('status'),
                             'assignedTo': task.get('assignedTo'),
                             'failNote': task.get('failNote')})
    return children


@app.get('/api/replay/{task_id}')
async def task_replay(task_id: str, request: Request):
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    state = await asyncio.to_thread(get_state_from_db)
    task = (state.get('tasks') or {}).get(task_id)
    if not task:
        return JSONResponse({'error': f'unknown task {task_id}'},
                            status_code=404)
    summary = {k: task.get(k) for k in (
        'id', 'title', 'status', 'assignedTo', 'taskType', 'room',
        'createdAt', 'blockedAt', 'failNote', 'nextAction', 'budgetUsd',
        'deliveryState', 'libraryPath', 'reviewOf')}
    with _db() as conn:
        trail = _task_replay_rows(conn, task_id)
    return JSONResponse({
        'task': summary,
        'children': _child_cards(state or {}, task_id),
        'trail': trail,
        'trailCount': len(trail),
        'trailTruncated': len(trail) >= _TRAIL_LIMIT,
    })
