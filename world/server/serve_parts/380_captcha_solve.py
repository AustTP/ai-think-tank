

@app.post('/api/captcha/{request_id}/solve')
async def captcha_solve(request_id: str, request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not agent_id:
        return JSONResponse({'error': 'a valid agent key is required'}, status_code=401)
    rec = captcha.get(request_id)
    if rec is None:
        return JSONResponse({'error': 'request not found'}, status_code=404)
    if rec['agentId'] != agent_id:
        return JSONResponse({'error': 'request belongs to another agent'}, status_code=403)
    if rec['status'] == 'solved':
        return JSONResponse({'ok': True, 'status': 'solved', 'requestId': request_id})
    if rec['status'] in ('expired', 'failed'):
        return JSONResponse({'error': f"request is {rec['status']}"}, status_code=410)
    if not EXECUTION_ENABLED or not BROWSING_ENABLED:
        return JSONResponse({'error': 'browser execution is disabled'}, status_code=403)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'Rate limit exceeded.'}, status_code=429)
    page_host = urllib.parse.urlparse(rec.get('pageUrl') or '').hostname or ''
    if not page_host or not _is_safe_public_host(page_host):
        return JSONResponse({'error': 'captcha page URL is not a safe public host'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        wait_s = max(1, min(int(body.get('waitSec') or 120), 300))
    except (TypeError, ValueError):
        wait_s = 120
    rec = await asyncio.to_thread(captcha.wait_for_answer, request_id, wait_s, 1.0)
    if rec is None:
        return JSONResponse({'error': 'request not found'}, status_code=404)
    if rec['status'] in ('pending',):
        return JSONResponse({'ok': False, 'status': 'waiting', 'requestId': request_id,
                             'replyHint': f'Reply on Telegram with: captcha {request_id} <answer>'},
                            status_code=202)
    if rec['status'] in ('expired', 'failed'):
        return JSONResponse({'error': f"request is {rec['status']}"}, status_code=410)
    if not rec.get('inputSelector'):
        return JSONResponse({'error': 'this challenge has no fillable input; solve it manually in a browser'}, status_code=400)
    if not rec.get('pageUrl'):
        return JSONResponse({'error': 'request has no page URL'}, status_code=400)
    sandbox_dir = _sandbox_dir_for(rec['sandboxId'])
    action = {
        'op': 'login',
        'url': rec['pageUrl'],
        'selector': '',
        'value': '',
        'timeoutMs': 30000,
        'maxText': 16000,
        'fields': [{'selector': rec['inputSelector'], 'value': rec['answer']}],
        'submitSelector': rec.get('submitSelector') or '',
    }
    parsed, run_error = await asyncio.to_thread(
        _run_browser_act_sync, sandbox_dir, action, agent_id)
    if parsed is None:
        captcha.mark_failed(request_id, run_error or 'browser run failed', actor=agent_id)
        return JSONResponse({'error': run_error or 'browser run failed'}, status_code=502)
    shot = parsed.pop('_shotB64', None)
    if parsed.get('captchas'):
        captcha.mark_failed(request_id, 'captcha still present', actor=agent_id)
        return JSONResponse({'ok': False, 'status': 'captcha_still_present',
                             'requestId': request_id, 'page': parsed,
                             'screenshotBase64': shot}, status_code=409)
    captcha.mark_solved(request_id, actor=agent_id)
    return JSONResponse({'ok': True, 'status': 'solved', 'requestId': request_id,
                         'page': parsed, 'screenshotBase64': shot})


def _task_need_text(task):
    parts = []
    for key in ('title', 'instructions', 'userStory', 'acceptanceCriteria',
                'goal', 'note', 'projectLabel'):
        value = task.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
        elif value:
            parts.append(str(value))
    return ' '.join(parts).lower()


@app.post('/api/credentials/request-access')
async def request_credential_access(request: Request):
    """Need-based access to ONE read-only credential for ONE active task.

    An agent cannot enumerate the vault. It names a service that the operator
    put in world/credential_access.json, proves it is working a task assigned
    to it, and the task text must reference that service or one of its hosts.
    The server then mints a short-lived handle scoped to exactly those hosts
    and methods. The secret stays in the vault; the response contains only the
    handle. This is the on-demand path, not a standing key.
    """
    body = await request.json()
    agent_id = (body.get('agentId') or '').strip()
    service = (body.get('service') or '').strip().lower()
    purpose = (body.get('purpose') or '').strip()[:300]
    task_id = (body.get('taskId') or '').strip()
    if not verify_agent_key(agent_id, request.headers.get('X-Agent-Key')):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
    if not service or not purpose or not task_id:
        return JSONResponse({'error': 'service, purpose, and taskId are required'},
                            status_code=400)
    if len(purpose) < 10:
        return JSONResponse({'error': 'purpose must say what task need this serves'},
                            status_code=400)
    cfg = _credential_access_cfg()
    spec = (cfg.get('autoGrant') or {}).get(service)
    if not spec:
        return JSONResponse({'error': 'that service is not enabled for need-based '
                                      'read-only access'}, status_code=403)
    state = get_state_from_db() or {}
    task = (state.get('tasks') or {}).get(task_id)
    if not isinstance(task, dict):
        return JSONResponse({'error': 'task not found'}, status_code=404)
    if (task.get('assignedTo') or '') != agent_id:
        return JSONResponse({'error': 'that task is not assigned to you'},
                            status_code=403)
    if task.get('status') not in ('walking', 'working'):
        return JSONResponse({'error': f"task is {task.get('status')!r}, not active"},
                            status_code=409)
    text = _task_need_text(task)
    needles = [service] + [h.lower() for h in (spec.get('hosts') or [])]
    if not any(n and n in text for n in needles):
        return JSONResponse({'error': 'the assigned task does not reference this '
                                      'service; request access only for work that '
                                      'actually needs it'}, status_code=403)
    credential = spec.get('credential') or service
    purpose_tag = f'task:{task_id} '
    handle = None
    with _db() as conn:
        row = conn.execute(
            "SELECT kind FROM external_credentials WHERE name = ? AND status = 'active'",
            (credential,)).fetchone()
        if not row or row[0] != 'login':
            return JSONResponse({'error': 'credential is not an active login'},
                                status_code=404)
        # Idempotent per task: repeated requests for the SAME active task
        # return the same live handle instead of revoking and minting a new
        # one. Live miss 2026-10-19: a spike asked three times, each call
        # rotated the handle, and the model kept submitting an earlier one.
        existing = conn.execute(
            'SELECT handle FROM capability_handles WHERE agent_id = ? '
            'AND credential_name = ? AND purpose LIKE ? AND expires_at > ? '
            'ORDER BY created_at DESC LIMIT 1',
            (agent_id, credential, purpose_tag + '%', time.time())).fetchone()
        if existing:
            handle = existing[0]
        else:
            # One live handle per agent per credential: a new need replaces
            # the prior task's handle.
            conn.execute('DELETE FROM capability_handles WHERE agent_id = ? '
                         'AND credential_name = ?', (agent_id, credential))
    refusal = None
    if handle is None:
        handle, refusal = mint_capability_handle(
            agent_id, credential, purpose_tag + purpose,
            list(spec.get('hosts') or []),
            list(spec.get('methods') or ['POST']), 'need-based-read-only',
            int(spec.get('ttlSec') or 900))
        if handle is None:
            return JSONResponse({'error': refusal or 'could not mint a handle'},
                                status_code=403)
    log_action(agent_id, 'credential_access_granted', {
        'service': service, 'credential': credential, 'taskId': task_id,
        'hosts': spec.get('hosts'), 'methods': spec.get('methods'),
        'ttlSec': spec.get('ttlSec'), 'scope': 'read-only-social',
    }, authorized=True)
    _append_passport_decision('credential_access_granted', agent_id, {
        'service': service, 'credential': credential, 'taskId': task_id,
        'hosts': spec.get('hosts'), 'scope': 'read-only-social'})
    return JSONResponse({'ok': True, 'handle': handle, 'service': service,
                         'credential': credential, 'hosts': spec.get('hosts'),
                         'methods': spec.get('methods'),
                         'expiresInSec': int(spec.get('ttlSec') or 900),
                         'readOnly': True})


@app.post('/api/keys/handles')
async def add_handle(request: Request):
    # Handle minting is "limited by the ask": every grant is a purpose + host
    # scope + method scope + TTL, resolved server-side so the agent never holds
    # the secret. The authorizer is the player, or a director/admin (real
    # in-game authority). It must NOT be inferable from the ABSENCE of a
    # self-declared `requesterId` query param: an agent's own HTTP client can
    # clear the global auth middleware with its key and reach this handler by
    # simply omitting requesterId, so the gate requires an actual verified
    # player session OR a proven director/admin identity.
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'handle minting requires a player session or a director/admin'}, status_code=403)
    body = await request.json()
    agent_id = (body.get('agentId') or '').strip()
    credential_name = (body.get('credentialName') or '').strip()
    purpose = (body.get('purpose') or '').strip()
    allowed_hosts = body.get('allowedHosts') or '*'
    allowed_methods = body.get('allowedMethods') or ['GET']
    try:
        ttl_s = max(1, int(body.get('ttlSec') or 3600))
    except (TypeError, ValueError):
        ttl_s = 3600
    if not agent_id or not credential_name or not purpose:
        return JSONResponse({'error': 'agentId, credentialName, and purpose are required'}, status_code=400)
    if isinstance(allowed_hosts, str):
        allowed_hosts = ['*'] if allowed_hosts == '*' else [h for h in allowed_hosts.replace(' ', '').split(',') if h]
    if isinstance(allowed_methods, str):
        allowed_methods = [m.strip().upper() for m in allowed_methods.replace(' ', '').split(',') if m.strip()]
    handle, refusal = mint_capability_handle(agent_id, credential_name, purpose,
                                             allowed_hosts, [m.upper() for m in allowed_methods],
                                             actor, ttl_s)
    if handle is None:
        status = 403 if refusal and refusal != 'unknown credential' else 404
        return JSONResponse({'error': refusal or f'unknown credential: {credential_name}'}, status_code=status)
    log_action(actor, 'handle_minted', {'agentId': agent_id, 'credential': credential_name,
                                        'purpose': purpose, 'ttlSec': ttl_s}, authorized=True)
    # Chain the mint (credential shown to agent, host/method scope, TTL) -- but
    # never the handle nonce itself, which would defeat its being a secret.
    _append_passport_decision('handle_minted', actor, {
        'agentId': agent_id, 'credential': credential_name, 'purpose': purpose,
        'ttlSec': ttl_s, 'hosts': allowed_hosts if isinstance(allowed_hosts, list) else ['*']})
    # The handle nonce is shown exactly once -- like a minted token -- and is
    # the ONLY thing that grants use. It is never stored in a readable form.
    return JSONResponse({'ok': True, 'handle': handle})


