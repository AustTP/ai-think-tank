

@app.get('/api/jev/model')
async def jev_model_get():
    # Read-only for anyone logged in: what decisions chain is this think tank
    # actually running on right now? Answers via the same _jev_model() the
    # call sites use, so this always reflects the live value, plus the full
    # failover chain and each slug's breaker state (a multi-slug chain tripping
    # the leader over to a fallback is the whole point of the failover --
    # visible here instead of silently degraded).
    chain = _decision_model_chain()
    if len(chain) <= 1:
        states = {chain[0]: 'sole'}
    else:
        states = {slug: ('broken' if is_model_circuit_broken(slug) else 'available')
                  for slug in chain}
    judge = _jev_judge_model()
    return JSONResponse({'model': _jev_model(), 'primary': chain[0], 'chain': chain,
                         'slugs': states, 'fallback': JEV_MODEL,
                         'judge_model': judge,
                         'judge_drift': _escalation_judge_drift_summary()})


@app.get('/api/jev/calibration')
async def jev_calibration(window_s: Optional[float] = None):
    # Read surface for _decision_calibration_report -- Jev's stated confidence
    # vs its real success rate on safety-gated actions, so the "act when
    # confident" contract is verifiable instead of assumed. `window_s` trims
    # the lookback for a quick check; default is 7 days. DB-only, no model
    # calls, so this is free to hit as often as the player wants. Includes the
    # LIVE feedback-loop threshold (the bar the gates actually enforce now),
    # so the report shows the bar alongside the reliability at that bar.
    report = _decision_calibration_report(window_s) if window_s else _decision_calibration_report()
    review_report = _review_grade_calibration_report(window_s) if window_s else _review_grade_calibration_report()
    return JSONResponse({'effective_safety_confidence': _effective_safety_confidence(),
                         'effective_review_grade_confidence': _effective_review_grade_confidence(),
                         'review_grade': review_report, **report})


@app.get('/api/jev/decision-keys')
async def jev_decision_keys(request: Request):
    # Measurement-only report: how often Jev decision requests repeat, so the
    # operator can decide whether a decision cache is worth building. 'q' is
    # model + questions (question-only), 'r' is model + state + questions
    # (exact request). A high repeat_pct means caching would pay; near-zero
    # means it would not. Read-only; the stats table is never read by any
    # decision path.
    return JSONResponse({
        'window_s': DECISION_KEY_WINDOW_S,
        'question_only': decision_key_report('q'),
        'full_request': decision_key_report('r'),
        'cache': decision_cache_report(),
    })


@app.post('/api/jev/model')
async def jev_model_set(request: Request):
    # Operator-gated (player session or a director/admin agent), same
    # gate as the other registries: switching the think tank's decisions
    # model is an operator action. Writes the `settings` DB row so the
    # switch survives restarts and takes effect immediately -- no process
    # restart, and (unlike model_tiers) it is never auto-updated; a new
    # decision model on OpenRouter is something the operator chooses to
    # adopt, deliberately.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Jev model switching is operator-only'}, status_code=403)
    body = await request.json()
    model = (body.get('model') or '').strip()
    if not model:
        return JSONResponse({'error': 'model is required'}, status_code=400)
    _set_setting('jev_model', model)
    log_action(actor, 'jev_model_set', {'model': model}, authorized=True)
    _append_passport_decision('jev_model_set', actor, {'model': model})
    return JSONResponse({'ok': True, 'model': model})


@app.post('/api/review/escalate')
async def review_escalate(request: Request):
    # Phase G -- surface an unresolvable review/QA requirement to the player
    # the same way every other escalation reaches them (email + resolve
    # link). A review loop that would otherwise keep churning on a
    # requirement it can't confidently judge -- or has exhausted its
    # revision budget on -- escalates instead of guessing or looping
    # forever (the Jev "act when confident, escalate when unsure" contract
    # applied to grading). Attributed + rate-limited like every other
    # agent-initiated call so a tight loop can't spam the human.
    body = await request.json()
    agent_id = body.get('agentId')
    kind = (body.get('kind') or 'unresolved review requirement')[:60]
    question = (body.get('question') or '').strip()
    if not agent_id or not question:
        return JSONResponse({'error': 'agentId and question are required'}, status_code=400)
    allowed, _reason = _decide_allowed(agent_id)
    if not allowed:
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    esc_id = create_escalation(kind,
                               f'Agent {agent_id} could not resolve a review requirement:\n\n{question}',
                               what_checked=f"Agent {agent_id}'s {kind} pass",
                               look_first=question)
    log_action(agent_id, 'review_escalate', {'kind': kind, 'escalationId': esc_id})
    return JSONResponse({'queued': True, 'escalationId': esc_id})


@app.get('/api/rule-proposals')
async def rule_proposals_get(request: Request):
    # Operator surface for the weekly rule-mining pass: the recurring-failure
    # patterns that became PROPOSED rules (rule text + test fixture). Player-only
    # like the other operator surfaces; read-only -- a proposal becomes a real
    # rule when the operator encodes it (ban list, Jev criteria) and pastes the
    # fixture into a conformance test.
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'rule proposals are player-only'}, status_code=403)
    return JSONResponse({'proposals': _rule_proposals()})


