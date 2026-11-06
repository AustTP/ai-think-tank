"""Cross-village team migration (2026-11-06).

The operator's model: directors span villages -- a director's agent row
(his home, where his sim runs) stays in the parent while his TEAMS live
wherever the work lives; three or more teams sit under subdirectors who
span the same way. Moving a team is deliberate surgery, performed by the
PARENT process (the operator surface) talking to the DESTINATION village
through its own token-authenticated API -- each side mutates only its
own process's state, so no cross-process DB writes can be clobbered by a
running sim.

Parent (this side):  POST /api/admin/move-team-to-village
                     {teamId, villageId} -- admin key or the parent
                     token. Collects the team + its workers' rows and
                     open cards, POSTs them to the village's move-in
                     endpoint, then removes them locally and registers
                     the placement.

Village side:        POST /api/villages-self/move-in (X-Parent-Token) --
                     inserts the team row, its cards, and the workers'
                     roster/live/key rows into the village's own DB.

Refusals (both sides validate): unknown team, ACTIVE SPRINT on the team,
unregistered village, empty member set.
"""
import json
import os
import time
import urllib.request

from fastapi import Request
from fastapi.responses import JSONResponse

_MOVE_TOKEN_ENV = 'THINK_TANK_PARENT_TOKEN'
_OPEN_STATUSES = ('walking', 'working', 'needs_review', 'blocked',
                  'repairing', 'queued', None)


def _move_token_ok(request: Request) -> bool:
    expected = (os.environ.get(_MOVE_TOKEN_ENV)
                or _load_env().get(_MOVE_TOKEN_ENV, ''))
    if not expected:
        return False
    supplied = (request.headers.get('X-Parent-Token') or '').strip()
    return supplied == expected


def _is_director_row(roster_by_id, aid):
    row = roster_by_id.get(aid) or {}
    return bool(row.get('isDirector'))


def _active_sprint_for_team(state, team_id):
    for s in (state.get('sprints') or {}).values():
        if isinstance(s, dict) and s.get('status') == 'active' \
                and team_id in (s.get('teamIds') or []):
            return s.get('id')
    return None


def _collect_move_payload(state, team_id):
    """Everything the destination village needs, as plain data."""
    team = next((t for t in (state.get('teams') or [])
                 if isinstance(t, dict) and t.get('id') == team_id), None)
    if team is None:
        return None, 'unknown team'
    sprint = _active_sprint_for_team(state, team_id)
    if sprint:
        return None, f'team has an active sprint ({sprint})'
    roster_by_id = {r.get('id'): r for r in (state.get('agentRoster') or [])
                    if isinstance(r, dict)}
    members = [m for m in (team.get('members') or [])
               if not _is_director_row(roster_by_id, m)]
    members = sorted(set(members))
    if not members:
        return None, 'no workers to move'
    member_set = set(members)
    tasks = {}
    # Move the team's OWN cards plus a worker's TEAMLESS cards (a card
    # with no team follows its worker). A card a moving worker was
    # ASSIGNED but that belongs to another team STAYS with that team (its
    # assignee is cleared so the team re-staffs) -- the first real move
    # proved why: assignee-based collection dragged five parent-team
    # cards into a village DB their owners could no longer see.
    reassigned_elsewhere = {}
    for tid, t in (state.get('tasks') or {}).items():
        if not isinstance(t, dict):
            continue
        if t.get('status') not in _OPEN_STATUSES:
            continue
        team_of_card = t.get('teamId') or None
        if team_of_card == team_id:
            tasks[tid] = t
        elif team_of_card is None and t.get('assignedTo') in member_set:
            tasks[tid] = t  # teamless work follows its worker
        elif t.get('assignedTo') in member_set:
            reassigned_elsewhere[tid] = t
    queue = [q for q in (state.get('workQueue') or [])
             if isinstance(q, dict) and q.get('teamId') == team_id]
    with _db() as conn:
        rows = {}
        ph = ','.join('?' * len(members))
        for table in ('roster_agent', 'live_agent'):
            rows[table] = conn.execute(
                f'SELECT id, seq, blob, updated_at FROM {table}'
                f' WHERE id IN ({ph})', members).fetchall()
        keys = conn.execute(
            f'SELECT agent_id, secret_key, created_at FROM agent_keys'
            f' WHERE agent_id IN ({ph})', members).fetchall()
        # completed_deliverable blobs carry agentId inside the blob; the
        # portable filter avoids dialect-specific JSON functions by
        # parsing in python.
        deliverables = conn.execute(
            'SELECT id, seq, blob, updated_at FROM completed_deliverable'
            ' ORDER BY seq').fetchall()
        wanted = []
        for rid, seq, blob, upd in deliverables:
            try:
                aid = json.loads(blob).get('agentId')
            except Exception:
                continue
            if aid in member_set:
                wanted.append((rid, seq, blob, upd))
        backlog = conn.execute(
            'SELECT id, seq, blob, updated_at FROM backlog_request'
            ' ORDER BY seq').fetchall()
        wanted_backlog = []
        for rid, seq, blob, upd in backlog:
            try:
                bteam = json.loads(blob).get('teamId')
            except Exception:
                continue
            if bteam == team_id:
                wanted_backlog.append((rid, seq, blob, upd))
    return {
        'team': team,
        'members': members,
        'rosterRows': [{'id': r[0], 'seq': r[1], 'blob': r[2],
                        'updatedAt': r[3]} for r in rows['roster_agent']],
        'liveRows': [{'id': r[0], 'seq': r[1], 'blob': r[2],
                      'updatedAt': r[3]} for r in rows['live_agent']],
        'keyRows': [{'agentId': k[0], 'secretKey': k[1], 'createdAt': k[2]}
                    for k in keys],
        'tasks': tasks,
        # Cards left behind: assigned to a moving worker but owned by
        # another team. The parent clears their assignee after the move.
        'reassignedElsewhere': sorted(reassigned_elsewhere),
        'queue': queue,
        'deliverableRows': wanted,
        'backlogRows': wanted_backlog,
    }, None


