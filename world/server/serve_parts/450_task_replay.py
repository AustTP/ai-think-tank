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
        # details is freeform: JSON rows carry taskId, others are prose.
        # Only cast rows that even look like JSON; a malformed JSON row would
        # still raise, so fall back to the substring match on any error.
        try:
            rows = conn.execute(
                "SELECT ts, agent_id, action, details FROM action_log "
                "WHERE (details LIKE '{%' AND details::jsonb->>'taskId' = ?) "
                "   OR details LIKE ? "
                "ORDER BY ts ASC LIMIT ?",
                (task_id, like, _TRAIL_LIMIT),
            ).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT ts, agent_id, action, details FROM action_log "
                "WHERE details LIKE ? ORDER BY ts ASC LIMIT ?",
                (like, _TRAIL_LIMIT),
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


def _child_cards_from_rows(rows, task_id):
    """One-line status for every card whose reviewOf/investigationOf points
    at this task -- the repair/review chain an operator replays with it."""
    children = []
    for cid, task in rows:
        if task.get('reviewOf') == task_id or task.get('investigationOf') == task_id:
            children.append({'id': cid, 'title': task.get('title'),
                             'status': task.get('status'),
                             'assignedTo': task.get('assignedTo'),
                             'failNote': task.get('failNote')})
    return children


def _load_task_and_children(task_id):
    """The task record and its child cards WITHOUT the whole-state merge.

    get_state_from_db('copy') costs a ~1.9s whole-archive deepcopy for ONE
    task's summary (observed 2026-11-04: replay took ~6s, ~5s of it the
    merge). The live blob and the archive table are queried directly
    instead; JSON path filters do the child scan in the DB."""
    children = []
    task = None
    with _db() as conn:
        row = conn.execute(
            "SELECT blob FROM kv_state WHERE id = 1").fetchone()
        if row:
            data = json.loads(row[0])
            task = (data.get('tasks') or {}).get(task_id)
            children = _child_cards_from_rows(
                ((cid, t) for cid, t in (data.get('tasks') or {}).items()
                 if isinstance(t, dict)), task_id)
        if task is None:
            arow = conn.execute(
                'SELECT blob FROM task_archive WHERE id = ?',
                (task_id,)).fetchone()
            if arow:
                task = json.loads(arow[0])
        # Child scan over the archive. Postgres does it in the DB with a
        # JSON path filter; sqlite has no jsonb cast, so scan the (test-
        # sized) archive in Python.
        import db_backend as _dbm
        if _dbm.is_postgres():
            arows = conn.execute(
                "SELECT id, blob FROM task_archive "
                "WHERE blob::jsonb->>'reviewOf' = ? "
                "   OR blob::jsonb->>'investigationOf' = ?",
                (task_id, task_id)).fetchall()
        else:
            arows = [(cid, blob) for cid, blob in conn.execute(
                'SELECT id, blob FROM task_archive').fetchall()
                if _blob_refs((cid, blob), task_id)]
        children.extend(_child_cards_from_rows(
            ((cid, json.loads(b) if isinstance(b, str) else b)
             for cid, b in arows), task_id))
    return task, children


def _blob_refs(row, task_id):
    _cid, blob = row
    try:
        t = json.loads(blob) if isinstance(blob, str) else blob
    except Exception:
        return False
    return isinstance(t, dict) and (t.get('reviewOf') == task_id
                                    or t.get('investigationOf') == task_id)


@app.get('/api/replay/{task_id}')
async def task_replay(task_id: str, request: Request):
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    task, children = await asyncio.to_thread(_load_task_and_children, task_id)
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
        'children': children,
        'trail': trail,
        'trailCount': len(trail),
        'trailTruncated': len(trail) >= _TRAIL_LIMIT,
    })
