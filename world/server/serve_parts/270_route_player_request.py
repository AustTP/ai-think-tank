

async def _route_player_request(state, text, session_id=None,
                                 requester_id=None, raw_key=None):
    """Classify a free-text player request into a lane (_ROUTING_LANES) and
    dispatch to the matching handler. Entry point for the Telegram bridge
    (_telegram_process_update) -- replaces its old unconditional _ask_core
    pin. Returns the same {'reply', ...} / {'error', 'status'} shape
    _ask_core already produces, since every lane either wraps _ask_core
    directly or builds its own reply text before returning.

    `session_id` (the Telegram chat id) is forwarded to the ask/unclear lanes
    so the chief of staff answers follow-ups in conversation context (world/memory.py);
    the other lanes have their own handlers and ignore it.

    `requester_id` is who is talking to Theo: None is the player's own
    session -- the owner of the think tank -- and an agent id is that agent.
    The role policy (world/theo_policy.py) decides what this requester may
    ask Theo to do BEFORE any lane handler runs: a denied request gets a
    reply saying why, and a request that needs the owner's approval gets a
    reply recording the pending approval id. The player is the owner, so
    the player's own requests are unchanged.

    `raw_key` is a granted Theo key (ask 3): the credential an agent
    presents instead of a roster identity. The key names the agent and the
    role it holds, so the request is decided by evaluate_key -- the key's
    own role, rate limit, and budget, not the roster's -- and an invalid,
    revoked, or expired key is refused before any classification runs. An
    owner session (requester_id None, no key) keeps the approval-command
    parse; a key holder never adjudicates approvals.

    The owner adjudicates a pending approval by texting
    "approve apr_<id>" or "deny apr_<id>"; that is handled before lane
    classification so an approval answer is never mistaken for a request.

    Every lane except ask/unclear mutates `state` and is responsible for its
    OWN save_state_to_db call -- _ask_core never touches state, so this
    function itself must not assume one happened."""
    text = (text or '').strip()
    if not text:
        return {'error': 'a message is required', 'status': 400}
    key = None
    if raw_key:
        if _theo_policy.registry is None:
            return {'error': 'key registry not configured',
                    'status': 503}
        key = _theo_policy.registry.verify(raw_key)
        if key is None:
            return {'error': 'invalid, revoked, or expired key',
                    'status': 401}
    if requester_id is None and raw_key is None:
        command = _parse_approval_command(text)
        if command:
            verb, approval_id = command
            if verb == 'approve':
                decided = _theo_policy.approve(state, None, approval_id)
            else:
                decided = _theo_policy.deny_approval(state, None, approval_id)
            log_action('player', 'theo_approval_decided',
                       {'verb': verb, 'approvalId': approval_id,
                        'decided': decided},
                       authorized=True)
            if decided:
                return {'reply': 'Approval %s %s.'
                        % (approval_id, 'approved' if verb == 'approve'
                           else 'denied')}
            return {'reply': 'No pending approval %s to decide -- or it '
                             'was already decided.' % approval_id}
        # Draft-desk owner commands (Tier-2, 2026-10-30): "draft" generates
        # 0-2 grounded local-area candidates, "post <id>"/"skip <id>" decide
        # them, "drafts" lists pending. Handled BEFORE lane classification so
        # a draft decision is never mistaken for a request. "post" is a
        # ledger entry only -- the tank never posts anywhere (Facebook hard
        # rule: the operator's personal account is untouchable by the tank).
        draft_reply = _draft_desk_handle_owner_command(state, text, session_id)
        if draft_reply is not None:
            return await draft_reply
    admin_id = _admin_agent_id(state)
    lane = await asyncio.to_thread(_lane_decider, state, text) or 'unclear'
    actor_id = (key.agent_id if key is not None
                else requester_id) or 'player'
    log_action('player', 'route_classified',
               {'lane': lane, 'text': text[:200],
                'requester': actor_id},
               authorized=True)
    action = _LANE_ACTIONS.get(lane, 'converse')
    if key is not None:
        decision = _theo_policy.evaluate_key(
            theo_policy.ActionRequest(action, {'text': text}),
            raw_key)
    else:
        decision = _theo_policy.evaluate(state, requester_id, action,
                                         {'text': text})
    if not decision.allowed:
        actor = actor_id
        if decision.requires_approval:
            log_action(actor, 'theo_approval_required',
                       {'lane': lane, 'action': action,
                        'approvalId': decision.approval_id},
                       authorized=False)
            return {'reply': "That needs the owner's approval first -- I've "
                             'recorded approval %s for it.'
                             % decision.approval_id}
        log_action(actor, 'theo_denied',
                   {'lane': lane, 'action': action,
                    'reason': decision.reason},
                   authorized=False)
        return {'reply': "I can't do that: %s." % decision.reason}
    handler = _ROUTING_HANDLERS.get(lane, _route_lane_unclear)
    if lane in ('ask', 'unclear'):
        return await handler(state, text, admin_id, session_id)
    return await handler(state, text, admin_id)