@app.get('/api/keys/handles/mine')
async def list_my_handles(request: Request):
    """Agent-facing: return the calling agent's own active capability handles
    (handle nonce + scope metadata). Safe because resolve_capability_handle
    already scopes every handle to exactly this agent_id -- fetching your own
    grants does not widen anything, it only lets the agent discover what a
    director/admin or the player already minted for it (handles are otherwise
    shown once at mint time and not re-listed, which makes them unusable in
    autonomous work). Authenticated by the agent's own key."""
    agent_id = (request.query_params.get('agentId') or '').strip()
    if not agent_id or not verify_agent_key(agent_id, request.headers.get('X-Agent-Key')):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    now = time.time()
    with _db() as conn:
        conn.execute('DELETE FROM capability_handles WHERE expires_at <= ?', (now,))
        rows = conn.execute(
            'SELECT handle, credential_name, purpose, allowed_hosts, allowed_methods, expires_at '
            'FROM capability_handles WHERE agent_id = ? ORDER BY created_at DESC',
            (agent_id,)).fetchall()
    handles = []
    for r in rows:
        try:
            hosts = json.loads(r[3]) if isinstance(r[3], str) else r[3]
            methods = json.loads(r[4]) if isinstance(r[4], str) else r[4]
        except Exception:
            hosts, methods = r[3], r[4]
        handles.append({
            'handle': r[0], 'credentialName': r[1], 'purpose': r[2],
            'allowedHosts': hosts, 'allowedMethods': methods,
            'expiresAt': r[5], 'expiresInSec': max(0, int(r[5] - now)),
        })
    return JSONResponse({'agentId': agent_id, 'handles': handles})


