"""TRUE MOBILITY: agents visiting other villages (2026-11-06).

The operator's model: agents can WALK BETWEEN VILLAGES -- go somewhere to
gather knowledge or talk with the people there, then come home. A visit is
a temporary rehome built on the team-move surgery, with one iron rule:

  an agent with an open task cannot visit -- finish or hand off first.

Why the surgery, not an in-process move: a village process's queued saves
race its sim's tick saves (last writer wins), and the first real move
proved an in-process write can be silently reverted. So BOTH directions of
a visit stop the destination process, edit its DB while nothing is
running, and start it again:

  POST /api/admin/send-visitor  {agentId, villageId, hours, reason}
      parent collects the agent's footprint (roster, live blob, keys,
      LTM) -> stops the village -> writes her in (stamping
      _visitFrom/_visitReturnAt and the visit mail on her blob) ->
      restarts the village -> removes her from the parent -> records the
      visit.

  POST /api/villages/{id}/return-visitor  {agentId, force?}
      the village's heartbeat fires this when a visiting agent's
      _visitReturnAt comes due (or the operator calls it early). The
      parent verifies the visit record, stops the village, extracts her
      footprint + un-stamps her blob, restarts the village, re-homes her
      in the parent, closes the visit record, mails her and theo.

The visitor appears in the destination's cross-village directory
automatically (the heartbeat lists whoever's roster is local), so mail
addressed to her routes to the village she is VISITING, and her home
simply does not see her until she is back.
"""
import json
import os
import plistlib
import subprocess
import time

from fastapi import Request
from fastapi.responses import JSONResponse

import psycopg2


def _visit_token_ok(request: Request) -> bool:
    expected = (os.environ.get('THINK_TANK_PARENT_TOKEN')
                or _load_env().get('THINK_TANK_PARENT_TOKEN', ''))
    if not expected:
        return False
    return (request.headers.get('X-Parent-Token') or '').strip() == expected


def _ensure_visits_table():
    with _db() as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS villages_visits ('
            ' agent_id TEXT PRIMARY KEY, from_village TEXT NOT NULL,'
            ' to_village TEXT NOT NULL, started_at REAL NOT NULL,'
            ' return_at REAL NOT NULL, returned_at REAL, reason TEXT)')


def _ensure_registry_columns():
    with _db() as conn:
        for col in ('launchd_label', 'db_url'):
            try:
                conn.execute(f'ALTER TABLE villages_registry'
                             f' ADD COLUMN {col} TEXT')
            except Exception:
                pass  # column already exists


def _village_transport(village_id):
    """(launchd_label, db_url) for a registered village; None when the
    village was registered before the mobility build (re-run
    tools/village_plist.py --install to attach them)."""
    _ensure_registry_columns()
    with _db() as conn:
        row = conn.execute(
            'SELECT launchd_label, db_url FROM villages_registry'
            ' WHERE id = ?', (village_id,)).fetchone()
    if row and row[0] and row[1]:
        return row[0], row[1]
    return None


def _village_port(label):
    path = os.path.join(os.path.expanduser('~'), 'Library',
                        'LaunchAgents', label + '.plist')
    try:
        with open(path, 'rb') as fh:
            plist = plistlib.load(fh)
        return int(plist['ProgramArguments'][2])
    except Exception:
        return None


def _bootout(label):
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'],
                   capture_output=True)


def _bootstrap(label):
    path = os.path.join(os.path.expanduser('~'), 'Library', 'LaunchAgents',
                        label + '.plist')
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', path],
                   capture_output=True)


def _wait_port(port, want_open, seconds):
    import socket
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1.5):
                if want_open:
                    return True
        except OSError:
            if not want_open:
                return True
        time.sleep(1)
    return False


def _village_extract_agent(dsn, agent_id):
    """Her full footprint from the village DB, with her live blob."""
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    out = {}
    cur.execute('SELECT seq, blob, updated_at FROM roster_agent'
                ' WHERE id = %s', (agent_id,))
    out['roster'] = cur.fetchone()
    cur.execute('SELECT seq, blob, updated_at FROM live_agent'
                ' WHERE id = %s', (agent_id,))
    out['live'] = cur.fetchone()
    cur.execute('SELECT secret_key, created_at FROM agent_keys'
                ' WHERE agent_id = %s', (agent_id,))
    out['keys'] = cur.fetchall()
    try:
        cur.execute('SELECT agent_id, mem_type, content, created_at,'
                    ' active, confidence, source_type FROM agent_ltm'
                    ' WHERE agent_id = %s', (agent_id,))
        out['ltm'] = cur.fetchall()
    except Exception:
        out['ltm'] = []
    cur.close()
    conn.close()
    return out


