"""Programs (2026-11-07): the program-level roll-up for multi-team work.

A large ask decomposes across TEAMS, but nothing above the team tracked the
whole: per-team retros exist, yet no single record said "which of the 12
pieces landed, which is blocked, and is it time to file the change
request". A program is that record: {title, goal, ownerAgentId (normally
the chief of staff), pieceSpecs (team-pinned pieces with dependsOn)}.

Lifecycle the operator directed (2026-11-07):
  1. The operator (or a director) creates the program. The engine mints a
     directed PLANNING card to the owner: confer with the senior directors,
     decide team creation, and break the pieces into sprints under the
     owning directors.
  2. Teams execute the pieces (sprint cards, peer-review gates, grade
     floors). Overlapping pieces may be pulled from the shared backlog by
     any team already working the overlap.
  3. The engine roll-up (166__programs.py) matches pieces to live tasks
     every pass. When every piece is done/validated, it stamps the program
     and mints the owner a CHANGE-REQUEST card: create the selfedit push
     window(s) (one CR, or several given the scale) and merge.

Pieces are SPECS until refinement mints their cards; the roll-up matches by
title, so the spec must carry the eventual card title verbatim.
"""
from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse


def _validate_program(body):
    """Shared create/update validation. Returns (spec, error)."""
    if not isinstance(body, dict):
        return None, 'body must be a JSON object'
    title = str(body.get('title') or '').strip()
    if not title:
        return None, 'title is required'
    owner = str(body.get('ownerAgentId') or '').strip()
    if not owner:
        return None, 'ownerAgentId is required (normally the chief of staff)'
    pieces = body.get('pieces')
    if not isinstance(pieces, list) or not pieces:
        return None, 'pieces must be a non-empty list'
    clean = []
    for p in pieces:
        if not isinstance(p, dict):
            return None, 'each piece must be an object'
        ptitle = str(p.get('title') or '').strip()
        if not ptitle:
            return None, 'each piece needs a title'
        clean.append({
            'title': ptitle,
            'teamId': (str(p.get('teamId') or '').strip() or None),
            'taskType': (str(p.get('taskType') or '').strip() or 'code'),
            'deliverable': (str(p.get('deliverable') or '').strip() or 'code'),
            'dependsOn': [str(d) for d in (p.get('dependsOn') or [])],
            'instructions': (str(p.get('instructions') or '').strip() or None),
        })
    spec = {
        'title': title,
        'goal': (str(body.get('goal') or '').strip() or None),
        'ownerAgentId': owner,
        'pieces': clean,
        'contextFiles': [str(f) for f in (body.get('contextFiles') or [])][:10],
    }
    return spec, None


@app.post('/api/intent/programs')
@_state_writer
async def intent_create_program(request: Request):
    """Create a program: {title, ownerAgentId, goal?, pieces: [{title,
    teamId?, taskType?, deliverable?, dependsOn?, instructions?}],
    contextFiles?}. The engine mints the owner's planning card (confer with
    seniors, create teams, break into sprints) on the next pass."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    spec, err = _validate_program(body)
    if err:
        return JSONResponse({'error': err}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    programs = state.setdefault('programs', [])
    if not isinstance(programs, list):
        state['programs'] = programs = []
    seq = int(state.get('programSeq') or 0) + 1
    state['programSeq'] = seq
    program = dict(spec)
    program.update({
        'id': f'prog-{seq}', 'status': 'planning', 'createdAtMs': int(time.time() * 1000),
        'planningCardMinted': False, 'crRequested': False,
        'pieceStatus': {},
    })
    programs.append(program)
    save_state_to_db(state)
    log_action('player', 'program_created',
               {'id': program['id'], 'title': program['title'][:120],
                'owner': program['ownerAgentId'], 'pieces': len(program['pieces'])},
               authorized=True)
    return JSONResponse({'ok': True, 'program': program})


@app.get('/api/programs')
async def list_programs(request: Request):
    """Operator-gated read: every program with its derived roll-up."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db() or {}
    out = []
    for p in (state.get('programs') or []):
        if not isinstance(p, dict):
            continue
        rollup = _program_rollup(p, state)
        out.append({'id': p.get('id'), 'title': p.get('title'),
                    'status': p.get('status'), 'owner': p.get('ownerAgentId'),
                    'rollup': rollup})
    return JSONResponse({'ok': True, 'programs': out})


@app.delete('/api/programs/{program_id}')
@_state_writer
async def delete_program(program_id: str, request: Request):
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    programs = state.get('programs') or []
    kept = [p for p in programs
            if not (isinstance(p, dict) and p.get('id') == program_id)]
    if len(kept) == len(programs):
        return JSONResponse({'error': 'unknown program'}, status_code=404)
    state['programs'] = kept
    save_state_to_db(state)
    log_action('player', 'program_deleted', {'id': program_id}, authorized=True)
    return JSONResponse({'ok': True})


def _program_rollup(program, state):
    """Derived program status: match each piece spec to a live/queued card
    by title and report where it stands. Read-only; the engine persists the
    same numbers onto the program each pass."""
    tasks = state.get('tasks') or {}
    queue = state.get('workQueue') or []
    by_title = {}
    for t in tasks.values():
        if isinstance(t, dict) and t.get('title'):
            by_title.setdefault(t['title'], t)
    out = []
    for piece in program.get('pieces') or []:
        title = piece.get('title')
        t = by_title.get(title)
        if t is None:
            out.append({'title': title, 'state': 'unminted'})
        else:
            st = t.get('status')
            out.append({'title': title, 'taskId': t.get('id'), 'state': st,
                        'assignedTo': t.get('assignedTo')})
        if t is None and any(q.get('title') == title for q in queue):
            out[-1]['state'] = 'queued'
    return out
