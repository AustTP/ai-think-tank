
# The steal-list surfaces (2026-11-17): the four Nora items that needed an
# HTTP + ask-tool edge -- epistemic dimension queries, agent-run replay,
# Seer failure investigations, and the issue-engine sweep. All read-mostly;
# the two write paths (Seer anchors, Engine cards/validations) are gated to
# operators/admins and the ask tool's admin/director gate via epistemics'
# own rules. See core/epistemics.query_items, core/agent_replay,
# core/seer.investigate_failure, core/issue_engine.sweep.
import core.agent_replay as agent_replay
import core.issue_engine as issue_engine
import core.seer as seer


@app.get('/api/replay/agent/{agent_id}')
async def agent_replay_endpoint(agent_id: str, request: Request,
                                window_s: float = 86400, limit: int = 200):
    """Operator read: the agent-run replay timeline (typed frames + stats)
    for one agent. Pure composition over action_log; no state mutation."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    with _db() as conn:
        out = agent_replay.replay(conn, agent_id=agent_id,
                                  since_ts=time.time() - float(window_s or 86400),
                                  limit=limit)
    if 'error' in out:
        return JSONResponse(out, status_code=400)
    return JSONResponse(out)


@app.post('/api/epistemics/query')
async def epistemics_query_endpoint(request: Request):
    """Operator read: dimension query over the epistemic ledger (status,
    kind, owner, source count, contradiction flag, creation window).
    Served by the SQL index when available, blob scan otherwise."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    out = epistemics.query_items(
        status=body.get('status'), kind=body.get('kind'),
        owner=body.get('owner'), min_sources=body.get('minSources'),
        has_open_contradiction=body.get('hasOpenContradiction'),
        created_after=body.get('createdAfter'),
        created_before=body.get('createdBefore'),
        order=body.get('order') or 'newest',
        limit=body.get('limit') or 50)
    return JSONResponse(out)


@app.post('/api/seer/investigate')
async def seer_investigate_endpoint(request: Request):
    """Operator write: run a Seer investigation for one agent's real
    failure. Body: {agentId, kind?, taskId?, failureCount?, teamName?}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    agent_id = (body.get('agentId') or '').strip() if isinstance(body, dict) else ''
    if not agent_id:
        return JSONResponse({'error': 'agentId is required'}, status_code=400)
    with _db() as conn:
        out = seer.investigate_failure(
            agent_id, body.get('kind') or 'quality',
            task_id=body.get('taskId'),
            failure_count=int(body.get('failureCount') or 1),
            team_name=body.get('teamName'),
            conn=conn, actor=actor or 'operator')
    return JSONResponse(out)


@app.post('/api/issues/engine-sweep')
async def issue_engine_sweep_endpoint(request: Request):
    """Operator write: cluster the window's failures, root-cause them, and
    (fileCards=true) file one deduped fix card per cluster. Body:
    {windowS?, fileCards?}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    with _db() as conn:
        out = issue_engine.sweep(conn, window_s=body.get('windowS') or 86400,
                                 file_cards=bool(body.get('fileCards')),
                                 actor=actor or 'operator')
    return JSONResponse(out)


# --- ask-tool actions on the epistemics tool -------------------------------

_EPISTEMICS_TOOL['function']['parameters']['properties'].update({
    'action': {'type': 'string',
               'enum': ['status', 'unknown', 'hypothesize', 'evidence',
                        'validate', 'result', 'review', 'contradict',
                        'resolve', 'decide', 'action', 'done', 'stale',
                        'query', 'replay', 'evaluate', 'investigate']},
    'minSources': {'type': 'integer'},
    'hasOpenContradiction': {'type': 'boolean'},
    'order': {'type': 'string'},
    'limit': {'type': 'integer'},
    'windowS': {'type': 'number'},
    'taskId': {'type': 'string'},
    'failureCount': {'type': 'integer'},
})
_EPISTEMICS_TOOL['function']['description'] += (
    ' Also: query (dimension search over the ledger: status/kind/owner/'
    'minSources/hasOpenContradiction), replay <agentId> (typed timeline of '
    'the agent run from the action log), evaluate <itemId> (structural '
    'score + possible promotion toward evidenced -- never validated), '
    'investigate <agentId> (Seer failure root-cause).')

_TOOL_ACTION_HANDLERS = {}


