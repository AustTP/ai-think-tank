"""HTTP surface for bounded live video coverage."""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

import live_coverage

router = APIRouter()
HERE = os.path.dirname(os.path.abspath(__file__))


def _reader(request):
    """Any player session or valid agent key may read coverage observations."""
    import serve
    if serve._require_player_session(request):
        return 'player'
    actor = serve._resolve_requester(request)
    if actor and serve.verify_agent_key(
            actor, request.headers.get('X-Agent-Key')) is True:
        return actor
    return None


@router.get('/live-coverage')
async def live_coverage_page(request: Request):
    import serve
    if not serve._require_player_session(request):
        return RedirectResponse('/', status_code=303)
    return FileResponse(os.path.join(HERE, 'live_coverage.html'))


@router.post('/api/live-coverage/start')
async def live_coverage_start(request: Request):
    import serve
    authed, actor = serve._operator_gate(request)
    if not authed:
        return JSONResponse(
            {'error': 'Unauthorized -- player session or director/admin agent required'},
            status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    try:
        run = live_coverage.start(
            url=body.get('url') or '',
            label=body.get('label'),
            interval_s=body.get('intervalS', body.get('interval_s')),
            max_seconds=body.get('maxSeconds', body.get('max_seconds')),
            max_frames=body.get('maxFrames', body.get('max_frames')),
            max_usd=body.get('maxUsd', body.get('max_usd')),
            actor=actor,
        )
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)
    return JSONResponse({'ok': True, 'run': run})


@router.get('/api/live-coverage/status')
async def live_coverage_status(request: Request, run_id: str | None = None,
                               limit: int = 30):
    actor = _reader(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    max_items = max(1, min(int(limit or 30), 200))
    data = live_coverage.status(run_id=run_id, max_items=max_items)
    if run_id and not data.get('run'):
        return JSONResponse({'error': f'unknown run {run_id}', **data},
                            status_code=404)
    return JSONResponse({'ok': True, **data})


@router.post('/api/live-coverage/stop')
async def live_coverage_stop(request: Request):
    import serve
    authed, actor = serve._operator_gate(request)
    if not authed:
        return JSONResponse(
            {'error': 'Unauthorized -- player session or director/admin agent required'},
            status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    run_id = str(body.get('runId') or body.get('run_id') or '').strip()
    if not run_id:
        return JSONResponse({'error': 'runId is required'}, status_code=400)
    try:
        run = live_coverage.stop(run_id)
    except KeyError:
        return JSONResponse({'error': f'unknown run {run_id}'}, status_code=404)
    serve.log_action(actor, 'live_coverage_stop_requested',
                     {'runId': run_id}, authorized=True)
    return JSONResponse({'ok': True, 'run': run})
