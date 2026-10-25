

@app.get('/api/research-desk/ops')
async def research_desk_ops(request: Request):
    # The article's weekly operating report, consolidated from data the desk
    # already writes (evidence.py ledgers + the spend ledger): how many claims
    # were recorded, how many before/after observations logged, how many claims
    # are blocked vs pending review, how many identities are covered, how many
    # sources the watchlist holds, and what the whole operation cost over the
    # last seven days. Read-only, so the report can be scheduled and pasted
    # into a run summary without a second mutation channel.
    claims = _evidence_read() or {}
    coverage = _coverage_read() or {}
    now = time.time()
    window_s = 7 * 86400
    claims_since = [c for c in claims.values()
                    if c.get('created_ts', 0) > now - window_s]
    observed = [c for c in claims.values() if c.get('observations')]
    blocked = [c for c in claims.values() if c.get('status') == 'blocked']
    needs_review = [c for c in claims.values() if c.get('status') == 'needs_review']
    covered = [v for v in coverage.values() if v.get('status') == 'covered']
    ledger = _spend_ledger_read() or {}
    reserved = {_SPEND_CAP_BASELINE_KEY}
    spend_7d = 0.0
    for key, bucket in ledger.items():
        if key in reserved or key.startswith('__'):
            continue
        by_day = bucket.get('byDay') or {}
        spend_7d += sum(v for d, v in by_day.items() if d >= time.strftime('%Y-%m-%d', time.localtime(now - window_s)))
    import config as _config
    return JSONResponse({
        'windowDays': 7,
        'claims': {
            'total': len(claims),
            'recorded_in_window': len(claims_since),
            'needs_review': len(needs_review),
            'blocked': len(blocked),
            'with_observations': len(observed),
        },
        'coverage': {
            'identities_total': len(coverage),
            'covered': len(covered),
        },
        'watchlist': {
            'topics': len(_config.RESEARCH_WATCHLIST),
            'passes': len(_config.RESEARCH_DESK_PASSES),
        },
        'spendUsdLast7d': round(spend_7d, 6),
        'at': now,
    })


@app.get('/api/evidence')
async def evidence_list(request: Request):
    # Read view of the claim ledger for the operator/Verifier. Filters:
    # ?status=needs_review|cleared|blocked, ?limit=N (tail).
    status = request.query_params.get('status')
    if status is not None and status not in CLAIM_STATUSES:
        return JSONResponse({'error': f'status must be one of {sorted(CLAIM_STATUSES)}'}, status_code=400)
    try:
        limit = int(request.query_params.get('limit') or 200)
    except (TypeError, ValueError):
        limit = 200
    return JSONResponse({'claims': list_claims(status=status, limit=limit)})


@app.get('/api/evidence/audit')
async def evidence_audit_view(request: Request):
    # The immutable audit trail of every evidence/coverage mutation (who, what,
    # when) -- the detection layer for a compromised key. Read-only; the rows
    # are append-only by construction.
    try:
        limit = int(request.query_params.get('limit') or 500)
    except (TypeError, ValueError):
        limit = 500
    return JSONResponse({'audit': list_audit(limit=limit)})


def _agent_is_verifier(agent_id):
    """Verifier authority: the player, or a roster agent with isDirector or
    isAdmin. A valid agent key proves IDENTITY, not authority -- the role gate
    is separate, so a compromised ordinary agent cannot flip coverage."""
    if agent_id == 'player':
        return True
    try:
        state = get_state_from_db() or {}
        roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
        entry = roster.get(agent_id) or {}
        return bool(entry.get('isDirector') or entry.get('isAdmin'))
    except Exception:
        return False


