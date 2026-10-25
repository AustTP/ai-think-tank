

def _standing_task_from_body(body):
    """Build a schedule.StandingTask from a JSON declaration.
    Returns (task, None), or (None, error) when the declaration
    is missing a field or the schedule contract rejects it."""

    def _opt_float(key):
        value = body.get(key)
        if value is None or value == '':
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f'"{key}" must be a number')

    def _opt_int(key, minimum=None):
        value = body.get(key)
        if value is None or value == '':
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f'"{key}" must be an integer')
        if minimum is not None and number < minimum:
            raise ValueError(f'"{key}" must be >= {minimum}')
        return number

    task_id = (body.get('taskId') or '').strip()
    name = (body.get('name') or '').strip()
    if not task_id:
        return None, '"taskId" is required'
    if not name:
        return None, '"name" is required'
    reset = _opt_float('consecutiveResetAfterHours')
    try:
        task = schedule.StandingTask(
            task_id=task_id,
            name=name,
            mode=(body.get('mode') or schedule.ALWAYS).strip(),
            agent_count=_opt_int('agentCount', 1),
            duration_hours=_opt_float('durationHours'),
            start_time=(body.get('startTime') or None),
            end_time=(body.get('endTime') or None),
            days_of_week=set(body.get('daysOfWeek') or []),
            day_of_month=_opt_int('dayOfMonth'),
            yearly_date=(
                tuple(body['yearlyDate'])
                if body.get('yearlyDate') is not None else None
            ),
            max_occurrences=_opt_int('maxOccurrences', 1),
            max_consecutive_hours=_opt_float('maxConsecutiveHours'),
            max_total_hours=_opt_float('maxTotalHours'),
            consecutive_reset_after=(
                datetime.timedelta(hours=reset)
                if reset is not None else None
            ),
            team=(body.get('team') or None),
        )
    except (TypeError, ValueError) as exc:
        return None, str(exc)
    return task, None


def _signal_plan_from_body(body):
    """Build a durable signal-plan declaration from a JSON body.
    Returns (plan, None), or (None, error). The declaration's
    scenario dicts carry their own armed flag -- the durable home
    of arming -- so a fired scenario stays disarmed across restarts
    unless it asked to rearm."""
    plan_id = (body.get('planId') or '').strip()
    agent_id = (body.get('agentId') or '').strip()
    subject = (body.get('subject') or '').strip()
    if not plan_id:
        return None, '"planId" is required'
    if not agent_id:
        return None, '"agentId" is required'
    if not subject:
        return None, '"subject" is required'
    raw_scenarios = body.get('scenarios')
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        return None, '"scenarios" must be a non-empty list'
    scenarios = []
    for entry in raw_scenarios:
        if not isinstance(entry, dict):
            return None, 'each scenario must be an object'
        scenario_id = (entry.get('scenarioId') or '').strip()
        condition = (entry.get('condition') or '').strip()
        action = (entry.get('action') or '').strip()
        if not scenario_id:
            return None, 'each scenario requires "scenarioId"'
        if not condition or not action:
            return None, 'each scenario requires "condition" and "action"'
        scenarios.append({
            'scenarioId': scenario_id,
            'name': (entry.get('name') or scenario_id).strip(),
            'condition': condition,
            'action': action,
            'exitStrategy': (entry.get('exitStrategy') or '').strip(),
            'armed': bool(entry.get('armed', True)),
            'rearm': bool(entry.get('rearm', False)),
        })
    plan = {
        'planId': plan_id,
        'agentId': agent_id,
        'team': (body.get('team') or '').strip(),
        'subject': subject,
        'task': (body.get('task') or '').strip(),
        'activeWhen': (body.get('activeWhen') or '').strip(),
        'scenarios': scenarios,
    }
    return plan, None


def _agent_requester(request):
    """The agent identity for agent-authenticated routes (the
    container broker): the request must present the X-Agent-Key
    owned by the agent it acts as. The player session is not an
    agent and gets no containers. Returns (agent_id, None), or
    (None, error response)."""
    requester = _resolve_requester(request)
    if not requester or requester == 'player':
        return None, JSONResponse({'error': 'unauthenticated'}, status_code=401)
    if verify_agent_key(requester, request.headers.get('X-Agent-Key')) is not True:
        return None, JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    return requester, None


