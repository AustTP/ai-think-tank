

def _api_channel_execute(agent_id, agent_key, channel, url, method, req_headers, req_body, purpose, trace_id, _channel_depth=0):
    """Run a channel's ORDERED backend chain: the first backend that answers
    wins; on failure the chain falls through to the next backend (agent-reach's
    per-platform ordered-backends pattern). Each backend is a registered service
    plus a url_template, so each hop re-enters _api_execute against the backend
    URL -- the backend's own path rules, credential, auth injection, SSRF and
    redirect re-check, redaction, and spend accrual all apply. Channels are
    strictly read-only GET/HEAD. Returns an _api_execute-style result dict;
    never raises for a blocked or failed request."""
    if method not in ('GET', 'HEAD'):
        return {'ok': False, 'allowed': False, 'reason': 'Channels are read-only (GET/HEAD only). Use a registered service for writes.'}
    if _channel_depth > 1:
        return {'ok': False, 'allowed': True, 'error': 'Channel backend chain exceeded maximum depth.'}
    errors = []
    for backend in channel.get('backends') or []:
        bservice_id = backend.get('service')
        bservice = _API_SERVICES.get(bservice_id)
        if not bservice:
            errors.append(f'backend {bservice_id} is not a registered service')
            continue
        backend_url = _api_backend_url(backend, url)
        bparsed = urllib.parse.urlparse(backend_url)
        if bparsed.scheme not in ('http', 'https') or not bparsed.hostname:
            errors.append(f'backend {bservice_id}: produced a malformed URL')
            continue
        if not _is_safe_public_host(bparsed.hostname):
            errors.append(f'backend {bservice_id}: resolves to a private or internal network location')
            continue
        out = _api_execute(agent_id, agent_key, backend_url, method, req_headers, req_body,
                           purpose, False, None, trace_id, _channel_depth=_channel_depth + 1)
        if out.get('allowed') is True and out.get('ok') is True:
            out['channel'] = channel.get('name')
            out['backend'] = bservice_id
            out['service'] = f"{channel.get('name')} (via {bservice_id})"
            # The content is the channel target the agent asked for, not the
            # reader transport -- report the original URL so the Jev redirect
            # re-gate (same-host check) treats it as the approved destination.
            out['finalUrl'] = url
            return out
        errors.append(f'backend {bservice_id}: {out.get("error") or out.get("reason") or "request failed"}')
    return {'ok': False, 'allowed': True, 'error': f"All backends for channel {channel.get('name')} failed: {'; '.join(errors)}"}


def _api_price_line(quote):
    """One line the model sees before it reads an API response: what the call
    cost, or why there is no single price."""
    if not isinstance(quote, dict):
        return ''
    kind = quote.get('kind') or 'unknown'
    usd = quote.get('usd')
    if isinstance(usd, (int, float)):
        basis = (quote.get('basis') or kind).strip()
        return f'Price: ${float(usd):.4f} ({basis}).'
    note = (quote.get('note') or '').strip()
    enforced = quote.get('enforcedBy')
    tail = f' Enforced by: {enforced}.' if enforced else ''
    return (f'Price: {kind}.' + (f' {note}' if note else '') + tail).strip()[:400]