@app.post('/api/theo/ask')
@_state_writer
async def theo_ask(request: Request):
    """Ask Theo with a granted key (ask 3) instead of a
    player session or a direct agent ask. The key's agent
    acts through the role, rate limit, and budget the owner
    granted -- not through the roster lane it would have
    talking to Theo directly. Body: {text}; the raw key
    travels in the X-Agent-Key header, the same credential
    the other agent-authenticated routes already use."""
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    raw_key = (request.headers.get('X-Agent-Key') or '').strip()
    if not raw_key:
        return JSONResponse(
            {'error': 'X-Agent-Key header is required'},
            status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {'error': 'body must be a JSON object'},
            status_code=400)
    text = body.get('text')
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {'error': '"text" is required'}, status_code=400)
    result = await _route_player_request(state, text,
                                          raw_key=raw_key)
    if 'error' in result:
        return JSONResponse({'error': result['error']},
                            status_code=result.get('status', 400))
    return JSONResponse(result)


@app.post('/api/intent/schedule')
@_state_writer
async def intent_schedule(request: Request):
    """Structured parity endpoint for the schedule lane: POST
    {topic, startUrl, cadenceMs, dependsOnTask?}. `dependsOnTask` optionally
    gates the recurring topic until that task id is done. The free-text
    extraction step (_extract_schedule_fields_sync) is exclusive to the
    Telegram routing layer; this takes already-structured fields -- the same
    relationship /api/intent/ask already has to _ask_core."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    import sim as _sim
    record = _sim.add_research_topic(state, body.get('topic'), body.get('startUrl'),
                                     body.get('cadenceMs'),
                                     depends_on_task=(body.get('dependsOnTask') or '').strip() or None)
    if not record:
        return JSONResponse({'error': 'topic, a real absolute startUrl, and cadenceMs are required'}, status_code=400)
    note_watchlist_topic(record)  # reflect the villager's addition into world/watchlist.json
    save_state_to_db(state)
    log_action('player', 'schedule_created', {'topicId': record['id'], 'topic': record['topic'],
                                              'dependsOnTask': record.get('dependsOnTask')}, authorized=True)
    _append_passport_decision('schedule_created', 'player', {'topicId': record['id']})
    return JSONResponse({'ok': True, 'topic': record})


@app.get('/api/schedules')
async def list_schedules(request: Request):
    """The player's schedule control panel: every recurring research topic and
    every pending one-off, in one read. Player-session only."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db() or {}
    topics = []
    for t in (state.get('researchTopics') or []):
        topics.append({
            'id': t.get('id'),
            'topic': t.get('topic'),
            'startUrl': t.get('startUrl'),
            'cadenceMs': t.get('cadenceMs'),
            'enabled': t.get('enabled', True) is not False,
            'lastRunAt': t.get('lastRunAt') or 0,
            'dependsOnTask': t.get('dependsOnTask'),
            'longHorizon': bool(t.get('longHorizon')),
        })
    one_offs = []
    for index, item in enumerate(state.get('workQueue') or []):
        if not item.get('notBefore'):
            continue
        one_offs.append({
            'key': f'q:{index}', 'title': item.get('title'),
            'at': item.get('notBefore'), 'room': item.get('room'),
            'assignedTo': item.get('assignedTo'),
            'dependsOnTask': item.get('dependsOn'),
            'status': 'queued',
        })
    for tid, task in (state.get('tasks') or {}).items():
        if not task.get('notBefore') or task.get('status') in ('done', 'cancelled', 'abandoned'):
            continue
        one_offs.append({
            'key': f't:{tid}', 'taskId': tid, 'title': task.get('title'),
            'at': task.get('notBefore'), 'room': task.get('room'),
            'assignedTo': task.get('assignedTo'),
            'dependsOnTask': task.get('dependsOn'),
            'status': task.get('status'),
        })
    one_offs.sort(key=lambda x: int(x.get('at') or 0))
    return JSONResponse({'ok': True, 'topics': topics, 'oneOffs': one_offs})