@app.delete('/api/keys/handles/{handle}')
async def delete_handle(handle: str, request: Request):
    # Same fix as add_handle above, same reason: require a proven player
    # session rather than inferring "not an agent" from an omitted param.
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'handles are player-only'}, status_code=403)
    _revoke_handle(handle)
    log_action('player', 'handle_revoked', {'handle': handle[:8]}, authorized=True)
    _append_passport_decision('handle_revoked', 'player', {'handle': handle[:8]})
    return JSONResponse({'ok': True})


@app.get('/api/experiments/sublow/quote')
async def sublow_quote(request: Request):
    """Cost-estimate worksheet for minting a grant: the model the proxy will
    serve, its blended $/M price, a per-call and total estimate for the
    requested call count, the agent's month-to-date spend and remaining
    headroom, and the tank's month against the shared cap -- everything needed
    to declare an estimate that will actually hold. Operator-gated (same gate
    as handle minting): a player session or a proven director/admin."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'quoting requires a player session or a director/admin'}, status_code=403)
    qp = request.query_params
    try:
        estimated_calls = int(qp.get('estimatedCalls') or 1)
    except (TypeError, ValueError):
        estimated_calls = 1
    quote = quote_sublow_grant(qp.get('agentId'), estimated_calls, qp.get('maxTokensPerCall'))
    return JSONResponse({'ok': True, 'actor': actor, **quote})


@app.post('/api/experiments/sublow/grant')
async def sublow_grant(request: Request):
    # Mint a sublow experiment grant. Same operator gate as add_handle: a
    # verified player session OR a proven director/admin -- never inferable
    # from an omitted requesterId, because an agent's own HTTP client can
    # clear the global middleware by simply not sending one. The declared
    # cost estimate IS the authorization: no estimate, no grant (the proxy
    # has nothing to enforce). The raw token is shown exactly once here and
    # only its SHA-256 is stored, like a minted credential.
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'grant minting requires a player session or a director/admin'}, status_code=403)
    body = await request.json()
    token, refusal = mint_sublow_grant(
        body.get('agentId'), body.get('purpose'), body.get('estimateUsd'), actor,
        task_id=body.get('taskId'), ttl_s=body.get('ttlSec'),
        max_tokens_per_call=body.get('maxTokensPerCall'))
    if token is None:
        return JSONResponse({'error': refusal}, status_code=403)
    grant = validate_sublow_grant(token)
    details = {'agentId': grant['agent_id'], 'grant': grant['id'], 'taskId': grant['task_id'],
               'purpose': grant['purpose'], 'estimateUsd': grant['estimate_usd'],
               'ttlSec': int(grant['expires_at'] - grant['created_at']),
               'maxTokensPerCall': grant['max_tokens_per_call']}
    log_action(actor, 'sublow_grant_minted', details, authorized=True)
    # Chain the mint (purpose, estimate, TTL) -- but never the token itself,
    # which would defeat its being a secret.
    _append_passport_decision('sublow_grant_minted', actor, details)
    return JSONResponse({
        'ok': True, 'grant': grant['id'], 'token': token,
        'expiresAt': grant['expires_at'], 'model': _sublow_tier_slug(), **details,
    })


@app.post('/api/experiments/sublow/revoke')
async def sublow_revoke(request: Request):
    """Operator revokes an outstanding experiment grant before it is spent or
    after it leaks. Accepts the grant id (always shown at mint) or the raw
    token; deletes the row outright so it can never resolve again."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'revoking requires a player session or a director/admin'}, status_code=403)
    body = await request.json()
    grant_ref = (body.get('grant') or '').strip()
    if not grant_ref:
        return JSONResponse({'error': 'grant is required'}, status_code=400)
    if not revoke_sublow_grant(grant_ref):
        return JSONResponse({'error': 'unknown grant'}, status_code=404)
    # Never log the raw token, only the opaque grant id (a token handed in
    # for revocation is logged as a fixed marker instead of its bytes).
    shown = grant_ref if grant_ref.startswith('sg-') else '<token>'
    log_action(actor, 'sublow_grant_revoked', {'grant': shown}, authorized=True)
    _append_passport_decision('sublow_grant_revoked', actor, {'grant': shown})
    return JSONResponse({'ok': True})


