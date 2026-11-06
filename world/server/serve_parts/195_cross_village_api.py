"""The rich cross-village API (2026-11-06).

The operator's architecture: directors may span villages -- a director's
agent row (her home) lives in ONE database while her teams live wherever
they are needed; three or more teams sit under subdirectors, who span
the same way. The parent control plane is the POST OFFICE between
village DBs, so no village ever talks to another village directly:

  POST /api/villages/{id}/teams        -- register team placements
                                          {teamId -> directorId} so the
                                          parent knows where work lives.
  POST /api/villages/{id}/escalate     -- a village's worker/director
                                          asks the ORG for something:
                                          routed into the chief of
                                          staff's mailbox with village
                                          attribution.
  POST /api/villages/{id}/relay-brief  -- a supervision brief generated
                                          in the village for a director
                                          whose home is the parent.
  POST /api/villages/{id}/relay-card   -- an attention card queued in the
                                          village for a director homed in
                                          the parent: re-queued into the
                                          parent's workQueue.
  GET  /api/villages/{id}/inbox        -- the parent -> village outbox
                                          (chief-of-staff replies,
                                          directives). The village's
                                          heartbeat loop polls this and
                                          delivers into local mailboxes;
                                          delivery is at-most-once (the
                                          rows are consumed on read).

Auth: every village-scoped call takes the shared THINK_TANK_PARENT_TOKEN
(X-Parent-Token). Rate caps per endpoint keep a chatty village from
flooding the chief of staff.
"""
import json
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse

_PARENT_TOKEN_ENV = 'THINK_TANK_PARENT_TOKEN'
_ESCALATE_CAP_PER_CALL = 10
_RELAY_CARD_CAP_PER_CALL = 5


def _parent_token_ok(request: Request) -> bool:
    expected = (os.environ.get(_PARENT_TOKEN_ENV)
                or _load_env().get(_PARENT_TOKEN_ENV, ''))
    if not expected:
        return False
    supplied = (request.headers.get('X-Parent-Token') or '').strip()
    return supplied == expected


def _village_known(village_id):
    with _db() as conn:
        return conn.execute(
            'SELECT 1 FROM villages_registry WHERE id = ?',
            (village_id,)).fetchone() is not None


def _theo_mailbox_append(state, item):
    theo = (state.get('agents') or {}).get('theo')
    if not isinstance(theo, dict):
        return False
    box = theo.setdefault('mailbox', [])
    box.append(item)
    del box[:-200]
    return True


def _village_teams_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS villages_teams ('
        ' village_id TEXT NOT NULL, team_id TEXT NOT NULL,'
        ' director_id TEXT, updated_at REAL,'
        ' PRIMARY KEY (village_id, team_id))')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS villages_outbox ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' village_id TEXT NOT NULL, to_agent TEXT NOT NULL,'
        ' kind TEXT, text TEXT NOT NULL, ts REAL NOT NULL)')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_villages_outbox_village'
        ' ON villages_outbox (village_id)')


def _register_teams_tables():
    # Lazy for the same reason as 194's registry table: the test harness
    # repoints DB_PATH after the parts are exec'd.
    with _db() as conn:
        _village_teams_table(conn)


