"""HTTP surface for durable confirmation proposals.

The router only records, confirms, lists, or cancels. It never executes an
external side effect. Execution belongs to the action's own endpoint, which
must call confirmations.assert_ready() with the same target and args.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import confirmations

router = APIRouter()


def _actor(request: Request) -> str | None:
    import serve as _serve
    if _serve.verify_session(request.cookies.get(_serve.SESSION_COOKIE_NAME)):
        return 'player'
    return _serve._agent_id_for_key(request.headers.get('X-Agent-Key'))


def _error(error: confirmations.ConfirmationError, status=409):
    return JSONResponse({'error': str(error), 'code': error.code}, status_code=status)


def _body(payload):
    return {'proposal': payload}


@router.post('/api/confirmations')
async def create_confirmation(request: Request):
    actor = _actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    try:
        proposal = confirmations.propose(
            body.get('action'), body.get('target'), body.get('args'),
            actor=actor, channel=body.get('channel') or 'web',
            ttl_s=body.get('ttlS') or confirmations.DEFAULT_TTL_S)
    except confirmations.ConfirmationError as error:
        return _error(error, 400)
    import serve as _serve
    _serve.log_action(actor, 'confirmation_proposed',
                      {'id': proposal['id'], 'action': proposal['action']},
                      authorized=True)
    return JSONResponse(_body(proposal), status_code=201)


@router.get('/api/confirmations')
async def list_confirmations(request: Request):
    actor = _actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    items = confirmations.list_for_actor(actor, request.query_params.get('status'))
    return JSONResponse({'items': items})


@router.get('/api/confirmations/{proposal_id}')
async def get_confirmation(proposal_id: str, request: Request):
    actor = _actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    item = confirmations.get(proposal_id)
    if not item or (actor != 'player' and item['actor'] != actor):
        return JSONResponse({'error': 'confirmation not found'}, status_code=404)
    return JSONResponse(_body(item))


@router.post('/api/confirmations/{proposal_id}/confirm')
async def confirm_confirmation(proposal_id: str, request: Request):
    actor = _actor(request)
    if actor != 'player':
        return JSONResponse({'error': 'only the player can confirm'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    try:
        item = confirmations.confirm(
            proposal_id, str((body or {}).get('phrase') or ''), actor)
    except confirmations.ConfirmationError as error:
        return _error(error)
    import serve as _serve
    _serve.log_action('player', 'confirmation_confirmed',
                      {'id': item['id'], 'action': item['action']},
                      authorized=True)
    return JSONResponse(_body(item))


@router.post('/api/confirmations/{proposal_id}/cancel')
async def cancel_confirmation(proposal_id: str, request: Request):
    actor = _actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        item = confirmations.cancel(proposal_id, actor)
    except confirmations.ConfirmationError as error:
        return _error(error)
    import serve as _serve
    _serve.log_action(actor, 'confirmation_cancelled',
                      {'id': item['id'], 'action': item['action']},
                      authorized=True)
    return JSONResponse(_body(item))
