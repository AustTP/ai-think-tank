"""Revisioned approval gate for large requests.

The player proposes a plan, reviews the exact route and budget, then approves
or rejects it. Approval binds the revision hash. Applying an approved plan is
the only path that files, staffs, or backlogs the work.
"""

import core.plan_approvals as plan_approvals


def _planning_authority(state):
    """An admin, else a director, who is not busy. Off-duty is allowed: apply
    wakes her. Unlike _free_authority this does not mutate state."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    for d in roster:
        if d.get('isAdmin'):
            a = agents.get(d.get('id'))
            if a is not None and not a.get('busy'):
                return d
    for d in roster:
        if d.get('isDirector') and not d.get('isAdmin') and not d.get('director'):
            a = agents.get(d.get('id'))
            if a is not None and not a.get('busy'):
                return d
    return None


def _first_authority_id(state):
    roster = state.get('agentRoster') or []
    for d in roster:
        if d.get('isAdmin'):
            return d.get('id')
    for d in roster:
        if d.get('isDirector') and not d.get('isAdmin') and not d.get('director'):
            return d.get('id')
    return None


def _team_by_id(state, team_id):
    for team in state.get('teams') or []:
        if team_id in (team.get('id'), team.get('directorId')):
            return team
    return None


def _propose_route(state, goal, constraints):
    """Return (route, payload, error). Pure: reads state, never mutates it."""
    import sim as _sim
    if _all_teams_busy_in_sprint(state):
        admin_id = _first_authority_id(state)
        if not admin_id:
            return None, None, 'No admin or director is available to plan this request.'
        return 'backlog', {'admin': admin_id}, None
    authority = _planning_authority(state)
    if not authority:
        return None, None, 'No admin or director is free right now -- try again shortly.'
    team = _staffing_team_for_authority(state, authority['id'])
    if not team:
        return None, None, 'No free team is available to take this request right now -- try again shortly.'
    team_id = team.get('id') or team.get('directorId')
    if _sim._team_in_active_sprint(state, team_id):
        return None, None, 'The selected team entered an active sprint -- revise the plan.'
    return 'staffed', {
        'admin': authority['id'],
        'adminName': authority.get('name'),
        'team': team_id,
        'teamName': team.get('name') or team_id,
    }, None


def _validate_budget(constraints):
    budget = (constraints or {}).get('budgetUsd')
    if budget is None:
        return None
    if not isinstance(budget, (int, float)) or budget <= 0:
        raise plan_approvals.PlanError('budgetUsd must be a positive number')
    cap = float(SPEND_CAP_USD or 0)
    if cap and budget > cap:
        raise plan_approvals.PlanError(f'budgetUsd exceeds the ${cap:.2f} tank cap')
    return float(budget)


def _plan_payload(state, route, payload, constraints):
    return {
        **payload,
        'allowedRooms': list(_DELEGATABLE_ROOMS),
        'estimatedBudgetUsd': _validate_budget(constraints),
    }


def _apply_staffed_plan(state, plan, actor):
    import sim as _sim
    payload = plan.get('payload') or {}
    admin_id = payload.get('admin')
    team_id = payload.get('team')
    authority = next((d for d in (state.get('agentRoster') or [])
                      if d.get('id') == admin_id), None)
    agent = (state.get('agents') or {}).get(admin_id)
    if not authority or not agent or agent.get('busy'):
        raise plan_approvals.PlanError('The approved authority is no longer available; revise the plan.', 'stale')
    team = _team_by_id(state, team_id)
    if not team:
        raise plan_approvals.PlanError('The approved team no longer exists; revise the plan.', 'stale')
    if _sim._team_in_active_sprint(state, team.get('id') or team.get('directorId')):
        raise plan_approvals.PlanError('The approved team entered an active sprint; revise the plan.', 'stale')
    if agent.get('offDuty'):
        _sim.appear_from_outskirts(state, admin_id)
    req = _sim.file_large_request(state, admin_id, plan['goal'],
                                  team.get('id') or team.get('directorId'),
                                  int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
                                  constraints=plan.get('constraints') or {})
    if not req:
        raise plan_approvals.PlanError('Could not file the request; revise the plan.', 'apply')
    log_action(actor, 'big_task_plan_applied',
               {'planId': plan['plan_id'], 'version': plan['version'],
                'goal': plan['goal'][:200], 'admin': admin_id,
                'team': team.get('id') or team.get('directorId'),
                'request': req['id']}, authorized=True)
    return {
        'ok': True, 'staffed': True, 'admin': admin_id,
        'team': team.get('id') or team.get('directorId'),
        'teamName': team.get('name') or team.get('id'),
        'request': req['id'],
        'note': f"{authority.get('name') or admin_id} filed this with the "
                f"{team.get('name') or team.get('id')} team.",
    }


async def _apply_backlog_plan(state, plan, actor):
    import sim as _sim
    payload = plan.get('payload') or {}
    admin_id = payload.get('admin')
    if not admin_id:
        raise plan_approvals.PlanError('The plan has no planning authority; revise it.', 'stale')
    backlogged = await asyncio.to_thread(
        _breakdown_into_shared_backlog, state, plan['goal'], admin_id,
        constraints=plan.get('constraints') or {})
    if backlogged:
        log_action(actor, 'big_task_plan_applied',
                   {'planId': plan['plan_id'], 'version': plan['version'],
                    'goal': plan['goal'][:200], 'feature': backlogged['feature'].get('id'),
                    'items': [b.get('id') for b in backlogged['items']]}, authorized=True)
        return {'ok': True, 'backlogged': True,
                'feature': backlogged['feature'].get('id'),
                'featureName': backlogged['feature'].get('name'),
                'items': len(backlogged['items']),
                'note': 'The approved plan was broken down into the shared backlog.'}
    new_team = _sim.spawn_new_team_for_request(
        state, plan['goal'], admin_id=admin_id,
        constraints=plan.get('constraints') or {})
    if new_team:
        log_action(actor, 'big_task_plan_applied',
                   {'planId': plan['plan_id'], 'version': plan['version'],
                    'goal': plan['goal'][:200], 'team': new_team.get('id'),
                    'members': new_team.get('members')}, authorized=True)
        return {'ok': True, 'newTeam': True, 'team': new_team.get('id'),
                'teamName': new_team.get('name'), 'members': new_team.get('members'),
                'note': 'The approved plan created a new team to take the request.'}
    raise plan_approvals.PlanError('Plan application failed; revise the plan.', 'apply')


async def _apply_plan(state, plan, actor):
    if plan['route'] == 'staffed':
        return _apply_staffed_plan(state, plan, actor)
    if plan['route'] == 'backlog':
        return await _apply_backlog_plan(state, plan, actor)
    raise plan_approvals.PlanError(f"unknown plan route {plan['route']!r}", 'route')


def _plan_json(item):
    if item is None:
        return None
    return {
        'planId': item['plan_id'], 'version': item['version'],
        'goal': item['goal'], 'route': item['route'],
        'constraints': item['constraints'], 'payload': item['payload'],
        'hash': item['plan_hash'], 'status': item['status'],
        'createdBy': item['created_by'], 'createdAt': item['created_at'],
        'decidedBy': item['decided_by'], 'decidedAt': item['decided_at'],
        'reason': item['reason'], 'appliedAt': item['applied_at'],
        'expiresAt': item['expires_at'],
    }


@app.post('/api/intent/plan-big-task')
@_state_writer
async def intent_plan_big_task(request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    goal = (body.get('goal') or '').strip()
    if not goal:
        return JSONResponse({'error': 'no goal given'}, status_code=400)
    raw_constraints = body.get('constraints')
    constraints = raw_constraints if isinstance(raw_constraints, dict) else {}
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    route, payload, error = _propose_route(state, goal, constraints)
    if error:
        return JSONResponse({'error': error}, status_code=200)
    try:
        payload = _plan_payload(state, route, payload, constraints)
        plan = plan_approvals.propose(
            goal, route, payload, actor, constraints=constraints)
    except plan_approvals.PlanError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    log_action(actor, 'big_task_plan_proposed',
               {'planId': plan['plan_id'], 'goal': goal[:200],
                'route': route}, authorized=True)
    return JSONResponse({'ok': True, 'plan': _plan_json(plan),
                         'note': 'Plan proposed. Approve it before any work is filed.'})


@app.get('/api/plans')
async def get_plans(request: Request, status: str = 'pending', limit: int = 50):
    authed, _ = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    plans = plan_approvals.list_revisions(status or None, limit)
    return JSONResponse({'plans': [_plan_json(p) for p in plans]})


@app.get('/api/plans/{plan_id}')
async def get_plan(plan_id: str, request: Request):
    authed, _ = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    history = plan_approvals.history(plan_id)
    if not history:
        return JSONResponse({'error': 'plan not found'}, status_code=404)
    return JSONResponse({'planId': plan_id,
                         'revisions': [_plan_json(p) for p in history]})


@app.post('/api/plans/{plan_id}/approve')
@_state_writer
async def approve_plan(plan_id: str, request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    try:
        version = int(body.get('version') or 0)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'version is required'}, status_code=400)
    plan = plan_approvals.get(plan_id, version)
    if not plan:
        return JSONResponse({'error': 'plan not found'}, status_code=404)
    if plan['status'] == 'applied':
        return JSONResponse({'ok': True, 'alreadyApplied': True, 'plan': _plan_json(plan)})
    try:
        if plan['status'] == 'pending':
            plan = plan_approvals.approve(plan_id, version, actor)
        plan = plan_approvals.claim_apply(plan_id, version, actor)
    except plan_approvals.PlanError as exc:
        return JSONResponse({'error': str(exc), 'planStatus': plan.get('status')},
                            status_code=409)
    state = _state_begin()
    if not state:
        plan_approvals.mark_failed(plan_id, version, 'state unavailable')
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        outcome = await _apply_plan(state, plan, actor)
        save_state_to_db(state)
        plan = plan_approvals.mark_applied(plan_id, version, actor)
    except plan_approvals.PlanError as exc:
        plan = plan_approvals.mark_failed(plan_id, version, str(exc))
        return JSONResponse({'error': str(exc), 'plan': _plan_json(plan)},
                            status_code=409 if exc.code == 'stale' else 500)
    return JSONResponse({**outcome, 'plan': _plan_json(plan)})


@app.post('/api/plans/{plan_id}/reject')
@_state_writer
async def reject_plan(plan_id: str, request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    try:
        version = int(body.get('version') or 0)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'version is required'}, status_code=400)
    try:
        plan = plan_approvals.reject(plan_id, version, actor, body.get('reason'))
    except plan_approvals.PlanError as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)
    log_action(actor, 'big_task_plan_rejected',
               {'planId': plan_id, 'version': version,
                'reason': (body.get('reason') or '')[:300]}, authorized=True)
    return JSONResponse({'ok': True, 'plan': _plan_json(plan)})


@app.post('/api/plans/{plan_id}/revise')
@_state_writer
async def revise_plan(plan_id: str, request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    goal = (body.get('goal') or '').strip()
    if not goal:
        return JSONResponse({'error': 'no goal given'}, status_code=400)
    raw_constraints = body.get('constraints')
    constraints = raw_constraints if isinstance(raw_constraints, dict) else {}
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    route, payload, error = _propose_route(state, goal, constraints)
    if error:
        return JSONResponse({'error': error}, status_code=200)
    previous = plan_approvals.get(plan_id)
    if not previous:
        return JSONResponse({'error': 'plan not found'}, status_code=404)
    try:
        payload = _plan_payload(state, route, payload, constraints)
        plan = plan_approvals.revise(plan_id, goal, route, payload, actor,
                                     constraints=constraints)
        diff = plan_approvals.diff(plan_id, previous['version'], plan['version'])
    except plan_approvals.PlanError as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)
    log_action(actor, 'big_task_plan_revised',
               {'planId': plan_id, 'version': plan['version'],
                'previousVersion': plan['version'] - 1}, authorized=True)
    return JSONResponse({'ok': True, 'plan': _plan_json(plan), 'diff': diff})
