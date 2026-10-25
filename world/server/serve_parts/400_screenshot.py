

@app.post('/api/screenshot')
async def screenshot(request: Request):
    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    sandbox_id = body.get('sandboxId')
    sandbox_id = _village_sandbox_id(agent_id, sandbox_id)  # per-village repo isolation
    if not _SANDBOX_ID_RE.match(sandbox_id or ''):
        return JSONResponse({'error': 'invalid sandboxId'}, status_code=400)
    rel_path = body.get('path', 'index.html')
    try:
        width = min(max(int(body.get('width', 900)), 200), 1600)
        height = min(max(int(body.get('height', 700)), 200), 1600)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'width and height must be integers'}, status_code=400)
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'screenshot'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if not sandbox_id:
        return JSONResponse({'error': 'sandboxId is required'}, status_code=400)

    sandbox_dir = _sandbox_dir_for(sandbox_id)
    # Same path-traversal guard shape as _safe_library_path -- must
    # resolve to strictly inside this sandbox's own directory.
    target = os.path.normpath(os.path.join(sandbox_dir, rel_path))
    if target != sandbox_dir and not target.startswith(sandbox_dir + os.sep):
        return JSONResponse({'error': 'invalid path'}, status_code=400)
    if not os.path.isfile(target):
        return JSONResponse({'error': f'{rel_path} not found in this sandbox'}, status_code=404)

    chrome = _find_chrome()
    if not chrome:
        return JSONResponse({'error': 'no headless-capable browser found on this machine'}, status_code=501)

    out_path = target + f'.screenshot-{secrets.token_hex(4)}.png'
    cmd = [
        chrome, '--headless', '--disable-gpu', '--no-sandbox',
        '--host-resolver-rules=MAP * 0.0.0.0', '--disable-background-networking',
        # The resolver rule only blocks DNS. Route every actual connection
        # through a dead proxy so an IP literal or localhost cannot bypass it.
        '--proxy-server=127.0.0.1:9', '--proxy-bypass-list=<-loopback>',
        f'--screenshot={out_path}', f'--window-size={width},{height}',
        '--virtual-time-budget=2000', f'file://{target}',
    ]
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=SCREENSHOT_TIMEOUT_S)
        if not os.path.isfile(out_path):
            log_action(agent_id, 'screenshot', {'sandboxId': sandbox_id, 'path': rel_path, 'ok': False}, authorized=None)
            return JSONResponse({'error': 'screenshot failed', 'stderr': result.stderr.decode(errors='replace')[-2000:]}, status_code=502)
        with open(out_path, 'rb') as f:
            image_b64 = base64.b64encode(f.read()).decode()
        os.remove(out_path)
        log_action(agent_id, 'screenshot', {'sandboxId': sandbox_id, 'path': rel_path, 'ok': True}, authorized=None)
        return JSONResponse({'imageBase64': image_b64, 'width': width, 'height': height})
    except subprocess.TimeoutExpired:
        return JSONResponse({'error': 'screenshot timed out'}, status_code=504)


# Real capability gap this closes: several separate fix attempts, across
# models spanning a wide real coding-benchmark spread, all independently
# guessed at the same nonexistent hook (a body class, a global function
# name) because none of them had any way to check what the actual running
# page exposes. A static file read plus one screenshot can show what a page
# LOOKS like; neither can click a button, press a real key, wait, and then
# check what actually
# happened. This can.
PAGE_PROBE_TIMEOUT_S = 20
PAGE_PROBE_MAX_ACTIONS = 40
PAGE_PROBE_MAX_PROBES = 20
PAGE_PROBE_MAX_WAIT_MS = 5000
PAGE_PROBE_MAX_RESULT_CHARS = 20000
PAGE_PROBE_MAX_GLOBAL_KEYS = 100


def _bounded_probe_result(value, max_chars=PAGE_PROBE_MAX_RESULT_CHARS):
    """Keep a probe result JSON-transportable and bounded. A page can return a
    huge object from one evaluate; without this the endpoint serializes it all
    and can stall the server."""
    try:
        text = json.dumps(value, default=str)
    except Exception:
        text = repr(value)
    if len(text) <= max_chars:
        return value
    return {'truncated': True, 'chars': len(text), 'preview': text[:max_chars]}