@app.get('/api/success-proposals')
async def success_proposals_get(request: Request):
    # Operator surface for the weekly success-mining pass: the rooms that keep
    # shipping high-graded work as PROPOSED success lessons (the positive mirror
    # of the rule-proposals surface). Player-only, read-only -- a lesson becomes
    # a real standard when the operator encodes it.
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'success proposals are player-only'}, status_code=403)
    return JSONResponse({'proposals': _success_proposals()})


_LOGIN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Think Tank -- Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&family=VT323&display=swap" rel="stylesheet">
<style>
  @keyframes blink { 50% { opacity: 0; } }
  body {
    margin: 0;
    background: #05070d;
    background-image:
      radial-gradient(900px 700px at 50% -10%, #0d1526 0%, #05070d 65%),
      repeating-linear-gradient(0deg, rgba(255,255,255,0.015) 0 1px, transparent 1px 28px),
      repeating-linear-gradient(90deg, rgba(255,255,255,0.015) 0 1px, transparent 1px 28px);
    color: #c8d2e2;
    font-family: 'VT323', 'Courier New', monospace;
    display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;
  }
  form {
    background:
      linear-gradient(180deg, rgba(255,255,255,0.06) 0%, rgba(255,255,255,0) 50%),
      linear-gradient(180deg, #283157 0%, #1b2242 55%, #141a33 100%);
    border: 2px solid;
    border-color: #4a5ba0 #0c1126 #0c1126 #4a5ba0;
    box-shadow: 0 0 0 1px rgba(0,0,0,0.5), 0 10px 40px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.08);
    padding: 30px 32px;
    min-width: 300px;
    text-align: center;
  }
  h1 {
    margin: 0 0 6px;
    font-family: 'Press Start 2P', monospace;
    font-size: 15px; font-weight: 400;
    color: #6ff0ff;
    letter-spacing: 0.05em;
    text-shadow: 0 0 8px rgba(111,240,255,0.55);
  }
  h1::after { content: '\\258C'; color: #ffd54a; animation: blink 1.1s steps(1) infinite; margin-left: 4px; }
  .sub {
    margin: 0 0 22px;
    font-family: 'VT323', monospace;
    font-size: 16px; letter-spacing: 0.18em; text-transform: uppercase; color: #8fa2d9;
  }
  label {
    display: block; text-align: left;
    font-family: 'VT323', monospace; font-size: 16px; letter-spacing: 0.12em;
    text-transform: uppercase; color: #7d8ec0; margin-bottom: 2px;
  }
  input {
    width: 100%; box-sizing: border-box;
    padding: 9px 10px; margin-bottom: 16px;
    border: 2px solid; border-radius: 0;
    border-color: #0c1126 #5a6cab #5a6cab #0c1126;
    background: #0a0e1c; color: #7dff9a;
    font-family: 'VT323', monospace; font-size: 18px;
    outline: none;
  }
  input:focus { border-color: #8ff6ff #0a3050 #0a3050 #8ff6ff; box-shadow: 0 0 12px rgba(111,240,255,0.4); }
  button {
    width: 100%; margin-top: 6px;
    font-family: 'Press Start 2P', monospace; font-size: 10px; line-height: 1.2;
    padding: 13px 12px; color: #eaf1ff; text-shadow: 0 1px 0 rgba(0,0,0,0.6);
    background:
      linear-gradient(180deg, rgba(255,255,255,0.24) 0%, rgba(255,255,255,0.04) 42%, rgba(255,255,255,0) 100%),
      linear-gradient(180deg, #3d4c86 0%, #232c52 52%, #141a38 100%);
    border: 2px solid; border-radius: 0;
    border-color: #91a2e6 #0b1026 #0b1026 #7a8dce;
    box-shadow: 0 2px 0 rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.18);
    cursor: pointer;
    transition: color 0.12s ease, border-color 0.12s ease, box-shadow 0.12s ease;
  }
  button:hover { color:#fff; border-color:#8ff6ff #0a3050 #0a3050 #5fd8f0; box-shadow: 0 0 14px rgba(111,240,255,0.6), inset 0 1px 0 rgba(255,255,255,0.28); }
  button:active { transform: translateY(1px); box-shadow: inset 0 2px 4px rgba(0,0,0,0.5); }
  .err { color:#ff7b72; font-family:'VT323',monospace; font-size:16px; margin:0 0 12px; text-align:left; }
  .err::before { content: '\\26A0 '; }
</style></head>
<body>
<form method="post" action="/login">
  <h1>AI THINK TANK</h1>
  <p class="sub">// secure access terminal</p>
  __ERROR_HTML__
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" autofocus>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password">
  <button type="submit">SIGN IN</button>
</form>
</body></html>"""


@app.get('/', response_class=HTMLResponse)
@app.get('/index.html', response_class=HTMLResponse)
async def serve_index(request: Request):
    # Real login gate, once a public deployment became a
    # real possibility -- no valid session, no game page at all, not even
    # a read-only peek. Nothing embedded in this page is a secret anymore
    # (the old SERVER_ACCESS_KEY-in-<script> approach is gone); every API
    # call authenticates via the session cookie the browser already holds.
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return HTMLResponse(_LOGIN_PAGE.replace('__ERROR_HTML__', ''))
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        page = f.read()
    return HTMLResponse(page)


@app.get('/pulse', response_class=HTMLResponse)
async def serve_pulse(request: Request):
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return HTMLResponse(_LOGIN_PAGE.replace('__ERROR_HTML__', ''))
    from agents.pulse_ui import PULSE_HTML
    return HTMLResponse(PULSE_HTML)


@app.get('/api/pulse/overview')
async def api_pulse_overview(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    import agents.pulse as pulse
    return JSONResponse({
        'brief': pulse.brief(None, 24),
        'watches': pulse.list_watches(None),
        'events': pulse.events(agent_id=None, limit=100),
    })


@app.get('/api/project-board')
async def api_project_board(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'player session required'}, status_code=401)
    import content as _content
    q = request.query_params
    try:
        limit = int(q.get('limit') or 40)
    except Exception:
        limit = 40
    payload = _content.project_board({
        'team': q.get('team') or '',
        'project': q.get('project') or '',
        'status': q.get('status') or 'open',
        'limit': limit,
    })
    return JSONResponse(json.loads(payload))


@app.post('/login')
async def login(request: Request):
    ip = request.client.host if request.client else 'unknown'
    if not _check_login_rate_limit(ip):
        return HTMLResponse(_LOGIN_PAGE.replace('__ERROR_HTML__', '<div class="err">Too many attempts -- wait a few minutes.</div>'), status_code=429)

    content_type = request.headers.get('content-type', '')
    if 'application/json' in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    username = (body.get('username') or '').strip()
    password = body.get('password') or ''

    # Constant-time-ish check: always hash with the real salt even on a
    # wrong username, so a wrong-username response doesn't return
    # measurably faster than a wrong-password one.
    _salt, digest = _hash_password(password, ADMIN_PASSWORD_SALT)
    valid = secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(digest, ADMIN_PASSWORD_HASH)
    if not valid:
        log_action(None, 'login_failed', {'ip': ip, 'username': username})
        if 'application/json' in content_type:
            return JSONResponse({'error': 'Invalid username or password'}, status_code=401)
        return HTMLResponse(_LOGIN_PAGE.replace('__ERROR_HTML__', '<div class="err">Invalid username or password.</div>'), status_code=401)

    session_id = create_session()
    log_action(None, 'login_success', {'ip': ip})
    resp: Response
    if 'application/json' in content_type:
        resp = JSONResponse({'ok': True})
    else:
        resp = RedirectResponse(url='/', status_code=303)
    resp.set_cookie(
        SESSION_COOKIE_NAME, session_id,
        max_age=SESSION_LIFETIME_S, httponly=True, samesite='lax',
        secure=(request.url.scheme == 'https'),
    )
    return resp


@app.post('/logout')
async def logout(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        destroy_session(session_id)
    resp = RedirectResponse(url='/', status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


# Static files last -- the explicit routes above are registered first and
# always win; this is the catch-all for *.js/assets/... etc. (index.html
# itself is now served by the templated route above, not this mount).
app.mount('/', StaticFiles(directory=ROOT, html=True), name='static')


# ---------------------------------------------------------------------------
# Content-executor re-exports (phase-consolidation). The per-room executors + the
# router moved to content.py (see there for the "why"); they are re-exported
# here so `serve._run_*` / `serve._server_content_dispatcher` stay the stable
# public surface the tests (and the lifespan wiring above) call. `content` is
# imported lazily from here (bottom of module) rather than the top: content.py
# itself does `import serve as _serve`, so a top-level import of content from
# serve would be the classic import-time cycle. By the time this line runs the
# serve module is fully populated, and content's `_serve.X` lookups resolve at
# call time, never at import time -- no cycle.
# ---------------------------------------------------------------------------
_CONTENT_REEXPORTS = {
    '_server_content_dispatcher', '_run_research_content', '_run_media_content',
    '_run_skill_review_content', '_run_bank_content',
    '_run_research_project_content', '_run_research_bare_content',
    '_run_quality_pipeline', '_run_coding_content', '_run_review_content',
    '_run_product_build_content', '_run_spike_content', '_run_workroom_content',
    '_quality_pipeline_steps', 'CODING_STANDARDS_PROMPT',
}


def __getattr__(name):
    """Lazy content re-exports. content imports serve at module load, so eager
    re-exports here fail when a test imports content first. Resolve on first
    access after that import has completed."""
    if name in _CONTENT_REEXPORTS:
        import content as _content
        value = getattr(_content, name)
        globals()[name] = value
        return value
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')