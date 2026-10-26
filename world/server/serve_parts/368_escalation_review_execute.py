"""Review-only execution for a pending escalation.

Directors and the chief of staff may run the candidate command from a pending
escalation in the isolated sandbox to see what it actually does. This is the
only code execution they are offered outside a normal sandbox card, and the
command must come from that escalation; it is not a general code tool.
"""


def _norm_command(text):
    return ' '.join((text or '').split())


@app.post('/api/escalation/review-execute')
async def escalation_review_execute(request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'escalation review is operator-only'}, status_code=403)
    if not EXECUTION_ENABLED:
        return JSONResponse({'error': 'Agent execution is disabled (AGENT_EXECUTION_ENABLED=false in .env)'},
                            status_code=403)
    if not check_rate_limit(f'escalation-review:{actor}'):
        return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    esc_id = str(body.get('escalationId') or '').strip()
    if not esc_id:
        return JSONResponse({'error': 'escalationId is required'}, status_code=400)
    esc = (_load_escalations() or {}).get(esc_id)
    if not esc:
        return JSONResponse({'error': f'unknown escalation: {esc_id}'}, status_code=404)
    if esc.get('status') != 'pending':
        return JSONResponse({'error': f'escalation is already {esc.get("status")}'}, status_code=400)
    review_text = esc.get('reviewQuestion') or esc.get('question') or ''
    command = str(body.get('command') or '').strip()
    if command:
        if _norm_command(command) not in _norm_command(review_text) and \
                _norm_command(command) not in _norm_command(esc.get('question') or ''):
            return JSONResponse({'error': 'that command is not part of this escalation'},
                                status_code=403)
    else:
        command = review_text
    if not command:
        return JSONResponse({'error': 'this escalation has no command to review'},
                            status_code=400)
    sandbox_id = f'review-{esc_id}'
    if not _SANDBOX_ID_RE.match(sandbox_id):
        return JSONResponse({'error': 'invalid escalation id'}, status_code=400)
    try:
        sandbox_dir = _sandbox_dir_for(sandbox_id)
        result = await asyncio.to_thread(_run_in_sandbox_sync, sandbox_dir, command)
    except Exception as exc:
        return JSONResponse({'error': f'could not run the review command: {exc}'},
                            status_code=500)
    log_action(actor, 'escalation_review_execute', {
        'escalationId': esc_id, 'exitCode': result.get('exitCode'),
        'timedOut': result.get('timedOut'), 'purpose': (body.get('purpose') or '')[:200],
        'command': command[:1000]}, authorized=True)
    return JSONResponse({'ok': True, 'escalationId': esc_id, **result})