def _page_probe_sync(file_path, actions, probes):
    console_messages = []
    page_errors = []
    action_log = []
    results = {}

    with sync_playwright() as p:
        # Same network blackhole as /api/screenshot's sandbox-file path --
        # this loads the agent's OWN sandbox code, not a real external
        # site, so there's no reason it should ever reach the internet.
        browser = p.chromium.launch(
            headless=True,
            args=['--host-resolver-rules=MAP * 0.0.0.0', '--disable-background-networking',
                  '--proxy-server=127.0.0.1:9', '--proxy-bypass-list=<-loopback>'],
        )
        try:
            # A fresh blank page in the same browser/engine gives the real
            # baseline set of `window` properties. Diffing the loaded
            # page's `window` against this tells the caller exactly what
            # NEW globals the page itself actually defines -- instead of
            # making it guess likely-sounding names one probe at a time.
            # Real gap this closes: a fix attempt guessed at
            # window.FDJudge, then window.handlePadInputScoring, then
            # window.animateNotes -- three different names across three
            # attempts, none of them real -- because nothing ever told it
            # what actually IS there. An inventory beats a multiple-choice
            # quiz the caller has to keep re-guessing.
            baseline_page = browser.new_page()
            baseline_globals = set(baseline_page.evaluate('Object.keys(window)'))
            baseline_page.close()

            page = browser.new_page()

            def _deny_non_file(route):
                url = (route.request.url or '').lower()
                if url.startswith(('file:', 'data:', 'blob:')):
                    route.continue_()
                else:
                    route.abort()

            # Belt and braces with the dead proxy: abort anything that is not
            # the local sandbox file. This catches http(s), ws(s), and IP
            # literals that a DNS blackhole alone would let through.
            page.route('**/*', _deny_non_file)
            page.on('console', lambda msg: console_messages.append(f'[{msg.type}] {msg.text}'))
            page.on('pageerror', lambda exc: page_errors.append(str(exc)))
            page.goto(f'file://{file_path}', timeout=PAGE_PROBE_TIMEOUT_S * 1000)

            # A bounded, real interaction sequence -- 'keydown' uses
            # Playwright's actual OS-level key dispatch (page.keyboard),
            # not a synthetic dispatchEvent() from inside page JS, so
            # there's no ambiguity about whether a real user's keypress
            # would behave differently than what got tested here.
            for i, action in enumerate(actions[:PAGE_PROBE_MAX_ACTIONS]):
                kind = action.get('type')
                try:
                    if kind == 'click':
                        page.click(action['selector'], timeout=3000)
                    elif kind == 'keydown':
                        page.keyboard.press(action['key'])
                    elif kind == 'wait':
                        try:
                            ms = min(int(action.get('ms', 200)), PAGE_PROBE_MAX_WAIT_MS)
                        except (TypeError, ValueError):
                            ms = min(200, PAGE_PROBE_MAX_WAIT_MS)
                        page.wait_for_timeout(ms)
                    elif kind == 'eval':
                        page.evaluate(action['code'])
                    else:
                        action_log.append(f'action {i} ({kind}): unknown action type')
                        continue
                    action_log.append(f'action {i} ({kind}): ok')
                except Exception as e:
                    # A failed action (e.g. a selector that doesn't exist)
                    # is itself real diagnostic signal -- report it and
                    # keep going, rather than aborting the whole probe.
                    action_log.append(f'action {i} ({kind}): FAILED -- {e}')

            # Evaluated AFTER every action, against whatever the page's
            # real state actually is now -- this is the whole point, the
            # thing a static read and a screenshot can't give: "does
            # window.FDJudge exist," "what is body.className," "what does
            # the score element actually say right now."
            for expr in probes[:PAGE_PROBE_MAX_PROBES]:
                try:
                    results[expr] = _bounded_probe_result(page.evaluate(expr))
                except Exception as e:
                    results[expr] = f'ERROR: {e}'

            # One level deeper than just names: a bug found
            # building this exact tool -- a fix correctly found and used
            # window._fdGameState (a real global this inventory surfaced),
            # but then guessed at a `.playing` property on it that doesn't
            # exist (the real one is `.isInActivePlay`). The name alone
            # wasn't enough; the shape is what actually answers "what's
            # really in here" instead of leaving one more thing to guess.
            try:
                custom_global_names = sorted(set(page.evaluate('Object.keys(window)')) - baseline_globals)
                custom_globals = page.evaluate(
                    '''(names) => names.map(name => {
                        let v;
                        try { v = window[name]; } catch (e) { return { name, type: 'ERROR', error: String(e) }; }
                        if (v === null) return { name, type: 'null' };
                        if (Array.isArray(v)) return { name, type: 'array', length: v.length };
                        if (typeof v === 'object' && !(v instanceof Node)) {
                            let keys = [];
                            try { keys = Object.keys(v).slice(0, 100); } catch (e) {}
                            return { name, type: 'object', keys };
                        }
                        // A bug found building this tool: a fix
                        // re-used a guard flag (_fdMenuDupPatched) an
                        // EARLIER fix attempt had already set true, so its
                        // own genuinely-correct removal logic short-
                        // circuited at `if (already) return;` before ever
                        // running. Showing only the TYPE ('boolean') never
                        // would have surfaced that; primitives are cheap
                        // to serialize, so their actual VALUE is included
                        // -- exactly the signal "this guard already fired"
                        // needs.
                        return { name, type: typeof v, value: v };
                    })''',
                    custom_global_names,
                )
            except Exception as e:
                custom_globals = [{'name': 'ERROR', 'type': 'ERROR', 'error': str(e)}]
        finally:
            browser.close()

    return {
        'actionLog': action_log,
        'console': console_messages[-100:],
        'pageErrors': page_errors[-50:],
        'customGlobals': custom_globals,
        'results': results,
    }


