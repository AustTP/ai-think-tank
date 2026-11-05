"""Backstage rooms (2026-11-05).

Owner directive: agents get a place to coordinate that is not bound by
the player's physical world -- they can enter a BLANK room, never
physically appear in rooms the player sees, and interact without
pathfinding or door geometry.

Design (mirroring the commandcenter precedent, which is already entered
from anywhere via a HUD button and has no door):

  - `backstage` is a room id the client knows about (rooms.js ROOMS) but
    that has NO door trigger and NO outdoor presence. An agent with
    inRoom == 'backstage' renders in NO room the player visits (agents
    show inside a room view only when the player is in the same room),
    so backstage agents are invisible unless the player deliberately
    opens the Backstage view -- the owner's audit peek.
  - Entering is a STATE TRANSITION, not a walk: the server sets
    inRoom='backstage' directly and clears any residual path. No
    movement physics applies.
  - Interaction is unchanged: same-room co-presence and mail work exactly
    as in any other room, so cross-team sessions need no pathfinding.
  - Guardrails: backstage is not a work room -- it is not in the
    delegatable/valued room sets, tasks can never target it, and an
    agent busy on a task must finish or drop the task before entering
    (no mid-task teleports). Room-scoped tools keep their real rooms.

Endpoints (agent key = self only; player/admin = anyone):

  POST /api/backstage/enter   {}            -> inRoom='backstage'
  POST /api/backstage/exit    {}            -> inRoom=null
  GET  /api/backstage/who                   -> who is backstage now

The server anchors the backstage value through the autosave merge (see
_merge_server_owned_agent in 200_post_state): the client's 5s autosave
must not clobber a server-written backstage presence.
"""
import time

_BACKSTAGE_ROOM = 'backstage'


def _backstage_agent(state, agent_id):
    agents = state.get('agents')
    if not isinstance(agents, dict):
        return None
    rec = agents.get(agent_id)
    return rec if isinstance(rec, dict) else None


def _backstage_busy(rec):
    """An agent mid-task may not teleport: the walk/task machinery owns her
    until the task ends. busy/taskId are the client-sim's own markers."""
    if rec.get('task') or rec.get('taskId') or rec.get('busy'):
        return True
    return False


@app.post('/api/backstage/enter')
@_state_writer
async def api_backstage_enter(request: Request):
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    is_agent = agent_id and verify_agent_key(agent_id, key) is True
    is_player = bool(verify_session(request.cookies.get(SESSION_COOKIE_NAME)))
    if not is_agent and not is_player:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(agent_id or 'player'):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = agent_id
    if is_player:
        target = str(body.get('agentId') or '').strip() or None
    if not target:
        return JSONResponse({'error': 'agentId required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    rec = _backstage_agent(state, target)
    if rec is None:
        return JSONResponse({'error': 'no such agent'}, status_code=404)
    if _backstage_busy(rec):
        return JSONResponse(
            {'error': 'agent is mid-task; finish or drop the task before '
                      'going backstage'}, status_code=409)
    was = rec.get('inRoom')
    rec['inRoom'] = _BACKSTAGE_ROOM
    # Teleport semantics: no residual walk state may survive the transition.
    for f in ('path', 'pathIndex', 'pathIndex'.lower(), 'pathTarget',
              'path_target', 'roomX', 'roomY'):
        if f in rec:
            rec[f] = None
    save_state_to_db(state)
    log_action(target, 'backstage_enter',
               {'from': was or None}, authorized=True)
    return JSONResponse({'ok': True, 'inRoom': _BACKSTAGE_ROOM,
                         'was': was})


@app.post('/api/backstage/exit')
@_state_writer
async def api_backstage_exit(request: Request):
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    is_agent = agent_id and verify_agent_key(agent_id, key) is True
    is_player = bool(verify_session(request.cookies.get(SESSION_COOKIE_NAME)))
    if not is_agent and not is_player:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(agent_id or 'player'):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = agent_id
    if is_player:
        target = str(body.get('agentId') or '').strip() or None
    if not target:
        return JSONResponse({'error': 'agentId required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    rec = _backstage_agent(state, target)
    if rec is None:
        return JSONResponse({'error': 'no such agent'}, status_code=404)
    if rec.get('inRoom') != _BACKSTAGE_ROOM:
        return JSONResponse({'ok': True, 'inRoom': rec.get('inRoom'),
                             'note': 'was not backstage'})
    rec['inRoom'] = None
    save_state_to_db(state)
    log_action(target, 'backstage_exit', {}, authorized=True)
    return JSONResponse({'ok': True, 'inRoom': None})


@app.get('/api/backstage/who')
async def api_backstage_who(request: Request):
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    is_agent = agent_id and verify_agent_key(agent_id, key) is True
    is_player = bool(verify_session(request.cookies.get(SESSION_COOKIE_NAME)))
    if not is_agent and not is_player:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(agent_id or 'player'):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    state = get_state_from_db() or {}
    agents = state.get('agents') or {}
    who = []
    for aid, rec in agents.items():
        if isinstance(rec, dict) and rec.get('inRoom') == _BACKSTAGE_ROOM:
            who.append({'id': aid, 'name': rec.get('name'),
                        'busy': _backstage_busy(rec)})
    log_action(agent_id or 'player', 'backstage_who_read',
               {'count': len(who)}, authorized=True)
    return JSONResponse({'room': _BACKSTAGE_ROOM, 'agents': who})
