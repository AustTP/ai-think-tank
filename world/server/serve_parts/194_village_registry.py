"""The parent control plane's village registry (2026-11-06).

One row per village PROCESS the parent knows about: id, name, API base,
and the heartbeat bookkeeping that turns "the village process is alive"
into an observable fact instead of an assumption.

  * POST /api/villages/registry   -- register a village (the village_new
    tool calls it right after creating the village's DB).
  * POST /api/villages/{id}/heartbeat -- the village worker's minute
    pulse; carries counts and alert lines, which land in the chief of
    staff's mailbox (org intake -- the one channel a village needs).
  * GET  /api/villages/registry   -- operator view: every village, its
    last heartbeat, and a staleness verdict (>5 min = STALE).

Auth: heartbeat takes the shared THINK_TANK_PARENT_TOKEN (the village
holds the same secret in its own .env); register and the registry view
take the player session like the rest of the operator surface.
"""
import json
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse

_PARENT_TOKEN_ENV = 'THINK_TANK_PARENT_TOKEN'
_REGISTRY_STALE_S = 5 * 60


def _ensure_village_registry_table():
    # Called lazily by every handler: at parts-exec/import time the test
    # harness has not pointed DB_PATH at its temp database yet, so an
    # import-time DDL would create the table in the WRONG database.
    with _db() as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS villages_registry ('
            ' id TEXT PRIMARY KEY, name TEXT, api_base TEXT,'
            ' created_at REAL, last_heartbeat_at REAL, last_status TEXT)')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_villages_registry_hb'
            ' ON villages_registry (last_heartbeat_at)')


def _heartbeat_token_ok(request: Request) -> bool:
    # os.environ first, then the repo .env (the codebase convention: a key
    # added to .env after boot still counts without a restart).
    expected = (os.environ.get(_PARENT_TOKEN_ENV)
                or _load_env().get(_PARENT_TOKEN_ENV, ''))
    if not expected:
        return False  # no token configured: no heartbeat trust at all
    supplied = (request.headers.get('X-Parent-Token') or '').strip()
    return supplied == expected


def _village_row_exists(conn, vid):
    return conn.execute(
        'SELECT 1 FROM villages_registry WHERE id = ?', (vid,)).fetchone()