@app.post('/api/evidence/review')
async def evidence_review(request: Request):
    # The Verifier step (article section 6): a human or reviewing agent opens
    # the source, separates the source's claim from our inference, and returns
    # a per-record decision. decision in usable_as_written /
    # usable_with_narrower_wording / blocked_until_checked. A usable decision
    # clears the claim and (when it is an independent claim) folds its
    # identity into the coverage ledger as COVERED -- the ONLY state that
    # suppresses a future re-report. A blocked decision blocks the claim and
    # never marks coverage.
    #
    # Authority is role-gated, not key-gated: the player (session) or a
    # director/admin agent may act as Verifier; an ordinary agent cannot, even
    # with a valid key. Agent verifiers are additionally rate-capped on
    # covered flips so a compromised director cannot silently cover the whole
    # ledger in one pass.
    is_player = _require_player_session(request)
    body = await request.json()
    agent_id = 'player' if is_player else (body.get('agentId') or 'unknown')
    if not is_player:
        if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not _agent_is_verifier(agent_id):
            return JSONResponse({'error': 'Forbidden -- only directors/admin (or the player) can act as Verifier'}, status_code=403)
    claim_id = (body.get('claimId') or '').strip()
    decision = (body.get('decision') or '').strip()
    if not claim_id or decision not in CLAIM_DECISIONS:
        return JSONResponse({'error': f'claimId and a decision in {sorted(CLAIM_DECISIONS)} are required'}, status_code=400)
    # Covered-flip rate cap for agent verifiers: a usable decision on an
    # INDEPENDENT claim folds the identity into the coverage ledger as covered
    # (suppressing re-report). Cap the number of such flips per actor per
    # window; the player is exempt (operator discretion, fully visible).
    if not is_player and decision in ('usable_as_written', 'usable_with_narrower_wording'):
        prior = get_claim(claim_id)
        if prior and prior.get('independent'):
            used = count_actor_action(agent_id, 'coverage_covered',
                                      time.time() - VERIFIER_COVERED_WINDOW_S)
            if used >= VERIFIER_COVERED_MAX:
                return JSONResponse({'error': f'Covered-flip cap reached: max {VERIFIER_COVERED_MAX} per {VERIFIER_COVERED_WINDOW_S}s for a single verifier. The player must review further claims.'}, status_code=429)
    try:
        rec = mark_reviewed(claim_id, decision, artifact=body.get('artifact'), actor=agent_id)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    if rec is None:
        return JSONResponse({'error': 'unknown claim id'}, status_code=404)
    log_action(agent_id, 'evidence_review', {'claim_id': claim_id, 'decision': decision})
    return JSONResponse({'ok': True, 'claim': rec})


@app.post('/api/evidence/resolve')
async def evidence_resolve(request: Request):
    # Resolve a URL to its final landing URL, SSRF-safe, WITHOUT fetching its
    # content. This is how the desk verifies a short link before trusting it:
    # t.co/abc and bit.ly/abc may look the same but land on different sites,
    # and a short token can be repointed to a malicious destination. Returns
    # the resolved URL, hop count, and the source identity derived from it.
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    url = (body.get('url') or '').strip()
    if not url:
        return JSONResponse({'error': 'url is required'}, status_code=400)
    if not _is_safe_public_host((urllib.parse.urlparse(url).hostname or '').lower()):
        return JSONResponse({'error': 'that host is not a public, reachable address'}, status_code=400)
    try:
        final_url, hops = _resolve_final_url(url)
    except Exception as e:
        return JSONResponse({'error': f'could not resolve: {e}'}, status_code=500)
    identity, meta = source_identity(url, final_url if final_url != url else None)
    if final_url != url:
        log_action(agent_id, 'evidence_resolve', {'url': url, 'finalUrl': final_url,
                                                  'hops': hops, 'is_short_link': meta['is_short_link']})
    return JSONResponse({'url': url, 'finalUrl': final_url, 'hops': hops,
                         'resolved': final_url != url,
                         'identity': identity, 'meta': meta})


@app.post('/api/coverage')
async def coverage_write(request: Request):
    # Write a coverage entry: {identity, reviewed, artifact, event_id, ...}.
    # reviewed=False (the crawl's default after a synthesis lands) yields
    # 'pending_review' -- visible but NOT covered. reviewed=True marks 'covered'
    # and is PLAYER-SESSION-ONLY: it is the "this brief was reviewed" move, and
    # only the operator may mark an item covered directly. The research crawl
    # (agent key, reviewed=False) records pending_review/failed only, so a
    # compromised agent key cannot suppress re-reporting through this route.
    # {identity, failure: reason} records a failed run and never suppresses the
    # next attempt.
    body = await request.json()
    is_player = _require_player_session(request)
    if body.get('reviewed'):
        if not is_player:
            return JSONResponse({'error': 'Forbidden -- marking covered requires the player session; agents record pending_review only'}, status_code=403)
        agent_id = 'player'
    else:
        agent_id = (body.get('agentId') or 'unknown')
        if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    identity = (body.get('identity') or '').strip()
    if not identity:
        return JSONResponse({'error': 'identity is required'}, status_code=400)
    if body.get('failure'):
        rec = record_run_failure(identity, body['failure'], artifact=body.get('artifact'),
                                 event_id=body.get('eventId'),
                                 source_url=body.get('sourceUrl'),
                                 resolved_url=body.get('resolvedUrl'),
                                 actor=agent_id)
        log_action(agent_id, 'coverage_failure', {'identity': identity, 'reason': body['failure']})
        return JSONResponse({'ok': True, 'coverage': rec})
    rec = mark_covered(identity, artifact=body.get('artifact'),
                       reviewed=bool(body.get('reviewed')),
                       event_id=body.get('eventId'),
                       source_url=body.get('sourceUrl'),
                       resolved_url=body.get('resolvedUrl'),
                       material_update_ms=body.get('materialUpdateMs'),
                       run_id=body.get('runId'),
                       actor=agent_id)
    log_action(agent_id, 'coverage_write', {'identity': identity, 'reviewed': bool(body.get('reviewed'))})
    return JSONResponse({'ok': True, 'coverage': rec})