@app.post('/api/page-probe')
async def page_probe(request: Request):
    body = await request.json()
    agent_id = body.get('agentId', 'unknown')
    sandbox_id = body.get('sandboxId')
    sandbox_id = _village_sandbox_id(agent_id, sandbox_id)  # per-village repo isolation
    if not _SANDBOX_ID_RE.match(sandbox_id or ''):
        return JSONResponse({'error': 'invalid sandboxId'}, status_code=400)
    rel_path = body.get('path', 'index.html')
    actions = body.get('actions') or []
    probes = body.get('probes') or []

    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'page-probe'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if sync_playwright is None:
        return JSONResponse({'error': 'playwright is not installed on this machine'}, status_code=501)
    if not sandbox_id:
        return JSONResponse({'error': 'sandboxId is required'}, status_code=400)
    if not isinstance(actions, list) or not isinstance(probes, list):
        return JSONResponse({'error': 'actions and probes must be arrays'}, status_code=400)
    if not probes:
        return JSONResponse({'error': 'at least one probe expression is required -- this checks real page state, it does not just drive the page blind'}, status_code=400)

    sandbox_dir = _sandbox_dir_for(sandbox_id)
    # Same path-traversal guard shape as /api/screenshot -- must resolve to
    # strictly inside this sandbox's own directory.
    target = os.path.normpath(os.path.join(sandbox_dir, rel_path))
    if target != sandbox_dir and not target.startswith(sandbox_dir + os.sep):
        return JSONResponse({'error': 'invalid path'}, status_code=400)
    if not os.path.isfile(target):
        return JSONResponse({'error': f'{rel_path} not found in this sandbox'}, status_code=404)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_page_probe_sync, target, actions, probes),
            timeout=PAGE_PROBE_TIMEOUT_S * 2,
        )
    except asyncio.TimeoutError:
        log_action(agent_id, 'page_probe', {'sandboxId': sandbox_id, 'path': rel_path, 'ok': False, 'error': 'timed out'}, authorized=None)
        return JSONResponse({'error': 'probe timed out'}, status_code=504)
    except Exception as e:
        log_action(agent_id, 'page_probe', {'sandboxId': sandbox_id, 'path': rel_path, 'ok': False, 'error': str(e)}, authorized=None)
        return JSONResponse({'error': f'probe failed: {e}'}, status_code=502)

    log_action(agent_id, 'page_probe', {
        'sandboxId': sandbox_id, 'path': rel_path, 'ok': True,
        'actionCount': len(actions), 'probeCount': len(probes),
    }, authorized=None)
    return JSONResponse(result)


@app.get('/api/escalation/resolve')
async def resolve_escalation(id: str, token: str, decision: str):
    # Tappable from the email itself, no app or login needed -- the whole
    # point is answering from an iPhone in whatever moment you actually
    # see it. The token (not just the id) is what makes this link
    # unguessable; id alone would be sequential.
    escalations = _load_escalations()
    esc = escalations.get(id)
    if not esc or esc.get('token') != token:
        return HTMLResponse('<p>Invalid or expired link.</p>', status_code=404)
    if esc['status'] != 'pending':
        return HTMLResponse(f"<p>Already resolved: {esc['status']}.</p>")
    if decision not in ('approve', 'deny'):
        return HTMLResponse('<p>Invalid decision.</p>', status_code=400)

    esc['status'] = 'approved' if decision == 'approve' else 'denied'
    esc['resolvedBy'] = 'admin'
    esc['resolvedAt'] = time.time()
    escalations[id] = esc
    _save_escalations(escalations)
    # A human resolving this kind clears its judge cross-check drift circuit --
    # the director's delegation for that kind is actionable again (a tripped
    # circuit had routed it all to the human's link; a real human answer resets
    # that signal, exactly like a repair of the underlying failure).
    _reset_escalation_judge_drift(esc.get('kind') or 'unknown')
    # Approval side-effect: an approved 'allowlist request' or 'allowlist
    # candidate' turns the note's host into a REAL runtime allowlist grant.
    # For 'allowlist request' (floor 1.0) this link is the only path; for
    # 'allowlist candidate' (delegable) the director loop can also apply it,
    # and this link is the human override. The note field carries the
    # hostname; on deny nothing is granted.
    if decision == 'approve' and esc.get('kind') in ('allowlist request', 'allowlist candidate'):
        granted = _grant_allowlist(esc.get('note'))
        if granted:
            return HTMLResponse(f"<p>Recorded: <b>approved</b> for {esc['kind']}.</p>"
                                f"<p>{html.escape(esc['question'])}</p>"
                                f"<p>Allowlist grant applied: <b>{html.escape(granted)}</b> is now "
                                f"reachable by agents (and the egress proxy has been refreshed).</p>")
    # Approval side-effect: an approved 'api service proposal' writes the
    # validated spec into the api_services registry as active (the only path
    # that can register a service -- the director loop is blocked from it by
    # the floor-1.0 kind). The note carries the JSON spec; on deny nothing is
    # registered.
    if decision == 'approve' and esc.get('kind') == 'api service proposal':
        _ok, _msg = _approve_api_service_proposal(esc)
        return HTMLResponse(f"<p>Recorded: <b>approved</b> for {esc['kind']}.</p>"
                            f"<p>{html.escape(esc['question'])}</p><p>{html.escape(_msg)}</p>")
    # Side effect (approve OR deny): the player's link decision lands on the
    # requested dormancy-window row. Approve = the slot is scheduled and the
    # firing loop will sleep the tank at that time so armed code self-edits
    # apply. Deny = nothing is scheduled. The supervisor's pre-clearance
    # already passed; this link is the operator decision the kind's floor
    # 1.0 makes mandatory.
    if esc.get('kind') == 'dormancy window request':
        resolved = _selfedit.resolve_window_escalation(id, decision == 'approve')
        if resolved:
            verdict = ('The window is scheduled: the think tank will go '
                       'dormant at the requested time so armed code '
                       'self-edits can apply.'
                       if decision == 'approve' else
                       'Nothing was scheduled.')
            return HTMLResponse(f"<p>Recorded: <b>{esc['status']}</b> for the dormancy window.</p>"
                                f"<p>{html.escape(esc['question'])}</p><p>{verdict}</p>")
    # Side effect (approve OR deny) for Tier B oversight files (audit
    # 2026-10-18): the player's link signature arms the waiting self-edit
    # row, or refuses it. Both quorum review and quarantine validation
    # already passed, so the human signs a known-good artifact.
    if esc.get('kind') == 'oversight self-edit':
        resolved = _selfedit.resolve_oversight_signature(id, decision == 'approve')
        if resolved:
            verdict = ('The edit is armed: it applies the next time the '
                       'tank goes dormant.' if decision == 'approve' else
                       'The edit was refused by your signature.')
            return HTMLResponse(f"<p>Recorded: <b>{esc['status']}</b> for the oversight self-edit.</p>"
                                f"<p>{html.escape(esc['question'])}</p><p>{verdict}</p>")
    return HTMLResponse(f"<p>Recorded: <b>{esc['status']}</b> for {esc['kind']}.</p><p>{html.escape(esc['question'])}</p>")


