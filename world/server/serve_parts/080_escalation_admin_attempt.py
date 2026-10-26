

from core.review_decode import review_target


def _review_question(esc):
    q = (esc or {}).get('reviewQuestion')
    if q:
        return q
    return review_target((esc or {}).get('question') or '')['text']


def _escalation_admin_attempt(state, esc_id, esc, esc_kind, director_id, roster):
    """One chief-of-staff/admin attempt after the senior director gives up.

    Returns True only when the admin produced a valid, sufficiently confident
    approve/deny. A failed or malformed admin turn is not a decision: the
    escalation then falls through to the player exactly as before.
    """
    admin_id = _admin_agent_id(state)
    if not admin_id or admin_id == director_id:
        return False
    if esc.get('adminAskedAt'):
        return False
    esc['adminAskedAt'] = time.time()
    admin_name = (roster.get(admin_id) or {}).get('name', admin_id)
    instr = (
        f'You are {admin_name}, the chief of staff/admin of the AI think tank. '
        f'The senior director already tried {esc.get("askCount") or 0} time(s) '
        f'and could not decide this escalation. This is the admin pass before '
        f'the owner is asked. Kind: {esc.get("kind")}. '
        f'Question: {_review_question(esc)}. Approve only if clearly legitimate, '
        'in-scope, and safe. Deny if out of scope, unsafe, or clearly no. '
        'When genuinely unsure, leave it for the owner.'
    )
    criteria = {
        'approve': 'An ordinary, legitimate, in-scope request that should go ahead.',
        'deny': 'Out of scope, unsafe, insufficiently justified, or clearly should not happen.',
    }
    decision, confidence, _cost = _jev_quorum_choice_sync(instr, criteria)
    if decision not in ('approve', 'deny'):
        log_action(admin_id, 'escalation_admin_unsure',
                   {'escalationId': esc_id, 'kind': esc_kind,
                    'decision': decision, 'confidence': confidence},
                   authorized=False)
        return False
    composite = _jev_directory_score(esc_kind, confidence)
    floor = _escalation_floor(esc_kind)
    if composite < floor or confidence < _effective_safety_confidence():
        log_action(admin_id, 'escalation_admin_unsure',
                   {'escalationId': esc_id, 'kind': esc_kind,
                    'decision': decision, 'confidence': confidence,
                    'composite': round(composite, 3), 'floor': floor},
                   authorized=False)
        return False
    judge_model = _jev_judge_model()
    if judge_model:
        judge_decision, judge_conf, _judge_cost = _escalation_judge_crosscheck(
            instr, criteria)
        if judge_decision is None or judge_decision != decision:
            log_action(admin_id, 'escalation_admin_judge_refused',
                       {'escalationId': esc_id, 'kind': esc_kind,
                        'decision': decision, 'judge': judge_model,
                        'judge_decision': judge_decision,
                        'confidence': confidence,
                        'judge_confidence': judge_conf},
                       authorized=False)
            return False
    esc['status'] = 'approved' if decision == 'approve' else 'denied'
    esc['resolvedBy'] = f'{admin_name} ({admin_id})'
    esc['resolvedAt'] = time.time()
    if decision == 'approve' and esc_kind in ('allowlist candidate', 'allowlist request') and esc.get('note'):
        _grant_allowlist(esc['note'], actor=admin_id, via='admin escalation')
    log_action(admin_id, 'escalation_admin_' + ('approve' if decision == 'approve' else 'deny'),
               {'escalationId': esc_id, 'kind': esc_kind,
                'question': esc.get('question'), 'confidence': confidence,
                'composite': round(composite, 3), 'floor': floor},
               authorized=False)
    return True


def _mark_escalation_rung(esc, rung):
    if rung == 'immediate':
        esc['_immediateTried'] = True
    elif rung == 'senior':
        esc['_seniorTried'] = True
    elif rung == 'admin':
        esc['_adminTried'] = True
    esc['askCount'] = 0
    esc['lastAskedAt'] = 0


def _escalation_requester_director(state, esc):
    """The agent's own director, the first rung of the escalation chain."""
    agent_id = (esc or {}).get('agentId')
    if not agent_id:
        return None
    roster = {d.get('id'): d for d in ((state or {}).get('agentRoster') or [])}
    entry = roster.get(agent_id) or {}
    director = entry.get('director')
    if director and director != agent_id:
        return director
    return None