@app.get('/api/coverage')
async def coverage_list(request: Request):
    coverage = _coverage_read() or {}
    status = request.query_params.get('status')
    if status is not None and status not in COVERAGE_STATUSES:
        return JSONResponse({'error': f'status must be one of {sorted(COVERAGE_STATUSES)}'}, status_code=400)
    rows = list(coverage.values())
    if status is not None:
        rows = [r for r in rows if r.get('status') == status]
    rows = sorted(rows, key=lambda r: r.get('updated_ts', 0))
    return JSONResponse({'coverage': rows})


@app.get('/api/policy')
async def policy_view(request: Request):
    # The operator-edited policy (world/config.py) as loaded right now: the
    # ban list, browse block categories, desk passes, and research watchlist.
    # This is the read surface for the operator UI and for villagers who want
    # to see the standing desk rules.
    import config as _config
    return JSONResponse({
        'plain_writing': {'banned': list(_config.PLAIN_WRITING_BAN_LIST)},
        'browse_policy': {'block_categories': list(_config.BROWSE_BLOCK_CATEGORIES)},
        'research_desk': {'passes': [dict(p) for p in _config.RESEARCH_DESK_PASSES]},
        'watchlist': {'topics': [dict(t) for t in _config.RESEARCH_WATCHLIST]},
        'writeback_enabled': bool(_config.ALLOW_WATCHLIST_WRITE),
        'operator': dict(_config.get_operator_policy()),
    })


@app.post('/api/policy/reload')
async def policy_reload(request: Request):
    # Re-read the four world/*.json policy files so an operator's file edits
    # apply without a server restart. Fails closed: a bad edit keeps the
    # previous loaded values, and the response reports what each file loaded.
    # OPERATOR-ONLY (player session or the chief of staff agent): reloading is an
    # operator action; an ordinary agent key never suffices, so a compromised
    # key cannot fiddle with the reload cadence.
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- reloading policy requires the player session or the admin'}, status_code=401)
    result = reload_policy_config()
    log_action(None, 'policy_reload', {
        k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in result.items()
    })
    return JSONResponse({'ok': True, 'loaded': result})


# ---------------------------------------------------------------------------
# Live budget knobs (player-session only). The monthly ceilings (SPEND_CAP_USD,
# HIGH_TIER_MONTHLY_BUDGET_USD, APIFY_MONTHLY_BUDGET_USD, PAGE_REQUEST_MONTHLY_BUDGET,
# COLAB_MONTHLY_UNITS) and the per-model price ceilings (HIGH/CODING/VISION_TIER_MAX_PRICE_USD)
# are .env values that used to require a hand-edit + restart or /api/env/reload.
# The PLAYER -- the operator -- can now set them live via POST /api/budget,
# which rewrites the named keys in .env and rebinds the globals through the
# existing _reload_env_config path. Agents cannot: the endpoint is
# _require_player_session gated, same as /api/env/reload and /api/policy/reload.
# A cap the AGENTS could silently raise isn't a real ceiling; the player raising
# their OWN budget is exactly the intended control surface.
# ---------------------------------------------------------------------------

# name -> (loader for validation, kind) where kind is 'usd' (float >= 0) or
# 'count' (int >= 0). Only these names are settable -- never API keys.
_BUDGET_KNOBS = {
    'SPEND_CAP_USD': ('usd', 'Monthly total model/API spend cap (0 = unlimited).'),
    'AGENT_SPEND_CAP_USD': ('usd', 'Per-agent monthly spend ceiling (0 = disabled).'),
    'OPENROUTER_MONTHLY_BUDGET_USD': ('usd', 'Expected total OpenRouter funding for the month (planning context for the gauge, not an alert trigger).'),
    'HIGH_TIER_MONTHLY_BUDGET_USD': ('usd', 'High-tier monthly spend budget (0 = unlimited).'),
    'APIFY_MONTHLY_BUDGET_USD': ('usd', 'Apify monthly spend budget (0 = disables the budget row).'),
    'COLAB_MONTHLY_UNITS': ('usd', 'Colab monthly compute-unit budget (0 = unlimited).'),
    'PAGE_REQUEST_MONTHLY_BUDGET': ('count', 'Monthly external page-request budget (0 = unlimited).'),
    'HIGH_TIER_MAX_PRICE_USD': ('usd', 'Highest per-million-token price the high tier will pick (0 = no ceiling).'),
    'CODING_TIER_MAX_PRICE_USD': ('usd', 'Highest per-million-token price the coding tier will pick (0 = no ceiling).'),
    'VISION_TIER_MAX_PRICE_USD': ('usd', 'Highest per-million-token price the vision tier will pick (0 = no ceiling).'),
    'SUBLOW_TIER_MAX_PRICE_USD': ('usd', 'Sublow tier per-million-token price ceiling (the band definition).'),
}


