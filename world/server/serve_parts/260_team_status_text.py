

def _team_status_text(max_chars=1600):
    """Read-only, DB-only FORWARD-LOOKING team view: who is working on what
    right now, what is queued, what failed, and what is waiting on a human.
    Complements team_digest (which is retrospective, from the action log) --
    added for the live agent complaint 'I can't see how work moves between
    teammates... nothing I can reach shows the planning queue, spike status,
    or review backlog' (Clara). No LLM, no network."""
    state = get_state_from_db() or {}
    agents = state.get('agents') or {}
    tasks = state.get('tasks') or {}
    lines = []
    # Why is it stuck? A bare status word ("blocked") is what every agent
    # said they could see with no way to find out WHY (2026-10-13 review: the
    # status view 'doesn't show why the loops start'). Pull the most recent
    # rejection/stuck note per task straight from the action log -- DB-only,
    # no LLM, same shape as the rest of this view.
    last_note = {}
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT action, details FROM action_log "
                "WHERE action IN ('quality_gate_reject', 'worker_stuck_help') "
                "ORDER BY ts DESC LIMIT 400").fetchall()
        for _action, _details in rows:
            try:
                _d = json.loads(_details) or {}
            except Exception:
                continue
            _tid = _d.get('taskId')
            if not _tid or _tid in last_note:
                continue
            _note = str(_d.get('note') or _d.get('reason') or _action)
            last_note[_tid] = ' '.join(_note.split())[:180]
    except Exception:
        pass
    working = []
    for aid, a in agents.items():
        if not isinstance(a, dict):
            continue
        tid = a.get('task')
        if tid:
            t = tasks.get(tid) or {}
            # Room included: the 2026-10-13 review asked for cross-room
            # visibility ('I cannot see other rooms' active workflows unless
            # they send a handoff'). Task title alone does not say WHERE.
            working.append(f'- {aid}: {t.get("title") or tid} [{t.get("status") or "?"}'
                           f'{", " + t["room"] if t.get("room") else ""}]')
    lines.append('WORKING NOW:\n' + ('\n'.join(working) if working else '- nobody is on a task right now'))
    queue = [w for w in (state.get('workQueue') or []) if isinstance(w, dict)]
    if queue:
        # Owner shown: 'nothing shows who picks up queued work' was the
        # complaint that hid a 21k-attempt wedge -- assignedTo existed in the
        # state the whole time and simply was not rendered here.
        ql = []
        for w in queue[:10]:
            owner = w.get('assignedTo')
            room = w.get('room')
            tries = w.get('attempts') or 0
            bits = [f'- [{w.get("taskType") or "work"}] {w.get("title") or "??"}']
            if owner:
                bits.append(f'pinned to {owner}')
            if room:
                bits.append(f'room {room}')
            if tries > 1:
                bits.append(f'{tries} attempts')
            ql.append(', '.join(bits))
        if len(queue) > 10:
            ql.append(f'- ...and {len(queue) - 10} more queued')
        lines.append('QUEUE (waiting to be picked up):\n' + '\n'.join(ql))
    else:
        lines.append('QUEUE: empty')
    counts, attention = {}, []
    for tid, t in tasks.items():
        if not isinstance(t, dict):
            continue
        st = t.get('status') or '?'
        counts[st] = counts.get(st, 0) + 1
        if st in ('failed', 'blocked') or (st == 'working' and t.get('workUntil')
                                           and t['workUntil'] < time.time() - 300):
            entry = f'- {tid}: {st} -- {t.get("title") or "??"}'
            if last_note.get(tid):
                entry += f'\n    why: {last_note[tid]}'
            attention.append(entry)
    lines.append('TASK STATUS COUNTS: ' + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))
    if attention:
        lines.append('NEEDS ATTENTION (stuck/failed):\n' + '\n'.join(attention[:8]))
    try:
        pending_esc = [(eid, e) for eid, e in (_load_escalations() or {}).items()
                       if isinstance(e, dict) and e.get('status') == 'pending']
        if pending_esc:
            kinds = {}
            for _eid, e in pending_esc:
                kinds[e.get('kind') or '?'] = kinds.get(e.get('kind') or '?', 0) + 1
            lines.append('WAITING ON A HUMAN/DIRECTOR DECISION: '
                         + ', '.join(f'{k} x{v}' for k, v in sorted(kinds.items())))
            # The count alone read as "routine x1" with no detail, which is
            # what the leaked placeholder escalation looked like for a week.
            # Show what is actually waiting, by id, so it is actionable.
            now = time.time()
            detail = []
            for eid, e in pending_esc[:6]:
                q = (e.get('question') or e.get('note') or '').strip() or '(no detail on record)'
                age_min = max(0, int((now - (e.get('ts') or now)) / 60))
                detail.append(
                    f'- {eid} [{e.get("kind") or "?"}] {q[:200]} '
                    f'(waiting {age_min}m, director pass {e.get("askCount") or 0}'
                    f'{", player already notified" if e.get("humanNotified") else ""})')
            lines.append('\n'.join(detail))
    except Exception:
        pass
    return '\n\n'.join(lines)[:max_chars]