@app.post('/api/villages/registry')
async def register_village(request: Request):
    from serve import verify_session, SESSION_COOKIE_NAME, log_action
    authed = verify_session(request.cookies.get(SESSION_COOKIE_NAME))
    # The village_new tool is operator tooling holding the same shared
    # secret the village heartbeat uses -- it registers without a browser
    # session.
    if not authed and not _heartbeat_token_ok(request):
        return JSONResponse({'error': 'player session required'},
                            status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    _ensure_village_registry_table()
    vid = str((body or {}).get('id') or '').strip()
    name = str((body or {}).get('name') or '').strip() or vid
    api_base = str((body or {}).get('apiBase') or '').strip().rstrip('/')
    if not vid or not api_base:
        return JSONResponse(
            {'error': 'id and apiBase are required'}, status_code=400)
    launchd_label = str((body or {}).get('launchdLabel') or '').strip()
    db_url = str((body or {}).get('dbUrl') or '').strip()
    _ensure_registry_columns()
    with _db() as conn:
        conn.execute(
            'INSERT INTO villages_registry (id, name, api_base,'
            ' created_at, launchd_label, db_url, last_heartbeat_at,'
            ' last_status) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)'
            ' ON CONFLICT(id) DO UPDATE SET name = excluded.name,'
            ' api_base = excluded.api_base,'
            ' launchd_label = COALESCE(NULLIF(excluded.launchd_label, \'\'),'
            ' villages_registry.launchd_label),'
            ' db_url = COALESCE(NULLIF(excluded.db_url, \'\'),'
            ' villages_registry.db_url)',
            (vid, name, api_base, time.time(), launchd_label, db_url))
    log_action('player', 'village_registered',
               {'id': vid, 'name': name, 'apiBase': api_base},
               authorized=True)
    return {'ok': True, 'id': vid, 'name': name, 'apiBase': api_base}


@app.post('/api/villages/{village_id}/heartbeat')
async def village_heartbeat(village_id: str, request: Request):
    if not _heartbeat_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not body.get('ok'):
        # Keep the village's own error line: a failing beat must be
        # diagnosable from the parent's registry view alone.
        status = {'ok': False, 'seenAt': time.time(),
                  'error': str(body.get('error') or '')[:200] if
                  isinstance(body, dict) else ''}
    else:
        status = {'ok': True, 'seenAt': time.time(),
                  'openTasks': body.get('openTasks'),
                  'queued': body.get('queued'),
                  'alerts24h': body.get('alerts24h')}
    _ensure_village_registry_table()
    with _db() as conn:
        if not _village_row_exists(conn, village_id):
            return JSONResponse(
                {'error': f'unknown village {village_id}'}, status_code=404)
        conn.execute(
            'UPDATE villages_registry SET last_heartbeat_at = ?,'
            ' last_status = ? WHERE id = ?',
            (time.time(), json.dumps(status), village_id))
        # The cross-village directory: who lives in this village right now.
        conn.execute(
            'CREATE TABLE IF NOT EXISTS villages_agents ('
            ' village_id TEXT NOT NULL, agent_id TEXT NOT NULL,'
            ' name TEXT, role TEXT, room TEXT, updated_at REAL,'
            ' PRIMARY KEY (village_id, agent_id))')
        agents = body.get('agents') if isinstance(body, dict) else None
        if isinstance(agents, list):
            conn.execute('DELETE FROM villages_agents WHERE village_id = ?',
                         (village_id,))
            for a in agents[:60]:
                if not isinstance(a, dict) or not a.get('id'):
                    continue
                conn.execute(
                    'INSERT INTO villages_agents (village_id, agent_id,'
                    ' name, role, room, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                    (village_id, str(a.get('id')), str(a.get('name') or ''),
                     str(a.get('role') or ''), str(a.get('room') or ''),
                     time.time()))
    # Org intake: alert lines ride the heartbeat into the chief of staff's
    # mailbox -- the channel that keeps a village process from being an
    # island when it needs org-level help.
    alerts = body.get('alerts') or []
    if isinstance(alerts, list) and alerts:
        try:
            state = _state_begin()
            if state:
                theo = (state.get('agents') or {}).get('theo')
                if isinstance(theo, dict):
                    box = theo.setdefault('mailbox', [])
                    for line in alerts[:3]:
                        box.append({
                            'text': f"Village {village_id} reports: {line}",
                            'read': False,
                            'ts': int(time.time() * 1000),
                        })
                    del box[:-200]
                    save_state_to_db(state)
        except Exception:
            pass  # intake is best-effort; the heartbeat itself already landed
    return {'ok': True}


@app.get('/api/villages/registry')
async def village_registry_view(request: Request):
    from serve import verify_session, SESSION_COOKIE_NAME
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'player session required'},
                            status_code=401)
    now = time.time()
    rows = []
    _ensure_village_registry_table()
    with _db() as conn:
        for r in conn.execute(
                'SELECT id, name, api_base, created_at, last_heartbeat_at,'
                ' last_status FROM villages_registry ORDER BY id'):
            hb = r[4]
            rows.append({
                'id': r[0], 'name': r[1], 'apiBase': r[2],
                'createdAt': r[3],
                'lastHeartbeatAt': hb,
                'secondsSinceHeartbeat': (now - hb) if hb else None,
                'liveness': ('alive' if hb and now - hb < _REGISTRY_STALE_S
                             else 'STALE'),
                'status': json.loads(r[5]) if r[5] else None,
            })
    return {'villages': rows, 'staleAfterS': _REGISTRY_STALE_S}


def _village_heartbeat_test_hooks():
    return None