def _village_remove_agent(dsn, agent_id):
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    for sql in ('DELETE FROM roster_agent WHERE id = %s',
                'DELETE FROM live_agent WHERE id = %s',
                'DELETE FROM agent_keys WHERE agent_id = %s',
                'DELETE FROM agent_ltm WHERE agent_id = %s'):
        try:
            cur.execute(sql, (agent_id,))
        except Exception:
            pass
    conn.commit()
    cur.close()
    conn.close()


def _village_insert_agent(dsn, agent_id, rows, live_blob):
    """Write a visiting agent into a STOPPED village's DB. `live_blob` is
    her full live blob as she should exist there (visit stamps + the
    visit mail already inside)."""
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute('INSERT INTO roster_agent (id, seq, blob, updated_at)'
                ' VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET'
                ' blob = EXCLUDED.blob, seq = EXCLUDED.seq,'
                ' updated_at = EXCLUDED.updated_at',
                (agent_id, rows['roster'][0], rows['roster'][1],
                 rows['roster'][2]))
    cur.execute('INSERT INTO live_agent (id, seq, blob, updated_at)'
                ' VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET'
                ' blob = EXCLUDED.blob, seq = EXCLUDED.seq,'
                ' updated_at = EXCLUDED.updated_at',
                (agent_id, rows['live'][0], json.dumps(live_blob),
                 time.time()))
    for key, created in rows.get('keys') or []:
        cur.execute('INSERT INTO agent_keys (agent_id, secret_key,'
                    ' created_at) VALUES (%s, %s, %s) ON CONFLICT'
                    ' (agent_id) DO UPDATE SET secret_key ='
                    ' EXCLUDED.secret_key', (agent_id, key, created))
    cur.execute('CREATE TABLE IF NOT EXISTS agent_ltm (id SERIAL PRIMARY'
                ' KEY, agent_id TEXT NOT NULL, mem_type TEXT, content TEXT,'
                ' created_at DOUBLE PRECISION, active INTEGER DEFAULT 1,'
                ' confidence DOUBLE PRECISION, source_type TEXT)')
    for r in rows.get('ltm') or []:
        cur.execute('INSERT INTO agent_ltm (agent_id, mem_type, content,'
                    ' created_at, active, confidence, source_type)'
                    ' VALUES (%s, %s, %s, %s, %s, %s, %s)', r)
    conn.commit()
    cur.close()
    conn.close()


def _has_open_task(state, agent_id):
    """Terminal means closed in the org's vocabulary AND in the archive:
    the merged read includes 12k archived rows whose statuses (e.g.
    'failed') are historical, so an archive-table membership check keeps
    an old failure from blocking a visit forever."""
    for t in (state.get('tasks') or {}).values():
        if not isinstance(t, dict) or t.get('assignedTo') != agent_id:
            continue
        if t.get('status') in ('done', 'cancelled', 'abandoned', 'failed'):
            continue
        with _db() as conn:
            row = conn.execute(
                'SELECT 1 FROM task_archive WHERE id = ?',
                (t.get('id'),)).fetchone()
        if row is None:
            return True
    return False


