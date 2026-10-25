"""Per-agent trace feed for the ops page.

The ops page already shows browser sessions. This endpoint exposes the rest
of an agent's trail in one place: tool calls and actions from action_log,
model replies kept in short-term memory, plan checkpoints, and JEV decisions
correlated by trace id. Read-only and operator-gated.
"""


def _trace_json(raw, default=None):
    if not isinstance(raw, str):
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, (dict, list)) else default


def _action_summary(action, details):
    if not isinstance(details, dict):
        return str(details or '')[:400]
    for key in ('command', 'url', 'path', 'target', 'title', 'summary',
                'purpose', 'message', 'text', 'decision', 'error', 'reason',
                'goal', 'issue', 'taskId', 'status'):
        value = details.get(key)
        if value:
            return str(value)[:400]
    return action


def _action_event(row):
    _id, action, raw, ts, trace_id = row
    details = _trace_json(raw, raw if raw else {})
    if isinstance(details, dict):
        tool = details.get('tool') or details.get('op') or action
    else:
        tool = action
    return {
        'ts': ts,
        'kind': 'action',
        'action': action,
        'tool': str(tool)[:80],
        'summary': _action_summary(action, details),
        'details': details if isinstance(details, (dict, list)) else str(details)[:1200],
        'traceId': trace_id,
    }


@app.get('/api/ops/agents')
async def ops_agents(request: Request):
    authed, _ = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db() or {}
    agents = state.get('agents') or {}
    roster = state.get('agentRoster') or []
    since = time.time() - 86400
    recent = {}
    with _db() as conn:
        for aid, last, count in conn.execute(
                'SELECT agent_id, MAX(ts), COUNT(*) FROM action_log '
                'WHERE ts >= ? GROUP BY agent_id', (since,)):
            recent[aid] = {'lastTs': last, 'lastDay': count}
    rows = []
    seen = set()
    for entry in roster:
        aid = entry.get('id')
        if not aid or aid in seen:
            continue
        seen.add(aid)
        live = agents.get(aid) or {}
        rows.append({
            'id': aid,
            'name': entry.get('name') or aid,
            'role': entry.get('role') or 'worker',
            'isAdmin': bool(entry.get('isAdmin')),
            'isDirector': bool(entry.get('isDirector')),
            'busy': bool(live.get('busy')),
            'offDuty': bool(live.get('offDuty')),
            'task': live.get('task'),
            'lastTs': (recent.get(aid) or {}).get('lastTs'),
            'lastDay': (recent.get(aid) or {}).get('lastDay') or 0,
        })
    for aid, meta in recent.items():
        if not aid or aid in seen:
            continue
        seen.add(aid)
        live = agents.get(aid) or {}
        rows.append({
            'id': aid, 'name': aid, 'role': 'system',
            'isAdmin': aid == 'admin', 'isDirector': False,
            'busy': bool(live.get('busy')), 'offDuty': bool(live.get('offDuty')),
            'task': live.get('task'), 'lastTs': meta.get('lastTs'),
            'lastDay': meta.get('lastDay') or 0,
        })
    rows.sort(key=lambda row: row.get('lastTs') or 0, reverse=True)
    return JSONResponse({'agents': rows})


@app.get('/api/ops/agents/{agent_id}/trace')
async def ops_agent_trace(agent_id: str, request: Request, limit: int = 150):
    authed, _ = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db() or {}
    roster_ids = {d.get('id') for d in (state.get('agentRoster') or [])}
    if agent_id not in roster_ids and agent_id not in (state.get('agents') or {}) \
            and agent_id not in ('admin', 'player', 'system'):
        return JSONResponse({'error': 'unknown agent'}, status_code=404)
    limit = max(1, min(int(limit or 150), 300))
    events = []
    with _db() as conn:
        rows = conn.execute(
            'SELECT id, action, details, ts, trace_id FROM action_log '
            'WHERE agent_id = ? ORDER BY ts DESC LIMIT ?',
            (agent_id, limit)).fetchall()
        events.extend(_action_event(row) for row in rows)
        trace_ids = [row[4] for row in rows if row[4]]
        if trace_ids:
            marks = ','.join('?' for _ in trace_ids)
            for kind, model, choice, confidence, cost, ts in conn.execute(
                    f'SELECT kind, model, choice, confidence, cost, ts FROM decision_tape '
                    f'WHERE trace_id IN ({marks}) ORDER BY ts DESC LIMIT ?',
                    (*trace_ids, limit)):
                events.append({
                    'ts': ts, 'kind': 'decision', 'action': kind,
                    'tool': 'jev', 'model': model, 'choice': choice,
                    'confidence': confidence, 'cost': cost,
                    'summary': f'{kind}: {choice or "no verdict"}'
                               + (f' ({confidence:.2f})' if isinstance(confidence, (int, float)) else ''),
                })
        sessions = (f'player-ask:{agent_id}', f'chat:{agent_id}', f'agent:{agent_id}')
        marks = ','.join('?' for _ in sessions)
        for role, content, created_at in conn.execute(
                f'SELECT role, content, created_at FROM stm_messages '
                f'WHERE session_id IN ({marks}) ORDER BY created_at DESC LIMIT ?',
                (*sessions, limit)):
            text = str(content or '')[:2000]
            events.append({
                'ts': created_at,
                'kind': 'thought' if role == 'assistant' else 'message',
                'action': role, 'tool': 'memory',
                'summary': text[:400], 'text': text,
            })
        task_ids = [tid for tid, task in (state.get('tasks') or {}).items()
                    if task.get('assignedTo') == agent_id]
        if task_ids:
            marks = ','.join('?' for _ in task_ids)
            for task_id, phase, payload, updated_at in conn.execute(
                    f'SELECT task_id, phase, payload, updated_at FROM task_checkpoints '
                    f'WHERE task_id IN ({marks}) ORDER BY updated_at DESC LIMIT ?',
                    (*task_ids, limit)):
                data = _trace_json(payload, {})
                plan = (data or {}).get('plan') if isinstance(data, dict) else None
                if not plan:
                    continue
                events.append({
                    'ts': updated_at, 'kind': 'thought', 'action': phase,
                    'tool': 'task-checkpoint', 'taskId': task_id,
                    'summary': str(plan)[:400], 'text': str(plan)[:4000],
                })
    events.sort(key=lambda event: event.get('ts') or 0, reverse=True)
    return JSONResponse({'agentId': agent_id, 'events': events[:limit],
                         'count': len(events)})
