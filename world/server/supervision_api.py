"""Per-director, per-project supervision levels (0-5)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import core.governance as governance

router = APIRouter()


def _caller(request):
    import serve
    if serve.verify_session(request.cookies.get(serve.SESSION_COOKIE_NAME)):
        return 'player'
    requester = serve._resolve_requester(request)
    if requester and serve.verify_agent_key(
            requester, request.headers.get('X-Agent-Key')) is True:
        return requester
    return None


@router.get('/api/supervision')
async def get_supervision(request: Request):
    import serve
    actor = _caller(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = serve.get_state_from_db() or {}
    levels = state.get('directorSupervision') or {}
    if actor != 'player' and not governance.is_admin(state, actor):
        levels = {actor: levels.get(actor, {})}
    return JSONResponse({'ok': True, 'directorSupervision': levels,
                         'min': governance.SUPERVISION_MIN,
                         'max': governance.SUPERVISION_MAX,
                         'default': governance.SUPERVISION_DEFAULT})


@router.put('/api/supervision')
async def put_supervision(request: Request):
    import serve
    actor = _caller(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    state = serve.get_state_from_db() or {}
    director_id = (body.get('directorId') or actor).strip()
    if actor != 'player' and not governance.is_admin(state, actor):
        director_id = actor
    if not governance.is_director(state, director_id):
        return JSONResponse({'error': f'{director_id} is not a director'},
                            status_code=400)
    project = (body.get('project') or '').strip() or None
    try:
        entry = governance.set_supervision_level(
            state, director_id, body.get('level'), project)
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    serve.save_state_to_db(state)
    serve.log_action(actor, 'supervision_level_set',
                     {'directorId': director_id, 'project': project,
                      'level': entry.get(project or '_default')},
                     authorized=True)
    return JSONResponse({'ok': True, 'directorId': director_id,
                         'project': project or '_default',
                         'level': entry.get(project or '_default')})