def _api_execute(agent_id, agent_key, url, method='GET', req_body=None, req_headers=None,
                 purpose='', workflow=False, workflow_params=None, trace_id=None, _channel_depth=0):
    """Shared sync chokepoint for the generic api_call path. Used by BOTH the
    /api/api-call endpoint (after its async Jev gate for unregistered hosts)
    and the five curated tool executors (registered services -- registration
    is the approval, so no Jev). Runs registry lookup, path/method
    enforcement, credential resolution, the metered-budget gate, header
    sanitization + server-side auth injection, then either a single request or
    the registry's workflow sequence. Builds, redacts, and wraps the
    model-facing text and accrues spend. Returns a result dict; never raises
    for a blocked or failed request."""
    _ensure_api_services_fresh()
    parsed = urllib.parse.urlparse(url)
    service = _api_service_for_host(parsed.hostname)
    registered = service is not None
    req_headers = dict(req_headers or {})

    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return {'ok': False, 'allowed': False, 'reason': 'Only http/https URLs are allowed.'}
    if not _is_safe_public_host(parsed.hostname):
        return {'ok': False, 'allowed': False, 'reason': 'That address resolves to a private or internal network location and cannot be reached.'}

    # Channel routing: an unregistered host that a curated channel owns (e.g.
    # reddit.com) is fetched through the channel's ordered backend chain instead
    # of a plain direct GET -- the channel is a transport improvement, never a
    # bypass (the host is still SSRF-gated here and Jev-gated in the endpoint).
    if not registered and _channel_depth == 0:
        channel = _api_channel_for_host(parsed.hostname)
        if channel is not None:
            return _api_channel_execute(agent_id, agent_key, channel, url, method, req_headers, req_body, purpose, trace_id, _channel_depth)


    token = token2 = None
    if registered:
        # In workflow mode the actual submitted path lives in the registry's
        # workflow spec (the base URL alone carries no path, e.g. Higgsfield),
        # so enforce path rules only for plain (non-workflow) calls.
        is_workflow = bool(workflow) and bool((service or {}).get('workflow'))
        if not is_workflow and not _api_service_path_allowed(service, method, parsed.path):
            return {'ok': False, 'allowed': False, 'reason': 'That path/method is not in the registered service\'s allowed rules.'}
        token, token2, cred_error = _api_credential_value(service)
        if cred_error:
            return {'ok': False, 'allowed': False, 'reason': f'Service credential unavailable: {cred_error}'}
    else:
        if method not in ('GET', 'HEAD'):
            return {'ok': False, 'allowed': False, 'reason': 'Write access is only available for hosts in the registered-services list. Use GET/HEAD for unregistered hosts.'}

    spend_kind = (service or {}).get('spend') or {}
    if spend_kind.get('kind') == 'apify' and _apify_budget_exceeded():
        return {'ok': False, 'allowed': False, 'reason': 'The Apify monthly budget is spent. Do not retry; runs are refused until next month.'}
    if _api_daily_calls_exceeded(service):
        return {'ok': False, 'allowed': False, 'reason': 'The daily call budget for this service is spent. Do not retry until the quota refills.'}

    # Sanitize first (strip auth-ish agent headers), then inject registry
    # credential + default headers server-side.
    for h in ('authorization', 'cookie', 'host'):
        req_headers.pop(h, None)
    req_headers = {k: v for k, v in req_headers.items() if k.lower() in ('accept', 'accept-language', 'user-agent')}
    # The allowlist above strips any caller-supplied content-type, and a dict
    # body is sent as JSON -- so say so explicitly. Services like Tavily 422 a
    # JSON body sent without the header (confirmed live: HTTP 422
    # "Input should be a valid dictionary or object to extract fields from").
    if req_body is not None and not any(h.lower() == 'content-type' for h in req_headers):
        req_headers['Content-Type'] = 'application/json'
    if registered and token is not None:
        try:
            url, req_headers, req_body = _api_apply_auth(service, token, token2, method, url, req_headers, req_body)
        except Exception as e:
            # Auth injection can fail (e.g. the OAuth2 token mint refused or the
            # endpoint errored). The request must NOT go out unauthenticated --
            # fail cleanly, same shape as a blocked request.
            return {'ok': False, 'allowed': True, 'error': f'Approved, but auth could not be applied: {e}'}
    for hdr_name, hdr_value in ((service or {}).get('default_headers') or {}).items():
        req_headers.setdefault(hdr_name, hdr_value)

    quote_path = parsed.path
    if workflow and (service or {}).get('workflow'):
        quote_path = ((service.get('workflow') or {}).get('submit') or {}).get('path_template') or quote_path
    try:
        price_quote = estimate_service_spec_cost(service, quote_path, req_body)
    except Exception:
        price_quote = None
    if workflow and (service or {}).get('workflow'):
        try:
            wf = _api_execute_workflow(service, spend_kind, url, method, req_headers, req_body, workflow_params, agent_id=agent_id)
        except Exception as e:
            return {'ok': False, 'allowed': True, 'error': f'Approved, but the workflow failed: {e}'}
        parsed_json, ctype, wrapped, instruction = _api_build_text(
            {'body': json.dumps(wf['data']) if wf['data'] is not None else '', 'contentType': wf.get('contentType', 'application/json'),
             'truncated': False, 'status': 200, 'finalUrl': url}, service, True, parsed.hostname)
        price_line = _api_price_line(price_quote)
        if price_line:
            instruction = (instruction + '\n' + price_line).strip()
        try:
            record_service_price_observation(
                _api_service_id(service), quote_path,
                (price_quote or {}).get('usd'), wf.get('actualUsd'),
                (price_quote or {}).get('basis'), 'api_call', agent_id)
        except Exception:
            pass
        return {'ok': True, 'allowed': True, 'textForModel': wrapped, 'modelInstruction': instruction,
                'status': 200, 'service': (service or {}).get('name'), 'data': wf['data'], 'wrapped_json': wrapped,
                'ids': wf.get('ids') or {}, 'usd': wf.get('usd'), 'actualUsd': wf.get('actualUsd'),
                'priceQuote': price_quote, 'finalUrl': url}

    try:
        result = _api_request_sync(method, url, req_headers, req_body)
    except Exception as e:
        return {'ok': False, 'allowed': True, 'error': f'Approved, but the request failed: {e}'}

    parsed_json, ctype, wrapped, instruction = _api_build_text(result, service, registered, parsed.hostname)
    price_line = _api_price_line(price_quote)
    if price_line:
        instruction = (instruction + '\n' + price_line).strip()
    actual_usd = _api_actual_usd(parsed_json)
    try:
        record_service_price_observation(
            _api_service_id(service), parsed.path,
            (price_quote or {}).get('usd'), actual_usd,
            (price_quote or {}).get('basis'), 'api_call', agent_id)
    except Exception:
        pass
    _api_accrue(spend_kind, ctype, parsed_json)
    _api_accrue_daily_call(service)
    return {'ok': True, 'allowed': True, 'textForModel': wrapped, 'modelInstruction': instruction,
            'status': result['status'], 'service': (service or {}).get('name'), 'data': parsed_json, 'wrapped_json': wrapped,
            'priceQuote': price_quote, 'actualUsd': actual_usd, 'finalUrl': result.get('finalUrl') or url}


def _api_call_response(agent_id, authorized, out, trace_id=None, url='', method='', purpose=''):
    """Map an _api_execute result dict onto the /api/api-call HTTP response,
    logging each outcome exactly once."""
    if out.get('allowed') is False:
        log_action(agent_id, 'api-call', {'url': url, 'method': method, 'purpose': purpose, 'decision': 'blocked', 'reason': out.get('reason')}, authorized=authorized, trace_id=trace_id)
        return JSONResponse({'allowed': False, 'reason': out.get('reason')})
    if out.get('ok') is False:
        log_action(agent_id, 'api-call', {'url': url, 'method': method, 'purpose': purpose, 'decision': 'allowed_but_failed', 'reason': out.get('error')}, authorized=authorized, trace_id=trace_id)
        return JSONResponse({'allowed': True, 'error': out.get('error')})
    log_action(agent_id, 'api-call', {'url': url, 'method': method, 'purpose': purpose, 'decision': 'allowed', 'status': out.get('status'), 'service': out.get('service'), 'channel': out.get('channel'), 'backend': out.get('backend'), 'estimateUsd': (out.get('priceQuote') or {}).get('usd')}, authorized=authorized, trace_id=trace_id)
    return JSONResponse({'allowed': True, 'textForModel': out.get('textForModel'), 'modelInstruction': out.get('modelInstruction'), 'status': out.get('status')})