def _allowlist_status_text(host=None, max_chars=1600):
    """Read-only status of browse-allowlist access: for a named host, whether
    it is reachable now (and via which tier -- env baseline, runtime grant, or
    the read-only standing list); plus every PENDING allowlist request /
    candidate with its age and how many director passes it has had, recent
    grant/revoke/request activity, and the historical wait estimate. Answers
    the live agent complaint 'I have no way to tell whether that wait will
    take minutes or days' (Dev, June) without any email."""
    host = (host or '').strip().lower()
    lines = []
    if host:
        if _is_allowlisted_host(host):
            src = 'runtime grant' if host in _runtime_allowlist_grants() else 'env baseline'
            lines.append(f'{host}: ALLOWLISTED ({src}) -- full read+write reachability, no Jev round trip.')
        elif _is_standing_safe_host(host):
            lines.append(f'{host}: standing safe domain (read-only tier) -- browse/GET skip Jev; '
                         f'sandbox scripts still need a real grant.')
        else:
            lines.append(f'{host}: NOT allowlisted -- normal Jev gate applies per request.')
    now = time.time()
    pending = [(eid, e) for eid, e in (_load_escalations() or {}).items()
               if e.get('status') == 'pending'
               and e.get('kind') in ('allowlist request', 'allowlist candidate')]
    if pending:
        lines.append('PENDING REQUESTS:')
        for eid, e in pending[:8]:
            target = (e.get('note') or '').strip() or '(unknown host)'
            age_min = int((now - (e.get('ts') or now)) / 60)
            asks = e.get('askCount') or 0
            state_note = ('player emailed (directors gave up)' if e.get('humanNotified')
                          else f'director pass {asks}')
            lines.append(f'- {eid}: {target} -- waiting {age_min}m, {state_note}')
    else:
        lines.append('PENDING REQUESTS: none')
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT action, ts, details FROM action_log "
                "WHERE action IN ('allowlist_request', 'allowlist_grant', "
                "'allowlist_revoke', 'escalation_approve', 'escalation_deny') "
                "AND ts > ? ORDER BY ts DESC LIMIT 8",
                (now - 30 * 86400,)).fetchall()
        if rows:
            recent = []
            for action, ts, details in rows:
                d = details or ''
                try:
                    d = (json.loads(details) or {}).get('host') or d
                except Exception:
                    pass
                recent.append(f'- {int(now - ts)}s ago: {action} {str(d)[:80]}')
            lines.append('RECENT ACTIVITY:\n' + '\n'.join(recent))
    except Exception:
        pass
    lines.append(_allowlist_wait_estimate())
    return '\n\n'.join(lines)[:max_chars]


@app.post('/api/intent/ask')
@_state_writer
async def intent_ask(request: Request):
    """Player asks the think tank a genuinely NEW, one-off question -- something
    unrelated to existing products/work, which is exactly what makes it distinct
    from /api/intent/clarify (a clarify is a question ABOUT completed work and is
    keyed by productId; this is net-new and keyed by nothing but the ask itself).

    Body: {question, location?, agentId?, approvedTools?}. The question is
    required; `approvedTools` is the per-conversation allowlist for commit-tier
    tools (for example ["create_calendar_event"]); `location`
    is a hint for location-bearing asks (e.g. dressing advice) though the agent
    can also derive it. A free, non-chief-of-staff agent is dispatched round-robin
    unless `agentId` names a specific eligible candidate to pin instead (so
    the player can deliberately address one agent, e.g. the Red
    Team Auditor role's live security checks, rather than whoever's next). It
    may call tools (weather; plus real curl/capability-handle tools when the
    dispatched agent's role is Red Team Auditor) mid-turn via an agentic tool
    loop; the FINAL answer comes straight back to the player. Nothing here
    enters the sprint/grade/release/publish pipeline -- an ask produces a
    reply, not a deliverable.

    Tool outputs are external data and are wrapped with the injection boundary
    (wrap_external_content) before they fold into the model's context, so the
    agent reasons about weather as DATA, never as instructions it must follow.

    Thin wrapper: all real behavior lives in _ask_core (also called directly
    by the Telegram bridge, added with no HTTP hop in between).
    """
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    if not check_rate_limit(ASK_LANE_RATE_LIMIT_KEY):
        return JSONResponse({'error': 'Rate limit hit -- too many questions at once. Wait a minute and try again.'}, status_code=429)
    # Read a snapshot without holding the whole-state write lock. The ask lane
    # can call the model and tools for minutes; holding the lock across that
    # work starves the sim tick (and a lost release wedges it forever). Only a
    # parked ask mutates state, and that branch re-reads under the lock below.
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    # Conversation memory for the public ask lane: one STM/LTM bucket per
    # named agent (a shared 'any' bucket when the player does not name one).
    # The 2026-13 capability review could not answer "are the concerns you
    # raised last night resolved?" for 12 of 14 agents because the lane was
    # stateless and the reply was never stored -- the memory tables already
    # existed for the Telegram bridge and simply were not used here. Read path
    # is DB-only; the only model call (LTM extraction) is background and runs
    # every LTM_EXTRACT_EVERY_TURNS, never on the ask's critical path.
    _ask_target = (body.get('agentId') or '').strip() or 'any'
    pending_before = {a.get('id') for a in (state.get('_pendingAsks') or [])}
    result = await _ask_core(state, body.get('question'), body.get('agentId'),
                             body.get('location'), body.get('max_tokens', 300),
                             session_id=f'player-ask:{_ask_target.lower()}',
                             approved_tools=body.get('approvedTools'),
                             best_effort=bool(body.get('bestEffort')))
    if 'error' in result:
        return JSONResponse({'error': result['error']}, status_code=result.get('status', 500))
    if result.get('queued'):
        fresh = [a for a in (state.get('_pendingAsks') or [])
                 if a.get('id') not in pending_before]
        latest = _state_begin()
        if latest is None:
            _state_abort()
        else:
            try:
                existing = {a.get('id') for a in (latest.get('_pendingAsks') or [])}
                latest.setdefault('_pendingAsks', []).extend(
                    a for a in fresh if a.get('id') not in existing)
                save_state_to_db(latest)
            finally:
                _state_abort()
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Theo request routing: classify a free-text player request
# (today only reached from the Telegram bridge) into one of six lanes, using
# the same Jev N-way choice pattern the rest of the codebase already uses for
# judgment calls (sim._governance_decider_default; the model-tier picker
# above). Every lane wires into machinery that already existed but had no
# real caller (sim.queue_spike, sim.queue_bug) or no creation path at all
# (researchTopics).
# ---------------------------------------------------------------------------