def _env_file_path():
    return os.path.join(THINK_TANK_DIR, '.env')


def _set_env_keys(updates):
    """Rewrite named keys in .env in place, preserving comments and every other
    line. Appends a key that isn't present yet. Returns the list of names
    actually written. Raises on IO failure (caller reports; never half-writes
    silently)."""
    path = _env_file_path()
    lines = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        key = None
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
        if key and key in updates:
            out.append(f'{key}={updates[key]}\n')
            seen.add(key)
        else:
            out.append(line)
    for key in updates:
        if key not in seen:
            if out and not out[-1].endswith('\n'):
                out[-1] += '\n'
            out.append(f'{key}={updates[key]}\n')
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(out)
    return list(updates.keys())


def _budget_usage_snapshot():
    """Live usage for every budget knob, read from the ledgers (best-effort;
    a ledger failure never blocks the view -- the missing row just omits
    usage). Pure read."""
    out = {}
    try:
        m = _spend_cap_month_view()
        out['SPEND_CAP_USD'] = {'used': m.get('usage'), 'cap': m.get('cap'),
                                'exceeded': m.get('exceeded')}
    except Exception:
        pass
    try:
        used = _high_tier_spend_this_month()
        cap = HIGH_TIER_MONTHLY_BUDGET_USD
        out['HIGH_TIER_MONTHLY_BUDGET_USD'] = {
            'used': round(used, 6), 'cap': cap,
            'exceeded': bool(cap) and used >= cap}
    except Exception:
        pass
    try:
        used = _apify_spend_this_month()
        cap = APIFY_MONTHLY_BUDGET_USD
        out['APIFY_MONTHLY_BUDGET_USD'] = {
            'used': round(used, 6), 'cap': cap,
            'exceeded': bool(cap) and used >= cap}
    except Exception:
        pass
    try:
        used = _page_budget_used()
        cap = PAGE_REQUEST_MONTHLY_BUDGET
        out['PAGE_REQUEST_MONTHLY_BUDGET'] = {
            'used': used, 'cap': cap,
            'exceeded': bool(cap) and used >= cap}
    except Exception:
        pass
    try:
        used = _colab_spend_this_month()
        cap = COLAB_MONTHLY_UNITS
        out['COLAB_MONTHLY_UNITS'] = {
            'used': round(used, 6), 'cap': cap,
            'exceeded': bool(cap) and used >= cap}
    except Exception:
        pass
    return out


