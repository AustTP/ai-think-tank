

@app.post('/api/selfedit/schedule')
async def selfedit_schedule(request: Request):
    """Agent-facing change request for a one-shot DORMANCY WINDOW.

    Runs the full chain in selfedit.request_window OFF the event loop
    (it blocks on the supervisor review's sequential judge calls):
    conflict scan (standing tasks + other windows) -> supervisor JEV
    judgment -> floor-1.0 escalation whose email link is the player's
    final, mandatory signature. ok=False refusals carry the colliding
    items so the agent can pick a different slot."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'selfedit_schedule'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    date = (body.get('date') or '').strip()
    start = (body.get('start') or '').strip()
    reason = (body.get('reason') or '').strip()
    validator = (body.get('validator') or '').strip()
    if not date or not start or not reason:
        return JSONResponse({'error': 'date (YYYY-MM-DD), start (HH:MM) and '
                                      'reason are required'}, status_code=400)
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.request_window, agent_id, date, start,
            body.get('durationMin'), reason, validator,
            title=body.get('title'),
            description=body.get('description'),
            change_type=body.get('changeType') or 'normal',
            risk_level=body.get('riskLevel') or 'low',
            service_mode=body.get('serviceMode') or 'idle_ok',
            affected_services=body.get('affectedServices'),
            file_manifest=body.get('fileManifest'),
            rollback_plan=body.get('rollbackPlan'),
            rollback_owner=body.get('rollbackOwner'),
            success_criteria=body.get('successCriteria'),
            abort_criteria=body.get('abortCriteria'),
            post_apply_checks=body.get('postApplyChecks'),
            safe_state_plan=body.get('safeStatePlan'),
            restore_plan=body.get('restorePlan'),
            on_call=body.get('onCall'),
            acknowledgements=body.get('acknowledgements'),
            blackout_windows=body.get('blackoutWindows'),
            # No creation-time Telegram. The director -> senior director ->
            # chief of staff chain decides first; the player is only paged by
            # notify_human_of_escalation when every rung fails.
            notify_created=False)
    except Exception as exc:
        print(f'[selfedit] schedule crashed for {agent_id}: {exc}', flush=True)
        log_action(agent_id, 'selfedit_crashed', {'error': str(exc)[:300]},
                   authorized=authorized)
        return JSONResponse({'error': f'the window request harness failed: {exc}'}, status_code=500)
    log_action(agent_id, 'selfedit_schedule',
               {'date': date, 'start': start,
                'durationMin': (payload or {}).get('durationMin'),
                'ok': ok, 'id': (payload or {}).get('id'),
                'note': (((payload or {}).get('error') or '') or
                         ((payload or {}).get('message') or ''))[:160]},
               authorized=authorized)
    return JSONResponse(payload, status_code=200 if ok else 400)


@app.post('/api/selfedit/window/cancel')
async def selfedit_window_cancel(request: Request):
    """Operator-gated cancel for a PENDING or APPROVED dormancy window.

    Same two legitimate callers as the revert endpoint: the player session
    or the chief of staff agent (agentId + key + _is_admin). Cancelled windows never
    fire. Rebooking the same slot afterwards is a NEW request."""
    body = await request.json()
    window_id = (body.get('windowId') or '').strip()
    if not window_id:
        return JSONResponse({'error': 'windowId is required'}, status_code=400)
    agent_id = (body.get('agentId') or '').strip()
    session = verify_session(request.cookies.get(SESSION_COOKIE_NAME))
    admin_agent = None
    if agent_id and verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) \
            and _is_admin(get_state_from_db() or {}, agent_id):
        admin_agent = agent_id
    if not session and not admin_agent:
        return JSONResponse({'error': 'only the player session or the chief of staff '
                                      'agent may cancel a dormancy window'}, status_code=403)
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.cancel_window, window_id,
            'player' if session else admin_agent)
    except Exception as exc:
        print(f'[selfedit] window cancel crashed for {window_id}: {exc}', flush=True)
        return JSONResponse({'error': f'cancel failed: {exc}'}, status_code=500)
    log_action('player' if session else admin_agent, 'selfedit_window_cancel',
               {'windowId': window_id, 'ok': ok,
                'note': ((payload or {}).get('error') or '')[:160]}, authorized=True)
    return JSONResponse(payload, status_code=200 if ok else 400)


@app.post('/api/selfedit/window/veto')
async def selfedit_window_veto(request: Request):
    """Player or chief-of-staff veto for a change request.

    Before the slot fires: cancel it and release the reservations. After it
    fired but before push: roll back every applied edit and block the push.
    A pushed window cannot be vetoed. A reason is always required.
    """
    body = await request.json()
    window_id = (body.get('windowId') or '').strip()
    reason = (body.get('reason') or '').strip()
    if not window_id:
        return JSONResponse({'error': 'windowId is required'},
                            status_code=400)
    if not reason:
        return JSONResponse({'error': 'a reason is required for a veto'},
                            status_code=400)
    session = verify_session(request.cookies.get(SESSION_COOKIE_NAME))
    agent_id = (body.get('agentId') or '').strip()
    admin_agent = None
    if agent_id and verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) \
            and _is_admin(get_state_from_db() or {}, agent_id):
        admin_agent = agent_id
    if not session and not admin_agent:
        return JSONResponse({'error': 'only the player session or the chief '
                                      'of staff agent may veto a change '
                                      'request'}, status_code=403)
    actor = 'player' if session else admin_agent
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.veto_window, window_id, actor, reason)
    except Exception as exc:
        print(f'[selfedit] veto crashed for {window_id}: {exc}', flush=True)
        log_action(actor, 'selfedit_veto_crashed',
                   {'error': str(exc)[:300], 'windowId': window_id},
                   authorized=True)
        return JSONResponse({'error': f'the veto harness failed: {exc}'},
                            status_code=500)
    log_action(actor, 'selfedit_window_veto',
               {'windowId': window_id, 'ok': ok,
                'reason': reason[:200],
                'note': (((payload or {}).get('error') or '') or
                         ((payload or {}).get('message') or ''))[:160]},
               authorized=True)
    return JSONResponse(payload, status_code=200 if ok else 400)


@app.get('/api/selfedit/window/veto-link')
async def selfedit_window_veto_link(windowId: str = '', token: str = '',
                                    reason: str = ''):
    """One-click Telegram veto link for a change request.

    The per-window token is the auth (no player session needed), matching
    the escalation approve/deny links. It delegates to veto_window, so a
    pushed window still refuses and a pending/approved window is cancelled
    with its reserved edits released.
    """
    if not windowId or not token:
        return HTMLResponse('<p>Invalid or expired veto link.</p>',
                            status_code=404)
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.veto_window_by_link, windowId, token,
            (reason or 'Vetoed from Telegram link').strip())
    except Exception as exc:
        log_action('player', 'selfedit_window_veto_link_crashed',
                   {'windowId': windowId, 'error': str(exc)[:300]},
                   authorized=True)
        return HTMLResponse(f'<p>Veto failed: {html.escape(str(exc))}</p>',
                            status_code=500)
    log_action('player', 'selfedit_window_veto_link',
               {'windowId': windowId, 'ok': ok,
                'note': (((payload or {}).get('error') or '') or
                         ((payload or {}).get('message') or ''))[:200]},
               authorized=True)
    if ok:
        return HTMLResponse(
            f'<p>Vetoed change request <b>{html.escape(windowId)}</b>. '
            'Nothing will be pushed.</p>')
    return HTMLResponse(
        f'<p>Could not veto: '
        f'{html.escape(str((payload or {}).get("error") or "unknown error"))}</p>',
        status_code=400)


@app.post('/api/selfedit/fix/edit')
async def selfedit_fix_edit(request: Request):
    """Agent-facing IN-WINDOW corrective splice for an open remediation
    session (change-window contract). Agent-key gated; refuse when no
    session is open. This path is wake-exempt while the session runs, so
    the body must carry the agent identity (agentId) plus the slot headers
    set by the executor."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.submit_remediation_fix, agent_id, body.get('path'),
            body.get('find'), body.get('replace'), body.get('summary'))
    except Exception as exc:
        print(f'[selfedit] fix crashed for {agent_id}: {exc}', flush=True)
        log_action(agent_id, 'selfedit_fix_crashed', {'error': str(exc)[:300]},
                   authorized=authorized)
        return JSONResponse({'error': f'the fix harness failed: {exc}'}, status_code=500)
    log_action(agent_id, 'selfedit_fix_edit',
               {'ok': ok, 'id': (payload or {}).get('id'),
                'window': (payload or {}).get('window'),
                'note': ((payload or {}).get('error') or '')[:160]},
               authorized=authorized)
    return JSONResponse(payload, status_code=200 if ok else 400)