@app.get('/api/escalation/{esc_id}')
async def get_escalation(esc_id: str):
    # Polled by whatever's waiting on a decision (e.g. the firing review)
    # to see if you've responded yet, without needing a websocket for
    # something this infrequent.
    escalations = _load_escalations()
    esc = escalations.get(esc_id)
    if not esc:
        return JSONResponse({'error': 'not found'}, status_code=404)
    return JSONResponse({'status': esc['status'], 'kind': esc['kind']})


@app.get('/api/activity')
async def activity_feed(limit: int = 50):
    # Straight off action_log -- the same table every real action already
    # writes into, not a second parallel record to keep in sync. Capped
    # (default 50, hard ceiling 200) since this is a UI feed, not an
    # export. Dedup of repeated near-identical rows (e.g. a burst of
    # routine 'decide' calls) happens client-side, where the count is
    # actually useful to show -- collapsing it here would throw away the
    # per-row timestamps a client might still want.
    limit = max(1, min(200, limit))
    with _db() as conn:
        rows = conn.execute(
            'SELECT agent_id, action, details, ts FROM action_log ORDER BY ts DESC LIMIT ?',
            (limit,),
        ).fetchall()
    return JSONResponse({'entries': [
        {'agentId': r[0], 'action': r[1], 'details': json.loads(r[2]) if r[2] else None, 'ts': r[3]}
        for r in rows
    ]})


@app.get('/api/decisions')
async def decision_tape_feed(kind: Optional[str] = None, min_conf: Optional[float] = None, limit: int = 200):
    # Read surface for the decision tape -- every Jev decision the chokepoint
    # recorded, newest first. `kind` filters to one derived label (escalation,
    # personnel, grade, peer_report, groom, runbook, triage, other); `min_conf`
    # filters to decisions AT OR BELOW a confidence floor -- the "unsure but
    # acted on" case that's the point of the tape. Capped like /api/activity
    # (default 200, hard ceiling 1000) since this is a feed, not an export.
    limit = max(1, min(1000, limit))
    sql = 'SELECT ts, kind, model, choice, confidence, cost, ok FROM decision_tape'
    conds: list = []
    args: list = []
    if kind:
        conds.append('kind = ?')
        args.append(kind)
    if min_conf is not None:
        conds.append('confidence IS NULL OR confidence <= ?')
        args.append(float(min_conf))
    if conds:
        sql += ' WHERE ' + ' AND '.join(conds)
    sql += ' ORDER BY ts DESC LIMIT ?'
    args.append(limit)
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return JSONResponse({'entries': [
        {'ts': r[0], 'kind': r[1], 'model': r[2], 'choice': r[3],
         'confidence': r[4], 'cost': r[5], 'ok': bool(r[6])}
        for r in rows
    ]})


@app.get('/api/decisions/verify')
async def decision_tape_verify():
    """Tamper check for the decision tape: walks the HMAC-SHA256 hash chain
    (_verify_decision_tape) and reports every row that fails -- a content edit,
    a chain break, or a row with no hash. Returns {ok, tampered: [{id, issue}],
    count}. The head row's prev_hash anchors to the pruned past and is exempt."""
    ok, problems = _verify_decision_tape()
    return JSONResponse({'ok': ok, 'tampered': problems, 'count': len(problems)})


@app.get('/api/reviews')
async def get_weekly_reviews(request: Request):
    """Read the weekly review ledger: every persisted diff-against-expectation
    report, newest period first. `?generate=1` forces a fresh build of the
    current week (idempotent per week) so the player can review live without
    waiting for the weekly cadence. Returns {ok, reviews, count}."""
    force = (request.query_params.get('generate') or '').strip() in ('1', 'true', 'yes')
    if force:
        await asyncio.to_thread(_generate_weekly_review)
    with _db() as conn:
        rows = conn.execute(
            'SELECT period_start, generated_at, digest, markdown FROM weekly_reviews '
            'ORDER BY period_start DESC',
        ).fetchall()
    return JSONResponse({'ok': True, 'count': len(rows), 'reviews': [
        {'periodStartMs': r[0], 'generatedAt': r[1],
         'digest': json.loads(r[2]), 'markdown': r[3]}
        for r in rows
    ]})


