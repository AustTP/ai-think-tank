"""Director card-level failure scan (2026-11-05).

The two-layer issue hunt, per the owner's directive:

  DIRECTORS scan THEIR WORKERS' CARDS: failures, rejections, refusals,
  stalls and repeated retries inside the span they manage. Their surface
  is GET /api/ops/cards?hours=24 -- one call, scoped to the director's
  own span.

  THE REGIONAL VP scans the VILLAGE: systemic and machinery failure
  (classifier outages, provider incidents, blocked-command patterns,
  budget, escalations) via GET /api/ops/failures, plus every director's
  span on request. The layers must not duplicate each other: a director
  who finds a card-level pattern routes a fix to the assigned worker; the
  regional VP who finds a village-level pattern routes to directors or
  files work. (constitution rule 9.)

GET /api/ops/cards?hours=24&directorId=me   (agent key)
  - directors get their own span (directorId must match the caller);
  - the chief of staff, admin, and the player may pass ?scope=tank for
    every span, or a specific director's span by id.

Output per worker: open cards, cards that failed/refused/were abandoned
in the window, blocked commands, failed actions -- the material a
director needs to decide "route a fix" vs "coach the worker".
"""
import time

_CARD_BAD_STATUSES = ('failed', 'refused', 'abandoned', 'auto-closed',
                      'stalled', 'error')


def _roster_span(director_id):
    """The workers reporting to a director, from the roster table. Unknown
    directors yield an empty span (no error -- a new director has none)."""
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT blob FROM roster_agent').fetchall()
    except Exception:
        return []
    workers = []
    for (blob,) in rows:
        try:
            r = json.loads(blob)
        except Exception:
            continue
        if str(r.get('director') or '').lower() == str(director_id or '').lower():
            workers.append(r.get('id'))
    return [w for w in workers if w]


def _card_failure_category(task):
    """The failure signal for one card, or None when it looks healthy."""
    status = str(task.get('status') or '').lower()
    if status in _CARD_BAD_STATUSES:
        return status
    for field in ('failureCategory', 'failCategory'):
        val = task.get(field)
        if val:
            return str(val)
    delivery = str(task.get('deliveryState') or '').lower()
    if delivery in _CARD_BAD_STATUSES:
        return delivery
    return None


def _ops_cards(hours, worker_ids):
    """Per-worker card + command failure roll-up over the window."""
    cutoff = time.time() - hours * 3600
    workers = set(worker_ids)
    state = get_state_from_db() or {}
    live_tasks = state.get('tasks') or {}
    per_worker = {w: {'failedCards': [], 'openCards': 0,
                      'blockedCommands': 0, 'failedActions': 0} for w in workers}
    for tid, task in live_tasks.items():
        if not isinstance(task, dict):
            continue
        who = task.get('assignedTo')
        if who not in workers:
            continue
        entry = per_worker[who]
        cat = _card_failure_category(task)
        updated = task.get('updatedAt') or task.get('updated_at') or 0
        try:
            age_h = max(0.0, (time.time() - float(updated)) / 3600)
        except (TypeError, ValueError):
            age_h = None
        if cat:
            if age_h is None or age_h <= hours:
                entry['failedCards'].append({
                    'id': tid, 'title': str(task.get('title') or '')[:120],
                    'category': cat, 'room': task.get('room'),
                    'ageHours': round(age_h, 1) if age_h is not None else None,
                })
        else:
            entry['openCards'] += 1
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT agent_id, action, details FROM action_log "
                "WHERE ts >= ? LIMIT 20000", (cutoff,)).fetchall()
    except Exception:
        rows = []
    for agent_id, action, details in rows:
        entry = per_worker.get(agent_id)
        if entry is None:
            continue
        if action == 'execute_classify':
            try:
                d = json.loads(details)
            except Exception:
                continue
            if d.get('decision') == 'blocked':
                entry['blockedCommands'] += 1
        elif action.endswith('_failed') or 'error' in action \
                or 'timeout' in action:
            entry['failedActions'] += 1
    out = []
    for w in workers:
        e = per_worker[w]
        e['failedCards'].sort(key=lambda c: c.get('ageHours') or 0)
        out.append({'workerId': w, **e})
    out.sort(key=lambda x: -(len(x['failedCards']) + x['blockedCommands']
                             + x['failedActions']))
    return out


@app.get('/api/ops/cards')
async def api_ops_cards(request: Request):
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
        hours = max(1, min(int(request.query_params.get('hours') or 24), 168))
    except ValueError:
        hours = 24
    scope = (request.query_params.get('scope') or '').strip().lower()
    wanted = (request.query_params.get('directorId') or '').strip().lower()
    blob = {}
    if is_agent:
        try:
            with _db() as conn:
                row = conn.execute(
                    'SELECT blob FROM roster_agent WHERE id = ?',
                    (agent_id,)).fetchone()
            blob = json.loads(row[0]) if row else {}
        except Exception:
            blob = {}
    is_scan_authority = (is_agent and (blob.get('role') == 'regional_vp'
                                       or blob.get('role') == 'Chief of Staff'
                                       or blob.get('isAdmin')))
    if is_player or is_scan_authority:
        if wanted and wanted not in ('me',):
            span = _roster_span(wanted)
        elif scope == 'tank':
            span = None  # every span
        else:
            span = None
    elif is_agent:
        # A director (or anyone) sees their OWN span only.
        span = _roster_span(agent_id)
    else:
        span = []
    if span is None:
        # Tank scope: group every worker with a reporting director.
        try:
            with _db() as conn:
                rows = conn.execute('SELECT blob FROM roster_agent'
                                    ).fetchall()
        except Exception:
            rows = []
        workers = []
        for (b,) in rows:
            try:
                r = json.loads(b)
            except Exception:
                continue
            if r.get('director'):
                workers.append(r.get('id'))
        span = [w for w in workers if w]
    cards = await asyncio.to_thread(_ops_cards, hours, span)
    log_action(agent_id or 'player', 'ops_cards_read',
               {'hours': hours, 'span': wanted or ('tank' if span is None
                                                   else 'own'),
                'workers': len(span)}, authorized=True)
    return JSONResponse({'windowHours': hours, 'workers': cards})