@app.patch('/api/schedules/topic/{topic_id}')
@_state_writer
async def update_schedule_topic(topic_id: str, request: Request):
    """Edit one recurring topic in place. Player-session only; mirrors the edit
    into world/watchlist.json so the live sync does not undo it."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    topics = state.get('researchTopics') or []
    record = next((t for t in topics if t.get('id') == topic_id), None)
    if not record:
        return JSONResponse({'error': 'unknown schedule'}, status_code=404)
    old = (record.get('topic'), record.get('startUrl'))
    topic = (body.get('topic') or record.get('topic') or '').strip()
    start_url = (body.get('startUrl') or record.get('startUrl') or '').strip()
    parsed = urllib.parse.urlparse(start_url)
    if not topic or parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return JSONResponse({'error': 'topic and a real absolute startUrl are required'},
                            status_code=400)
    import sim as _sim
    try:
        cadence_ms = int(body.get('cadenceMs') or record.get('cadenceMs') or 0)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'cadenceMs must be a number'}, status_code=400)
    cadence_ms = max(cadence_ms, _sim.MIN_RESEARCH_CADENCE_MS)
    enabled = body.get('enabled', record.get('enabled', True)) is not False
    record.update(topic=topic, startUrl=start_url, cadenceMs=cadence_ms,
                  enabled=enabled)
    if 'dependsOnTask' in body:
        record['dependsOnTask'] = (body.get('dependsOnTask') or '').strip() or None
    config.update_watchlist_topic(old[0], old[1], record)
    save_state_to_db(state)
    log_action('player', 'schedule_updated', {'topicId': record.get('id'),
                                              'topic': topic, 'enabled': enabled},
               authorized=True)
    return JSONResponse({'ok': True, 'topic': record})


@app.delete('/api/schedules/topic/{topic_id}')
@_state_writer
async def delete_schedule_topic(topic_id: str, request: Request):
    """Delete one recurring topic from state and from the operator watchlist."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    topics = state.get('researchTopics') or []
    record = next((t for t in topics if t.get('id') == topic_id), None)
    if not record:
        return JSONResponse({'error': 'unknown schedule'}, status_code=404)
    state['researchTopics'] = [t for t in topics if t.get('id') != topic_id]
    config.remove_watchlist_topic(record.get('topic'), record.get('startUrl'))
    save_state_to_db(state)
    log_action('player', 'schedule_deleted', {'topicId': topic_id,
                                             'topic': record.get('topic')},
               authorized=True)
    return JSONResponse({'ok': True})


@app.patch('/api/schedules/once/{key}')
@_state_writer
async def update_schedule_once(key: str, request: Request):
    """Edit or retime a pending one-off. `key` is q:<index> while it waits in
    the queue, t:<taskId> once it has been assigned."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    at_ms = _parse_schedule_once_at(body.get('at')) if body.get('at') else None
    title = (body.get('title') or '').strip() or None
    if body.get('at') and not at_ms:
        return JSONResponse({'error': '`at` must be an ISO 8601 time or epoch ms'},
                            status_code=400)
    kind, _, ident = key.partition(':')
    if kind == 'q':
        try:
            item = state['workQueue'][int(ident)]
        except (KeyError, IndexError, ValueError):
            return JSONResponse({'error': 'unknown one-off'}, status_code=404)
        if at_ms:
            item['notBefore'] = at_ms
        if title:
            item['title'] = title
        save_state_to_db(state)
        return JSONResponse({'ok': True, 'oneOff': {'key': key, 'title': item.get('title'),
                                                    'at': item.get('notBefore')}})
    if kind == 't':
        task = (state.get('tasks') or {}).get(ident)
        if not task:
            return JSONResponse({'error': 'unknown one-off'}, status_code=404)
        if at_ms:
            task['notBefore'] = at_ms
        if title:
            task['title'] = title
        save_state_to_db(state)
        return JSONResponse({'ok': True, 'oneOff': {'key': key, 'title': task.get('title'),
                                                    'at': task.get('notBefore')}})
    return JSONResponse({'error': 'key must be q:<index> or t:<taskId>'}, status_code=400)


@app.delete('/api/schedules/once/{key}')
@_state_writer
async def delete_schedule_once(key: str, request: Request):
    """Cancel a pending one-off (queue item or assigned task)."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    kind, _, ident = key.partition(':')
    if kind == 'q':
        try:
            item = state['workQueue'][int(ident)]
        except (KeyError, IndexError, ValueError):
            return JSONResponse({'error': 'unknown one-off'}, status_code=404)
        state['workQueue'].pop(int(ident))
        save_state_to_db(state)
        log_action('player', 'schedule_deleted', {'oneOff': key, 'title': item.get('title')},
                   authorized=True)
        return JSONResponse({'ok': True})
    if kind == 't':
        task = (state.get('tasks') or {}).get(ident)
        if not task:
            return JSONResponse({'error': 'unknown one-off'}, status_code=404)
        task['status'] = 'cancelled'
        task['cancelledAt'] = int(time.time() * 1000)
        task['notBefore'] = None
        save_state_to_db(state)
        log_action('player', 'schedule_deleted', {'oneOff': key, 'title': task.get('title')},
                   authorized=True)
        return JSONResponse({'ok': True})
    return JSONResponse({'error': 'key must be q:<index> or t:<taskId>'}, status_code=400)


