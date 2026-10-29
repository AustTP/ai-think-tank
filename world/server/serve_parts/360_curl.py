from core.review_decode import review_target


@app.post('/api/curl')
async def curl(request: Request):
    # A lower-level sibling to /api/browse -- agents need
    # raw HTTP access (real status codes, real headers, unprocessed
    # HTML/JSON) that /api/browse's text-stripped, human-readable output
    # deliberately never gives. Same "classify before the request goes
    # out" gate, same SSRF host-checking, PLUS a real room restriction
    # /api/browse doesn't have: curl only works for an agent whose last
    # saved position was actually inside the Weather Station.
    if not BROWSING_ENABLED:
        return JSONResponse({'error': 'Agent browsing is disabled (AGENT_BROWSING_ENABLED=false in .env)'}, status_code=403)
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)

    body_json = await request.json()
    agent_id = body_json.get('agentId', 'unknown')
    url = (body_json.get('url') or '').strip()
    method = (body_json.get('method') or 'GET').upper()
    req_headers = body_json.get('headers') or {}
    req_body = body_json.get('body')
    purpose = (body_json.get('purpose') or '').strip()
    # Work-context-aware gating: same rule as /api/browse -- a host the player
    # explicitly named in the current assigned work bypasses Jev like the
    # allowlist; every other URL is judged with the work context in the Jev
    # prompt, so on-task HTTP requests are allowed instead of blocked for
    # being unfamiliar.
    work_context = (body_json.get('workContext') or '').strip()
    # Live room override for the gate below -- the agent's real-time
    # in-room, so a freshly-arrived agent (whose `inRoom` hasn't been
    # autosaved yet) isn't wrongly blocked (see _agent_is_in_weatherstation).
    live_room = body_json.get('inRoom')

    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'curl'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))

    if not (_agent_is_in_weatherstation(agent_id, live_room) or _has_active_temp_access(agent_id, 'curl')):
        log_action(agent_id, 'curl', {'url': url, 'method': method, 'purpose': purpose, 'decision': 'blocked', 'reason': 'not in the Weather Station and no active temporary grant'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'curl only works from inside the Weather Station, or with a real temporary grant from your supervisor (see /api/access/request).'})
    if not url:
        return JSONResponse({'error': 'url is required'}, status_code=400)
    if method not in CURL_METHODS:
        return JSONResponse({'error': f'method must be one of {sorted(CURL_METHODS)}'}, status_code=400)

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        log_action(agent_id, 'curl', {'url': url, 'method': method, 'purpose': purpose, 'decision': 'blocked', 'reason': 'invalid or non-http(s) scheme'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'Only http/https URLs are allowed.'})
    if not _is_safe_public_host(parsed.hostname):
        log_action(agent_id, 'curl', {'url': url, 'method': method, 'purpose': purpose, 'decision': 'blocked', 'reason': 'private/internal host'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'That address resolves to a private or internal network location and cannot be reached.'})

    criteria = {
        'allow': 'The URL, method, and stated purpose look like ordinary, legal HTTP/API interaction -- reading a real response, checking raw HTML/headers, inspecting response structure.'
                 + (' A request that is clearly part of, or relevant to, the agent\'s CURRENT assigned work is allowed, provided it does not fall into any blocked category.' if work_context else ''),
        'block': 'The URL, domain, method, body, or stated purpose suggests: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '; or an attempt to submit data to, authenticate against, or modify state on a real service without a clear, legitimate, stated reason. A blocked category ALWAYS blocks: relevance to the assigned work or a stated purpose never overrides it -- an agent can describe its work however it likes, and that must not launder an inappropriate site.',
    }
    instructions = (
        f'An in-game agent wants to make a real {method} HTTP request to: {url}\n'
        f'Headers: {json.dumps(req_headers)[:500]}\nBody: {(req_body or "")[:500]}\n'
        f'Stated reason: {purpose or "not given"}\n'
    )
    if work_context:
        instructions += f'The agent is currently working on this assigned task: {work_context[:600]}\n'
    instructions += 'Decide allow or block based on the request, the stated purpose, and the work the request serves (nothing has been sent yet). If the request falls into any blocked category, you MUST block it regardless of work relevance or stated purpose.'
    decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria)
    if not _jev_safety_gate(agent_id, 'curl', 'This HTTP request', f'{method} {url}', purpose, decision, confidence, cost, authorized, trace_id):
        return JSONResponse({'allowed': False, 'reason': 'This request was not approved for a think tank agent to make.'})

    # Phase D: a capability handle lets an agent attach a scoped external
    # credential (e.g. an API key) to this request. The handle only authorizes
    # the host/method it was scoped to, the secret is decrypted in-process and
    # injected without ever being returned to the caller, and the room/Jev
    # gates above still run -- a handle is an ADDITIONAL permission, not a
    # bypass. Cross-check attribution: whoever presented this key must also be
    # the handle's grantee.
    capability_handle = (body_json.get('capabilityHandle') or '').strip()
    if capability_handle:
        grant = resolve_capability_handle(agent_id, capability_handle, method, url)
        if grant is None:
            log_action(agent_id, 'curl', {'url': url, 'method': method, 'purpose': purpose,
                                          'decision': 'blocked', 'reason': 'capability handle invalid, expired, or out of scope'}, authorized=authorized, trace_id=trace_id)
            return JSONResponse({'allowed': False, 'reason': 'That capability handle is not valid for this request (expired, wrong agent, or out-of-scope host/method).'})
        # Inject the real credential server-side. The secret never rides in the
        # response or logs. Every use is chained into the hashed-product-passport
        # (a tamper-evident ledger of world-changing decisions), so "who used
        # which key, when, for what, on which host" is auditable and un-rewritable.
        if grant.get('kind') == 'login':
            # A login credential (username + password) is not a request header.
            # It only makes sense through a service-specific login flow, so a
            # handle to one must never be accepted as an api_call attachment.
            log_action(agent_id, 'curl', {'url': url, 'method': method, 'purpose': purpose,
                                          'decision': 'blocked', 'reason': 'login credential cannot be attached as a request header'},
                       authorized=authorized, trace_id=trace_id)
            return JSONResponse({'allowed': False,
                                 'reason': 'That capability handle is for a login credential, which cannot be attached to an HTTP request as a header.'})
        for hdr_name, hdr_value in _capability_auth_headers(grant['credential_name'], grant['secret']).items():
            req_headers.setdefault(hdr_name, hdr_value)
        log_action(agent_id, 'curl', {'url': url, 'method': method, 'purpose': purpose,
                                      'decision': 'allowed', 'credential': grant['credential_name'],
                                      'scope': grant['purpose']}, authorized=authorized, trace_id=trace_id)
        _append_passport_decision('credential_used', agent_id, {
            'credential': grant['credential_name'], 'service': grant.get('service'),
            'scope': grant['purpose'], 'host': urllib.parse.urlparse(url).hostname,
            'method': method, 'url': url[:200]})

    try:
        result = await asyncio.to_thread(_curl_request_sync, method, url, req_headers, req_body)
    except Exception as e:
        log_action(agent_id, 'curl', {'url': url, 'method': method, 'purpose': purpose, 'decision': 'allowed_but_failed', 'reason': str(e)}, authorized=authorized, trace_id=trace_id)
        return JSONResponse({'allowed': True, 'error': f'Approved, but the request failed: {e}'})

    # Real gap closure, mirroring /api/browse: the redirect chain's FINAL host
    # is network-re-checked inside _curl_request_sync but never re-judged for
    # CONTENT. A short link approved by Jev can land on a host it never
    # classified -- re-gate the final url before its body is returned.
    redirect_allowed, redirect_reason = await _jev_re_gate_redirect_final(
        agent_id, 'curl', 'This HTTP request', url, result.get('finalUrl') or url, purpose,
        work_context, False, authorized, trace_id)
    if not redirect_allowed:
        log_action(agent_id, 'curl', {'url': url, 'method': method, 'finalUrl': result.get('finalUrl'),
                                      'purpose': purpose, 'decision': 'blocked', 'reason': redirect_reason},
                   authorized=authorized, trace_id=trace_id)
        return JSONResponse({'allowed': False, 'reason': redirect_reason})

    # Real, if narrow, residual risk (audit): a capability-handle-
    # authenticated request's response used to go back to the agent
    # completely raw. If the target API ever echoes the injected credential
    # (some do, in error/debug responses), that secret would land in the
    # agent's visible tool output -- and from there could get written into
    # the shared, PERSISTENT sandbox (workroom-shared/research-shared),
    # readable by any later, unrelated task. Exactly the "leftover
    # credential in an unrelated file" shape a real incident took. Redact by
    # the EXACT known value when a handle was used (guaranteed precision,
    # not pattern luck), plus the same general _redact_secrets scan
    # /api/library/file already applies to agent-written content.
    result['body'] = _redact_secrets(result['body'])
    if capability_handle and grant and grant.get('secret'):
        secret = grant['secret']
        result['body'] = result['body'].replace(secret, '[REDACTED]')
        result['headers'] = {k: (v.replace(secret, '[REDACTED]') if secret in v else v)
                             for k, v in result['headers'].items()}

    log_action(agent_id, 'curl', {'url': url, 'method': method, 'finalUrl': result['finalUrl'], 'purpose': purpose, 'decision': 'allowed', 'status': result['status'], 'bytes': len(result['body'])}, authorized=authorized, trace_id=trace_id)
    return JSONResponse({'allowed': True, **result})


