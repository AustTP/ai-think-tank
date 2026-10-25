

def set_charter(state, goal, interests=None, notes='', now_ms=None):
    """The player's charter -- the tank-level SPINE (the article's rule 2: one
    direction every lane points at). Player-owned: the player decides, the tank
    plans against it. `goal` is the north-star statement; `interests` are the
    free-form threads (crafts, topics, rooms) that goal is built from; `notes`
    is anything else the directors should weigh. Stores on state['charter'] and
    returns the record, or None when no goal is given (a vague goal gets a
    vague plan -- the tank never invents a spine for you)."""
    goal = (goal or '').strip()
    if not goal:
        return None
    interests = [str(i).strip() for i in (interests or []) if str(i).strip()]
    charter = {
        'goal': goal,
        'interests': interests,
        'notes': (notes or '').strip(),
        'updatedAt': int(time.time() * 1000) if now_ms is None else int(now_ms),
    }
    state['charter'] = charter
    return charter


def get_charter(state):
    """The player's charter record, or None (a fresh tank has no spine until
    the player sets one -- the tank never fabricates a direction)."""
    ch = state.get('charter')
    if not isinstance(ch, dict) or not (ch.get('goal') or '').strip():
        return None
    return ch


def _charter_alignment(state, room):
    """Is `room` aligned with the charter's spine? True when the charter's goal
    or interests name any of the room's craft keywords (see _ROOM_KEYWORDS) --
    the article's rule 2 made real: fragmented interests that share a spine
    stop competing. Returns True/False, or None when no charter exists."""
    ch = get_charter(state)
    if not ch:
        return None
    text = ' '.join([ch.get('goal') or '', ' '.join(ch.get('interests') or [])]).lower()
    if not text:
        return False  # pragma: no cover -- join always leaves a space
    return any(k in text for k in (_ROOM_KEYWORDS.get(room) or []))


def _charter_note(state):
    """One line the tank prepends to every task's instructions: the spine the
    work points at. Returns None when no charter exists."""
    ch = get_charter(state)
    if not ch:
        return None
    return f"Tank charter: {ch['goal']}"


def _roadmap_step(state, now_ms):
    """Weekly silent recompute: director-owned roadmap maps each room to a
    priority derived from trailing grades + recent delivery. A room that shipped
    poorly (low grade) or that agents keep flagging gets higher priority so fresh
    work lands where it's weakest. Pure derivation -- no ceremony, no Jev spend.

    The spine: when the player has set a charter, a room aligned with it gets a
    priority boost -- and a FRESH room (no grade, no delivery yet) defaults to
    aligned>0 instead of 0, so the charter decides a new tank's direction
    instead of every room starting flat. The charter aligns, the grades steer."""
    if now_ms - (state.get('lastRoadmapReviewAt') or 0) < ROADMAP_CADENCE_MS:
        return
    state['lastRoadmapReviewAt'] = now_ms
    roadmap = state.setdefault('roadmap', {})
    for room in VALUED_QUEUE_ROOMS:
        gm = _room_trailing_grade(state, room)
        demand = _roadmap_release_demand(state, room)
        aligned = _charter_alignment(state, room)
        prev = roadmap.get(room) or {}
        if gm is None and demand == 0:
            # No history yet -- leave whatever the director set, else default.
            # The charter decides a fresh room's starting priority: aligned
            # rooms get a baseline so the spine (not the void) picks direction.
            if not prev:
                roadmap[room] = {'priority': 1 if aligned else 0,
                                 'ownerId': prev.get('ownerId'),
                                 'lastGrade': None, 'demand': 0,
                                 'charterAligned': aligned}
            continue
        # Weak grade and/or low delivery -> higher priority to rebalance.
        priority = 0
        if gm is not None and gm < DELIVERABLE_GRADE_FLOOR:
            priority += 2
        if demand == 0:
            priority += 1           # starved room -- feed it
        elif gm is not None and gm < 7:
            priority += 1
        if aligned:
            priority += 1           # on the spine -- keep its thread alive
        roadmap[room] = {'priority': priority, 'ownerId': prev.get('ownerId'),
                         'lastGrade': gm, 'demand': demand,
                         'charterAligned': aligned}


def _consensus_relay_step(state, now_ms):
    """Weekly silent recompute of the hive-mind consensus relay (auto-co-meta's
    'read consensus -> act -> update -> repeat'). One small shared state note
    the next cycle reads FIRST (_augment_task_instructions prepends it) and
    that this step updates LAST, so a think tank's direction survives between
    cycles and across restarts (state is persisted). Derives the current
    consensus from the roadmap the director just recomputed: the highest-
    priority room and why it is weak/starved. Pure derivation -- no ceremony,
    no Jev spend. A no-op until a roadmap exists, so a fresh think tank never
    fabricates a consensus that isn't grounded."""
    roadmap = state.get('roadmap') or {}
    ranked = sorted(((roadmap[r].get('priority') or 0, r) for r in roadmap),
                    reverse=True)
    if not ranked:
        return
    priority, room = ranked[0]
    gm = _room_trailing_grade(state, room)
    reasons = []
    if gm is not None and gm < DELIVERABLE_GRADE_FLOOR:
        reasons.append(f'weak trailing grade ({gm}/10)')
    if _roadmap_release_demand(state, room) == 0:
        reasons.append('no recent delivery')
    why = ', '.join(reasons) if reasons else 'prioritized by the director'
    # The spine (when the player set one): the charter goal leads the consensus
    # so the direction every task acts on names the GOAL, not just the weakest
    # room. "point them at one thing, and they all turn into one direction."
    charter = get_charter(state)
    lead = f"Current direction: {charter['goal']}. " if charter else ''
    state['consensusRelay'] = {
        'room': room,
        'consensus': f"{lead}Focus next work on {room} ({why}).",
        'updatedAt': now_ms,
    }