# Jev calibration (CS329A takeaway #1): Jev reports a confidence
# with every decision, and the whole low-confidence-escalation floor
# (JEV_SAFETY_CONFIDENCE) is built on the assumption that high confidence
# means high reliability -- but nothing here ever VERIFIED that. The safety
# gates log confidence+decision, and the fetch outcomes log what actually
# happened, but no code read the two together. This report does: it buckets
# Jev's stated confidence and compares it against the real success rate of
# the actions it allowed, so an overconfident classifier (says 0.9, succeeds
# 60%) is visible instead of quietly eroding the safety floor. DB-only -- no
# network, no LLM -- so it can be read as often as the player wants.
#
# Outcomes: an 'allowed' gate row is "scoreable" when a matching outcome row
# follows it (browse/download/curl/sandbox rows log decision=allowed on
# success, allowed_but_fetch_failed / allowed_but_vpn_failed /
# allowed_but_failed on failure). Blocked and escalated_unsure rows have no
# ground truth (we don't know what Jev "should" have said) so they're counted
# but never scored. Matching prefers the trace_id threaded through the gate
# (exact chain); when it's absent (older rows) it falls back to same
# agent+action within a short window, taking the LAST outcome row as terminal
# (a capability pre-check 'allowed' row precedes the real one).
JEV_CALIBRATION_BINS = (
    (0.0, 0.5, '0.00-0.50'),
    (0.5, 0.6, '0.50-0.60'),
    (0.6, 0.7, '0.60-0.70'),
    (0.7, 0.8, '0.70-0.80'),
    (0.8, 0.9, '0.80-0.90'),
    (0.9, 1.01, '0.90-1.00'),
)
_JEV_SUCCESS_OUTCOMES = {'allowed'}
_JEV_FAILURE_OUTCOMES = {'allowed_but_fetch_failed', 'allowed_but_vpn_failed', 'allowed_but_failed'}
_JEV_OUTCOME_WINDOW_S = 300.0


def _decision_calibration_report(window_s=7 * 86400):
    now = time.time()
    since = now - window_s
    with _db() as conn:
        gate_rows = conn.execute(
            'SELECT agent_id, action, details, ts, trace_id FROM action_log '
            'WHERE ts > ? AND details LIKE \'%"confidence"%\'',
            (since,),
        ).fetchall()
        action_rows = conn.execute(
            'SELECT agent_id, action, details, ts, trace_id FROM action_log WHERE ts > ?',
            (since,),
        ).fetchall()

    # Index every scoreable outcome row: by trace_id (exact chain) and in a
    # flat list for the heuristic fallback.
    by_trace = {}
    outcome_rows = []
    for agent_id, action, details, ts, trace_id in action_rows:
        if details is None:
            continue
        try:
            d = json.loads(details)
        except Exception:
            continue
        if d.get('decision') not in _JEV_SUCCESS_OUTCOMES | _JEV_FAILURE_OUTCOMES:
            continue
        row = (agent_id, action, d, ts, trace_id)
        outcome_rows.append(row)
        if trace_id:
            by_trace.setdefault(trace_id, []).append(row)

    buckets = [{'lo': lo, 'hi': hi, 'label': label,
                'center': (lo + min(hi, 1.0)) / 2.0,
                'n': 0, 'n_allowed': 0, 'n_blocked': 0, 'n_escalated': 0,
                'n_outcome': 0, 'n_success': 0, 'n_failure': 0}
               for lo, hi, label in JEV_CALIBRATION_BINS]

    def _bin(confidence):
        for b in buckets:
            if b['lo'] <= confidence < b['hi']:
                return b
        return buckets[-1]  # >= 1.0 or missing -> top bin

    def _outcome_for(agent_id, action, gate_ts, trace_id):
        if trace_id:
            for oa, oaction, od, ots, otid in by_trace.get(trace_id, []):
                if oa == agent_id and oaction == action and ots > gate_ts:
                    return od
        candidates = [r for r in outcome_rows
                      if r[0] == agent_id and r[1] == action and gate_ts < r[3] <= gate_ts + _JEV_OUTCOME_WINDOW_S]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r[3])[2]

    for agent_id, action, details, ts, trace_id in gate_rows:
        if details is None:
            continue  # pragma: no cover -- gate_rows are pre-filtered on details LIKE '%confidence%', never NULL
        try:
            d = json.loads(details)
        except Exception:
            continue
        confidence = d.get('confidence')
        decision = d.get('decision')
        if not isinstance(confidence, (int, float)) or decision not in ('allowed', 'blocked', 'escalated_unsure'):
            continue
        b = _bin(float(confidence))
        b['n'] += 1
        if decision == 'blocked':
            b['n_blocked'] += 1
            continue
        if decision == 'escalated_unsure':
            b['n_escalated'] += 1
            continue
        b['n_allowed'] += 1
        outcome = _outcome_for(agent_id, action, ts, trace_id)
        if outcome is None:
            continue
        b['n_outcome'] += 1
        if outcome.get('decision') in _JEV_SUCCESS_OUTCOMES:
            b['n_success'] += 1
        else:
            b['n_failure'] += 1

    total_scoreable = sum(b['n_outcome'] for b in buckets)
    total_success = sum(b['n_success'] for b in buckets)
    total_decisions = sum(b['n'] for b in buckets)
    calibration_error = 0.0
    for b in buckets:
        if b['n_outcome']:
            b['success_rate'] = round(b['n_success'] / b['n_outcome'], 4)
            if total_scoreable:
                calibration_error += (b['n_outcome'] / total_scoreable) * abs(b['center'] - b['success_rate'])
        else:
            b['success_rate'] = None
    return {
        'window_s': window_s,
        'generated_at': now,
        'total_decisions': total_decisions,
        'scoreable': total_scoreable,
        'scoreable_fraction': round(total_scoreable / total_decisions, 4) if total_decisions else 0.0,
        'overall_success_rate': round(total_success / total_scoreable, 4) if total_scoreable else None,
        'calibration_error': round(calibration_error, 4),
        'buckets': [{'bin': b['label'], 'n': b['n'], 'n_allowed': b['n_allowed'],
                     'n_blocked': b['n_blocked'], 'n_escalated': b['n_escalated'],
                     'n_outcome': b['n_outcome'], 'n_success': b['n_success'],
                     'n_failure': b['n_failure'], 'success_rate': b['success_rate']}
                    for b in buckets],
    }


