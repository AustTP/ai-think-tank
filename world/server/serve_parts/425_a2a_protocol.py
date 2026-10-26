"""A2A facade: one Agent Card and a JSON-RPC endpoint at the edge.

The think tank's internal coordination stays as it is (shared state, mail,
handoffs, the task cycle). This module exposes the tank AS ONE external agent
so another agent or tool can submit a goal and read the result:

  GET  /.well-known/agent-card.json   public Agent Card
  POST /a2a                           JSON-RPC 2.0

Supported methods:
  message/send  -> file a large request with a team; returns an A2A Task whose
                   id is the think tank request id (wrq-N)
  tasks/get     -> read that request's state, linked cards, and Library files
  tasks/cancel  -> cancel a request whose cards are still queued

Auth: a player session, or a director/admin agent key as
`Authorization: Bearer <key>` (or the existing X-Agent-Key header). Streaming
and push notifications are deliberately not advertised.
"""

A2A_PROTOCOL_VERSION = '0.3.0'


def _a2a_caller(request):
    """The authenticated agent id, or 'player' for a session. None when the
    request carries no usable credential (the middleware also rejects those)."""
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return 'player'
    key = (request.headers.get('X-Agent-Key') or '').strip()
    auth = (request.headers.get('Authorization') or '').strip()
    if not key and auth.lower().startswith('bearer '):
        key = auth[7:].strip()
    if not key:
        return None
    with _db() as conn:
        row = conn.execute('SELECT agent_id FROM agent_keys WHERE secret_key = ?',
                           (key,)).fetchone()
    return row[0] if row else None


def _a2a_is_operator(state, caller):
    if caller == 'player':
        return True
    return bool(caller and _is_director_or_admin(state, caller))


def _a2a_error(request_id, code, message, status_code=200):
    return JSONResponse({'jsonrpc': '2.0', 'id': request_id,
                         'error': {'code': code, 'message': message}},
                        status_code=status_code)


def _a2a_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _a2a_find_request(state, task_id):
    for req in state.get('backlogRequests') or []:
        if req.get('id') == task_id:
            return req
    return None


def _a2a_linked(state, request_id):
    tasks = [t for t in (state.get('tasks') or {}).values()
             if t.get('requestId') == request_id]
    seen = {t.get('id') for t in tasks}
    # Completed cards are pruned from the live map; read the archive too so a
    # tasks/get after completion still returns its Library artifact.
    try:
        archived = _archived_tasks()
    except Exception:
        archived = {}
    for task in archived.values():
        if task.get('requestId') == request_id and task.get('id') not in seen:
            tasks.append(task)
            seen.add(task.get('id'))
    queued = [q for q in (state.get('workQueue') or [])
              if q.get('requestId') == request_id]
    return tasks, queued


def _a2a_task_state(req, tasks, queued):
    if req.get('status') == 'cancelled':
        return 'canceled'
    if not tasks and not queued:
        return 'submitted' if req.get('status') == 'pending' else 'working'
    statuses = [t.get('status') for t in tasks]
    if statuses and all(s in ('done', 'closed') for s in statuses) and not queued:
        return 'completed'
    if any(s in ('failed', 'abandoned') for s in statuses):
        return 'failed'
    if any(s == 'blocked' for s in statuses):
        return 'input-required'
    if statuses and all(s == 'cancelled' for s in statuses) and not queued:
        return 'canceled'
    return 'working'


def _a2a_status_message(state_name, req, tasks, queued):
    if state_name == 'submitted':
        return 'Breakdown is queued for the receiving team.'
    if state_name == 'completed':
        return f'{len(tasks)} card(s) completed.'
    if state_name == 'failed':
        failed = [t.get('title') or t.get('id') for t in tasks
                  if t.get('status') in ('failed', 'abandoned')]
        return 'Failed: ' + '; '.join(str(x) for x in failed[:3])
    if state_name == 'input-required':
        return 'A card is blocked and needs a decision.'
    if state_name == 'canceled':
        return 'Canceled.'
    if tasks:
        return f'{len(tasks)} card(s) in progress, {len(queued)} queued.'
    return 'Breakdown complete; cards are waiting in the queue.'