@app.post('/api/standing-tasks')
@_state_writer
async def standing_tasks_create(request: Request):
    # Operator-gated: a standing task spins agents on a
    # schedule, the same class of commitment as starting
    # a VPN exit.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'}, status_code=400)
    # Script-backed standing tasks run on the HOST with the full environment
    # and network. They are operator-authored by definition: require the real
    # player session, not just a verified agent key, so no agent can schedule
    # a script around the sandbox egress allowlist.
    if (body.get('script') or '').strip() and not _require_player_session(request):
        return JSONResponse({'error': 'script-backed standing tasks are player/operator only'},
                            status_code=403)
    task, error = _standing_task_from_body(body)
    if task is None:
        return JSONResponse({'error': error}, status_code=400)
    state = get_state_from_db() or {}
    declarations = state.setdefault('standingTasks', [])
    if any(
        isinstance(declaration, dict) and declaration.get('taskId') == task.task_id
        for declaration in declarations
    ):
        return JSONResponse({'error': f'duplicate taskId "{task.task_id}"'}, status_code=409)
    declaration = _public_dict(dataclasses.asdict(task))
    room = (body.get('room') or '').strip()
    if room:
        # The scheduler reads the room off the declaration
        # when it queues the task's work cards.
        declaration['room'] = room
    fields, error = cron_scripts.declaration_fields(body)
    if error:
        return JSONResponse({'error': error}, status_code=400)
    declaration.update(fields)
    declarations.append(declaration)
    save_state_to_db(state)
    log_action(actor, 'standing_task_declared',
               {'taskId': task.task_id, 'mode': task.mode}, authorized=True)
    return JSONResponse({'ok': True, 'task': declaration})


@app.get('/api/standing-tasks')
async def standing_tasks_list(request: Request):
    state = get_state_from_db() or {}
    declarations = [
        declaration for declaration in (state.get('standingTasks') or [])
        if isinstance(declaration, dict)
    ]
    import sim as _sim
    states = getattr(getattr(_sim, '_STANDING_SCHEDULER', None), 'states', {}) or {}
    tasks = []
    for declaration in declarations:
        entry = dict(declaration)
        runtime = states.get(declaration.get('taskId'))
        entry['scheduler'] = (
            _public_dict(vars(runtime)) if runtime is not None else None
        )
        tasks.append(entry)
    return JSONResponse({'tasks': tasks})


