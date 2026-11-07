

@app.post('/api/browser-act')
async def browser_act(request: Request):
    if not EXECUTION_ENABLED:
        return JSONResponse({'error': 'Agent execution is disabled (AGENT_EXECUTION_ENABLED=false in .env)'}, status_code=403)
    if not BROWSING_ENABLED:
        return JSONResponse({'error': 'Agent browsing is disabled (AGENT_BROWSING_ENABLED=false in .env)'}, status_code=403)

    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    op = (body.get('op') or '').strip().lower()
    url = (body.get('url') or '').strip()
    selector = (body.get('selector') or '').strip()
    value = body.get('value')
    purpose = (body.get('purpose') or '').strip()
    max_text = int(body.get('maxText') or 0) or 0
    # Work-context-aware gating: the player-filed task this agent is executing.
    # A host explicitly named in it bypasses Jev (same rule as /api/browse);
    # every other action is judged with the work context in the Jev prompt.
    work_context = (body.get('workContext') or '').strip()
    # Player-authored provenance: the work-context bypass inside
    # _classify_browser_action (a host named in the assigned work skipping Jev
    # like the allowlist) fires ONLY for a task the PLAYER wrote. An
    # agent-authored story can name any URL in its own text -- never trusted
    # for the bypass.
    work_context_trusted = _work_trust_from_body(body)
    sandbox_id = body.get('sandboxId') or ('sandbox-' + secrets.token_hex(4))
    sandbox_id = _village_sandbox_id(agent_id, sandbox_id)  # per-village repo isolation
    if not _SANDBOX_ID_RE.match(sandbox_id or ''):
        return JSONResponse({'error': 'invalid sandboxId'}, status_code=400)
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'browser-act'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = _actor_is_authorized(request, agent_id)
    if not authorized:
        return JSONResponse({'error': 'a valid agent key for agentId is required'}, status_code=401)
    if op not in BROWSER_ACT_OPS:
        return JSONResponse({'error': f'op must be one of {", ".join(BROWSER_ACT_OPS)}'}, status_code=400)
    if op in ('goto',) and not url:
        return JSONResponse({'error': 'url is required for op=goto'}, status_code=400)
    if op == 'fill' and not selector:
        return JSONResponse({'error': 'selector is required for op=fill'}, status_code=400)
    if op == 'fill_many' and not (body.get('fields') or []):
        return JSONResponse({'error': 'fields is required for op=fill_many'}, status_code=400)
    if op == 'login' and not (body.get('fields') or []):
        return JSONResponse({'error': 'fields is required for op=login'}, status_code=400)
    if op == 'click' and not selector:
        return JSONResponse({'error': 'selector is required for op=click'}, status_code=400)
    # Fill missing social-login handles from the live task handle BEFORE the
    # read-only guard runs. The executor sends taskId, so a model that already
    # requested access but omitted the opaque handle still logs in instead of
    # bouncing on a copy error.
    _early_fields = body.get('fields') or []
    _early_task = (body.get('taskId') or '').strip()
    _early_url = (url or body.get('pageUrl') or '').strip()
    if (op in ('login', 'fill_many') and _early_fields and _early_task and
            _is_readonly_social_host(urllib.parse.urlparse(_early_url).hostname or '')):
        _fallback = _active_task_handle(agent_id, _early_task, 'POST', _early_url)
        if _fallback:
            for _f in _early_fields:
                if isinstance(_f, dict) and not (_f.get('credentialHandle') or '').strip():
                    _f['credentialHandle'] = _fallback
    social_error = _social_readonly_guard(
        op, url, (body.get('pageUrl') or '').strip(), body.get('fields') or [])
    if social_error:
        # Missing/invalid login fields are a provisioning mistake, not a
        # policy denial: the agent must request a fresh handle and retry.
        # Mark it so the tool loop does not spend its one browser strike.
        retryable = ((op == 'login' and
                      ('credential handle' in social_error or
                       'needs username' in social_error or
                       'username or password' in social_error)) or
                     (op in ('read', 'screenshot') and
                      'pass the current' in social_error))
        log_action(agent_id, 'browser_act', {'sandboxId': sandbox_id, 'op': op,
                                              'url': url or body.get('pageUrl'),
                                              'decision': 'blocked',
                                              'reason': 'read-only social policy'},
                   authorized=authorized)
        return JSONResponse({'allowed': False, 'reason': social_error,
                             'credentialHandleError': retryable}, status_code=200)
    # Confused-deputy fill: an agent can type a vault login field WITHOUT ever
    # holding the secret. It presents a capability handle + which field to
    # inject (username|password). The server resolves the handle against the
    # page host (same host/method scope as api_call), pulls the value from the
    # encrypted vault, and puts it only into the sandbox action JSON. The
    # response never echoes the typed value; the agent only sees the resulting
    # page state. A plain `value` fill still works and is unchanged.
    credential_handle = (body.get('credentialHandle') or '').strip()
    credential_field = (body.get('credentialField') or '').strip().lower()
    # Validate the handle's SCOPE first (agent, expiry, host, method) without
    # touching the vault. The secret is only fetched AFTER Jev approves the
    # action -- a blocked action never decrypts or writes a credential.
    handle_scope = None
    scope_url = (url or body.get('pageUrl') or '').strip()
    if op in ('fill', 'fill_many', 'login', 'submit', 'click') and credential_handle:
        if op == 'fill' and credential_field not in ('username', 'password'):
            return JSONResponse({'error': 'credentialField must be "username" or "password" when credentialHandle is set'}, status_code=400)
        if not scope_url:
            # Fill after a prior goto often omits url; the persistent profile
            # implies a host. Require an explicit pageUrl so the handle's host
            # scope is always checked against a real URL, never a guess.
            return JSONResponse({'error': 'url or pageUrl is required when credentialHandle is set'}, status_code=400)
        handle_scope = verify_capability_handle_scope(agent_id, credential_handle, 'POST', scope_url)
        if handle_scope is None:
            log_action(agent_id, 'browser_act', {'sandboxId': sandbox_id, 'op': op, 'url': scope_url,
                                                  'purpose': purpose, 'decision': 'blocked',
                                                  'reason': 'credential handle invalid, expired, out of scope, or not a login credential'}, authorized=authorized)
            return JSONResponse({'error': 'That capability handle is not valid for this login fill (expired, wrong agent, out-of-scope host, or not a login credential).'}, status_code=403)
    # Per-field handles (op=login/fill_many). Resolve every field's handle
    # BEFORE the policy gate so a fully scoped login can skip Jev's generic
    # credential-submission block: the player already vetted the credential and
    # the host. A single invalid/missing handle fails closed without touching
    # the vault.
    fields_scope = None
    supplied_fields = body.get('fields') or []
    scope_is_social = _is_readonly_social_host(
        (urllib.parse.urlparse(scope_url).hostname or '') if scope_url else '')
    task_id = (body.get('taskId') or '').strip()
    fallback_handle = None
    used_fallback = False
    if op in ('login', 'fill_many') and supplied_fields:
        ok_fields = []
        for field in supplied_fields:
            if not isinstance(field, dict):
                ok_fields = None
                break
            fh = (field.get('credentialHandle') or '').strip()
            ff = (field.get('credentialField') or '').strip().lower()
            fscope = verify_capability_handle_scope(agent_id, fh, 'POST', scope_url) if fh else None
            if fscope is None and scope_is_social and task_id:
                if fallback_handle is None:
                    fallback_handle = _active_task_handle(agent_id, task_id, 'POST', scope_url)
                if fallback_handle:
                    field['credentialHandle'] = fallback_handle
                    fh = fallback_handle
                    fscope = verify_capability_handle_scope(agent_id, fh, 'POST', scope_url)
                    used_fallback = True
            if fscope is None or ff not in ('username', 'password'):
                ok_fields = None
                break
            ok_fields.append(fscope)
        if ok_fields is not None:
            fields_scope = ok_fields[0]
            if used_fallback:
                log_action(agent_id, 'browser_act_handle_fallback', {
                    'sandboxId': sandbox_id, 'op': op, 'url': scope_url,
                    'taskId': task_id, 'reason': 'used the live task handle',
                }, authorized=authorized)
        elif scope_is_social:
            # A social login WITHOUT a live, host-scoped handle is not a
            # policy judgment -- the agent simply has not been granted access
            # yet (or used an expired/old handle). Tell it exactly what to do
            # without burning its one browser strike.
            log_action(agent_id, 'browser_act', {
                'sandboxId': sandbox_id, 'op': op, 'url': scope_url,
                'decision': 'blocked', 'reason': 'missing or stale credential handle',
            }, authorized=authorized)
            return JSONResponse({
                'allowed': False,
                'credentialHandleError': True,
                'reason': ('This social login needs a fresh, host-scoped credential '
                           'handle on every field. Call request_credential_access for '
                           'this service while working this task, then retry op=login '
                           'with the returned handle in each field.'),
            }, status_code=200)
    if url:
        # Same URL guardrails as /api/browse: http(s) only, and the final host
        # must be a safe public host (SSRF/private-network protection). Fires
        # for ANY op that carries a url, not just goto: the driver navigates to
        # the url for every such op (browser_driver.py navigates before
        # fill/click/etc.), and _run_browser_act_sync grants the url's host
        # egress op-agnostically -- so a fill with a link-local/metadata url
        # would otherwise have skipped this check entirely while still
        # reaching the network. The sandbox egress proxy still applies
        # underneath -- this is the intent gate; the proxy is the hard
        # boundary for everything except hosts Jev explicitly approved (which
        # get a short-lived, scoped egress grant for exactly this run -- see
        # _run_browser_act_sync).
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return JSONResponse({'error': 'url must be http(s) with a real host'}, status_code=400)
        if not _is_safe_public_host(parsed.hostname):
            return JSONResponse({'error': 'url host is not an allowed public host'}, status_code=400)

    # When a handle-scoped fill omits `url` (a fill after a prior goto in the
    # persistent profile), classify against the `scope_url` the handle was
    # resolved against so the Jev bypass sees the real host, not an empty one.
    classify_url = url or (scope_url if (op in ('fill', 'fill_many', 'login', 'submit', 'click') and credential_handle) else '')
    allowed, reason = await _classify_browser_action(op, classify_url, purpose, agent_id, work_context, work_context_trusted, handle_scoped_host=bool(handle_scope or fields_scope))
    if not allowed:
        # Same supervisor-override path as /api/execute: the director (non-Jev
        # model) gets one review before the block is final.
        overridden, override_note = await _supervisor_override_decision(
            agent_id, 'browser action', f'{op} {url or "(current page)"}', purpose, reason, BROWSE_BLOCK_CATEGORIES)
        if not overridden:
            log_action(agent_id, 'browser_act', {'sandboxId': sandbox_id, 'op': op, 'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': reason, 'supervisorNote': override_note}, authorized=authorized)
            return JSONResponse({'allowed': False, 'reason': reason, 'supervisorNote': override_note})
        log_action(agent_id, 'browser_act', {'sandboxId': sandbox_id, 'op': op, 'url': url, 'purpose': purpose, 'decision': 'allowed_supervisor_override', 'reason': reason, 'supervisorNote': override_note}, authorized=authorized)

    # Jev has approved the action. NOW fetch the secret and inject it -- a
    # blocked action never reaches here, so the vault is only read for actions
    # the policy gate has already cleared.
    if op in ('fill', 'fill_many', 'login') and handle_scope is not None:
        grant = resolve_capability_handle(agent_id, credential_handle, 'POST', scope_url)
        if grant is None or grant.get('kind') != 'login':
            return JSONResponse({'error': 'That capability handle is not valid for this login fill (expired, wrong agent, out-of-scope host, or not a login credential).'}, status_code=403)
        injected = grant.get(credential_field)
        if not injected:
            return JSONResponse({'error': f'the stored credential has no {credential_field}'}, status_code=400)
        value = injected  # only reaches the sandbox action file; never logged or returned

    sandbox_dir = _sandbox_dir_for(sandbox_id)
    action = {'op': op, 'url': url, 'pageUrl': scope_url,
              'selector': selector, 'value': value if value is not None else '',
             'timeoutMs': 30000, 'maxText': max_text}
    # Automatic solving is opt-in twice: the global switch AND this host's
    # policy must both say yes. A relay-only host never gets a driver solve.
    _auto_mode = (_load_env().get('CAPTCHA_AUTO_SOLVE') or 'off').strip().lower()
    if _auto_mode != 'off':
        _auto_host = urllib.parse.urlparse(
            url or scope_url or (body.get('pageUrl') or '')).hostname or ''
        if not _auto_host or captcha.policy_for_host(_auto_host) != 'attempt':
            _auto_mode = 'off'
    action['autoSolve'] = _auto_mode
    if op in ('fill', 'fill_many', 'login'):
        # Per-field credential injection: each field may carry its own
        # credentialHandle + credentialField. Resolve each against the
        # already-validated scope_url and put the secret into the field's
        # value. The driver only ever sees {selector, value} -- never the
        # handle. A field with no handle passes its literal value through.
        fields_out = []
        for f in (body.get('fields') or []):
            field_out = {'selector': f.get('selector') or ''}
            fh = (f.get('credentialHandle') or '').strip()
            ff = (f.get('credentialField') or '').strip().lower()
            if fh:
                if ff not in ('username', 'password'):
                    return JSONResponse({'error': 'credentialField must be "username" or "password" when credentialHandle is set'}, status_code=400)
                fgrant = resolve_capability_handle(agent_id, fh, 'POST', scope_url)
                if fgrant is None or fgrant.get('kind') != 'login':
                    return JSONResponse({'error': 'That capability handle is not valid for this login fill (expired, wrong agent, out-of-scope host, or not a login credential).'}, status_code=403)
                fval = fgrant.get(ff)
                if not fval:
                    return JSONResponse({'error': f'the stored credential has no {ff}'}, status_code=400)
                field_out['value'] = fval
            else:
                field_out['value'] = f.get('value') or ''
            fields_out.append(field_out)
        action['fields'] = fields_out
        action['submitSelector'] = (body.get('submitSelector') or '').strip()
        entry = (body.get('loginEntryUrl') or '').strip()
        if not entry and 'facebook.com' in (urllib.parse.urlparse(scope_url).hostname or ''):
            entry = 'https://www.facebook.com/'
        if entry:
            parsed_entry = urllib.parse.urlparse(entry)
            scope_host = (urllib.parse.urlparse(scope_url).hostname or '').lower()
            entry_host = (parsed_entry.hostname or '').lower()
            social_sibling = (
                (entry_host == 'facebook.com' or entry_host.endswith('.facebook.com'))
                and (scope_host == 'facebook.com' or scope_host.endswith('.facebook.com'))
            ) or (
                (entry_host == 'instagram.com' or entry_host.endswith('.instagram.com'))
                and (scope_host == 'instagram.com' or scope_host.endswith('.instagram.com'))
            )
            same_site = bool(entry_host and scope_host and (
                entry_host == scope_host or entry_host.endswith('.' + scope_host)
                or scope_host.endswith('.' + entry_host) or social_sibling))
            if (parsed_entry.scheme not in ('http', 'https') or not same_site
                    or parsed_entry.username or parsed_entry.password):
                return JSONResponse({'error': 'loginEntryUrl must be http(s) on the same site as url'}, status_code=400)
        action['loginEntryUrl'] = entry
    parsed_result, run_error = await asyncio.to_thread(
        _browser_act_with_pacing, sandbox_dir, action, agent_id)
    if parsed_result is None:
        log_action(agent_id, 'browser_act', {'sandboxId': sandbox_id, 'op': op, 'url': url, 'purpose': purpose, 'decision': 'error', 'reason': run_error}, authorized=authorized)
        return JSONResponse({'allowed': True, 'ok': False, 'error': run_error})
    # The driver result carries page state (text/fields/links) plus an optional
    # embedded screenshot (_shotB64) for the vision-tier readback loop.
    shot = parsed_result.pop('_shotB64', None)
    # Never echo the typed value back (credential-injected fills put a vault
    # secret into action['value']; plain fills are also omitted for symmetry).
    if op == 'fill':
        parsed_result.pop('value', None)
    # A WAF/challenge page is a site-side refusal, not a selector miss. Stop,
    # record it with the host and vendor, and hand the agent a clear next
    # step instead of letting it retry the same URL.
    if parsed_result.get('blocked') or parsed_result.get('challenge'):
        blocked = _browser_block_payload(parsed_result, agent_id, op, url, purpose)
        log_action(agent_id, 'browser_act', {'sandboxId': sandbox_id, 'op': op, 'url': url, 'purpose': purpose, 'decision': 'blocked', 'reason': (blocked.get('blocked') or {}).get('vendor')}, authorized=authorized)
        return JSONResponse({'allowed': True, **blocked, 'screenshotBase64': shot})
    captcha_info = parsed_result.get('captchas') or []
    if not isinstance(captcha_info, list):
        captcha_info = []
    solver_info = parsed_result.get('captchaSolver') or {}
    if not isinstance(solver_info, dict):
        solver_info = {}
    if solver_info.get('solved'):
        parsed_result['captchaSolved'] = True
        log_action(agent_id, 'browser_act_captcha_autosolved', {
            'sandboxId': sandbox_id, 'op': op, 'url': url,
            'kind': solver_info.get('kind'),
            'challengeType': solver_info.get('challengeType'),
            'attempts': solver_info.get('attempts'),
        }, authorized=authorized)
    elif captcha_info:
        parsed_result['captchaRequired'] = await asyncio.to_thread(
            _handle_captcha_detected, agent_id, sandbox_id,
            parsed_result.get('url') or url, captcha_info, shot,
            purpose, (body.get('submitSelector') or '').strip())
    log_action(agent_id, 'browser_act', {'sandboxId': sandbox_id, 'op': op, 'url': url, 'purpose': purpose, 'decision': 'allowed', 'ok': parsed_result.get('ok'), 'status': parsed_result.get('status'), 'retryCount': parsed_result.get('retryCount'), 'dataRoutes': len(parsed_result.get('dataRoutes') or []), 'credentialInjected': bool(credential_handle)}, authorized=authorized)
    return JSONResponse({'allowed': True, **parsed_result, 'screenshotBase64': shot})