# Role policy for Theo: the same request means different authority
# depending on who is talking to him. Each lane maps to one policy
# action; a requester of None is the player's own session -- the owner
# of the think tank -- so the player's requests keep working exactly as
# before, while an agent requester is gated by its roster role (see
# world/theo_policy.py for the role, capability, approval, rate-limit,
# and budget rules).
_LANE_ACTIONS = {
    'ask': 'converse',
    'unclear': 'converse',
    'schedule': 'create_schedule',
    'schedule_once': 'create_schedule',
    'spike': 'start_run',
    'story': 'create_story',
    'incident': 'report_status',
}

# The granted-key registry (ask 3): the owner's
# grants of Theo's authority to agents, each key
# carrying the agent id, the role that agent holds
# through the key, and the key's own rate limit and
# budget. Raw keys are shown once at grant time and
# stored nowhere -- only their SHA-256 hashes
# persist, in the library, so a restart keeps the
# owner's grants without keeping anything replayable.
_KEY_REGISTRY_PATH = os.path.join(LIBRARY_DIR,
                                  'theo-keys.json')


def _load_key_registry():
    """The persisted registry, or an empty one. A
    missing file is the normal first boot; an
    unreadable or corrupt one must not stop the
    server -- start empty and let the next grant
    write a clean file."""
    try:
        return theo_policy.KeyRegistry.load(
            _KEY_REGISTRY_PATH)
    except (FileNotFoundError, OSError,
            ValueError, KeyError):
        return theo_policy.KeyRegistry()


def _save_key_registry():
    """Persist the registry (hashes only) to the
    library, creating the directory on first boot."""
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    _KEY_REGISTRY.save(_KEY_REGISTRY_PATH)


_KEY_REGISTRY = _load_key_registry()

_theo_policy = theo_policy.TheoPolicy(
    registry=_KEY_REGISTRY,
    storage_path=os.path.join(LIBRARY_DIR, 'theo-approvals.json'))

# The owner adjudicates a pending approval by texting
# "approve apr_<id>" or "deny apr_<id>" -- checked before lane
# classification so an approval answer is never mistaken for a
# new request.
_APPROVAL_COMMAND_RE = re.compile(
    r'^\s*(approve|deny)\s+(apr-[0-9a-f]+)\s*$', re.IGNORECASE)


def _parse_approval_command(text):
    """Return (verb, approval_id) when `text` is an owner approval
    command, else None."""
    match = _APPROVAL_COMMAND_RE.match(text or '')
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