@app.post('/api/villages/{village_id}/teams')
async def village_register_teams(village_id: str, request: Request):
    if not _parent_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    if not _village_known(village_id):
        return JSONResponse({'error': f'unknown village {village_id}'},
                            status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    teams = (body or {}).get('teams') if isinstance(body, dict) else None
    if not isinstance(teams, list) or not teams:
        return JSONResponse({'error': 'teams must be a non-empty list'},
                            status_code=400)
    _register_teams_tables()
    now = time.time()
    kept = []
    with _db() as conn:
        for t in teams[:50]:
            tid = str((t or {}).get('teamId') or '').strip()
            did = str((t or {}).get('directorId') or '').strip() or None
            if not tid:
                continue
            conn.execute(
                'INSERT INTO villages_teams (village_id, team_id,'
                ' director_id, updated_at) VALUES (?, ?, ?, ?)'
                ' ON CONFLICT(village_id, team_id) DO UPDATE SET'
                ' director_id = excluded.director_id,'
                ' updated_at = excluded.updated_at',
                (village_id, tid, did, now))
            kept.append(tid)
    return {'ok': True, 'registered': kept}


@app.post('/api/villages/{village_id}/escalate')
async def village_escalate(village_id: str, request: Request):
    """A village asks the ORG for something. Lands in the chief of
    staff's mailbox with village attribution; nothing is auto-acted on."""
    if not _parent_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    if not _village_known(village_id):
        return JSONResponse({'error': f'unknown village {village_id}'},
                            status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    items = (body or {}).get('items') if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        return JSONResponse({'error': 'items must be a non-empty list'},
                            status_code=400)
    from serve import save_state_to_db
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    delivered = 0
    for item in items[:_ESCALATE_CAP_PER_CALL]:
        text = str((item or {}).get('text') or '').strip()
        if not text:
            continue
        frm = str((item or {}).get('from') or 'village').strip()
        kind = str((item or {}).get('kind') or 'escalation').strip()
        task_ref = str((item or {}).get('taskId') or '').strip()
        suffix = f' (task {task_ref})' if task_ref else ''
        ok = _theo_mailbox_append(state, {
            'kind': 'village_escalation',
            'about': village_id,
            'read': False,
            'ts': int(time.time() * 1000),
            'text': f'[village {village_id}] {frm} ({kind}){suffix}: {text}',
        })
        delivered += 1 if ok else 0
    if delivered:
        save_state_to_db(state)
    else:
        _state_abort()
    return {'ok': True, 'delivered': delivered}


@app.post('/api/villages/{village_id}/relay-brief')
async def village_relay_brief(village_id: str, request: Request):
    """A supervision brief for a director homed in the PARENT: straight
    into her mailbox. A director homed in ANOTHER village is 404 -- the
    caller (the village sim) retries on its next brief pass; multi-hop
    relay waits until a chain actually exists."""
    if not _parent_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    if not _village_known(village_id):
        return JSONResponse({'error': f'unknown village {village_id}'},
                            status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    did = str((body or {}).get('directorId') or '').strip()
    text = str((body or {}).get('text') or '').strip()
    team_name = str((body or {}).get('teamName') or '').strip()
    if not did or not text:
        return JSONResponse({'error': 'directorId and text are required'},
                            status_code=400)
    from serve import save_state_to_db
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    agent = (state.get('agents') or {}).get(did)
    if not isinstance(agent, dict):
        _state_abort()
        return JSONResponse(
            {'error': f'director {did} is not homed in the parent'},
            status_code=404)
    box = agent.setdefault('mailbox', [])
    box.append({
        'kind': 'supervisor_brief',
        'about': did,
        'title': f'Supervisor brief (village {village_id}): {team_name}',
        'text': text,
        'read': False,
        'createdAt': int(time.time() * 1000),
    })
    del box[:-200]
    save_state_to_db(state)
    return {'ok': True, 'relayed': did}


@app.post('/api/villages/{village_id}/relay-card')
async def village_relay_card(village_id: str, request: Request):
    """An attention card the village queued for a director homed in the
    parent: re-queued into the PARENT's workQueue so the parent's own
    machinery (wake, assignment, budget) drives it."""
    if not _parent_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    if not _village_known(village_id):
        return JSONResponse({'error': f'unknown village {village_id}'},
                            status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    cards = (body or {}).get('cards') if isinstance(body, dict) else None
    if not isinstance(cards, list) or not cards:
        return JSONResponse({'error': 'cards must be a non-empty list'},
                            status_code=400)
    from serve import save_state_to_db
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    queue = state.get('workQueue') if isinstance(state.get('workQueue'),
                                                 list) else None
    if queue is None:
        queue = []
        state['workQueue'] = queue
    queued = 0
    for card in cards[:_RELAY_CARD_CAP_PER_CALL]:
        if not isinstance(card, dict):
            continue
        card = dict(card)
        card['villageId'] = village_id
        card.setdefault('priority', 'high')
        queue.append(card)
        queued += 1
    if queued:
        save_state_to_db(state)
    else:
        _state_abort()
    return {'ok': True, 'queued': queued}


@app.get('/api/villages/{village_id}/inbox')
async def village_inbox(village_id: str, request: Request):
    """Consume the parent -> village outbox (at-most-once: rows are
    returned and deleted in one transaction)."""
    if not _parent_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    if not _village_known(village_id):
        return JSONResponse({'error': f'unknown village {village_id}'},
                            status_code=404)
    _register_teams_tables()
    with _db() as conn:
        rows = conn.execute(
            'SELECT id, to_agent, kind, text, ts FROM villages_outbox'
            ' WHERE village_id = ? ORDER BY id LIMIT 50',
            (village_id,)).fetchall()
        if rows:
            conn.execute(
                'DELETE FROM villages_outbox WHERE village_id = ? AND id <= ?',
                (village_id, rows[-1][0]))
    return {'items': [{'id': r[0], 'to': r[1], 'kind': r[2], 'text': r[3],
                       'ts': r[4]} for r in rows]}


def _village_deliver_to_org(village_id, items):
    """Parent-side helper the migration/ops tools use: push a directive
    INTO a village's outbox so its heartbeat loop delivers it."""
    _register_teams_tables()
    now = time.time()
    with _db() as conn:
        for item in (items or [])[:20]:
            to = str((item or {}).get('to') or '').strip()
            text = str((item or {}).get('text') or '').strip()
            if not to or not text:
                continue
            conn.execute(
                'INSERT INTO villages_outbox (village_id, to_agent, kind,'
                ' text, ts) VALUES (?, ?, ?, ?, ?)',
                (village_id, to, str((item or {}).get('kind') or 'directive'),
                 text, now))