@app.post('/api/selfedit/fix/checks')
async def selfedit_fix_checks(request: Request):
    """Rerun the repo test gate NOW for an open remediation session; a pass
    recovers the failed rows to applied and closes the session."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.run_remediation_checks, agent_id)
    except Exception as exc:
        print(f'[selfedit] recheck crashed: {exc}', flush=True)
        log_action(agent_id, 'selfedit_recheck_crashed',
                   {'error': str(exc)[:300]}, authorized=authorized)
        return JSONResponse({'error': f'the recheck harness failed: {exc}'}, status_code=500)
    log_action(agent_id, 'selfedit_recheck',
               {'ok': ok, 'tests': (payload or {}).get('tests')},
               authorized=authorized)
    return JSONResponse(payload, status_code=200 if ok else 400)


@app.post('/api/selfedit/validate')
async def selfedit_validate(request: Request):
    """Director validator or admin approval for a holding change request."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    try:
        if body.get('adminApproval'):
            ok, payload = await asyncio.to_thread(
                _selfedit.admin_approve_window, agent_id,
                (body.get('windowId') or '').strip(),
                bool(body.get('approved')),
                body.get('note') if isinstance(body.get('note'), str) else '')
        else:
            ok, payload = await asyncio.to_thread(
                _selfedit.validate_window, agent_id,
                (body.get('windowId') or '').strip(),
                bool(body.get('approved')),
                body.get('note') if isinstance(body.get('note'), str) else '',
                bool(body.get('issues')))
    except Exception as exc:
        print(f'[selfedit] validate crashed for {agent_id}: {exc}', flush=True)
        log_action(agent_id, 'selfedit_validate_crashed',
                   {'error': str(exc)[:300]}, authorized=authorized)
        return JSONResponse({'error': f'the validate harness failed: {exc}'},
                            status_code=500)
    log_action(agent_id, 'selfedit_validate',
               {'ok': ok, 'windowId': (body.get('windowId') or '').strip(),
                'approved': bool(body.get('approved')),
                'status': (payload or {}).get('status'),
                'note': (((payload or {}).get('error') or '') or
                         ((payload or {}).get('message') or ''))[:160]},
               authorized=authorized)
    return JSONResponse(payload, status_code=200 if ok else 400)