@app.post('/api/experiments/sublow/inference')
async def sublow_inference(request: Request):
    # Server-side sublow inference for scale experiments (the Colab lane):
    # compute runs in the notebook, every model call round-trips HERE, and
    # the OpenRouter key never leaves this process. The grant token is the
    # ONLY credential (X-Sublow-Grant, or Authorization: Bearer) -- not a
    # session, not an agent key, because a notebook has neither. The model is
    # NEVER taken from the body: the proxy hard-codes the sublow tier pick,
    # clamps max_tokens to what the grant allows, runs the shared input
    # guard, and re-checks the burn-down estimate plus every Bank gate
    # (agent cap, per-task budget, shared tank cap) before a cent moves.
    # NOT protected by the deny-by-default middleware (listed in
    # AUTH_PUBLIC_PATHS): the grant token is the auth, the same
    # self-guarding shape /api/chat uses.
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)
    raw_token = request.headers.get('X-Sublow-Grant') or ''
    if not raw_token:
        auth_header = request.headers.get('Authorization') or ''
        if auth_header[:7].lower() == 'bearer ':
            raw_token = auth_header[7:].strip()
    grant = validate_sublow_grant(raw_token.strip())
    if not grant:
        return JSONResponse({'error': 'a valid sublow grant token is required (X-Sublow-Grant header)'},
                            status_code=401)
    body = await request.json()
    agent_id = grant['agent_id']
    task_id = (grant['task_id'] or '').strip()
    messages = body.get('messages')
    if not messages:
        return JSONResponse({'error': 'messages is required'}, status_code=400)
    model = _sublow_tier_slug()
    if not model:
        return JSONResponse({'error': 'no sublow model pick yet -- run a model-tier refresh first'},
                            status_code=503)
    # Shared input guard, identical rule to /api/chat.
    violation = _input_guard_violation(messages)
    if violation:
        log_action(agent_id, 'sublow_input_guard_blocked',
                   {'grantId': grant['id'], 'model': model,
                    'patterns_matched': violation['patterns'], 'snippet': violation['snippets']},
                   authorized=True)
        return JSONResponse({'error': 'Blocked by input guard (potential prompt injection)'}, status_code=400)
    # The estimate gate: the declared estimate is a hard ceiling, enforced on
    # ACCRUED ACTUAL cost. Refused at >= so the grant dies exactly when the
    # estimate is covered -- never one call past it, never "approximately".
    estimate = float(grant['estimate_usd'])
    if float(grant['accrued_usd']) >= estimate:
        log_action(agent_id, 'sublow_grant_exhausted',
                   {'grantId': grant['id'], 'model': model,
                    'accruedUsd': round(float(grant['accrued_usd']), 6),
                    'estimateUsd': estimate}, authorized=True)
        return JSONResponse({
            'error': 'This grant\'s declared cost estimate is fully spent -- mint a new grant to continue.',
            'grantExhausted': True, 'estimateUsd': estimate,
            'accruedUsd': round(float(grant['accrued_usd']), 6),
        }, status_code=429)
    # The Bank's own gates, same order and semantics as /api/chat: per-agent
    # monthly cap, per-task budget, shared tank cap. Each is best-effort the
    # same way it is there (an accounting hiccup never blocks; a real breach
    # hard-refuses with 429).
    if _agent_spend_cap_exceeded(agent_id):
        log_action(agent_id, 'sublow_agent_budget_blocked',
                   {'grantId': grant['id'], 'model': model}, authorized=True)
        return JSONResponse({
            'error': (f'This agent has used its full monthly model-spend budget '
                      f'(${AGENT_SPEND_CAP_USD:.2f}) and further calls are paused '
                      f'until a director raises it.'),
            'agentBudgetExhausted': True, 'agentSpendCapUsd': AGENT_SPEND_CAP_USD,
        }, status_code=429)
    _dir_blocked = _director_budget_gate(
        agent_id, model, grant['id'], True, 'sublow')
    if _dir_blocked:
        return _dir_blocked
    if task_id and _task_budget_exhausted(task_id):
        spent, attempts = _task_budget_spent(task_id)
        log_action(agent_id, 'sublow_task_budget_blocked',
                   {'grantId': grant['id'], 'model': model, 'taskId': task_id,
                    'taskSpendUsd': round(spent, 6)}, authorized=True)
        return JSONResponse({
            'error': (f'This task has used its full model-spend budget '
                      f'(${spent:.4f} spent) and further model calls are paused '
                      f'until a director raises it.'),
            'budgetExhausted': True, 'taskSpendUsd': round(spent, 6),
            'taskSpendAttempts': attempts,
        }, status_code=429)
    if _think_tank_spend_cap_exceeded():
        log_action(agent_id, 'sublow_tank_budget_blocked',
                   {'grantId': grant['id'], 'model': model}, authorized=True)
        return JSONResponse({
            'error': (f'Think Tank spend cap (${SPEND_CAP_USD}) reached -- no further '
                      f'model calls until it is raised in .env'),
            'tankBudgetExhausted': True, 'spendCapUsd': SPEND_CAP_USD,
        }, status_code=429)
    try:
        max_tokens = min(max(int(body.get('max_tokens', grant['max_tokens_per_call'])), 1),
                         int(grant['max_tokens_per_call']))
    except (TypeError, ValueError):
        max_tokens = int(grant['max_tokens_per_call'])
    try:
        # urllib is blocking -- off the event loop, same as /api/chat.
        data = await asyncio.to_thread(_call_openrouter_sync, model, messages, max_tokens)
        reply = data['choices'][0]['message'].get('content') or ''
        usage_cost = (data.get('usage') or {}).get('cost', 0.0)
    except urllib.error.HTTPError as e:
        return JSONResponse({'error': e.read().decode()}, status_code=e.code)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    if isinstance(usage_cost, (int, float)) and usage_cost:
        village_id = _sim_village_for_agent(agent_id)
        _accrue_spend(SUBLOW_LEDGER_SERVICE, usage_cost, village_id=village_id, agent_id=agent_id)
        if task_id:
            _accrue_task_spend(task_id, usage_cost)
        accrue_sublow_grant(grant['id'], usage_cost)
    log_action(agent_id, 'sublow_experiment_call',
               {'grantId': grant['id'], 'model': model,
                'cost': float(usage_cost) if isinstance(usage_cost, (int, float)) else 0.0},
               authorized=True)
    # Re-read the grant for authoritative burn-down numbers (the accrual
    # above just landed); fall back to the pre-call row if it expired in the
    # meantime, since the reply is already earned either way.
    fresh = validate_sublow_grant(raw_token.strip()) or grant
    accrued = float(fresh['accrued_usd'])
    return JSONResponse({
        'reply': reply,
        'model': model,
        'cost': float(usage_cost) if isinstance(usage_cost, (int, float)) else 0.0,
        'grant': {'id': fresh['id'], 'estimateUsd': estimate,
                  'accruedUsd': round(accrued, 6),
                  'remainingUsd': round(max(0.0, estimate - accrued), 6),
                  'calls': int(fresh['calls']),
                  'exhausted': accrued >= estimate},
    })