@app.post('/api/admin/send-visitor')
async def admin_send_visitor(request: Request):
    from serve import verify_session, SESSION_COOKIE_NAME, log_action, \
        save_state_to_db
    key = request.headers.get('X-Agent-Key')
    actor = _is_tank_admin(request) if key else None
    if not actor and not _visit_token_ok(request) and \
            not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    agent_id = str((body or {}).get('agentId') or '').strip()
    village_id = str((body or {}).get('villageId') or '').strip()
    try:
        hours = float((body or {}).get('hours') or 8)
    except (TypeError, ValueError):
        hours = 8.0
    hours = max(0.01, min(hours, 24 * 14))
    reason = str((body or {}).get('reason') or '').strip()[:300]
    if not agent_id or not village_id:
        return JSONResponse({'error': 'agentId and villageId required'},
                            status_code=400)
    transport = _village_transport(village_id)
    if transport is None:
        return JSONResponse(
            {'error': f'village {village_id} has no launchd label or DB '
                      'url registered; run tools/village_plist.py '
                      '--install again'}, status_code=409)
    label, dsn = transport
    _ensure_visits_table()
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        roster_ids = {r.get('id') for r in (state.get('agentRoster') or [])
                      if isinstance(r, dict)}
        if agent_id not in roster_ids:
            return JSONResponse(
                {'error': f'no agent {agent_id} is homed here'},
                status_code=404)
        if _has_open_task(state, agent_id):
            return JSONResponse(
                {'error': f'{agent_id} still holds an open task -- '
                          'finish or hand it off before visiting'},
                status_code=409)
        with _db() as conn:
            roster = conn.execute(
                'SELECT seq, blob, updated_at FROM roster_agent'
                ' WHERE id = ?', (agent_id,)).fetchone()
            live = conn.execute(
                'SELECT seq, blob, updated_at FROM live_agent'
                ' WHERE id = ?', (agent_id,)).fetchone()
            keys = conn.execute(
                'SELECT secret_key, created_at FROM agent_keys'
                ' WHERE agent_id = ?', (agent_id,)).fetchall()
            try:
                ltm = conn.execute(
                    'SELECT agent_id, mem_type, content, created_at,'
                    ' active, confidence, source_type FROM agent_ltm'
                    ' WHERE agent_id = ?', (agent_id,)).fetchall()
            except Exception:
                ltm = []
        rows = {'roster': roster, 'live': live, 'keys': keys, 'ltm': ltm}
        if not roster or not live:
            return JSONResponse(
                {'error': f'{agent_id} has no roster/live footprint'},
                status_code=409)
        now = time.time()
        return_at = now + hours * 3600
        # Her blob AS SHE SHOULD EXIST THERE: visit stamps + the visit
        # mail riding along (the parent copy is about to be removed, so
        # anything she should read on arrival goes in NOW).
        live_blob = json.loads(live[1])
        live_blob.update({
            '_visitFrom': 'main',
            '_visitReturnAt': int(return_at * 1000),
            '_visitReason': reason,
        })
        box = live_blob.setdefault('mailbox', [])
        box.append({
            'kind': 'visit', 'read': False,
            'ts': int(now * 1000),
            'text': f'You are VISITING village {village_id} for about '
                    f'{hours:g}h (return ~'
                    f'{time.strftime("%H:%M", time.localtime(return_at))}).'
                    ' Your work stayed home: you hold no tasks while'
                    ' visiting. Gather knowledge, talk with the people'
                    ' there; the village walks you home automatically.',
        })
        del box[:-200]
        port = _village_port(label)
        _bootout(label)
        if port and not _wait_port(port, False, 30):
            _bootstrap(label)
            return JSONResponse(
                {'error': f'village {village_id} would not stop; visit '
                          'aborted, nothing moved'}, status_code=502)
        try:
            _village_insert_agent(dsn, agent_id, rows, live_blob)
        except Exception as exc:
            _bootstrap(label)
            return JSONResponse(
                {'error': f'village insert failed: '
                          f'{type(exc).__name__}'[:160]}, status_code=502)
        _bootstrap(label)
        if port and not _wait_port(port, True, 90):
            return JSONResponse(
                {'error': f'village did not come back on port {port}; '
                          f'{agent_id} is IN the village DB -- check '
                          'launchd before retrying'}, status_code=502)
        # Remove her from the parent + record the visit.
        with _db() as conn:
            for table in ('roster_agent', 'live_agent'):
                conn.execute(f'DELETE FROM {table} WHERE id = ?',
                             (agent_id,))
            conn.execute('DELETE FROM agent_keys WHERE agent_id = ?',
                         (agent_id,))
            try:
                conn.execute('DELETE FROM agent_ltm WHERE agent_id = ?',
                             (agent_id,))
            except Exception:
                pass
            conn.execute(
                'INSERT INTO villages_visits (agent_id, from_village,'
                ' to_village, started_at, return_at, reason)'
                ' VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (agent_id) DO'
                ' UPDATE SET to_village = EXCLUDED.to_village,'
                ' started_at = EXCLUDED.started_at,'
                ' return_at = EXCLUDED.return_at, returned_at = NULL,'
                ' reason = EXCLUDED.reason',
                (agent_id, 'main', village_id, now, return_at, reason))
        (state.get('agents') or {}).pop(agent_id, None)
        save_state_to_db(state)
        try:
            from sim import _log_governance
            _log_governance(state, actor or 'admin', 'visit_started',
                            {'agent': agent_id, 'village': village_id,
                             'returnAt': int(return_at * 1000),
                             'reason': reason})
        except Exception:
            pass
        log_action(actor or 'operator-tool', 'visitor_sent',
                   {'agent': agent_id, 'village': village_id,
                    'hours': hours, 'reason': reason}, authorized=True)
        return {'ok': True, 'agent': agent_id, 'village': village_id,
                'returnAt': int(return_at * 1000)}
    finally:
        _state_abort()