# "Can the sandbox download files from websites,
# with Jev as judge?" -- yes, but NOT by giving the sandbox itself
# network access. SANDBOX_NETWORK is created `--internal` specifically
# so sandboxed code has no route to the internet at all -- reusing that
# boundary is a feature here, not a limitation to work around. Instead
# this fetches the file SERVER-SIDE (this process has real internet
# access; the sandbox container never does) through the exact same
# Jev-classify-before-fetching gate library_download() already uses, then
# writes the result straight into the sandbox's own working directory --
# the container sees it at /workspace/downloads/<name> the next time
# /api/execute or /api/pipeline runs there. The sandbox's own code is
# still responsible for treating anything it reads from there as
# untrusted input, same discipline wrap_external_content already expects
# of browsed page text -- Jev judges whether the REQUEST (url + stated
# purpose) is legitimate, not what's actually inside the file, which
# isn't something a classifier can meaningfully do for arbitrary content
# before it's even been fetched. Works from either sandboxed room --
# Observatory (research) or Work Room --
# `sandboxId` just says which one to write into.
SANDBOX_DOWNLOAD_MAX_BYTES = 20_000_000


@app.post('/api/sandbox-download')
async def sandbox_download(request: Request):
    if not BROWSING_ENABLED:
        return JSONResponse({'error': 'Agent browsing is disabled (AGENT_BROWSING_ENABLED=false in .env)'}, status_code=403)
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)

    body = await request.json()
    url = (body.get('url') or '').strip()
    agent_id = body.get('agentId', 'unknown')
    sandbox_id = (body.get('sandboxId') or '').strip()
    sandbox_id = _village_sandbox_id(agent_id, sandbox_id)  # per-village repo isolation
    if not _SANDBOX_ID_RE.match(sandbox_id or ''):
        return JSONResponse({'error': 'invalid sandboxId'}, status_code=400)
    purpose = (body.get('purpose') or '').strip()
    filename = (body.get('filename') or '').strip()
    # Live room override (see _agent_is_in_sandbox_room) -- an agent that
    # just arrived in a sandboxed room hasn't had its `inRoom` autosaved yet,
    # so the first download right after arrival would otherwise be wrongly
    # blocked by the stale-state gate.
    live_room = body.get('inRoom')

    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'sandbox-download'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))

    if not (_agent_is_in_sandbox_room(agent_id, live_room) or _has_active_temp_access(agent_id, 'sandbox-download')):
        log_action(agent_id, 'sandbox_download', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': 'not in the Work Room/Observatory and no active temporary grant'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'Downloading into a sandbox only works from inside the Work Room or the Observatory, or with a real temporary grant from your supervisor (see /api/access/request).'})
    if not url or not filename or not sandbox_id:
        return JSONResponse({'error': 'url, filename, and sandboxId are required'}, status_code=400)

    safe_name = _sanitize_download_filename(filename)
    if not safe_name:
        return JSONResponse({'error': 'filename did not contain any safe characters'}, status_code=400)

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        log_action(agent_id, 'sandbox_download', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': 'invalid or non-http(s) scheme'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'Only http/https URLs are allowed.'})
    if not _is_safe_public_host(parsed.hostname):
        log_action(agent_id, 'sandbox_download', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': 'private/internal host'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'That address resolves to a private or internal network location and cannot be reached.'})

    criteria = {
        'allow': 'The URL and stated purpose look like an ordinary, legal file download the research team would actually need to work with (a dataset, paper, reference tool, or similar).',
        'block': 'The URL, domain, or stated purpose suggests: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '.',
    }
    instructions = (
        f'An in-game agent wants to download this file DIRECTLY INTO their research sandbox, where it (or code reacting to it) will actually run: {url}\n'
        f'Stated reason: {purpose or "not given"}\nDecide allow or block based on the URL/domain and stated purpose alone (the file has not been fetched yet).'
    )
    decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria)

    if not _jev_safety_gate(agent_id, 'sandbox_download', 'This sandbox download', url, purpose, decision, confidence, cost, authorized, trace_id):
        return JSONResponse({'allowed': False, 'reason': 'This file was not approved to download into the sandbox.'})

    try:
        final_url, content_type, raw, truncated = await asyncio.to_thread(_download_file_sync, url, SANDBOX_DOWNLOAD_MAX_BYTES)
    except Exception as e:
        log_action(agent_id, 'sandbox_download', {'url': url, 'purpose': purpose, 'decision': 'allowed_but_fetch_failed', 'reason': str(e)}, authorized=authorized, trace_id=trace_id)
        return JSONResponse({'allowed': True, 'ok': False, 'reason': str(e)})

    # Real gap closure, mirroring /api/browse: the redirect chain's FINAL host
    # is network-re-checked inside _download_file_sync but never re-judged for
    # CONTENT. A short link approved by Jev can land on a host it never
    # classified -- re-gate the final url before the file enters the sandbox.
    redirect_allowed, redirect_reason = await _jev_re_gate_redirect_final(
        agent_id, 'sandbox_download', 'This sandbox download', url, final_url, purpose,
        '', False, authorized, trace_id)
    if not redirect_allowed:
        log_action(agent_id, 'sandbox_download', {'url': url, 'finalUrl': final_url, 'purpose': purpose,
                                                  'sandboxId': sandbox_id, 'decision': 'blocked', 'reason': redirect_reason},
                   authorized=authorized, trace_id=trace_id)
        return JSONResponse({'allowed': False, 'reason': redirect_reason})

    sandbox_dir = _sandbox_dir_for(sandbox_id)
    dest_dir = os.path.join(sandbox_dir, 'downloads')
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, safe_name)
    with open(dest_path, 'wb') as f:
        f.write(raw)

    rel_path = f'downloads/{safe_name}'  # relative to /workspace, exactly where the sandbox container will see it
    log_action(agent_id, 'sandbox_download', {
        'url': url, 'finalUrl': final_url, 'purpose': purpose, 'sandboxId': sandbox_id,
        'decision': 'allowed', 'path': rel_path, 'bytes': len(raw), 'truncated': truncated, 'contentType': content_type,
    }, authorized=authorized, trace_id=trace_id)
    return JSONResponse({'allowed': True, 'ok': True, 'path': rel_path, 'bytes': len(raw), 'truncated': truncated, 'contentType': content_type})


SANDBOX_SAVE_MAX_BYTES = 2_000_000  # a page's extracted text, not a binary download -- SANDBOX_DOWNLOAD_MAX_BYTES's 20MB would be generous to the point of pointless here


@app.post('/api/sandbox-save-page')
async def sandbox_save_page(request: Request):
    # The persist half of a real multi-page "collect and save" tool, modeled on
    # the Desktop bug_bounty's
    # page-collection feature. The FETCH half already exists and stays
    # untouched -- /api/browse already Jev-classifies each URL, SSRF-checks
    # it, fetches it, and extracts text/links -- so this only adds the
    # missing piece: writing an already-fetched page's text into a
    # sandbox. It does NOT fetch anything itself and has no new network
    # surface.
    #
    # Still re-runs its own Jev url+purpose classification on the same
    # url/purpose the content came from, even though /api/browse already
    # approved that exact URL once. Skipping this would make the endpoint
    # a way to write arbitrary, unreviewed text into a sandbox just by
    # claiming it came from a browse call -- every other gate in this
    # file re-checks independently rather than trusting a caller's prior
    # approval (see sandbox_download, browse, library_download above),
    # and this should too.
    if not BROWSING_ENABLED:
        return JSONResponse({'error': 'Agent browsing is disabled (AGENT_BROWSING_ENABLED=false in .env)'}, status_code=403)
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)

    body = await request.json()
    url = (body.get('url') or '').strip()
    agent_id = body.get('agentId', 'unknown')
    sandbox_id = (body.get('sandboxId') or '').strip()
    sandbox_id = _village_sandbox_id(agent_id, sandbox_id)  # per-village repo isolation
    if not _SANDBOX_ID_RE.match(sandbox_id or ''):
        return JSONResponse({'error': 'invalid sandboxId'}, status_code=400)
    purpose = (body.get('purpose') or '').strip()
    filename = (body.get('filename') or '').strip()
    content = body.get('content') or ''
    # Live room override (see _agent_is_in_sandbox_room) -- the exact race
    # you reported: the very first sandbox-save right after an agent arrives
    # in a sandboxed room can fail this gate because it read the agent's
    # stale autosaved `inRoom` instead of the live one. Same fix as
    # sandbox-download/curl: the client's real-time `inRoom` is accepted and
    # validated against the eligible room set.
    live_room = body.get('inRoom')

    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'sandbox-save-page'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))

    if not (_agent_is_in_sandbox_room(agent_id, live_room) or _has_active_temp_access(agent_id, 'sandbox-download')):
        log_action(agent_id, 'sandbox_save_page', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': 'not in the Work Room/Observatory and no active temporary grant'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'Saving a page into a sandbox only works from inside the Work Room or the Observatory, or with a real temporary grant from your supervisor (see /api/access/request).'})
    if not url or not filename or not sandbox_id or not content:
        return JSONResponse({'error': 'url, filename, sandboxId, and content are required'}, status_code=400)

    safe_name = _sanitize_download_filename(filename)
    if not safe_name:
        return JSONResponse({'error': 'filename did not contain any safe characters'}, status_code=400)

    raw = content.encode('utf-8')[:SANDBOX_SAVE_MAX_BYTES]
    truncated = len(content.encode('utf-8')) > SANDBOX_SAVE_MAX_BYTES

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        log_action(agent_id, 'sandbox_save_page', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': 'invalid or non-http(s) scheme'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'Only http/https URLs are allowed.'})
    if not _is_safe_public_host(parsed.hostname):
        log_action(agent_id, 'sandbox_save_page', {'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': 'private/internal host'}, authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': 'That address resolves to a private or internal network location and cannot be reached.'})

    criteria = {
        'allow': 'The URL and stated purpose look like an ordinary, legal page the research team would actually need to keep a copy of (a dataset page, reference doc, article, or similar).',
        'block': 'The URL, domain, or stated purpose suggests: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '.',
    }
    instructions = (
        f'An in-game agent wants to save the text of this already-visited page into their sandbox: {url}\n'
        f'Stated reason: {purpose or "not given"}\nDecide allow or block based on the URL/domain and stated purpose alone.'
    )
    decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria)

    if not _jev_safety_gate(agent_id, 'sandbox_save_page', 'This page save', url, purpose, decision, confidence, cost, authorized, trace_id):
        return JSONResponse({'allowed': False, 'reason': 'This page was not approved to save into the sandbox.'})

    sandbox_dir = _sandbox_dir_for(sandbox_id)
    dest_dir = os.path.join(sandbox_dir, 'downloads')
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, safe_name)
    with open(dest_path, 'wb') as f:
        f.write(raw)

    rel_path = f'downloads/{safe_name}'
    log_action(agent_id, 'sandbox_save_page', {
        'url': url, 'purpose': purpose, 'sandboxId': sandbox_id,
        'decision': 'allowed', 'path': rel_path, 'bytes': len(raw), 'truncated': truncated,
    }, authorized=authorized, trace_id=trace_id)
    return JSONResponse({'allowed': True, 'ok': True, 'path': rel_path, 'bytes': len(raw), 'truncated': truncated})


@app.post('/api/access/request')
async def access_request(request: Request):
    # Real "ask your supervisor" flow: an agent
    # without standing access to something can ask for it, and gets a
    # REAL, TEMPORARY grant if the reason is legitimate -- judged, not
    # rubber-stamped, and not permanent even when approved. The judgment
    # itself is a real Jev classification (the same mechanism every other
    # gate in this think tank already uses for "is this reason legitimate"),
    # framed as the supervisor's call; `supervisorId` just records who it
    # was asked of, for a real, visible mail trail.
    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    supervisor_id = body.get('supervisorId', 'unknown')
    capability = body.get('capability')
    reason = (body.get('reason') or '').strip()
    task_id = (body.get('taskId') or '').strip() or None

    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'access-request'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if capability not in TEMP_ACCESS_CAPABILITIES:
        return JSONResponse({'error': f'capability must be one of {sorted(TEMP_ACCESS_CAPABILITIES)}'}, status_code=400)
    if not reason:
        return JSONResponse({'error': 'reason is required'}, status_code=400)
    # Per-story capability grant: a temp grant must name the SPECIFIC story it's
    # for, and that story must be the agent's own live task (the server checks
    # the real persisted state, not the client's word) -- so the grant dies with
    # the story (revoke_task_access on completion) instead of riding the timer
    # alone. An agent with no live task cannot get a story-scoped grant.
    if task_id:
        st = get_state_from_db()
        live_task = None
        if st:
            live_task = (st.get('agents') or {}).get(agent_id, {}).get('task')
        if live_task != task_id:
            return JSONResponse({'error': 'taskId must be the story the agent is currently working on'}, status_code=400)
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)

    criteria = {
        'approve': f'The stated reason is a specific, legitimate, task-related need for this capability ({TEMP_ACCESS_CAPABILITIES[capability]}) -- not vague, not "just in case."',
        'deny': 'The reason is vague, unrelated to real work, unconvincing, or suggests: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '.',
    }
    instructions = (
        f'An agent is asking their supervisor for TEMPORARY access to "{capability}" ({TEMP_ACCESS_CAPABILITIES[capability]}), '
        f'which they don\'t have by default. Stated reason: {reason}\n'
        f'Decide approve or deny based on whether this is a specific, legitimate, task-related need, not a blanket or unjustified request.'
    )
    decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria)

    if not _jev_safety_gate(agent_id, 'access_request', 'This temp-access grant', f'capability: {capability}', reason, decision, confidence, cost, None, trace_id):
        log_action(agent_id, 'access_request', {'capability': capability, 'reason': reason, 'supervisorId': supervisor_id, 'decision': 'denied'})
        return JSONResponse({'approved': False, 'reason': 'Not approved -- the stated reason did not justify temporary access.'})

    expires_at = _grant_temp_access(agent_id, capability, supervisor_id, reason, task_id=task_id)
    # A story-scoped grant reports "until the story ships", not a timestamp --
    # it dies with revoke_task_access on completion (or with the agent, on
    # firing), never on a clock.
    story_scoped = bool(task_id)
    log_action(agent_id, 'access_request', {'capability': capability, 'reason': reason, 'supervisorId': supervisor_id, 'decision': 'approved', 'expiresAt': expires_at, 'taskId': task_id, 'untilStoryComplete': story_scoped})
    # Approving temporary access is a consequential (privilege) decision --
    # chain it, same ledger as hires/fires/promotions.
    _append_passport_decision('grant_access', supervisor_id, {'agent': agent_id, 'capability': capability, 'reason': reason, 'expiresAt': expires_at, 'taskId': task_id})
    return JSONResponse({'approved': True, 'capability': capability, 'expiresAt': expires_at, 'untilStoryComplete': story_scoped, 'durationS': None if story_scoped else TEMP_ACCESS_DURATION_S, 'taskId': task_id})