# ---- api_call channels: per-channel probe (agent-reach doctor mirror) --------
# A "doctor" for the read-only channels, mirroring agent-reach's `doctor`
# command: for each channel, probe each backend in the ordered chain and report
# which backend currently answers, so the player can see what the channel is
# actually routing through today (and which backends are down). Player-only --
# it makes real outbound calls, so it must not be callable by agents.
CHANNEL_PROBE_TIMEOUT_S = 8


def _probe_backend_sync(backend, probe_url):
    """One backend probe: build the fetch URL from the channel's probe_url,
    do a lightweight GET, return {ok, status, error}. Never raises."""
    bservice_id = backend.get('service')
    bservice = _API_SERVICES.get(bservice_id)
    if not bservice:
        return {'service': bservice_id, 'ok': False, 'error': 'backend service not registered'}
    try:
        url = _api_backend_url(backend, probe_url)
    except Exception as e:
        return {'service': bservice_id, 'ok': False, 'error': f'bad url template: {e}'}
    bparsed = urllib.parse.urlparse(url)
    if bparsed.scheme not in ('http', 'https') or not bparsed.hostname or not _is_safe_public_host(bparsed.hostname):
        return {'service': bservice_id, 'ok': False, 'error': 'probe target not a safe public URL'}
    try:
        req = urllib.request.Request(url, method='GET')
        req.add_header('User-Agent', 'AIThinkTankChannelProbe/1.0')
        with _safe_urlopen(req, timeout=CHANNEL_PROBE_TIMEOUT_S) as resp:
            resp.read(4096)
            status = resp.status
        return {'service': bservice_id, 'ok': status < 500, 'status': status, 'url': url}
    except Exception as e:
        return {'service': bservice_id, 'ok': False, 'error': str(e)[:200]}


def _probe_all_channels_sync():
    """Probe every configured channel's backend chain. Returns {channel_id: {...}}
    where the first backend that answers is 'current'."""
    results = {}
    for chost, channel in sorted(_API_CHANNELS.items()):
        cid = channel.get('id') or chost
        if cid in results:
            continue
        probe_url = (channel.get('probe_url') or '').strip()
        if not probe_url:
            results[cid] = {'name': channel.get('name'), 'ok': False, 'error': 'no probe_url configured', 'backends': []}
            continue
        backends = [_probe_backend_sync(b, probe_url) for b in (channel.get('backends') or [])]
        current = next((b.get('service') for b in backends if b.get('ok')), None)
        results[cid] = {
            'name': channel.get('name'),
            'hosts': channel.get('hosts') or [],
            'probe_url': probe_url,
            'ok': current is not None,
            'current': current,
            'backends': backends,
        }
    return results


