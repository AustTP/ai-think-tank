"""Admin: retire a team and redistribute its people (2026-11-06).

The operator's org rule: the regional VP must not also direct a team -- nora
held both the villageChief role and "Supporting Director's Team". Dissolving
a team is state surgery with three coupled moves, so it lives in one
transactional state-writer endpoint instead of hand edits:

  1. Every member's roster `director` pointer moves to a receiving team (a
     team IS its director: agent_team_ref resolves membership through that
     pointer, so this is the load-bearing link).
  2. The retired team's open tasks follow their assignee's new team.
  3. The team row itself is removed.

Refuses an active sprint (close it first), an unknown team, and retiring the
last team. Auth: tank admin key or the player session, like the stale-card
endpoint.
"""
from fastapi import Request
from fastapi.responses import JSONResponse


def _retire_is_tank_admin(request):
    from serve import _agent_id_for_key, verify_agent_key, get_state_from_db
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    if agent_id and verify_agent_key(agent_id, key) is True:
        try:
            state = get_state_from_db(archive_merge='none') or {}
        except Exception:
            return None
        for d in state.get('agentRoster') or []:
            if d.get('id') == agent_id and d.get('isAdmin'):
                return agent_id
    return None


@app.post('/api/admin/retire-team')
@_state_writer
async def admin_retire_team(request: Request):
    from serve import verify_session, SESSION_COOKIE_NAME, log_action
    actor = _retire_is_tank_admin(request)
    if not actor and not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- tank admin or the '
                             'player only'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    team_id = str((body or {}).get('teamId') or '').strip()
    reason = str((body or {}).get('reason') or '').strip()[:300]
    if not team_id:
        return JSONResponse({'error': 'teamId is required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    teams = state.get('teams') or []
    team = next((t for t in teams
                 if isinstance(t, dict) and t.get('id') == team_id), None)
    if team is None:
        return JSONResponse({'error': f'no team {team_id}'}, status_code=404)
    active_sprints = [sid for sid, s in (state.get('sprints') or {}).items()
                      if isinstance(s, dict) and s.get('status') == 'active'
                      and team_id in (s.get('teamIds') or [])]
    if active_sprints:
        return JSONResponse(
            {'error': f'team has an active sprint: {active_sprints[0]}'},
            status_code=409)
    remaining = [t for t in teams
                 if isinstance(t, dict) and t.get('id') != team_id and t.get('id')]
    if not remaining:
        return JSONResponse({'error': 'refusing to retire the last team'},
                            status_code=409)
    members = [m for m in (team.get('members') or []) if m]
    if not members:
        members = [r.get('id') for r in (state.get('agentRoster') or [])
                   if isinstance(r, dict) and r.get('director') == team_id
                   and r.get('id')]
    receivers = [(t.get('id'), t.get('directorId') or t.get('id'))
                 for t in remaining]
    assignments = {}
    for i, mid in enumerate(members):
        rid, rdir = receivers[i % len(receivers)]
        assignments[mid] = {'teamId': rid, 'directorId': rdir}
    # 1. roster pointers (the load-bearing membership link) + receiving
    #    teams' member lists.
    moved = []
    for r in (state.get('agentRoster') or []):
        if isinstance(r, dict) and r.get('id') in assignments:
            r['director'] = assignments[r['id']]['directorId']
            moved.append(r['id'])
    for tid, _dir in receivers:
        recv = next(t for t in remaining if t.get('id') == tid)
        got = [m for m in members if assignments[m]['teamId'] == tid]
        if got:
            recv['members'] = list(dict.fromkeys(
                list(recv.get('members') or []) + got))
    # 2. open tasks, queued items, and pending backlog requests follow their
    #    people; unassigned work drops its teamId so the owner-director chain
    #    resolves through the assignee's roster pointer instead of dangling.
    retasked = 0
    for t in (state.get('tasks') or {}).values():
        if not isinstance(t, dict) or t.get('teamId') != team_id:
            continue
        if t.get('status') in ('done', 'cancelled', 'abandoned'):
            continue
        dest = assignments.get(t.get('assignedTo'))
        t['teamId'] = dest['teamId'] if dest else None
        retasked += 1
    for w in (state.get('workQueue') or []):
        if isinstance(w, dict) and w.get('teamId') == team_id:
            dest = assignments.get(w.get('assignedTo'))
            w['teamId'] = dest['teamId'] if dest else None
    for r in (state.get('backlogRequests') or []):
        if isinstance(r, dict) and r.get('teamId') == team_id:
            r['teamId'] = None
    # 3. the row itself.
    state['teams'] = [t for t in teams
                      if not (isinstance(t, dict) and t.get('id') == team_id)]
    try:
        import sim as _sim
        _sim._log_governance(state, actor or 'admin', 'team_dissolved', {
            'team': team_id,
            'members': moved,
            'retasked': retasked,
            'reason': reason or 'org restructure',
        })
    except Exception:
        pass
    _retire_mail(state, team_id,
                 f'Your team "{team.get("name") or team_id}" is retired; its '
                 f'workers were redistributed ({", ".join(moved)}). You remain '
                 'the regional VP: the village scan and senior-tier routing '
                 'are yours; team directing is not.')
    for tid, rdir in receivers:
        got = [m for m in members if assignments[m]['teamId'] == tid]
        if got:
            _retire_mail(state, rdir,
                         f'{len(got)} worker(s) joined your team from the '
                         f'retired "{team.get("name") or team_id}": '
                         f'{", ".join(got)}.')
    log_action(actor, 'team_dissolved', {'team': team_id, 'members': moved,
                                         'retasked': retasked},
               authorized=True)
    return {'ok': True, 'team': team_id, 'membersMoved': moved,
            'tasksRetasked': retasked,
            'assignments': {m: a['teamId'] for m, a in assignments.items()}}


def _retire_mail(state, to_id, text):
    try:
        import sim as _sim
        target = (state.get('agents') or {}).get(to_id)
        if not isinstance(target, dict):
            return
        box = target.setdefault('mailbox', [])
        box.append({'text': f'Theo: {text}', 'read': False,
                    'ts': int(time.time() * 1000)})
        del box[:-200]
    except Exception:
        pass