@app.get('/api/experiments/jev/quote')
async def jev_quote(request: Request):
    """Cost-estimate worksheet for minting a JEV grant: the model the proxy
    will serve (resolved JEV slug), its blended $/M price (a model_tiers row
    when one exists, else the conservative fallback basis, with priceSource
    saying which), a per-call and total estimate for the requested call
    count, the agent's month-to-date spend and remaining headroom, and the
    tank's month against the shared cap. Operator-gated, same as the sublow
    quote."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'quoting requires a player session or a director/admin'}, status_code=403)
    qp = request.query_params
    try:
        estimated_calls = int(qp.get('estimatedCalls') or 1)
    except (TypeError, ValueError):
        estimated_calls = 1
    quote = quote_jev_grant(qp.get('agentId'), estimated_calls, qp.get('maxTokensPerCall'))
    return JSONResponse({'ok': True, 'actor': actor, **quote})


@app.post('/api/experiments/jev/grant')
async def jev_grant(request: Request):
    # Mint a JEV experiment grant. Same operator gate and same rules as the
    # sublow mint: a verified player session OR a proven director/admin, a
    # declared cost estimate that IS the authorization, a token shown exactly
    # once with only its SHA-256 stored. The model is resolved server-side
    # from the JEV chain -- never accepted from the body.
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'grant minting requires a player session or a director/admin'}, status_code=403)
    body = await request.json()
    token, refusal = mint_jev_grant(
        body.get('agentId'), body.get('purpose'), body.get('estimateUsd'), actor,
        task_id=body.get('taskId'), ttl_s=body.get('ttlSec'),
        max_tokens_per_call=body.get('maxTokensPerCall'))
    if token is None:
        return JSONResponse({'error': refusal}, status_code=403)
    grant = validate_jev_grant(token)
    details = {'agentId': grant['agent_id'], 'grant': grant['id'], 'taskId': grant['task_id'],
               'purpose': grant['purpose'], 'estimateUsd': grant['estimate_usd'],
               'ttlSec': int(grant['expires_at'] - grant['created_at']),
               'maxTokensPerCall': grant['max_tokens_per_call']}
    log_action(actor, 'jev_grant_minted', details, authorized=True)
    # Chain the mint (purpose, estimate, TTL) -- but never the token itself,
    # which would defeat its being a secret.
    _append_passport_decision('jev_grant_minted', actor, details)
    return JSONResponse({
        'ok': True, 'grant': grant['id'], 'token': token,
        'expiresAt': grant['expires_at'], 'model': _jev_openrouter_slug(), **details,
    })


@app.post('/api/experiments/jev/revoke')
async def jev_revoke(request: Request):
    """Operator revokes an outstanding JEV experiment grant before it is
    spent or after it leaks. Accepts the grant id (always shown at mint) or
    the raw token; deletes the row outright so it can never resolve again."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'revoking requires a player session or a director/admin'}, status_code=403)
    body = await request.json()
    grant_ref = (body.get('grant') or '').strip()
    if not grant_ref:
        return JSONResponse({'error': 'grant is required'}, status_code=400)
    if not revoke_jev_grant(grant_ref):
        return JSONResponse({'error': 'unknown grant'}, status_code=404)
    # Never log the raw token, only the opaque grant id (a token handed in
    # for revocation is logged as a fixed marker instead of its bytes).
    shown = grant_ref if grant_ref.startswith('jg-') else '<token>'
    log_action(actor, 'jev_grant_revoked', {'grant': shown}, authorized=True)
    _append_passport_decision('jev_grant_revoked', actor, {'grant': shown})
    return JSONResponse({'ok': True})