def _resolve_pending_escalations_sync():
    # Runs on the director loop (a thread). No pending escalation, no work.
    escalations = _load_escalations()
    pending = {i: e for i, e in escalations.items() if e.get('status') == 'pending'}
    if not pending:
        return 0
    state = get_state_from_db()
    senior_id = _senior_most_director_id(state)
    if not senior_id:
        return 0
    roster = {d['id']: d for d in (state.get('agentRoster', []) if state else [])}

    resolved = 0
    now = time.time()
    dirty = False
    for esc_id, esc in pending.items():
        esc_kind = esc.get('kind') or 'unknown'
        # Chain: the requester's own director, then the senior director, then
        # the chief of staff. `_immediateTried` is set when the first rung
        # gives up or cannot decide.
        immediate_id = _escalation_requester_director(state, esc)
        admin_id = _admin_agent_id(state)
        # One attempt per rung: the requester's own director, then the senior
        # director (only when distinct), then the chief of staff/admin. A
        # non-decision marks that rung tried and the NEXT pass asks the next
        # rung, instead of re-asking the same unsure reviewer for hours.
        reviewer_id, rung = None, None
        if immediate_id and not esc.get('_immediateTried'):
            reviewer_id, rung = immediate_id, 'immediate'
        elif senior_id and senior_id != immediate_id and not esc.get('_seniorTried'):
            reviewer_id, rung = senior_id, 'senior'
        elif admin_id and admin_id not in (immediate_id, senior_id) and not esc.get('_adminTried'):
            reviewer_id, rung = admin_id, 'admin'
        director_id = reviewer_id or senior_id
        director_def = roster.get(director_id, {}) if director_id else {}
        director_name = director_def.get('name', director_id or 'the director')
        # Escalations with a 1.0 risk floor are "never auto-approve" by design
        # (blocked command / blocked pipeline step) -- the director is not
        # permitted to resolve them, so don't burn a Jev call re-asking. They
        # wait for the human's email link.
        if _escalation_floor(esc_kind) >= 1.0:
            continue
        # Heterogeneous-judge drift circuit: a kind whose judge cross-check has
        # repeatedly failed closed is taken out of delegation entirely and waits
        # for the human's link. Notify once, immediately; do not spend a pass
        # marking rungs or re-asking a chain the judge gate no longer trusts.
        if _escalation_judge_drift(esc_kind) >= ESCALATION_JUDGE_DRIFT_CIRCUIT:
            if not esc.get('humanNotified'):
                esc['humanNotified'] = True
                notify_human_of_escalation(
                    esc_id, esc,
                    context='The director chain could not resolve this automatically '
                            '(the judge cross-check kept failing closed), so it is now yours.')
                dirty = True
            continue
        if rung is None:
            if not esc.get('humanNotified'):
                esc['humanNotified'] = True
                notify_human_of_escalation(
                    esc_id, esc,
                    context='The director, senior director, and chief of staff all '
                            'declined or could not decide this, so it is now yours.')
                dirty = True
            continue
        if rung == 'admin':
            if _escalation_admin_attempt(state, esc_id, esc, esc_kind,
                                         director_id, roster):
                dirty = True
                resolved += 1
                continue
            esc['_adminTried'] = True
            dirty = True
            if not esc.get('humanNotified'):
                esc['humanNotified'] = True
                notify_human_of_escalation(
                    esc_id, esc,
                    context='The director, senior director, and chief of staff all '
                            'declined or could not decide this, so it is now yours.')
            continue
        esc['lastAskedAt'] = now
        esc['askCount'] = (esc.get('askCount') or 0) + 1
        dirty = True
        instr = (
            f'You are {director_name}, the senior-most director of the AI think tank, '
            f'standing in for the owner on this request. The chief of staff has delegated '
            f'routine approval/denial to you. Decide the following escalation. '
            f'Kind: {esc.get("kind")}. Question: {_review_question(esc)}.\n'
            f'Approve if the request is clearly legitimate, in-scope, and safe for the think tank. '
            f'Deny if it is out of scope, unsafe, or the answer is clearly no. Favor denying '
            f'when genuinely unsure -- an unconvincing approval is the real risk.'
        )
        criteria = {
            'approve': 'An ordinary, legitimate, in-scope request that should go ahead.',
            'deny': 'Out of scope, unsafe, insufficiently justified, or clearly should not happen.',
        }
        # _jev_quorum_choice_sync never raises (fails closed to (None, 0.0, 0.0)
        # internally) -- an unreachable/non-binary classifier is caught by the
        # decision-not-in-(approve,deny) check right below, same as before.
        decision, confidence, _cost = _jev_quorum_choice_sync(instr, criteria)
        if decision not in ('approve', 'deny'):
            # A non-binary answer is a non-decision: mark this rung tried and
            # move to the next one on the next pass. Never auto-approve on a
            # classifier failure.
            _escalation_jev_errors.bump(esc_kind)
            _mark_escalation_rung(esc, rung)
            dirty = True
            continue
        # Composite multi-signal trust gate: confidence is just one signal. We
        # (a) blend in the per-kind Jev-error history (a model that's been
        # failing on this kind must be more confident to win the delegation) and
        # (b) require a per-KIND floor that the escalation's risk profile sets --
        # a safety-critical kind ("blocked command"/"blocked pipeline step") is
        # never auto-approved, and a genuinely-uncertain kind ("unsure safety
        # decision") needs a much stronger signal than a routine one.
        _escalation_jev_errors.reset(esc_kind)
        composite = _jev_directory_score(esc_kind, confidence)
        floor = _escalation_floor(esc_kind)
        if composite < floor or confidence < _effective_safety_confidence():
            # The director standing in for the chief of staff is the clearest case for
            # Jev's "escalate when unsure": a low-trust auto-approval here is
            # exactly the risk the delegation exists to avoid, so leave it for
            # the human rather than deciding on weak signal. Composite score and
            # the raw confidence are both logged so the audit shows *why*.
            log_action(director_id, 'escalation_unsure',
                       {'escalationId': esc_id, 'kind': esc_kind, 'question': esc.get('question'),
                        'confidence': confidence, 'composite': round(composite, 3), 'floor': floor,
                        'reason': 'below-kind-floor' if composite < floor else 'low-confidence'}, authorized=False)
            _mark_escalation_rung(esc, rung)
            dirty = True
            continue
        # Heterogeneous-judge cross-check: the director's auto-approval now has
        # to survive a SECOND, different decisions model independently agreeing
        # with the primary (self-evolve judges.py). Fail-closed by construction:
        #   - judge unavailable / non-binary / call failed -> human, bump drift;
        #   - judge disagrees with the primary -> human, bump drift (a
        #     confident-but-wrong primary must not win against a real objection);
        #   - both models answer the SAME way at high confidence WHILE this kind
        #     is already drifting -> suspected collusion ("confident but wrong
        #     together"), human, bump drift.
        judge_model = _jev_judge_model()
        if judge_model:
            judge_decision, judge_conf, _judge_cost = _escalation_judge_crosscheck(instr, criteria)
            if judge_decision is None:
                _bump_escalation_judge_drift(esc_kind)
                log_action(director_id, 'escalation_judge_unavailable',
                           {'escalationId': esc_id, 'kind': esc_kind, 'judge': judge_model,
                            'primary': decision, 'primary_confidence': confidence},
                           authorized=False)
                _mark_escalation_rung(esc, rung)
                dirty = True
                continue
            if judge_decision != decision:
                _bump_escalation_judge_drift(esc_kind)
                log_action(director_id, 'escalation_judge_disagree',
                           {'escalationId': esc_id, 'kind': esc_kind, 'judge': judge_model,
                            'primary': decision, 'judge_decision': judge_decision,
                            'primary_confidence': confidence, 'judge_confidence': judge_conf},
                           authorized=False)
                _mark_escalation_rung(esc, rung)
                dirty = True
                continue
            if (confidence >= ESCALATION_JUDGE_ALPHA_HIGH
                    and judge_conf >= ESCALATION_JUDGE_ALPHA_HIGH
                    and _escalation_judge_drift(esc_kind) > 0):
                _bump_escalation_judge_drift(esc_kind)
                log_action(director_id, 'escalation_judge_collusion',
                           {'escalationId': esc_id, 'kind': esc_kind, 'judge': judge_model,
                            'decision': decision, 'confidence': confidence, 'judge_confidence': judge_conf},
                           authorized=False)
                _mark_escalation_rung(esc, rung)
                dirty = True
                continue
        esc['status'] = 'approved' if decision == 'approve' else 'denied'
        esc['resolvedBy'] = f'{director_name} ({director_id})'
        esc['resolvedAt'] = time.time()
        bloom = decision == 'approve'
        # Side-effect for the delegable kinds that carry one: an approved
        # 'allowlist candidate' or 'allowlist request' applies its RUNTIME
        # allowlist grant, mirroring how the human's approve link mutates the
        # record. The note carries the hostname (set by record_browse_success
        # / the request endpoint); on deny nothing is granted.
        # Attribution names the director (_grant_allowlist logs it), so the
        # action log shows the grant came through delegation, not the human.
        if bloom and esc_kind in ('allowlist candidate', 'allowlist request') and esc.get('note'):
            _grant_allowlist(esc['note'], actor=director_id, via='director delegation')
        judge_note = {'judge': judge_model, 'judge_confidence': judge_conf} if judge_model else {}
        log_action(director_id, 'escalation_' + ('approve' if bloom else 'deny'),
                   {'escalationId': esc_id, 'kind': esc_kind, 'question': esc.get('question'),
                    'confidence': confidence, 'composite': round(composite, 3), 'floor': floor,
                    **judge_note}, authorized=False)
        resolved += 1
    if dirty:
        _save_escalations(escalations)
    return resolved