def _consensus_relay_note(state):
    """Read the consensus relay FIRST -- the one-line 'current direction' the
    next cycle acts on. Returns None when no consensus has been derived yet."""
    relay = state.get('consensusRelay')
    if not relay:
        return None
    return relay.get('consensus')


def _rule_mine_step(state, now_ms):
    """Weekly silent pass: mine recurring classified failures (the review
    ledger in serve.py) into operator rule proposals -- the 'write a rule' +
    'add a test' steps of the draft-review loop. Mirrors _roadmap_step: pure
    derivation, no ceremony, no Jev spend. Cadence-gated on the state stamp;
    the mining itself lives in serve.py (file-backed ledger + proposals).
    Nothing is auto-applied -- a proposal surfaces to the operator (governance
    log + the /api/rule-proposals surface) and becomes a real rule only when
    the operator encodes it. Best-effort: a ledger failure just skips this
    week's mine, never blocks the task cycle."""
    if now_ms - (state.get('lastRuleMineAt') or 0) < RULE_MINE_CADENCE_MS:
        return
    state['lastRuleMineAt'] = now_ms
    try:
        import serve as _serve_mod
        created = _serve_mod._mine_rule_proposals()
    except Exception:
        return
    for p in created:
        _log_governance(state, None, 'rule_proposal', {
            'proposalId': p.get('id'),
            'type': p.get('type'),
            'rule': p.get('rule'),
            'count': p.get('count'),
            'dedupeKey': p.get('dedupeKey'),
        })


def _archive_distill_step(state, now_ms):
    """Weekly silent pass: fold the ARCHIVED decision tape (kept permanently --
    see serve._prune_logs's decision_archive table) into a short 'decision
    archive' wiki page, so the think tank can reason over what it has decided as
    a body instead of pruning the memory away. Pure derivation + one wiki write
    (server-authority path, actor 'distill'), no ceremony, no Jev spend.
    Cadence-gated like rule mining; best-effort -- a DB or write failure just
    skips the week."""
    if now_ms - (state.get('lastArchiveDistillAt') or 0) < RULE_MINE_CADENCE_MS:
        return
    state['lastArchiveDistillAt'] = now_ms
    try:
        import serve as _serve_mod
        since_ms = state.get('lastArchiveDistillWindowAt') or (now_ms - RULE_MINE_CADENCE_MS)
        summary = _serve_mod._decision_archive_summary(since_ms / 1000.0)
        if not summary or not summary.get('total'):
            return
        state['lastArchiveDistillWindowAt'] = now_ms
        week_label = datetime.datetime.utcfromtimestamp(now_ms / 1000.0).strftime('%Y-%m-%d')
        section = [f'## {week_label}', '']
        for k in summary.get('kinds') or []:
            section.append(f"- {k['kind']}: {k['count']} decision(s), {k['ok']} ok, ${k['cost']:.4f}")
        section.append(f"- total: {summary.get('total')} decision(s), {summary.get('failed') or 0} failed, ${summary.get('cost') or 0.0:.4f}")
        # Read the existing page (if any) and keep only the last 8 weekly
        # sections so the archive page stays bounded as the years accumulate.
        existing = _serve_mod._wiki_page_body('decision-archive', 'think_tank') or ''
        body = '\n'.join(section) + '\n'
        if existing.strip():
            kept = existing.split('\n## ')[:8]
            body = '\n## '.join(kept).strip('\n') + '\n\n' + body
        _serve_mod._write_wiki_server('decision-archive', 'Decision Archive',
                                      'think_tank', body)
    except Exception:
        return


def _success_mine_step(state, now_ms):
    """Weekly silent pass: mine recurring high-graded deliverables (the success
    ledger in serve.py) into operator success-lesson proposals -- the positive
    half of the rule-mining loop, so 'agents get smarter' by propagating what
    WORKED, not just by avoiding what failed (ChatDev ECL: acquisition,
    utilization, propagation, elimination). Same shape as _rule_mine_step:
    pure derivation, no ceremony, no Jev spend, cadence-gated on the state
    stamp. Nothing is auto-applied -- a proposal surfaces to the operator
    (governance log + the /api/success-proposals surface) and becomes a real
    standard only when the operator encodes it. Best-effort: a ledger failure
    just skips this week's mine, never blocks the task cycle."""
    if now_ms - (state.get('lastSuccessMineAt') or 0) < RULE_MINE_CADENCE_MS:
        return
    state['lastSuccessMineAt'] = now_ms
    try:
        import serve as _serve_mod
        created = _serve_mod._mine_success_proposals()
    except Exception:
        return
    for p in created:
        _log_governance(state, None, 'success_proposal', {
            'proposalId': p.get('id'),
            'room': p.get('room'),
            'lesson': p.get('lesson'),
            'count': p.get('count'),
            'dedupeKey': p.get('dedupeKey'),
        })