@app.post('/api/villages-self/move-in')
async def village_move_in(request: Request):
    """VILLAGE side: receive a team + its people from the parent."""
    from serve import VILLAGE_MODE
    if not VILLAGE_MODE:
        return JSONResponse({'error': 'village process only'},
                            status_code=404)
    if not _move_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    team = (payload or {}).get('team')
    if not isinstance(team, dict) or not team.get('id'):
        return JSONResponse({'error': 'team row is required'}, status_code=400)
    from serve import save_state_to_db
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        teams = state.setdefault('teams', [])
        if any(t.get('id') == team['id'] for t in teams if isinstance(t, dict)):
            _state_abort()
            return JSONResponse({'error': 'team id already exists here'},
                                status_code=409)
        teams.append(team)
        tasks = state.setdefault('tasks', {})
        moved = payload.get('tasks') or {}
        for tid, t in moved.items():
            tasks[tid] = t
        queue = state.setdefault('workQueue', [])
        for q in payload.get('queue') or []:
            queue.append(q)
        with _db() as conn:
            for row in payload.get('rosterRows') or []:
                conn.execute(
                    'INSERT OR REPLACE INTO roster_agent (id, seq, blob,'
                    ' updated_at) VALUES (?, ?, ?, ?)',
                    (row['id'], row['seq'], row['blob'], row['updatedAt']))
            for row in payload.get('liveRows') or []:
                conn.execute(
                    'INSERT OR REPLACE INTO live_agent (id, seq, blob,'
                    ' updated_at) VALUES (?, ?, ?, ?)',
                    (row['id'], row['seq'], row['blob'], row['updatedAt']))
            for row in payload.get('keyRows') or []:
                conn.execute(
                    'INSERT OR REPLACE INTO agent_keys (agent_id,'
                    ' secret_key, created_at) VALUES (?, ?, ?)',
                    (row['agentId'], row['secretKey'], row['createdAt']))
            for row in payload.get('deliverableRows') or []:
                conn.execute(
                    'INSERT OR REPLACE INTO completed_deliverable (id, seq,'
                    ' blob, updated_at) VALUES (?, ?, ?, ?)',
                    (row[0], row[1], row[2], row[3]))
            for row in payload.get('backlogRows') or []:
                conn.execute(
                    'INSERT OR REPLACE INTO backlog_request (id, seq,'
                    ' blob, updated_at) VALUES (?, ?, ?, ?)',
                    (row[0], row[1], row[2], row[3]))
        save_state_to_db(state)
    except Exception as exc:
        _state_abort()
        return JSONResponse({'error': f'move-in failed: {exc}'[:200]},
                            status_code=500)
    return {'ok': True, 'team': team['id'],
            'members': payload.get('members') or [],
            'tasks': len(moved)}