@app.get('/api/channel-probe')
async def channel_probe(request: Request):
    """Player-only doctor for the read-only channels: reports, per channel,
    which backend in the ordered chain currently answers (agent-reach's
    `doctor` mirror). Makes real outbound GETs to each backend's probe URL, so
    it is session-gated (player) and rate-limited like every other outbound
    tool -- agents never call this."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    if not check_rate_limit('channel-probe'):
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if _API_CHANNELS_LOAD_ERROR:
        return JSONResponse({'error': f'channel registry failed to load: {_API_CHANNELS_LOAD_ERROR}'}, status_code=500)
    log_action('player', 'channel-probe', {'channels': len(_API_CHANNELS)})
    results = await asyncio.to_thread(_probe_all_channels_sync)
    return JSONResponse({'channels': results})


@app.get('/api/ais/recent')
async def ais_recent(request: Request):
    """Read the AISStream collector's buffered maritime feed (see the
    read_ais_feed tool). Any valid agent key or player session may read it --
    it is buffered, read-only, and never exposes the API key. Returns
    connection status, buffer health, and the most recent frames within the
    window."""
    agent_id = (request.query_params.get('agentId') or '').strip()
    presented = request.headers.get('X-Agent-Key') or ''
    authed = (agent_id and verify_agent_key(agent_id, presented) is True)
    if not authed:
        authed = _require_player_session(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    max_msgs = max(1, min(int(request.query_params.get('max') or 20), 100))
    window_s = max(10, min(int(request.query_params.get('window_s') or 1800), 86400))
    status = _ws_status('aisstream')
    return JSONResponse({
        'ok': True,
        'connected': status.get('connected'),
        'lastError': status.get('last_error'),
        'connectedAt': status.get('connected_at'),
        'lastMessageAt': status.get('last_tick_at'),
        'frames': _ws_recent('aisstream', max_items=max_msgs, window_s=window_s),
    })


@app.get('/api/rss/recent')
async def rss_recent(request: Request):
    """Read the RSS aggregator's buffered items for a feed (or all feeds).
    Same auth + read-only contract as /api/ais/recent: agent key or player
    session, returns only buffered public feed data."""
    agent_id = (request.query_params.get('agentId') or '').strip()
    presented = request.headers.get('X-Agent-Key') or ''
    authed = (agent_id and verify_agent_key(agent_id, presented) is True)
    if not authed:
        authed = _require_player_session(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    feed_id = (request.query_params.get('feed') or '').strip().lower()
    if feed_id and feed_id not in _RSS_FEEDS:
        return JSONResponse({'error': f'unknown feed: {feed_id}',
                             'available': sorted(_RSS_FEEDS)}, status_code=400)
    max_items = max(1, min(int(request.query_params.get('max') or 10), 30))
    feeds = [feed_id] if feed_id else sorted(_RSS_FEEDS)
    return JSONResponse({
        'ok': True,
        'feeds': {fid: {
            'name': _RSS_FEEDS[fid]['name'],
            'status': _RSS_STATUS.get(fid) or {},
            'items': _rss_recent(fid, max_items=max_items),
        } for fid in feeds},
    })


@app.get('/api/market/quote')
async def market_quote(request: Request):
    """Read the Kraken collector's latest crypto quotes. Same auth + read-only
    contract as /api/ais/recent."""
    agent_id = (request.query_params.get('agentId') or '').strip()
    presented = request.headers.get('X-Agent-Key') or ''
    authed = (agent_id and verify_agent_key(agent_id, presented) is True)
    if not authed:
        authed = _require_player_session(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    pair = _kraken_normalize_pair(request.query_params.get('pair'))
    status = _ws_status('kraken')
    return JSONResponse({
        'ok': True,
        'connected': status.get('connected'),
        'lastError': status.get('last_error'),
        'lastTickAt': status.get('last_tick_at'),
        'quotes': _ws_latest('kraken', [pair] if pair else None),
    })


@app.get('/api/ws/recent')
async def ws_recent(request: Request):
    """Read buffered records from ANY registered real-time feed service (the
    generic view behind the read_live_feed tool). Same auth + read-only
    contract as /api/ais/recent: any valid agent key or player session."""
    agent_id = (request.query_params.get('agentId') or '').strip()
    presented = request.headers.get('X-Agent-Key') or ''
    authed = (agent_id and verify_agent_key(agent_id, presented) is True)
    if not authed:
        authed = _require_player_session(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    service = (request.query_params.get('service') or '').strip().lower()
    if service and service not in _WS_FEEDS:
        return JSONResponse({'error': f'unknown service: {service}',
                             'available': sorted(_WS_FEEDS)}, status_code=400)
    topic = (request.query_params.get('topic') or '').strip() or None
    max_items = max(1, min(int(request.query_params.get('max') or 10), 50))
    services = [service] if service else sorted(_WS_FEEDS)
    return JSONResponse({
        'ok': True,
        'services': {sid: {
            'name': (_WS_FEEDS.get(sid) or {}).get('name'),
            'status': _ws_status(sid),
            'records': _ws_recent(sid, max_items=max_items, topic=topic),
        } for sid in services},
    })


def _feed_registry_status(table, value, pk_col='id'):
    """Current status row ('active'/'proposed'/'denied'/'removed') for a row in
    one of the operator registries, or None when unknown. Table and pk_col are
    fixed internal constants, never user input."""
    try:
        with _db() as conn:
            row = conn.execute(
                f'SELECT status FROM {table} WHERE {pk_col} = ?',  # nosec B608 -- fixed internal table/column constants
                (value,)).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001 -- fail closed to None (unknown)
        return None


def _operator_gate(request):
    """Registry-management gate: a valid PLAYER session, OR an agent who is a
    director/admin (any agent with direct reports, per _is_director). Returns
    (authorized, actor)."""
    if _require_player_session(request):
        return True, 'player'
    actor = _resolve_requester(request)
    if actor:
        try:
            state = get_state_from_db() or {}
        except Exception:  # noqa: BLE001 -- state unavailable -> not a director
            state = {}
        if _is_director_or_admin(state, actor):
            return True, actor
    return False, None


@app.get('/api/data-sources')
async def get_data_sources(request: Request):
    """Current benchmark data-source registry (the external URLs the daily
    model-tier refresh fetches evaluator data from). Operator-gated read --
    same gate as the other registries: player session or director/admin agent.
    Returns the known KEYS (a fixed code contract) alongside whatever values
    an operator has configured, so a fresh registry self-documents what to
    fill in."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    with _db() as conn:
        rows = conn.execute('SELECT key, value, updated_by, updated_at FROM data_sources').fetchall()
    return JSONResponse({
        'ok': True,
        'knownKeys': list(DATA_SOURCE_KEYS),
        'sources': {k: {'value': v, 'updatedBy': u, 'updatedAt': t} for k, v, u, t in rows},
        'actor': actor,
    })