async def _director_approval_loop():
    while True:
        await asyncio.sleep(DIRECTOR_APPROVAL_INTERVAL_S)
        try:
            await asyncio.to_thread(_resolve_pending_escalations_sync)
        except Exception as e:
            print(f'[director] loop error: {e}', flush=True)


# --- autonomous peer help signals (issue #5, redesigned 2026-10-06) ---------
# Peers DO notice when a worker is out of line -- but the machine never writes
# a NEGATIVE review about a worker's productivity. The old design ranked
# workers by real-work counts and probabilistically filed "Underperforming"
# reports against whoever did the least, which would ding a legitimate
# bug-hunter who spent the window deep in one investigation. That conflated
# two different signals that never share a ledger:
#   underload/overload = a HELP signal ("get this person help or more work")
#   misconduct          = a REPORT (firing evidence)
# This pass watches the REAL action_log (real work vs. silence) on a cadence,
# and when the evidence supports it raises a HELP SIGNAL to the worker's
# DIRECTOR: a mailbox note + action_log entry + state['helpSignals'] record
# telling them to check in (quiet worker) or offer help/reallocation (standout
# carrying the load). No report is ever filed, no firing evidence is
# fabricated, and the reports ledger holds only genuine agent/player-written
# reviews. Nothing is deterministic: which worker is noticed, and whether a
# given watch raises a signal, are weighted probabilities, so the village reads
# naturally instead of firing the same fixed note on a fixed clock. Idempotent:
# a worker already signaled in this window isn't re-signaled until the next
# review.
PEER_REVIEW_INTERVAL_S = 90
PEER_REVIEW_MIN_LOOKBACK_S = 3600  # judge an hour of real activity, not 90 stray seconds
# Chance a peer watch that finds genuine divergence actually raises a signal.
# Evidence gates (below) decide WHO is worth signaling; this decides whether a
# given cadence raises anything at all, so signals arrive organically rather
# than every PEER_REVIEW_INTERVAL_S like clockwork.
PEER_REVIEW_FILE_PROBABILITY = 0.4
# A worker already signaled WITHIN this window is not re-signaled: the pass's
# job is to spread coverage across the roster, not hammer one worker.
# Signals are not reports (they don't feed firing review); a worker only
# re-enters the pool after this window elapses.
PEER_REVIEW_SIGNAL_STALE_S = 6 * 3600


