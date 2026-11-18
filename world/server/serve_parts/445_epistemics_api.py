
# The Epistemic Ledger's HTTP + agent surface (see core/epistemics.py for the
# model). The behavioral contract the surface exists for: the tank must
# distinguish what has been ASSERTED from what has been ESTABLISHED, keep
# contradictory evidence visible until a written resolution lands, keep stale
# records readable but untrusted, and make unknowns actionable as validations
# whose cost goes through the Bank. HTTP is the player/admin console view;
# agents reach the same state through the `epistemics` ask tool, so an agent
# can never bypass the review gate by talking to the ledger directly.
import core.epistemics as epistemics


_EPISTEMICS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'epistemics',
        'description': "The think tank's epistemic ledger: what is "
                       'ESTABLISHED (validated), what is only ASSERTED '
                       '(hypothesis/assumption), what is UNKNOWN, and where '
                       'evidence conflicts. Actions: status (the brief), '
                       'unknown <text>, hypothesize <text>, evidence '
                       '<itemId> <claimId|sourceUrl> (attach evidence; same '
                       'source twice never counts as independent '
                       'corroboration), validate <itemId> <procedure> '
                       '(propose a validation with cost/blocked), result '
                       '<validationId> <resultText> (cite claimId or '
                       'sourceUrl), review <validationId> true|false '
                       '(different agent than the completer), contradict '
                       '<itemId> <claimIdA> <claimIdB>, resolve '
                       '<contradictionId> <resolutionText> '
                       '[winnerClaimId], decide <text> (cite itemIds), '
                       'action <decisionId> <text>, done <decisionId> '
                       '<actionId>, stale <itemId> (refresh after '
                       're-verifying).',
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string',
                           'enum': ['status', 'unknown', 'hypothesize',
                                    'evidence', 'validate', 'result',
                                    'review', 'contradict', 'resolve',
                                    'decide', 'action', 'done', 'stale']},
                'text': {'type': 'string'},
                'itemId': {'type': 'string'},
                'claimId': {'type': 'string'},
                'sourceUrl': {'type': 'string'},
                'claimText': {'type': 'string'},
                'resolvedUrl': {'type': 'string'},
                'procedure': {'type': 'string'},
                'neededEvidence': {'type': 'string'},
                'obtainment': {'type': 'string'},
                'blocked': {'type': 'boolean'},
                'blockedReason': {'type': 'string'},
                'estimatedCostUsd': {'type': 'number'},
                'worthIt': {'type': 'boolean'},
                'validationId': {'type': 'string'},
                'result': {'type': 'string'},
                'approve': {'type': 'boolean'},
                'note': {'type': 'string'},
                'claimIdA': {'type': 'string'},
                'claimIdB': {'type': 'string'},
                'contradictionId': {'type': 'string'},
                'resolutionText': {'type': 'string'},
                'winnerClaimId': {'type': 'string'},
                'decisionId': {'type': 'string'},
                'actionId': {'type': 'string'},
                'rationale': {'type': 'string'},
                'reconsiderWhen': {'type': 'string'},
                'itemIds': {'type': 'array',
                            'items': {'type': 'string'}},
                'actions': {'type': 'array', 'items': {'type': 'string'}},
            },
            'required': ['action'],
        },
    },
}


def _epistemic_budget_ok(state):
    """The Bank verdict for uncertainty reduction: the shared monthly cap
    must have headroom, and if the operator caps the epistemic lane via a
    product record, the lane's month-to-date spend must be within it.
    Failures open the gate: a Bank read error must not dead-end research
    (the per-call JEV/Bank gates at the actual spend sites still apply)."""
    try:
        from integrations import bank as _bank
        if _think_tank_spend_cap_exceeded():
            return False
        ledger = _spend_ledger_read() or {}
        used = _bank._service_month_to_date(ledger, epistemics.BANK_SERVICE)
        products = state.get('products')
        products = (list(products.values()) if isinstance(products, dict)
                    else list(products or []))
        cap = _bank._budget_cap_usd(epistemics.BANK_SERVICE, products)
        if cap <= 0:
            return True
        return used < cap
    except Exception:
        return True