def _refinement_context_for_room(state, room):
    """The roadmap context the refinement groom should weigh: the room's current
    roadmap priority + trailing grade, so a scrum master grooms high-priority /
    weak rooms preferentially. A charter (the spine) is weighed too -- an
    aligned room is worth grooming in, an off-spine one is easier to park.
    Returns a short string or None."""
    roadmap = (state.get('roadmap') or {}).get(room) or {}
    gm = _room_trailing_grade(state, room)
    bits = []
    if roadmap.get('priority'):
        bits.append(f"roadmap priority {roadmap['priority']}")
    if gm is not None:
        bits.append(f"trailing grade {gm}/10")
    aligned = _charter_alignment(state, room)
    if aligned is not None:
        bits.append('charter aligned' if aligned else 'charter: off-spine')
    return ('; '.join(bits)) or None


# ---------------------------------------------------------------------------
# Cut 3: on-call escalation. The on-call agent tried to restore a
# broken product and COULDN'T (assignment-abandoned, or the bug stayed open past
# the restore window). Instead of the incident dying silently, the owning team's
# scrum master grooms it into the backlog: a STORY when the root cause is scoped
# enough to card, a SPIKE when it isn't. Composes with the Cut-1 refinement
# ceremony (stories go through the same backlogRequests pipeline the SM owns).
# ---------------------------------------------------------------------------

def _escalation_decider_default(state, instructions, product_id, title):
    """Default Jev resolver for the on-call escalation groom: is this broken
    product's failure scoped enough to card a STORY, or is the root cause
    unknown enough that we need a SPIKE investigation first? Returns
    'story' | 'spike', or None on a Jev outage (-> spike fallback)."""
    try:
        import serve
    except Exception:
        return None
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': {
                            'story': "The failing behavior is understood and scoped -- a team can card a concrete story to restore it.",
                            'spike': "The root cause is unknown / not yet reproducible -- file an investigation spike before any story can be scoped."}}})
        choice, _, _ = serve._jev_choice(data)
        if choice in ('story', 'spike'):
            return choice
    except Exception:
        pass
    return None


_escalation_decider = _escalation_decider_default


def _escalation_scrum_master_for(state, director_id):
    """The scrum master of an incident's owning team, or None. The SM is who
    owns story creation (Cut 1), so the escalation works THROUGH them -- we
    never bypass the SM to push a card in ourselves."""
    for t in (state.get('teams') or []):
        if t.get('directorId') == director_id and t.get('scrumMasterId'):
            return t['scrumMasterId']
    return None


def _escalated_product_pending(state, product_id):
    """Whether a product already has an in-flight/unresolved escalation for the
    same open incident -- dedup so a prolonged outage doesn't fire a new
    escalation every restore-window tick."""
    pend = state.get('_pendingEscalation')
    if pend and pend.get('productId') == product_id:
        return True
    return product_id in (state.get('_escalatedProducts') or {})


def _start_escalation(state, product_id, director_id, title, source, now_ms):
    """File a pending-escalation record for the owning team's scrum master to
    groom on the next pass. Cheap + idempotent (dedup by product while the
    incident stays unresolved). Returns the scrum master id, or None if the
    team has no SM yet (nothing to escalate to)."""
    if _escalated_product_pending(state, product_id):
        return None
    sm = _escalation_scrum_master_for(state, director_id)
    if not sm:
        return None
    # Cap concurrent escalations per owning team before opening a new one.
    open_count = len([p for p in (state.get('_escalatedProducts') or {}).values()
                      if p.get('directorId') == director_id and p.get('resolved') is not True])
    if open_count >= team_policy(state, director_id, 'escalationMaxOpen'):
        return None
    state['_pendingEscalation'] = {
        'at': now_ms, 'embarked': False, 'scrumMasterId': sm,
        'directorId': director_id, 'productId': product_id,
        'title': title, 'source': source,
    }
    state.setdefault('_escalatedProducts', {})[product_id] = {
        'at': now_ms, 'directorId': director_id, 'resolved': False,
    }
    return sm


def _escalation_convener(state, product_id, scrum_master_id):
    """The escalation ceremony's attendee: the scrum master alone (this is a
    groom decision, not a meeting). Must be free + on-duty to convene; a
    busy/off-duty SM defers to the next pass."""
    a = (state.get('agents') or {}).get(scrum_master_id)
    if not a or a.get('busy') or a.get('offDuty'):
        return None
    return [scrum_master_id]


def _embark_escalation(state, scrum_master_id, now_ms):
    """Convene the escalation: snapshot the SM's prior state, mark them busy at
    the Command Center, schedule the resolve. Mirrors _start_refinement."""
    ids = _escalation_convener(state, None, scrum_master_id)
    if not ids:
        return False
    a = (state.get('agents') or {}).get(scrum_master_id)
    pend = state['_pendingEscalation']
    pend['people'] = {scrum_master_id: {k: a.get(k) for k in
        ('offDuty', 'visible', 'x', 'y', 'dir', 'task', 'busy', 'inRoom',
         'pairWith', 'handoff', 'workUntil')}}
    a['busy'] = True; a['visible'] = True; a['offDuty'] = False
    a['task'] = None; a['inRoom'] = 'commandcenter'; a['dir'] = 'south'
    a['roomX'] = 315; a['roomY'] = 155
    pend['embarked'] = True
    pend['at'] = now_ms + ESCALATION_MEET_MS
    _log_governance(state, scrum_master_id, 'oncall_escalation',
                    {'action': 'groom_start', 'product': pend.get('productId'),
                     'source': pend.get('source')})
    return True