async def _classify_command(command, purpose, agent_id='unknown'):
    # Fails closed, same direction as browsing and for the same reason:
    # an unreachable classifier is not consent to run something unvetted.
    if not OPENROUTER_API_KEY:
        return False, 'OPENROUTER_API_KEY not set'
    criteria = {
        'allow': 'Ordinary development/CI work -- running tests, building, linting, installing from a well-known package registry, writing/reading files inside the working directory.',
        'block': 'The command, or its stated purpose, suggests: ' + '; '.join(EXECUTE_BLOCK_CATEGORIES) + '.',
    }
    reviewed = review_target(command)
    # Deterministic host-path refusal before the model is asked. The sandbox
    # mounts the task directory at /workspace and has its own ephemeral /tmp;
    # a host home path is never ordinary work. This also keeps the read path
    # consistent with the write path, which previously allowed a /tmp scratch
    # write and then blocked reading the same file (task-389).
    _host_home = os.path.expanduser('~')
    if _host_home and _host_home != '/' and _host_home in reviewed['text']:
        return False, ('blocked: the command references a host filesystem path outside the sandbox. '
                       'Use /workspace, or the sandbox\'s own /tmp.')
    instructions = (f'An in-game agent in the Work Room wants to run this command: {reviewed["text"]}\n'
                    f'Stated reason: {purpose or "not given"}\n'
                    'The sandbox is a container: /workspace is the task directory and its /tmp is '
                    'ephemeral and internal, not the host filesystem. Decide allow or block.')
    try:
        instructions += '\n' + _jev_actor_framing(get_state_from_db() or {}, agent_id)
    except Exception:
        pass  # framing is presentation, never a gate
    if reviewed['decoded']:
        instructions += ('\n(The command above was decoded from a base64 wrapper for review. '
                         'The original wrapper is what would run.)')
    # Gap fixed: this used to hand-duplicate _jev_safety_
    # gate's own low-confidence-escalation logic (and its own separate,
    # non-quorum-sampled single Jev call) instead of sharing it -- the
    # riskiest primitive in the system (arbitrary shell execution) was on a
    # different, un-shared code path than every other Jev-gated action, so
    # a future fix to the shared gate (quorum sampling, right here) would
    # silently never have reached command execution.
    decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria, kind='cmd_allow')
    if not _jev_safety_gate(agent_id, 'execute_classify', 'This command', command, purpose, decision, confidence, cost, None, trace_id):
        if decision not in ('allow', 'approve'):
            return False, decision or 'classifier unavailable or gave no answer'
        # Low-confidence allow blocked. The old message was just
        # "unsure (Jev confidence 0.16), escalated" -- agents saw a score
        # with no reason and no fix. Name what Jev blocks and what to do.
        _cats = '; '.join(EXECUTE_BLOCK_CATEGORIES)
        return False, (f'Jev was not confident enough to approve this command '
                       f'(confidence {confidence:.2f}, needed {_effective_safety_confidence():.2f}). '
                       f'Jev blocks: {_cats}. If your command is ordinary work, rephrase the stated '
                       f'purpose to say exactly what work task it serves, or simplify the command '
                       f'so its intent is clearer. Do not retry the same command unchanged.')
    return True, 'allow'


