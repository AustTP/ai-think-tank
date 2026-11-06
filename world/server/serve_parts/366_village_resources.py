import time

import core.resources as _resources


def _village_resource_usage(village_id):
    usage = _resources.GOVERNOR.snapshot()
    village = usage.get('villages', {}).get(
        village_id, {'total': 0, 'heavy': 0,
                     'classes': {name: 0 for name in _resources.CLASS_NAMES}})
    return {'global': usage.get('global', {}), 'village': village}


def _village_resource_view(state, village_id):
    policy = _resources.village_policy(state, village_id)
    return {
        'policy': policy,
        'defaults': dict(_resources.VILLAGE_DEFAULTS),
        'limits': {name: dict(cfg) for name, cfg in _resources.CLASS_LIMITS.items()},
        'usage': _village_resource_usage(village_id),
    }


def _can_manage_village_resources(state, actor, village_id):
    if actor == 'player' or _is_admin(state, actor):
        return True
    if not _is_director_or_admin(state, actor):
        return False
    import sim as _sim
    return _sim.village_of_agent(state, actor) == village_id


@app.get('/api/villages/{village_id}/resources')
async def get_village_resources(village_id: str, request: Request):
    """The village's resource share and live usage. Directors use this to see
    what is running before they lower a limit or hold heavy work."""
    actor = _resolve_requester(request)
    if not actor and not _require_player_session(request):
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    if actor == 'player':
        actor = 'player'
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _can_manage_village_resources(state, actor or 'player', village_id):
        return JSONResponse({'error': 'only this village\'s director or an admin may read its resource policy'}, status_code=403)
    return JSONResponse({'villageId': village_id,
                         **_village_resource_view(state, village_id)})


@app.post('/api/villages/{village_id}/resources')
@_state_writer
async def set_village_resources(village_id: str, request: Request):
    """Set a village's share of the laptop. Body:
    {maxConcurrent, maxHeavy, hold, reason}. `hold` is a list of resource
    classes or ['heavy']; a held class is refused until cleared."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _can_manage_village_resources(state, actor, village_id):
        return JSONResponse({'error': 'only this village\'s director or an admin may change its resource policy'}, status_code=403)
    body = await request.json()
    village = next((v for v in (state.get('villages') or [])
                    if v.get('id') == village_id), None)
    if not village:
        return JSONResponse({'error': 'unknown village'}, status_code=404)
    current = _resources.village_policy(state, village_id)
    try:
        max_concurrent = int(body.get('maxConcurrent', current['maxConcurrent']))
    except (TypeError, ValueError):
        return JSONResponse({'error': 'maxConcurrent must be an integer'}, status_code=400)
    try:
        max_heavy = int(body.get('maxHeavy', current['maxHeavy']))
    except (TypeError, ValueError):
        return JSONResponse({'error': 'maxHeavy must be an integer'}, status_code=400)
    ceiling = max(int(cfg.get('maxConcurrent') or 1)
                  for cfg in _resources.CLASS_LIMITS.values())
    heavy_ceiling = sum(1 for name in _resources.HEAVY_CLASSES
                        if int(_resources.CLASS_LIMITS[name].get('maxConcurrent') or 0) > 0)
    if max_concurrent < 1 or max_concurrent > ceiling:
        return JSONResponse({'error': f'maxConcurrent must be 1..{ceiling}'}, status_code=400)
    if max_heavy < 0 or max_heavy > heavy_ceiling:
        return JSONResponse({'error': f'maxHeavy must be 0..{heavy_ceiling}'}, status_code=400)
    hold = body.get('hold', current['hold'])
    if not isinstance(hold, list):
        return JSONResponse({'error': 'hold must be a list'}, status_code=400)
    allowed = set(_resources.CLASS_NAMES) | {'heavy'}
    hold = [str(x).strip().lower() for x in hold if str(x).strip()]
    bad = sorted(set(hold) - allowed)
    if bad:
        return JSONResponse({'error': 'unknown hold class(es): ' + ', '.join(bad)}, status_code=400)
    village['resourcePolicy'] = {
        'maxConcurrent': max_concurrent,
        'maxHeavy': max_heavy,
        'hold': sorted(set(hold)),
        'reason': str(body.get('reason') or '')[:300],
        'updatedAt': int(time.time() * 1000),
        'updatedBy': actor,
    }
    save_state_to_db(state)
    log_action(actor, 'village_resource_policy_set',
               {'villageId': village_id, **village['resourcePolicy']},
               authorized=True)
    return JSONResponse({'ok': True, 'villageId': village_id,
                         **_village_resource_view(state, village_id)})


import os


def _village_token_ok(request):
    expected = (os.environ.get('THINK_TANK_PARENT_TOKEN')
                or _load_env().get('THINK_TANK_PARENT_TOKEN', ''))
    if not expected:
        return False
    return (request.headers.get('X-Parent-Token') or '').strip() == expected


@app.post('/api/villages/{village_id}/resource-acquire')
async def village_resource_acquire(village_id: str, request: Request):
    """A village asks the HOST-WIDE ledger for a sandbox lease. The
    parent's governor enforces the deployment.json class caps across every
    village plus its own work, and the village's own policy (share, holds)
    from the parent's state."""
    if not _village_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    cls = str((body or {}).get('resourceClass') or 'light')
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        lease = _resources.GOVERNOR.acquire(
            village_id, cls,
            task_id=str((body or {}).get('taskId') or ''),
            agent_id=str((body or {}).get('agentId') or ''),
            state=state, wait_s=0)
    except _resources.ResourceDenied as exc:
        return JSONResponse({'granted': False, 'reason': str(exc)[:200]})
    return {'granted': True, 'lease': lease}


@app.post('/api/villages/{village_id}/resource-release')
async def village_resource_release(village_id: str, request: Request):
    if not _village_token_ok(request):
        return JSONResponse({'error': 'bad village token'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    lease_id = str((body or {}).get('leaseId') or '')
    if lease_id:
        _resources.GOVERNOR.release(lease_id)
    return {'ok': True}