@app.post('/api/intent/schedule-once')
@_state_writer
async def intent_schedule_once(request: Request):
    """Structured parity endpoint for the one-off scheduling lane: POST
    {title, at, room?, instructions?, taskType?, goal?, dependsOnTask?,
    assignedTo?}.
    `at` is an ISO 8601 day+time (a trailing 'Z' or explicit offset; naive
    times are treated as UTC) or an epoch timestamp. `dependsOnTask`
    optionally holds the one-off in the queue until that task id is done.
    `assignedTo` optionally hands the one-off to the PLAYER instead of an
    agent ('player' -- the only accepted value): the card is delivered into
    your player-inbox and anything queued dependsOnTask on it resumes when
    you mark it done. Combined with dependsOnTask this is the "give me X once
    story T lands" scheduling lane.
    The task sits in the work queue untouched (its notBefore gate) until
    `at`, then runs the normal lifecycle exactly once. The free-text
    extraction step (_extract_schedule_once_fields_sync) is exclusive to the
    Telegram routing layer; this takes already-structured fields."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    title = (body.get('title') or '').strip()
    at_ms = _parse_schedule_once_at(body.get('at'))
    if not title or not at_ms:
        return JSONResponse({'error': 'title and a parseable future `at` (ISO 8601 or epoch) are required'}, status_code=400)
    now_ms = int(time.time() * 1000)
    if at_ms <= now_ms:
        return JSONResponse({'error': '`at` must be in the future'}, status_code=400)
    if at_ms - now_ms > SCHEDULE_ONCE_MAX_AHEAD_MS:
        return JSONResponse({'error': '`at` must be within a year of now'}, status_code=400)
    room = (body.get('room') or '').strip() or None
    instructions = (body.get('instructions') or '').strip() or None
    task_type = (body.get('taskType') or '').strip() or None
    goal = (body.get('goal') or '').strip() or None
    depends_on_task = (body.get('dependsOnTask') or '').strip() or None
    assigned_to = (body.get('assignedTo') or '').strip() or None
    if assigned_to and assigned_to != 'player':
        return JSONResponse({'error': '`assignedTo` only accepts "player" (hand this one-off to you)'}, status_code=400)
    import sim as _sim
    lane = (body.get('lane') or '').strip() or None
    if lane and lane not in _sim.LANES:
        return JSONResponse({'error': f'`lane` must be one of: {", ".join(sorted(_sim.LANES))}'}, status_code=400)
    item = _sim.queue_once(state, title, at_ms, room=room, instructions=instructions,
                           task_type=task_type, goal=goal, depends_on_task=depends_on_task,
                           assigned_to=assigned_to, lane=lane)
    if not item:
        return JSONResponse({'error': 'could not queue that one-off task'}, status_code=400)
    save_state_to_db(state)
    log_action('player', 'schedule_once_created', {'title': title, 'atMs': at_ms,
                                                   'room': room, 'taskType': task_type,
                                                   'dependsOnTask': depends_on_task,
                                                   'assignedTo': assigned_to,
                                                   'lane': lane},
               authorized=True)
    _append_passport_decision('schedule_once_created', 'player', {'title': title, 'atMs': at_ms})
    return JSONResponse({'ok': True, 'task': {'title': title, 'atMs': at_ms, 'notBefore': item.get('notBefore')}})


@app.post('/api/pipelines')
@_state_writer
async def pipelines_create(request: Request):
    """The player-facing ORDERED-pipeline lane: POST {name, cadenceMs, steps,
    dependsOnTask?}. Each step is {title, room, offsetMs, instructions, tool,
    args}. Steps fire in strict sequence -- step N+1 waits for step N's task
    to complete -- on a cadence floored to MIN_PIPELINE_CADENCE_MS (1h).
    `dependsOnTask` optionally holds the whole pipeline at its first run
    boundary until that task id is done. Same structured-parity relationship
    to _check_pipelines that /api/intent/schedule has to add_research_topic:
    this takes already-structured fields, no free-text extraction."""
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    import sim as _sim
    record = _sim.add_pipeline(state, body.get('name'), body.get('cadenceMs'), body.get('steps'),
                               depends_on_task=(body.get('dependsOnTask') or '').strip() or None)
    if not record:
        return JSONResponse({'error': 'name, a non-empty steps list (each with title and room), and cadenceMs are required'}, status_code=400)
    save_state_to_db(state)
    log_action('player', 'pipeline_created', {'pipelineId': record['id'], 'name': record['name'],
                                              'steps': len(record['steps']),
                                              'dependsOnTask': record.get('dependsOnTask')}, authorized=True)
    _append_passport_decision('pipeline_created', 'player', {'pipelineId': record['id']})
    return JSONResponse({'ok': True, 'pipeline': record})


@app.get('/api/pipelines')
async def pipelines_list(request: Request):
    """List the ordered pipelines currently scheduled, newest first."""
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    pipelines = list(reversed(state.get('pipelines') or []))
    return JSONResponse({'ok': True, 'pipelines': pipelines})


@app.delete('/api/pipelines/{pipeline_id}')
@_state_writer
async def pipelines_delete(request: Request, pipeline_id: str):
    """Delete one scheduled pipeline (and its unscheduled steps)."""
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    before = len(state.get('pipelines') or [])
    state['pipelines'] = [p for p in (state.get('pipelines') or []) if p.get('id') != pipeline_id]
    after = len(state.get('pipelines') or [])
    if before == after:
        return JSONResponse({'error': 'no such pipeline'}, status_code=404)
    save_state_to_db(state)
    log_action('player', 'pipeline_deleted', {'pipelineId': pipeline_id}, authorized=True)
    _append_passport_decision('pipeline_deleted', 'player', {'pipelineId': pipeline_id})
    return JSONResponse({'ok': True})


@app.post('/api/intent/incidents')
@_state_writer
async def intent_incidents(request: Request):
    """Structured parity endpoint for the incident lane: POST {productId, title}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    product_id = (body.get('productId') or '').strip()
    title = (body.get('title') or '').strip()
    if not product_id or not title:
        return JSONResponse({'error': 'productId and title are required'}, status_code=400)
    import sim as _sim
    queued = _sim.queue_bug(state, product_id, title, reported_by='player')
    if not queued:
        return JSONResponse({'error': 'could not route this incident (unknown product, no on-call, or one already open)'}, status_code=400)
    save_state_to_db(state)
    log_action('player', 'incident_filed', {'productId': product_id, 'title': title}, authorized=True)
    _append_passport_decision('incident_filed', 'player', {'productId': product_id, 'title': title})
    return JSONResponse({'ok': True, 'productId': product_id, 'title': title})


