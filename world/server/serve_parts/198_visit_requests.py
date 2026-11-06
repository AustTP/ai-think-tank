"""AGENT-INITIATED VISITS (2026-11-06).

An agent walks between villages on her own initiative: she asks, the org
checks her out, and the same stop -> surgery -> start machinery from
197_visits moves her. The agent-facing surface:

  POST /api/visit-requests         {villageId, hours, reason}
      the key owner asks to VISIT a village. Guardrails:
        - no open task (finish or hand off first -- the org's rule)
        - one visit per agent per 12h (no wander-loops)
        - at most 3 visitors org-wide at once (the laptop still has to
          run the tank)
        - the village must be registered with transport
      On grant she is MOVED (temporarily) and the visit mail is in her
      blob when she arrives; theo is told.

  POST /api/visit-requests/return  {}
      the key owner walks HERSELF home early. Nobody else's visit can be
      ended this way.

Why auto-grant and not a theo approval step: the guardrails ARE the
policy. An agent with no open work, inside the duration and concurrency
caps, gathering knowledge, is exactly what visits are FOR; an approval
queue would just add latency. The reason string is mandatory and is
mailed to theo, so every visit is observable after the fact.
"""
import time

from fastapi import Request
from fastapi.responses import JSONResponse


def _visit_actor(request: Request):
    """The authenticated principal for the agent-facing visit endpoints:
    the presented X-Agent-Key's OWNER (a player session is not an agent
    and cannot walk)."""
    return _agent_id_for_key(request.headers.get('X-Agent-Key'))


def _open_visit_count():
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT count(*) FROM villages_visits'
                ' WHERE returned_at IS NULL').fetchone()
        return row[0]
    except Exception:
        return 0


def _hours_since_last_visit(agent_id):
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT started_at FROM villages_visits'
                ' WHERE agent_id = ? ORDER BY started_at DESC LIMIT 1',
                (agent_id,)).fetchone()
        if row is None:
            return None
        return (time.time() - row[0]) / 3600
    except Exception:
        return None


@app.post('/api/visit-requests')
async def agent_visit_request(request: Request):
    actor = _visit_actor(request)
    if not actor:
        return JSONResponse({'error': 'agent key required'}, status_code=401)
    if not check_rate_limit(actor):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    village_id = str((body or {}).get('villageId') or '').strip()
    try:
        hours = float((body or {}).get('hours') or 4)
    except (TypeError, ValueError):
        hours = 4.0
    hours = max(0.5, min(hours, 8))
    reason = str((body or {}).get('reason') or '').strip()
    if not village_id:
        return JSONResponse({'error': 'villageId required'}, status_code=400)
    if len(reason) < 20:
        return JSONResponse(
            {'error': 'say what you are going there to learn or whom to '
                      'talk to (a real reason, at least a sentence)'},
            status_code=400)
    since = _hours_since_last_visit(actor)
    if since is not None and since < 12:
        return JSONResponse(
            {'error': f'you visited {since:.1f}h ago; visits need '
                      '12h between them'}, status_code=429)
    if _open_visit_count() >= 3:
        return JSONResponse(
            {'error': 'three agents are already out visiting; try again '
                      'when someone comes home'}, status_code=429)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        result = _perform_visit(state, actor, village_id, hours, reason,
                                actor)
        if isinstance(result, dict):
            try:
                theo = (state.get('agents') or {}).get('theo')
                if isinstance(theo, dict):
                    box = theo.setdefault('mailbox', [])
                    box.append({
                        'kind': 'visit', 'read': False,
                        'ts': int(time.time() * 1000),
                        'text': f'{actor} asked to visit village '
                                f'{village_id} and went (about {hours:g}h).'
                                f' Reason: {reason}',
                    })
                    del box[:-200]
                from serve import save_state_to_db
                save_state_to_db(state)
            except Exception:
                pass
        return result
    finally:
        _state_abort()


@app.post('/api/visit-requests/return')
async def agent_visit_return(request: Request):
    actor = _visit_actor(request)
    if not actor:
        return JSONResponse({'error': 'agent key required'}, status_code=401)
    if not check_rate_limit(actor):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    return _perform_return(actor, actor, forced=True)