@app.get('/api/budget')
async def budget_view(request: Request):
    """Budget readout: every settable knob's current value plus live
    month-to-date usage where a ledger exists. Operator-gated (a player
    session or a director/admin agent) -- the Banking role and the senior
    director are meant to manage the treasury, so they can SEE the knobs.
    Setting them is a separate, player-only write (budget_set below)."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- budget is an operator control'}, status_code=401)
    usage = _budget_usage_snapshot()
    knobs = {}
    g = globals()
    for name, (kind, desc) in _BUDGET_KNOBS.items():
        knobs[name] = {
            'value': g.get(name),
            'kind': kind,
            'description': desc,
            'usage': usage.get(name),
        }
    # OpenRouter funding gauge: the live account funds (from the warmed cache,
    # no network call here) beside the monthly plan, so the operator can see
    # "only $10 deposited right now, expecting to add $50 this month" in one
    # place. used_pct is against the live deposit, matching the alert math.
    credits = _OPENROUTER_CREDITS_CACHE['data']
    openrouter = {
        'monthly_budget_usd': OPENROUTER_MONTHLY_BUDGET_USD,
        'warn_pct_remaining': OPENROUTER_CREDITS_WARN_PCT,
        'crit_pct_remaining': OPENROUTER_CREDITS_CRIT_PCT,
        'api_key_set': bool(OPENROUTER_API_KEY),
        'credits': credits,
        # The credits gauge is a LIFETIME account balance: money the
        # player deposited into the OpenRouter account, which does NOT
        # reset on the first of the month. The monthToDateUsd field
        # below is the slice that does.
        'credits_scope': 'account-lifetime',
        'credits_age_s': (
            (time.time() - _OPENROUTER_CREDITS_CACHE['at'])
            if _OPENROUTER_CREDITS_CACHE['at'] else None),
    }
    if isinstance(credits, dict) and (credits.get('totalCredits') or 0) > 0:
        total = float(credits.get('totalCredits') or 0)
        used = float(credits.get('totalUsage') or 0)
        openrouter['deposited_usd'] = round(total, 4)
        openrouter['spent_usd'] = round(used, 4)
        openrouter['remaining_usd'] = round(max(0.0, total - used), 4)
        openrouter['used_pct'] = round(used / total, 4)
    # Month-to-date spend: the slice that rolls over at each month
    # boundary, for the OpenRouter account and per service. Best-effort
    # -- a ledger failure leaves the knobs and the lifetime account
    # gauge as the readout.
    month_view = {}
    services_view = []
    try:
        ledger = _spend_ledger_read() or {}
        month_view = {
            'month': _spend_cap_period(),
            'openrouterUsd': _openrouter_month_to_date(ledger),
            'allServicesUsd': _ledger_month_to_date(ledger),
        }
        openrouter['monthToDateUsd'] = month_view['openrouterUsd']
        for svc, row in _bank_budget_view(get_state_from_db() or {}).items():
            services_view.append({
                'service': svc,
                'monthUsedUsd': row.get('monthUsed'),
                'totalUsedUsd': row.get('used'),
                'calls': row.get('calls'),
                'cap': row.get('cap'),
                'currency': 'units' if svc == COLAB_LEDGER_KEY else 'usd',
            })
    except Exception:
        pass
    # Per-director slices: the admin-set monthly ceiling each
    # director's team draws on, with live month-to-date usage from
    # the ledger (own burn plus the WHOLE subtree's -- each agent's
    # spend accrues to its roster director, so a director's team
    # total is the sum down the reporting chain), plus the raise
    # requests the chief of staff has not answered yet (filed via
    # POST /api/budget/request).
    director_budgets = {}
    budget_requests = []
    try:
        state = get_state_from_db() or {}
        month = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')
        ledger = _spend_ledger_read()
        usage = {}
        for key, bucket in (ledger or {}).items():
            if (isinstance(key, str) and key.startswith('__director__/')
                    and isinstance(bucket, dict)):
                did = key.split('__director__/', 1)[1]
                series = bucket.get('byMonth') or {}
                usage[did] = round(sum(
                    float(v or 0) for k, v in series.items()
                    if isinstance(k, str) and k.startswith(month)), 6)
        for did, cap in ((state.get('directorBudgets') or {}) or {}).items():
            try:
                cap_usd = float(cap)
            except (TypeError, ValueError):
                continue
            if cap_usd > 0:
                did = str(did)
                subtree = round(sum(
                    float(usage.get(d) or 0)
                    for d in _subtree_agent_ids(state, did)
                    if d in usage), 6)
                director_budgets[did] = {
                    'capUsd': round(cap_usd, 6),
                    'usedUsd': usage.get(did, 0.0),
                    'subtreeUsedUsd': subtree,
                }
        budget_requests = [r for r in (state.get('budgetRequests') or [])
                           if isinstance(r, dict)]
    except Exception:
        pass  # the slice view is best-effort; the knobs stay primary
    return JSONResponse({'ok': True, 'knobs': knobs, 'openrouter': openrouter,
                         'monthToDate': month_view, 'services': services_view,
                         'directorBudgets': director_budgets,
                         'budgetRequests': budget_requests})


@app.post('/api/budget')
async def budget_set(request: Request):
    """Player-session budget write: set one or more knobs by name. Body is a
    flat object of knob names -> non-negative numbers, e.g.
    {"SPEND_CAP_USD": 75, "HIGH_TIER_MONTHLY_BUDGET_USD": 2}. Unknown names and
    non-numeric/negative values are refused (never a partial silent apply --
    all-or-nothing validation first, then one .env write + one reload).
    PLAYER-ONLY (not even the admin): these are the .env spend caps --
    real-money ceilings, the one surface agents never hold."""
    if not _require_player_session(request):
        return JSONResponse({'error': 'Unauthorized -- spend caps are a player-only control'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict) or not body:
        return JSONResponse({'error': 'provide at least one budget knob to set'}, status_code=400)
    updates = {}
    errors = []
    for name, raw in body.items():
        spec = _BUDGET_KNOBS.get(name)
        if not spec:
            errors.append(f'{name} is not a settable budget knob')
            continue
        kind, _desc = spec
        try:
            num = float(raw)
        except (TypeError, ValueError):
            errors.append(f'{name} must be a non-negative number')
            continue
        if num < 0 or num != num or num in (float('inf'), float('-inf')):
            errors.append(f'{name} must be a finite non-negative number')
            continue
        updates[name] = str(int(num)) if kind == 'count' else (f'{num:.6f}'.rstrip('0').rstrip('.') or '0')
    if errors:
        return JSONResponse({'error': '; '.join(errors), 'rejected': errors}, status_code=400)
    try:
        written = _set_env_keys(updates)
    except Exception as e:
        return JSONResponse({'error': f'could not write .env: {e}'}, status_code=500)
    result = _reload_env_config()
    log_action(None, 'budget_set', {'changed': result['changed']}, authorized=True)
    g = globals()
    return JSONResponse({'ok': True, 'written': written,
                         'reloaded': result['changed'],
                         'values': {n: g.get(n) for n in written}})


@app.post('/api/budget/director')
@_state_writer
async def budget_director_set(request: Request):
    """Set (or clear) one director's monthly model-spend slice -- the
    hard ceiling for that director's WHOLE team. The player and
    the chief of staff decide every slice; a director decides only a
    DIRECT sub-director's slice. 0/null clears the cap. A raise
    also answers the pending requests it covers."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- director budgets are an operator control'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    director_id = str(body.get('directorId') or '').strip()
    if not director_id:
        return JSONResponse({'error': 'directorId is required'}, status_code=400)
    raw = body.get('budgetUsd')
    if raw is None:
        cap = 0.0
    else:
        try:
            cap = float(raw)
        except (TypeError, ValueError):
            return JSONResponse({'error': 'budgetUsd must be a non-negative number or null'}, status_code=400)
    if cap < 0 or cap != cap or cap in (float('inf'), float('-inf')):
        return JSONResponse({'error': 'budgetUsd must be a non-negative number or null'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    roster = state.get('agentRoster') or []
    teams = state.get('teams') or []
    known = any(isinstance(d, dict) and d.get('id') == director_id
                for d in roster)
    if not known:
        known = any(isinstance(t, dict) and director_id in (t.get('directorId'), t.get('id'))
                    for t in teams)
    if not known:
        return JSONResponse({'error': f'unknown director: {director_id}'}, status_code=400)
    # The player and the chief of staff decide every slice; a director may
    # decide only a DIRECT sub-director's slice -- never their own,
    # a sibling's, or a worker's (a worker has no slice to set).
    player = bool(verify_session(request.cookies.get(SESSION_COOKIE_NAME)))
    if not player and not _is_admin(state, actor) \
            and not _is_direct_supervisor(state, actor, director_id):
        return JSONResponse({'error': "a director may set only a direct report's slice"}, status_code=403)
    budgets = state.get('directorBudgets')
    if not isinstance(budgets, dict):
        budgets = {}
    if cap > 0:
        budgets[director_id] = round(cap, 6)
    else:
        budgets.pop(director_id, None)
    state['directorBudgets'] = budgets
    granted = 0
    for r in (state.get('budgetRequests') or []):
        if (isinstance(r, dict) and r.get('directorId') == director_id
                and r.get('status') == 'pending'):
            try:
                asked = float(r.get('requestedUsd') or 0)
            except (TypeError, ValueError):
                asked = 0.0
            if cap >= asked:
                r['status'] = 'granted'
                r['grantedAt'] = int(time.time() * 1000)
                granted += 1
    save_state_to_db(state)
    log_action(None, 'director_budget_set',
               {'directorId': director_id, 'capUsd': round(cap, 6),
                'requestsGranted': granted}, authorized=True)
    return JSONResponse({'ok': True, 'directorId': director_id,
                         'capUsd': round(cap, 6),
                         'requestsGranted': granted})


@app.post('/api/budget/request')
@_state_writer
async def budget_request(request: Request):
    """Ask for a bigger monthly slice. A director files for their OWN
    slice (agent key matching their id -- a worker cannot ask, and no
    one asks for another director); the player and the chief of staff may file
    for any director. The request is recorded on state (visible at
    GET /api/budget) and mailed straight to the chief of staff -- never
    relayed up a chain of directors."""
    player = bool(verify_session(request.cookies.get(SESSION_COOKIE_NAME)))
    requester = 'player' if player else _resolve_requester(request)
    if not requester:
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    if requester != 'player':
        if verify_agent_key(requester, request.headers.get('X-Agent-Key')) is not True:
            return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    director_id = str(body.get('directorId') or '').strip()
    if not director_id:
        return JSONResponse({'error': 'directorId is required'}, status_code=400)
    try:
        requested_raw = body.get('requestedUsd')
        if requested_raw is None:
            raise ValueError('requestedUsd is required')
        requested = float(requested_raw)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'requestedUsd must be a positive number'}, status_code=400)
    if requested <= 0 or requested != requested or requested in (float('inf'), float('-inf')):
        return JSONResponse({'error': 'requestedUsd must be a positive number'}, status_code=400)
    reason = str(body.get('reason') or '').strip()[:500]
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    teams = state.get('teams') or []
    is_director = any(isinstance(t, dict) and director_id in (t.get('directorId'), t.get('id'))
                      for t in teams)
    is_admin_actor = _is_admin(state, requester)
    if not is_director:
        return JSONResponse({'error': f'unknown director: {director_id}'}, status_code=400)
    # An agent may only ask for its OWN slice; the player and the
    # admin may ask for any director's.
    if requester != 'player' and not is_admin_actor and requester != director_id:
        return JSONResponse({'error': 'a director may only request a raise for their own slice'}, status_code=403)
    now_ms = int(time.time() * 1000)
    entry = {
        'id': f'br-{now_ms}',
        'directorId': director_id,
        'requestedUsd': round(requested, 6),
        'currentUsd': round(float(((state.get('directorBudgets') or {}).get(director_id)) or 0), 6),
        'reason': reason,
        'status': 'pending',
        'requestedBy': requester,
        'requestedAt': now_ms,
    }
    state.setdefault('budgetRequests', []).append(entry)
    mail_text = (f'Budget request from director {director_id}: raise the '
                 f"monthly slice from ${entry['currentUsd']:.2f} to "
                 f'${requested:.2f}.'
                 + (f' Reason: {reason}' if reason else '')
                 + ' Grant it via POST /api/budget/director.')
    # Flat routing on purpose: every director's raise request goes
    # straight to the chief of staff -- the grantor of any slice -- never
    # relayed up a chain of directors one hop at a time (the operator
    # keeps the org chart flat and the chains short). When the
    # requester IS the admin, the player is the only authority left:
    # no mail, the request rides on GET /api/budget for the player.
    admin_id = _admin_agent_id(state)
    mail_to = admin_id if admin_id and admin_id != requester else 'player'
    mailed = False
    if mail_to != 'player':
        try:
            import sim as _sim
            _sim._deliver_mail(state, mail_to, 'budget_request',
                               {'text': mail_text})
            mailed = True
        except Exception as e:
            print(f'[budget] request mail to admin failed: {e}', flush=True)
    save_state_to_db(state)
    log_action(requester if requester != 'player' else None,
               'budget_request',
               {'directorId': director_id, 'requestedUsd': round(requested, 6),
                'requestId': entry['id'], 'mailedTo': mail_to}, authorized=True)
    return JSONResponse({'ok': True, 'request': entry,
                         'mailedTo': mail_to, 'mailed': mailed})