# browser_act is a REAL headed-browser form interaction running inside the
# sandbox container -- more consequential than a plain browse_page GET because
# it can submit forms, so it gets its own JEV gate rather than riding browse's.
# Read ops (goto/read/screenshot) on a player-vetted allowlisted host skip Jev
# exactly like browse_page does (the allowlist already stands in for a content
# judgment call there); anything else -- a submit, a fill, or any op on a
# non-allowlisted host -- is classified before it runs. Fail-closed like every
# other gate: an unreachable classifier is not consent to drive a browser.
BROWSER_ACT_READ_OPS = frozenset({'goto', 'read', 'screenshot'})


async def _classify_browser_action(op, url, purpose, agent_id='unknown', work_context='', work_context_trusted=False, handle_scoped_host=False):
    try:
        host = urllib.parse.urlparse(url).hostname or ''
    except Exception:
        host = ''
    # The work-context bypass (a host named in the assigned work skipping Jev
    # like the allowlist) fires ONLY for a task the PLAYER wrote. An
    # agent-authored story can name any URL in its own text -- never trusted
    # for the bypass.
    if op in BROWSER_ACT_READ_OPS and (_is_allowlisted_host(host) or (work_context_trusted and _is_work_context_host(work_context, host))):
        return True, 'allow (allowlisted or explicitly named in the assigned work)'
    # Player-vetted, need-based social read path: the host is in the operator's
    # credential_access.json readOnlyHosts, and the browser_act guard has
    # already refused every write-shaped op. Reads skip Jev exactly like the
    # allowlist; the login submit is allowed only with a fully scoped handle.
    if op in BROWSER_ACT_READ_OPS and _is_readonly_social_host(host):
        return True, 'allow (read-only social host)'
    if op == 'login' and handle_scoped_host and _is_readonly_social_host(host):
        return True, 'allow (read-only social login with player-stored handle)'
    # A fill that carries a VALID, player-minted login capability handle scoped to
    # THIS host skips Jev, same as the allowlist bypass. Reasoning: Jev's
    # credential-submission block exists to stop an agent from typing a secret it
    # holds into an arbitrary/phishing site. Here the agent holds NO secret -- the
    # server already resolved the handle against this exact host (scope-checked,
    # unexpired, right agent) and pulls the value from the encrypted vault
    # (see /api/browser-act's credential-injection block). The credential is
    # player-vetted (in the vault) and the host is player-vetted (allowlisted or
    # named in the assigned work) -- both vetting decisions are the PLAYER's, so
    # letting Jev re-litigate them every fill only reintroduces the false-positive
    # phishing block on the tank's own legitimate login path. `handle_scoped_host`
    # is passed in already-verified by the caller; it is True only when the handle
    # resolved cleanly AND its host scope matches the page host being filled.
    if op in ('fill', 'fill_many', 'login', 'submit', 'click') and handle_scoped_host and (_is_allowlisted_host(host) or (work_context_trusted and _is_work_context_host(work_context, host))):
        return True, 'allow (valid player-minted login handle scoped to this vetted host)'
    if not OPENROUTER_API_KEY:
        return False, 'OPENROUTER_API_KEY not set'
    criteria = {
        'allow': 'An in-game agent filling or submitting a routine form or reading a page on the web for legitimate research or work -- standard web usage a human doing the same research would perform.'
                 + (' A browser action that is clearly part of, or relevant to, the agent\'s CURRENT assigned work is allowed, provided it does not fall into any blocked category.' if work_context else ''),
        'block': 'The browser action, or its stated purpose, suggests: submitting credentials, payment information, or sensitive personal data to an arbitrary third party; exfiltrating the think tank\'s data or an agent\'s findings to an external site; bypassing authentication, anti-bot, or CAPTCHA protections; ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '. A blocked category ALWAYS blocks: relevance to the assigned work or a stated purpose never overrides it -- an agent can describe its work however it likes, and that must not launder an inappropriate site.',
    }
    instructions = (f'An in-game agent wants to drive a real browser ({op}) to: {url or "(current page)"}\n'
                    f'Stated reason: {purpose or "not given"}\n')
    if work_context:
        instructions += f'The agent is currently working on this assigned task: {work_context[:600]}\n'
    instructions += 'Decide allow or block based on the action, the stated purpose, and the work the action serves. If the action falls into any blocked category, you MUST block it regardless of work relevance or stated purpose.'
    decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria)
    if not _jev_safety_gate(agent_id, 'browser_act_classify', 'This browser action', f'{op} {url or ""}', purpose, decision, confidence, cost, None, trace_id):
        if decision not in ('allow', 'approve'):
            return False, decision or 'classifier unavailable or gave no answer'
        # Same actionable-message fix as _classify_command: agents saw a bare
        # confidence score with no reason and no fix path.
        return False, (f'Jev was not confident enough to approve this browser action '
                       f'(confidence {confidence:.2f}, needed {_effective_safety_confidence():.2f}). '
                       f'Jev blocks credential submission, data exfiltration, and CAPTCHA/auth '
                       f'bypass. If this action is ordinary research, rephrase the stated purpose '
                       f'to say exactly what work task it serves. Do not retry the same action unchanged.')
    return True, 'allow'