@app.post('/api/selfedit/read')
async def selfedit_read(request: Request):
    """Agent-facing bounded READ access to the self-edit surface (audit Q3,
    2026-10-18): empty path lists readable world modules; a path reads one
    file's head with its sha256 so exact anchors can be proposed. The read
    rule is smaller than the write rule on purpose; the refusals say why."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.read_module, body.get('path') or '',
            body.get('query') or '', body.get('context') or 40)
    except Exception as exc:
        print(f'[selfedit] read crashed: {exc}', flush=True)
        log_action(agent_id, 'selfedit_read_crashed',
                   {'error': str(exc)[:300]}, authorized=authorized)
        return JSONResponse({'error': f'the read harness failed: {exc}'}, status_code=500)
    log_action(agent_id, 'selfedit_read',
               {'ok': ok, 'path': body.get('path') or '*listing*',
                'note': ((payload or {}).get('error') or '')[:160]},
               authorized=authorized)
    return JSONResponse(payload, status_code=200 if ok else 400)


# Governed agent policy_edit (Phase 3, 2026-10-18). Policy values are DATA,
# not code: an agent proposes ONE key/value change against the operator-policy
# schema. The same JEV quorum that reviews code splices judges the change, the
# key must be on config.AGENT_EDITABLE_POLICY_KEYS (fail-closed otherwise),
# the value must pass _coerce_operator_value, and the write is a read-merge-
# write against the player's operator_policy.json (atomic temp+rename under a
# module lock), followed by config.reload_policy_config() so every bound
# module -- serve, sim, content, sublow -- rebinds without a restart. The
# player keeps final authority: the JSON itself and POST /api/policy/reload
# are untouched, so any agent change can simply be reverted by editing the
# file. No change window is needed: this is decision data landing between
# ticks, not code entering the applied tree.
_POLICY_FILE_LOCK = threading.Lock()

_POLICY_EDIT_INSTRUCTIONS = (
    'You are the policy-change reviewer for a governed self-governing think '
    'tank. Agents may tune DATA knobs (cadences, thresholds, caps) without a '
    'code change, but only through this review. Approve ONLY if the change is '
    'inside the allowed schema range, the reason is concrete and honest, and '
    'the new value plausibly helps the tank more than it hurts it. Refuse '
    'repeated gaming, values that reduce oversight quality, or reasons that '
    'do not match the change.')
_POLICY_EDIT_CRITERIA = (
    'One of: approve, reject, escalate.\n'
    'The key and its allowed range are stated; values outside the range are '
    'impossible by construction.\n')

_POLICY_EDIT_REASONS = (
    'reject', 'escalate')


def _policy_edit_apply(agent_id, key, raw_value, reason):
    """Synchronous governed policy change. Returns (ok, payload) with the
    applied change or a typed error; every attempt is action-logged."""
    import core.config as _cfg
    key = (key or '').strip()
    reason = (reason or '').strip()
    if not key:
        return False, {'error': 'key is required'}
    if len(reason) > 400:
        return False, {'error': 'the reason needs 400 characters or fewer'}
    if key not in _cfg.OPERATOR_POLICY_DEFAULT:
        return False, {'error': f'{key!r} is not an operator-policy key '
                                 '(keys and ranges live in world/'
                                 'operator_policy.json)'}
    if key not in _cfg.AGENT_EDITABLE_POLICY_KEYS:
        return False, {'error': f'{key!r} is operator-only in v1 of '
                                 'policy_edit'}
    value = _cfg._coerce_operator_value(key, raw_value)
    if value is None:
        kind, lo, hi = _cfg._OPERATOR_SCHEMA[key]
        return False, {'error': f'value for {key} must be a {kind} between '
                                f'{lo} and {hi}'}
    current = _cfg.OPERATOR_POLICY.get(key)
    question = (f'Policy change request from {agent_id}.\n'
                f'Key: {key}\n'
                f'Current value: {current!r}\n'
                f'Proposed value: {value!r}\n'
                f'Schema range: {_cfg._OPERATOR_SCHEMA[key]}\n'
                f'Reason: {reason}')
    try:
        decision, confidence, _cost = _jev_quorum_choice_sync(
            _POLICY_EDIT_INSTRUCTIONS, _POLICY_EDIT_CRITERIA + question)
    except Exception as e:
        decision, confidence = None, 1.0
        log_action(agent_id, 'policy_edit_review_failed',
                   {'error': str(e)[:200]}, authorized=False)
        return False, {'error': f'review transport failed: {str(e)[:120]} -- '
                                'nothing changed'}
    if decision not in ('approve',):
        note = _POLICY_EDIT_REASONS[0] if decision == 'reject' else 'refused'
        log_action(agent_id, 'policy_edit_rejected',
                   {'key': key, 'decision': decision,
                    'confidence': round(float(confidence or 0.0), 2),
                    'value': value},
                   authorized=True)
        return False, {'error': f'the policy reviewer said {decision!r} '
                                f'({note}); nothing changed'}
    path = _cfg.OPERATOR_POLICY_PATH
    with _POLICY_FILE_LOCK:
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            log_action(agent_id, 'policy_edit_file_error',
                       {'error': str(e)[:200]}, authorized=False)
            return False, {'error': f'cannot read the policy file: '
                                    f'{str(e)[:120]}'}
        previous = data.get(key, current)
        data[key] = value
        tmp = f'{path}.tmp-{os.getpid()}-{threading.get_ident()}'
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=2)
                fh.write('\n')
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            log_action(agent_id, 'policy_edit_file_error',
                       {'error': str(e)[:200]}, authorized=False)
            return False, {'error': f'cannot write the policy file: '
                                    f'{str(e)[:120]}'}
        _cfg.reload_policy_config()
    applied = _cfg.OPERATOR_POLICY.get(key)
    log_action(agent_id, 'policy_edit',
               {'key': key, 'previous': previous, 'value': applied,
                'confidence': round(float(confidence or 0.0), 2)},
               authorized=True)
    return True, {'ok': True, 'key': key, 'previous': previous,
                  'value': applied,
                  'confidence': round(float(confidence or 0.0), 2),
                  'message': f'{key} is now {applied!r}; every bound module '
                             'rebinds live'}


@app.post('/api/policy/agent_edit')
async def policy_agent_edit(request: Request):
    """The agent lane for policy data changes: one reviewed key/value edit
    against the AGENT_EDITABLE_POLICY_KEYS allowlist. Fails closed on
    unknown keys, out-of-range values, and review refusals."""
    body = await request.json()
    agent_id = body.get('agentId') or 'unknown'
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    if not authorized:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        ok, payload = await asyncio.to_thread(
            _policy_edit_apply, agent_id, body.get('key'),
            body.get('value'), body.get('reason'))
    except Exception as exc:
        print(f'[policy-edit] crashed for {agent_id}: {exc}', flush=True)
        log_action(agent_id, 'policy_edit_crashed',
                   {'error': str(exc)[:300]}, authorized=authorized)
        return JSONResponse({'error': f'the policy harness failed: {exc}'},
                            status_code=500)
    return JSONResponse(payload, status_code=200 if ok else 400)


def _normalize_allowlist_target(target):
    """Normalize a host-or-URL string down to its bare lowercase hostname.
    Returns (host, error). Mirrors the /api/allowlist/request normalization
    so every allowlist path reads a target the same way."""
    parsed = urllib.parse.urlparse(target if '://' in target else 'https://' + target)
    host = (parsed.hostname or '').strip().lower()
    if not host:
        return None, 'could not read a hostname from that target'
    return host, None


@app.get('/api/allowlist')
async def allowlist_get(request: Request):
    """Operator-gated read of the effective browse allowlist, split by origin
    so a caller can tell what a revoke would actually change: the env baseline
    (code-reviewed, .env restart change) vs the runtime grants (DB-persisted,
    revocable live). Same gate as the other registries: player session or a
    director/admin agent."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return JSONResponse({
        'ok': True,
        'envBaseline': sorted(BROWSE_ALLOWLIST_DOMAINS),
        'runtimeGrants': sorted(_runtime_allowlist_grants()),
        'effective': sorted(_effective_allowlist_domains()),
        'readBy': actor,
    })