# ---------------------------------------------------------------------------
# Phase E: products + wiki. A PRODUCT is a named, director-authored artifact
# with spec/owner/contributors whose release freezes its sandbox repo into
# library/projects/<id>/v<N>/ and flips status to 'released'. The WIKI is a
# structured, director-structurable knowledge layer its pages' bodies are read
# by agents BEFORE they act (injected as context by the content executors).
# Reads are open to any authenticated client; the write surface (create/
# release/status, wiki page/category writes) is gated to directors/admins. The
# /api/intent is protected by default (deny-by-default auth middleware).
# Governing actions are chained into the hashed product-passport like
# sprints/teams. sim.py holds the pure state-catalog helpers; these wrappers
# do the real sandbox snapshot + disk write + passport chaining.
# ---------------------------------------------------------------------------
def _valid_sandbox_ids():
    # The real sandbox repos on disk (workroom-shared, research-shared, ...).
    # Only these may back a product; validated against the filesystem so a
    # typo'd id fails at create time, not at first release. Per-story git
    # worktrees (sandboxes/story-<task-id>) are ephemeral isolation trees
    # merged back into their base workroom on gate close -- they must never
    # appear as product-binding candidates.
    if os.path.isdir(SANDBOXES_DIR):
        try:
            return sorted(d for d in os.listdir(SANDBOXES_DIR)
                          if os.path.isdir(os.path.join(SANDBOXES_DIR, d))
                          and not is_story_sandbox_id(d)) or ['workroom-shared']
        except OSError:
            pass
    return ['workroom-shared']


def _product_projects_dir(product_id):
    # .passport.json lives in the LIBRARY root, so the versioned release dirs
    # go under library/projects/<id>/ -- findable next to the other archive
    # content, and inside LIBRARY_DIR so the path-traversal guard's base holds.
    return os.path.join(LIBRARY_DIR, 'projects', product_id)