@app.post('/api/admin/move-team-to-village')
async def admin_move_team_to_village(request: Request):
    """PARENT side: move a team + its workers into a village."""
    from serve import (verify_session, SESSION_COOKIE_NAME, log_action,
                       save_state_to_db)
    key = request.headers.get('X-Agent-Key')
    actor = _is_tank_admin(request) if key else None
    if not actor and not _move_token_ok(request) and \
            not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- tank admin, the '
                             'parent token, or the player only'},
                            status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    team_id = str((body or {}).get('teamId')
                  or request.query_params.get('teamId') or '').strip()
    village_id = str((body or {}).get('villageId')
                     or request.query_params.get('villageId') or '').strip()
    if not team_id or not village_id:
        return JSONResponse({'error': 'teamId and villageId are required'},
                            status_code=400)
    if (body or {}).get('dryRun') or request.query_params.get('dryRun'):
        _state = _state_begin()
        payload, err = _collect_move_payload(_state, team_id) \
            if _state else (None, 'state unavailable')
        if err:
            _state_abort()
            return JSONResponse({'error': err}, status_code=409)
        _state_abort()
        return {'dryRun': True, 'team': payload['team'],
                'members': payload['members'],
                'taskCount': len(payload['tasks']),
                'queueCount': len(payload['queue']),
                'deliverableCount': len(payload['deliverableRows']),
                'backlogCount': len(payload['backlogRows'])}
    with _db() as conn:
        reg = conn.execute(
            'SELECT api_base FROM villages_registry WHERE id = ?',
            (village_id,)).fetchone()
    if reg is None:
        return JSONResponse({'error': f'unknown village {village_id}'},
                            status_code=404)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    payload, err = _collect_move_payload(state, team_id)
    if err:
        _state_abort()
        return JSONResponse({'error': err}, status_code=409)
    # Tell the village FIRST: if it refuses, nothing has changed here.
    try:
        vbody = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{reg[0]}/api/villages-self/move-in", data=vbody, method='POST',
            headers={'Content-Type': 'application/json',
                     'X-Parent-Token':
                     os.environ.get(_MOVE_TOKEN_ENV) or
                     _load_env().get(_MOVE_TOKEN_ENV, '')})
        with urllib.request.urlopen(req, timeout=30) as resp:
            vres = json.loads(resp.read())
    except Exception as exc:
        _state_abort()
        return JSONResponse(
            {'error': f'village refused the move-in: '
                      f'{type(exc).__name__}: {exc}'[:240]},
            status_code=502)
    # Remove locally.
    members = payload['members']
    team_id = team_id
    state['teams'] = [t for t in (state.get('teams') or [])
                      if not (isinstance(t, dict) and t.get('id') == team_id)]
    tasks = state.get('tasks') or {}
    for tid in list(payload['tasks'].keys()):
        tasks.pop(tid, None)
    if payload['queue']:
        state['workQueue'] = [q for q in (state.get('workQueue') or [])
                              if q not in payload['queue']]
    for tid in payload.get('reassignedElsewhere') or []:
        card = tasks.get(tid)
        if isinstance(card, dict):
            card['assignedTo'] = None
            card['note'] = (str(card.get('note') or '') +
                            ' [assignee moved to a village; re-staffed '
                            f'{time.strftime("%Y-%m-%d")}]')[:400]
    ph = ','.join('?' * len(members))
    with _db() as conn:
        for table in ('roster_agent', 'live_agent'):
            conn.execute(f'DELETE FROM {table} WHERE id IN ({ph})', members)
        conn.execute(
            f'DELETE FROM agent_keys WHERE agent_id IN ({ph})', members)
        for row in payload['deliverableRows']:
            conn.execute('DELETE FROM completed_deliverable WHERE id = ?',
                         (row[0],))
        for row in payload['backlogRows']:
            conn.execute('DELETE FROM backlog_request WHERE id = ?',
                         (row[0],))
        conn.execute(
            'CREATE TABLE IF NOT EXISTS villages_teams ('
            ' village_id TEXT NOT NULL, team_id TEXT NOT NULL,'
            ' director_id TEXT, updated_at REAL,'
            ' PRIMARY KEY (village_id, team_id))')
        conn.execute(
            'INSERT INTO villages_teams (village_id, team_id, director_id,'
            ' updated_at) VALUES (?, ?, ?, ?)'
            ' ON CONFLICT(village_id, team_id) DO UPDATE SET'
            ' director_id = excluded.director_id,'
            ' updated_at = excluded.updated_at',
            (village_id, team_id, payload['team'].get('directorId'),
             time.time()))
    save_state_to_db(state)
    log_action(actor or 'operator-tool', 'team_moved_to_village',
               {'team': team_id, 'village': village_id,
                'members': members, 'tasks': len(payload['tasks'])},
               authorized=True)
    # Tell both sides' people.
    try:
        import sim as _sim
        _sim._log_governance(state, actor or 'admin',
                             'team_moved_to_village',
                             {'team': team_id, 'village': village_id,
                              'members': members})
        director_id = payload['team'].get('directorId')
        agent = (state.get('agents') or {}).get(director_id)
        if isinstance(agent, dict):
            box = agent.setdefault('mailbox', [])
            box.append({
                'kind': 'team_moved', 'read': False,
                'ts': int(time.time() * 1000),
                'about': team_id,
                'text': (f'Your team "{payload["team"].get("name") or team_id}" '
                         f'now lives in village {village_id} (its own '
                         'process and database). Your workers moved with '
                         'it; your agent row stayed home. Supervision '
                         'briefs for that team will be relayed to you '
                         'here. Send anything the team needs from the org '
                         'through the chief of staff.'),
            })
            del box[:-200]
        save_state_to_db(state)
    except Exception:
        pass
    return {'ok': True, 'team': team_id, 'village': village_id,
            'members': members, 'tasksMoved': len(payload['tasks']),
            'villageReport': vres}