# Feedback-loop actuator: the calibration report is the SENSOR.
# This is the actuator that turns "reliability below the stated bar" into a
# threshold the gates actually enforce. The escalation floor assumed high
# confidence == reliable; this pass checks that at the CURRENT threshold and
# moves the bar so auto-approved actions historically succeed ~as claimed.
# Deliberately conservative: DB-only (fit for a timer, no LLM/network), never
# moves on noise (a minimum scored-decision count per bin + a dead-band), moves
# by one fixed step per pass, and clamps to a sane operating range. The safety-
# critical kinds ('blocked command'/'blocked pipeline step' at floor 1.0) are
# untouched by design -- this bar only ever raises/lowers the routine default.
JEV_CALIBRATION_TARGET_RELIABILITY = 0.9   # aim: >=90% of auto-approved actions succeed
JEV_CALIBRATION_HYSTERESIS = 0.05          # dead-band: don't move unless off by > this
JEV_CALIBRATION_MIN_OUTCOME = 10           # need >= this many scored decisions at the bar
JEV_CALIBRATION_STEP = 0.05                # threshold moves in one fixed step per pass
JEV_CALIBRATION_THRESHOLD_MIN = 0.5
JEV_CALIBRATION_THRESHOLD_MAX = 0.95
CALIBRATION_ADJUST_INTERVAL_S = 30 * 60    # own timer: not piggy-backed on the 5-min health loop


def _effective_safety_confidence():
    """The LIVE low-confidence floor, resolved fresh at call time like
    _jev_model: a validated, range-clamped `settings` row (`jev_safety_confidence`,
    written by _calibration_adjust_pass -- or by an operator) overrides the
    process constant JEV_SAFETY_CONFIDENCE. Every gate that used to read the
    constant now reads this, so a calibration move takes effect immediately."""
    raw = _get_setting('jev_safety_confidence')
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return JEV_SAFETY_CONFIDENCE
    if not (JEV_CALIBRATION_THRESHOLD_MIN <= value <= JEV_CALIBRATION_THRESHOLD_MAX):
        return JEV_SAFETY_CONFIDENCE
    return value


def _calibration_adjust_pass(now=None):
    """Close the calibration loop. Reads _decision_calibration_report and moves
    the live threshold (settings `jev_safety_confidence`) so that decisions
    auto-approved at ~the current confidence bar actually succeed at roughly the
    claimed reliability:
      - observed success rate at the current bar BELOW the target - dead-band ->
        raise the bar (require stronger evidence to auto-approve);
      - observed success rate ABOVE the target + dead-band -> lower it toward the
        floor (don't make the think tank more cautious than its own evidence says
        it needs to be).
    Returns the new threshold, or None when no move was warranted (no scored
    decisions at the current bar / inside the dead-band / clamped). Audit row
    logged so the calibration history is visible in the action_log."""
    now = time.time() if now is None else now
    report = _decision_calibration_report()
    threshold = _effective_safety_confidence()
    # Which calibration bin does the LIVE bar actually sit in? The report's
    # public buckets are keyed by LABEL, so resolve the label from the raw bin
    # ranges first, then find the matching reported bucket.
    current_label = None
    for lo, hi, label in JEV_CALIBRATION_BINS:
        if lo <= threshold < hi:
            current_label = label
            break
    current_bin = next((b for b in report['buckets'] if b['bin'] == current_label), None)
    if current_bin is None:
        return None
    rate = current_bin['success_rate']
    if rate is None or current_bin['n_outcome'] < JEV_CALIBRATION_MIN_OUTCOME:
        return None
    deviation = JEV_CALIBRATION_TARGET_RELIABILITY - rate
    if abs(deviation) <= JEV_CALIBRATION_HYSTERESIS:
        return None
    direction = 1 if deviation > 0 else -1  # reliability too low -> raise the bar
    new_threshold = round(min(max(threshold + direction * JEV_CALIBRATION_STEP,
                                  JEV_CALIBRATION_THRESHOLD_MIN),
                              JEV_CALIBRATION_THRESHOLD_MAX), 2)
    if new_threshold == threshold:
        return None
    _set_setting('jev_safety_confidence', str(new_threshold))
    log_action('system', 'jev_calibration_adjust',
               {'from': threshold, 'to': new_threshold, 'bin': current_bin['bin'],
                'success_rate': rate, 'n_outcome': current_bin['n_outcome'],
                'target': JEV_CALIBRATION_TARGET_RELIABILITY,
                'reason': 'raised' if direction > 0 else 'lowered'}, authorized=None)
    return new_threshold


