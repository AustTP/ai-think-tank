"""Product ownership transfer between teams, by mutual agreement
(2026-11-05).

Owner directive: teams may exchange ownership of a product when BOTH
teams agree. Products live in the server-owned state
(`state['products'][productId].teamId`); ownership routing
(_owner_team_for_goal) reads that field, so a completed transfer
re-routes future asks to the new owner automatically.

The handshake (all writes go through the state writer, all steps audited):

  POST /api/products/transfer-request  {productId, toTeamId, note?}
      A member of the OWNING team proposes the transfer. A member is the
      team's supervisor or any worker reporting to them (the same span
      rule the roster uses everywhere else).
  POST /api/products/transfer-accept   {productId}
      A member of the RECEIVING team accepts. The product's teamId flips
      atomically; the pending proposal is consumed.
  POST /api/products/transfer-cancel   {productId}
      Either side cancels while pending.

Pending proposals live in state['pendingTransfers'][productId]. A
product can have at most one pending transfer; a transfer to the
current owner is refused as a no-op.
"""
_TRANSFER_PENDING_KEY = 'pendingTransfers'


def _roster_member_of(agent_id, team_id):
    """True when the agent is the team's supervisor or reports to it."""
    if not agent_id or not team_id:
        return False
    if str(agent_id).lower() == str(team_id).lower():
        return True
    try:
        with _db() as conn:
            row = conn.execute('SELECT blob FROM roster_agent WHERE id = ?',
                               (agent_id,)).fetchone()
    except Exception:
        return False
    if not row:
        return False
    try:
        blob = json.loads(row[0])
    except Exception:
        return False
    return str(blob.get('director') or '').lower() == str(team_id).lower()


@app.post('/api/products/transfer-request')
@_state_writer
async def api_product_transfer_request(request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not agent_id or verify_agent_key(
            agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    product_id = str(body.get('productId') or '').strip()
    to_team = str(body.get('toTeamId') or '').strip()
    note = str(body.get('note') or '').strip()[:300]
    if not product_id or not to_team:
        return JSONResponse({'error': 'productId and toTeamId are required'},
                            status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    products = state.get('products') or {}
    product = products.get(product_id)
    if not isinstance(product, dict):
        return JSONResponse({'error': 'no such product'}, status_code=404)
    owner = product.get('teamId')
    if not owner:
        return JSONResponse({'error': 'product is unowned; claim it through '
                            'the normal backlog flow instead'},
                            status_code=409)
    if not _roster_member_of(agent_id, owner):
        return JSONResponse({'error': 'only a member of the owning team can '
                            'propose a transfer'}, status_code=403)
    teams = {t.get('id') for t in (state.get('teams') or [])}
    if to_team == owner:
        return JSONResponse({'error': 'the product is already owned by that '
                            'team'}, status_code=409)
    if to_team not in teams:
        return JSONResponse({'error': 'no such team'}, status_code=404)
    pending = state.setdefault(_TRANSFER_PENDING_KEY, {})
    if product_id in pending:
        return JSONResponse({'error': 'a transfer is already pending for '
                            'this product'}, status_code=409)
    pending[product_id] = {
        'fromTeamId': owner, 'toTeamId': to_team, 'by': agent_id,
        'note': note, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                          time.gmtime()),
    }
    save_state_to_db(state)
    log_action(agent_id, 'product_transfer_requested',
               {'productId': product_id, 'from': owner, 'to': to_team},
               authorized=True)
    # The handshake is only real if the receiving side knows it exists:
    # mail the receiving team's director (best-effort, off the response),
    # with the same wake-on-mail parity the regular mail path uses.
    try:
        import sim as _sim
        _teams = state.get('teams') if isinstance(state, dict) else None
        _team = next((x for x in (_teams or [])
                      if str(x.get('id') or '').lower() == str(to_team).lower()),
                     None)
        _director = str((_team or {}).get('directorId') or '') or None
        if _director:
            _text = (f'{agent_id}: product transfer requested: {product_id} '
                     f'({owner} -> {to_team}). Accept with '
                     f'POST /api/products/transfer-accept '
                     f'{{productId: "{product_id}"}} or cancel it.'
                     + (f' Note: {note}' if note else ''))
            if _director in (state.get('agents') or {}):
                _sim._deliver_mail(state, _director, 'agent_mail',
                                   {'text': _text})
            else:
                _target = {'mailbox': []}
                state.setdefault('agents', {})[_director] = _target
                _sim._append_mailbox(_target, {
                    'text': _text, 'read': False,
                    'ts': int(time.time() * 1000)})
    except Exception:
        pass
    return JSONResponse({'ok': True, 'pending': pending[product_id]})


@app.post('/api/products/transfer-accept')
@_state_writer
async def api_product_transfer_accept(request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not agent_id or verify_agent_key(
            agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    product_id = str(body.get('productId') or '').strip()
    if not product_id:
        return JSONResponse({'error': 'productId required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    pending = (state.get(_TRANSFER_PENDING_KEY) or {}).get(product_id)
    if not pending:
        return JSONResponse({'error': 'no pending transfer for this '
                            'product'}, status_code=404)
    if not _roster_member_of(agent_id, pending['toTeamId']):
        return JSONResponse({'error': 'only a member of the receiving team '
                            'can accept'}, status_code=403)
    product = (state.get('products') or {}).get(product_id)
    if not isinstance(product, dict):
        return JSONResponse({'error': 'product vanished mid-handshake'},
                            status_code=409)
    if product.get('teamId') != pending['fromTeamId']:
        return JSONResponse({'error': 'ownership changed since the '
                            'proposal; the transfer is void'},
                            status_code=409)
    product['teamId'] = pending['toTeamId']
    (state.get(_TRANSFER_PENDING_KEY) or {}).pop(product_id, None)
    save_state_to_db(state)
    log_action(agent_id, 'product_transfer_completed',
               {'productId': product_id, 'from': pending['fromTeamId'],
                'to': pending['toTeamId'], 'proposedBy': pending['by']},
               authorized=True)
    return JSONResponse({'ok': True, 'productId': product_id,
                         'owner': pending['toTeamId']})


@app.post('/api/products/transfer-cancel')
@_state_writer
async def api_product_transfer_cancel(request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not agent_id or verify_agent_key(
            agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    product_id = str(body.get('productId') or '').strip()
    if not product_id:
        return JSONResponse({'error': 'productId required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    pending = (state.get(_TRANSFER_PENDING_KEY) or {}).get(product_id)
    if not pending:
        return JSONResponse({'error': 'no pending transfer for this '
                            'product'}, status_code=404)
    if not (_roster_member_of(agent_id, pending['fromTeamId'])
            or _roster_member_of(agent_id, pending['toTeamId'])):
        return JSONResponse({'error': 'only a member of either side can '
                            'cancel'}, status_code=403)
    (state.get(_TRANSFER_PENDING_KEY) or {}).pop(product_id, None)
    save_state_to_db(state)
    log_action(agent_id, 'product_transfer_cancelled',
               {'productId': product_id}, authorized=True)
    return JSONResponse({'ok': True, 'cancelled': product_id})