@app.post('/api/experiments/jev/inference')
async def jev_inference(request: Request):
    # Server-side JEV-model inference for experiments: the OpenRouter key
    # never leaves this process, and the grant token is the ONLY credential
    # (X-Jev-Grant, or Authorization: Bearer) -- not a session, not an agent
    # key, because an off-server experiment has neither. The model is NEVER
    # taken from the body: the proxy resolves the JEV slug server-side,
    # clamps max_tokens to what the grant allows, runs the shared input
    # guard, and re-checks the burn-down estimate plus every Bank gate
    # (agent cap, per-task budget, shared tank cap) before a cent moves.
    # NOT protected by the deny-by-default middleware (listed in
    # AUTH_PUBLIC_PATHS): the grant token is the auth, the same
    # self-guarding shape /api/chat and the sublow inference endpoint use.
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)
    raw_token = request.headers.get('X-Jev-Grant') or ''
    if not raw_token:
        auth_header = request.headers.get('Authorization') or ''
        if auth_header[:7].lower() == 'bearer ':
            raw_token = auth_header[7:].strip()
    grant = validate_jev_grant(raw_token.strip())
    if not grant:
        return JSONResponse({'error': 'a valid JEV grant token is required (X-Jev-Grant header)'},
                            status_code=401)
    body = await request.json()
    agent_id = grant['agent_id']
    task_id = (grant['task_id'] or '').strip()
    messages = body.get('messages')
    if not messages:
        return JSONResponse({'error': 'messages is required'}, status_code=400)
    model = _jev_openrouter_slug()
    if not model:
        return JSONResponse({'error': 'no JEV model slug resolved -- check JEV_MODEL / the /api/jev/model switch'},
                            status_code=503)
    # Shared input guard, identical rule to /api/chat and the sublow proxy.
    violation = _input_guard_violation(messages)
    if violation:
        log_action(agent_id, 'jev_input_guard_blocked',
                   {'grantId': grant['id'], 'model': model,
                    'patterns_matched': violation['patterns'], 'snippet': violation['snippets']},
                   authorized=True)
        return JSONResponse({'error': 'Blocked by input guard (potential prompt injection)'}, status_code=400)
    # The estimate gate: the declared estimate is a hard ceiling, enforced on
    # ACCRUED ACTUAL cost. Refused at >= so the grant dies exactly when the
    # estimate is covered -- never one call past it, never "approximately".
    estimate = float(grant['estimate_usd'])
    if float(grant['accrued_usd']) >= estimate:
        log_action(agent_id, 'jev_grant_exhausted',
                   {'grantId': grant['id'], 'model': model,
                    'accruedUsd': round(float(grant['accrued_usd']), 6),
                    'estimateUsd': estimate}, authorized=True)
        return JSONResponse({
            'error': 'This grant\'s declared cost estimate is fully spent -- mint a new grant to continue.',
            'grantExhausted': True, 'estimateUsd': estimate,
            'accruedUsd': round(float(grant['accrued_usd']), 6),
        }, status_code=429)
    # The Bank's own gates, same order and semantics as /api/chat: per-agent
    # monthly cap, per-task budget, shared tank cap. Each is best-effort the
    # same way it is there (an accounting hiccup never blocks; a real breach
    # hard-refuses with 429).
    if _agent_spend_cap_exceeded(agent_id):
        log_action(agent_id, 'jev_agent_budget_blocked',
                   {'grantId': grant['id'], 'model': model}, authorized=True)
        return JSONResponse({
            'error': (f'This agent has used its full monthly model-spend budget '
                      f'(${AGENT_SPEND_CAP_USD:.2f}) and further calls are paused '
                      f'until a director raises it.'),
            'agentBudgetExhausted': True, 'agentSpendCapUsd': AGENT_SPEND_CAP_USD,
        }, status_code=429)
    _dir_blocked = _director_budget_gate(
        agent_id, model, grant['id'], True, 'jev')
    if _dir_blocked:
        return _dir_blocked
    if task_id and _task_budget_exhausted(task_id):
        spent, attempts = _task_budget_spent(task_id)
        log_action(agent_id, 'jev_task_budget_blocked',
                   {'grantId': grant['id'], 'model': model, 'taskId': task_id,
                    'taskSpendUsd': round(spent, 6)}, authorized=True)
        return JSONResponse({
            'error': (f'This task has used its full model-spend budget '
                      f'(${spent:.4f} spent) and further model calls are paused '
                      f'until a director raises it.'),
            'budgetExhausted': True, 'taskSpendUsd': round(spent, 6),
            'taskSpendAttempts': attempts,
        }, status_code=429)
    if _think_tank_spend_cap_exceeded():
        log_action(agent_id, 'jev_tank_budget_blocked',
                   {'grantId': grant['id'], 'model': model}, authorized=True)
        return JSONResponse({
            'error': (f'Think Tank spend cap (${SPEND_CAP_USD}) reached -- no further '
                      f'model calls until it is raised in .env'),
            'tankBudgetExhausted': True, 'spendCapUsd': SPEND_CAP_USD,
        }, status_code=429)
    try:
        max_tokens = min(max(int(body.get('max_tokens', grant['max_tokens_per_call'])), 1),
                         int(grant['max_tokens_per_call']))
    except (TypeError, ValueError):
        max_tokens = int(grant['max_tokens_per_call'])
    try:
        # urllib is blocking -- off the event loop, same as /api/chat.
        data = await asyncio.to_thread(_call_openrouter_sync, model, messages, max_tokens)
        reply = data['choices'][0]['message'].get('content') or ''
        usage_cost = (data.get('usage') or {}).get('cost', 0.0)
    except urllib.error.HTTPError as e:
        return JSONResponse({'error': e.read().decode()}, status_code=e.code)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    if isinstance(usage_cost, (int, float)) and usage_cost:
        village_id = _sim_village_for_agent(agent_id)
        _accrue_spend(JEV_LEDGER_SERVICE, usage_cost, village_id=village_id, agent_id=agent_id)
        if task_id:
            _accrue_task_spend(task_id, usage_cost)
        accrue_jev_grant(grant['id'], usage_cost)
    log_action(agent_id, 'jev_experiment_call',
               {'grantId': grant['id'], 'model': model,
                'cost': float(usage_cost) if isinstance(usage_cost, (int, float)) else 0.0},
               authorized=True)
    # Re-read the grant for authoritative burn-down numbers (the accrual
    # above just landed); fall back to the pre-call row if it expired in the
    # meantime, since the reply is already earned either way.
    fresh = validate_jev_grant(raw_token.strip()) or grant
    accrued = float(fresh['accrued_usd'])
    return JSONResponse({
        'reply': reply,
        'model': model,
        'cost': float(usage_cost) if isinstance(usage_cost, (int, float)) else 0.0,
        'grant': {'id': fresh['id'], 'estimateUsd': estimate,
                  'accruedUsd': round(accrued, 6),
                  'remainingUsd': round(max(0.0, estimate - accrued), 6),
                  'calls': int(fresh['calls']),
                  'exhausted': accrued >= estimate},
    })