@app.get('/api/theo/keys')
async def theo_keys_list(request: Request):
    """List every granted Theo key, secret-free: the
    owner sees which agent holds which role, with
    which limits and expiry. Operator-gated -- only
    the owner (or a director/admin) may see the
    grants at all."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return JSONResponse({'keys': _KEY_REGISTRY.list_keys()})


@app.post('/api/theo/keys')
async def theo_keys_grant(request: Request):
    """Grant an agent a Theo key (ask 3). Body:
    {agentId, role, rateLimit?, budget?, ttlSeconds?}.
    The raw key is returned exactly once, in this
    response, and stored nowhere -- only its hash
    is kept, so a stolen registry copy cannot be
    replayed. `rateLimit` is the key's own per-window
    allowance (the default when omitted), `budget`
    caps what the key may charge (omitted = no cap),
    and `ttlSeconds` expires the grant."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {'error': 'body must be a JSON object'}, status_code=400)
    agent_id = (body.get('agentId') or '').strip()
    role = (body.get('role') or '').strip()
    if not agent_id:
        return JSONResponse(
            {'error': '"agentId" is required'}, status_code=400)
    if not role:
        return JSONResponse(
            {'error': '"role" is required'}, status_code=400)
    rate_limit = body.get('rateLimit')
    if rate_limit is not None and not isinstance(rate_limit, int):
        return JSONResponse(
            {'error': '"rateLimit" must be an integer'},
            status_code=400)
    budget = body.get('budget')
    if budget is not None and not isinstance(budget, (int, float)):
        return JSONResponse(
            {'error': '"budget" must be a number'}, status_code=400)
    ttl = body.get('ttlSeconds')
    if ttl is not None and not isinstance(ttl, (int, float)):
        return JSONResponse(
            {'error': '"ttlSeconds" must be a number'},
            status_code=400)
    granted = _KEY_REGISTRY.grant_key(
        'owner', agent_id, role,
        rate_limit=(rate_limit
                    if rate_limit is not None
                    else theo_policy.DEFAULT_RATE_LIMIT),
        budget=budget,
        ttl=(ttl if ttl is not None else None))
    if granted is None:
        return JSONResponse(
            {'error': 'unknown role, or grant refused'},
            status_code=400)
    raw, key = granted
    _save_key_registry()
    log_action('player', 'theo_key_granted',
               {'keyId': key.key_id, 'agentId': agent_id,
                'role': role}, authorized=True)
    return JSONResponse({'ok': True, 'key': raw,
                         'record': key.as_dict()})