def _resolve_escalation(state, pending, now_ms, decider=None):
    """When the escalation window elapses, the scrum master grooms the incident:
    injectable decider picks story|spike. A story is filed as a backlogRequests
    record (origin:'oncall_escalation') for the SM's OWN refinement ceremony to
    card -- never bypassing Cut-1 story creation. A spike is queued directly
    (taskType:'spike', non-gated lane). On outage, default to a spike. Restores
    the SM's prior state, records the outcome, clears the pending record."""
    decider = decider or _escalation_decider
    scrum_master_id = pending.get('scrumMasterId')
    sm_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == scrum_master_id), {})
    sm_name = sm_def.get('name') or scrum_master_id
    product_id = pending.get('productId')
    title = pending.get('title') or 'broken product'
    instructions = (f"{sm_name} is the scrum master for an on-call incident on "
                    f"the {product_id} product ('{title}' -- the on-call agent could "
                    f"not restore it: {pending.get('source')}). Groom it into the backlog: "
                    f"is the failing behavior scoped enough to card a STORY, or is the "
                    f"root cause unknown (file a SPIKE investigation first)?")
    choice = decider(state, instructions, product_id, title)
    if choice not in ('story', 'spike'):
        choice = 'spike'  # outage / unknown -> an investigation card is safer than a guess
    if choice == 'story':
        # File a backlog WORK-REQUEST the SM's refinement ceremony will card.
        # The SM is the filer so per-team grooming keeps it in the right team.
        req_id = f"esc-{int(time.time() * 1000)}"
        (state.setdefault('backlogRequests', [])).append({
            'id': req_id, 'filedBy': scrum_master_id, 'title': title,
            'room': 'pressoffice', 'reason': f"On-call could not restore {product_id}",
            'filedAt': now_ms, 'status': 'pending', 'origin': 'oncall_escalation',
            'productId': product_id,
        })
        outcome = 'story'
    else:
        queue_work(state, [{
            'title': f"SPIKE: root cause of '{title}' in {product_id} unknown",
            'room': 'pressoffice',
            'goal': f"Investigate the {product_id} failure the on-call agent could not restore",
            'instructions': (f"Filed by the {sm_name} scrum-master escalation for {product_id}: "
                             f"the on-call could not restore '{title}' ({pending.get('source')}). "
                             f"Spike to find the root cause."),
            'taskType': 'spike',
        }])
        outcome = 'spike'
    # The incident is now back in the pipeline (as a backlog item or a spike),
    # so this escalation is resolved; clear before restoring the SM so a fresh
    # restore failure can file a NEW escalation later.
    _escalatedProducts = state.get('_escalatedProducts') or {}
    record = _escalatedProducts.get(product_id)
    if record:
        record['resolved'] = True
        record['outcome'] = outcome
    state['_pendingEscalation'] = None
    _restore_escalation_agent(state, scrum_master_id, (pending.get('people') or {}).get(scrum_master_id), now_ms)
    _log_escalation_digest(state, pending, outcome)
    _log_governance(state, scrum_master_id, 'oncall_escalation',
                    {'action': 'groom_resolve', 'product': product_id, 'outcome': outcome})