@app.post('/api/intent/product')
@_state_writer
async def intent_product(request: Request):
    """Create a product: {name, summary, spec, ownerId, sandboxId, teamId?,
    contributorIds?, handles?}. The player (session holder) seeds on behalf of
    an acting authority (admin, else senior-most director) -- identical to
    sprint create. Chains product_created into the passport."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    name = (body.get('name') or '').strip()
    if not name:
        return JSONResponse({'error': 'a product name is required'}, status_code=400)
    owner_id = (body.get('ownerId') or '').strip()
    sandbox_id = (body.get('sandboxId') or '').strip()
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    authority = _free_authority(state)
    if not authority:
        return JSONResponse({'error': 'No admin or director is free right now to sponsor a product. Try again shortly.'})
    roster = {d.get('id') for d in (state.get('agentRoster') or [])}
    if owner_id and owner_id not in roster and owner_id != 'player':
        return JSONResponse({'error': f'unknown owner agent: {owner_id}'}, status_code=404)
    valid = _valid_sandbox_ids()
    if sandbox_id not in valid:
        return JSONResponse({'error': f'unknown sandbox ({" ".join(valid)}): {sandbox_id}'}, status_code=400)
    handles = body.get('handles')
    if not isinstance(handles, list):
        handles = []
    import sim as _sim
    product_id = _sim.next_product_id(state)
    record = _sim.create_product(state, product_id, name, body.get('summary'),
                                 body.get('spec'), owner_id or authority['id'],
                                 sandbox_id, body.get('teamId'),
                                 body.get('contributorIds'), handles)
    if record is None:
        return JSONResponse({'error': 'could not create product'}, status_code=409)
    save_state_to_db(state)
    log_action('player', 'product_created', {'id': product_id, 'name': name}, authorized=True)
    _append_passport_decision('product_created', authority['id'],
                              {'id': product_id, 'name': name, 'sandboxId': sandbox_id})
    return JSONResponse({'ok': True, 'product': record})


@app.get('/api/intent/products')
async def get_products(request: Request):
    """Read-only product listing (board modal). Returns the catalog; revision
    CONTENT is never inlined (it lives under library/projects/)."""
    state = get_state_from_db()
    return JSONResponse({'products': (state or {}).get('products') or {}})


@app.post('/api/intent/product/{product_id}/status')
@_state_writer
async def set_product_status(product_id: str, request: Request):
    """Transition draft/in_progress/review. 'released' is unreachable here --
    only /release flips it. Director/admin gated."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    status = (body.get('status') or '').strip()
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    actor = _resolve_requester(request)
    if not actor or not _is_director_or_admin(state, actor):
        return JSONResponse({'error': 'only a director or the chief of staff may set product status'}, status_code=403)
    import sim as _sim
    record = _sim.set_product_status(state, product_id, status)
    if record is None:
        return JSONResponse({'error': f'unknown product or invalid status: {product_id}'}, status_code=404)
    save_state_to_db(state)
    log_action(actor, 'product_status', {'id': product_id, 'status': status}, authorized=True)
    return JSONResponse({'ok': True, 'product': record})


