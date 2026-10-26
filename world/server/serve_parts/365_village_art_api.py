"""Operator surface for village art: quote, generate now, or outsource it.

The heavy work lives in generate_village_map (055). This is the thin HTTP
edge: a price quote, a synchronous call for an operator who wants it now, and
a queued supervise card for a team director when the village should do it.
"""

VILLAGE_ART_CARD_MAX_USD = 0.05


@app.get('/api/villages/{village}/art/price')
async def village_art_price(request: Request, village: str):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'village art is operator-only'}, status_code=403)
    try:
        max_usd = float(request.query_params.get('maxUsd') or VILLAGE_ART_CARD_MAX_USD)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'maxUsd must be a number'}, status_code=400)
    theme = request.query_params.get('theme') or ''
    result = generate_village_map(actor, village, theme, quote_only=True, max_usd=max_usd)
    return JSONResponse(result)


@app.post('/api/villages/{village}/art')
async def village_art_generate(request: Request, village: str):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'village art is operator-only'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    theme = str(body.get('theme') or '')
    try:
        max_usd = float(body.get('maxUsd') or VILLAGE_ART_CARD_MAX_USD)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'maxUsd must be a number'}, status_code=400)
    force = bool(body.get('force', True))
    if body.get('sync'):
        result = generate_village_map(actor, village, theme, force=force,
                                      max_usd=max_usd, mirror=bool(body.get('mirror', True)))
        return JSONResponse(result)
    quote = generate_village_map(actor, village, theme, quote_only=True, max_usd=max_usd)
    if not quote.get('ok'):
        return JSONResponse(quote, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        import sim as _sim
        team = _team_by_id(state, body.get('teamId')) if body.get('teamId') else None
        if not team:
            team = next((t for t in (state.get('teams') or [])
                         if t.get('directorId') == actor), None)
        if not team:
            authority = actor if actor != 'player' else None
            team = _staffing_team_for_authority(state, authority) if authority else None
        if not team:
            return JSONResponse({'error': 'no team is available to take the art card'},
                                status_code=400)
        director = team.get('directorId') or actor
        if director == 'player':
            director = next((d.get('id') for d in (state.get('agentRoster') or [])
                             if d.get('isAdmin')), None)
        if not director:
            return JSONResponse({'error': 'no director is available for the art card'},
                                status_code=400)
        inner = quote.get('quote') or {}
        estimate = (inner.get('estimate') or {}).get('usd')
        basis = (inner.get('estimate') or {}).get('basis') or ''
        _sim.queue_work(state, [{
            'title': f'[VILLAGE ART] Generate {village} background, collision, and doors'[:120],
            'room': 'commandcenter',
            'taskType': 'supervise',
            'assignedTo': director,
            'teamId': team.get('id') or director,
            'priority': 'normal',
            'budgetUsd': _sim.SUPERVISOR_CARD_BUDGET_USD,
            'sizeEstimate': 'S',
            'instructions': (
                f'Generate the village map package for "{village}" with the '
                'generate_village_map tool. Theme: ' + (theme or 'a quiet village') +
                f'. The quoted PixelLab cost is ${estimate:.4f} ({basis}), and the '
                f'per-call cap is ${max_usd:.4f}. Call quoteOnly first if you want a '
                'fresh quote, then call with force=true. The tool writes the '
                f'background, collision_grid_{village}.json, door_triggers_{village}.json, '
                'and a scenes.json entry. Report the quote, the real cost, and the paths. '
                'If the quote exceeds the cap or the PixelLab balance, report that plainly '
                'and do not spend.'
            ),
        }])
        save_state_to_db(state)
        log_action(actor, 'village_art_queued', {
            'village': village, 'assignedTo': director,
            'teamId': team.get('id') or director,
            'estimateUsd': estimate, 'maxUsd': max_usd,
            'theme': theme[:200]}, authorized=True)
        return JSONResponse({'ok': True, 'queued': True, 'village': village,
                             'assignedTo': director,
                             'teamId': team.get('id') or director,
                             'quote': quote})
    finally:
        _state_abort()