def _brief_json(action, result):
    """One-line JSON for tool replies -- agents parse JSON better than
    prose for ids they must pass back."""
    import json as _json
    return _json.dumps(result, default=str)[:2000]


def _epistemics_tool(agent_id, args):
    args = args or {}
    action = (args.get('action') or '').strip()
    state = get_state_from_db() or {}
    actor = agent_id
    if action == 'status':
        return epistemics.epistemic_brief() or \
            'Epistemic ledger empty: nothing asserted and nothing established yet.'
    if action == 'unknown':
        out = epistemics.record_unknown(args.get('text'), actor=actor)
        return _brief_json(action, out) if out.get('recorded') else \
            f"epistemics: {out.get('error') or out.get('reason')}"
    if action == 'hypothesize':
        out = epistemics.record_hypothesis(args.get('text'), actor=actor)
        return _brief_json(action, out) if out.get('recorded') else \
            f"epistemics: {out.get('error') or out.get('reason')}"
    if action == 'evidence':
        item_id = (args.get('itemId') or '').strip()
        claim_id = (args.get('claimId') or '').strip()
        source_url = (args.get('sourceUrl') or '').strip()
        if claim_id:
            out = epistemics.attach_evidence(item_id, claim_id, actor=actor)
        elif source_url:
            out = epistemics.attach_evidence_from_source(
                item_id, source_url, args.get('claimText'),
                resolved_url=args.get('resolvedUrl'), actor=actor)
        else:
            return 'epistemics: evidence needs claimId or sourceUrl (+claimText)'
        if out.get('attached'):
            if out.get('corroborated'):
                return (f"Attached as a CORROBORATION (same source already "
                        f"attached -- not an independent source). Item "
                        f"{item_id}: {out['independentSources']} independent "
                        f"source(s), status {out['status']}.")
            return (f"Attached. Item {item_id} now has "
                    f"{out['independentSources']} independent source(s), "
                    f"status {out['status']}.")
        return f"epistemics: {out.get('error') or out.get('reason')}"
    if action == 'validate':
        out = epistemics.propose_validation(
            item_id=(args.get('itemId') or '').strip(),
            procedure=args.get('procedure') or args.get('text'),
            needed_evidence=args.get('neededEvidence'),
            obtainment=args.get('obtainment'),
            blocked=bool(args.get('blocked')),
            blocked_reason=args.get('blockedReason'),
            estimated_cost_usd=args.get('estimatedCostUsd'),
            worth_it=args.get('worthIt'),
            actor=actor)
        if not out.get('proposed'):
            return f"epistemics: {out.get('error') or out.get('reason')}"
        line = (f"Validation {out['id']} proposed for {out['itemId']}. "
                'Start it with the epistemics tool action=result only after '
                'running it; a different agent must review.')
        if args.get('blocked'):
            line += f" BLOCKED: {args.get('blockedReason') or 'no authority'}"
        return line
    if action == 'result':
        ok = _epistemic_budget_ok(state)
        if args.get('validationId') and not ok:
            start_out = epistemics.start_validation(
                (args.get('validationId') or '').strip(), cost_ok=False,
                actor=actor)
            return ('epistemics: the Bank says the budget is exhausted -- '
                    f"{start_out.get('reason')}; the validation stays "
                    'blocked rather than running past the cap')
        out = epistemics.complete_validation(
            (args.get('validationId') or '').strip(),
            args.get('result') or args.get('text'),
            claim_id=args.get('claimId') or None,
            source_url=args.get('sourceUrl') or None,
            claim_text=args.get('claimText'),
            resolved_url=args.get('resolvedUrl'),
            actor=actor)
        if not out.get('completed'):
            return f"epistemics: {out.get('error') or out.get('reason')}"
        return (f"Result filed on {out['id']}; awaiting review by a "
                f"different agent (claim {out['resultClaimId']}). The item "
                'does NOT become validated until that review approves.')
    if action == 'review':
        out = epistemics.review_validation(
            (args.get('validationId') or '').strip(),
            bool(args.get('approve')), reviewer=actor,
            note=args.get('note'))
        if not out.get('reviewed'):
            return f"epistemics: {out.get('error') or out.get('reason')}"
        if out.get('itemStatus') == 'validated':
            return ('Approved: the item is now VALIDATED (established, '
                    'with a review window).')
        return (f"Review recorded: {out['status']}. "
                + ('Item stays disputed until its contradiction resolves.'
                   if not out.get('itemStatus') else
                   f"Item status: {out['itemStatus']}."))
    if action == 'contradict':
        out = epistemics.record_contradiction(
            (args.get('itemId') or '').strip(),
            (args.get('claimIdA') or '').strip(),
            (args.get('claimIdB') or '').strip(),
            note=args.get('note'), actor=actor)
        if not out.get('recorded'):
            return f"epistemics: {out.get('error') or out.get('reason')}"
        return (f"Contradiction {out['id']} open on {out['itemId']}: both "
                'sides stay visible and neither is established until '
                'resolved.')
    if action == 'resolve':
        out = epistemics.resolve_contradiction(
            (args.get('contradictionId') or '').strip(),
            args.get('resolutionText'),
            winner_claim_id=args.get('winnerClaimId') or None,
            actor=actor)
        if not out.get('resolved'):
            return f"epistemics: {out.get('error') or out.get('reason')}"
        return (f"Resolved. Item status: {out.get('itemStatus')} "
                '(None means another contradiction is still open).')
    if action == 'decide':
        out = epistemics.record_decision(
            args.get('text'),
            rationale=args.get('rationale'),
            item_ids=args.get('itemIds') or [],
            reconsider_when=args.get('reconsiderWhen'),
            actions=args.get('actions') or [],
            actor=actor)
        if not out.get('recorded'):
            return f"epistemics: {out.get('error') or out.get('reason')}"
        return (f"Decision {out['id']} recorded as a replayable artifact "
                f"with actions {out['actionIds']}.")
    if action == 'action':
        out = epistemics.record_action(
            (args.get('decisionId') or '').strip(), args.get('text'),
            actor=actor)
        return _brief_json(action, out) if out.get('recorded') else \
            f"epistemics: {out.get('error') or out.get('reason')}"
    if action == 'done':
        out = epistemics.complete_action(
            (args.get('decisionId') or '').strip(),
            (args.get('actionId') or '').strip(), actor=actor)
        return _brief_json(action, out) if out.get('completed') else \
            f"epistemics: {out.get('reason')}"
    if action == 'stale':
        out = epistemics.refresh_stale(
            (args.get('itemId') or '').strip(), actor=actor)
        return _brief_json(action, out) if out.get('refreshed') else \
            f"epistemics: {out.get('reason')}"
    return 'epistemics: unknown action'


@app.get('/api/epistemics')
async def epistemics_snapshot_endpoint(request: Request):
    """Operator read: the full snapshot, grouped asserted vs established."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return JSONResponse(epistemics.epistemic_snapshot())


@app.get('/api/epistemics/brief')
async def epistemics_brief_endpoint(request: Request):
    """Operator read: the plain-text brief agents see in their context."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return JSONResponse({'brief': epistemics.epistemic_brief()})


@app.post('/api/epistemics')
async def epistemics_write_endpoint(request: Request):
    """Player-or-admin write: the same action vocabulary the agent tool
    uses. Body: {action, ...params}. Reviewer independence and the Bank
    gate apply identically on both surfaces."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    action = (body.get('action') or '').strip()
    if not action:
        return JSONResponse({'error': 'action is required'}, status_code=400)
    # The ledger owns its own row and _epistemics_tool reads state itself
    # for the Bank's product-cap lookup, so a wedged state read must not
    # 503 a write the ledger itself can complete.
    out = _epistemics_tool(actor or 'player', body)
    log_action(actor or 'player', 'epistemics_write',
               {'action': action}, authorized=True)
    return JSONResponse({'ok': True, 'action': action, 'result': out})
