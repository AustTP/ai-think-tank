# ---------------------------------------------------------------------------
# Investigations HTTP surface (foundry/coherence work, 2026-11-18): the
# case-level composite of the coherence review, Priority 1. The epistemic
# ledger stays the truth store; an investigation is the thin INDEX binding
# question -> ledger items -> tasks -> wiki pages -> owner -> deadline ->
# conclusion -> disposition, with the validation state derived live at read
# time (never stored). Operator/admin gated on every verb; agents reach the
# same object through the ledger (cite itemIds in the case) rather than a
# second write path.
# ---------------------------------------------------------------------------
import core.investigations as investigations


@app.get('/api/investigations')
async def investigations_list(request: Request, disposition: str = '',
                              owner: str = ''):
    """Operator read: every case, open first. `disposition`/`owner` filter."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    cases = investigations.list_investigations(
        disposition=(disposition or '').strip() or None,
        owner=(owner or '').strip() or None)
    return JSONResponse({'ok': True, 'investigations': cases,
                         'count': len(cases)})


@app.get('/api/investigations/{inv_id}')
async def investigations_get(inv_id: str, request: Request):
    """Operator read: one case with its live validation view (per-item
    ledger statuses, staleness, overdue/reassessment flags)."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    case = investigations.get_investigation(inv_id)
    if case is None:
        return JSONResponse({'error': f'unknown investigation: {inv_id}'},
                            status_code=404)
    return JSONResponse({'ok': True, 'investigation': case})


@app.post('/api/investigations')
@_state_writer
async def investigations_create(request: Request):
    """Operator write: open a case. Body: {question, owner?, ledgerItemIds?,
    taskIds?, wikiPageIds?, deadlineMs?, reassessmentMs?}. References are
    ids into their own truth stores (epistemic items, the task mirror, wiki
    pages); unknown ids are carried as-is and reported 'missing' in the
    validation view, so a case can be opened before its knowledge lands."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'},
                            status_code=400)
    out = investigations.create_investigation(
        body.get('question'), owner=body.get('owner'), actor=actor,
        ledger_item_ids=body.get('ledgerItemIds') or (),
        task_ids=body.get('taskIds') or (),
        wiki_page_ids=body.get('wikiPageIds') or (),
        deadline_ms=body.get('deadlineMs'),
        reassessment_ms=body.get('reassessmentMs'))
    if not out.get('created'):
        return JSONResponse({'error': out.get('error')
                             or out.get('reason')}, status_code=400)
    log_action(actor or 'player', 'investigation_created',
               {'id': out['id'], 'question': (body.get('question')
                                              or '')[:200]},
               authorized=True)
    return JSONResponse(out)


@app.patch('/api/investigations/{inv_id}')
@_state_writer
async def investigations_update(inv_id: str, request: Request):
    """Operator write: reassign owner, move deadline/reassessment, attach
    or detach references. Body: any of {owner, deadlineMs, reassessmentMs,
    addLedgerItemIds, addTaskIds, addWikiPageIds, dropRefIds}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'},
                            status_code=400)
    out = investigations.update_investigation(
        inv_id, actor=actor, owner=body.get('owner'),
        deadline_ms=body.get('deadlineMs'),
        reassessment_ms=body.get('reassessmentMs'),
        add_ledger_item_ids=body.get('addLedgerItemIds') or (),
        add_task_ids=body.get('addTaskIds') or (),
        add_wiki_page_ids=body.get('addWikiPageIds') or (),
        drop_ref_ids=body.get('dropRefIds') or ())
    if not out.get('updated'):
        status = 404 if out.get('reason') == 'unknown_investigation' else 400
        return JSONResponse({'error': out.get('error')
                             or out.get('reason')}, status_code=status)
    log_action(actor or 'player', 'investigation_updated',
               {'id': inv_id, 'changed': out.get('changed')},
               authorized=True)
    return JSONResponse(out)


@app.post('/api/investigations/{inv_id}/conclude')
@_state_writer
async def investigations_conclude(inv_id: str, request: Request):
    """Operator write: close the case. Body: {conclusion, confidence?,
    disposition? (concluded|superseded|abandoned), finalText?}. Discipline
    lives in core/investigations.py: a conclusion requires text, an explicit
    confidence, and at least one cited ledger item; an open contradiction
    in the cited items does NOT block concluding (the conclusion may be
    'evidence conflicts; parked') but the validation view will keep saying
    contradicted until the ledger resolves it."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'},
                            status_code=400)
    out = investigations.conclude_investigation(
        inv_id, body.get('conclusion'), confidence=body.get('confidence'),
        disposition=(body.get('disposition') or 'concluded').strip()
        or 'concluded',
        final_text=body.get('finalText'), actor=actor)
    if not out.get('concluded'):
        status = 404 if out.get('reason') == 'unknown_investigation' else 400
        return JSONResponse({'error': out.get('error')
                             or out.get('reason')}, status_code=status)
    log_action(actor or 'player', 'investigation_concluded',
               {'id': inv_id, 'disposition': out.get('disposition')},
               authorized=True)
    return JSONResponse(out)