@app.post('/api/allowlist/grant')
async def allowlist_grant_endpoint(request: Request):
    """Operator-gated DIRECT allowlist grant -- the director/admin path, next
    to the existing player-email escalation. Body: {"host": ..., "purpose":
    "..."}. Same SSRF boundary as /api/allowlist/request: private/internal
    hosts are refused outright, the allowlist extends PUBLIC reachability
    only. An already-effective host is a no-op (idempotent), so a director
    retrying never double-grants. Gated by _operator_gate: player session or
    a director/admin agent -- an ordinary agent's key is refused, and every
    grant is attributed in the action log with this endpoint as the `via`."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- malformed body -> 400
        return JSONResponse({'error': 'request body must be JSON'}, status_code=400)
    target = (body.get('host') or body.get('url') or '').strip()
    purpose = (body.get('purpose') or '').strip()
    if not target:
        return JSONResponse({'error': 'a host or url is required'}, status_code=400)
    host, norm_err = _normalize_allowlist_target(target)
    if norm_err:
        return JSONResponse({'error': norm_err}, status_code=400)
    if not _is_safe_public_host(host):
        return JSONResponse({'error': f'{host} is a private or internal host and can never be allowlisted'}, status_code=400)
    if _is_allowlisted_host(host):
        return JSONResponse({'ok': True, 'host': host, 'granted': False,
                             'message': f'{host} is already on the allowlist -- nothing to grant.'})
    granted = _grant_allowlist(host, actor=actor, via='operator grant')
    return JSONResponse({'ok': True, 'host': granted, 'granted': True,
                         'message': f'{granted} is now on the allowlist (egress proxy refreshed).',
                         'grantedBy': actor, 'purpose': purpose[:200]})


@app.post('/api/allowlist/revoke')
async def allowlist_revoke_endpoint(request: Request):
    """Operator-gated removal of a RUNTIME allowlist grant -- the mirror of the
    grant endpoint, so a host that changes standing later can be taken back
    off without a restart. Body: {"host": ..., "purpose": "..."}.

    Only runtime grants (the DB-persisted additions layered on top of the env
    baseline) are removable; a host that only exists in the code-reviewed env
    baseline is refused with a pointer to edit .env, so the effective list
    always stays a superset of the reviewed baseline. Removing an absent host
    is a no-op (idempotent), so a director retrying never errors. Gated by
    _operator_gate: player session or a director/admin agent."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- malformed body -> 400
        return JSONResponse({'error': 'request body must be JSON'}, status_code=400)
    target = (body.get('host') or body.get('url') or '').strip()
    purpose = (body.get('purpose') or '').strip()
    if not target:
        return JSONResponse({'error': 'a host or url is required'}, status_code=400)
    host, norm_err = _normalize_allowlist_target(target)
    if norm_err:
        return JSONResponse({'error': norm_err}, status_code=400)
    result = _revoke_allowlist(host, actor=actor, via='operator revoke')
    if result is None:
        return JSONResponse({'ok': True, 'host': host, 'revoked': False,
                             'message': f'{host} is not on the allowlist -- nothing to revoke.'})
    if result == 'not_granted':
        return JSONResponse({'error': f'{host} is part of the code-reviewed env baseline, not a runtime '
                                      f'grant -- edit BROWSE_ALLOWLIST_DOMAINS in .env and restart to '
                                      f'remove it.'}, status_code=400)
    return JSONResponse({'ok': True, 'host': result, 'revoked': True,
                         'message': f'{result} removed from the allowlist (egress proxy refreshed).',
                         'revokedBy': actor, 'purpose': purpose[:200]})