# Supervisor override for a Jev block (2026-10-11, per the player's directive
# that the tank should self-handle blocks and only page the player for player
# stories, sprint completion, or permanent access grants). When Jev blocks a
# command / pipeline step / browser action, the agent's DIRECTOR gets one
# autonomous review: a NON-Jev model (mid tier -- a binary classification
# task the mid tier handles fine; high tier is reserved for planning. Asking
# Jev again would just rubber-stamp its own block) framed as
# the boss deciding whether the block was a false positive on ordinary work.
# Overrides are logged to action_log AND the tamper-evident passport chain
# (kind 'supervisor_override'), so every bypass of the safety gate is
# attributable. Fails closed on every path: no roster director, no API key,
# no mid tier, an unreachable model, a non-JSON or low-confidence answer
# -- all keep the block.
async def _supervisor_override_decision(agent_id, action_kind, detail, purpose, block_reason, block_categories=None):
    """Ask the agent's real roster director (via a non-Jev model) whether to
    override a Jev block. Returns (overridden: bool, note: str). `detail` is
    the command or browser action; `block_categories` names the hard-blocked
    categories the supervisor is told never to wave through."""
    if not OPENROUTER_API_KEY:
        return False, 'no model key for the supervisor review'
    try:
        state = get_state_from_db()
    except Exception:
        state = None
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])} if state else {}
    me = roster.get(agent_id) or {}
    director_id = me.get('director')
    if not director_id or director_id not in roster:
        # Top of the chain (or an unknown id) -- nobody above them to grant,
        # so the block stands exactly as before.
        return False, 'no supervisor on the roster to review the block'
    director_name = roster[director_id].get('name') or director_id
    agent_name = me.get('name') or agent_id
    model = _mid_tier_slug()
    if not model:
        return False, 'no mid model tier configured for the supervisor review'
    cats = list(block_categories or EXECUTE_BLOCK_CATEGORIES)
    prompt = (
        f'You are {director_name}, the supervisor of {agent_name} at an AI research think tank.\n'
        f'Your subordinate tried this action and the automated safety classifier (Jev) blocked it.\n'
        f'Action ({action_kind}): {review_target(detail)["text"]}\n'
        f'Stated purpose: {purpose or "not given"}\n'
        f"Jev's block reason: {block_reason}\n"
        f'Hard-blocked categories the classifier enforces: {"; ".join(cats)}\n\n'
        f'As the supervisor, decide whether to OVERRIDE the block (let the action proceed) '
        f'or KEEP IT BLOCKED. Override only if this is clearly ordinary, legitimate work '
        f'the classifier misread. An action that matches a blocked category stays blocked '
        f'regardless of the stated purpose. When genuinely unsure, keep the block -- a '
        f'wrong override is the expensive mistake.\n'
        f'Reply with ONLY a JSON object, no other text: '
        f'{{"decision": "override" or "keep_blocked", "confidence": 0.0 to 1.0, "reason": "one sentence"}}'
    )
    try:
        data = await asyncio.to_thread(
            _call_openrouter_once_sync, model,
            [{'role': 'system', 'content': 'You are a hands-on engineering supervisor. Reply with the JSON object only.'},
             {'role': 'user', 'content': prompt}],
            400,
        )
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        parsed = json.loads(reply.strip().replace('```json', '').replace('```', '').strip())
    except Exception as e:
        log_action(director_id, 'supervisor_override', {'agent': agent_id, 'kind': action_kind,
                   'detail': detail[:500], 'purpose': purpose[:300], 'blockReason': str(block_reason)[:300],
                   'decision': 'error', 'error': str(e)[:200], 'model': model})
        return False, f'supervisor review failed ({type(e).__name__}), block stands'
    decision = str(parsed.get('decision') or '').strip().lower()
    try:
        confidence = float(parsed.get('confidence'))
    except (TypeError, ValueError):
        confidence = 0.0
    note = str(parsed.get('reason') or '')[:300]
    if decision != 'override' or confidence < _effective_safety_confidence():
        log_action(director_id, 'supervisor_override', {'agent': agent_id, 'kind': action_kind,
                   'detail': detail[:500], 'purpose': purpose[:300], 'blockReason': str(block_reason)[:300],
                   'decision': 'kept_blocked', 'modelConfidence': confidence, 'note': note, 'model': model})
        return False, note or 'supervisor kept the block'
    log_action(director_id, 'supervisor_override', {'agent': agent_id, 'kind': action_kind,
               'detail': detail[:500], 'purpose': purpose[:300], 'blockReason': str(block_reason)[:300],
               'decision': 'overridden', 'modelConfidence': confidence, 'note': note, 'model': model})
    _append_passport_decision('supervisor_override', director_id, {'agent': agent_id, 'kind': action_kind,
                              'detail': detail[:500], 'purpose': purpose[:300],
                              'blockReason': str(block_reason)[:300], 'confidence': confidence, 'note': note})
    return True, note or 'supervisor overrode the block'