def _restore_escalation_agent(state, aid, snap, now_ms):
    """Return the scrum master to their pre-escalation state. A mid-task SM
    resumes the same task with its work budget extended; an off-duty one returns
    off-duty (vanished). Mirrors _restore_refinement_agent."""
    agents = state.get('agents') or {}
    a = agents.get(aid)
    if not a or not snap:
        return
    a['dir'] = snap.get('dir'); a['x'] = snap.get('x'); a['y'] = snap.get('y')
    if snap.get('offDuty'):
        a['offDuty'] = True; a['visible'] = False
        a['task'] = None; a['busy'] = False; a['inRoom'] = None
        return
    if snap.get('task'):
        t = (state.get('tasks') or {}).get(snap['task'])
        a['task'] = snap['task']; a['busy'] = True; a['inRoom'] = snap.get('inRoom')
        a['visible'] = snap.get('visible')
        if t and t.get('status') in ('walking', 'working'):
            t['workUntil'] = (t.get('workUntil') or 0) + (ESCALATION_MEET_MS // 1000)
        return
    a['task'] = None; a['busy'] = False; a['inRoom'] = None
    a['visible'] = snap.get('visible'); a['offDuty'] = snap.get('offDuty')


def _log_escalation_digest(state, pending, outcome):
    """Write a short digest of an on-call escalation to library/escalations/ --
    the SM, the product, why it escalated, and the filed story/spike. Matches
    the refinement/social digest precedent (best-effort, never load-bearing)."""
    try:
        import serve
        serve.log_escalation_digest(state, pending, outcome)
    except Exception:
        pass


def _escalate_oncall_failure(state, product_id, director_id, title, source, now_ms):
    """Shared entry point for BOTH triggers: file a pending escalation to the
    product's owning team SM. Returns the SM id or None (no SM / capped / dup).
    Called from the assignment-abandonment hook and the unrestored sweep."""
    sm = _start_escalation(state, product_id, director_id, title, source, now_ms)
    return sm


def _escalation_step(state, now, now_ms, decider=None):
    """Ungated server-owned pass (like refinement/social/roadmap): (a) advance
    an in-flight escalation ceremony, (b) sweep open incident tasks older than
    the restore window and escalate the ones no one restored. Deterministic,
    state-only. Called from _task_cycle. `decider` injectable for tests
    (mirrors _refinement_step's threading)."""
    # (a) Advance the single in-flight escalation: embark then resolve.
    pend = state.get('_pendingEscalation')
    if pend is not None:
        if not pend.get('embarked'):
            _embark_escalation(state, pend.get('scrumMasterId'), now_ms)
        else:
            _resolve_escalation(state, pend, now_ms, decider=decider)
        return
    # (b) Sweep: any open incident task past its restore window escalates once.
    timeout = RESTORE_TIMEOUT_MS
    for tid, t in (state.get('tasks') or {}).items():
        if t.get('taskType') != 'bug':
            continue
        if t.get('status') not in ('walking', 'working'):
            continue
        # Active age (paused/dormant time excluded) so a bug is not escalated
        # just because the tank slept through its restore window.
        age_ms = _task_active_age_ms(state, t, now_ms)
        if age_ms is None or age_ms < timeout:
            continue
        product_id = t.get('productId')
        if not product_id:
            continue
        # Resolve the owning team -> SM via the product.
        director_id = _product_director(state, product_id)
        if not director_id:
            continue
        _escalate_oncall_failure(state, product_id, director_id,
                                 t.get('title') or 'unrestored incident',
                                 'unrestored', now_ms)


def _product_director(state, product_id):
    """Owning team (director id) for a product, or None. Mirrors queue_bug's
    product.teamId lookup so the timeout sweep routes like the initial report."""
    prod = (state.get('products') or {}).get(product_id)
    if not prod:
        return None
    return prod.get('teamId')


def _governance_pass(state, now=None, now_ms=None, grid=None, decider=None):
    """Phase 4: ONE cadence pass over server-owned governance. Fires an auto-
    hire (hiring.js attemptAutoHire + finishHire) and an auto-firing review
    (firing.js attemptAutoFiringReview + finishFiringReview), gated on the
    same idle rule as everything else: no work, no governance, no Jev spend.
    Mutates `state` only; pure. Called from _task_cycle so it shares the
    single read-modify-write. `decider` injectable (defaults to Jev)."""
    sim = state.get('sim') or {}
    if sim.get('owner') != 'server':
        return state
    now = time.time() if now is None else now
    now_ms = int(now * 1000) if now_ms is None else now_ms
    # Idle-quiet: both browser loops bailed early on !thinkTankHasWork() and only
    # made their Jev call after. Mirror that so an idle think tank spends nothing.
    if not think_tank_has_work(state, now_ms):
        return state
    decider = decider or _governance_decider
    if grid is None:
        grid, _ = _load_outdoor_geometry()

    # All-time name reserve: backfill once from the roster so every pre-existing
    # name (including any a browser-client hired before this shipped) is
    # reserved forever, not only while that agent is employed.
    if not state.get('_usedNames'):
        for _d in (state.get('agentRoster') or []):
            _remember_name(state, _d.get('name'))

    # --- Auto-hire: complete a pending hire whose duration elapsed, then start
    # a new one if eligible.
    pending = state.get('_pendingHire')
    if pending and now_ms >= pending['at']:
        _complete_auto_hire(state, pending, grid, now_ms)
        pending = None
    if state.get('_pendingHire') is None:
        if now_ms - (state.get('lastHireAt') or 0) >= HIRE_COOLDOWN_MS:
            _start_auto_hire(state, now_ms, grid, decider)

    # --- Phase B onboard ceremony: a fresh hire with a _pendingOnboard has its
    # Town-Hall meeting start (director + coworkers convene at Command Center)
    # if not already underway; once the meet duration elapses, stage the new
    # agent's AGENT.md progressively until final. A multi-agent team spawn
    # queues behind the single live ceremony.
    onboard = state.get('_pendingOnboard')
    if onboard is None:
        queued = state.get('_onboardQueue') or []
        if queued:
            onboard = queued.pop(0)
            state['_pendingOnboard'] = onboard
            if queued:
                state['_onboardQueue'] = queued
            else:
                state.pop('_onboardQueue', None)
    if onboard is not None:
        if not onboard.get('embarked'):
            _start_onboard_meeting(state, onboard, now_ms)
        elif now_ms >= onboard.get('at', 0):
            _resolve_onboard_meeting(state, onboard, now_ms)

    # --- Auto-firing review: resolve a pending review whose duration elapsed,
    # then start a new one if due.
    pending_review = state.get('_pendingFiringReview')
    if pending_review and now_ms >= pending_review['at']:
        _resolve_firing_review(state, pending_review, now_ms, decider)
        pending_review = None
    if state.get('_pendingFiringReview') is None:
        if now_ms - (state.get('lastFiringReviewAt') or 0) >= FIRING_COOLDOWN_MS:
            _start_auto_firing_review(state, now_ms, decider)

    return state


def _start_auto_firing_review(state, now_ms, decider):
    """firing.js attemptAutoFiringReview: BOTH approvers (admin + senior-most
    director) must exist, be on-duty, and be free; then a real review starts --
    reviewers convene at Command Center and the candidate is looked at
    FIRING_REVIEW_DURATION_MS later by _resolve_firing_review."""
    reviewers = _firing_reviewers(state)
    if len(reviewers) < 2:
        return False
    agents = state.get('agents') or {}
    r1 = agents.get(reviewers[0]['id'])
    r2 = agents.get(reviewers[1]['id'])
    if not r1 or not r2 or r1.get('busy') or r2.get('busy') or r1.get('offDuty') or r2.get('offDuty'):
        return False
    candidate = who_needs_review(state, now_ms)
    if not candidate:
        return False
    state['lastFiringReviewAt'] = now_ms
    for r in (r1, r2):
        r['busy'] = True
        r['visible'] = False
        r['inRoom'] = 'commandcenter'
        r['dir'] = 'south'
    r1['roomX'] = 315
    r1['roomY'] = 155
    r2['roomX'] = 485
    r2['roomY'] = 155
    state['_pendingFiringReview'] = {
        'reviewer1Id': reviewers[0]['id'], 'reviewer2Id': reviewers[1]['id'],
        'candidateId': candidate.get('id'), 'at': now_ms + FIRING_REVIEW_DURATION_MS,
    }
    return True


def _resolve_firing_review(state, pending, now_ms, decider=None):
    """firing.js finishFiringReview: the Jev fire/keep verdict, the anti-
    premature consultation guard, and the delete-or-keep outcome. Idempotent
    on a candidate that vanished or became busy mid-review."""
    agents = state.get('agents') or {}
    reviewer1 = agents.get(pending['reviewer1Id'])
    reviewer2 = agents.get(pending['reviewer2Id'])
    for r in (reviewer1, reviewer2):
        if r:
            r['busy'] = False
            r['visible'] = True
            r['inRoom'] = None
    candidate = agents.get(pending['candidateId'])
    if not candidate:
        state.pop('_pendingFiringReview', None)
        return
    reviewers = [next((d for d in (state.get('agentRoster') or []) if d.get('id') == pending['reviewer1Id']), {}),
                 next((d for d in (state.get('agentRoster') or []) if d.get('id') == pending['reviewer2Id']), {})]
    candidate_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == pending['candidateId']), None)
    if not candidate_def:
        state.pop('_pendingFiringReview', None)
        return
    decision, morale = _fire_decision(state, reviewers, candidate_def, now_ms, decider)
    if decision == 'fire':
        blocks, info = _consultation_blocks_firing(state, pending['candidateId'], now_ms)
        if blocks:
            # Deferred for consultation -- don't mark reviewed so a real change
            # (collaboration ends, evidence grows) re-opens it.
            candidate['lastFiringReview'] = None
            reviewer1_id = pending['reviewer1Id']
            _log_governance(state, reviewer1_id, 'firing_review',
                            {'about': pending['candidateId'], 'decision': 'deferred_for_consultation',
                             'morale': morale,
                             'reviewers': [pending['reviewer1Id'], pending['reviewer2Id']],
                             'consulted': {'reporters': [r['id'] for r in info['reporters']],
                                           'coworkers': [c['id'] for c in info['coworkers']]}})
            state.pop('_pendingFiringReview', None)
            return
        # Same idle guard re-checked after the Jev await: don't delete someone
        # who got picked up by a task/pair/handoff while the review ran.
        if candidate.get('busy') or candidate.get('task') or candidate.get('pairWith') or candidate.get('handoff'):
            _log_governance(state, pending['reviewer1Id'], 'firing_review',
                            {'about': pending['candidateId'], 'decision': 'deferred',
                             'reason': 'candidate became busy mid-review'})
            state.pop('_pendingFiringReview', None)
            return
        # Fire: remove from both the live map and the roster.
        _remember_name(state, candidate_def.get('name'))
        agents.pop(pending['candidateId'], None)
        roster = state.get('agentRoster') or []
        state['agentRoster'] = [d for d in roster if d.get('id') != pending['candidateId']]
        # Durable blocklist: a fired employee can never reenter the think tank.
        # The id (and the name, via _usedNames) is reserved forever and the hire
        # gate refuses it, so no ceremony, generative name chooser, or re-seed
        # can bring the same person back under a new cover.
        state.setdefault('firedAgents', {})[pending['candidateId']] = {
            'name': candidate_def.get('name'),
            'role': candidate_def.get('role'),
            'at': now_ms,
            'firedBy': pending['reviewer1Id'],
            'reviewers': [pending['reviewer1Id'], pending['reviewer2Id']],
        }
        # Revoke every standing credential the fired agent held (capability
        # handles + attribution key + temp grants) -- a fired agent must lose
        # all of it, not just its roster entry.
        _revoke_agent_credentials(pending['candidateId'])
        _log_governance(state, pending['reviewer1Id'], 'firing_review',
                        {'about': pending['candidateId'], 'decision': 'fire', 'morale': morale,
                         'reviewers': [pending['reviewer1Id'], pending['reviewer2Id']]})
    else:
        # Keep: record the verdict on the agent so _review_is_stale skips re-
        # litigating this exact evidence until morale/report count move.
        candidate['lastFiringReview'] = {
            'morale': morale,
            'reportCount': len(reports_about(state, pending['candidateId'])),
            'verdict': 'keep',
            'at': now_ms,
        }
        _log_governance(state, pending['reviewer1Id'], 'firing_review',
                        {'about': pending['candidateId'], 'decision': 'keep', 'morale': morale,
                         'reviewers': [pending['reviewer1Id'], pending['reviewer2Id']]})
    state.pop('_pendingFiringReview', None)