# Review-grade calibration (self-evolve selfdeception.py -- "judge the judge").
# Jev grades review-checklist requirements subjectively (content.py
# _grade_review_checklist); when a requirement is 'code'-type, the quality
# pipeline also produces a MECHANICAL meets/fails verdict that is ground truth
# the subjective grader never sees. Every such anchored grade is recorded in
# review_judge_calibration; the agreement rate between Jev's verdict and the
# pipeline anchor calibrates the review-grade confidence bar exactly the way
# _calibration_adjust_pass calibrates the safety bar -- an overconfident grader
# (says MEETS confidently, pipeline says FAILS) gets a RAISED bar so fewer
# unchecked grades slip through on weak signal.
REVIEW_GRADE_CALIBRATION_TARGET = 0.9    # aim: >=90% agreement with the mechanical anchor
REVIEW_GRADE_CALIBRATION_HYSTERESIS = 0.05  # dead-band: don't move unless off by > this
REVIEW_GRADE_CALIBRATION_MIN_SAMPLES = 10   # need >= this many anchored grades before moving
REVIEW_GRADE_CALIBRATION_STEP = 0.05        # the bar moves in one fixed step per pass

# These calibration knobs are defined after the main serve bind_policy() call,
# so register them now; bind_policy merges into the module's existing pairs.
bind_policy(__name__, [
    ('JEV_CALIBRATION_TARGET_RELIABILITY', 'jevCalibrationTargetReliability'),
    ('JEV_CALIBRATION_HYSTERESIS', 'jevCalibrationHysteresis'),
    ('JEV_CALIBRATION_MIN_OUTCOME', 'jevCalibrationMinOutcome'),
    ('JEV_CALIBRATION_STEP', 'jevCalibrationStep'),
    ('REVIEW_GRADE_CALIBRATION_TARGET', 'reviewGradeCalibrationTarget'),
    ('REVIEW_GRADE_CALIBRATION_HYSTERESIS', 'reviewGradeCalibrationHysteresis'),
    ('REVIEW_GRADE_CALIBRATION_MIN_SAMPLES', 'reviewGradeCalibrationMinSamples'),
    ('REVIEW_GRADE_CALIBRATION_STEP', 'reviewGradeCalibrationStep'),
    ('CALIBRATION_ADJUST_INTERVAL_S', 'calibrationAdjustIntervalS'),
])


def _cohens_kappa_binary(judge_labels, anchor_labels):
    """Cohen's kappa for the two binary raters in review_judge_calibration
    (Jev verdict vs the mechanical pipeline anchor). Raw agreement is
    misleading when most outputs pass -- 60% raw agreement can be chance
    (the evals lesson) -- so kappa corrects for the base rate. The labels are
    the stored 'meets'/'fails' verdict strings; the result is label-swap
    symmetric. None when there is nothing to compare (no samples, or either
    rater never varied -- kappa is undefined and raw agreement is the only
    honest number); 0.0 when chance agreement is total (a degenerate all-one-
    cell table), so the report never divides by zero."""
    n = len(judge_labels)
    if n == 0:
        return None
    j_vals = sorted(set(judge_labels))
    a_vals = sorted(set(anchor_labels))
    if len(j_vals) < 2 or len(a_vals) < 2:
        return None
    pos = j_vals[0]  # arbitrary; binary kappa is label-swap symmetric
    a = b = c = d = 0
    for jv, av in zip(judge_labels, anchor_labels):
        j_pos = jv == pos
        a_pos = av == pos
        if j_pos and a_pos:
            a += 1
        elif j_pos:
            b += 1
        elif a_pos:
            c += 1
        else:
            d += 1
    p_o = (a + d) / n
    p_e = ((a + b) * (a + c) + (b + d) * (c + d)) / (n * n)
    if p_e >= 1.0:
        return 0.0  # pragma: no cover
    return (p_o - p_e) / (1.0 - p_e)


def _insert_review_calibration_sample(section, judge_verdict, judge_confidence, anchor_verdict):
    """Best-effort recorder for one anchored review grade -- a write failure must
    never break the grading path it is called from, so any DB error is swallowed."""
    try:
        agree = 1 if judge_verdict == anchor_verdict else 0
        confidence = float(judge_confidence) if isinstance(judge_confidence, (int, float)) else 0.0
        with _db() as conn:
            conn.execute(
                'INSERT INTO review_judge_calibration (ts, section, judge_verdict, judge_confidence, anchor_verdict, agree) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (time.time(), section, judge_verdict, confidence, anchor_verdict, agree),
            )
    except Exception:
        pass