@app.put('/api/data-sources')
async def put_data_sources(request: Request):
    """Upload a fresh benchmark data-source document. Body:
    {"sources": {"aa_leaderboard_url": "https://...", ...}}. Validated against
    the known keys and http(s) URL shape; applied to the registry immediately
    -- the next model-tier refresh reads the new value, no restart, no cache to
    invalidate. Gated by _operator_gate: a player or a director/admin agent.
    Every change is attributed in action_log so the operator who moved a
    link is on record."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- malformed body -> 400
        return JSONResponse({'error': 'request body must be JSON'}, status_code=400)
    updates = body.get('sources') or {}
    if not isinstance(updates, dict):
        return JSONResponse({'error': 'sources must be an object of {key: url}'}, status_code=400)
    applied = {}
    now = time.time()
    for key, value in updates.items():
        if key not in DATA_SOURCE_KEYS:
            return JSONResponse({'error': f'unknown data source key: {key}', 'knownKeys': list(DATA_SOURCE_KEYS)}, status_code=400)
        if not isinstance(value, str) or not value.strip():
            return JSONResponse({'error': f'{key} must be a non-empty URL string'}, status_code=400)
        value = value.strip()
        if not re.match(r'^https?://[^\s]+$', value):
            return JSONResponse({'error': f'{key} must be an http(s) URL'}, status_code=400)
        applied[key] = value
    with _db() as conn:
        for key, value in applied.items():
            conn.execute(
                'INSERT INTO data_sources (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at',
                (key, value, actor, now),
            )
    log_action(actor, 'data_sources_updated', {'sources': applied})
    return JSONResponse({'ok': True, 'updatedBy': actor, 'sources': applied})


@app.post('/api/rss/propose')
async def rss_propose(request: Request):
    """Agent/player-facing proposal of a new RSS feed. Validates the URL
    (public https only) and PROBES it live (fetch + parse once) so a dead or
    non-XML URL is caught before it is ever approved. Inserts a 'proposed'
    row; a director/admin then approves or denies via /api/rss/approve."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'player').strip() or 'player'
    spec = body.get('feed') or {}
    purpose = (body.get('purpose') or '').strip()
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'rss-propose'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if not (verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is True
            or _require_player_session(request)):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    fid, normalized, error = _validate_rss_feed_spec(spec)
    if error:
        return JSONResponse({'error': error}, status_code=400)
    existing = _feed_registry_status('rss_feeds', fid)
    if existing == 'active':
        return JSONResponse({'error': f'feed "{fid}" is already active'}, status_code=409)
    if existing == 'proposed':
        return JSONResponse({'error': f'a proposal for feed "{fid}" is already pending review'}, status_code=409)
    probe = await asyncio.to_thread(_probe_rss_url, normalized['url'])
    if not probe['ok']:
        return JSONResponse({'error': f'feed URL failed a live probe: {probe["error"]}',
                             'probe': probe}, status_code=422)
    _rss_feed_upsert(normalized, status='proposed', proposed_by=agent_id)
    log_action(agent_id, 'rss_feed_propose', {'feedId': fid, 'name': normalized['name'],
                                              'url': normalized['url'], 'probe': probe,
                                              'purpose': purpose[:200]})
    return JSONResponse({'requested': True, 'feedId': fid, 'probe': probe,
                         'message': f'Proposal for feed "{fid}" filed (probe saw {probe["itemCount"]} items). '
                                    'A director/admin approves or denies it via /api/rss/approve.'})


@app.post('/api/rss/approve')
async def rss_approve(request: Request):
    """Director/admin/player approval or denial of a proposed RSS feed. The
    spec is re-validated at approval time (a forged proposal cannot grant more
    than the validator allows); an approved feed goes live on the poll loop's
    next pass with no restart."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a director, the admin, or the player may approve RSS feeds'}, status_code=403)
    body = await request.json()
    fid = (body.get('feedId') or '').strip().lower()
    decision = (body.get('decision') or 'approve').strip().lower()
    if not fid:
        return JSONResponse({'error': 'feedId is required'}, status_code=400)
    existing = _feed_registry_status('rss_feeds', fid)
    if existing is None:
        return JSONResponse({'error': f'no feed row "{fid}"'}, status_code=404)
    if existing == 'active':
        return JSONResponse({'error': f'feed "{fid}" is already active'}, status_code=409)
    try:
        with _db() as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute('SELECT * FROM rss_feeds WHERE id = ?', (fid,)).fetchone())
    except Exception as e:  # noqa: BLE001 -- never approve on a DB hiccup
        return JSONResponse({'error': f'db unavailable: {e}'}, status_code=503)
    spec = _rss_feed_row_to_spec(row)
    _fid, normalized, error = _validate_rss_feed_spec(spec)
    if error:
        return JSONResponse({'error': f'proposal no longer validates: {error}'}, status_code=400)
    status = 'active' if decision != 'deny' else 'denied'
    _rss_feed_upsert(normalized, status=status, proposed_by=actor)
    log_action(actor, 'rss_feed_approve', {'feedId': fid, 'status': status})
    return JSONResponse({'ok': True, 'feedId': fid, 'status': status})


@app.post('/api/rss/remove')
async def rss_remove(request: Request):
    """Director/admin/player removal of an RSS feed. Sets the row to 'removed';
    the poll loop skips it on the next pass with no restart."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a director, the admin, or the player may remove RSS feeds'}, status_code=403)
    body = await request.json()
    fid = (body.get('feedId') or '').strip().lower()
    if not fid:
        return JSONResponse({'error': 'feedId is required'}, status_code=400)
    if _feed_registry_status('rss_feeds', fid) is None:
        return JSONResponse({'error': f'no feed row "{fid}"'}, status_code=404)
    _rss_feed_upsert({'id': fid, 'name': fid, 'url': 'https://removed.invalid/', 'interval_s': 600},
                     status='removed', proposed_by=actor)
    log_action(actor, 'rss_feed_remove', {'feedId': fid})
    return JSONResponse({'ok': True, 'feedId': fid, 'status': 'removed'})


@app.get('/api/rss/feeds')
async def rss_feeds_list(request: Request):
    """Operator management view of the RSS registry: every row with its status,
    not just the active ones. Director/admin/player gated."""
    authorized, _actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        with _db() as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                'SELECT id, name, url, interval_s, status, proposed_by, updated_at '
                'FROM rss_feeds ORDER BY id')]
    except Exception as e:  # noqa: BLE001
        return JSONResponse({'error': f'db unavailable: {e}'}, status_code=503)
    return JSONResponse({'feeds': rows})