def _epistemics_tool_extended(agent_id, args):
    """The steal-list actions, layered onto the epistemics tool. Self-scope
    rule: an agent may only replay itself; operators/admins replay anyone
    (the HTTP endpoint stays operator-gated for the full surface)."""
    args = args or {}
    action = (args.get('action') or '').strip()
    if action == 'query':
        out = epistemics.query_items(
            status=args.get('status') or None, kind=args.get('kind') or None,
            owner=args.get('owner') or None,
            min_sources=args.get('minSources'),
            has_open_contradiction=args.get('hasOpenContradiction'),
            order=args.get('order') or 'newest',
            limit=min(int(args.get('limit') or 20), 50))
        import json as _json
        items = out.get('items') or []
        lines = [f"{len(items)} item(s) [{out.get('source')}]:"] + \
            [f"- {v['id']} ({v['status']}, {v['independentSources']} src) "
             f"{v['text'][:100]}" for v in items[:12]]
        return '\n'.join(lines) if items else 'No matching ledger items.'
    if action == 'replay':
        target = (args.get('itemId') or '').strip() or agent_id
        if target != agent_id:
            return ('replay is self-scoped for agents; operators use '
                    'GET /api/replay/agent/<id>')
        with _db() as conn:
            out = agent_replay.replay(
                conn, agent_id=agent_id,
                since_ts=time.time() - float(args.get('windowS') or 86400),
                limit=args.get('limit') or 120)
        return agent_replay.render_text(out)
    if action == 'evaluate':
        item_id = (args.get('itemId') or '').strip()
        out = epistemics.evaluate_item(item_id, apply=True, actor=agent_id)
        if not out.get('evaluated'):
            return f"epistemics: {out.get('reason')}"
        line = (f"Score {out['score']}: {', '.join(out['reasons']) or 'no signals'} "
                f"-> {out['recommendation']}")
        if out.get('applied'):
            line += ' (applied: item is now evidenced; validated still ' \
                    'requires the review gate)'
        return line
    if action == 'investigate':
        target = (args.get('itemId') or '').strip()
        if target != agent_id:
            return ('investigate is self-scoped for agents; operators use '
                    'POST /api/seer/investigate')
        with _db() as conn:
            out = seer.investigate_failure(
                agent_id, args.get('kind') or 'quality',
                task_id=args.get('taskId'),
                failure_count=int(args.get('failureCount') or 1),
                conn=conn, actor=agent_id)
        if not out.get('investigated'):
            return f"seer: {out.get('reason')}"
        return (f"Investigation recorded ({out['symptom']}). Hypothesis: "
                f"{out['hypothesis']}. Question: "
                f"{(out.get('question') or {}).get('id')}. Precursors: "
                + '; '.join(p['action'] for p in (out.get('precursors') or [])[:5])
                or 'none')
    return _epistemics_tool_orig(agent_id, args)


_epistemics_tool_orig = _epistemics_tool


def _epistemics_tool(agent_id, args):
    """Dispatch wrapper: steal-list actions first, then the original
    epistemics actions. The ask-loop calls this name (it binds the latest
    definition in the shared part namespace)."""
    return _epistemics_tool_extended(agent_id, args)


# --- harness optimizer (the Self-Improving Memory lesson) ------------------
# Propose -> apply -> measure, with the epistemic ledger carrying the
# discipline: revisions are hypotheses with sweep evidence, adoption is a
# decision artifact, and keep-vs-supersede goes through the review gate.
# Operator-gated on both endpoints; no ask-tool surface for v1 (an agent
# rewriting its own harness unmeasured is exactly the failure mode this
# guards against).

import core.harness_optimizer as harness_optimizer


def _ensure_harness_category(state, actor):
    wiki = state.setdefault('wiki', {})
    cats = wiki.setdefault('categories', {})
    if 'harness' not in cats:
        cats['harness'] = {'label': 'Harness doctrine (optimizer-managed)',
                           'order': 85}
    return cats


def _apply_registry_page(state, proposal, actor):
    """Write the registry page: director/admin-equivalent write done by the
    operator's authority, through the same sim seam the wiki route uses.
    Village-scoped to the acting operator, think-tank-wide affinity (the
    'harness' category has no room mapping), 30-day review window so stale
    doctrine loses trusted-current status like every other wiki page."""
    import sim as _sim
    page_id = proposal['pageId']
    cats = _ensure_harness_category(state, actor)
    if 'harness' not in cats:
        return None
    village_id = _sim.village_of_agent(state, actor)
    record, is_new = _sim.wiki_write_page(
        state, page_id, proposal['title'], 'harness', proposal['body'],
        edited_by=actor, village_id=village_id, owner=actor,
        clock='slow', scope='team',
        review_after_ms=int(time.time() * 1000) + 30 * 86400 * 1000,
        source=f"issue-engine:{proposal['symptomKey']}")
    if record is None:
        return None
    _sim.record_memory_provenance(
        state, actor, page_id, record.get('title'), proposal['body'][:2000],
        source=f"issue-engine:{proposal['symptomKey']}", scope='team',
        clock='slow',
        review_after_ms=int(time.time() * 1000) + 30 * 86400 * 1000,
        village_id=village_id)
    target = _wiki_page_target('harness', page_id)
    if target:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _write_file(target, proposal['body'])
    return record


@app.post('/api/harness/optimize')
async def harness_optimize_endpoint(request: Request):
    """Operator write: diagnose the window's failure clusters and propose
    one indexed-registry harness revision. Applying writes the wiki page
    and records the provisional-adoption decision. Body: {apply?, windowS?}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    proposal = harness_optimizer.propose(
        window_s=body.get('windowS') or 86400, actor=actor or 'operator')
    if not proposal.get('proposed'):
        return JSONResponse(proposal)
    if not body.get('apply', True):
        proposal['applied'] = False
        return JSONResponse(proposal)
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    page = _apply_registry_page(state, proposal, actor or 'operator')
    save_state_to_db(state)
    if page is None:
        return JSONResponse({'error': 'could not write registry page'},
                            status_code=500)
    out = harness_optimizer.mark_applied(proposal, page,
                                         actor=actor or 'operator')
    log_action(actor, 'harness_revision_applied',
               {'pageId': proposal['pageId'],
                'symptomKey': proposal['symptomKey'],
                'itemId': proposal['itemId'],
                'version': out.get('pageVersion')}, authorized=True)
    proposal['applied'] = True
    proposal['adoption'] = out
    return JSONResponse(proposal)


@app.post('/api/harness/measure')
async def harness_measure_endpoint(request: Request):
    """Operator write: re-run the sweep and complete each open revision's
    measure validation with the delta. Keep-vs-supersede goes through the
    review gate. Body: {windowS?}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    out = harness_optimizer.measure(window_s=body.get('windowS'),
                                    actor=actor or 'operator')
    return JSONResponse(out)