def _review_grade_calibration_report(window_s=7 * 86400):
    """Aggregate anchored review grades over the window: total samples, total
    agreement, agreement rate, Cohen's kappa (base-rate-corrected agreement --
    raw agreement flatters when most outputs pass), and the per-section
    breakdown -- the sensor the _review_grade_calibration_pass actuator reads.
    DB-only, no network."""
    now = time.time()
    with _db() as conn:
        rows = conn.execute(
            'SELECT section, agree, judge_verdict, anchor_verdict '
            'FROM review_judge_calibration WHERE ts > ?',
            (now - window_s,),
        ).fetchall()
    total = len(rows)
    agreed = sum(r[1] for r in rows)
    kappa = _cohens_kappa_binary([r[2] for r in rows], [r[3] for r in rows])
    by_section = {}
    for section, agree, _jv, _av in rows:
        s = by_section.setdefault(section, {'samples': 0, 'agreed': 0})
        s['samples'] += 1
        s['agreed'] += int(agree)
    return {
        'window_s': window_s,
        'generated_at': now,
        'samples': total,
        'agreed': agreed,
        'agreement_rate': round(agreed / total, 4) if total else None,
        'kappa': round(kappa, 4) if kappa is not None else None,
        'sections': [{'section': k, 'samples': v['samples'], 'agreed': v['agreed'],
                      'agreement_rate': round(v['agreed'] / v['samples'], 4) if v['samples'] else None}
                     for k, v in sorted(by_section.items())],
    }


def _effective_review_grade_confidence():
    """The LIVE review-grade confidence bar, resolved fresh at call time like
    _effective_safety_confidence: a validated, range-clamped `settings` row
    (`jev_review_grade_confidence`, written by _review_grade_calibration_pass --
    or by an operator) overrides the process constant JEV_SAFETY_CONFIDENCE, so
    a calibration move takes effect on the next grade."""
    raw = _get_setting('jev_review_grade_confidence')
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return JEV_SAFETY_CONFIDENCE
    if not (JEV_CALIBRATION_THRESHOLD_MIN <= value <= JEV_CALIBRATION_THRESHOLD_MAX):
        return JEV_SAFETY_CONFIDENCE
    return value


def _review_grade_calibration_pass(now=None):
    """Close the review-grade calibration loop. Reads
    _review_grade_calibration_report and moves the live review-grade confidence
    bar (settings `jev_review_grade_confidence`) so Jev's subjective review
    grades agree with the mechanical pipeline anchor at roughly the claimed
    rate. Deliberately conservative, mirroring _calibration_adjust_pass:
    DB-only, never moves on noise (minimum sample count + dead-band), moves by
    one fixed step per pass, clamps to the operating range. Returns the new
    bar, or None when no move was warranted."""
    now = time.time() if now is None else now
    report = _review_grade_calibration_report()
    if report['samples'] < REVIEW_GRADE_CALIBRATION_MIN_SAMPLES:
        return None
    rate = report['agreement_rate']
    if rate is None:
        return None
    deviation = REVIEW_GRADE_CALIBRATION_TARGET - rate
    if abs(deviation) <= REVIEW_GRADE_CALIBRATION_HYSTERESIS:
        return None
    direction = 1 if deviation > 0 else -1  # agreement too low -> raise the bar
    threshold = _effective_review_grade_confidence()
    new_threshold = round(min(max(threshold + direction * REVIEW_GRADE_CALIBRATION_STEP,
                                  JEV_CALIBRATION_THRESHOLD_MIN),
                              JEV_CALIBRATION_THRESHOLD_MAX), 2)
    if new_threshold == threshold:
        return None
    _set_setting('jev_review_grade_confidence', str(new_threshold))
    log_action('system', 'review_grade_calibration_adjust',
               {'from': threshold, 'to': new_threshold, 'samples': report['samples'],
                'agreement_rate': rate, 'target': REVIEW_GRADE_CALIBRATION_TARGET,
                'reason': 'raised' if direction > 0 else 'lowered'}, authorized=None)
    return new_threshold


# Bot Ops / weekly review cadence: one report per UTC week. The review is the
# diff-against-expectation surface -- it reads GROUND TRUTH (action_log +
# decision_tape) for the window, never an agent's self-reported summary, so
# what the player reviews is what actually happened.
WEEKLY_REVIEW_INTERVAL_S = 7 * 86400
# Actions that count as SHIPPED work vs. process/ceremony, for the same
# ceremony-vs-progress lens the health check already uses (see _CEREMONY_ACTIONS).
_WEEKLY_REVIEW_WINDOW_S = 7 * 86400


def _weekly_period_start(now=None):
    """The UTC start of the current review window, epoch milliseconds -- the
    dedup key: the weekly_reviews table stores one row per period, so the loop
    (and the manual generate endpoint) never write two reports for the same
    week. Matches the kv_state/queue epoch-ms convention."""
    now = time.time() if now is None else now
    start = now - (now % WEEKLY_REVIEW_INTERVAL_S)
    return int(start * 1000)