@app.delete('/api/standing-tasks/{task_id}')
@_state_writer
async def standing_tasks_delete(task_id: str, request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db() or {}
    declarations = state.get('standingTasks') or []
    remaining = [
        declaration for declaration in declarations
        if not (isinstance(declaration, dict)
                and declaration.get('taskId') == task_id)
    ]
    if len(remaining) == len(declarations):
        return JSONResponse({'error': f'no standing task "{task_id}"'}, status_code=404)
    state['standingTasks'] = remaining
    import sim as _sim
    scheduler = getattr(_sim, '_STANDING_SCHEDULER', None)
    if scheduler is not None:
        # Drop the task's runtime bookkeeping with the
        # declaration: occurrences, hours, and anchor state
        # belong to the declaration, not to the id.
        scheduler.states.pop(task_id, None)
    save_state_to_db(state)
    log_action(actor, 'standing_task_removed', {'taskId': task_id}, authorized=True)
    return JSONResponse({'ok': True, 'removed': task_id})


@app.post('/api/signal-plans')
@_state_writer
async def signal_plans_create(request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'}, status_code=400)
    plan, error = _signal_plan_from_body(body)
    if plan is None:
        return JSONResponse({'error': error}, status_code=400)
    state = get_state_from_db() or {}
    plans = state.setdefault('signalPlans', [])
    if any(
        isinstance(declaration, dict) and declaration.get('planId') == plan['planId']
        for declaration in plans
    ):
        return JSONResponse({'error': f'duplicate planId "{plan["planId"]}"'}, status_code=409)
    plans.append(plan)
    save_state_to_db(state)
    log_action(actor, 'signal_plan_declared',
               {'planId': plan['planId'], 'subject': plan['subject']}, authorized=True)
    return JSONResponse({'ok': True, 'plan': plan})


@app.get('/api/signal-plans')
async def signal_plans_list(request: Request):
    state = get_state_from_db() or {}
    plans = [
        declaration for declaration in (state.get('signalPlans') or [])
        if isinstance(declaration, dict)
    ]
    return JSONResponse({'plans': plans})


@app.delete('/api/signal-plans/{plan_id}')
@_state_writer
async def signal_plans_delete(plan_id: str, request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db() or {}
    plans = state.get('signalPlans') or []
    remaining = [
        declaration for declaration in plans
        if not (isinstance(declaration, dict)
                and declaration.get('planId') == plan_id)
    ]
    if len(remaining) == len(plans):
        return JSONResponse({'error': f'no signal plan "{plan_id}"'}, status_code=404)
    state['signalPlans'] = remaining
    save_state_to_db(state)
    log_action(actor, 'signal_plan_removed', {'planId': plan_id}, authorized=True)
    return JSONResponse({'ok': True, 'removed': plan_id})


@app.post('/api/signals/emit')
@_state_writer
async def signals_emit(request: Request):
    # Operator-gated: an emission wakes agents, and a
    # misdirected emission wakes the wrong ones.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'}, status_code=400)
    subject = (body.get('subject') or '').strip()
    if not subject:
        return JSONResponse({'error': '"subject" is required'}, status_code=400)
    if body.get('data') is not None:
        data = body.get('data')
        if not isinstance(data, str):
            return JSONResponse({'error': '"data" must be a string'}, status_code=400)
    else:
        task = body.get('task')
        if isinstance(task, dict):
            import sim as _sim
            data = _sim.task_completed_data(task)
        elif task is None or task == '':
            data = ''
        else:
            data = str(task)
    state = get_state_from_db() or {}
    import sim as _sim
    events = _sim.emit_signals(state, subject, data)
    # emit_signals disarms fired scenarios on the plan objects
    # it rebuilt from state and writes them back; persisting
    # the state here is what makes the disarm durable.
    save_state_to_db(state)
    log_action(actor, 'signal_emitted',
               {'subject': subject, 'events': len(events)}, authorized=True)
    return JSONResponse({
        'ok': True,
        'subject': subject,
        'events': [_public_dict(dataclasses.asdict(event))
                   for event in events],
    })


@app.post('/api/supervisor/meeting')
@_state_writer
async def supervisor_meeting(request: Request):
    # Operator-gated: the meeting spends budget lines and
    # commits the village to stories.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'}, status_code=400)
    title = (body.get('title') or '').strip()
    goal = (body.get('goal') or '').strip()
    items = [str(item).strip() for item in (body.get('items') or []) if str(item).strip()]
    specialties = body.get('specialties')
    if not title or not goal:
        return JSONResponse({'error': '"title" and "goal" are required'}, status_code=400)
    if not items:
        return JSONResponse({'error': '"items" must be a non-empty list'}, status_code=400)
    if not isinstance(specialties, dict) or not specialties:
        return JSONResponse({'error': '"specialties" must be a non-empty object'}, status_code=400)
    durations = {}
    raw_durations = body.get('durations')
    if isinstance(raw_durations, dict):
        for key, value in raw_durations.items():
            try:
                durations[str(key)] = float(value)
            except (TypeError, ValueError):
                return JSONResponse({'error': f'"durations.{key}" must be a number'}, status_code=400)
    try:
        stories_per_agent = max(1, int(body.get('storiesPerAgent') or 1))
        batch_size = max(1, int(body.get('batchSize') or planner.DEFAULT_BATCH_SIZE))
    except (TypeError, ValueError):
        return JSONResponse({'error': '"storiesPerAgent" and "batchSize" must be integers'}, status_code=400)
    state = get_state_from_db() or {}
    import sim as _sim
    view = _sim.roster_view_from_state(state)
    if view is None:
        return JSONResponse(
            {'error': 'the roster is empty; add agents before a supervisor meeting'},
            status_code=409,
        )
    ledger = story_budget.BudgetLedger.from_state(state.get('budgetLedger')) or story_budget.BudgetLedger()
    meeting = planner.SupervisorMeeting(
        view,
        specialties,
        stories_per_agent=stories_per_agent,
        batch_size=batch_size,
        ledger=ledger,
    )
    brief, stories = meeting.breakdown(
        planner.PlannerSubmission(title=title, goal=goal, items=items, durations=durations)
    )
    assignments = meeting.assign(stories)
    # The meeting's budget lines and its overrun history are
    # durable: the next meeting scales by them.
    state['budgetLedger'] = ledger.to_state()
    state.setdefault('meetings', []).append({
        'title': title,
        'goal': goal,
        'decidedBy': actor,
        'brief': _public_dict(dataclasses.asdict(brief)),
        'stories': [_public_dict(dataclasses.asdict(story))
                    for story in stories],
        'assignments': [_public_dict(dataclasses.asdict(assignment))
                        for assignment in assignments],
    })
    save_state_to_db(state)
    log_action(actor, 'supervisor_meeting',
               {'title': title, 'stories': len(stories)}, authorized=True)
    return JSONResponse({
        'ok': True,
        'brief': _public_dict(dataclasses.asdict(brief)),
        'stories': [_public_dict(dataclasses.asdict(story))
                    for story in stories],
        'assignments': [_public_dict(dataclasses.asdict(assignment))
                        for assignment in assignments],
    })


@app.get('/api/budget/story')
async def budget_overview(request: Request):
    state = get_state_from_db() or {}
    ledger = story_budget.BudgetLedger.from_state(state.get('budgetLedger'))
    if ledger is None:
        return JSONResponse({'alerts': [], 'reviews': [], 'overrunFactor': 0.0, 'lines': []})
    return JSONResponse({
        'alerts': [_public_dict(dataclasses.asdict(alert))
                   for alert in ledger.alerts()],
        'reviews': [_public_dict(dataclasses.asdict(review))
                    for review in ledger.reviews()],
        'overrunFactor': ledger.overrun_factor(),
        'lines': [_public_dict(dataclasses.asdict(line))
                  for line in ledger.lines()],
    })


@app.post('/api/budget/review')
@_state_writer
async def budget_review(request: Request):
    # The director's review of an over-budget story: grant
    # more minutes, or stop the line. Operator-gated because
    # it spends the village's budget.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'}, status_code=400)
    story_id = body.get('storyId')
    reason = (body.get('reason') or '').strip()
    decision = (body.get('decision') or '').strip()
    if story_id is None or story_id == '':
        return JSONResponse({'error': '"storyId" is required'}, status_code=400)
    if decision not in ('grant', 'stop'):
        return JSONResponse({'error': '"decision" must be "grant" or "stop"'}, status_code=400)
    additional_minutes = 0.0
    if decision == 'grant':
        raw_additional = body.get('additionalMinutes')
        try:
            additional_minutes = float(raw_additional or 0)
        except (TypeError, ValueError):
            return JSONResponse({'error': '"additionalMinutes" must be a positive number'}, status_code=400)
        if additional_minutes <= 0:
            return JSONResponse({'error': '"additionalMinutes" must be a positive number'}, status_code=400)
    state = get_state_from_db() or {}
    ledger = story_budget.BudgetLedger.from_state(state.get('budgetLedger'))
    if ledger is None:
        return JSONResponse({'error': 'no budget ledger has been opened yet'}, status_code=409)
    review = ledger.review(story_id, reason, decision, additional_minutes)
    if review is None:
        return JSONResponse({'error': f'no budget line for story "{story_id}"'}, status_code=404)
    state['budgetLedger'] = ledger.to_state()
    save_state_to_db(state)
    log_action(actor, 'budget_reviewed',
               {'storyId': story_id, 'decision': decision}, authorized=True)
    line = ledger.line(story_id)
    return JSONResponse({
        'ok': True,
        'review': _public_dict(dataclasses.asdict(review)),
        'line': _public_dict(dataclasses.asdict(line)) if line is not None else None,
        'overrunFactor': ledger.overrun_factor(),
    })


@app.get('/api/containers')
async def containers_list(request: Request):
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    mine = _CONTAINER_BROKER.describe(agent_id)
    return JSONResponse({
        'containers': [dataclasses.asdict(container) for container in mine],
        'running': _CONTAINER_BROKER.running(),
        'runningForMe': _CONTAINER_BROKER.running_for(agent_id),
    })


@app.post('/api/containers')
async def containers_create(request: Request):
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'}, status_code=400)
    image = (body.get('image') or '').strip()
    if not image:
        return JSONResponse({'error': '"image" is required'}, status_code=400)
    egress = body.get('egress')
    if egress is not None and egress != '' and not isinstance(egress, str):
        return JSONResponse({'error': '"egress" must be a string'}, status_code=400)
    result = _CONTAINER_BROKER.provision(agent_id, image, egress=(egress or None))
    if not result.ok:
        # Broker refusals (cap, unapproved image, bad egress
        # profile) are caller-fixable policy decisions.
        return JSONResponse({'error': result.reason}, status_code=409)
    log_action(agent_id, 'container_provisioned',
               {'containerId': result.container_id, 'image': image}, authorized=True)
    return JSONResponse({
        'ok': True,
        'container': {
            'containerId': result.container_id,
            'agentId': agent_id,
            'image': image,
            'egress': egress or None,
        },
        'running': _CONTAINER_BROKER.running(),
        'runningForMe': _CONTAINER_BROKER.running_for(agent_id),
    })


@app.post('/api/containers/{container_id}/shutdown')
async def containers_shutdown(container_id: str, request: Request):
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    if not _CONTAINER_BROKER.shutdown(agent_id, container_id):
        return JSONResponse(
            {'error': 'unknown or foreign container, or already stopped'},
            status_code=409,
        )
    log_action(agent_id, 'container_shutdown', {'containerId': container_id}, authorized=True)
    return JSONResponse({'ok': True, 'containerId': container_id})


@app.post('/api/containers/{container_id}/restart')
async def containers_restart(container_id: str, request: Request):
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    result = _CONTAINER_BROKER.restart(agent_id, container_id)
    if not result.ok:
        return JSONResponse({'error': result.reason}, status_code=409)
    log_action(agent_id, 'container_restarted', {'containerId': container_id}, authorized=True)
    return JSONResponse({'ok': True, 'containerId': result.container_id})


@app.post('/api/containers/{container_id}/switch')
async def containers_switch(container_id: str, request: Request):
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    if not _CONTAINER_BROKER.switch(agent_id, container_id):
        return JSONResponse(
            {'error': 'unknown or foreign container, or no exit to switch to'},
            status_code=409,
        )
    log_action(agent_id, 'container_switched', {'containerId': container_id}, authorized=True)
    return JSONResponse({'ok': True, 'containerId': container_id})


@app.post('/api/browse')
async def browse(request: Request):
    # Real internet access for agents: accept
    # the residual risk of open-web + a Jev gate over a hard allowlist --
    # but classify BEFORE fetching, not after: a page's content never
    # lands on this machine unless Jev already approved the destination
    # and stated purpose. Every request is logged to think_tank.db regardless
    # of outcome, and the whole endpoint can be shut off in one place via
    # AGENT_BROWSING_ENABLED in .env.
    if not BROWSING_ENABLED:
        return JSONResponse({'error': 'Agent browsing is disabled (AGENT_BROWSING_ENABLED=false in .env)'}, status_code=403)
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)
    # Monthly page-request budget: a count-based quota on external
    # fetches, NOT a dollar cap. When the month's allowance is spent, browsing
    # refuses rather than silently running over the 1000 free requests.
    if _page_budget_exhausted():
        return JSONResponse({'allowed': False, 'reason': 'The think tank has used its monthly page-request budget. Browsing is paused until next month (or raise PAGE_REQUEST_MONTHLY_BUDGET in .env).'})

    body = await request.json()
    url = (body.get('url') or '').strip()
    agent_id = body.get('agentId', 'unknown')
    purpose = (body.get('purpose') or '').strip()
    # Work-context-aware gating: the player-filed task the agent is currently
    # executing (threaded by the executors). A host the player EXPLICITLY
    # named in that work bypasses Jev like the allowlist -- the player vetted
    # it by writing it into the assignment. Every other URL is judged with the
    # work context IN the Jev prompt, so on-task sites are allowed instead of
    # blocked for being unfamiliar.
    work_context = (body.get('workContext') or '').strip()
    # Player-authored provenance (threaded by the executors from the task's
    # playerAuthored flag): a host the player EXPLICITLY named in a task the
    # PLAYER wrote bypasses Jev like the allowlist -- the player vetted it by
    # writing it into the assignment. An agent-authored story's work text is
    # NEVER trusted for this bypass (the agent wrote the text itself), so its
    # named hosts always go through the story-aware Jev prompt.
    work_context_trusted = _work_trust_from_body(body)
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'browse'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    if not url:
        return JSONResponse({'error': 'url is required'}, status_code=400)

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        log_action(agent_id, 'browse', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': 'invalid or non-http(s) scheme'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'Only http/https URLs are allowed.'})
    if not _is_safe_public_host(parsed.hostname):
        log_action(agent_id, 'browse', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': 'private/internal host'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'That address resolves to a private or internal network location and cannot be reached.'})

    # trace_id is only produced by the Jev gate branch below; the allowlist
    # path has no decision to trace, so default to None before the branch
    # (the outcome rows after it are threaded with whichever value applied).
    trace_id = None
    if _is_allowlisted_host(parsed.hostname) or _is_standing_safe_host(parsed.hostname) or (work_context_trusted and _is_work_context_host(work_context, parsed.hostname)):
        # Player-vetted domain (allowlist, or a host the player EXPLICITLY
        # named in THIS task's work, and the task is player-authored) -- skip
        # the Jev classify+escalate round trip entirely (gap: the SAME url got
        # a low-confidence 'escalated_unsure' from Jev on one run and a clean
        # allow on another, purely from classifier variance on a site the
        # player had already decided was fine). Standing safe domains join
        # this skip on READ paths only (curated public reference; see
        # STANDING_SAFE_DOMAINS) -- the egress proxy/Colab gates never see
        # them. SSRF/private-network protection
        # above is NOT skipped -- this only replaces the content/purpose
        # judgment call, never the network-safety one. An agent-authored
        # story's own text is never trusted for this bypass: the agent wrote
        # that text, so it must not be able to launder a site by naming it in
        # its own story.
        log_action(agent_id, 'browse', {'url': url, 'purpose': purpose, 'decision': 'allowed_by_allowlist', 'workContext': bool(work_context)}, authorized=authorized)
    else:
        criteria = {
            'allow': 'The URL and stated purpose look like ordinary, legal browsing (reference material, news, weather, general research, public information).'
                     + (' A URL that is clearly part of, or relevant to, the agent\'s CURRENT assigned work is allowed, provided it does not fall into any blocked category.' if work_context else ''),
            'block': 'The URL, domain, or stated purpose suggests: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '. A blocked category ALWAYS blocks: relevance to the assigned work or a stated purpose never overrides it -- an agent can describe its work however it likes, and that must not launder an inappropriate site.',
        }
        instructions = f'An in-game agent wants to visit this URL: {url}\nStated reason: {purpose or "not given"}\n'
        if work_context:
            instructions += f'The agent is currently working on this assigned task: {work_context[:600]}\n'
        instructions += 'Decide allow or block based on the URL/domain, the stated purpose, and the work the URL serves (the page has not been fetched yet). If the URL falls into any blocked category, you MUST block it regardless of work relevance or stated purpose.'
        decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria)

        if not _jev_safety_gate(agent_id, 'browse', 'This page', url, purpose, decision, confidence, cost, authorized, trace_id):
            return JSONResponse({'allowed': False, 'reason': 'This site was not approved for a think tank agent to visit.'})
        # Real trail evidence: a confident allow (never an escalated/unsure
        # one) on a domain not already vetted -- see record_browse_success.
        record_browse_success(parsed.hostname)

    # robots.txt is a crawler's contract, not an interactive browser's. Apply
    # it only when the caller says this page is part of a crawl (crawlAndCollect
    # in world.js sets `crawl: true`), and refuse the page instead of quietly
    # walking a site the operator asked not to be walked. Cached per host.
    robots_info = None
    if body.get('crawl'):
        robots_allowed, robots_reason = await asyncio.to_thread(_robots_allows, url)
        robots_info = {'allowed': robots_allowed, 'reason': robots_reason}
        if not robots_allowed:
            log_action(agent_id, 'browse', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': robots_reason, 'crawl': True}, authorized=authorized, trace_id=trace_id)
            return JSONResponse({'allowed': False, 'reason': f'robots.txt: {robots_reason}', 'robots': robots_info})

    # Real fix for a real, confirmed gap: plain urllib (_fetch_page_sync)
    # only ever sees a page's INITIAL HTML. For a site that renders its
    # actual content client-side via JS/AJAX -- Reddit, Twitter/X, and any
    # modern single-page app -- that's a near-empty shell, not the real
    # content. `render: true` switches to a real headless-Chrome fetch
    # (the same Playwright already installed for page-probe/screenshots)
    # that waits for the page to actually run its JS before reading the
    # text back out. Same Jev-approved URL either way -- this is a
    # different WAY to read an already-cleared page, not a second,
    # unvetted path to one.
    render = bool(body.get('render'))
    # Optional real VPN exit for geo-restricted content. This
    # only replaces WHICH network path the fetch below takes -- the URL has
    # already been through the exact same SSRF check and Jev/allowlist gate
    # above either way; a country never grants a second, unvetted path to a
    # URL Jev hasn't already cleared.
    via_vpn_country = (body.get('viaVpnCountry') or '').strip().lower()
    via_vpn_exit = (body.get('viaVpnExit') or '').strip()
    if via_vpn_country and via_vpn_exit:
        return JSONResponse({'allowed': False, 'reason': 'Set either viaVpnCountry (the host Mullvad tunnel) or viaVpnExit (a sandbox VPN container) -- not both.'})
    if via_vpn_exit:
        exit_registry = _vpn_exit_registry()
        if via_vpn_exit not in exit_registry:
            available = ', '.join(sorted(exit_registry)) or '(none configured -- set VPN_EXIT_PROXIES in .env)'
            return JSONResponse({'allowed': False, 'reason': f'unknown VPN exit "{via_vpn_exit}". Configured exits: {available}. GET /api/vpn-exits lists each one with when-to-use guidance; an operator can start another with POST /api/vpn-exits/start.'})
    elif via_vpn_country:
        if not MULLVAD_BIN:
            return JSONResponse({'allowed': False, 'reason': 'Mullvad is not installed on this host -- viaVpnCountry is unavailable.'})
        if via_vpn_country not in MULLVAD_COUNTRY_ALLOWLIST:
            return JSONResponse({'allowed': False, 'reason': f'"{via_vpn_country}" is not on MULLVAD_COUNTRY_ALLOWLIST in .env.'})

    async def _do_fetch(proxy=None):
        if render:
            if sync_playwright is None:
                raise RuntimeError('playwright is not installed on this machine')
            final_url, text, links = await asyncio.to_thread(_fetch_rendered_page_sync, url, proxy=proxy)
            content_type = 'text/html (rendered)'
            text = text[:20000]
            truncated = len(text) >= 20000
            raw_body = text  # only used below for a byte-length log field
            last_modified = None  # a headless render has no raw HTTP response header to read this from
        else:
            final_url, content_type, raw_body, truncated, last_modified = await asyncio.to_thread(_fetch_page_sync, url, proxy=proxy)
            is_html = 'html' in content_type
            text = _strip_html_to_text(raw_body) if is_html else raw_body[:BROWSE_MAX_BYTES]
            links = _extract_links(raw_body, final_url) if is_html else []
            text = text[:20000]
        return final_url, content_type, raw_body, truncated, last_modified, text, links

    try:
        if via_vpn_exit:
            # Per-container exit: only THIS fetch is rerouted, through the
            # named sandbox container's own WireGuard tunnel. No host-wide
            # route change and no _MULLVAD_LOCK serialization -- the tunnel
            # is the container's, not this machine's, so a second caller
            # through any exit (or no exit at all) is never touched.
            proxy = exit_registry[via_vpn_exit]['proxy']
            final_url, content_type, raw_body, truncated, last_modified, text, links = await _do_fetch(proxy=proxy)
        elif via_vpn_country:
            # _MULLVAD_LOCK is real, host-wide serialization -- `mullvad
            # connect` changes the WHOLE MACHINE's default route, so two
            # overlapping callers could otherwise disconnect/reconnect out
            # from under each other. Held across connect+fetch+disconnect,
            # never released early, so a second caller simply waits its
            # turn rather than racing this one.
            async with _MULLVAD_LOCK:
                vpn_ok, vpn_error = await asyncio.to_thread(_mullvad_connect_sync, via_vpn_country)
                try:
                    if not vpn_ok:
                        log_action(agent_id, 'browse', {'url': url, 'purpose': purpose, 'decision': 'allowed_but_vpn_failed', 'reason': vpn_error, 'viaVpnCountry': via_vpn_country}, authorized=authorized, trace_id=trace_id)
                        return JSONResponse({'allowed': True, 'error': f'Approved, but could not connect via Mullvad ({via_vpn_country}): {vpn_error}'})
                    final_url, content_type, raw_body, truncated, last_modified, text, links = await _do_fetch()
                finally:
                    # Always torn down, even on a failed/timed-out connect
                    # attempt (the `connect` command may already have been
                    # issued) or a fetch exception -- a return inside this
                    # try still runs this finally, so no exit path skips it.
                    await asyncio.to_thread(_mullvad_disconnect_sync)
        else:
            final_url, content_type, raw_body, truncated, last_modified, text, links = await _do_fetch()
    except Exception as e:
        log_action(agent_id, 'browse', {'url': url, 'purpose': purpose, 'decision': 'allowed_but_fetch_failed', 'reason': str(e), 'render': render, 'viaVpnCountry': via_vpn_country or None, 'viaVpnExit': via_vpn_exit or None}, authorized=authorized, trace_id=trace_id)
        return JSONResponse({'allowed': True, 'error': f'Approved, but the page could not be loaded: {e}'})

    # Real gap closure: the redirect chain's FINAL host was network-re-checked
    # inside the fetch, but never re-judged for CONTENT. Jev approved the
    # ORIGINAL url (a short link can point anywhere); the destination it
    # actually landed on is a host Jev never classified. Re-gate it now,
    # before a byte of its content is wrapped for the agent.
    redirect_allowed, redirect_reason = await _jev_re_gate_redirect_final(
        agent_id, 'browse', 'This page', url, final_url, purpose,
        work_context, work_context_trusted, authorized, trace_id)
    if not redirect_allowed:
        log_action(agent_id, 'browse', {'url': url, 'finalUrl': final_url, 'purpose': purpose,
                                        'decision': 'blocked', 'reason': redirect_reason,
                                        'render': render, 'viaVpnCountry': via_vpn_country or None, 'viaVpnExit': via_vpn_exit or None},
                   authorized=authorized, trace_id=trace_id)
        return JSONResponse({'allowed': False, 'reason': redirect_reason})

    # `text` stays plain for human display (the Weather Station/Work Room
    # modals render this directly) -- `textForModel` is the boundary-
    # wrapped version any FUTURE code path must use instead if it ever
    # feeds this content into an agent's own chat call. Nothing does yet,
    # but this exists so that when something does, it's protected by
    # default rather than by whoever remembers to wrap it.
    wrapped, nonce, tag, model_instruction = wrap_external_content(text, source_label=f'a page at {final_url}')

    # Real UI/visual research: text extraction alone
    # throws away the actual design of a page -- exactly the kind of thing
    # a UI/UX researcher needs to SEE (layouts, note-highway designs,
    # visual style), not read a stripped-text summary of. Reuses the
    # SAME already-Jev-approved URL and SSRF-checked host as the text
    # fetch above -- this never gives an agent a second, unvetted path to
    # a URL Jev hasn't already cleared. Unlike the sandbox screenshot
    # endpoint (network-blackholed, since that renders an agent's OWN
    # code), this one intentionally allows real network access -- it's
    # rendering a real external page, which needs its own real assets to
    # look like anything.
    image_b64 = None
    if body.get('visual') and _find_chrome():
        try:
            image_b64 = await asyncio.to_thread(_screenshot_url_sync, final_url)
        except Exception:
            image_b64 = None  # visual capture is best-effort -- text results above still stand either way

    log_action(agent_id, 'browse', {'url': url, 'finalUrl': final_url, 'purpose': purpose, 'decision': 'allowed', 'contentType': content_type, 'bytes': len(raw_body), 'visual': bool(image_b64), 'render': render, 'viaVpnCountry': via_vpn_country or None, 'viaVpnExit': via_vpn_exit or None}, authorized=authorized, trace_id=trace_id)
    # One real external fetch happened -- count it against the monthly page-
    # request budget. Best-effort accounting, never blocks the
    # response (the pre-fetch check already refused if the budget was spent).
    _accrue_page_request()
    return JSONResponse({
        'allowed': True, 'url': final_url, 'text': text, 'links': links, 'truncated': truncated,
        'robots': robots_info,
        'textForModel': wrapped, 'modelInstruction': model_instruction,
        'imageBase64': image_b64,
        # Real, if imperfect, "did this actually change" signal for
        # date-aware incremental research (crawlAndCollect, world.js) --
        # epoch ms, or null when the response never set one (common) or
        # this was a headless render instead of a raw fetch.
        'lastModified': last_modified,
    })


# ---------------------------------------------------------------------------
# Research Desk evidence + coverage endpoints (world/evidence.py). The
# scheduled research executor POSTs each candidate source here so every claim
# is recorded with its RESOLVED identity (a short link's raw string is never
# the identity -- see evidence.source_identity), and the operator/Verifier
# reads and clears records through these same endpoints. All paths are
# session/agent-key protected by the deny-by-default auth middleware.
# ---------------------------------------------------------------------------
@app.post('/api/evidence')
async def evidence_record(request: Request):
    # Record one claim into the evidence ledger. The server applies source
    # identity (resolved landing URL), the coverage gate (skip already-covered
    # identities without a material update), and the repost rule (a second
    # record on the same identity is independent=False). This is the one call
    # the research crawl makes per candidate -- it returns the decision so the
    # crawl can skip a covered source before saving a duplicate page.
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    record = body.get('record') or {}
    if not isinstance(record, dict):
        return JSONResponse({'error': 'record must be an object'}, status_code=400)
    try:
        result = record_source_claim(record, actor=agent_id)
    except Exception as e:
        return JSONResponse({'error': f'could not record claim: {e}'}, status_code=500)
    if result.get('recorded'):
        log_action(agent_id, 'evidence_record', {
            'claim_id': result.get('id'),
            'independent': result.get('independent'),
            'repost_of': result.get('repost_of'),
            'is_short_link': result.get('is_short_link'),
            'resolved_ok': result.get('resolved_ok'),
            'source_url': record.get('source_url'),
        })
    return JSONResponse(result)


@app.post('/api/evidence/observation')
async def evidence_observation(request: Request):
    # The change-record primitive from the research-desk model: a structured
    # before -> after observation (pricing, positioning, feature availability)
    # appended to a claim, or turned into a new claim when there is none. Same
    # auth/rate-limit surface as POST /api/evidence.
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        result = record_observation_change(
            body.get('claimId'),
            body.get('field'),
            body.get('before'),
            body.get('after'),
            source_url=body.get('sourceUrl'),
            note=body.get('note'),
            actor=agent_id,
        )
    except Exception as e:
        return JSONResponse({'error': f'could not record observation: {e}'}, status_code=500)
    if result.get('recorded'):
        log_action(agent_id, 'evidence_observation', {
            'claim_id': result.get('id'),
            'field': body.get('field'),
            'before': body.get('before'),
            'after': body.get('after'),
        })
    return JSONResponse(result)