@app.post('/api/env/reload')
async def env_reload(request: Request):
    # Re-read .env and rebind the live-swappable globals (API keys, budget
    # ceilings, feature toggles, tuning knobs) so an operator's .env edit
    # applies without a server restart -- the same operator action as
    # /api/policy/reload, OPERATOR-ONLY (player session or the chief of staff agent)
    # for the same reason. Values are never echoed back; only the names that
    # changed are reported.
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- reloading env requires the player session or the admin'}, status_code=401)
    result = _reload_env_config()
    log_action(None, 'env_reload', {'changed': result['changed']})
    return JSONResponse({'ok': True, 'changed': result['changed'], 'count': result['count']})

@app.post('/api/allowlist/request')
async def allowlist_request(request: Request):
    # Agent-facing request to extend the player-vetted allowlist.
    # An allowlist entry means FULL read+write reachability for sandboxed
    # scripts AND it skips every future Jev classify round trip -- a permanent
    # capability grant, because a worker can only get one through an
    # explicit human approval, so this fires a floor-1.0 escalation that ONLY
    # the player's email link can resolve (the director is never permitted to
    # auto-approve it). Agents call this instead of editing .env; when the
    # player approves, the host is granted to the runtime allowlist and the
    # egress proxy is refreshed so already-running scripts can reach it too.
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    target = (body.get('host') or body.get('url') or '').strip()
    purpose = (body.get('purpose') or '').strip()
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'allowlist_request'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    if not target:
        return JSONResponse({'error': 'a host or url is required'}, status_code=400)
    # Normalize a full URL down to its hostname, a bare host is acceptable.
    parsed = urllib.parse.urlparse(target if '://' in target else 'https://' + target)
    host = (parsed.hostname or '').strip().lower()
    if not host:
        return JSONResponse({'error': 'could not read a hostname from that target'}, status_code=400)
    # Same boundary as every other gate: private/internal targets are refused
    # outright -- the allowlist extends PUBLIC reachability, never SSRF surface.
    if not _is_safe_public_host(host):
        return JSONResponse({'error': f'{host} is a private or internal host and can never be allowlisted'}, status_code=400)
    if _is_allowlisted_host(host):
        return JSONResponse({'allowlisted': True, 'host': host,
                             'message': 'already on the allowlist -- new requests are not needed'})
    # One pending request per host at a time -- no escalation spam from the
    # same agent retrying a request that's already waiting on the player.
    for esc_id, esc in _load_escalations().items():
        if esc.get('kind') == 'allowlist request' and esc.get('status') == 'pending' \
                and esc.get('note', '').strip().lower() == host:
            return JSONResponse({'requested': True, 'host': host, 'escalationId': esc_id,
                                 'message': f'an allowlist request for this host is already pending '
                                            f'director review ({_allowlist_wait_estimate()}) -- '
                                            f'check allowlist_status for progress, do not re-file'})
    esc_id = create_escalation(
        'allowlist request',
        f'Agent {agent_id} requests {host} be added to the player-vetted allowlist.\n'
        f'Stated purpose: {purpose or "not given"}\n\n'
        f'Approving gives agents standing read+write reachability to {host} for sandboxed '
        f'scripts, AND lets them reach it without a Jev classify round trip on every call. '
        f'A director reviews this request first (the player is only emailed if the director '
        f'cannot decide it automatically).',
        on_approve_note=host,
    )
    log_action(agent_id, 'allowlist_request', {'host': host, 'purpose': purpose[:200],
                                               'escalationId': esc_id}, authorized=authorized)
    return JSONResponse({'requested': True, 'host': host, 'escalationId': esc_id,
                         'message': f'Allowlist request for {host} filed -- escalated to the '
                                    f'directors for review ({_allowlist_wait_estimate()}); the '
                                    f'player is only emailed if the directors cannot decide. '
                                    f'Until a grant lands, {host} still goes through the normal '
                                    f'Jev gate. Check progress with allowlist_status.'})