CURL_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'}
CURL_MAX_BODY_BYTES = 200_000
CURL_TIMEOUT_S = 15

# An agent without standing access to something
# (e.g. an agent with no Weather Station curl access) should be able to
# ask their supervisor for it and get REAL, TEMPORARY access if the reason
# is legitimate -- a real, gated, expiring exception layered on top of the
# existing room check, not a way around it. Starts
# with curl; the mechanism generalizes to
# any future room/role-gated capability by just adding it here.
TEMP_ACCESS_CAPABILITIES = {
    'curl': 'Real HTTP requests to the open internet -- normally restricted to inside the Weather Station.',
    'sandbox-download': 'Downloading a file directly into a sandbox -- normally restricted to inside the Work Room or the Observatory.',
}
# Long enough to actually finish one real task, short enough that this is
# genuinely temporary rather than a backdoor standing grant.
TEMP_ACCESS_DURATION_S = 1200


def _has_active_temp_access(agent_id, capability):
    with _db() as conn:
        row = conn.execute(
            'SELECT expires_at FROM temp_access_grants WHERE agent_id = ? AND capability = ?',
            (agent_id, capability),
        ).fetchone()
    # NULL expires_at = a story-scoped grant: no timer, active until its story
    # ships (revoke_task_access on completion). Anything else must still be
    # inside its window.
    return bool(row and (row[0] is None or row[0] > time.time()))


def _grant_temp_access(agent_id, capability, granted_by, reason, task_id=None):
    now = time.time()
    # A story-scoped grant rides NO clock: it lives until the story it was
    # granted for ships (revoke_task_access on completion). Only a grant with
    # no story keeps the finite window, so nothing is ever permanent.
    expires_at = None if task_id else now + TEMP_ACCESS_DURATION_S
    with _db() as conn:
        conn.execute(
            'INSERT INTO temp_access_grants (agent_id, capability, granted_by, reason, granted_at, expires_at, task_id) VALUES (?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(agent_id, capability) DO UPDATE SET granted_by=excluded.granted_by, reason=excluded.reason, granted_at=excluded.granted_at, expires_at=excluded.expires_at, task_id=excluded.task_id',
            (agent_id, capability, granted_by, reason, now, expires_at, task_id),
        )
    return expires_at