@app.post('/api/execute')
async def execute(request: Request):
    # Real sandboxed execution for the Work Room -- real
    # execution, not simulated. Every command is classified BEFORE it runs
    # (same "classify the request, not the result" shape as /api/browse),
    # then actually runs inside an isolated, network-disabled, resource-
    # capped Docker container with only its own scratch directory mounted
    # -- confirmed before any of this was wired up: no network
    # access, no host filesystem access, resource limits all hold.
    if not EXECUTION_ENABLED:
        return JSONResponse({'error': 'Agent execution is disabled (AGENT_EXECUTION_ENABLED=false in .env)'}, status_code=403)

    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    command = (body.get('command') or '').strip()
    purpose = (body.get('purpose') or '').strip()
    sandbox_id = body.get('sandboxId') or ('sandbox-' + secrets.token_hex(4))
    sandbox_id = _village_sandbox_id(agent_id, sandbox_id)  # per-village repo isolation
    if not _SANDBOX_ID_RE.match(sandbox_id or ''):
        return JSONResponse({'error': 'invalid sandboxId'}, status_code=400)
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'execute'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = _actor_is_authorized(request, agent_id)
    if not authorized:
        return JSONResponse({'error': 'a valid agent key for agentId is required'}, status_code=401)
    if not command:
        return JSONResponse({'error': 'command is required'}, status_code=400)
    confirmation_error, confirmation_id = _confirmation_gate_or_response(
        body, agent_id, 'execute', {'sandboxId': sandbox_id},
        {'command': command, 'purpose': purpose})
    if confirmation_error:
        return confirmation_error
    allowed, reason = await _classify_command(command, purpose, agent_id)
    if not allowed:
        # Blocked by Jev: the agent's supervisor (a NON-Jev model) gets one
        # autonomous review before the block is final. Overrides are logged
        # and passport-chained; access/API-key needs still go through the
        # dedicated request endpoints (those escalate to the player).
        overridden, override_note = await _supervisor_override_decision(
            agent_id, 'command', command, purpose, reason, EXECUTE_BLOCK_CATEGORIES)
        if not overridden:
            # Safety blocks no longer page the player -- the agent sees the block
            # and adapts. Access/API-key needs go through /api/allowlist/request
            # or /api/api-service/request, which DO escalate (only those need the
            # player's grant).
            log_action(agent_id, 'execute', {'sandboxId': sandbox_id, 'command': command, 'purpose': purpose, 'decision': 'blocked', 'reason': reason, 'supervisorNote': override_note}, authorized=authorized)
            return JSONResponse({'allowed': False, 'reason': reason, 'supervisorNote': override_note})
        log_action(agent_id, 'execute', {'sandboxId': sandbox_id, 'command': command, 'purpose': purpose, 'decision': 'allowed_supervisor_override', 'reason': reason, 'supervisorNote': override_note}, authorized=authorized)

    sandbox_dir = _sandbox_dir_for(sandbox_id)
    # BEFORE the write, not after -- a snapshot taken after a destructive
    # command has already run would just capture the damage.
    await asyncio.to_thread(_snapshot_sandbox, sandbox_id, sandbox_dir)
    result = await asyncio.to_thread(_run_in_sandbox_sync, sandbox_dir, command)
    log_action(agent_id, 'execute', {'sandboxId': sandbox_id, 'command': command, 'purpose': purpose, 'decision': 'allowed', 'exitCode': result['exitCode'], 'timedOut': result['timedOut']}, authorized=authorized)
    _finish_confirmation(confirmation_id, 'executed')
    sync_prototypes(agent_id, sandbox_dir)
    return JSONResponse({'allowed': True, 'sandboxId': sandbox_id, **result})