# A sandbox_id may only be a safe, flat slug -- never path separators, never
# '..' (a traversal there would mount the repo root -- with .env -- into the
# container via os.path.join). Letters/digits/._- only, one component, capped
# length. Enforced at the one chokepoint every sandbox endpoint funnels
# through, so an agent-supplied sandboxId cannot escape SANDBOXES_DIR.
_SANDBOX_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


def _sandbox_dir_for(sandbox_id):
    if not sandbox_id or not isinstance(sandbox_id, str) or not _SANDBOX_ID_RE.match(sandbox_id):
        raise ValueError(f'invalid sandbox id: {sandbox_id!r}')
    path = os.path.join(SANDBOXES_DIR, sandbox_id)
    os.makedirs(path, exist_ok=True)
    return path


def _sandbox_has_python_files(sandbox_dir):
    """True when the sandbox tree contains any *.py file. The quality
    pipeline's Python steps are trivially green for HTML/JS-only deliverables
    (mypy/pytest fail on the absence of Python itself); this predicate tells
    /api/pipeline whether to skip them."""
    if not sandbox_dir or not isinstance(sandbox_dir, str) or not os.path.isdir(sandbox_dir):
        return False
    for root, _dirs, files in os.walk(sandbox_dir):
        for f in files:
            if f.endswith('.py'):
                return True
    return False