def revoke_task_access(task_id):
    """Per-story capability grant lifecycle: a temp grant is tied to the SPECIFIC
    story it was granted for (task_id), so the moment that story ships the grant
    dies with it -- capability access never outlives the work that justified it.
    Best-effort (a missing DB must never break a completion path)."""
    if not task_id:
        return
    try:
        with _db() as conn:
            conn.execute('DELETE FROM temp_access_grants WHERE task_id = ?', (task_id,))
    except Exception:
        pass


def _agent_is_in_weatherstation(agent_id, live_room=None):
    # Real server-side room gate: curl only
    # from the Weather Station, same exclusivity Eli's own profile already
    # states for outside/internet access generally (Studio's screenshot-
    # based visual research is the one other exception, and that goes
    # through /api/browse's existing Jev gate, not this raw-HTTP one).
    # Checked against the real persisted state (autosaved every 5s), not
    # trusted from whatever the client claims in the request -- a client
    # could lie about its own location, but it can't rewrite what was
    # already saved to the server's own database.
    #
    # Race fixed: an agent's `inRoom` is
    # maintained in the browser in real-time but only persisted to the DB
    # on the client's 5s autosave. So an agent arriving in the Weather
    # Station and making its very first curl call within those 5 seconds
    # could have `inRoom: 'weatherstation'` live but not yet autosaved --
    # and this gate, reading the stale saved state, would wrongly deny a
    # legitimate in-room request. The fix: accept the calling client's
    # LIVE assertion of the agent's current room (`live_room`, passed in
    # the request body) as an override, validated against the exact room
    # that grants this capability. The client is the origin of all
    # position truth anyway (agents move in the browser); this just closes
    # the autosave-latency gap. The claimed room must still be the one
    # real eligible room -- an agent can't assert an arbitrary room to
    # unlock a capability.
    #
    # 'player' is a real exception, not a loophole: the human player has
    # no `inRoom` entry at all (that field only exists for NPC agents), so
    # this would otherwise ALWAYS block the player even when standing
    # right in the Weather Station -- which is the only way this UI is
    # ever reachable in the first place (openTerminal()'s own room gate,
    # index.html). Every other identity still gets the real check.
    if agent_id == 'player':
        return True
    # live_room override first -- it's the freshest truth an agent has.
    if live_room == 'weatherstation':
        return True
    state = get_state_from_db()
    if not state:
        return False
    agent = (state.get('agents') or {}).get(agent_id)
    return bool(agent and agent.get('inRoom') == 'weatherstation')


SANDBOX_DOWNLOAD_ROOMS = ('observatory', 'pressoffice')


def _agent_is_in_sandbox_room(agent_id, live_room=None):
    # Real server-side room gate, extended to
    # the Work Room: downloading directly into a sandbox
    # only works from inside a room that actually has one -- Observatory
    # (RESEARCH_SANDBOX_ID) or Work Room (WORKROOM_SANDBOX_ID) -- same
    # exclusivity curl draws around the Weather Station
    # (_agent_is_in_weatherstation, right above). Checked against the
    # real persisted state, not trusted from whatever the client claims.
    # `live_room` is the agent's current in-room as asserted by the
    # calling client in real-time -- accepted as the same autosave-latency
    # override described in _agent_is_in_weatherstation (an agent that
    # just arrived hasn't had its `inRoom` autosaved yet, and the very
    # first sandbox call would otherwise be wrongly denied).
    if agent_id == 'player':
        return True
    if live_room in SANDBOX_DOWNLOAD_ROOMS:
        return True
    state = get_state_from_db()
    if not state:
        return False
    agent = (state.get('agents') or {}).get(agent_id)
    return bool(agent and agent.get('inRoom') in SANDBOX_DOWNLOAD_ROOMS)


API_CALL_MAX_BODY_BYTES = 200_000
# 60s (was 20): Overpass QL evaluation routinely takes 10-60s on public
# infrastructure; the 2026-10-30 Tier-1 probe saw two Overpass reads die at
# the 20s ceiling while the ask model then invented numbers from the failed
# calls. The call is rate-limited and Bank-metered, so a longer ceiling is
# bounded.
API_CALL_TIMEOUT_S = 60
# Generation workflows (PixelLab, Higgsfield) submit/poll long jobs. A single
# request can legitimately outlive the plain api_call timeout, so the workflow
# interpreter gets its own per-request ceiling; the workflow's own poll
# timeout still bounds the whole job.
API_WORKFLOW_REQUEST_TIMEOUT_S = 90