def _peer_review_pass(state, now):
    """Peer help-signal writer, run on the caller's in-hand `state` object
    (mutated in place, no separate get/save of the whole blob). Returns the
    number of signals raised. This is the CORE of the loop -- see
    _peer_review_loop_pass (DB wrapper) and _peer_review_tick (cadence +
    single-writer driver).

    Redesigned 2026-10-06: the pass NEVER writes a report. A worker's
    productivity is already tracked structurally (approvedCount, droppedCount,
    morale, the firing drop-off signal) and needs no report to act on. All the
    pass does is watch the real action_log and, when the evidence shows a
    worker genuinely out of line (an underperformer or a standout), raise a
    HELP SIGNAL to that worker's DIRECTOR so help or reallocation can follow.
    A quiet worker under a real investigation (a bug-hunt, a long synthesis)
    is exactly the case the director should be told to CHECK IN on -- never to
    ding. No Jev, no senior director always authoring every signal."""
    roster = state.get('agentRoster', [])
    live = state.get('agents', {})
    reports = state.get('reports', [])
    if not isinstance(reports, list):
        reports = state['reports'] = []
    # Gather real activity from the action_log for every NON-director worker.
    cutoff = now - PEER_REVIEW_MIN_LOOKBACK_S  # action_log.ts is seconds (time.time())
    # A worker counts as "already covered" only if a help signal was raised for
    # them within the staleness window -- an old signal (or none) stops
    # blocking a fresh one.
    fresh_cutoff_ms = (now - PEER_REVIEW_SIGNAL_STALE_S) * 1000
    existing_signals = {s.get('aboutId') for s in state.get('helpSignals', [])
                        if s.get('ts', 0) >= fresh_cutoff_ms}
    candidates = []
    for d in roster:
        aid = d.get('id')
        if not aid:
            continue
        # Only workers -- skip admin and directors (peers watch workers,
        # exactly like the firing review does).
        if d.get('isAdmin') or d.get('isDirector') or _direct_reports(state, aid):
            continue
        if aid not in live:
            continue
        with _db() as conn:
            rows = conn.execute(
                'SELECT action, details, ts FROM action_log WHERE agent_id = ? AND ts >= ? ORDER BY ts DESC',
                (aid, cutoff),
            ).fetchall()
        total = len(rows)
        real = 0
        for action, _details, _ts in rows:
            if action in ('task_completed', 'handoff', 'library_promote', 'library_write', 'execute', 'browse', 'curl', 'report_received'):
                real += 1
        candidates.append({
            'id': aid, 'name': d.get('name', aid), 'role': d.get('role', ''),
            'actions': total, 'real': real, 'last': rows[0][2] if rows else None,
            'already': aid in existing_signals, 'director': d.get('director'),
        })
    if not candidates:
        return 0
    # EVIDENCE GATE (village): a help signal is a consequential claim about
    # another worker's load, so it must rest on solid evidence -- not a quiet
    # baseline. If NO ONE in the think tank has done any real work in the
    # lookback window, the whole village is simply idle: nobody is
    # underperforming (everyone is equally quiet) and nobody is a standout, so
    # there is nothing legitimate to signal. Raising a help signal against a
    # village where every worker is at zero fabricates evidence. So: no real
    # work anywhere => raise nothing.
    group_activity = max((c['real'] for c in candidates), default=0)
    if group_activity < 2:
        return 0
    # Dedup, not fallback: candidates already covered within the staleness
    # window are excluded, and an ALL-covered pool raises nothing.
    cand_pool = [c for c in candidates if not c['already']]
    if not cand_pool:
        return 0
    # EVIDENCE GATE (target): only a worker backed by a genuine, evidence-based
    # divergence qualifies -- either a clear underperformer (well behind while
    # the rest of the village demonstrably worked) or a clear standout (doing
    # far more real work than peers). A nominal "everyone performed about the
    # same" worker is NOT a signal: group_activity >= 2 is already guaranteed
    # by the village gate above, so this reduces to: the target is genuinely
    # behind (real < 2) or genuinely ahead (real >= 5, the group max).
    report_worthy = [
        c for c in cand_pool
        if c['real'] < 2 or (c['real'] >= 5 and c['real'] >= group_activity)
    ]
    if not report_worthy:
        return 0
    # PROBABILITY GATE: evidence says someone IS worth a signal, but a real
    # village doesn't raise one on a fixed clock -- a peer happens to notice,
    # or doesn't. This roll is what makes signals arrive organically instead of
    # every cadence like clockwork.
    if random.random() > PEER_REVIEW_FILE_PROBABILITY:
        return 0
    # Weighted random pick: the further out of line a worker is, the more
    # likely they are to be noticed -- but never a guaranteed pick, so the
    # village keeps some natural variance. Underperformers weight by how far
    # below the floor they are; standouts weight by how far above the pack.
    median_real = sorted(c['real'] for c in cand_pool)[len(cand_pool) // 2]
    weights = []
    for c in report_worthy:
        if c['real'] < 2:
            weights.append(max(1, 2 - c['real']))
        else:
            weights.append(max(1, c['real'] - median_real))
    target = random.choices(report_worthy, weights=weights, k=1)[0]
    # A help signal needs a DIRECTOR to act on it. A worker with no director on
    # record can't be helped by this pass -- skip (no admin stand-in: a senior
    # director flagging everyone is exactly the old single-author churn).
    director_id = target.get('director')
    if not director_id or director_id not in live:
        return 0
    direction = 'overloaded' if target['real'] >= 5 else 'quiet'
    if direction == 'overloaded':
        text = (f'Help signal: {target["name"]} ({target["role"]}) is carrying the load this '
                f'period -- {target["real"]} real actions. Check whether they need help or work '
                f'reallocation before they burn out.')
    else:
        text = (f'Help signal: {target["name"]} ({target["role"]}) has done little real work this '
                f'period ({target["real"]} real actions). Could be idle, or deep in a long '
                f'investigation -- check in before assuming anything.')
    signal = {
        'id': f'help-{int(now * 1000)}-{target["id"]}',
        'aboutId': target['id'], 'directorId': director_id,
        'direction': direction, 'real': target['real'], 'actions': target['actions'],
        'ts': int(now * 1000), 'text': text,
    }
    state.setdefault('helpSignals', []).append(signal)
    # In-world: the director is told, not a random peer. Best-effort -- a
    # mailbox failure must never block the signal itself.
    try:
        import sim as _sim
        _sim._deliver_mail(state, director_id, 'help_signal',
                           {'text': text, 'about': target['id']})
    except Exception:
        pass
    log_action(director_id, 'help_signal', {
        'about': target['id'], 'direction': direction,
        'real': target['real'], 'actions': target['actions'],
    }, authorized=False)
    return 1


def _peer_review_loop_pass(state=None, now=None):
    """DB-backed wrapper around _peer_review_pass: load the whole state, run
    the pass on it, persist. Kept for the peer-review tests (which drive it as
    a standalone read-modify-write). The LIVE path never calls this with
    state=None -- the sim loop owns the single read-modify-write and calls
    _peer_review_pass directly through _peer_review_tick, so no second thread
    ever does its own get/save of the whole blob (that race clobbered freshly
    filed reports before the staleness dedup could see them, re-filing the same
    worker every PEER_REVIEW_INTERVAL_S forever)."""
    if state is None:
        try:
            state = _state_begin()
            if not state:
                return 0
            n = _peer_review_pass(state, time.time() if now is None else now)
            save_state_to_db(state)
            return n
        finally:
            _state_abort()
    return _peer_review_pass(state, time.time() if now is None else now)


def _peer_review_tick(state):
    """Cadence + single-writer driver for the LIVE path. Called from inside the
    sim loop's one read-modify-write (see _sim_loop_pass in sim.py) on the same
    `state` object that is about to be saved, so the signal it raises can never
    be clobbered by a concurrent whole-blob save -- the exact race that made
    reports vanish and the loop re-file forever. Gated to the same cadence the
    old standalone thread used, so it fires ~once per PEER_REVIEW_INTERVAL_S,
    not on every 2s sim tick."""
    now = time.time()
    if now - (state.get('lastPeerReviewAt') or 0) < PEER_REVIEW_INTERVAL_S:
        return 0
    state['lastPeerReviewAt'] = now
    # Prune stale reports on the LIVE path so the report list can't accumulate
    # unbounded and drag every worker's firing signal down (stale reports never
    # decay out of the review pile otherwise). The 14-day strike window
    # (2026-10-06) means serious/severe EVIDENCE must survive long enough to
    # count as a strike -- pruning a serious report at 6h could silently erase
    # the first of two strikes spaced days apart. So: serious/severe reports
    # are retained for the full strike window; minor/major reports (which never
    # count as strikes) decay at the old staleness window. The pass's own
    # list contract (see the peer-review tests) is untouched -- this runs in
    # the single-writer driver, not in _peer_review_pass.
    stale_cutoff_ms = (now - PEER_REVIEW_SIGNAL_STALE_S) * 1000
    reports = state.get('reports')
    if isinstance(reports, list):
        import sim as _sim
        strike_cutoff_ms = (now * 1000) - _sim.REPORT_STRIKE_WINDOW_MS
        pruned = []
        for r in reports:
            ts = r.get('ts') or 0
            if r.get('severity') in ('serious', 'severe'):
                keep = ts >= strike_cutoff_ms
            else:
                keep = ts >= stale_cutoff_ms
            if keep:
                pruned.append(r)
        if len(pruned) != len(reports):
            state['reports'] = pruned
    return _peer_review_pass(state, now)



def _seed_watchlist_topics(state):
    """Seed researchTopics from the operator-edited world/watchlist.json on
    boot. Idempotent and additive: a topic already in state (by topic +
    startUrl) is never duplicated, so a topic the player deleted is not
    silently re-created (same policy as the long-horizon seed). Runs only in
    the real boot path (lifespan), never in tests. Entries that fail
    add_research_topic's own fail-closed validation (empty topic, non-absolute
    URL) are skipped. Returns the number of topics newly seeded."""
    import core.config as _config
    if not _config.RESEARCH_WATCHLIST:
        return 0
    import sim as _sim
    existing = {(t.get('topic'), t.get('startUrl')) for t in (state.get('researchTopics') or [])}
    seeded = 0
    for t in _config.RESEARCH_WATCHLIST:
        key = (t.get('topic'), t.get('startUrl'))
        if key in existing:
            continue
        record = _sim.add_research_topic(
            state, t.get('topic'), t.get('startUrl'), t.get('cadenceMs') or 0,
            link_keyword=t.get('linkKeyword'), page_keyword=t.get('pageKeyword'))
        if record is not None:
            existing.add(key)
            seeded += 1
    if seeded:
        log_action(None, 'watchlist_seeded', {'count': seeded, 'source': 'watchlist.json'})
    return seeded


# Sim-loop watchdog thresholds (seconds). Env SIM_WATCHDOG_*_S still
# overrides operator_policy.json when set (one escape hatch for operators who
# live in .env); otherwise the operator JSON / rebind hooks own these.
_SimWatchdogIntervalSEnv = _load_env().get('SIM_WATCHDOG_INTERVAL_S')
_SimWatchdogStaleSEnv = _load_env().get('SIM_WATCHDOG_STALE_S')
_SIM_WATCHDOG_INTERVAL_S = float(_SimWatchdogIntervalSEnv) if _SimWatchdogIntervalSEnv else 30.0
_SIM_WATCHDOG_STALE_S = float(_SimWatchdogStaleSEnv) if _SimWatchdogStaleSEnv else 180.0


# Operator policy rebind (world/operator_policy.json -> serve-owned scalars).
# Declared once here via config.bind_policy; applied at import and re-applied
# on every config.reload_policy_config() run. The two watchdog knobs keep
# their env fallbacks through overrides, which re-read serve's live globals
# so a patched/env-derived capture stays honored at each rebind.
def _watchdog_interval_s(policy):
    env = globals().get('_SimWatchdogIntervalSEnv')
    return float(env) if env else float(policy['simWatchdogIntervalS'])


def _watchdog_stale_s(policy):
    env = globals().get('_SimWatchdogStaleSEnv')
    return float(env) if env else float(policy['simWatchdogStaleS'])


def _agent_dir_sync_interval_s(policy):
    env = globals().get('_AGENT_DIR_SYNC_ENV_S')
    return float(env) if env else float(policy['agentDirSyncIntervalS'])


bind_policy(__name__, [
    ('SUCCESS_GRADE_FLOOR', 'successGradeFloor'),
    ('PEER_REVIEW_INTERVAL_S', 'peerReviewIntervalS'),
    ('PEER_REVIEW_FILE_PROBABILITY', 'peerReviewFileProbability'),
    # Phase 3 externalization (2026-10-18): the remaining serve statics
    # moved into operator_policy.json. Every loop that consumes an interval
    # reads it per iteration, so a reload takes effect on the next sleep.
    ('DB_BACKUP_KEEP', 'dbBackupKeep'),
    ('DB_BACKUP_INTERVAL_S', 'dbBackupIntervalS'),
    ('LOG_RETENTION_DAYS_DEFAULT', 'logRetentionDaysDefault'),
    ('LOG_PRUNE_INTERVAL_S', 'logPruneIntervalS'),
    ('LOG_PRUNE_MAX_ROWS', 'logPruneMaxRows'),
    ('MODEL_TIER_REFRESH_INTERVAL_S', 'modelTierRefreshIntervalS'),
    ('TIER_PRICE_WATCH_INTERVAL_S', 'tierPriceWatchIntervalS'),
    ('TIER_RECHECK_COOLDOWN_S', 'tierRecheckCooldownS'),
    ('TIER_GATE_MIN_CONFIDENCE', 'tierGateMinConfidence'),
    ('FAILURE_MAX_RECORDS', 'failureMaxRecords'),
    ('SUCCESS_MAX_RECORDS', 'successMaxRecords'),
    ('RULE_PROPOSALS_MAX', 'ruleProposalsMax'),
    ('SUCCESS_PROPOSALS_MAX', 'successProposalsMax'),
    ('RULE_MIN_RECURRENCE', 'ruleMinRecurrence'),
    ('SUCCESS_MIN_RECURRENCE', 'successMinRecurrence'),
    ('MEMORY_MAX_AGE_DAYS', 'memoryMaxAgeDays'),
    ('MEMORY_DECAY_FACTOR', 'memoryDecayFactor'),
    ('MEMORY_KEEP_COUNT', 'memoryKeepCount'),
    ('MAX_DOWNLOAD_BYTES', 'maxDownloadBytes'),
    ('YOUTUBE_TRANSCRIPT_MAX_CHARS', 'youtubeTranscriptMaxChars'),
    ('YOUTUBE_TRANSCRIPT_MAX_SEGMENTS', 'youtubeTranscriptMaxSegments'),
    ('AGENT_DIR_SYNC_INTERVAL_S', 'agentDirSyncIntervalS'),
    ('PEER_REVIEW_MIN_LOOKBACK_S', 'peerReviewMinLookbackS'),
    ('PEER_REVIEW_SIGNAL_STALE_S', 'peerReviewSignalStaleS'),
    ('DIRECTOR_ESCALATION_REASK_COOLDOWN_S',
     'directorEscalationReaskCooldownS'),
    ('DIRECTOR_ESCALATION_MAX_ASKS', 'directorEscalationMaxAsks'),
    ('DELEGATE_MAX_OPEN', 'delegateMaxOpen'),
    ('DELEGATE_BUDGET_CAP_USD', 'delegateBudgetCapUsd'),
    ('SUPERVISOR_BUDGET_CAP_USD', 'supervisorBudgetCapUsd'),
    ('_SIM_WATCHDOG_INTERVAL_S', 'simWatchdogIntervalS'),
    ('_SIM_WATCHDOG_STALE_S', 'simWatchdogStaleS'),
    ('CHAT_MAX_TOKENS_CAP', 'chatMaxTokensCap'),
    ('SMS_TTL_DEFAULT_S', 'smsTtlDefaultS'),
    ('SMS_TTL_MIN_S', 'smsTtlMinS'),
    ('SMS_TTL_MAX_S', 'smsTtlMaxS'),
    ('DECIDE_BURST_ALLOW', 'decideBurstAllow'),
    ('DECIDE_BURST_WINDOW_S', 'decideBurstWindowS'),
    ('DECIDE_MIN_INTERVAL_S', 'decideMinIntervalS'),
    ('HEALTH_CHECK_INTERVAL_S', 'healthCheckIntervalS'),
    ('HEALTH_ALERT_DEDUP_WINDOW_S', 'healthAlertDedupWindowS'),
    ('HEALTH_DIGEST_INTERVAL_S', 'healthDigestIntervalS'),
    ('AVATAR_TASK_INTERVAL_S', 'avatarTaskIntervalS'),
    ('AVATAR_TASK_BATCH_MAX', 'avatarTaskBatchMax'),
    ('AVATAR_TASK_RETRY_COOLDOWN_S', 'avatarTaskRetryCooldownS'),
    ('AVATAR_GENERATION_MIN_CREDITS_USD', 'avatarGenerationMinCreditsUsd'),
], overrides={
    '_SIM_WATCHDOG_INTERVAL_S': _watchdog_interval_s,
    '_SIM_WATCHDOG_STALE_S': _watchdog_stale_s,
    'AGENT_DIR_SYNC_INTERVAL_S': _agent_dir_sync_interval_s,
})


# Sim-loop failure escalation ladder (R6). Counts consecutive watchdog failure
# events since the last healthy tick. The in-tank ladder (senior director, then
# the chief of staff once restarts >= 2) runs in sim._deliver_watchdog_event; the PLAYER
# is told only when nothing in the think tank can resolve the outage -- i.e.
# a restart attempt itself failed, or the streak reaches
# _SIM_WATCHDOG_PLAYER_ALERT_AFTER (repeated restarts that never restore a
# healthy tick). One Telegram per streak.
_SIM_WATCHDOG_RESTARTS = 0
_SIM_WATCHDOG_PLAYER_ALERTED = False
_SIM_WATCHDOG_LAST_EVENT_AT = 0.0
_SIM_WATCHDOG_PLAYER_ALERT_AFTER = 3


def _sim_watchdog_note_failure(kind, detail, restarted):
    """Record one watchdog failure on the escalation ladder.

    Stamps sim._SIM_WATCHDOG_EVENT -- the side channel _sim_loop_pass consumes
    inside its single read-modify-write; this function runs on the watchdog
    thread and must NEVER touch the DB. Bumps the streak counter, then decides
    the player alert: immediate when the restart attempt itself failed, else at
    _SIM_WATCHDOG_PLAYER_ALERT_AFTER consecutive failures (best-effort
    Telegram, one per streak). Synchronous and side-effect-light so tests can
    drive it directly. Returns the streak count."""
    global _SIM_WATCHDOG_RESTARTS, _SIM_WATCHDOG_PLAYER_ALERTED
    global _SIM_WATCHDOG_LAST_EVENT_AT
    _SIM_WATCHDOG_RESTARTS += 1
    _SIM_WATCHDOG_LAST_EVENT_AT = time.time()
    evt_kind = kind if restarted else f'{kind}_restart_failed'
    evt = {
        'kind': evt_kind,
        'detail': detail,
        'restarts': _SIM_WATCHDOG_RESTARTS,
        'at': _SIM_WATCHDOG_LAST_EVENT_AT,
        'player_alert': False,
    }
    try:
        import sim as _sim_module
        _sim_module._SIM_WATCHDOG_EVENT = evt
    except Exception as e:
        print(f'[sim-watchdog] failed to stamp sim event: {e}', flush=True)
    should_alert = ((not restarted)
                    or _SIM_WATCHDOG_RESTARTS >= _SIM_WATCHDOG_PLAYER_ALERT_AFTER)
    if should_alert and not _SIM_WATCHDOG_PLAYER_ALERTED:
        _SIM_WATCHDOG_PLAYER_ALERTED = True
        evt['player_alert'] = True  # same dict reference as the stamp above
        body = (f'The simulation loop is down ({evt_kind}: {detail}) after '
                f'{_SIM_WATCHDOG_RESTARTS} consecutive failure(s). Nothing in '
                f'the think tank can resolve this -- check the server logs.')
        print(f'[sim-watchdog] player alert: {body}', flush=True)
        try:
            from integrations.notify import send_player_telegram_sync
            send_player_telegram_sync('[AI Think Tank] sim loop down', body)
        except Exception as e:
            print(f'[sim-watchdog] player alert send failed: {e}', flush=True)
    return _SIM_WATCHDOG_RESTARTS


def _sim_watchdog_note_healthy(last_tick_epoch):
    """Reset the escalation ladder once a successful tick lands at/after the
    last stamped failure event (wall-clock seconds, same clock as
    sim._LAST_TICK_WALL_EPOCH). A tick older than the failure keeps the
    streak."""
    global _SIM_WATCHDOG_RESTARTS, _SIM_WATCHDOG_PLAYER_ALERTED
    if (last_tick_epoch >= _SIM_WATCHDOG_LAST_EVENT_AT
            and (_SIM_WATCHDOG_RESTARTS or _SIM_WATCHDOG_PLAYER_ALERTED)):
        _SIM_WATCHDOG_RESTARTS = 0
        _SIM_WATCHDOG_PLAYER_ALERTED = False


async def _sim_watchdog_loop(sim_holder, interval_s=None, stale_s=None):
    """Supervise the sim loop task; recreate it if it dies or its tick freezes.

    2026-10-13: the live village froze mid-sprint with every executor worker
    idle, CPU ~1%, and NO error logged. The sim task had died silently --
    `except Exception` in _sim_loop missed whatever BaseException escaped, and
    the lifespan (the only holder of the task reference) never awaits it, so
    Python's "Task exception was never retrieved" warning never fired. The
    loop itself now catches BaseException; this watchdog is the second layer:
    if the task object is done, or a successful tick has not landed for
    `stale_s` wall-clock seconds while the sim is not dormant (dormancy
    intentionally runs no ticks), cancel/recycle the task and log loudly.

    `sim_holder` is the lifespan-owned dict  {'task': Task|None,
    'shutting_down': bool}  so restarts are visible to teardown and shutdown
    stops the recycle loop before cancelling the final task.
    """
    interval_s = _SIM_WATCHDOG_INTERVAL_S if interval_s is None else interval_s
    stale_s = _SIM_WATCHDOG_STALE_S if stale_s is None else stale_s
    while True:
        await asyncio.sleep(interval_s)
        try:
            if sim_holder.get('shutting_down'):
                return
            task = sim_holder.get('task')
            if task is None or task.done():
                detail = 'missing'
                if task is not None:
                    if task.cancelled():
                        detail = 'cancelled'
                    else:
                        try:
                            detail = f'exception {task.exception()!r}'
                        except BaseException as e:
                            detail = f'exception lookup failed {e!r}'
                print(f'[sim-watchdog] sim loop dead ({detail}); restarting', flush=True)
                restarted = False
                try:
                    import sim as _sim_module
                    sim_holder['task'] = asyncio.create_task(_sim_module._sim_loop())
                    restarted = True
                except Exception as e:
                    print(f'[sim-watchdog] restart failed: {e}', flush=True)
                try:
                    _sim_watchdog_note_failure('dead', detail, restarted)
                except Exception as e:
                    print(f'[sim-watchdog] note failure error: {e}', flush=True)
                continue
            # Alive: check tick staleness via the pass's wall-clock stamp.
            # Skip while dormant -- _sim_loop_pass is intentionally idle then.
            try:
                import sim as _sim_module
                last = float(getattr(_sim_module, '_LAST_TICK_WALL_EPOCH', 0.0) or 0.0)
            except Exception:
                last = 0.0
            # A tick at/after the last failure means the loop recovered: reset
            # the escalation ladder (consecutive-failure count + alert latch).
            try:
                _sim_watchdog_note_healthy(last)
            except Exception as e:
                print(f'[sim-watchdog] healthy reset error: {e}', flush=True)
            if last and not _dormant():
                gap = time.time() - last
                if gap > stale_s:
                    print(f'[sim-watchdog] tick stale {gap:.0f}s (> {stale_s:.0f}s); recycling sim loop', flush=True)
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        pass
                    restarted = False
                    try:
                        sim_holder['task'] = asyncio.create_task(_sim_module._sim_loop())
                        restarted = True
                    except Exception as e:
                        print(f'[sim-watchdog] restart failed: {e}', flush=True)
                    try:
                        _sim_watchdog_note_failure(
                            'stale', f'no successful tick for {gap:.0f}s', restarted)
                    except Exception as e:
                        print(f'[sim-watchdog] note failure error: {e}', flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f'[sim-watchdog] error: {e}', flush=True)


def _ensure_runtime_schemas():
    """Create module-owned tables on every boot path. init_db() only runs under
    the __main__ path, not `uvicorn serve:app`."""
    with _db() as conn:
        tank_accounts.ensure_schema(conn)
        captcha.ensure_schema(conn)
        import server.migrations as _migrations
        _migrations.apply(conn)
