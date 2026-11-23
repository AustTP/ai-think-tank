# Team roster + per-agent identity panel data (operator, 2026-11-23).
# The pulse page gains a roster section: per-agent name, role, team, color,
# mission, one-line summary, plus avatar availability. Per-agent identity
# comes from state['agents'][id] (name/role/color/profile.mission -- the
# same fields agent-dirs/_write_file mirror into agent.json), NOT from the
# prototypes mirror (sync_prototypes is a work-in-progress snapshot, never
# identity). Avatars: /api/avatar/{id}/{orientation}.png serves the
# generated art; the endpoint 'hasAvatar' flag lets the page skip 404s.
from urllib.parse import quote as _urlquote


def _roster_avatar_url(agent_id, orientation='front'):
    return '/api/avatar/{}/{}.png'.format(_urlquote(agent_id), orientation)


def _agent_has_avatar_dir(agent_id):
    base = os.path.join(AGENTS_DIR, str(agent_id), 'avatars')
    return os.path.isdir(base)


def team_roster_payload(state=None):
    """The roster data payload for the pulse page: one row per agent with
    identity + role + team + color + mission + summary + avatar flag."""
    state = state if state is not None else get_state_from_db()
    agents = state.get('agents') or {}
    teams = state.get('teams') or []
    team_of = {}
    for t in teams:
        if not isinstance(t, dict):
            continue
        for m in (t.get('members') or []):
            if isinstance(m, str):
                team_of[m] = t.get('id') or ''
            elif isinstance(m, dict):
                mid = m.get('id') or m.get('agentId') or ''
                if mid:
                    team_of[mid] = t.get('id') or ''
    rows = []
    for aid, a in agents.items():
        if not isinstance(a, dict):
            continue
        prof = a.get('profile') or {}
        summary = str(prof.get('mission') or a.get('mission') or '')[:140]
        rows.append({
            'id': str(aid),
            'name': a.get('name') or str(aid),
            'role': a.get('role') or '',
            'teamId': team_of.get(str(aid), ''),
            'color': a.get('color') or '',
            'mission': summary,
            'busy': bool(a.get('busy')),
            'avatarUrl': _roster_avatar_url(str(aid)),
            'hasAvatar': _agent_has_avatar_dir(str(aid)),
        })
    rows.sort(key=lambda r: (r['teamId'] == '', r['teamId'], r['name'].lower()))
    return {'ok': True, 'agents': rows}


@app.get('/api/team-roster')
async def team_roster(request: Request):
    """Player-session read of the roster payload (one row per agent:
    identity, team, color, mission, avatar URL + availability)."""
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    import json as _json
    return JSONResponse(team_roster_payload())


@app.get('/api/agent-summary/{agent_id}')
async def agent_summary(agent_id: str, request: Request):
    """One agent's card summary: identity + mission + avatar availability.
    Player-session read; unknown ids 404 like the avatar route."""
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    import json as _json
    state = get_state_from_db()
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not isinstance(a, dict):
        return JSONResponse({'error': 'unknown agent'}, status_code=404)
    prof = a.get('profile') or {}
    return JSONResponse({
        'ok': True,
        'id': agent_id,
        'name': a.get('name') or agent_id,
        'role': a.get('role') or '',
        'color': a.get('color') or '',
        'mission': str(prof.get('mission') or '')[:300],
        'avatarUrl': _roster_avatar_url(agent_id),
        'hasAvatar': _agent_has_avatar_dir(agent_id),
    })