def _api_request_sync(method, url, headers, body):
    """Raw outbound HTTP for /api/api-call, mirroring _curl_request_sync's
    redirect re-check (a redirect is exactly how an allowed-looking URL could
    end up pointed at an internal address) and size cap. Returns
    {'status', 'finalUrl', 'contentType', 'body', 'truncated'}.

    GETs without credentials are cached with ETag/Last-Modified revalidation.
    Authenticated calls and writes are never cached.
    """
    method = (method or 'GET').upper()
    cacheable = (method == 'GET'
                 and not any(str(k).lower() in ('authorization', 'cookie', 'x-api-key')
                             for k in (headers or {})))
    cached = _read_http_cache(url, None, 'api') if cacheable else None
    if cached and cached.get('expires', 0) > time.time():
        return {'status': 200, 'finalUrl': cached.get('final_url') or url,
                'contentType': cached.get('content_type') or '',
                'body': cached.get('body') or '',
                'truncated': bool(cached.get('truncated'))}
    req_headers = dict(headers or {})
    if cached and cached.get('etag'):
        req_headers['If-None-Match'] = cached['etag']
    elif cached and cached.get('last_modified_header'):
        req_headers['If-Modified-Since'] = cached['last_modified_header']
    data = json.dumps(body).encode() if (body is not None and method not in ('GET', 'HEAD')) else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    if 'User-Agent' not in {k.title() for k in req_headers}:
        req.add_header('User-Agent', 'AIThinkTankAgent/1.0')
    try:
        with _safe_urlopen(req, timeout=API_CALL_TIMEOUT_S) as resp:  # registry/SSRF-gated hosts only; _safe_urlopen pins each resolved IP so DNS cannot rebind to an internal address; re-checked below after redirects
            final_url = resp.geturl()
            final_host = urllib.parse.urlparse(final_url).hostname
            if not _is_safe_public_host(final_host):
                raise ValueError('redirected to a disallowed host')
            raw = resp.read(API_CALL_MAX_BODY_BYTES + 1)
            truncated = len(raw) > API_CALL_MAX_BODY_BYTES
            raw = raw[:API_CALL_MAX_BODY_BYTES]
            result = {
                'status': resp.status,
                'finalUrl': final_url,
                'contentType': (resp.headers.get('content-type') or ''),
                'body': raw.decode(errors='replace'),
                'truncated': truncated,
            }
            if cacheable and resp.status == 200:
                ttl = _cache_ttl_from_headers(resp.headers)
                if ttl is not None:
                    _write_http_cache(url, None, final_url,
                                      result['contentType'], result['body'],
                                      truncated, resp.headers.get('ETag'),
                                      _parse_http_date_ms(resp.headers.get('Last-Modified')),
                                      ttl, resp.headers.get('Last-Modified') or '',
                                      'api')
            return result
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            return {'status': 200, 'finalUrl': cached.get('final_url') or url,
                    'contentType': cached.get('content_type') or '',
                    'body': cached.get('body') or '',
                    'truncated': bool(cached.get('truncated'))}
        raise


def _api_build_text(result, service, registered, hostname):
    """Turn a raw fetch result into (parsed_json, ctype, wrapped, instruction)
    -- JSON kept structured, HTML stripped to text, else raw; capped,
    truncated, redacted, and always wrapped as untrusted external DATA."""
    body = result['body']
    ctype = (result.get('contentType') or '').lower()
    parsed_json = None
    if 'json' in ctype:
        try:
            parsed_json = json.loads(body)
            text = json.dumps(parsed_json, indent=2, ensure_ascii=False)[:20000]
        except ValueError:
            text = body[:20000]
    elif 'html' in ctype:
        text = _strip_html_to_text(body)[:20000]
    else:
        text = body[:20000]
    text = _redact_secrets(text)
    if result.get('truncated'):
        text += '\n[truncated: response larger than the size cap]'
    wrapped, _nonce, _tag, instruction = wrap_external_content(
        text, f'a {service.get("name", hostname)} API response' if registered else 'an external API response')
    return parsed_json, ctype, wrapped, instruction


def _api_accrue(spend_kind, ctype, parsed_json):
    """Accrue spend from a response per the registry's spend rule. Page-request
    budget for the services that consume it; Apify's real reported cost from
    the run object."""
    kind = (spend_kind or {}).get('kind')
    if kind == 'page_request':
        _accrue_page_request()
    elif kind == 'apify' and isinstance(parsed_json, dict):
        try:
            usage = float(parsed_json.get('usageTotalUsd') or 0)
        except (ValueError, TypeError):
            usage = 0.0
        if usage:
            _accrue_apify_spend(usage)


def _api_workflow_url(base_url, template, values):
    """Substitute {field} placeholders in a workflow path template and join to
    the service base URL. Missing placeholders raise so a misconfigured spec
    fails closed instead of hitting a raw '/{field}' path."""
    formatted = template
    for key, val in (values or {}).items():
        formatted = formatted.replace('{' + key + '}', str(val))
    if '{' in formatted:
        raise ValueError(f'workflow path template still has unresolved placeholders: {template}')
    return base_url.rstrip('/') + ('/' + formatted.lstrip('/') if formatted else '')