def _a2a_task_payload(req, state):
    tasks, queued = _a2a_linked(state, req.get('id'))
    state_name = _a2a_task_state(req, tasks, queued)
    artifacts = []
    for t in tasks:
        path = t.get('libraryPath') or t.get('reportPath')
        if not path:
            continue
        artifacts.append({
            'artifactId': f"{t.get('id')}-deliverable",
            'name': t.get('title') or t.get('id'),
            'parts': [{'kind': 'text', 'text': str(path)}],
            'metadata': {'taskId': t.get('id'), 'libraryPath': str(path),
                         'status': t.get('status')},
        })
    goal = req.get('goal') or req.get('title') or ''
    return {
        'id': req.get('id'),
        'contextId': req.get('a2aContextId') or f"ctx-{req.get('id')}",
        'status': {
            'state': state_name,
            'timestamp': _a2a_now_iso(),
            'message': _a2a_status_message(state_name, req, tasks, queued),
        },
        'artifacts': artifacts,
        'history': ([{'role': 'user', 'messageId': req.get('a2aMessageId') or f"msg-{req.get('id')}",
                      'parts': [{'kind': 'text', 'text': goal}]}] if goal else []),
        'metadata': {
            'goal': goal,
            'teamId': req.get('teamId'),
            'requestId': req.get('id'),
            'taskIds': [t.get('id') for t in tasks],
            'queued': len(queued),
            'origin': req.get('origin') or 'large_request',
        },
    }


def _a2a_pick_team(state, authority_id, wanted):
    if wanted:
        return _team_by_id(state, wanted)
    team = _staffing_team_for_authority(state, authority_id)
    if team:
        return team
    teams = state.get('teams') or []
    return teams[0] if teams else None


@app.get('/.well-known/agent-card.json')
@app.get('/.well-known/agent.json')
async def a2a_agent_card(request: Request):
    base = str(request.base_url).rstrip('/')
    return JSONResponse({
        'protocolVersion': A2A_PROTOCOL_VERSION,
        'name': 'AI Think Tank',
        'description': ('A simulated organization that plans a goal, breaks it '
                        'into cards, works them with peer review, and files the '
                        'deliverables in its Library.'),
        'url': base + '/a2a',
        'preferredTransport': 'JSONRPC',
        'version': '1.0.0',
        'capabilities': {
            'streaming': False,
            'pushNotifications': False,
            'stateTransitionHistory': False,
        },
        'defaultInputModes': ['text/plain'],
        'defaultOutputModes': ['text/plain'],
        'skills': [
            {'id': 'file_work', 'name': 'File work',
             'description': 'Submit a goal; the tank breaks it into cards and works it.',
             'tags': ['planning', 'execution', 'delegation']},
            {'id': 'ask_status', 'name': 'Ask status',
             'description': 'Read a submitted task, its cards, and its artifacts.',
             'tags': ['status', 'tasks']},
            {'id': 'cancel_work', 'name': 'Cancel work',
             'description': 'Cancel a submitted goal while its cards are still queued.',
             'tags': ['tasks', 'cancel']},
            {'id': 'fetch_artifact', 'name': 'Fetch artifact',
             'description': 'Artifact entries name the Library path each completed card saved.',
             'tags': ['artifacts', 'library']},
        ],
        'securitySchemes': {'bearer': {'type': 'http', 'scheme': 'bearer'}},
        'security': [{'bearer': []}],
    })


@app.post('/a2a')
async def a2a_jsonrpc(request: Request):
    caller = _a2a_caller(request)
    if not caller:
        return _a2a_error(None, -32001, 'Authentication required.', status_code=401)
    try:
        body = await request.json()
    except Exception:
        return _a2a_error(None, -32700, 'Parse error.', status_code=400)
    if not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
        return _a2a_error(None, -32600, 'Invalid JSON-RPC request.')
    request_id = body.get('id')
    method = body.get('method')
    params = body.get('params') if isinstance(body.get('params'), dict) else {}

    if method == 'message/send':
        return await _a2a_message_send(request_id, params, caller)
    if method == 'tasks/get':
        return await _a2a_tasks_get(request_id, params, caller)
    if method == 'tasks/cancel':
        return await _a2a_tasks_cancel(request_id, params, caller)
    return _a2a_error(request_id, -32601, f'Unsupported method: {method}')