@app.post('/api/villages-self/release-cards')
async def village_release_cards(request: Request):
    """VILLAGE side: hand cards back to the parent. Used to repair a move
    that dragged foreign-team cards into a village DB (the 2026-11-06 eli
    move did exactly that). Returns the full task dicts; the village
    removes them from its own state."""
    from serve import VILLAGE_MODE
    if not VILLAGE_MODE:
        return JSONResponse({'error': 'village process only'},
                            status_code=404)
    if not _move_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    wanted = set(str(x) for x in ((body or {}).get('taskIds') or []) if x)
    if not wanted:
        return JSONResponse({'error': 'taskIds required'}, status_code=400)
    from serve import save_state_to_db
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        tasks = state.get('tasks') or {}
        released = {}
        for tid in list(wanted):
            t = tasks.get(tid)
            if isinstance(t, dict):
                released[tid] = t
                tasks.pop(tid, None)
        save_state_to_db(state)
    finally:
        _state_abort()
    return {'ok': True, 'released': released}


@app.post('/api/admin/return-village-cards')
async def admin_return_village_cards(request: Request):
    """PARENT side: pull foreign-team cards BACK out of a village and
    re-home them here (assignee cleared so the owning team re-staffs)."""
    from serve import verify_session, SESSION_COOKIE_NAME, log_action, \
        save_state_to_db
    key = request.headers.get('X-Agent-Key')
    actor = _is_tank_admin(request) if key else None
    if not actor and not _move_token_ok(request) and \
            not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    village_id = str((body or {}).get('villageId') or '').strip()
    task_ids = [str(x) for x in ((body or {}).get('taskIds') or []) if x]
    if not village_id or not task_ids:
        return JSONResponse({'error': 'villageId and taskIds required'},
                            status_code=400)
    with _db() as conn:
        reg = conn.execute(
            'SELECT api_base FROM villages_registry WHERE id = ?',
            (village_id,)).fetchone()
    if reg is None:
        return JSONResponse({'error': f'unknown village {village_id}'},
                            status_code=404)
    token = (os.environ.get(_MOVE_TOKEN_ENV)
             or _load_env().get(_MOVE_TOKEN_ENV, ''))
    try:
        req = urllib.request.Request(
            f'{reg[0]}/api/villages-self/release-cards',
            data=json.dumps({'taskIds': task_ids}).encode(), method='POST',
            headers={'Content-Type': 'application/json',
                     'X-Parent-Token': token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            released = json.loads(resp.read()).get('released') or {}
    except Exception as exc:
        return JSONResponse(
            {'error': f'village release failed: '
                      f'{type(exc).__name__}: {exc}'[:240]}, status_code=502)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        tasks = state.setdefault('tasks', {})
        restored = []
        for tid, t in released.items():
            t['assignedTo'] = None
            t['note'] = (str(t.get('note') or '') +
                         ' [returned from village '
                         f'{village_id}; assignee cleared '
                         f'{time.strftime("%Y-%m-%d")}]')[:400]
            tasks[tid] = t
            restored.append(tid)
        if restored:
            save_state_to_db(state)
        else:
            _state_abort()
    except Exception:
        _state_abort()
        raise
    log_action(actor or 'operator-tool', 'village_cards_returned',
               {'village': village_id, 'restored': restored},
               authorized=True)
    return {'ok': True, 'restored': restored}