@app.post('/api/ws/propose')
async def ws_propose(request: Request):
    """Agent/player-facing proposal of a new WebSocket feed. Validates the
    declarative descriptor (public wss:// URL, credential source, frame filter,
    field paths). Inserts a 'proposed' row; a director/admin approves via
    /api/ws/approve. There is no live probe here -- a WebSocket probe would
    itself connect and subscribe (rate-limit risk), so approval relies on the
    validator and the collector's live status."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'player').strip() or 'player'
    spec = body.get('feed') or {}
    purpose = (body.get('purpose') or '').strip()
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'ws-propose'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if not (verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is True
            or _require_player_session(request)):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    sid, normalized, error = _validate_ws_feed_spec(spec)
    if error:
        return JSONResponse({'error': error}, status_code=400)
    existing = _feed_registry_status('ws_feeds', sid)
    if existing == 'active':
        return JSONResponse({'error': f'feed "{sid}" is already active'}, status_code=409)
    if existing == 'proposed':
        return JSONResponse({'error': f'a proposal for feed "{sid}" is already pending review'}, status_code=409)
    _ws_feed_upsert(normalized, status='proposed', proposed_by=agent_id)
    log_action(agent_id, 'ws_feed_propose', {'feedId': sid, 'name': normalized['name'],
                                             'url': (normalized.get('ws') or {}).get('url'),
                                             'purpose': purpose[:200]})
    return JSONResponse({'requested': True, 'feedId': sid,
                         'message': f'Proposal for feed "{sid}" filed. A director/admin approves '
                                    'or denies it via /api/ws/approve.'})


@app.post('/api/ws/approve')
async def ws_approve(request: Request):
    """Director/admin/player approval or denial of a proposed WebSocket feed.
    Re-validated at approval time; the supervisor picks up an approved feed
    within seconds and its collector connects -- no restart."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a director, the admin, or the player may approve WebSocket feeds'}, status_code=403)
    body = await request.json()
    sid = (body.get('feedId') or '').strip().lower()
    decision = (body.get('decision') or 'approve').strip().lower()
    if not sid:
        return JSONResponse({'error': 'feedId is required'}, status_code=400)
    existing = _feed_registry_status('ws_feeds', sid)
    if existing is None:
        return JSONResponse({'error': f'no feed row "{sid}"'}, status_code=404)
    if existing == 'active':
        return JSONResponse({'error': f'feed "{sid}" is already active'}, status_code=409)
    try:
        with _db() as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute('SELECT * FROM ws_feeds WHERE id = ?', (sid,)).fetchone())
    except Exception as e:  # noqa: BLE001 -- never approve on a DB hiccup
        return JSONResponse({'error': f'db unavailable: {e}'}, status_code=503)
    spec = _ws_feed_row_to_spec(row)
    _sid, normalized, error = _validate_ws_feed_spec(spec)
    if error:
        return JSONResponse({'error': f'proposal no longer validates: {error}'}, status_code=400)
    status = 'active' if decision != 'deny' else 'denied'
    _ws_feed_upsert(normalized, status=status, proposed_by=actor)
    log_action(actor, 'ws_feed_approve', {'feedId': sid, 'status': status})
    return JSONResponse({'ok': True, 'feedId': sid, 'status': status})