async def _a2a_message_send(request_id, params, caller):
    message = params.get('message') if isinstance(params.get('message'), dict) else {}
    parts = message.get('parts') if isinstance(message.get('parts'), list) else []
    text = ' '.join(
        str(p.get('text') or '').strip()
        for p in parts
        if isinstance(p, dict) and p.get('text') and p.get('kind', 'text') == 'text'
    ).strip()
    metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
    goal = str(metadata.get('goal') or text).strip()
    if not goal:
        return _a2a_error(request_id, -32602, 'A non-empty text part or metadata.goal is required.')
    message_id = str(message.get('messageId') or '').strip() or None
    context_id = str(message.get('contextId') or params.get('contextId') or '').strip() or None

    state = _state_begin()
    if not state:
        return _a2a_error(request_id, -32000, 'State unavailable; retry shortly.')
    try:
        if not _a2a_is_operator(state, caller):
            return _a2a_error(request_id, -32001,
                              'This A2A endpoint requires a director/admin key.',
                              status_code=403)
        if message_id:
            for req in state.get('backlogRequests') or []:
                if req.get('a2aMessageId') == message_id:
                    return JSONResponse({'jsonrpc': '2.0', 'id': request_id,
                                         'result': _a2a_task_payload(req, state)})
        authority = _free_authority(state) or _wake_authority_on_request(state)
        if not authority:
            authority = next((d for d in (state.get('agentRoster') or [])
                              if d.get('isAdmin')), None)
        if not authority:
            return _a2a_error(request_id, -32000, 'No authority is available to file the work.')
        team = _a2a_pick_team(state, authority.get('id'), metadata.get('team'))
        if not team:
            return _a2a_error(request_id, -32000, 'No team is available to take the work.')
        constraints = {}
        for key in ('budgetUsd', 'constraints', 'roles', 'order', 'mustInclude'):
            if key in metadata:
                constraints[key] = metadata[key]
        import sim as _sim
        req = _sim.file_large_request(
            state, authority.get('id'), goal,
            team.get('id') or team.get('directorId'),
            int(time.time() * 1000), constraints=constraints)
        if not req:
            return _a2a_error(request_id, -32602, 'Could not file the request (empty goal or team).')
        req['origin'] = 'a2a'
        req['a2aMessageId'] = message_id
        req['a2aContextId'] = context_id
        req['a2aCaller'] = caller
        save_state_to_db(state)
        log_action(caller, 'a2a_task_submitted',
                   {'requestId': req['id'], 'goal': goal[:200],
                    'teamId': req.get('teamId')}, authorized=True)
        return JSONResponse({'jsonrpc': '2.0', 'id': request_id,
                             'result': _a2a_task_payload(req, state)})
    finally:
        _state_abort()


async def _a2a_tasks_get(request_id, params, caller):
    task_id = str(params.get('id') or '').strip()
    if not task_id:
        return _a2a_error(request_id, -32602, 'params.id is required.')
    state = get_state_from_db() or {}
    if not _a2a_is_operator(state, caller):
        return _a2a_error(request_id, -32001,
                          'This A2A endpoint requires a director/admin key.',
                          status_code=403)
    req = _a2a_find_request(state, task_id)
    if not req:
        return _a2a_error(request_id, -32001, f'Task not found: {task_id}')
    if caller != 'player' and req.get('a2aCaller') != caller:
        return _a2a_error(request_id, -32001,
                          f'Task not found: {task_id}', status_code=403)
    return JSONResponse({'jsonrpc': '2.0', 'id': request_id,
                         'result': _a2a_task_payload(req, state)})


async def _a2a_tasks_cancel(request_id, params, caller):
    task_id = str(params.get('id') or '').strip()
    if not task_id:
        return _a2a_error(request_id, -32602, 'params.id is required.')
    state = _state_begin()
    if not state:
        return _a2a_error(request_id, -32000, 'State unavailable; retry shortly.')
    try:
        if not _a2a_is_operator(state, caller):
            return _a2a_error(request_id, -32001,
                              'This A2A endpoint requires a director/admin key.',
                              status_code=403)
        req = _a2a_find_request(state, task_id)
        if not req:
            return _a2a_error(request_id, -32001, f'Task not found: {task_id}')
        if caller != 'player' and req.get('a2aCaller') != caller:
            return _a2a_error(request_id, -32001,
                              f'Task not found: {task_id}', status_code=403)
        if req.get('status') == 'cancelled':
            return JSONResponse({'jsonrpc': '2.0', 'id': request_id,
                                 'result': _a2a_task_payload(req, state)})
        tasks, queued = _a2a_linked(state, task_id)
        if tasks:
            # A card already handed to a worker is not cancelable from here;
            # the agents own that lifecycle (blocked, repair, peer review).
            return _a2a_error(request_id, -32002,
                              'Task is already being worked and cannot be canceled.')
        if queued:
            state['workQueue'] = [q for q in state.get('workQueue') or []
                                  if q.get('requestId') != task_id]
        req['status'] = 'cancelled'
        req['cancelledAt'] = int(time.time() * 1000)
        req['cancelledBy'] = caller
        save_state_to_db(state)
        log_action(caller, 'a2a_task_cancelled',
                   {'requestId': task_id, 'removedCards': len(queued)}, authorized=True)
        return JSONResponse({'jsonrpc': '2.0', 'id': request_id,
                             'result': _a2a_task_payload(req, state)})
    finally:
        _state_abort()
