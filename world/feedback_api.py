"""HTTP surface for the player feedback page."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from feedback_store import FeedbackStore

router = APIRouter()
HERE = os.path.dirname(os.path.abspath(__file__))

TASTE_KEYS = ('bold_calm', 'dense_sparse', 'playful_serious',
              'premium_utilitarian', 'technical_accessible')
ACTIVE = {'walking', 'working', 'needs_review', 'repairing', 'blocked',
          'queued', 'assigned'}


def _store():
    import serve
    return FeedbackStore(serve.DB_PATH)


def _player(request):
    import serve
    if serve.verify_session(request.cookies.get(serve.SESSION_COOKIE_NAME)):
        return 'player'
    return None


def _task_map(state):
    tasks = (state or {}).get('tasks')
    if isinstance(tasks, dict):
        return tasks
    if isinstance(tasks, list):
        return {t.get('id'): t for t in tasks if isinstance(t, dict) and t.get('id')}
    return {}


def _text(value, limit=260):
    if isinstance(value, dict):
        value = value.get('summary') or value.get('note') or value.get('text') or ''
    return str(value or '')[:limit]


def _work_items(state):
    now = time.time() * 1000
    items = []
    for tid, task in _task_map(state).items():
        if not isinstance(task, dict):
            continue
        status = task.get('status') or 'unknown'
        updated = task.get('updatedAt') or task.get('lastProgressAt') or task.get('createdAt') or 0
        recent = now - float(updated or 0) <= 24 * 3600 * 1000
        if status not in ACTIVE and not (status == 'done' and recent):
            continue
        project = task.get('projectLabel') or task.get('projectId') or task.get('teamId') or ''
        items.append({
            'id': tid,
            'title': task.get('title') or tid,
            'status': status,
            'assignee': task.get('assignedTo') or task.get('assignee') or '',
            'room': task.get('room') or '',
            'team': task.get('teamId') or task.get('productId') or '',
            'project': str(project)[:140],
            'summary': _text(task.get('goal') or task.get('instructions') or task.get('note')),
            'result': _text(task.get('result') or task.get('note'), 400),
            'progress': task.get('progress'),
            'updatedAt': updated,
            'ageMinutes': int(max(0, now - float(updated or now)) / 60000),
            'active': status in ACTIVE,
        })
    items.sort(key=lambda x: (not x['active'], -float(x.get('updatedAt') or 0)))
    return items[:60]


@router.get('/feedback')
async def feedback_page():
    return FileResponse(os.path.join(HERE, 'feedback.html'))


@router.get('/api/work-items')
async def work_items(request: Request):
    import serve
    if not _player(request):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = serve.get_state_from_db() or {}
    items = _work_items(state)
    return JSONResponse({'ok': True, 'items': items,
                         'activeCount': sum(1 for i in items if i['active']),
                         'refreshedAt': int(time.time() * 1000)})


@router.get('/api/feedback')
async def list_feedback(request: Request, workItemId: str | None = None,
                        limit: int = 50):
    if not _player(request):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return JSONResponse({'ok': True, 'feedback': _store().list(
        work_item_id=workItemId, player='player', limit=limit)})


@router.post('/api/feedback')
async def add_feedback(request: Request):
    if not _player(request):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    try:
        fid = _store().add(
            str(body.get('workItemId') or ''),
            'player',
            body.get('sentiment'),
            body.get('works'),
            body.get('notes') or '',
            body.get('ratings'),
            body.get('quickActions'))
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    return JSONResponse({'ok': True, 'id': fid})


@router.get('/api/taste')
async def get_taste(request: Request):
    if not _player(request):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    taste = _store().get_taste('player')
    weights = {key: int(taste.get('weights', {}).get(key, 50)) for key in TASTE_KEYS}
    return JSONResponse({'ok': True, 'taste': {**taste, 'weights': weights},
                         'keys': list(TASTE_KEYS)})


@router.put('/api/taste')
async def put_taste(request: Request):
    if not _player(request):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    taste = _store().set_taste('player', body.get('weights') or {},
                               body.get('notes') or '')
    weights = {key: int(taste.get('weights', {}).get(key, 50)) for key in TASTE_KEYS}
    return JSONResponse({'ok': True, 'taste': {**taste, 'weights': weights}})