@app.post('/api/selfedit/propose')
async def selfedit_propose(request: Request):
    """Agent-facing gate for the self-edit pipeline (see selfedit.py).

    Binds the proposer server-side: agentId + X-Agent-Key verified here and
    rate-limited, then the FULL chain runs off the event loop (propose blocks
    on the JEV quorum's sequential judge calls, so it must not sit on the
    request loop). ok=False means nothing was armed and the file tree was
    never touched; every outcome lands in the self_edit_log table."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'selfedit_propose'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    path = (body.get('path') or '').strip()
    find_text = body.get('find') if isinstance(body.get('find'), str) else None
    replace_text = body.get('replace') if isinstance(body.get('replace'), str) else None
    summary = (body.get('summary') or '').strip()
    window_only = bool(body.get('windowOnly'))
    if not path or not find_text or replace_text is None:
        return JSONResponse({'error': 'path, find and replace are required '
                                      '(replace may be an empty string to delete '
                                      'the anchor) and a summary is required'}, status_code=400)
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.propose, agent_id, path, find_text, replace_text, summary,
            window_only=window_only)
    except Exception as exc:
        print(f'[selfedit] propose crashed for {agent_id}: {exc}', flush=True)
        log_action(agent_id, 'selfedit_crashed', {'error': str(exc)[:300]},
                   authorized=authorized)
        return JSONResponse({'error': f'the self-edit harness failed: {exc}'}, status_code=500)
    log_action(agent_id, 'selfedit_propose',
               {'path': path, 'ok': ok, 'id': (payload or {}).get('id'),
                'status': (payload or {}).get('status') or 'n/a',
                'note': (((payload or {}).get('error') or '') or
                         ((payload or {}).get('message') or ''))[:160]},
               authorized=authorized)
    return JSONResponse(payload, status_code=200 if ok else 400)


@app.post('/api/selfedit/status')
async def selfedit_status(request: Request):
    """Read-only bounded view of the self-edit ledger (newest first)."""

    body = await request.json()
    agent_id = (body.get('agentId') or 'unknown')
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'selfedit_status'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    try:
        limit = max(1, min(int(body.get('limit') or 15), 50))
    except (TypeError, ValueError):
        limit = 15
    edits = await asyncio.to_thread(_selfedit.status_snapshot, limit)
    windows = await asyncio.to_thread(_selfedit.windows_snapshot, limit)
    log_action(agent_id, 'selfedit_status',
               {'limit': limit, 'windows': len(windows)},
               authorized=authorized)
    return JSONResponse({'edits': edits, 'windows': windows})


@app.post('/api/selfedit/revert')
async def selfedit_revert(request: Request):
    """Operator-gated undo for an APPLIED self-edit (selfedit.revert).

    Two legitimate callers: the player (session cookie) or the chief of staff agent
    (agentId + key + _is_admin). Everyone else is refused; restores the
    pre-edit bytes, reloads the module, and marks the row reverted."""
    body = await request.json()
    edit_id = (body.get('editId') or '').strip()
    if not edit_id:
        return JSONResponse({'error': 'editId is required'}, status_code=400)
    agent_id = (body.get('agentId') or '').strip()
    session = verify_session(request.cookies.get(SESSION_COOKIE_NAME))
    admin_agent = None
    if agent_id and verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) \
            and _is_admin(get_state_from_db() or {}, agent_id):
        admin_agent = agent_id
    if not session and not admin_agent:
        return JSONResponse({'error': 'only the player session or the chief of staff '
                                      'agent may revert a self-edit'}, status_code=403)
    try:
        ok, payload = await asyncio.to_thread(
            _selfedit.revert, edit_id, 'player' if session else admin_agent)
    except Exception as exc:
        print(f'[selfedit] revert crashed for {edit_id}: {exc}', flush=True)
        return JSONResponse({'error': f'revert failed: {exc}'}, status_code=500)
    log_action('player' if session else admin_agent, 'selfedit_revert',
               {'editId': edit_id, 'ok': ok,
                'note': ((payload or {}).get('error') or '')[:160]}, authorized=True)
    return JSONResponse(payload, status_code=200 if ok else 400)