@app.post('/api/villages/{village_id}/return-visitor')
async def village_return_visitor(village_id: str, request: Request):
    """Bring a visiting agent HOME. Fired by the village's heartbeat when
    her _visitReturnAt comes due, or called directly for an early return
    (force)."""
    if not _visit_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    agent_id = str((body or {}).get('agentId') or '').strip()
    force = bool((body or {}).get('force'))
    if not agent_id:
        return JSONResponse({'error': 'agentId required'}, status_code=400)
    _ensure_visits_table()
    with _db() as conn:
        visit = conn.execute(
            'SELECT from_village, return_at, returned_at FROM'
            ' villages_visits WHERE agent_id = ?', (agent_id,)).fetchone()
    if visit is None or visit[2]:
        return JSONResponse(
            {'error': f'{agent_id} has no open visit'}, status_code=409)
    if not force and time.time() < visit[1]:
        return JSONResponse(
            {'error': f"{agent_id}'s visit is not due yet"},
            status_code=409)
    transport = _village_transport(village_id)
    if transport is None:
        return JSONResponse({'error': 'village not transportable'},
                            status_code=409)
    label, dsn = transport
    rows = _village_extract_agent(dsn, agent_id)
    if not rows.get('roster') or not rows.get('live'):
        return JSONResponse(
            {'error': f"{agent_id} is not in {village_id}'s DB"},
            status_code=404)
    # Un-stamp before she goes home.
    live_blob = json.loads(rows['live'][1])
    for k in ('_visitFrom', '_visitReturnAt', '_visitReason'):
        live_blob.pop(k, None)
    port = _village_port(label)
    _bootout(label)
    if port and not _wait_port(port, False, 30):
        _bootstrap(label)
        return JSONResponse({'error': 'village would not stop'},
                            status_code=502)
    try:
        _village_remove_agent(dsn, agent_id)
    finally:
        _bootstrap(label)
        if port:
            _wait_port(port, True, 90)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        with _db() as conn:
            if rows.get('roster'):
                conn.execute(
                    'INSERT INTO roster_agent (id, seq, blob, updated_at)'
                    ' VALUES (?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET'
                    ' blob = EXCLUDED.blob, updated_at ='
                    ' EXCLUDED.updated_at',
                    (agent_id, rows['roster'][0], rows['roster'][1],
                     rows['roster'][2]))
            if rows.get('live'):
                conn.execute(
                    'INSERT INTO live_agent (id, seq, blob, updated_at)'
                    ' VALUES (?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET'
                    ' blob = EXCLUDED.blob, updated_at ='
                    ' EXCLUDED.updated_at',
                    (agent_id, rows['live'][0], json.dumps(live_blob),
                     time.time()))
            for key, created in rows.get('keys') or []:
                conn.execute(
                    'INSERT INTO agent_keys (agent_id, secret_key,'
                    ' created_at) VALUES (?, ?, ?) ON CONFLICT (agent_id)'
                    ' DO UPDATE SET secret_key = EXCLUDED.secret_key',
                    (agent_id, key, created))
            conn.execute(
                'UPDATE villages_visits SET returned_at = ?'
                ' WHERE agent_id = ?', (time.time(), agent_id))
        agent = (state.get('agents') or {}).get(agent_id)
        if isinstance(agent, dict):
            box = agent.setdefault('mailbox', [])
            box.append({
                'kind': 'visit', 'read': False,
                'ts': int(time.time() * 1000),
                'text': f'You are HOME from village {village_id}. Your'
                        ' footprint is back where it belongs; your team'
                        ' and tasks are as you left them.',
            })
            del box[:-200]
        theo = (state.get('agents') or {}).get('theo')
        if isinstance(theo, dict):
            box = theo.setdefault('mailbox', [])
            box.append({'kind': 'visit_returned', 'read': False,
                        'ts': int(time.time() * 1000),
                        'text': f'{agent_id} returned from village '
                                f'{village_id}.'})
            del box[:-200]
        from serve import save_state_to_db, log_action
        save_state_to_db(state)
        log_action('village-heartbeat' if not force else 'operator',
                   'visitor_returned',
                   {'agent': agent_id, 'village': village_id},
                   authorized=True)
        return {'ok': True, 'agent': agent_id, 'home': 'main'}
    finally:
        _state_abort()
