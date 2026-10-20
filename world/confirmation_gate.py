"""Small execution gate that binds a write to a durable confirmation."""
from __future__ import annotations

import confirmations


def _serve():
    import serve
    return serve


def required() -> bool:
    env = _serve()._load_env()
    return (env.get('REQUIRE_CONFIRMATION_FOR_EXECUTION') or '0').strip().lower() in (
        '1', 'true', 'yes', 'on')


def gate(body, actor, action, target, args):
    """Return (response_or_None, confirmation_id_or_None). With the env gate
    off, a caller that sends confirmationId still gets the target binding check;
    with it on, a missing confirmation gets a 428 and a fresh proposal."""
    serve = _serve()
    confirmation_id = str(body.get('confirmationId') or '').strip()
    try:
        if confirmation_id:
            confirmations.assert_ready(confirmation_id, action, target, args, actor)
            return None, confirmation_id
        if not required():
            return None, None
        proposal = confirmations.propose(
            action, target, args, actor=actor,
            channel=str(body.get('channel') or 'api'))
        serve.log_action(actor, 'confirmation_required',
                         {'id': proposal['id'], 'action': action},
                         authorized=True)
        return serve.JSONResponse({
            'confirmationRequired': True,
            'confirmationId': proposal['id'],
            'phrase': proposal['phrase'],
            'expiresAt': proposal['expires_at'],
            'note': 'The player must confirm this exact phrase before the action runs.',
        }, status_code=428), None
    except confirmations.ConfirmationError as error:
        return serve.JSONResponse({'error': str(error), 'code': error.code},
                                  status_code=409), None


def finish(confirmation_id, status='executed', error=None):
    if not confirmation_id:
        return
    try:
        confirmations.finish(confirmation_id, status, error)
    except Exception:
        pass