def _api_execute_workflow(service, spend_kind, base_url, method, req_headers, req_body, workflow_params, agent_id=None):
    """Run a registered service's multi-step workflow (submit -> poll ->
    optional finalize), with the registry's spend rule applied. This is the
    generic engine behind the higgsfield (image/video) and pixellab
    (character) generation tools -- the bespoke _higgsfield_generate /
    pixellab orchestration is replaced by this config-driven interpreter.
    Returns the same shape as a single _api_execute result, with the final
    artifact's JSON under 'data'."""
    spec = (service or {}).get('workflow') or {}
    values = dict(workflow_params or {})
    req_headers = dict(req_headers or {})

    def _request(path_template, body, method, timeout=API_WORKFLOW_REQUEST_TIMEOUT_S):
        url = _api_workflow_url(base_url, path_template, values)
        result = _api_request_sync(method, url, req_headers, body)
        try:
            return json.loads(result['body'])
        except (ValueError, TypeError):
            return None

    # Spend resolution.
    spend = (spec.get('spend') or {})
    spend_service = spend.get('service')
    if spend.get('kind') == 'estimate_first':
        estimate = _request(spec['estimate']['path_template'], req_body, 'POST')
        usd = 0.0
        try:
            usd = float((estimate or {}).get(spec['estimate'].get('usd_field', 'usd')) or 0)
        except (ValueError, TypeError):
            usd = 0.0
    # Registry rows written before the base-url/path split may carry the full
    # '/v2/balance' against a base_url that already ends in '/v2'. Normalize it
    # here so an existing DB row keeps working without a manual migration.
    balance_path = spend.get('balance_path', '/v2/balance')
    if base_url.rstrip('/').endswith('/v2') and str(balance_path).startswith('/v2/'):
        balance_path = str(balance_path)[len('/v2'):]
    balance_before = None
    if spend.get('kind') == 'balance_delta':
        bal = _request(balance_path, None, 'GET')
        balance_before = (bal or {}).get('balance')

    # Submit.
    submit_spec = spec.get('submit') or {}
    submit_path = submit_spec.get('path_template') or '/'
    submit_resp = _request(submit_path, req_body, submit_spec.get('method', 'POST'))
    if not isinstance(submit_resp, dict):
        raise ValueError(f'submit step did not return JSON: {submit_resp}')
    id_field = submit_spec.get('id_field')
    job_id = submit_resp.get(id_field)
    if not job_id:
        raise ValueError(f'submit response missing "{id_field}": {json.dumps(submit_resp)[:300]}')
    values[id_field] = job_id
    for extra in submit_spec.get('extra_ids') or []:
        if submit_resp.get(extra):
            values[extra] = submit_resp[extra]

    # Poll.
    poll_spec = spec.get('poll') or {}
    terminal = set(poll_spec.get('terminal') or ['completed', 'failed'])
    success = set(poll_spec.get('success') or [])
    interval = float(poll_spec.get('interval_s') or 6)
    timeout_s = float(poll_spec.get('timeout_s') or 120)
    poll_path = poll_spec.get('path_template')
    poll_resp = submit_resp
    final_status = (poll_resp or {}).get('status') or 'RUNNING'
    deadline = time.time() + timeout_s
    if poll_path:
        while True:
            poll_resp = _request(poll_path, None, poll_spec.get('method', 'GET')) or {}
            final_status = poll_resp.get('status') or 'RUNNING'
            if final_status in terminal:
                break
            if time.time() >= deadline:
                raise TimeoutError(f'workflow poll timed out after {timeout_s:.0f}s (last status {final_status})')
            time.sleep(interval)

    # Finalize (fetch the artifact once complete).
    finalize_path = ((spec.get('finalize') or {}).get('path_template')) if spec.get('finalize') else None
    final_data = poll_resp
    if finalize_path:
        final_data = _request(finalize_path, None, (spec.get('finalize') or {}).get('method', 'GET')) or {}

    # Accrue spend ONLY on a successful terminal -- a failed/canceled/NSFW
    # generation is never charged (same rule _higgsfield_generate enforced).
    charged = bool(success) and final_status in success
    actual_cost = None
    if charged and spend.get('kind') == 'estimate_first' and usd > 0:
        _accrue_spend(spend_service, usd, agent_id=agent_id)
    elif charged and spend.get('kind') == 'balance_delta':
        bal = _request(balance_path, None, 'GET')
        balance_after = (bal or {}).get('balance')
        if isinstance(balance_before, (int, float)) and isinstance(balance_after, (int, float)):
            real_cost = max(0.0, balance_before - balance_after)
            actual_cost = real_cost
            if real_cost > 0:
                _accrue_spend(spend_service, real_cost, agent_id=agent_id)

    return {'data': final_data, 'status': final_status, 'contentType': 'application/json',
            'usd': usd if spend.get('kind') == 'estimate_first' else None,
            'actualUsd': actual_cost, 'ids': values}