def _village_sandbox_id(agent_id, sandbox_id, state=None):
    """Map a base sandbox id to a village-scoped repo for a non-main, non-chief-of-staff
    agent, so each village gets its own sandbox repos (e.g. 'winter-workroom').
    Main agents, the player, and the chief of staff keep the base id; non-main villages
    get '<vid>-<base>' (e.g. 'winter-workroom-shared'). Applying the remap at
    every sandbox endpoint (execute/pipeline/screenshot/page-probe/sandbox-download/
    sandbox-save-page) keeps a village's code physically separate on disk: a
    winter worker can never run against or read main's workroom repo, and vice
    versa. The repo is provisioned lazily by _sandbox_dir_for."""
    if not sandbox_id or not agent_id or agent_id == 'player':
        return sandbox_id
    state = state if state is not None else get_state_from_db()
    if not state or _is_admin(state, agent_id):
        return sandbox_id
    import village.sim_helpers as _sh
    vid = _sh.village_of_agent(state, agent_id)
    if not vid or vid == 'main':
        return sandbox_id
    if str(sandbox_id).startswith(vid + '-'):
        return sandbox_id
    return f'{vid}-{sandbox_id}'


# Real incident this exists to prevent from ever being unrecoverable
# again: a coding task, run with thin context, wrote `cat > index.html`
# and overwrote a real, real-work-containing file with a fabricated
# throwaway one -- and there was no git repo, no .bak, no Time Machine,
# no local APFS snapshot, nothing. The mistake itself is one any model can
# make; having no way to undo it is the actual, real gap.
#
# Local git, not a full-directory-copy-per-snapshot (the first version of
# this) and not a GitHub remote (a real trade-off) --
# weighed on real numbers: the copy approach measured
# at 868KB for 8 snapshots of the one sandbox that's seen real work, which
# isn't a problem YET, but it's linear in sandbox size with no way to
# store less than a full copy per snapshot. Git's content-addressed
# storage only stores what actually CHANGED between commits -- the
# efficiency problem, solved properly, not just bounded. A GitHub remote
# would add a real, avoidable cost to the SAFETY-CRITICAL path (this runs
# synchronously before every real write): a network round-trip, a new
# auth secret to manage, and a public/private hosting decision for code
# that can contain fetched external content nobody's reviewed. Local git
# gets nearly all the same win -- real diffs, real history, no pruning
# needed -- with none of that. Nothing stops you adding `git remote add`
# + a periodic push later if you want off-machine durability; that's a
# separate, non-safety-critical decision from this one.
SANDBOX_BACKUP_TIMEOUT_S = 10


def _run_git_sync(sandbox_dir, args):
    return subprocess.run(
        ['git'] + args, cwd=sandbox_dir, capture_output=True, text=True, timeout=SANDBOX_BACKUP_TIMEOUT_S,
    )


def _ensure_sandbox_git_repo(sandbox_dir):
    # os.path.exists, not isdir: a linked worktree's .git is a FILE (gitdir:
    # pointer), not a directory. The old isdir check returned False there and
    # re-ran `git init` inside the worktree, silently detaching it from the
    # main repo's history.
    if os.path.exists(os.path.join(sandbox_dir, '.git')):
        return
    _run_git_sync(sandbox_dir, ['init', '-q'])
    # A real identity scoped to just this repo -- doesn't touch or require
    # any global git config on the machine this happens to run on.
    _run_git_sync(sandbox_dir, ['config', 'user.email', 'sandbox-backup@ai-think-tank.local'])
    _run_git_sync(sandbox_dir, ['config', 'user.name', 'AI Think Tank sandbox backup'])
    # Transient browser-act state (screenshots, driver result files, the
    # persistent profile's cookies) must never land in the snapshot history.
    # The action file itself already lives outside the mount, but the profile
    # and shots are written here, so keep the whole dir out of git.
    try:
        gitignore = os.path.join(sandbox_dir, '.gitignore')
        existing = ''
        if os.path.isfile(gitignore):
            with open(gitignore, 'r') as f:
                existing = f.read()
        if '.browser-act/' not in existing:
            with open(gitignore, 'a') as f:
                f.write(('\n' if existing and not existing.endswith('\n') else '') + '.browser-act/\n')
    except OSError:
        pass


def _snapshot_sandbox(sandbox_id, sandbox_dir):
    if not os.path.isdir(sandbox_dir) or not os.listdir(sandbox_dir):
        return  # nothing real to protect yet
    try:
        _ensure_sandbox_git_repo(sandbox_dir)
        _run_git_sync(sandbox_dir, ['add', '-A'])
        # A real, non-zero exit here just means nothing actually changed
        # since the last snapshot -- not a failure, and not worth a commit.
        _run_git_sync(sandbox_dir, ['commit', '-q', '-m', f'snapshot before real write ({time.strftime("%Y-%m-%d %H:%M:%S")})'])
    except Exception as e:
        print(f'[sandbox-backup] snapshot failed for {sandbox_id}: {e}')