# ---------------------------------------------------------------------------
# Browser sessions: a long-lived, player-viewable Chromium per agent.
# Start one when an agent needs help (or is about to do something fiddly),
# watch it, take over for a moment, then hand back. Player session only.
def _browser_session_sandbox_id(agent_id, sandbox_id):
    sid = sandbox_id or ('sandbox-' + secrets.token_hex(4))
    sid = _village_sandbox_id(agent_id, sid)
    if not _SANDBOX_ID_RE.match(sid or ''):
        raise ValueError('invalid sandboxId')
    return sid


def _browser_session_proxy_url():
    return f'http://{PROXY_CONTAINER}:{PROXY_PORT}'


@app.get('/api/browser-session/list')
async def browser_session_list(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    sessions = await asyncio.to_thread(browser_session.list_sessions)
    return JSONResponse({'sessions': sessions})


@app.post('/api/browser-session/start')
async def browser_session_start(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    if not EXECUTION_ENABLED:
        return JSONResponse({'error': 'Agent execution is disabled (AGENT_EXECUTION_ENABLED=false in .env)'}, status_code=403)
    body = await request.json()
    agent_id = str(body.get('agentId') or '').strip()
    if not agent_id:
        return JSONResponse({'error': 'agentId is required'}, status_code=400)
    try:
        sandbox_id = _browser_session_sandbox_id(agent_id, body.get('sandboxId'))
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    # The session joins the sandbox network, so the network and its egress
    # proxy must exist first. Same call every other sandbox path makes.
    try:
        await asyncio.to_thread(ensure_sandbox_networking)
    except Exception as e:
        return JSONResponse({'error': f'sandbox networking unavailable: {e}'}, status_code=500)
    try:
        session = await asyncio.to_thread(
            browser_session.start, agent_id,
            sandbox_dir=_sandbox_dir_for(sandbox_id),
            profile_dir=_browser_profile_dir(agent_id),
            proxy_url=_browser_session_proxy_url())
    except browser_session.BrowserSessionError as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    log_action('player', 'browser_session_start',
               {'agentId': agent_id, 'sandboxId': sandbox_id}, authorized=True)
    return JSONResponse({'ok': True, 'session': session})


@app.post('/api/browser-session/stop')
async def browser_session_stop(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    body = await request.json()
    agent_id = str(body.get('agentId') or '').strip()
    if not agent_id:
        return JSONResponse({'error': 'agentId is required'}, status_code=400)
    try:
        stopped = await asyncio.to_thread(browser_session.stop, agent_id)
    except browser_session.BrowserSessionError as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    log_action('player', 'browser_session_stop', {'agentId': agent_id}, authorized=True)
    return JSONResponse({'ok': True, 'stopped': stopped})


@app.post('/api/browser-session/takeover')
async def browser_session_takeover(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    body = await request.json()
    agent_id = str(body.get('agentId') or '').strip()
    if not agent_id:
        return JSONResponse({'error': 'agentId is required'}, status_code=400)
    try:
        session = await asyncio.to_thread(browser_session.takeover, agent_id)
    except browser_session.BrowserSessionError as e:
        return JSONResponse({'error': str(e)}, status_code=404)
    log_action('player', 'browser_session_takeover', {'agentId': agent_id}, authorized=True)
    return JSONResponse({'ok': True, 'session': session})


@app.post('/api/browser-session/handback')
async def browser_session_handback(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    body = await request.json()
    agent_id = str(body.get('agentId') or '').strip()
    if not agent_id:
        return JSONResponse({'error': 'agentId is required'}, status_code=400)
    try:
        session = await asyncio.to_thread(browser_session.handback, agent_id)
    except browser_session.BrowserSessionError as e:
        return JSONResponse({'error': str(e)}, status_code=404)
    log_action('player', 'browser_session_handback', {'agentId': agent_id}, authorized=True)
    return JSONResponse({'ok': True, 'session': session})


@app.get('/api/browser-session/view')
async def browser_session_view(request: Request, token: str = ''):
    if not _require_player_session(request):
        return HTMLResponse('<p>Sign in at / first.</p>', status_code=401)
    match = await asyncio.to_thread(browser_session.session_by_token, token)
    if not match:
        return HTMLResponse('<p>Unknown or expired session.</p>', status_code=404)
    agent = re.sub(r'[^A-Za-z0-9_.-]', '', str(match.get('agentId') or ''))[:64]
    # Same-origin proxy: noVNC assets and its websocket both ride this app's
    # origin, so takeover works from any machine that can reach the server
    # (Tailscale included). The loopback port is never exposed to the player.
    vnc_base = f'/api/browser-session/vnc/{urllib.parse.quote(token)}'
    src = (vnc_base + '/vnc.html?autoconnect=1&resize=scale'
           + '&path=' + urllib.parse.quote(vnc_base + '/websockify', safe='/')
           + '&password=' + urllib.parse.quote(match.get('password') or ''))
    handback = "/api/browser-session/handback"
    page = f'''<!doctype html><html><head><meta charset="utf-8">
<title>Takeover: {agent}</title>
<style>
body{{margin:0;background:#0b0f14;color:#d7e2ef;font:14px ui-monospace,Menlo,monospace}}
header{{display:flex;gap:12px;align-items:center;padding:10px 14px;border-bottom:1px solid #223042}}
button{{background:#16222f;color:#d7e2ef;border:1px solid #223042;border-radius:6px;padding:6px 10px;font:inherit;cursor:pointer}}
iframe{{width:100%;height:calc(100vh - 48px);border:0;display:block}}
</style></head><body>
<header><b>Takeover: {agent}</b>
<button id="hb">Hand back to agent</button>
<span id="msg"></span></header>
<iframe src="{src}" allow="clipboard-read; clipboard-write"></iframe>
<script>
document.getElementById('hb').onclick = async () => {{
  const r = await fetch('{handback}', {{method:'POST', credentials:'same-origin',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{agentId: '{agent}'}})}});
  document.getElementById('msg').textContent = r.ok ? 'Handed back.' : 'Handback failed.';
}};
</script></body></html>'''
    return HTMLResponse(page)


@app.get('/api/browser-session/vnc/{token}/{asset:path}')
async def browser_session_vnc_asset(request: Request, token: str, asset: str):
    """Serve one noVNC static asset for a session from its loopback port.

    Token plus player session, and the upstream host/port come from the
    session registry, never from the request. This is a plain loopback fetch
    (not _safe_urlopen, which correctly refuses private addresses), so the red
    line is: the URL is built here from a registry entry the player started."""
    if not _require_player_session(request):
        return Response(status_code=401)
    match = await asyncio.to_thread(browser_session.session_by_token, token)
    if not match:
        return Response(status_code=404)
    asset = asset.lstrip('/') or 'vnc.html'
    if '..' in asset.split('/'):
        return Response(status_code=400)
    upstream = f'http://127.0.0.1:{int(match["port"])}/{asset}'
    try:
        req = urllib.request.Request(upstream, headers={'User-Agent': 'tank-novnc-proxy'})
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 -- loopback, registry-derived port
            body = resp.read(32 * 1024 * 1024)
            ctype = resp.headers.get('Content-Type') or 'application/octet-stream'
    except urllib.error.HTTPError as e:
        return Response(status_code=e.code, content=b'')
    except Exception:
        return Response(status_code=502, content=b'')
    return Response(content=body, media_type=ctype)


@app.websocket('/api/browser-session/vnc/{token}/websockify')
async def browser_session_vnc_ws(websocket: WebSocket, token: str):
    """Relay noVNC's websocket to the session's loopback websockify.

    Same auth as the page (session cookie plus token). Frames are forwarded
    verbatim in both directions; nothing is decoded, so the agent and the
    player still share one browser."""
    if not verify_session(websocket.cookies.get(SESSION_COOKIE_NAME)):
        await websocket.close(code=4401)
        return
    match = browser_session.session_by_token(token)
    if not match:
        await websocket.close(code=4404)
        return
    try:
        import websockets
    except Exception:
        await websocket.close(code=1011)
        return
    offered = list(websocket.scope.get('subprotocols') or [])
    try:
        upstream = await websockets.connect(
            f'ws://127.0.0.1:{int(match["port"])}/websockify',
            subprotocols=offered, open_timeout=10,
            max_size=None, ping_interval=None)
    except Exception:
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol=upstream.subprotocol)

    async def client_to_upstream():
        try:
            while True:
                message = await websocket.receive()
                if message.get('type') == 'websocket.disconnect':
                    break
                data = message.get('bytes')
                if data is not None:
                    await upstream.send(data)
                elif message.get('text') is not None:
                    await upstream.send(message['text'])
        except Exception:
            pass

    async def upstream_to_client():
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except Exception:
            pass

    try:
        await asyncio.gather(client_to_upstream(), upstream_to_client())
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()


_PIPELINE_JOBS = {}
_PIPELINE_SANDBOX_LOCKS = {}


def _pipeline_sandbox_lock(agent_id, sandbox_id):
    key = _village_sandbox_id(agent_id, sandbox_id)
    return _PIPELINE_SANDBOX_LOCKS.setdefault(key, asyncio.Lock())


@app.post('/api/pipeline')
async def pipeline(request: Request):
    if not EXECUTION_ENABLED:
        return JSONResponse({'error': 'Agent execution is disabled (AGENT_EXECUTION_ENABLED=false in .env)'}, status_code=403)
    body = await request.json()
    if isinstance(body, dict) and body.get('background'):
        return await _start_pipeline_job(request, body)
    return await _pipeline_execute(request, body)


async def _start_pipeline_job(request: Request, body: dict):
    agent_id = body.get('agentId', 'unknown')
    if not _require_player_session(request) and verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Login or agent key required.'}, status_code=401)
    steps = body.get('steps') or []
    pipeline_target = {'sandboxId': body.get('sandboxId') or 'unknown',
                       'steps': [{'name': s.get('name', 'step'),
                                  'command': s.get('command', '')} for s in steps]}
    confirmation_error, _confirmation_id = _confirmation_gate_or_response(
        body, agent_id, 'pipeline', pipeline_target, {'steps': steps})
    if confirmation_error:
        return confirmation_error
    job_id = secrets.token_hex(16)
    body['_jobId'] = job_id
    job = {'id': job_id, 'agentId': agent_id, 'status': 'running',
           'startedAt': time.time(), 'stepsTotal': len(body.get('steps') or []),
           'stepsCompleted': 0}
    if len(_PIPELINE_JOBS) >= 128:
        for old_id, old_job in list(_PIPELINE_JOBS.items()):
            if old_job.get('status') in ('done', 'failed'):
                _PIPELINE_JOBS.pop(old_id, None)
                if len(_PIPELINE_JOBS) < 128:
                    break
    if len(_PIPELINE_JOBS) >= 256:
        return JSONResponse({'error': 'too many pipeline jobs are running; retry shortly'},
                            status_code=429)
    _PIPELINE_JOBS[job_id] = job

    async def run():
        try:
            response = await _pipeline_execute(request, body)
            job['result'] = json.loads(response.body)
            job['status'] = 'done' if response.status_code < 400 else 'failed'
        except Exception as error:
            job.update(status='failed', result={'error': str(error)})
        finally:
            job['finishedAt'] = time.time()
    job['worker'] = asyncio.create_task(run())
    return JSONResponse({'jobId': job_id, 'status': 'running'}, status_code=202)


@app.get('/api/pipeline/jobs/{job_id}')
async def pipeline_job(job_id: str, request: Request):
    job = _PIPELINE_JOBS.get(job_id)
    if not job:
        return JSONResponse({'error': 'Unknown pipeline job.'}, status_code=404)
    if not _require_player_session(request) and verify_agent_key(job['agentId'], request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Login or owning agent key required.'}, status_code=401)
    return {key: value for key, value in job.items() if key != 'worker'}


async def _pipeline_execute(request: Request, body: dict):
    agent_id = body.get('agentId', 'unknown')
    sandbox_id = body.get('sandboxId') or 'unknown'
    async with _pipeline_sandbox_lock(agent_id, sandbox_id):
        return await _pipeline_execute_locked(request, body)


async def _pipeline_execute_locked(request: Request, body: dict):
    # "They would wait to see where the pipeline succeeds or fails" --
    # your words. A pipeline is just a sequence of the same primitive
    # /api/execute already provides, run in the SAME sandbox directory
    # (so file state persists step to step -- clone, then install, then
    # test), stopping at the first failure, whether that's a blocked
    # command or a real non-zero exit.
    if not EXECUTION_ENABLED:
        return JSONResponse({'error': 'Agent execution is disabled (AGENT_EXECUTION_ENABLED=false in .env)'}, status_code=403)
    agent_id = body.get('agentId', 'unknown')
    steps = body.get('steps') or []
    sandbox_id = body.get('sandboxId') or ('sandbox-' + secrets.token_hex(4))
    sandbox_id = _village_sandbox_id(agent_id, sandbox_id)  # per-village repo isolation
    if not _SANDBOX_ID_RE.match(sandbox_id or ''):
        return JSONResponse({'error': 'invalid sandboxId'}, status_code=400)
    # Counted as ONE call regardless of step count -- a pipeline is one
    # logical request from the caller's side even though it fans out into
    # several sandbox runs internally; each individual step already goes
    # through Jev classification on its own.
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'pipeline'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = _actor_is_authorized(request, agent_id)
    if not authorized:
        return JSONResponse({'error': 'a valid agent key for agentId is required'}, status_code=401)
    if not steps:
        return JSONResponse({'error': 'steps is required'}, status_code=400)
    if len(steps) > 64:
        return JSONResponse({'error': 'a pipeline may contain at most 64 steps'}, status_code=400)

    pipeline_target = {'sandboxId': sandbox_id,
                       'steps': [{'name': s.get('name', 'step'),
                                  'command': s.get('command', '')} for s in steps]}
    confirmation_error, confirmation_id = _confirmation_gate_or_response(
        body, agent_id, 'pipeline', pipeline_target, {'steps': steps})
    if confirmation_error:
        return confirmation_error

    sandbox_dir = _sandbox_dir_for(sandbox_id)
    # A sandbox with no Python files has nothing for the Python gate to check.
    # mypy exits 2 and pytest exits 1 on the ABSENCE itself ("no .py files" /
    # "no tests ran"), which is a false red for HTML/JS-only workroom
    # deliverables. When no *.py exists anywhere in the tree, the Python
    # quality steps are trivially green -- the deliverable simply isn't a
    # Python project. flake8/bandit already pass on an empty tree, so only
    # mypy/bandit/pytest-cov need the skip. The JS steps (node-syntax/
    # node-tests) self-skip green inside their own command when no JS files
    # are present, so no server-side skip is needed for them.
    has_py = _sandbox_has_python_files(sandbox_dir)
    results = []
    job = _PIPELINE_JOBS.get(body.get('_jobId'))
    for step in steps:
        name = step.get('name', 'step')
        if job:
            job['currentStep'] = name
        command = (step.get('command') or '').strip()
        if not has_py and name in ('mypy', 'bandit', 'pytest-cov'):
            results.append({'name': name, 'allowed': True, 'exitCode': 0,
                            'stdout': '', 'stderr': '(skipped: no .py files in sandbox)',
                            'timedOut': False})
            continue
        allowed, reason, block_kind = await _classify_command(command, f'CI/CD pipeline step "{name}"', agent_id)
        if not allowed:
            # Same supervisor-override path as /api/execute, per step.
            overridden, override_note = await _supervisor_override_decision(
                agent_id, f'pipeline step "{name}"', command, f'CI/CD pipeline step "{name}"', reason, EXECUTE_BLOCK_CATEGORIES)
            if not overridden:
                log_action(agent_id, 'execute', {'sandboxId': sandbox_id, 'command': command, 'decision': 'blocked', 'reason': reason, 'blockKind': block_kind, 'supervisorNote': override_note}, authorized=authorized)
                results.append({'name': name, 'allowed': False, 'reason': reason, 'blockKind': block_kind, 'supervisorNote': override_note})
                _finish_confirmation(confirmation_id, 'failed', f'blocked step {name}')
                sync_prototypes(agent_id, sandbox_dir)
                return JSONResponse({'sandboxId': sandbox_id, 'results': results, 'failedStep': name})
            log_action(agent_id, 'execute', {'sandboxId': sandbox_id, 'command': command, 'decision': 'allowed_supervisor_override', 'reason': reason, 'supervisorNote': override_note}, authorized=authorized)

        await asyncio.to_thread(_snapshot_sandbox, sandbox_id, sandbox_dir)
        result = await asyncio.to_thread(_run_in_sandbox_sync, sandbox_dir, command,
                                         agent_id=agent_id)
        log_action(agent_id, 'execute', {'sandboxId': sandbox_id, 'command': command, 'decision': 'allowed', 'exitCode': result['exitCode']}, authorized=authorized)
        results.append({'name': name, 'allowed': True, **result})
        if job:
            job['stepsCompleted'] = len(results)
        if result['exitCode'] != 0 or result['timedOut']:
            _finish_confirmation(confirmation_id, 'failed', f'step {name} failed')
            sync_prototypes(agent_id, sandbox_dir)
            return JSONResponse({'sandboxId': sandbox_id, 'results': results, 'failedStep': name})

    _finish_confirmation(confirmation_id, 'executed')
    sync_prototypes(agent_id, sandbox_dir)
    return JSONResponse({'sandboxId': sandbox_id, 'results': results, 'failedStep': None})


@app.post('/api/youtube-transcript')
async def youtube_transcript(request: Request):
    # Free route FIRST: YouTube's own captions via local yt-dlp (no metered API,
    # no Colab compute). Only when captions are unavailable/empty does this fall
    # back to the download-and-transcribe path on the Colab runtime (the Apify
    # actor + faster-whisper in _youtube_transcript_colab), which is the
    # expensive, metered one. This REVERSES the earlier "always download, treat
    # them all the same" decision -- the player's call now is to prefer the free
    # route and spend Colab only on videos captions can't serve. Both paths file
    # to the same media/transcripts/{video_id}.txt with the same header; the
    # header's Source line records which one produced it. Blocks run in a worker
    # thread so the blocking fetch/transcribe calls don't stall the event loop.
    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    url = (body.get('url') or '').strip()
    lang = (body.get('lang') or 'en').strip() or 'en'
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'youtube-transcript'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    if not _is_youtube_url(url):
        return JSONResponse({'error': 'a valid youtube.com or youtu.be URL is required'}, status_code=400)
    cached = _cached_youtube_transcript(url)
    if cached:
        text, filed_path = cached
        log_action(agent_id, 'youtube_transcript',
                   {'url': url, 'decision': 'cache', 'chars': len(text), 'source': 'cache'},
                   authorized=authorized)
        return JSONResponse({'ok': True, 'url': url, 'transcript': text, 'chars': len(text),
                             'source': 'cache', 'filed': filed_path, 'cached': True})
    text, captions_error = await asyncio.to_thread(_youtube_transcript, url, lang)
    source = 'yt-dlp-captions'
    if not text:
        text, error = await asyncio.to_thread(_youtube_transcript_colab, url, lang)
        source = 'colab-whisper'
        if error:
            log_action(agent_id, 'youtube_transcript',
                       {'url': url, 'decision': 'failed', 'error': error[:200],
                        'captionsError': (captions_error or '')[:200]}, authorized=authorized)
            return JSONResponse({'error': error}, status_code=422)
    # File the extracted transcript into the shared Library's media/
    # transcripts/ tree so ANY agent can read it later (the Studio's
    # media-building lane reads this same media/ area). Best-effort: the
    # transcript is returned to the caller regardless, and a write failure
    # must never fail the request.
    filed_path = _file_youtube_transcript(url, text, source)
    log_action(agent_id, 'youtube_transcript',
               {'url': url, 'decision': 'ok', 'chars': len(text), 'source': source}, authorized=authorized)
    return JSONResponse({'ok': True, 'url': url, 'transcript': text, 'chars': len(text),
                         'source': source, 'filed': filed_path})


# ---------------------------------------------------------------------------
# YouTube frame capture + vision review (2026-11-02, the player's ask). The
# transcript tool tells agents what was SAID; the operator's per-project UI
# work needs what was SHOWN. Ruth's spike on the Tiny Software Factories
# video proved the gap: the sandbox has no yt-dlp/ffmpeg and its browser
# egress to YouTube failed (ERR_TUNNEL_CONNECTION_FAILED), so no frame was
# ever captured or vision-reviewed. The HOST has both tools (yt-dlp 2026.08.19,
# ffmpeg) and youtube.com is already in BROWSE_ALLOWLIST_DOMAINS -- so this
# endpoint captures frames server-side, files them into the shared Library,
# and (by default) runs each through the vision tier the same way the
# player-photo review does. The sandbox never needs network egress for this.
# ---------------------------------------------------------------------------
YOUTUBE_FRAMES_MAX = 12
YOUTUBE_FRAMES_REVIEW_MAX = 6
_YTDLP_BIN_CANDIDATES = ('yt-dlp', '/opt/anaconda3/bin/yt-dlp', '/usr/local/bin/yt-dlp')
_FFMPEG_BIN_CANDIDATES = ('ffmpeg', '/usr/local/bin/ffmpeg', '/opt/anaconda3/bin/ffmpeg')


def _find_local_bin(names):
    for name in names:
        if os.path.sep in name:
            if os.path.isfile(name):
                return name
        else:
            found = shutil.which(name)
            if found:
                return found
    return None


def _youtube_frames_dir_rel(vid):
    return os.path.join('media', 'frames', vid)


def _youtube_frame_rel(vid, seconds):
    return os.path.join(_youtube_frames_dir_rel(vid), f'{int(seconds)}s.png')


def _parse_frame_timestamps(raw):
    """Accept seconds as numbers or '90'/'90s'/'1m30s' strings; return a
    sorted, deduped list of non-negative int seconds, capped at
    YOUTUBE_FRAMES_MAX. Malformed entries are dropped, not fatal."""
    out = set()
    for item in (raw or [])[:YOUTUBE_FRAMES_MAX * 4]:
        try:
            if isinstance(item, (int, float)) and item >= 0:
                out.add(int(item))
                continue
            text = str(item).strip().lower()
            if not text:
                continue
            if re.fullmatch(r'\d+', text):
                out.add(int(text))
                continue
            if re.fullmatch(r'\d+s', text):
                out.add(int(text[:-1]))
                continue
            match = re.fullmatch(r'(?:(\d+)m)?(?:(\d+)s)?', text)
            if match and (match.group(1) or match.group(2)):
                out.add(int(match.group(1) or 0) * 60 + int(match.group(2) or 0))
        except (TypeError, ValueError):
            continue
    return sorted(out)[:YOUTUBE_FRAMES_MAX]


def _extract_youtube_frames(url, vid, seconds_list):
    """Capture one PNG per requested timestamp. Frames already filed in the
    Library are reused without any download (cache). Otherwise yt-dlp
    downloads ONE <=720p copy into a private temp dir and ffmpeg seeks each
    frame out of it; the temp dir is always removed. Returns
    {seconds: png_bytes or None} -- a missing entry's None means that frame
    failed and must be reported as failed, never guessed."""
    frames = {}
    missing = []
    for seconds in seconds_list:
        rel = _youtube_frame_rel(vid, seconds)
        target = _safe_library_path(rel)
        data = None
        if target and os.path.isfile(target):
            try:
                with open(target, 'rb') as fh:
                    data = fh.read()
            except OSError:
                data = None
        if data:  # a zero-byte file is NOT a captured frame -- re-capture
            frames[seconds] = data
        else:
            missing.append(seconds)
    if not missing:
        return frames
    ytdlp = _find_local_bin(_YTDLP_BIN_CANDIDATES)
    ffmpeg = _find_local_bin(_FFMPEG_BIN_CANDIDATES)
    if not ytdlp or not ffmpeg:
        for seconds in missing:
            frames[seconds] = None
        return frames
    tmp = tempfile.mkdtemp(prefix='yt-frames-')
    try:
        proc = subprocess.run(
            [ytdlp, '--no-playlist', '-f',
             'bv*[height<=720][ext=mp4]/b[height<=720]/b',
             '-o', os.path.join(tmp, 'video.%(ext)s'), url],
            capture_output=True, timeout=900)
        video_path = None
        for name in sorted(os.listdir(tmp)):
            if name.startswith('video.'):
                video_path = os.path.join(tmp, name)
                break
        if proc.returncode != 0 or not video_path:
            for seconds in missing:
                frames[seconds] = None
            return frames
        for seconds in missing:
            out_path = os.path.join(tmp, f'frame-{seconds}.png')
            seek = subprocess.run(
                [ffmpeg, '-y', '-ss', str(seconds), '-i', video_path,
                 '-frames:v', '1', '-vf', 'scale=1280:-2', out_path],
                capture_output=True, timeout=120)
            if seek.returncode != 0 or not os.path.isfile(out_path):
                frames[seconds] = None
                continue
            with open(out_path, 'rb') as fh:
                data = fh.read()
            rel = _youtube_frame_rel(vid, seconds)
            target = _safe_library_path(rel)
            if target:
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    # BINARY write: _write_file is text-mode and a bytes body
                    # would raise inside f.write, leaving a zero-byte file
                    # that then poisons the cache (the next call would "reuse"
                    # the empty file as a captured frame).
                    with open(target, 'wb') as fh:
                        fh.write(data)
                except Exception:
                    pass  # filing is best-effort; the frame still returns
            frames[seconds] = data
        return frames
    except Exception:
        for seconds in missing:
            frames.setdefault(seconds, None)
        return frames
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _vision_describe_frame(png_bytes, question, source_label):
    """One vision-tier pass over one frame. Same guard rails as the
    player-photo review: the anti-instruction guard, honest-gap instruction,
    and a bounded reply. Returns (ok, text)."""
    slug = _vision_tier_slug()
    if not slug:
        return False, 'vision model tier not available'
    prompt = (
        VISION_ANTI_INSTRUCTION_GUARD + '\n'
        'You are reviewing ONE frame captured from a video the player asked '
        'this system to study. Describe exactly what is visible: UI layout, '
        'panels, nav bars, buttons, text, colors, and any interaction state. '
        'Transcribe legible text exactly. Only report what is actually '
        'visible; if the frame is black, buffering, or shows no UI, say that '
        f'instead of guessing. Source: {source_label}.'
        + (f'\nExtra focus: {question}' if question else '')
    )
    messages = [{'role': 'user', 'content': [
        {'type': 'text', 'text': prompt},
        {'type': 'image_url',
         'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png_bytes).decode()}},
    ]}]
    try:
        result = _call_openrouter_sync(slug, messages, 900)
        reply = (result.get('choices') or [{}])[0].get('message', {}).get('content') or ''
    except Exception as e:
        return False, f'vision call failed: {e}'
    if not reply.strip():
        return False, 'vision call returned no reply'
    return True, reply.strip()[:6000]


@app.post('/api/youtube-frames')
async def youtube_frames(request: Request):
    """Capture frames of a YouTube video at given timestamps, file them to
    library/media/frames/{video_id}/, and (default) run each through the
    vision tier. Body: {agentId, url, timestamps, question?, review?,
    includeImages?}. Responses carry the library path + SHA-256 for every
    frame and the vision text for reviewed ones; base64 bodies only on
    request, so the default response stays small."""
    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    url = (body.get('url') or '').strip()
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'youtube-frames'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    if not _is_youtube_url(url):
        return JSONResponse({'error': 'a valid youtube.com or youtu.be URL is required'}, status_code=400)
    vid = _youtube_video_id(url)
    if not vid:
        return JSONResponse({'error': 'could not resolve a video id from that URL'}, status_code=400)
    seconds_list = _parse_frame_timestamps(body.get('timestamps'))
    if not seconds_list:
        return JSONResponse({'error': 'timestamps must be a non-empty list of seconds'}, status_code=400)
    want_review = body.get('review', True)
    review_budget = seconds_list[:YOUTUBE_FRAMES_REVIEW_MAX] if want_review else []
    log_action(agent_id, 'youtube_frames', {'url': url, 'videoId': vid,
                                            'timestamps': seconds_list,
                                            'review': bool(want_review)},
               authorized=authorized)
    frames = await asyncio.to_thread(_extract_youtube_frames, url, vid, seconds_list)
    if not any(data for data in frames.values()):
        return JSONResponse({'error': 'no frame could be captured (download or seek failed)'},
                            status_code=502)
    question = (body.get('question') or '').strip()[:500]
    out_frames = []
    reviewed = 0
    for seconds in seconds_list:
        data = frames.get(seconds)
        if not data:
            out_frames.append({'timestamp': seconds, 'captured': False,
                               'libraryPath': _youtube_frame_rel(vid, seconds)})
            continue
        entry = {'timestamp': seconds, 'captured': True,
                 'libraryPath': _youtube_frame_rel(vid, seconds),
                 'sha256': hashlib.sha256(data).hexdigest(),
                 'bytes': len(data)}
        if seconds in review_budget:
            ok, text = _vision_describe_frame(
                data, question, f'YouTube video {vid} at {seconds}s')
            entry['visionOk'] = ok
            entry['visionReview'] = text
            reviewed += 1 if ok else 0
        if body.get('includeImages'):
            entry['imageBase64'] = base64.b64encode(data).decode()
        out_frames.append(entry)
    log_action(agent_id, 'youtube_frames_done',
               {'videoId': vid, 'captured': sum(1 for f in out_frames if f.get('captured')),
                'reviewed': reviewed}, authorized=authorized)
    return JSONResponse({'ok': True, 'url': url, 'videoId': vid, 'frames': out_frames})


@app.post('/api/vision-describe')
async def vision_describe(request: Request):
    """The general agent-callable vision tool: one image, one question, the
    vision tier's honest description. Resolves the tier server-side (agents
    never need a model slug), applies the same anti-instruction guard as the
    player-photo review, and rate-limits + agent-key-verifies like every
    other agent endpoint. Body: {agentId, imageBase64, question?}. The
    response is {ok, reply} or {ok: false, note} -- a failed call is a
    finding to report, never a reason to invent what the image shows."""
    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'vision-describe'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    image_b64 = (body.get('imageBase64') or '').strip()
    if not image_b64:
        return JSONResponse({'error': 'imageBase64 is required (raw base64 PNG bytes)'}, status_code=400)
    # Accept a data URL too, since screenshot tools sometimes return one.
    if image_b64.startswith('data:'):
        image_b64 = image_b64.split(',', 1)[-1]
    try:
        data = base64.b64decode(image_b64, validate=True)
    except Exception:
        return JSONResponse({'error': 'imageBase64 is not valid base64'}, status_code=400)
    if not data or len(data) > 12 * 1024 * 1024:
        return JSONResponse({'error': 'image is empty or over the 12 MiB cap'}, status_code=400)
    question = (body.get('question') or '').strip()[:500]
    ok, text = await asyncio.to_thread(_vision_describe_frame, data, question, 'an agent-supplied image')
    log_action(agent_id, 'vision_describe',
               {'bytes': len(data), 'ok': ok, 'question': question[:120]},
               authorized=True)
    if ok:
        return JSONResponse({'ok': True, 'reply': text})
    return JSONResponse({'ok': False, 'note': text})


@app.get('/api/sandbox-backups')
async def sandbox_backups(sandboxId: str):
    if not _SANDBOX_ID_RE.match(sandboxId or ''):
        return JSONResponse({'error': 'invalid sandboxId'}, status_code=400)
    return JSONResponse({'backups': _list_sandbox_backups(sandboxId)})


@app.post('/api/sandbox-backups/restore')
async def sandbox_backups_restore(request: Request):
    # Player-or-admin, same reasoning as /api/library/ingest -- restoring
    # overwrites whatever's currently there (itself snapshotted first, so
    # even a bad restore is undoable), which is a meaningfully
    # consequential action, not something an ordinary agent should trigger
    # on its own judgment about another agent's work.
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    body = await request.json()
    agent_id = body.get('agentId')
    if agent_id != 'player':
        requester = _resolve_requester(request)
        if not (requester and _is_admin(get_state_from_db() or {}, requester)):
            return JSONResponse({'error': "only the player (or the admin) can restore a sandbox backup"}, status_code=403)
    sandbox_id = body.get('sandboxId')
    stamp = body.get('stamp')
    if not sandbox_id or not stamp:
        return JSONResponse({'error': 'sandboxId and stamp are required'}, status_code=400)
    try:
        await asyncio.to_thread(_restore_sandbox_backup, sandbox_id, stamp)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=404)
    log_action('player', 'sandbox_backup_restored', {'sandboxId': sandbox_id, 'stamp': stamp})
    return JSONResponse({'ok': True})


# Gap: a developer/reviewer agent writing real HTML/
# CSS only ever sees its own source text, never what it actually looks
# like rendered -- confirmed directly: a stray note element
# rendering well outside its container was completely invisible from
# reading the code, only showed up in an actual screenshot. Headless
# Chrome (already installed for this machine's own tooling, no new
# dependency) renders the agent's own sandboxed file and returns a real
# PNG, base64-encoded so a caller can hand it straight to a vision-
# capable model's /api/chat call without a separate file round-trip.
# `--host-resolver-rules="MAP * 0.0.0.0"` blackholes all network access
# for this render -- the sandbox's OWN execution is already network-
# isolated (Docker, no route out), and this keeps the SCREENSHOT step
# (which runs on the host, not in that container) from quietly fetching
# something external if the generated HTML ever references one.
SCREENSHOT_TIMEOUT_S = 20
CHROME_PATHS = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/local/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
]


def _find_chrome():
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    return None


def _screenshot_url_sync(url, width=1280, height=900, proxy=None):
    # Same headless-Chrome mechanism as the sandbox screenshot below, but
    # deliberately WITHOUT the network-blackhole flags -- this renders a
    # real external page (already Jev-approved and SSRF-checked by the
    # caller), which needs its own real assets/fonts/styling to look like
    # anything at all. `proxy` routes Chrome through a sandbox VPN exit
    # (--proxy-server), so a visual capture shows the page as that
    # exit's vantage point sees it.
    chrome = _find_chrome()
    if not chrome:
        return None
    out_path = os.path.join(THINK_TANK_DIR, f'.tmp-screenshot-{secrets.token_hex(8)}.png')
    cmd = [
        chrome, '--headless', '--disable-gpu', '--no-sandbox',
        f'--screenshot={out_path}', f'--window-size={width},{height}',
        '--virtual-time-budget=4000', '--hide-scrollbars',
    ]
    if proxy:
        cmd.append(f'--proxy-server={proxy}')
    cmd.append(url)
    try:
        subprocess.run(cmd, capture_output=True, timeout=SCREENSHOT_TIMEOUT_S)
        if not os.path.isfile(out_path):
            return None
        with open(out_path, 'rb') as f:
            return base64.b64encode(f.read()).decode()
    finally:
        if os.path.isfile(out_path):
            os.remove(out_path)