@app.post('/api/ws/remove')
async def ws_remove(request: Request):
    """Director/admin/player removal of a WebSocket feed. Sets the row to
    'removed'; the supervisor cancels its collector task within seconds -- no
    restart."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a director, the admin, or the player may remove WebSocket feeds'}, status_code=403)
    body = await request.json()
    sid = (body.get('feedId') or '').strip().lower()
    if not sid:
        return JSONResponse({'error': 'feedId is required'}, status_code=400)
    if _feed_registry_status('ws_feeds', sid) is None:
        return JSONResponse({'error': f'no feed row "{sid}"'}, status_code=404)
    _ws_feed_upsert({'id': sid, 'name': sid, 'credential': {'type': 'none'},
                     'ws': {'url': 'wss://invalid.invalid', 'subscribe': {},
                            'frame_filter': {'data_kind': 'dict', 'fields': {'x': 'x'}},
                            'buffer': {'kind': 'latest_per_topic'}}},
                    status='removed', proposed_by=actor)
    log_action(actor, 'ws_feed_remove', {'feedId': sid})
    return JSONResponse({'ok': True, 'feedId': sid, 'status': 'removed'})


@app.get('/api/ws/feeds')
async def ws_feeds_list(request: Request):
    """Operator management view of the WebSocket registry: every row with its
    status and descriptor (credential keys redacted). Director/admin/player
    gated."""
    authorized, _actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        with _db() as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                'SELECT id, name, credential_type, credential_key, ws_spec, status, proposed_by, updated_at '
                'FROM ws_feeds ORDER BY id')]
    except Exception as e:  # noqa: BLE001
        return JSONResponse({'error': f'db unavailable: {e}'}, status_code=503)
    for r in rows:
        if r.get('credential_key'):
            r['credential_key'] = f'{r["credential_key"][:3]}...'
    return JSONResponse({'feeds': rows})


@app.post('/api/api-call')
async def api_call(request: Request):
    """Generic, registry-driven external API access for agents -- the
    Muse-style path for any service, bounded by the registry instead of a
    hardcoded adapter. Unregistered hosts get read-only GET/HEAD through the
    same Jev gate as /api/browse. Registered hosts (which include the five
    curated services) get credential injection, constrained methods/paths,
    spend accrual, and optionally a config-driven workflow -- registration is
    itself the approval, so registered hosts skip the Jev round trip. Auth
    comes from the registry, never from anything the caller supplies --
    agent-supplied Authorization/Cookie/Host headers are stripped. Every
    response is redacted and wrapped as untrusted external DATA before it
    reaches a model. Rate-limited and logged like every other outbound tool."""
    if not API_CALL_ENABLED:
        return JSONResponse({'error': 'Generic API access is disabled (API_CALL_ENABLED=false in .env)'}, status_code=403)
    if _API_SERVICES_LOAD_ERROR:
        return JSONResponse({'error': f'api service registry failed to load: {_API_SERVICES_LOAD_ERROR}'}, status_code=500)

    body_json = await request.json()
    agent_id = body_json.get('agentId', 'unknown')
    url = (body_json.get('url') or '').strip()
    method = (body_json.get('method') or 'GET').upper()
    req_headers = dict(body_json.get('headers') or {})
    req_body = body_json.get('body')
    purpose = (body_json.get('purpose') or '').strip()
    # Work-context-aware gating: same rule as /api/browse -- a host the player
    # explicitly named in the current assigned work bypasses Jev like the
    # allowlist; every other unregistered URL is judged with the work context
    # in the Jev prompt, so on-task API calls are allowed instead of blocked
    # for being unfamiliar.
    work_context = (body_json.get('workContext') or '').strip()
    # Player-authored provenance: the work-context host bypass below (a host
    # named in the assigned work skipping Jev like the allowlist) fires ONLY
    # for a task the PLAYER wrote. An agent-authored story can name any URL in
    # its own text -- never trusted for the bypass.
    work_context_trusted = _work_trust_from_body(body_json)
    workflow = bool(body_json.get('workflow'))

    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'api-call'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))

    if not url:
        return JSONResponse({'error': 'url is required'}, status_code=400)
    if method not in CURL_METHODS:
        return JSONResponse({'error': f'method must be one of {sorted(CURL_METHODS)}'}, status_code=400)

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return _api_call_response(agent_id, authorized, {'ok': False, 'allowed': False, 'reason': 'Only http/https URLs are allowed.'}, url=url, method=method, purpose=purpose)
    if not _is_safe_public_host(parsed.hostname):
        return _api_call_response(agent_id, authorized, {'ok': False, 'allowed': False, 'reason': 'That address resolves to a private or internal network location and cannot be reached.'}, url=url, method=method, purpose=purpose)

    # Jev classify only for UNREGISTERED, non-allowlisted hosts -- a registered
    # service went through the proposal/approval flow, so its approved
    # paths/methods are already trusted.
    registered = _api_service_for_host(parsed.hostname) is not None
    trace_id = None
    # Standing safe domains skip Jev on READ-ONLY methods only (GET/HEAD) --
    # a POST/PUT/DELETE to a standing host still goes through Jev like any
    # other unregistered host, because standing access is a read tier, never
    # standing write surface (see STANDING_SAFE_DOMAINS).
    standing_read = method in ('GET', 'HEAD') and _is_standing_safe_host(parsed.hostname)
    if not registered and not _is_allowlisted_host(parsed.hostname) and not standing_read and not (work_context_trusted and _is_work_context_host(work_context, parsed.hostname)):
        criteria = {
            'allow': 'The URL, method, and stated purpose look like ordinary, legal API interaction -- reading a real response, checking raw JSON/HTML, or a registered service call.'
                     + (' A request that is clearly part of, or relevant to, the agent\'s CURRENT assigned work is allowed, provided it does not fall into any blocked category.' if work_context else ''),
            'block': 'The URL, domain, method, body, or stated purpose suggests: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '; or an attempt to submit data, authenticate against, or modify state on a real service without a clear, legitimate, stated reason. A blocked category ALWAYS blocks: relevance to the assigned work or a stated purpose never overrides it -- an agent can describe its work however it likes, and that must not launder an inappropriate site.',
        }
        instructions = (
            f'An in-game agent wants to make a real {method} HTTP request to: {url}\n'
            f'Body: {(json.dumps(req_body) if req_body is not None else "")[:500]}\n'
            f'Stated reason: {purpose or "not given"}\n'
        )
        if work_context:
            instructions += f'The agent is currently working on this assigned task: {work_context[:600]}\n'
        instructions += 'Decide allow or block based on the request, the stated purpose, and the work the request serves (nothing has been sent yet). If the request falls into any blocked category, you MUST block it regardless of work relevance or stated purpose.'
        decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria)
        if not _jev_safety_gate(agent_id, 'api-call', 'This API request', f'{method} {url}', purpose, decision, confidence, cost, authorized, trace_id):
            return _api_call_response(agent_id, authorized, {'ok': False, 'allowed': False, 'reason': 'This request was not approved for a think tank agent to make.'}, url=url, method=method, purpose=purpose, trace_id=trace_id)

    out = await asyncio.to_thread(_api_execute, agent_id, authorized, url, method, req_body, req_headers, purpose, workflow, None, trace_id)
    # Real gap closure, mirroring /api/browse: the redirect chain's FINAL host
    # is network-re-checked inside _api_request_sync but never re-judged for
    # CONTENT. A short link approved by Jev can land on a host it never
    # classified -- re-gate the final url before its response is returned.
    if out.get('allowed') is True and out.get('ok') is True:
        redirect_allowed, redirect_reason = await _jev_re_gate_redirect_final(
            agent_id, 'api-call', 'This API request', url, out.get('finalUrl') or url, purpose,
            work_context, work_context_trusted, authorized, trace_id)
        if not redirect_allowed:
            return _api_call_response(agent_id, authorized, {'ok': False, 'allowed': False, 'reason': redirect_reason},
                                      trace_id=trace_id, url=url, method=method, purpose=purpose)
    return _api_call_response(agent_id, authorized, out, trace_id=trace_id, url=url, method=method, purpose=purpose)


# ---- api_call service registry: proposal / approval ------------------------
# Agents propose new services through an escalation of kind 'api service
# proposal'. That kind is NOT in the floor table, so it takes the default
# delegable floor -- the director loop may decide it (same composite+judge
# cross-check as every delegable kind), and the player is emailed only if the
# director gives up. (An older comment here claimed floor-1.0 human-only --
# the code never had that floor.) On approve the validated spec is written
# into the api_services table as active and the in-memory registry refreshes,
# so the new service is live without a restart. On deny nothing is granted.
def _api_service_spec_to_note(spec):
    """Serialize a validated spec into the escalation's on_approve_note, the
    only cross-boundary carrier (the email link hands it back on approve)."""
    return json.dumps(spec, sort_keys=True)


@app.post('/api/api-service/request')
async def api_service_request(request: Request):
    # Agent-facing proposal of a new registered service. Mirrors
    # /api/allowlist/request: validate, dedupe pending, file a floor-1.0
    # escalation the player resolves by email. An approval ADDS the service to
    # the active registry (status='active'), which is what grants agents
    # credentialed access to its declared paths/methods.
    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    spec = body.get('service') or {}
    purpose = (body.get('purpose') or '').strip()
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'api-service-request'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))

    sid, normalized, error = _validate_api_service_spec(spec)
    if error:
        return JSONResponse({'error': error}, status_code=400)
    note = _api_service_spec_to_note(normalized)
    # One pending proposal per service id at a time -- no escalation spam.
    for esc_id, esc in _load_escalations().items():
        if esc.get('kind') == 'api service proposal' and esc.get('status') == 'pending':
            try:
                existing = json.loads(esc.get('note') or '{}')
            except ValueError:
                continue
            if existing.get('id') == sid:
                return JSONResponse({'requested': True, 'serviceId': sid, 'escalationId': esc_id,
                                     'message': 'an api service proposal for this id is already pending review'})
    esc_id = create_escalation(
        'api service proposal',
        f'Agent {agent_id} proposes registering a new external service for the generic api_call tool.\n'
        f'Service id: {sid}\nName: {normalized.get("name")}\nBase URL: {normalized.get("base_url")}\n'
        f'Auth: {normalized.get("auth", {}).get("type")}\nMethods: {sorted(normalized.get("methods") or [])}\n'
        f'Stated purpose: {purpose or "not given"}\n\n'
        f'Approving adds this service to the active registry, letting agents reach its declared '
        f'paths/methods with the credential injected server-side. A director reviews this proposal '
        f'automatically; the player is only emailed if the director cannot decide.',
        on_approve_note=note,
        agent_id=agent_id,
    )
    log_action(agent_id, 'api_service_request', {'serviceId': sid, 'name': normalized.get('name'),
                                                 'baseUrl': normalized.get('base_url'), 'purpose': purpose[:200],
                                                 'escalationId': esc_id}, authorized=authorized)
    return JSONResponse({'requested': True, 'serviceId': sid, 'escalationId': esc_id,
                         'message': f'Proposal for service "{sid}" filed -- the player decides by email; '
                                    f'until approved, "{sid}" is not registered and only read-only GET/HEAD '
                                    f'to unregistered hosts is available.'})


@app.get('/api/api-service/list')
async def api_service_list():
    # Player-facing view of the registry (active services the agents can reach).
    _ensure_api_services_fresh()
    entries = []
    for sid, spec in _API_SERVICES.items():
        if '.' in sid:  # host-key entry, skip (service-id entries only)
            continue
        entries.append({'id': sid, 'name': spec.get('name'), 'baseUrl': spec.get('base_url'),
                        'auth': (spec.get('auth') or {}).get('type'),
                        'methods': sorted(spec.get('methods') or []),
                        'spend': (spec.get('spend') or {}).get('kind'),
                        'pricing': describe_service_pricing(spec),
                        'description': (spec.get('spend') or {}).get('note'),
                        'hasWorkflow': bool(spec.get('workflow'))})
    return JSONResponse({'services': sorted(entries, key=lambda e: e['id'])})


@app.post('/api/api-service/revoke')
async def api_service_revoke(request: Request):
    # Operator-gated removal: sets the service row to 'denied' so it stops
    # being served, and drops it from the in-memory registry. De-registering
    # a service kills its reachability for the WHOLE colony, so it sits in the
    # same unwind class as allowlist revoke: a player session OR a
    # director/admin agent (the same authority that can APPROVE a proposal
    # via the escalation loop must be able to pull it when something changes).
    # An ordinary agent key is still refused -- a worker cannot revoke
    # GitHub/Apify/Tavily out from under the colony.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    sid = (body.get('serviceId') or '').strip().lower()
    if not sid:
        return JSONResponse({'error': 'serviceId is required'}, status_code=400)
    if sid not in _API_SERVICES:
        return JSONResponse({'error': f'no active service "{sid}"'}, status_code=404)
    _api_service_upsert({'id': sid, 'base_url': _API_SERVICES[sid].get('base_url')},
                        status='denied', proposed_by=actor)
    log_action(actor, 'api_service_revoke', {'serviceId': sid})
    return JSONResponse({'revoked': True, 'serviceId': sid})



def _curl_request_sync(method, url, headers, body):
    data = body.encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    if 'User-Agent' not in {k.title() for k in (headers or {})}:
        req.add_header('User-Agent', 'AIThinkTankAgent/1.0')
    with _safe_urlopen(req, timeout=CURL_TIMEOUT_S) as resp:  # _safe_urlopen pins each resolved IP so DNS cannot rebind to an internal address; only pre-cleared browse/download URLs reach here
        final_url = resp.geturl()
        # Re-check the FINAL host after redirects -- same reasoning as
        # _fetch_page_sync: a redirect is exactly how an allowed-looking
        # URL could still end up pointed at an internal address.
        final_host = urllib.parse.urlparse(final_url).hostname
        if not _is_safe_public_host(final_host):
            raise ValueError('redirected to a disallowed host')
        raw = resp.read(CURL_MAX_BODY_BYTES + 1)
        truncated = len(raw) > CURL_MAX_BODY_BYTES
        raw = raw[:CURL_MAX_BODY_BYTES]
        return {
            'status': resp.status,
            'finalUrl': final_url,
            'headers': dict(resp.headers.items()),
            'body': raw[:CURL_MAX_BODY_BYTES].decode(errors='replace'),
            'truncated': truncated,
        }