# browser_act -- the think tank's only real headed-browser primitive
# (2026-10-08). /api/execute runs arbitrary scripts; /api/browse fetches a
# single page; NEITHER can fill a form, click a button, submit, and keep the
# session. This endpoint can: one fixed op (goto|read|fill|click|submit|
# screenshot) at a time, executed by the fixed /opt/browser_driver.py inside
# the SAME sandbox container (same network allowlist, same isolation), so the
# Jev-classified action runs where every other agent action runs. A persistent
# profile dir under the sandbox makes the session survive across calls, so an
# agent can go -> fill -> submit -> read like a real user.
BROWSER_ACT_OPS = ('goto', 'read', 'fill', 'fill_many', 'login', 'click', 'submit', 'screenshot')


def _credential_access_cfg():
    try:
        return get_credential_access_config()
    except Exception:
        return CREDENTIAL_ACCESS_CONFIG


def _host_matches(host, allowed):
    host = (host or '').strip().lower().rstrip('.')
    if not host:
        return False
    for item in allowed or []:
        base = str(item or '').strip().lower().rstrip('.')
        if not base:
            continue
        if host == base or host.endswith('.' + base):
            return True
    return False


def _is_readonly_social_host(host):
    return _host_matches(host, _credential_access_cfg().get('readOnlyHosts') or [])


def _is_social_login_url(url):
    try:
        parsed = urllib.parse.urlparse(url or '')
    except Exception:
        return False
    if not _is_readonly_social_host(parsed.hostname or ''):
        return False
    path = (parsed.path or '/').lower()
    return any(marker in path for marker in
               ('/login', '/accounts/login', '/checkpoint', '/challenge'))


def _active_task_handle(agent_id, task_id, method, url):
    """Return a live handle minted for THIS agent and THIS task whose scope
    covers the page host and method, or None. Used only for social login:
    it lets the browser re-use the handle the agent already requested instead
    of failing because the model copied an older/truncated token."""
    if not agent_id or not task_id:
        return None
    host = (urllib.parse.urlparse(url or '').hostname or '').lower()
    if not host:
        return None
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT handle, allowed_hosts, allowed_methods, expires_at '
                'FROM capability_handles WHERE agent_id = ? AND purpose LIKE ? '
                'AND expires_at > ? ORDER BY created_at DESC',
                (agent_id, f'task:{task_id} %', time.time())).fetchall()
    except Exception:
        return None
    for handle, hosts_raw, methods_raw, _expires in rows:
        try:
            hosts = json.loads(hosts_raw) if isinstance(hosts_raw, str) else hosts_raw
            methods = json.loads(methods_raw) if isinstance(methods_raw, str) else methods_raw
        except Exception:
            continue
        if host in (hosts or []) and method in (methods or []):
            return handle
    return None


def _social_readonly_guard(op, url, page_url, fields):
    """Hard read-only policy for the configured social hosts. Returns an error
    string, or None when the action is allowed. The ONLY write-shaped op is a
    login submit on an auth path, with every field injected from a scoped
    credential handle (the agent never supplies a literal secret)."""
    host = ''
    for candidate in (url, page_url):
        try:
            parsed = urllib.parse.urlparse(candidate or '')
        except Exception:
            continue
        if parsed.hostname:
            host = parsed.hostname
            if _is_readonly_social_host(host):
                break
    if not _is_readonly_social_host(host):
        return None
    if op in ('fill', 'fill_many', 'click', 'submit'):
        return ('read-only social policy: fill, click, and submit are disabled on '
                f'{host}; use op=login once on a login page, then only goto/read/screenshot')
    if op == 'login':
        if not _is_social_login_url(url):
            return ('read-only social policy: op=login is allowed only on a login '
                    f'path for {host}')
        supplied = fields or []
        if len(supplied) < 2:
            return 'read-only social policy: op=login needs username and password fields'
        for field in supplied:
            if not (field.get('credentialHandle') or '').strip():
                return ('read-only social policy: every login field must use a '
                        'scoped credential handle; literal values are refused')
            if (field.get('credentialField') or '').strip().lower() not in ('username', 'password'):
                return ('read-only social policy: login fields must be username or password')
        return None
    if op in ('read', 'screenshot') and not url:
        return (f'read-only social policy: pass the current {host} url on read/screenshot '
                'so the browser opens the real page')
    return None