def _list_sandbox_backups(sandbox_id):
    sandbox_dir = _sandbox_dir_for(sandbox_id)
    # exists, not isdir -- a linked worktree's .git is a file.
    if not os.path.exists(os.path.join(sandbox_dir, '.git')):
        return []
    result = _run_git_sync(sandbox_dir, ['log', '--format=%H %cI'])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _restore_sandbox_backup(sandbox_id, stamp):
    sandbox_dir = _sandbox_dir_for(sandbox_id)
    commit_hash = stamp.split(' ')[0]  # accepts either a bare hash or a "hash iso-date" line from the list endpoint
    verify = _run_git_sync(sandbox_dir, ['cat-file', '-e', commit_hash])
    if verify.returncode != 0:
        raise ValueError(f'no such backup: {stamp}')
    # A restore is itself a real overwrite -- snapshot what's there right
    # now FIRST, so restoring is undoable too, not just the original write.
    _snapshot_sandbox(sandbox_id, sandbox_dir)
    checkout = _run_git_sync(sandbox_dir, ['checkout', commit_hash, '--', '.'])
    if checkout.returncode != 0:
        raise ValueError(f'restore failed: {checkout.stderr.strip()}')
    # The checkout itself becomes a new, real commit -- keeps history
    # linear and undoable, rather than leaving the working tree ahead of
    # HEAD with nothing recording that a restore happened.
    _snapshot_sandbox(sandbox_id, sandbox_dir)


# ---------------------------------------------------------------------------
# Per-story git worktrees (concurrent-edit isolation).
#
# Root problem this exists to fix: every coding/review/fix pass for every
# deliverable story shared ONE sandbox dir (WORKROOM_SANDBOX_ID). Two agents
# writing different stories into the same tree overwrote each other, and one
# agent's half-written file failed every other agent's whole-tree lint. The
# writtenPy scope fix stopped the *lint* half of that; this stops the
# *filesystem* half by giving each deliverable story its own git worktree
# (branch story/<task-id> of the shared workroom's main) and merging it back
# only when the story's peer gate is satisfied.
#
# A worktree, not a clone: the main workroom repo's object store is shared
# (cheap), each story is a real branch off main, and the merge is a real
# `git merge` inside the main repo -- real history, real conflict detection,
# no GitHub remote anywhere (same local-git-only reasoning as the snapshot
# path above). Worktrees live as sibling dirs under SANDBOXES_DIR so
# _sandbox_dir_for / the docker mount already work unchanged.
# ---------------------------------------------------------------------------
STORY_SANDBOX_PREFIX = 'story-'    # sandboxes/story-<task-id>
STORY_BRANCH_PREFIX = 'story/'     # branch story/<task-id>
# The village-scoped form is '<vid>-story-<task-id>' (see _village_sandbox_id).
_STORY_SANDBOX_RE = re.compile(r'^(?:[A-Za-z0-9][A-Za-z0-9._-]*-)?story-([A-Za-z0-9][A-Za-z0-9._-]{0,63})$')
BASE_SANDBOX_ID = 'workroom-shared'
_STORY_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_STORY_WORKTREE_TIMEOUT_S = 15


def story_sandbox_id(task_id):
    """Canonical story sandbox id for a deliverable story (main village)."""
    if not task_id or not _STORY_ID_RE.match(str(task_id)):
        raise ValueError(f'invalid story/task id: {task_id!r}')
    return STORY_SANDBOX_PREFIX + str(task_id)


def is_story_sandbox_id(sandbox_id):
    return bool(sandbox_id and isinstance(sandbox_id, str) and _STORY_SANDBOX_RE.match(sandbox_id))


def _story_id_from_sandbox(sandbox_id):
    """Story/task id from a (possibly village-scoped) story sandbox id, or None."""
    m = _STORY_SANDBOX_RE.match(sandbox_id or '')
    return m.group(1) if m else None


def _village_prefix(sandbox_id):
    """'winter-' for 'winter-story-x', '' for 'story-x'. The village-scoped
    story worktree must branch the matching village workroom
    ('winter-workroom-shared'), not main's."""
    m = _STORY_SANDBOX_RE.match(sandbox_id or '')
    if not m:
        return ''
    head = (sandbox_id or '')[:-(len(m.group(1)) + len(STORY_SANDBOX_PREFIX))]
    return head


def _story_branch_name(sandbox_id):
    """Branch name for a story sandbox id. The village prefix is dropped: task
    ids are globally unique, so 'story/task-444' is unambiguous and a village
    story still merges cleanly into its own base workroom's history."""
    story_id = _story_id_from_sandbox(sandbox_id)
    if not story_id:
        raise ValueError(f'not a story sandbox id: {sandbox_id!r}')
    return STORY_BRANCH_PREFIX + story_id


def _base_sandbox_for_story(sandbox_id):
    """The base (shared) workroom sandbox this story worktree branches from."""
    return f'{_village_prefix(sandbox_id)}{BASE_SANDBOX_ID}'


def _git_identity_configured(sandbox_dir):
    r = _run_git_sync(sandbox_dir, ['config', '--get', 'user.email'])
    return r.returncode == 0 and bool((r.stdout or '').strip())


def _worktree_is_linked(worktree_dir, base_dir):
    """True when worktree_dir is already a git worktree whose main repo is
    base_dir. Checks the resolved common-dir, not just .git-exists, so a
    stale/orphaned dir is never silently reused."""
    if not os.path.isdir(worktree_dir):
        return False
    if not os.path.exists(os.path.join(worktree_dir, '.git')):
        return False
    base_real = os.path.realpath(base_dir)
    # `git rev-parse --git-common-dir` is the portable way to ask.
    r = _run_git_sync(worktree_dir, ['rev-parse', '--git-common-dir'])
    common = (r.stdout or '').strip()
    if r.returncode != 0 or not common:
        return False
    if not os.path.isabs(common):
        common = os.path.join(worktree_dir, common)
    return os.path.realpath(common) == os.path.join(base_real, '.git')


def _live_repo_head():
    """The live project's committed HEAD (the repo this server runs from)."""
    done = _run_git_sync(THINK_TANK_DIR, ['rev-parse', 'HEAD'])
    return (done.stdout or '').strip() if done.returncode == 0 else ''