@app.post('/api/theo/keys/revoke')
async def theo_keys_revoke(request: Request):
    """Revoke a granted Theo key by id. Body: {keyId}.
    A revoked key stops working immediately and looks
    to its holder exactly like one that never existed."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {'error': 'body must be a JSON object'}, status_code=400)
    key_id = (body.get('keyId') or '').strip()
    if not key_id:
        return JSONResponse(
            {'error': '"keyId" is required'}, status_code=400)
    if not _KEY_REGISTRY.revoke('owner', key_id):
        return JSONResponse({'error': 'unknown keyId'},
                            status_code=404)
    _save_key_registry()
    log_action('player', 'theo_key_revoked',
               {'keyId': key_id}, authorized=True)
    return JSONResponse({'ok': True, 'keyId': key_id})


_ROUTING_LANES = [
    {'id': 'ask', 'description': "A direct question expecting an immediate, conversational answer right now (trivia, opinion, a quick lookup, small talk) -- not a request to change, build, or investigate anything in the think tank or its products."},
    {'id': 'schedule', 'description': "Asks for something to happen automatically on a recurring or periodic basis going forward (e.g. 'check X every hour/day', 'keep watching Y and update it') -- a standing job, not a one-time favor."},
    {'id': 'schedule_once', 'description': "Asks for a single, one-time task to be done AT a specific day and time the player names (e.g. 'remind me to deploy the release Tuesday at 9am', 'run the monthly report on the 1st at 3pm once') -- a one-shot event on a calendar time, not a recurring cadence and not something to do right now."},
    {'id': 'spike', 'description': "Asks the think tank to look into or figure something out ONE TIME, with no need for an immediate reply and no clearly defined deliverable yet -- exploratory, time-boxed digging, not a committed piece of work."},
    {'id': 'story', 'description': "Asks for something substantial to be BUILT, CHANGED, or DELIVERED -- a real feature, fix, or piece of work with a concrete outcome, sized for a team's real backlog and sprint process."},
    {'id': 'incident', 'description': "Reports something already broken, down, or failing RIGHT NOW in a live product, wanting it fixed urgently."},
    {'id': 'unclear', 'description': "None of the above genuinely fits, or the message BUNDLES MULTIPLE UNRELATED REQUESTS at once (one message asking for two or more different things), or it's ambiguous/contradictory/high-stakes enough that only a human director should decide how to handle it -- do not force a single-lane fit or silently drop the rest."},
]


def _classify_request_lane_default(state, text):
    """Jev choice over _ROUTING_LANES -- same dynamic-candidates-to-criteria
    shape as sim._governance_decider_default and the model-tier picker above,
    kept in serve.py (not sim.py) because this feature is driven top-down FROM
    serve.py's Telegram bridge, and a separate injectable here can't collide
    with a test that monkeypatches one of sim.py's own ceremony deciders.
    Returns a lane id, or None on a Jev outage or an unrecognized choice --
    the caller treats None as 'unclear', this function never guesses."""
    # Quorum-sampled -- routing story vs spike (peer-gated or
    # not) off a single noisy Jev sample is the same class of real problem
    # already confirmed for safety gates (the same URL, a confident 0.87
    # allow one run, a low-confidence 0.56 deny the next), just never
    # protected here before.
    choice, _confidence, _cost = _jev_quorum_choice_sync(
        f'The think tank admin is triaging one free-text message the player just sent. Player\'s message: "{text}"',
        {c['id']: c['description'] for c in _ROUTING_LANES})
    return choice if any(c['id'] == choice for c in _ROUTING_LANES) else None


_lane_decider = _classify_request_lane_default  # injectable test seam


def _classify_team_default(state, text):
    """Jev choice over state['teams'] -- because a team IS a director
    (team['id'] == team['directorId'], teams are derived one-per-director,
    serve.py _backfill_teams_in_db), picking a team is picking a director; no
    separate director-resolution step is needed. Returns a team/director id,
    or None on outage or no teams exist yet."""
    # Only staffed teams are candidates: a team with an explicitly empty
    # roster (a stale admin entry, or a lead whose reports have not landed
    # yet) cannot take work, and offering it to the classifier turns team
    # routing into a coin flip. An unknown member list stays a candidate.
    admin_ids = _admin_ids()
    teams = [t for t in (state.get('teams') or [])
             if t.get('id') and t.get('id') not in admin_ids
             and t.get('members') != []]
    if not teams:
        return None
    candidates = [{'id': t['id'], 'description': t.get('purpose') or f"Team directed by {t.get('name', t['id'])}."}
                  for t in teams]
    choice, _confidence, _cost = _jev_quorum_choice_sync(
        f'Which team should handle this player request: "{text}"',
        {c['id']: c['description'] for c in candidates})
    return choice if any(c['id'] == choice for c in candidates) else None


_team_decider = _classify_team_default  # injectable test seam


def _room_keyword_pick(text):
    """Deterministic first pass for room routing: score each room's own
    craft vocabulary (sim's _ROOM_KEYWORDS, the same word map charter
    alignment already keys on) against the text and return the single
    clear winner. Free, no network, no quorum resampling. A wash or a
    tie is NOT a classification -- it returns None so the Jev choice
    below still gets the fuzzy-paraphase cases exactly as before.
    Word-boundary matching so 'press' never matches inside 'pressure'
    and 'art' never matches inside 'article'."""
    import sim as _sim
    low = (text or '').lower()
    scores = {}
    for room, words in (_sim._ROOM_KEYWORDS or {}).items():
        if room not in _DELEGATABLE_ROOMS:
            continue
        hits = 0
        for w in words:
            if w and re.search(r'(?<![a-z0-9])' + re.escape(w) + r'(?![a-z0-9])', low):
                hits += 1
        if hits:
            scores[room] = hits
    if not scores:
        return None
    best = max(scores.values())
    winners = sorted(r for r, s in scores.items() if s == best)
    return winners[0] if len(winners) == 1 else None


def _classify_room_default(state, text):
    """Two-rung room router: a deterministic keyword pass over the room
    craft vocabulary first (no model call, no quorum), then the Jev
    choice over _DELEGATABLE_ROOMS, reusing the same purpose text
    assign-big-task's own prompt already uses. Falls back to 'observatory'
    (the general investigate/research room) on outage rather than returning
    None -- every spike needs SOME room to be queued into."""
    keyword_pick = _room_keyword_pick(text)
    if keyword_pick:
        return keyword_pick
    room_defs = _room_definitions(state)
    candidates = [{'id': r, 'description': room_defs[r]['purpose']} for r in _DELEGATABLE_ROOMS]
    choice, _confidence, _cost = _jev_quorum_choice_sync(
        f"Which room's real capability best fits investigating this: \"{text}\"",
        {c['id']: c['description'] for c in candidates})
    return choice if any(c['id'] == choice for c in candidates) else 'observatory'


def _product_name_pin(products, text):
    """Deterministic first pass for product routing: pin the product only
    when exactly one product's full name appears verbatim (word-bounded,
    case-insensitive) in the text. Partial, short-name, or multi-product
    matches return None -- the incident lane must never guess which
    product is broken, so anything ambiguous stays with the Jev choice."""
    low = (text or '').lower()
    hits = []
    for pid, p in products.items():
        name = (p.get('name') or pid or '').strip()
        if len(name) < 4:
            continue
        if re.search(r'(?<![a-z0-9])' + re.escape(name.lower()) + r'(?![a-z0-9])', low):
            hits.append(pid)
    return hits[0] if len(hits) == 1 else None


def _classify_product_default(state, text):
    """Two-rung product router for the incident lane: a deterministic
    full-name pin first (an unambiguous verbatim product mention needs no
    judgment call), then the Jev choice over state['products']. Returns a
    product id, or None on outage/no products/no confident match -- the
    incident lane must never guess which product is broken (a mis-pinned
    incident is worse than an unattributed investigation)."""
    products = state.get('products') or {}
    if not products:
        return None
    pinned = _product_name_pin(products, text)
    if pinned:
        return pinned
    candidates = [{'id': pid, 'description': (p.get('name') or pid) + (f" -- {p.get('summary')}" if p.get('summary') else '')}
                  for pid, p in products.items()]
    choice, _confidence, _cost = _jev_quorum_choice_sync(
        f"Which product does this incident report describe: \"{text}\"",
        {c['id']: c['description'] for c in candidates})
    return choice if any(c['id'] == choice for c in candidates) else None


def _extract_schedule_fields_sync(admin_id, key, text):
    """A small /api/chat JSON-extraction call (mirrors assign-big-task's own
    extraction pattern above) -- Jev is a choice-over-a-fixed-set primitive,
    the wrong tool for pulling a URL and a cadence out of free text. Returns
    {'topic','startUrl','cadenceMs'} or None on anything that doesn't parse
    cleanly -- fails closed, never invents a URL. Blocking (urllib); callers
    run it via asyncio.to_thread, same convention as _call_openrouter_sync."""
    system_prompt = (
        'Extract a recurring web-monitoring request from the player\'s message. '
        'Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this shape: '
        '{"topic": "short label for what to watch", "startUrl": "a real absolute URL to start from, or empty string if none was given", "cadenceMs": integer milliseconds between checks, "dependsOnTask": "a task id this should wait to finish before starting, or empty string if none was given"}. '
        'If the message gives a cadence in words (daily, hourly, every 6 hours, weekly), convert it to milliseconds. '
        'If no real URL is identifiable, set startUrl to an empty string -- do not invent one. '
        'If the message asks for this to run only AFTER some other task/story lands, put that task id in dependsOnTask; otherwise leave it empty.'
    )
    try:
        r = _http_json('POST', SELF_BASE_URL, '/api/chat',
                       {'model': _resolve_model_tier('Extract a recurring web-monitoring request (topic, start URL, cadence) from a free-text message'),
                        'messages': [{'role': 'system', 'content': system_prompt},
                                     {'role': 'user', 'content': text}],
                        'max_tokens': 200, 'agentId': admin_id}, key)
    except Exception:
        return None
    if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
        return None
    cleaned = re.sub(r'^```json\s*|^```\s*|```\s*$', '', r['reply'].strip())
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    topic = (parsed.get('topic') or '').strip()
    start_url = (parsed.get('startUrl') or '').strip()
    cadence_ms = parsed.get('cadenceMs')
    depends_on_task = (parsed.get('dependsOnTask') or '').strip()
    if not topic or not start_url or not isinstance(cadence_ms, (int, float)):
        return None
    return {'topic': topic, 'startUrl': start_url, 'cadenceMs': int(cadence_ms),
            'dependsOnTask': depends_on_task or None}


# A one-off scheduled task can be requested as far ahead as this (366 days)
# -- far enough for a real annual event, tight enough that a misparsed date
# can't sit silently in the queue for years.
SCHEDULE_ONCE_MAX_AHEAD_MS = 366 * 24 * 3600 * 1000


def _parse_schedule_once_at(value, now_ms=None):
    """Parse a player-supplied "at" into epoch milliseconds, or None if it
    can't be read. Accepts an ISO 8601 string (a trailing 'Z' or an explicit
    offset; a naive time is treated as UTC so scheduling is unambiguous
    regardless of where the server sits) or a bare number (treated as epoch
    milliseconds above 1e12, epoch seconds otherwise). Fails closed -- a
    time that doesn't parse is None, never guessed."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return None
        return int(n) if n >= 1e12 else int(n * 1000)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # A bare numeric string is epoch seconds/ms, same as the numeric path.
        try:
            n = float(s)
            return int(n) if n >= 1e12 else int(n * 1000)
        except ValueError:
            pass
        normalized = s.replace('Z', '+00:00')
        try:
            dt = datetime.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def _extract_schedule_once_fields_sync(admin_id, key, text):
    """Small /api/chat JSON-extraction call (same shape as
    _extract_schedule_fields_sync above) that pulls {title, at, instructions}
    out of a free-text one-off scheduling request. `at` must be an ISO 8601
    day+time. Returns None on anything that doesn't parse -- fails closed,
    never invents a time."""
    system_prompt = (
        'Extract a one-time scheduled task from the player\'s message. '
        'Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this shape: '
        '{"title": "short label for the single task to do", "at": "ISO 8601 day and time, e.g. 2026-10-20T14:00:00-04:00, or an explicit Z suffix for UTC", "instructions": "short instruction for the task, or empty string if the title already says it", "dependsOnTask": "a task id this should wait to finish before running, or empty string if none was given"}. '
        'The player wants this done ONCE, at that specific time, not on a repeating cadence. '
        'If no clear day+time is identifiable, set "at" to an empty string -- do not invent one. '
        'If the message asks for this to run only AFTER some other task/story lands, put that task id in dependsOnTask; otherwise leave it empty.'
    )
    try:
        r = _http_json('POST', SELF_BASE_URL, '/api/chat',
                       {'model': _resolve_model_tier('Extract a one-off scheduled task (title, absolute ISO day+time) from a free-text message'),
                        'messages': [{'role': 'system', 'content': system_prompt},
                                     {'role': 'user', 'content': text}],
                        'max_tokens': 200, 'agentId': admin_id}, key)
    except Exception:
        return None
    if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
        return None
    cleaned = re.sub(r'^```json\s*|^```\s*|```\s*$', '', r['reply'].strip())
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    title = (parsed.get('title') or '').strip()
    at = (parsed.get('at') or '').strip()
    instructions = (parsed.get('instructions') or '').strip()
    depends_on_task = (parsed.get('dependsOnTask') or '').strip()
    if not title or not at:
        return None
    at_ms = _parse_schedule_once_at(at)
    if not at_ms:
        return None
    return {'title': title, 'at': at, 'atMs': at_ms, 'instructions': instructions or None,
            'dependsOnTask': depends_on_task or None}