@app.post('/api/intent/product/{product_id}/release')
@_state_writer
async def release_product(product_id: str, request: Request):
    """Release a product: snapshot its sandbox repo into
    library/projects/<id>/v<N>/ (RELEASE.md + a frozen copy), flip status ->
    'released', chain product_released into the passport. The release is run by
    the senior-most free authority; revision content is never returned."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json() or {}
    except Exception:
        body = {}
    note = (body.get('revisionNote') or '').strip()
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    import sim as _sim
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return JSONResponse({'error': f'unknown product: {product_id}'}, status_code=404)
    sandbox_id = record.get('sandboxId')
    src = os.path.join(SANDBOXES_DIR, sandbox_id) if sandbox_id else None
    if not src or not os.path.isdir(src):
        return JSONResponse({'error': f'cannot release: sandbox repo missing ({sandbox_id})'}, status_code=400)
    if record.get('status') == 'released':
        # Re-releases are allowed (a new revision/v), not a hard error.
        pass
    revision_no, _rel = _sim.next_product_revision(state, product_id)
    dest_dir = os.path.join(_product_projects_dir(product_id), f'v{revision_no}')
    os.makedirs(dest_dir, exist_ok=True)
    # Freeze a copy of the sandbox work (code + any working artifacts). The
    # source may be large; copy tree, dropping VCS internals + caches so the
    # release is a clean read-only snapshot.
    _copy_release_snapshot(src, dest_dir)
    _write_file(os.path.join(dest_dir, 'RELEASE.md'),
                f'# {record.get("name")} -- v{revision_no}\n\n'
                f'Released: {datetime.datetime.utcnow().isoformat()}Z\n'
                f'By: {record.get("ownerId") or "unknown"}\n'
                f'Last reviewed: {datetime.datetime.utcnow().date().isoformat()}\n'
                f'Revision: {revision_no}\n'
                f'\n{note}\n' if note else f'# {record.get("name")} -- v{revision_no}\n\n'
                f'Released: {datetime.datetime.utcnow().isoformat()}Z\n'
                f'By: {record.get("ownerId") or "unknown"}\n'
                f'Last reviewed: {datetime.datetime.utcnow().date().isoformat()}\n'
                f'Revision: {revision_no}\n')
    actor = _free_authority(state) or {}
    rev = _sim.product_release_record(state, product_id, revision_no,
                                      actor.get('id') or 'player', note,
                                      target_path=f'v{revision_no}')
    save_state_to_db(state)
    log_action('player', 'product_released', {'id': product_id, 'revision': revision_no}, authorized=True)
    _append_passport_decision('product_released', actor.get('id') or 'player',
                              {'id': product_id, 'revision': revision_no, 'path': f'v{revision_no}'})
    return JSONResponse({'ok': True, 'product': record, 'revision': rev})


def _copy_release_snapshot(src, dest_dir):
    # Copy a sandbox repo into a release dir, dropping .git, caches, and build
    # noise so the frozen snapshot is clean. Never copies onto itself.
    import shutil
    ignore = shutil.ignore_patterns('.git', '__pycache__', '.DS_Store', 'node_modules',
                                    '.cache', '.pytest_cache', 'dist', 'build')
    for entry in os.listdir(src):
        s = os.path.join(src, entry)
        d = os.path.join(dest_dir, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=ignore, dirs_exist_ok=True)
        else:
            try:
                with open(s, 'r', errors='replace') as f:
                    _write_file(d, f.read())
            except OSError:
                pass  # skip unreadable/unstable files (locks, sockets) in the freeze


@app.get('/api/intent/wiki')
async def get_wiki(request: Request):
    """Read-only wiki tree (metadata only -- no bodies). Returns pages grouped
    by category + the category labels/order."""
    state = get_state_from_db() or {}
    wiki = state.get('wiki') or {}
    return JSONResponse({
        'pages': wiki.get('pages') or {},
        'categories': wiki.get('categories') or {},
        'categoryRooms': wiki.get('categoryRooms') or {},
    })


@app.get('/api/intent/wiki/page/{page_id}')
async def get_wiki_page(page_id: str, request: Request):
    """Read one wiki page INCLUDING its body (frozen in library/wiki/<category>/<id>.md)."""
    state = get_state_from_db() or {}
    rec = (state.get('wiki') or {}).get('pages') or {}
    rec = rec.get(page_id)
    if not rec:
        return JSONResponse({'error': f'unknown wiki page: {page_id}'}, status_code=404)
    body = ''
    path = os.path.join(LIBRARY_DIR, 'wiki', rec.get('category') or '', f'{page_id}.md')
    if os.path.isfile(path):
        try:
            with open(path, 'r', errors='replace') as f:
                body = f.read()
        except OSError:
            body = ''
    return JSONResponse({'page': {**rec, 'body': body}})


def _wiki_page_target(category, page_id):
    """Resolve the on-disk target for a wiki page, or None if category/page_id
    would escape library/wiki/ (defense-in-depth on top of the page_id '/\\'
    checks -- a director-created category id must not be able to walk out of the
    wiki tree)."""
    base = os.path.join(LIBRARY_DIR, 'wiki')
    target = os.path.normpath(os.path.join(base, category, f'{page_id}.md'))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


@app.post('/api/intent/wiki/page')
@_state_writer
async def write_wiki_page(request: Request):
    """Create or write a wiki page: {id, title, category, body}. Director/admin
    gated (like template authorship). Version bumps + history append are done in
    sim; the body is written to library/wiki/<category>/<id>.md. Chains
    wiki_page_written into the passport."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    page_id = (body.get('id') or '').strip()
    title = (body.get('title') or '').strip() or page_id
    category = (body.get('category') or '').strip()
    content = body.get('body') or ''
    if not page_id or not category:
        return JSONResponse({'error': 'id and category are required'}, status_code=400)
    if any(ch in page_id for ch in '/\\'):
        return JSONResponse({'error': 'invalid page id'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    categories = (state.get('wiki') or {}).get('categories') or {}
    if category not in categories:
        return JSONResponse({'error': f'unknown category: {category}'}, status_code=400)
    actor = _resolve_requester(request)
    if not actor or not _is_director_or_admin(state, actor):
        return JSONResponse({'error': 'only a director or the chief of staff may write the wiki'}, status_code=403)
    import sim as _sim
    mfields = _sim.resolve_memory_fields(state, body, actor)
    village_id = _sim.village_of_agent(state, actor)
    record, is_new = _sim.wiki_write_page(state, page_id, title, category, content, actor,
                                          village_id=village_id, owner=mfields['owner'],
                                          clock=mfields['clock'], scope=mfields['scope'],
                                          source=mfields['source'],
                                          review_after_ms=mfields['review_after_ms'])
    if record is None:
        return JSONResponse({'error': 'could not write wiki page'}, status_code=400)
    _sim.record_memory_provenance(state, actor, page_id, title, content[:2000],
                                  source=mfields['source'], scope=mfields['scope'],
                                  clock=mfields['clock'],
                                  review_after_ms=mfields['review_after_ms'],
                                  village_id=village_id)
    # Persist the body to disk under library/wiki/<category>/ (inside LIBRARY_DIR).
    target = _wiki_page_target(category, page_id)
    if not target:
        return JSONResponse({'error': 'invalid destination'}, status_code=400)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _write_file(target, content)
    save_state_to_db(state)
    log_action(actor, 'wiki_page_written', {'id': page_id, 'category': category, 'version': record.get('version')}, authorized=True)
    _append_passport_decision('wiki_page_written', actor,
                              {'id': page_id, 'category': category, 'version': record.get('version'), 'isNew': is_new})
    return JSONResponse({'ok': True, 'page': record, 'isNew': is_new})


# --- Wiki agent-propose lane (quarantine) -----------------------------------
# A non-director agent has NO wiki-write rights (director/admin gated above) --
# but the think tank's knowledge layer is exactly where a researcher's findings
# should be able to land. The propose lane gives ANY agent a real path: the
# proposal sits in library/pending_review/wiki/ (the same quarantine that holds
# untrusted downloads and skill candidates), and a director/admin approves it
# into the LIVE wiki (wiki_write_page + disk + passport, exactly like a direct
# write) or rejects it to rejected/wiki/. Until approval the live wiki is
# untouched -- no content can enter the trusted knowledge layer without a
# director reviewing it, the same "promotion is never automatic" discipline as
# library_promote.


@app.post('/api/intent/wiki/propose')
async def propose_wiki_page(request: Request):
    """Agent-propose a wiki page: {id, title?, category, body, summary?}. Any
    authenticated agent may propose; the proposal lands in
    pending_review/wiki/<category>/<id>.json and the LIVE wiki is untouched.
    A director/admin approves or rejects it via the proposal endpoints below."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    actor = _resolve_requester(request)
    if not actor:
        return JSONResponse({'error': 'an agent key is required to propose a wiki page'}, status_code=403)
    page_id = (body.get('id') or '').strip()
    title = (body.get('title') or '').strip() or page_id
    category = (body.get('category') or '').strip()
    content = body.get('body') or ''
    summary = (body.get('summary') or '').strip()
    if not page_id or not category:
        return JSONResponse({'error': 'id and category are required'}, status_code=400)
    if any(ch in page_id for ch in '/\\'):
        return JSONResponse({'error': 'invalid page id'}, status_code=400)
    if not content.strip():
        return JSONResponse({'error': 'body is required'}, status_code=400)
    if len(content) > 200_000:
        return JSONResponse({'error': 'body too large (max 200k)'}, status_code=400)
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    categories = (state.get('wiki') or {}).get('categories') or {}
    if category not in categories:
        return JSONResponse({'error': f'unknown category: {category}'}, status_code=400)
    proposal = {
        'id': page_id, 'title': title, 'category': category,
        'body': content, 'summary': summary,
        'proposedBy': actor, 'proposedAt': int(time.time() * 1000),
    }
    rel_path = f'pending_review/wiki/{category}/{page_id}.json'
    target = _safe_library_path(rel_path)
    if not target:
        return JSONResponse({'error': 'invalid destination'}, status_code=400)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'w') as f:
        json.dump(proposal, f)
    log_action(actor, 'wiki_proposed',
               {'id': page_id, 'category': category, 'path': rel_path}, authorized=True)
    return JSONResponse({'ok': True, 'proposal': {
        'id': page_id, 'title': title, 'category': category,
        'path': rel_path, 'proposedBy': actor}})