def _log_governance(state, agent_id, action, details):
    """Append to the durable state's activity log, the same place the browser's
    logThinkTankAction wrote. No DB here -- state mutation only."""
    try:
        import serve
        serve.log_action(agent_id, action, details, authorized=True)
    except Exception:
        # Couldn't log (e.g. removing the logging agent); governance still
        # proceeds -- logging is best-effort, not load-bearing.
        pass


def _task_owner_director(state, task):
    """The director who owns the team working `task`, for budget-exhausted
    escalation. Candidate chain: the task's teamId -> its productId (via
    _product_director) -> the assigned agent's roster `director` pointer.
    Returns a director id (as state['agents'] keys it) or None."""
    if task.get('teamId'):
        team = _team_row(state, task['teamId'])
        if team:
            return team.get('directorId') or team.get('id')
    if task.get('productId'):
        director = _product_director(state, task.get('productId'))
        if director:
            return director
    roster = next((d for d in (state.get('agentRoster') or [])
                   if d.get('id') == task.get('assignedTo')), None)
    if roster and roster.get('director'):
        return roster['director']
    return None


def _notify_task_budget_exhausted(state, task, spent=None, attempts=None,
                                  analysis=None, completed=None):
    """One-time director notification when a task's model-spend ceiling is hit
    (see the budgetExhausted content result). Fires once per task (stamped
    `_budgetExhaustedNotified`); the owning director gets a NON-DISRUPTIVE
    mailbox note (kind task_budget_exhausted, no wake -- the card is already
    closed, waking her to act would be churn) + a governance log row carrying
    the diagnostic (budget / actual spend / attempt count / author / overrun
    cause / work landed / future-budget factor) so she can investigate and
    re-open the card with more budget. `analysis` and `completed` (budget.py)
    may be passed in from the budgetExhausted branch or are derived here.
    Pure state mutation."""
    if task.get('_budgetExhaustedNotified'):
        return
    task['_budgetExhaustedNotified'] = True
    director_id = _task_owner_director(state, task)
    title = task.get('title') or 'a task'
    budget = task.get('budgetUsd')
    budget_line = f" of ${budget:.2f}" if isinstance(budget, (int, float)) else ''
    spent_line = f" after spending ${spent:.4f}" if isinstance(spent, (int, float)) else ''
    attempts_line = f" across {int(attempts)} attempt(s)" if isinstance(attempts, (int, float)) else ''
    # Wave 3: the cause analysis, what had landed, and the
    # future-budget factor (already including this overrun, since
    # record_overrun ran first) ride along, so the director can
    # decide grant-more-budget vs close without a separate
    # investigation.
    cause_line = (f" Cause: {analysis.get('detail')}."
                  if analysis and analysis.get('detail') else '')
    if completed:
        work_line = (f" Work landed before the stop: {completed.get('verdict')}."
                     if completed.get('landed')
                     else ' No work landed before the stop.')
    else:
        work_line = ''
    try:
        import core.budget as _budget
        factor = _budget.overrun_factor(
            state, room=task.get('room'), team_id=task.get('teamId'),
            product_id=task.get('productId'), task_type=task.get('taskType'))
    except Exception:
        factor = 1.0
    factor_line = (f" Similar future cards now budget {factor:.2f}x "
                   f"their type default." if factor > 1.0 else '')
    text = (f'The task "{title}" exhausted its model-spend budget{budget_line}'
            f"{spent_line}{attempts_line} and was closed as failed."
            f"{cause_line}{work_line}{factor_line} Investigate the "
            f'task and either grant it more budget and re-open it, or close the work.')
    director = (state.get('agents') or {}).get(director_id) if director_id else None
    if director:
        _append_mailbox(director, {
            'kind': 'task_budget_exhausted',
            'taskId': task.get('id'),
            'title': title,
            'text': text,
            'reason': (analysis or {}).get('reason'),
            'overrunRatio': (analysis or {}).get('overrunRatio'),
            'perCallUsd': (analysis or {}).get('perCallUsd'),
            'workLanded': (completed or {}).get('landed'),
            'futureFactor': factor,
        })
    _log_governance(state, director_id or task.get('assignedTo'),
                    'task_budget_exhausted',
                    {'taskId': task.get('id'), 'title': title[:120],
                     'budgetUsd': budget, 'spentUsd': spent,
                     'attempts': attempts, 'assignedTo': task.get('assignedTo'),
                     'reason': (analysis or {}).get('reason'),
                     'overrunRatio': (analysis or {}).get('overrunRatio'),
                     'perCallUsd': (analysis or {}).get('perCallUsd'),
                     'workLanded': (completed or {}).get('landed'),
                     'futureFactor': factor})
    # A mailbox note alone is not a decision. Put a high-priority supervisor
    # card in front of the owning director so the overrun is investigated and
    # either funded or closed. Default is to grant when the spend bought
    # progress and no blocking issue exists; the grant tool enforces the
    # ceiling and reopens the failed card.
    if not task.get('_budgetReviewQueued') and director_id:
        task['_budgetReviewQueued'] = True
        spent_num = float(spent or 0.0)
        ceiling_num = float(budget or 0.0)
        suggested = max(spent_num * 2.0, ceiling_num * 1.5, 0.05)
        suggested = round(suggested, 2)
        queue_work(state, [{
            'title': f"[BUDGET] {task.get('id') or 'card'}: decide more budget or close",
            'room': 'pressoffice',
            'taskType': 'supervise',
            'assignedTo': director_id,
            'teamId': task.get('teamId'),
            'priority': 'high',
            'budgetUsd': SUPERVISOR_CARD_BUDGET_USD,
            'sizeEstimate': 'S',
            'instructions': (
                f"Budget review for card {task.get('id')} ({task.get('title')}). "
                f"It hit its cap of ${float(task.get('budgetUsd') or 0):.2f} "
                f"after ${spent_num:.4f} across {int(attempts or 0)} attempt(s). "
                f"Cause: {(analysis or {}).get('detail') or (analysis or {}).get('reason') or 'unknown'}. "
                f"Work landed before the stop: {(completed or {}).get('verdict') if completed and completed.get('landed') else 'none'}. "
                "Investigate why it went over. Default to granting more budget when the spend bought "
                "progress and no blocking issue exists: call supervisor_grant_budget with this task id "
                f"and a ceiling above ${spent_num:.4f} (a reasonable start is ${suggested:.2f}). "
                "If a real issue prevents completion, do not grant more; say what blocks it and direct "
                "a new card or close the work with that reason. Do not do the work yourself."
            ),
        }])