def _pick_team_worker(state, director_id):
    """A real worker to pin a directly-routed spike to -- reuses
    sim.on_call_agent's existing deterministic-rotation-preferring-active
    picker rather than a bespoke one; an incident already needs exactly this
    'a real team member, prefer someone awake' property, and a spike wants
    the same thing. Off-duty pinned agents are woken automatically at
    assignment time by _assign_due_item's existing pin-wake logic once
    directRoute is set -- no separate wake call is needed here."""
    import sim as _sim
    return _sim.on_call_agent(state, director_id)


async def _route_lane_ask(state, text, admin_id, session_id=None):
    # Trusted internal pin: the player is texting Theo specifically, so Theo
    # (the admin) is who answers -- allow_admin_pin=True is safe here in a
    # way it is not for the public /api/intent/ask endpoint's raw player-
    # submitted agentId (see _ask_core's own comment on the parameter).
    result = await _ask_core(state, text, admin_id, allow_admin_pin=True, session_id=session_id)
    if result.get('queued'):
        save_state_to_db(state)
    return result


async def _route_lane_unclear(state, text, admin_id, session_id=None):
    # Routing reconciliation: the 'unclear'
    # lane -- a multi-intent bundle or an ask no classifier can pin -- is a
    # director's judgment call. The SENIOR-MOST director answers directly,
    # with the chief of staff as fallback (the chief of staff is herself a director, but she
    # doesn't delegate to herself first). Pinning a specific authority instead
    # of a random free agent IS the "needs a director's judgment" behavior.
    authority = _unclear_lane_authority(state)
    result = await _ask_core(state, text, (authority or {}).get('id') or admin_id,
                             allow_admin_pin=True, session_id=session_id)
    if result.get('queued'):
        save_state_to_db(state)
    return result


