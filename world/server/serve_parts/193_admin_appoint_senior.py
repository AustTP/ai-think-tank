"""Admin: appoint -- or revoke -- a chain root among the directors.

The director-of-a-director chain is recursive and DERIVED, not stored: a
director sits above others when those directors point their roster
`director` pointer at her. This endpoint performs the edit:

  * {directorId: X}        -- re-point every other team director at X,
    making X the chain's root (the cross-team oversight cards follow the
    derivation in sim._senior_most_director).
  * {revoke: true}         -- flatten the chain: every team director
    points back at the admin. The operator clarified (2026-11-06) that
    the directors stand EQUAL -- there is no senior-most director, so the
    cross-team card falls back to the chief of staff.

The regional VP and admins are never re-pointed in either mode.
Auth: tank admin key or the player session.
"""
from fastapi import Request
from fastapi.responses import JSONResponse


def _appoint_is_tank_admin(request):
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


@app.post('/api/admin/appoint-senior-director')
@_state_writer
async def admin_appoint_senior_director(request: Request):
    from serve import (verify_session, SESSION_COOKIE_NAME, log_action,
                       save_state_to_db)
    actor = _appoint_is_tank_admin(request)
    if not actor and not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- tank admin or the '
                             'player only'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    roster = [r for r in (state.get('agentRoster') or []) if isinstance(r, dict)]
    admin_id = next((r.get('id') for r in roster if r.get('isAdmin')), None)
    if (body or {}).get('revoke'):
        if not admin_id:
            return JSONResponse({'error': 'no admin in the roster'},
                                status_code=409)
        repointed = []
        for r in roster:
            if r.get('isAdmin') or r.get('villageChief') \
                    or not r.get('isDirector'):
                continue
            if r.get('director') != admin_id:
                r['director'] = admin_id
                repointed.append(r.get('id'))
        try:
            import sim as _sim
            _sim._log_governance(state, actor or 'admin',
                                 'senior_director_revoked',
                                 {'repointed': repointed, 'to': admin_id})
        except Exception:
            pass
        log_action(actor, 'senior_director_revoked',
                   {'repointed': repointed, 'to': admin_id}, authorized=True)
        save_state_to_db(state)
        return {'ok': True, 'revoked': True, 'reports': repointed,
                'to': admin_id}
    target = str((body or {}).get('directorId') or '').strip()
    if not target:
        return JSONResponse({'error': 'directorId is required'},
                            status_code=400)
    roster = [r for r in (state.get('agentRoster') or []) if isinstance(r, dict)]
    tgt = next((r for r in roster if r.get('id') == target), None)
    if tgt is None or target not in (state.get('agents') or {}):
        return JSONResponse({'error': f'no live agent {target}'},
                            status_code=404)
    if tgt.get('isAdmin') or tgt.get('villageChief'):
        return JSONResponse(
            {'error': 'the target must be a team director; admins and the '
                      'regional VP stand outside the recursion'},
            status_code=409)
    if not tgt.get('isDirector'):
        return JSONResponse({'error': f'{target} is not a director'},
                            status_code=409)
    repointed = []
    for r in roster:
        if r.get('id') == target or r.get('isAdmin') or r.get('villageChief'):
            continue
        if r.get('isDirector'):
            r['director'] = target
            repointed.append(r['id'])
    try:
        import sim as _sim
        _sim._log_governance(state, actor or 'admin',
                             'senior_director_appointed',
                             {'senior': target, 'reports': repointed})
    except Exception:
        pass
    _appoint_mail(state, target,
                  'You are appointed senior-most director: the top of the '
                  'director-of-director chain. You now hold the cross-team '
                  'oversight cards (a daily routine sweep with the team '
                  'metrics rollup, plus attention cards when teams need '
                  'help). Route work to the directors beneath you; do not '
                  'do their work.')
    log_action(actor, 'senior_director_appointed',
               {'senior': target, 'reports': repointed}, authorized=True)
    save_state_to_db(state)
    return {'ok': True, 'senior': target, 'reports': repointed}


def _appoint_mail(state, to_id, text):
    try:
        import sim as _sim
        row = (state.get('agents') or {}).get(to_id)
        if not isinstance(row, dict):
            return
        box = row.setdefault('mailbox', [])
        box.append({'text': f'Theo: {text}', 'read': False,
                    'ts': int(time.time() * 1000)})
        del box[:-200]
    except Exception:
        pass