def _director_grant_budget_reopen(state, task_id, new_budget_usd, director_id=None, now_ms=None):
    """Item 4 runtime override: a director grants a budget-exhausted card more
    model-spend money and re-opens the work. Refuses unless the card is actually
    budget-failed (status 'failed' + budgetExhausted) and the new ceiling is a
    real raise past what was already spent -- otherwise the /api/chat gate would
    still refuse the very next call. Re-queues the card (same title/goal/room/
    product/instructions, pinned to its original author) carrying the raised
    budgetUsd, so the assignment loop works it again under the new ceiling. The
    failed record is kept and stamped with the raised budget (the audit trail);
    the re-opened card is a fresh task under the new budget. Pure state
    mutation; returns {'ok': ...}."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    tasks = state.get('tasks') or {}
    task = tasks.get(task_id) if task_id else None
    if not isinstance(task, dict):
        return {'ok': False, 'error': 'unknown task'}
    if task.get('status') != 'failed' or not task.get('budgetExhausted'):
        return {'ok': False, 'error': 'only a budget-exhausted (failed) card can be re-opened with more budget'}
    if not isinstance(new_budget_usd, (int, float)) or new_budget_usd <= 0:
        return {'ok': False, 'error': 'new budget must be a positive USD amount'}
    import serve as _serve_mod
    spent, attempts = _serve_mod._task_budget_spent(task_id)
    if float(new_budget_usd) <= spent:
        return {'ok': False,
                'error': f'new budget ${float(new_budget_usd):.4f} does not exceed the ${spent:.4f} already spent on this task -- the gate would refuse the next call anyway'}
    budget = float(new_budget_usd)
    # Audit trail on the failed record + the authoritative ceiling for the
    # re-opened card (queue_work whitelists budgetUsd onto the assigned task).
    task['budgetUsd'] = budget
    task['budgetGrantedAt'] = now_ms
    task['budgetGrantedBy'] = director_id
    fail_note = (task.get('failNote') or '').strip()
    queue_work(state, [{
        'title': task.get('title') or 'Re-opened task',
        'room': task.get('room'),
        'instructions': (f'This card was re-opened by {director_id or "the director"} with more model-spend budget '
                         f'(${budget:.2f} total) after exhausting its previous ceiling.'
                         + (f' Previous failure: {fail_note}' if fail_note else '')
                         + (f'\n\n{task.get("instructions")}' if task.get('instructions') else '')),
        'goal': task.get('projectLabel') or task.get('goal'),
        'projectLabel': task.get('projectLabel') or task.get('title'),
        'productId': task.get('productId'),
        'taskType': task.get('taskType') or 'code',
        'assignedTo': task.get('assignedTo'),  # pin back to the original author
        'budgetUsd': budget,
        'budgetBand': task.get('budgetBand') or 'standard',
        'userStory': task.get('userStory'),
        'acceptanceCriteria': task.get('acceptanceCriteria'),
        'never': task.get('never') or None,
    }])
    _log_governance(state, director_id or task.get('assignedTo'), 'task_budget_granted',
                    {'taskId': task_id, 'title': (task.get('title') or '')[:120],
                     'newBudgetUsd': budget, 'spentUsd': spent, 'attempts': attempts})
    return {'ok': True, 'taskId': task_id, 'budgetUsd': budget,
            'spentUsd': spent, 'attempts': attempts}


def _revoke_agent_credentials(agent_id):
    """Best-effort revocation of EVERY standing credential a fired agent held:
    the external-capability handles (Phase D), the per-agent attribution secret
    (agent_keys -- else a fired agent could keep signing requests it was never
    re-authorized for), and any temporary capability grants (temp_access_grants).
    Mirrors _log_governance's lazy serve import so sim.py stays unit-testable
    offline; a DB failure must never block the fire itself."""
    try:
        import serve
        serve.revoke_agent_credentials(agent_id)
    except Exception:
        pass


def _on_work_item_abandoned(state, pick, now_ms):
    """Trigger 1 for the Cut-3 escalation: a work item was permanently abandoned
    (assignment-attempt cap reached). Remember every abandoned card so the next
    supervisor pass can see what fell out of the queue. If it's an incident/bug,
    hand the failed restore to the product's owning team scrum master so it
    becomes a backlog story/spike instead of dying silently."""
    try:
        team_id = pick.get('teamId')
        if not team_id:
            try:
                director_id = _task_owner_director(state, pick)
                team_id = director_id
            except Exception:
                team_id = None
        state.setdefault('_abandonedCards', []).append({
            'title': (pick.get('title') or 'untitled')[:160],
            'room': pick.get('room'),
            'taskType': pick.get('taskType'),
            'teamId': team_id,
            'attempts': int(pick.get('attempts') or 0),
            'at': int(now_ms or time.time() * 1000),
        })
        state['_abandonedCards'] = state['_abandonedCards'][-100:]
    except Exception:
        pass
    if not (pick.get('taskType') == 'bug' or pick.get('incident')):
        return
    product_id = pick.get('productId')
    if not product_id:
        return
    director_id = _product_director(state, product_id)
    if not director_id:
        return
    _escalate_oncall_failure(state, product_id, director_id,
                             pick.get('title') or 'abandoned incident',
                             'assignment_abandoned', now_ms)