def _unclear_lane_authority(state):
    """The authority who answers an 'unclear' lane ask: the senior-most
    non-chief-of-staff director if free, else the chief of staff if free, else None (ask_core
    degrades to a round-robin agent / parking). Mirrors _free_authority's
    free/on-duty gate but flips the priority -- a multi-intent bundle is a
    director judgment call, so the senior-most director speaks first."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    def _free(d):
        a = agents.get(d.get('id'))
        return a is not None and not a.get('busy') and not a.get('offDuty')
    for d in roster:
        if d.get('isDirector') and not d.get('isAdmin') and not d.get('director') and _free(d):
            return d
    for d in roster:
        if d.get('isAdmin') and _free(d):
            return d
    return None


async def _route_lane_schedule(state, text, admin_id):
    key = get_or_create_agent_key(admin_id)
    fields = await asyncio.to_thread(_extract_schedule_fields_sync, admin_id, key, text)
    if not fields:
        return {'reply': "I couldn't pin down a starting link and how often to check it -- "
                          "can you give me a URL and a cadence, e.g. \"check https://example.com "
                          "daily\" or \"every 6 hours\"?"}
    import sim as _sim
    record = _sim.add_research_topic(state, fields['topic'], fields['startUrl'],
                                     fields['cadenceMs'],
                                     depends_on_task=fields.get('dependsOnTask'))
    if not record:
        return {'reply': "That didn't look like a real link I could start from -- can you double-check the URL?"}
    note_watchlist_topic(record)  # reflect the villager's addition into world/watchlist.json
    save_state_to_db(state)
    log_action('player', 'schedule_created', {'topicId': record['id'], 'topic': record['topic'],
                                              'startUrl': record['startUrl'],
                                              'cadenceMs': record['cadenceMs'],
                                              'dependsOnTask': record.get('dependsOnTask')},
              authorized=True)
    _append_passport_decision('schedule_created', admin_id, {'topicId': record['id'], 'topic': record['topic']})
    hours = record['cadenceMs'] / 3600000
    cadence_desc = f"every {hours:.1f}h" if hours < 48 else f"every {hours / 24:.1f}d"
    if record.get('dependsOnTask'):
        return {'reply': f"Got it -- I'll check \"{record['topic']}\" ({cadence_desc}) once "
                         f"{record['dependsOnTask']} finishes."}
    return {'reply': f"Got it -- I'll keep checking \"{record['topic']}\" ({cadence_desc}), starting shortly."}


async def _route_lane_schedule_once(state, text, admin_id):
    key = get_or_create_agent_key(admin_id)
    fields = await asyncio.to_thread(_extract_schedule_once_fields_sync, admin_id, key, text)
    if not fields:
        return {'reply': "I couldn't pin down a one-time task and a specific day+time -- "
                          "can you give me what to do and when, e.g. \"run the release checklist "
                          "Friday at 3pm\"?"}
    import sim as _sim
    now_ms = int(time.time() * 1000)
    at_ms = int(fields['atMs'])
    if at_ms <= now_ms:
        return {'reply': "That time is already in the past -- give me a day and time in the future."}
    if at_ms - now_ms > SCHEDULE_ONCE_MAX_AHEAD_MS:
        return {'reply': "That's more than a year out -- I'll schedule things up to a year ahead."}
    item = _sim.queue_once(state, fields['title'], at_ms,
                           instructions=fields.get('instructions'),
                           depends_on_task=fields.get('dependsOnTask'))
    if not item:
        return {'reply': "I couldn't schedule that -- try giving it a clear task name and a future time."}
    save_state_to_db(state)
    log_action('player', 'schedule_once_created', {'title': fields['title'],
                                                   'at': fields['at'], 'atMs': at_ms,
                                                   'instructions': fields.get('instructions'),
                                                   'dependsOnTask': item.get('dependsOn')},
              authorized=True)
    _append_passport_decision('schedule_once_created', admin_id, {'title': fields['title'], 'at': fields['at']})
    when = datetime.datetime.fromtimestamp(at_ms / 1000, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    if item.get('dependsOn'):
        return {'reply': f"Scheduled \"{fields['title']}\" to run once at {when}, after {item['dependsOn']} finishes."}
    return {'reply': f"Scheduled \"{fields['title']}\" to run once at {when}."}


async def _route_lane_spike(state, text, admin_id):
    import sim as _sim
    team_id = await asyncio.to_thread(_team_decider, state, text)
    room = await asyncio.to_thread(_classify_room_default, state, text)
    title = text[:120]
    queued = _sim.queue_spike(state, title, room, budget_ms=None, goal=text, player_authored=True)
    if not queued:
        return {'reply': "I couldn't queue that as an investigation -- try rephrasing it."}
    worker_id = None
    if team_id:
        worker_id = _pick_team_worker(state, team_id)
        if worker_id:
            state['workQueue'][-1]['assignedTo'] = worker_id
            state['workQueue'][-1]['directRoute'] = True
    save_state_to_db(state)
    log_action('player', 'spike_filed', {'title': title, 'room': room, 'teamId': team_id, 'assignedTo': worker_id},
              authorized=True)
    _append_passport_decision('spike_filed', team_id or admin_id, {'title': title, 'room': room})
    who = f" with {team_id}'s team" if team_id else ""
    return {'reply': f"On it -- I've queued a quick investigation on that{who}. I'll let you know what turns up."}


async def _route_lane_story(state, text, admin_id):
    # Filing against an unidentifiable team is worse than a director
    # answering directly -- fall back rather than guess.
    team_id = await asyncio.to_thread(_team_decider, state, text)
    if not team_id:
        return await _route_lane_unclear(state, text, admin_id)
    import sim as _sim
    team = next((t for t in (state.get('teams') or []) if t.get('id') == team_id), None)
    feature = (team or {}).get('name') or team_id
    issue = _sim.file_issue(state, team_id=team_id, issue_type='story',
                            summary=text[:500], feature=feature, reporter_id='player')
    if not issue:
        return await _route_lane_unclear(state, text, admin_id)
    save_state_to_db(state)
    # Reuses the existing 'issue_created' label/payload shape /api/intent/issues
    # already logs, with an added provenance field -- same convention, not a
    # parallel label.
    log_action('player', 'issue_created', {'key': issue['key'], 'type': 'story', 'teamId': team_id,
                                           'summary': issue['summary'][:200], 'origin': 'telegram_route'},
              authorized=True)
    _append_passport_decision('issue_created', 'player', {'key': issue['key'], 'teamId': team_id})
    team_name = (team or {}).get('name') or team_id
    return {'reply': f"Filed as {issue['key']} with {team_name} -- it'll go through their backlog grooming from here."}


async def _route_lane_incident(state, text, admin_id):
    # Never guess which product is broken -- an unattributed investigation
    # (spike) is safe, a mis-pinned incident is not.
    product_id = await asyncio.to_thread(_classify_product_default, state, text)
    if not product_id:
        return await _route_lane_spike(state, text, admin_id)
    import sim as _sim
    product = (state.get('products') or {}).get(product_id) or {}
    title = text[:120]
    queued = _sim.queue_bug(state, product_id, title, reported_by='player')
    if not queued:
        # Unroutable (no owning team, no on-call, or one already open) --
        # same safe fallback as an unidentifiable product.
        return await _route_lane_spike(state, text, admin_id)
    director_id = product.get('teamId')
    on_call = _sim.on_call_agent(state, director_id) if director_id else None
    on_call_name = ((state.get('agents') or {}).get(on_call) or {}).get('name') or on_call or 'someone'
    save_state_to_db(state)
    log_action('player', 'incident_filed', {'productId': product_id, 'title': title, 'assignedTo': on_call},
              authorized=True)
    _append_passport_decision('incident_filed', product_id, {'title': title})
    product_name = product.get('name') or product_id
    return {'reply': f"Flagged as an incident on {product_name} -- {on_call_name} is on it now."}


_ROUTING_HANDLERS = {
    'ask': _route_lane_ask,
    'schedule': _route_lane_schedule,
    'schedule_once': _route_lane_schedule_once,
    'spike': _route_lane_spike,
    'story': _route_lane_story,
    'incident': _route_lane_incident,
    'unclear': _route_lane_unclear,
}
