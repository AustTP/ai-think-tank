

# ---------------------------------------------------------------------------
# Director split (multi-team large requests). When a large ask lands on a
# director who leads a subdirector layer, the DIRECTOR reviews the ask FIRST
# and decides how it partitions across their org -- one piece per subteam,
# pieces for their own remaining pool, pieces left SHARED for whichever
# subteam can pick them up first, and (when the org needs capacity the roster
# doesn't have) the founding of a NEW subdirector from an existing worker.
# Each piece becomes its own child large-request for the chosen subteam's
# normal breakdown ceremony -- the proven carding path, unchanged -- with
# splitFrom/splitBy provenance back to the parent ask.
#
# Founding is approval-gated, never unilateral below the root. A ROOT team
# director may found a subdirector inside their own split review (the admin
# already stands behind them -- same standing the promote endpoint grants).
# A SUBDIRECTOR may only PROPOSE one: the proposal is held in
# pendingFoundations and the founder's PARENT director convenes an approval
# review (parent + proposing subdirector at the Command Center), where the
# parent judges necessity. Only an approval executes the founding; a denial
# (or an outage -- deny is the conservative default) files the held pieces
# inside the proposer's own subteam instead. Founding also never spawns
# agents -- it hands existing workers over -- so the manager count can only
# grow by consuming worker capacity, and the per-director subdirector
# ceiling still applies at every level.
#
# Same ceremony shape as every other meeting (embark on one pass, resolve on
# the next; decider injectable for tests; snapshot/restore for attendees).
# Priority is context AND intake order: an urgent ask can be split for
# parallel subteams now, while a slow-burn ask may stay one low-priority
# piece built over time. On a model outage the fallback is the CURRENT
# behavior -- the whole goal files as a normal breakdown request for the
# director's own team -- so a split outage costs nothing but a delay; the ask
# is never dropped.
#
# Villages: subteams live in villages (agents inherit their director's
# villageId). The split prompt names each candidate team's village so the
# director chooses a far team consciously, and every child request records
# the receiving village. Rooms are shared geometry today, so assignment does
# not care; cross-village WALKING is the mobility layer's future work.
# ---------------------------------------------------------------------------

_SPLIT_MEET_MS = 8 * 60 * 1000  # one in-sim workday block, like a breakdown
_FOUND_MEET_MS = 4 * 60 * 1000  # the approval review is a shorter desk meeting

_PRIORITY_NAMES = ('low', 'normal', 'high', 'urgent')


def _split_director_for_request(state, req):
    """The leader whose review owns a split-pending request: the receiving
    team's director -- or the SUBDIRECTOR the piece was routed to (their
    emergent subteam has no teams row, hence _team_row_for). Shared requests
    have no team and never await a split."""
    team_id = req.get('teamId')
    if not team_id:
        return None
    return (_team_row_for(state, team_id) or {}).get('directorId') or None


def _pending_split_requests(state, director_id):
    """Pending breakdown requests awaiting THIS leader's split review."""
    return [r for r in (state.get('backlogRequests') or [])
            if r.get('status') == 'pending' and r.get('breakdown')
            and r.get('awaitingSplit')
            and _split_director_for_request(state, r) == director_id]


def _split_org_summary(state, director_id):
    """The review context: the director's own pool and each subdirector's
    subteam (members, load, village, COMMITMENT). Load = busy/working members
    right now; village = the agents' inherited villageId; commitment = the
    team's standing obligations -- an ACTIVE sprint (the committed work a
    subteam cannot start until it closes) or a permanent dedication named on
    the team row. The director weighs this whole picture -- capacity,
    locality, urgency, and what each subteam is already TIED to -- when
    partitioning: routing an urgent piece to a committed subteam just parks
    it until the commitment ends."""
    agents = state.get('agents') or {}
    teams = {t.get('id'): t for t in (state.get('teams') or [])}

    def _commitment(team_id):
        active = [sid for sid, s in (state.get('sprints') or {}).items()
                  if s.get('status') == 'active' and team_id in (s.get('teamIds') or [])]
        row = teams.get(team_id) or {}
        return {'sprint': active[0] if active else None,
                'dedicatedTo': (row.get('dedicatedTo') or None)}

    own_team_id = next((tid for tid, t in teams.items()
                        if t.get('directorId') == director_id), director_id)
    # The reviewer's own commitment resolves at the ROOT team: a subdirector
    # reviewer sits inside the root team's committed sprint pool, so the
    # review sees the same "IN ACTIVE SPRINT" flag their parent would.
    own_root = governance.team_director_of(state, director_id) or own_team_id
    own = _commitment(own_root)
    org = [{'teamId': own_team_id, 'leader': director_id,
            'leaderName': (agents.get(director_id) or {}).get('name') or director_id,
            'village': village_of_agent(state, director_id),
            'members': governance.direct_reports(state, director_id),
            'sprint': own['sprint'], 'dedicatedTo': own['dedicatedTo']}]
    if governance.is_village_chief(state, director_id):
        # A village chief reviews at the SENIOR tier: their org is the
        # village's senior directors' teams (each holds a teams row and
        # reports to the chief), not a subdirector layer. Subdirectors_of
        # deliberately excludes team directors, so the chief sources their
        # org from the teams rows themselves.
        subs = [t.get('directorId') for t in (state.get('teams') or [])
                if isinstance(t, dict) and t.get('directorId')
                and (({r.get('id'): r for r in (state.get('agentRoster') or [])
                       if isinstance(r, dict)}.get(t.get('directorId')) or {})
                      .get('director')) == director_id]
    else:
        subs = governance.subdirectors_of(state, director_id)
    for sid in subs:
        members = governance.direct_reports(state, sid)
        busy = sum(1 for m in members
                   if (agents.get(m) or {}).get('busy')
                   or (agents.get(m) or {}).get('task'))
        # A subteam inherits its root team's commitment: sprint work is the
        # ROOT team's committed pool and every subteam inside it draws on the
        # same workers, so the review resolves the subteam's root team
        # (walking the reporting chain) and reads THAT team's obligations.
        root_id = governance.team_director_of(state, sid) or sid
        commit = _commitment(root_id)
        org.append({'teamId': sid,
                    'leader': sid,
                    'leaderName': (agents.get(sid) or {}).get('name') or sid,
                    'village': village_of_agent(state, sid),
                    'members': members, 'busy': busy,
                    'sprint': commit['sprint'],
                    'dedicatedTo': commit['dedicatedTo']})
    return org


def _split_decider_default(state, instructions, goal):
    """Default resolver for the director's split review: a high-tier (JEV-
    gated) chat call returns the partition. Returns the parsed dict, or None
    on a model outage (the resolve then falls back to the single-team path --
    the ask is never dropped)."""
    try:
        import serve
    except Exception:
        return None
    try:
        model = serve._resolve_model_tier(
            f'Deciding how a large request splits across subteams: {goal[:200]}',
            allow_high=True)
        if not model:
            return None
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': instructions},
             {'role': 'user', 'content': f"Review and partition this large request: {goal}"}],
            max_tokens=serve._big_task_max_tokens)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__director_splits__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        start = reply.find('{')
        end = reply.rfind('}')
        if start != -1 and end > start:
            reply = reply[start:end + 1]
        parsed = json.loads(reply)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return None


# Injectable for tests (mirrors _breakdown_decider); the live loop uses the
# high-tier default. The split NEVER blocks the staffing reply -- the ask is
# filed pending and partitioned on a later pass.
_director_split_decider = _split_decider_default


def _split_instructions(state, req, org, director_name, founding_mode='immediate',
                        director_id=None):
    """The director's review prompt: the ask, its urgency, the player's pins,
    and the org's subteams with their loads and villages. The output contract
    is pieces (+ optional new-subdirector foundings); every field except the
    ask is optional and malformed pieces fail open to the director's own
    team -- a bad plan must never drop the work."""
    pc = req.get('playerConstraints') or {}
    goal = req.get('goal') or req.get('title') or 'untitled large request'
    prio = req.get('priority') or 'normal'
    teams_text = []
    for t in org:
        members = ', '.join(t['members']) or 'no workers yet'
        line = (f"- team \"{t['teamId']}\" led by {t['leaderName']} in village "
                f"'{t['village']}' -- members: {members}")
        if t.get('busy'):
            line += f" ({t['busy']} currently busy)"
        if t.get('sprint'):
            line += f" [IN ACTIVE SPRINT {t['sprint']}]"
        if t.get('dedicatedTo'):
            line += f" [DEDICATED: {t['dedicatedTo']}]"
        teams_text.append(line)
    pin_bits = []
    if pc.get('budgetUsd'):
        pin_bits.append(f"The player named a total budget of ${pc['budgetUsd']:.2f} "
                        "for the whole ask; allocate it across pieces with judgment.")
    for key, lead in (('constraints', 'hard prohibitions'),
                      ('roles', 'preferred skills/roles'),
                      ('order', 'sequencing hints'),
                      ('mustInclude', 'tasks that must be covered')):
        if pc.get(key):
            pin_bits.append(f'{lead}: ' + '; '.join(pc[key]) + '.')
    pin_text = (' ' + ' '.join(pin_bits)) if pin_bits else ''
    owned = None
    if req.get('ownerProductId'):
        owned = (state.get('products') or {}).get(req['ownerProductId']) or {}
        pin_text += (f"\nOWNED WORK: this ask is about the product \"{owned.get('name') or req['ownerProductId']}\", "
                     f"owned by team \"{req.get('ownerTeamId')}\". Every piece must stay inside your own "
                     "org -- a \"shared\" piece means your own subteams only, first-come among them; "
                     "the work must never leave the owning team.")
    if director_id and governance.is_village_chief(state, director_id):
        # A village regional manager reviews at the senior tier: their tools
        # are ROUTING (pieces to their village's senior teams), not
        # founding -- every founding turns a worker into a manager, and the
        # chief's direct reports are senior directors, not workers to
        # convert. Senior directors found subdirector layers below
        # themselves in their own reviews.
        founding_text = (
            "- You are the regional manager (RM) of your village: route pieces to your senior "
            "directors' teams (the org list above) and say why each team "
            "fits. Founding subdirectors is not your tool -- your direct "
            "reports are senior directors, not workers to convert; a senior "
            "director grows their own subdirector layer in their own "
            "review when their team needs one.\n"
        )
    elif founding_mode == 'immediate':
        founding_text = (
            "- \"newSubdirectors\": each {\"seedWorker\": an existing member id to promote "
            "(omit to promote your most proven contributor), \"handOver\": how many workers "
            "move under them (default 2), \"purpose\": one line} -- founded in listed order; "
            "pieces tagged \"team\": \"new\" bind to them in that order. Founding is a "
            "capacity step of LAST RESORT (subteams committed or the ask truly needs "
            "another lane), never a default: every founding turns a worker into a "
            "manager. As a root director you may found directly.\n"
        )
    elif founding_mode == 'propose':
        founding_text = (
            "- \"newSubdirectors\": PROPOSALS ONLY -- each {\"seedWorker\": an existing "
            "member id to promote (omit for your most proven contributor), \"handOver\": "
            "how many workers move under them (default 2), \"purpose\": one line}. Your "
            "PARENT DIRECTOR judges whether the founding is necessary: only an approval "
            "creates the subteam (the \"new\" pieces file for it then), and a denial "
            "files those pieces inside your own subteam. Propose one only when your "
            "subteam genuinely cannot take the work otherwise.\n"
        )
    else:
        founding_text = "- You may not create subteams.\n"
    return (
        f"{director_name} is the director reviewing a large request before any team "
        f"starts work. The ask (priority: {prio}): '{goal[:400]}'.{pin_text} "
        "The org's subteams:\n" + '\n'.join(teams_text) + "\n"
        "A subteam already in an active sprint (or permanently dedicated to other "
        "work) cannot START new pieces until that commitment ends -- routing urgent "
        "work there just parks it; prefer free subteams for urgent pieces, and it is "
        "fine to give a committed subteam a slow-burn piece it will pick up later.\n"
        "Decide how the work partitions. You may return:\n"
        "- \"pieces\": each {\"team\": one of the team ids above, or \"own\" for your own "
        "remaining pool, or \"shared\" for work any of your subteams may pick up "
        "first-come, or \"new\" for a subteam you are founding below; \"ask\": ONE short "
        "sentence for that team's breakdown ceremony; \"priority\": \"low|normal|high|urgent\" "
        "or omit (an urgent ask deserves parallel pieces NOW; a slow-burn ask may be one "
        "low-priority piece built over time); \"notes\": optional one-line guidance.\n"
        + founding_text +
        "Keep every ask to one short sentence. A small ask may be ONE piece for one "
        "subteam; never fragment work that belongs together. Respond with ONLY valid "
        "JSON, no other text, no markdown fences, in exactly this shape: "
        '{"pieces":[{"team":"own|shared|new|<team id>","ask":"short ask",'
        '"priority":"low|normal|high|urgent","notes":"omit-ok"}],'
        '"newSubdirectors":[{"seedWorker":"omit-ok","handOver":2,"purpose":"one line"}]}'
    )


def _founding_mode(state, leader_id):
    """How a split reviewer may found subdirectors:
    - 'immediate': a ROOT team director (their parent is the admin or absent).
      The admin's standing authority already backs them; foundings execute
      inside their own review.
    - 'propose': a SUBDIRECTOR. They may PROPOSE foundings, but the founder's
      PARENT director judges necessity in an approval review before anything
      is created -- a subdirector never grows the management layer alone.
    - None: not a leader; no review convenes for them anyway."""
    index = {d.get('id'): d for d in (state.get('agentRoster') or [])
             if isinstance(d, dict) and d.get('id')}
    row = index.get(leader_id)
    if not row or row.get('isAdmin'):
        return None
    parent_row = index.get(row.get('director'))
    if parent_row is None or parent_row.get('isAdmin') \
            or parent_row.get('villageChief'):
        # Immediate founding covers the whole AUTHORITY chain: a village
        # chief of staff founders under the admin's standing authority, and
        # a SENIOR director under a chief holds the same standing their
        # admin-reporting peers have -- the chief tier must not demote the
        # senior layer into proposers.
        return 'immediate'
    return 'propose' if governance.direct_reports(state, leader_id) else None


def _can_found_subdirectors(state, director_id):
    """Whether a split reviewer may found subdirectors DIRECTLY (root team
    directors only). A subdirector proposes instead (see _founding_mode)."""
    return _founding_mode(state, director_id) == 'immediate'


def _found_subdirector(state, director_id, seed_worker=None, hand_over=2, purpose=None,
                       approved_by=None):
    """Promote an existing worker into a NEW subdirector under `director_id`:
    a subdirector is EMERGENT from the reporting graph (a direct report of the
    director who themselves leads people), so founding one hands `hand_over`
    of the director's plain workers to the seed (the promote-subdirector
    endpoint's exact mechanic). No new agents are spawned and no name is
    minted -- the roster's existing people reorganize.

    APPROVAL GATE: a ROOT team director founds inside their own review
    (approved_by may be omitted). A SUBDIRECTOR may only execute a founding
    their PARENT director approved: `approved_by` must name the founder's
    parent (the approval review records it), or the founding is refused --
    unilaterally growing the management layer below the root is exactly the
    cascade this gate exists to prevent. The per-director subdirector
    ceiling still applies at every level.

    Returns the subdirector id, or None when the founder lacks authority,
    the org is at the subdirector ceiling, the seed is invalid, or there are
    no workers to hand over (a leader with no reports is not a
    subdirector)."""
    roster = state.get('agentRoster') or []
    index = {d.get('id'): d for d in roster if isinstance(d, dict) and d.get('id')}
    director_row = index.get(director_id)
    if not director_row or director_row.get('isAdmin'):
        return None
    mode = _founding_mode(state, director_id)
    parent_id = director_row.get('director')
    if mode == 'immediate':
        pass  # root director: their own review IS the approval
    else:
        # Subdirector: the PARENT director's approval is required, by id.
        if not approved_by or approved_by != parent_id:
            return None
    if not governance.direct_reports(state, director_id):
        return None  # nothing to hand over: not a founder by structure
    existing = governance.subdirectors_of(state, director_id)
    if len(existing) >= governance.SUBDIRECTOR_LAYER_MAX:
        return None
    workers = [rid for rid in governance.direct_reports(state, director_id)
               if not governance.direct_reports(state, rid)]
    if seed_worker:
        seed_row = index.get(seed_worker)
        if (not seed_row or seed_row.get('director') != director_id
                or governance.direct_reports(state, seed_worker)):
            return None
    else:
        # Most-proven contributor first (approvedCount is the completed-work
        # signal the promote endpoint's docs treat as tenure), roster order as
        # the deterministic tiebreak.
        agents = state.get('agents') or {}
        workers.sort(key=lambda wid: (-int((agents.get(wid) or {}).get('approvedCount') or 0),))
        seed_worker = workers[0] if workers else None
    if not seed_worker:
        return None
    workers = [wid for wid in workers if wid != seed_worker]
    try:
        hand_over = max(1, min(8, int(hand_over or 2)))
    except (TypeError, ValueError):
        hand_over = 2
    moved = 0
    for wid in workers[:hand_over]:
        index[wid]['director'] = seed_worker
        moved += 1
    if not moved and not governance.direct_reports(state, seed_worker):
        return None  # nobody to lead -- not a subdirector by structure
    if purpose:
        seed_row = index.get(seed_worker) or {}
        notes = seed_row.setdefault('notes', [])
        note = f"Subdirector purpose: {str(purpose)[:200]}"
        if note not in notes:
            notes.append(note)
        if approved_by and approved_by != director_id:
            notes.append(f"Founded by {director_id} with {approved_by}'s approval.")
    governance.rebalance_under_subdirectors(state, director_id)
    return seed_worker


def _resolve_split(state, pending, now_ms, decider=None):
    """When the split window elapses, the reviewer partitions each pending
    ask: pieces file as child large-requests (each bound for its subteam's
    normal breakdown ceremony), shared pieces file team-less, and
    newSubdirectors are handled by the reviewer's founding mode -- a ROOT
    director founds immediately so pieces can bind; a SUBDIRECTOR's 'new'
    pieces are HELD on a pendingFoundation proposal for the parent director's
    approval review (filed bound to the new subteam on approval, or inside
    the proposer's own subteam on denial). The parent ask is accepted with
    splitPieces provenance either way -- a pending approval is a real
    partition decision, not a failure. On an outage or an unusable plan the
    parent drops awaitingSplit and becomes a NORMAL breakdown request for
    the reviewer's own team -- the current behavior, so the ask is never
    dropped. Idempotent if a request vanished."""
    decider = decider or _director_split_decider
    agents = state.get('agents') or {}
    director_id = pending.get('directorId')
    mode = _founding_mode(state, director_id)
    own_team_id = next((t.get('id') for t in (state.get('teams') or [])
                        if t.get('directorId') == director_id), director_id)
    director_name = (agents.get(director_id) or {}).get('name') or director_id
    for req in (state.get('backlogRequests') or []):
        if req.get('id') not in pending.get('reqIds', []):
            continue
        if req.get('status') != 'pending' or not req.get('awaitingSplit'):
            continue
        goal = req.get('goal') or req.get('title') or 'untitled large request'
        org = _split_org_summary(state, director_id)
        data = decider(state, _split_instructions(
            state, req, org, director_name, founding_mode=mode,
            director_id=director_id), goal)
        pieces, raw_pieces, new_subs = [], [], []
        if isinstance(data, dict):
            raw_pieces = [p for p in (data.get('pieces') or [])
                          if isinstance(p, dict) and (p.get('ask') or '').strip()]
            raw_subs = [s for s in (data.get('newSubdirectors') or [])
                        if isinstance(s, dict)]
            new_subs = raw_subs[:3]  # a review proposes/founds a capacity step, not a hiring spree
        founded_ids, held_proposals = [], []
        if mode == 'immediate':
            for spec in new_subs:
                sid = _found_subdirector(
                    state, director_id, seed_worker=spec.get('seedWorker') or None,
                    hand_over=spec.get('handOver') or 2, purpose=spec.get('purpose'))
                if sid:
                    founded_ids.append(sid)
                    _log_governance(state, director_id, 'subdirector_founded',
                                    {'subdirector': sid, 'purpose': str(spec.get('purpose') or '')[:200],
                                     'request': req.get('id')})
        new_slot = 0
        for p in raw_pieces:
            team_raw = (p.get('team') or 'own').strip()
            owned_product_id = req.get('ownerProductId') or None
            scope_for_child = None
            if team_raw == 'shared':
                child_team = None
                if owned_product_id:
                    # Owned work shared means WITHIN the owning org only:
                    # scope the piece to the reviewing director's own
                    # subteams (first-come among them). A director with no
                    # subteams has nowhere in-org to share it, so it stays
                    # home as their own work. Never company-wide for an
                    # owned ask -- that would hand the product to another
                    # team.
                    scope = list(governance.subdirectors_of(state, director_id))
                    if scope:
                        scope_for_child = scope
                    else:
                        child_team = own_team_id
            elif team_raw == 'new':
                if mode == 'propose':
                    # Held for the parent director's approval review; filed
                    # when it resolves (approved -> the new subteam, denied
                    # -> the proposer's own subteam). The ask itself is
                    # accepted with the held piece noted.
                    held_proposals.append((new_slot, p))
                    new_slot += 1
                    pieces.append({'team': 'pending-approval',
                                   'ask': (p.get('ask') or '').strip()[:120],
                                   'priority': p.get('priority') or 'normal'})
                    continue
                child_team = founded_ids[new_slot] if new_slot < len(founded_ids) else None
                new_slot += 1
                if not child_team:
                    child_team = own_team_id  # founding failed -- the piece stays in the org
            elif team_raw == 'own':
                child_team = own_team_id
            elif any(t['teamId'] == team_raw for t in org):
                child_team = team_raw
            else:
                child_team = own_team_id  # fail open: a bad team name keeps the work home
            priority = p.get('priority')
            child = _file_piece(state, director_id, own_team_id,
                                (p.get('ask') or '').strip(), child_team,
                                now_ms, constraints=req.get('playerConstraints'),
                                priority=priority,
                                owner_product_id=owned_product_id,
                                shared_scope=scope_for_child)
            if not child:
                continue
            child['splitFrom'] = req.get('id')
            child['splitBy'] = director_id
            if p.get('notes'):
                child['splitNotes'] = str(p['notes'])[:200]
            pieces.append({'team': child_team or 'shared', 'ask': child['title'],
                           'priority': child.get('priority') or 'normal',
                           'request': child['id']})
        if mode == 'propose' and held_proposals and new_subs:
            _stage_foundations(state, director_id, req, new_subs, held_proposals, now_ms)
            _log_governance(state, director_id, 'subdirector_founding_proposed',
                            {'request': req.get('id'), 'proposals': len(new_subs),
                             'parent': (next((d.get('director') for d in
                                              (state.get('agentRoster') or [])
                                              if d.get('id') == director_id), None))})
        if pieces:
            req['status'] = 'accepted'
            req['split'] = True
            req['splitPieces'] = pieces
            _log_governance(state, director_id, 'large_request_split',
                            {'request': req['id'], 'goal': goal[:120],
                             'pieces': len(pieces),
                             'shared': sum(1 for x in pieces if x['team'] == 'shared'),
                             'founded': founded_ids})
        else:
            # No usable plan (outage or an empty partition): fall back to the
            # single-team path -- the WHOLE goal breaks down inside the
            # reviewer's own team exactly as it would have before this layer
            # existed. Never drop the ask.
            req.pop('awaitingSplit', None)
            req['splitFallback'] = True
            _log_governance(state, director_id, 'large_request_split_fallback',
                            {'request': req.get('id'), 'goal': goal[:120],
                             'team': own_team_id})
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, _SPLIT_MEET_MS)
    state.setdefault('pendingDirectorSplits', {}).pop(director_id, None)


def _stage_foundations(state, founder_id, parent_req, specs, held_pieces, now_ms):
    """Register a subdirector's founding PROPOSALS for their parent director's
    approval review. 'new'-tagged pieces bind to the proposals in order
    (piece i rides proposal i); a proposal carries its held pieces and files
    them when the approval resolves -- approved -> the new subteam, denied ->
    the proposer's own subteam. Never drops a piece."""
    index = {d.get('id'): d for d in (state.get('agentRoster') or [])
             if isinstance(d, dict) and d.get('id')}
    parent_id = (index.get(founder_id) or {}).get('director')
    if not parent_id:
        # No parent to approve: file the held pieces inside the proposer's
        # own subteam now (never drop).
        for _slot, p in held_pieces:
            child = file_large_request(
                state, founder_id, (p.get('ask') or '').strip(), founder_id,
                now_ms=now_ms, constraints=parent_req.get('playerConstraints'),
                priority=p.get('priority'),
                owner_product_id=parent_req.get('ownerProductId'))
            if child:
                child['splitFrom'] = parent_req.get('id')
                child['splitBy'] = founder_id
        return
    proposals = state.setdefault('pendingFoundations', [])
    for slot, spec in enumerate(specs):
        piece = held_pieces[slot][1] if slot < len(held_pieces) else None
        prop = {
            'id': f"fnd-{len(proposals) + 1}",
            'founder': founder_id, 'parent': parent_id,
            'seedWorker': spec.get('seedWorker') or None,
            'handOver': spec.get('handOver') or 2,
            'purpose': str(spec.get('purpose') or '')[:200],
            'parentRequest': parent_req.get('id'),
            'piece': ({'ask': piece.get('ask'), 'priority': piece.get('priority')}
                      if piece else None),
            # Owned work stays owned through a founding: the eventual piece
            # (filed on approval or denial) re-stamps the owner.
            'ownerProductId': parent_req.get('ownerProductId') or None,
            'requestedAt': now_ms, 'status': 'pending',
        }
        proposals.append(prop)


def _start_split(state, director_id, req_ids, now_ms, pos_offset=0):
    """Convene the director's split review at the Command Center: the director
    (required, free+on-duty) plus whichever subdirectors are free -- a busy
    subdirector's absence never blocks the DIRECTOR's own decision, and the
    meeting restores attendees like every other ceremony. Returns True if
    convened, False to defer and retry next pass."""
    agents = state.get('agents') or {}
    director = agents.get(director_id)
    if not director or director.get('busy') or director.get('offDuty'):
        return False
    ids = [director_id]
    for sid in governance.subdirectors_of(state, director_id):
        a = agents.get(sid)
        if a and not a.get('busy') and not a.get('offDuty'):
            ids.append(sid)
    snap = {}
    base_x = 315 + (pos_offset * 460)
    for i, aid in enumerate(ids):
        a = agents.get(aid)
        snap[aid] = {k: a.get(k) for k in ('offDuty', 'visible', 'x', 'y', 'dir',
                                           'task', 'busy', 'inRoom', 'pairWith', 'handoff', 'workUntil')}
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = base_x + (i * 85)
        a['roomY'] = 155
    state.setdefault('pendingDirectorSplits', {})[director_id] = {
        'at': now_ms + _SPLIT_MEET_MS, 'embarked': True,
        'directorId': director_id, 'reqIds': req_ids, 'people': snap,
    }
    _log_governance(state, director_id, 'director_split',
                    {'action': 'meeting_start', 'requests': req_ids})
    return True


# --- Foundation approval reviews (subdirector proposals) -------------------

def _start_foundation(state, parent_id, proposal_ids, now_ms, pos_offset=0):
    """Convene the PARENT director's approval review: the parent plus the
    proposing subdirector(s) at the Command Center. The parent judges each
    proposal's necessity; nothing is created by convening. Returns True if
    convened, False to defer (parent or proposer busy/off-duty)."""
    agents = state.get('agents') or {}
    proposals = [p for p in (state.get('pendingFoundations') or [])
                 if p.get('id') in set(proposal_ids) and p.get('status') == 'pending']
    if not proposals:
        return False
    parent = agents.get(parent_id)
    if not parent or parent.get('busy') or parent.get('offDuty'):
        return False
    ids = [parent_id]
    for p in proposals:
        founder = p.get('founder')
        fa = agents.get(founder)
        if not fa or fa.get('busy') or fa.get('offDuty'):
            return False  # the proposer must be present to make their case
        if founder not in ids:
            ids.append(founder)
    snap = {}
    base_x = 315 + (pos_offset * 460)
    for i, aid in enumerate(ids):
        a = agents.get(aid)
        snap[aid] = {k: a.get(k) for k in ('offDuty', 'visible', 'x', 'y', 'dir',
                                           'task', 'busy', 'inRoom', 'pairWith', 'handoff', 'workUntil')}
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = base_x + (i * 85)
        a['roomY'] = 155
    state.setdefault('pendingFoundationReviews', {})[parent_id] = {
        'at': now_ms + _FOUND_MEET_MS, 'embarked': True,
        'parentId': parent_id, 'proposalIds': [p['id'] for p in proposals],
        'people': snap,
    }
    _log_governance(state, parent_id, 'foundation_review',
                    {'action': 'meeting_start',
                     'proposals': [p['id'] for p in proposals],
                     'founders': sorted({p['founder'] for p in proposals})})
    return True


def _foundation_instructions(state, proposals, parent_name):
    """The parent director's approval prompt: each proposal with the
    founding subdirector, seed, hand-over count, purpose, and the piece it
    would carry. The judgment is necessity, and the default is NO."""
    lines = []
    for p in proposals:
        piece = p.get('piece') or {}
        lines.append(
            f"- proposal {p['id']} from {p['founder']}: promote "
            f"{p.get('seedWorker') or 'their most proven contributor'}, hand over "
            f"{p.get('handOver') or 2} workers, purpose: {p.get('purpose') or '(none)'}. "
            f"Carried piece (priority {piece.get('priority') or 'normal'}): "
            f"'{(piece.get('ask') or '(none)')[:160]}'")
    return (
        f"{parent_name} is the director deciding whether their subdirectors' founding "
        "proposals are NECESSARY. Each founding converts a worker into a manager "
        "(handing their existing coworkers over) -- the default answer is NO; approve "
        "only when the proposing subteam genuinely cannot take its work otherwise "
        "(committed, overloaded, or the piece needs its own lane). Proposals:\n"
        + '\n'.join(lines) + "\n"
        "Respond with ONLY valid JSON, no other text, no markdown fences, in exactly "
        "this shape: {\"approvals\":[{\"id\":\"<proposal id>\",\"approve\":true|false,"
        "\"reason\":\"one line\"}]}"
    )


def _foundation_decider_default(state, instructions, goal):
    """Default resolver for the parent's approval review: a high-tier (JEV-
    gated) chat call. Returns the parsed dict, or None on outage (the resolve
    then DENIES every proposal -- the conservative default; the held pieces
    file inside the proposer's own subteam instead)."""
    try:
        import serve
    except Exception:
        return None
    try:
        model = serve._resolve_model_tier(
            f'Judging subdirector founding proposals: {goal[:200]}', allow_high=True)
        if not model:
            return None
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': instructions},
             {'role': 'user', 'content': f"Judge these founding proposals: {goal}"}],
            max_tokens=serve._big_task_max_tokens)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__foundation_reviews__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        start = reply.find('{')
        end = reply.rfind('}')
        if start != -1 and end > start:
            reply = reply[start:end + 1]
        parsed = json.loads(reply)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    return None


# Injectable for tests (mirrors every other ceremony decider).
_foundation_decider = _foundation_decider_default


def _file_piece(state, reviewer_id, own_team_id, ask, child_team, now_ms,
                constraints=None, priority=None, notes=None,
                owner_product_id=None, shared_scope=None):
    """File one split piece as a child large-request. A piece routed to the
    reviewer's OWN subteam must not await ANOTHER split by the same reviewer
    (that would ping-pong forever): the reviewer just partitioned this ask,
    so their own subteam's breakdown ceremony is the destination. Pieces
    routed to a DIFFERENT leader keep the review stamp -- that leader
    reviews their own piece before their team starts.

    Product ownership inherits down the cascade: an owned ask's pieces carry
    ownerProductId/ownerTeamId so every downstream breakdown card stays
    pinned to the owning team, and an owned 'shared' piece is scoped to the
    reviewer's own subteams (sharedScope) -- shared never means company-wide
    for owned work."""
    child = file_large_request(state, reviewer_id, ask, child_team,
                               now_ms=now_ms, constraints=constraints,
                               priority=priority,
                               owner_product_id=owner_product_id)
    if not child:
        return None
    if child_team and child_team == own_team_id:
        child.pop('awaitingSplit', None)
    if shared_scope:
        child['sharedScope'] = list(shared_scope)
    if notes:
        child['splitNotes'] = str(notes)[:200]
    return child


def _file_held_piece(state, prop, child_team, note, now_ms):
    """File a proposal's held piece as a child large-request bound for
    `child_team`, with splitFrom/splitBy provenance. Shared of the two
    outcomes (approved/denied) the piece is never dropped."""
    piece = prop.get('piece') or {}
    ask = (piece.get('ask') or '').strip()
    if not ask:
        return None
    child = _file_piece(state, prop['founder'], prop['founder'], ask,
                        child_team, now_ms, priority=piece.get('priority'),
                        notes=note,
                        owner_product_id=prop.get('ownerProductId'))
    if not child:
        return None
    child['splitFrom'] = prop.get('parentRequest')
    child['splitBy'] = prop.get('founder')
    return child


def _resolve_foundation(state, pending, now_ms, decider=None):
    """When the approval window elapses, the PARENT director rules on each
    proposal. Approved: the founding executes (_found_subdirector with the
    parent as approved_by) and the held piece files bound for the new
    subteam. Denied / outage / vanished founder: the piece files inside the
    proposer's own subteam -- the work is never dropped, it just stays
    home. Restores attendees; pops the review record. Idempotent."""
    decider = decider or _foundation_decider
    agents = state.get('agents') or {}
    parent_id = pending.get('parentId')
    parent_name = (agents.get(parent_id) or {}).get('name') or parent_id
    proposals = state.setdefault('pendingFoundations', [])
    batch = [p for p in proposals
             if p.get('id') in set(pending.get('proposalIds') or [])
             and p.get('status') == 'pending']
    if not batch:
        state.setdefault('pendingFoundationReviews', {}).pop(parent_id, None)
        return
    # The proposer must still lead people (they may have been fired or
    # reorged since convening); a proposal from a non-leader is denied.
    for p in batch:
        if not governance.direct_reports(state, p.get('founder')):
            p['status'] = 'denied'
            p['reason'] = 'proposer no longer leads a subteam'
    batch = [p for p in batch if p.get('status') == 'pending']
    data = None
    if batch:
        data = decider(state, _foundation_instructions(state, batch, parent_name),
                       f"{len(batch)} founding proposal(s) from "
                       f"{', '.join(sorted({p['founder'] for p in batch}))}")
    approvals = {}
    if isinstance(data, dict):
        for a in (data.get('approvals') or []):
            if isinstance(a, dict) and a.get('id'):
                approvals[a['id']] = a
    for p in batch:
        verdict = approvals.get(p['id']) or {}
        approve = bool(verdict.get('approve'))
        reason = str(verdict.get('reason') or '')[:200] or (
            'parent review denied' if not approve else '')
        if approve:
            sid = _found_subdirector(
                state, p['founder'], seed_worker=p.get('seedWorker') or None,
                hand_over=p.get('handOver') or 2, purpose=p.get('purpose'),
                approved_by=parent_id)
            if sid:
                p['status'] = 'approved'
                p['reason'] = reason
                p['subdirector'] = sid
                if p.get('piece'):
                    _file_held_piece(state, p, sid, f"Approved by {parent_name}.", now_ms)
                _log_governance(state, parent_id, 'subdirector_founded',
                                {'subdirector': sid, 'purpose': p.get('purpose'),
                                 'proposal': p['id'], 'approved': True})
            else:
                # Mechanic refused (ceiling / invalid seed): file home.
                p['status'] = 'denied'
                p['reason'] = 'founding refused (ceiling or invalid seed)'
                if p.get('piece'):
                    _file_held_piece(state, p, p['founder'],
                                     f"Founding refused; piece stayed with {p['founder']}.",
                                     now_ms)
        else:
            p['status'] = 'denied'
            p['reason'] = reason or 'parent review denied'
            if p.get('piece'):
                _file_held_piece(state, p, p['founder'],
                                 f"{parent_name} judged the founding unnecessary; "
                                 f"the piece stays with {p['founder']}.", now_ms)
            _log_governance(state, parent_id, 'subdirector_founding_denied',
                            {'proposal': p['id'], 'founder': p['founder'],
                             'reason': p['reason'][:120]})
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, _FOUND_MEET_MS)
    state.setdefault('pendingFoundationReviews', {}).pop(parent_id, None)


def _foundation_step(state, now, now_ms, decider=None):
    """One foundation-approval pass, called from _task_cycle right after the
    split step. Advances in-flight approval reviews (embark one pass, RESOLVE
    the next) and convenes a review for every parent with pending proposals
    -- parent AND every proposing subdirector must be free+on-duty (the
    proposer makes their case; a missing chair defers the review)."""
    review_map = state.setdefault('pendingFoundationReviews', {})
    for parent_id in list(review_map.keys()):
        pending = review_map[parent_id]
        if not pending.get('embarked'):
            if not _start_foundation(state, parent_id,
                                     pending.get('proposalIds', []), now_ms):
                continue
        else:
            _resolve_foundation(state, pending, now_ms, decider=decider)
    by_parent = {}
    for p in (state.get('pendingFoundations') or []):
        if p.get('status') == 'pending':
            by_parent.setdefault(p.get('parent'), []).append(p['id'])
    slot = 0
    for parent_id, prop_ids in sorted(by_parent.items()):
        if parent_id in review_map:
            continue  # already in flight
        if _start_foundation(state, parent_id, prop_ids, now_ms, pos_offset=slot):
            slot += 1


def _director_split_step(state, now, now_ms, decider=None):
    """One director-split pass, called from _task_cycle ungated next to the
    breakdown step. A pass (a) advances every in-flight split review
    (embark on one pass, RESOLVE on the next), and (b) convenes a review for
    every leader with split-pending requests -- ROOT directors via their
    team rows, SUBDIRECTORS via the roster walk (their emergent subteams
    have no teams row). Never convenes when the reviewer is mid-work: the
    review is their own judgment, and no borrowed stand-in may partition
    someone else's org."""
    pending_map = state.setdefault('pendingDirectorSplits', {})
    for director_id in list(pending_map.keys()):
        pending = pending_map[director_id]
        if not pending.get('embarked'):
            if not _start_split(state, director_id, pending.get('reqIds', []),
                                now_ms):
                continue
        else:
            _resolve_split(state, pending, now_ms, decider=decider)
    slot = 0
    handled = set()
    for team in (state.get('teams') or []):
        director_id = team.get('directorId')
        if not director_id or director_id in pending_map:
            continue
        handled.add(director_id)
        reqs = _pending_split_requests(state, director_id)
        if not reqs:
            continue  # nothing to review -- don't convene an empty meeting
        reqs.sort(key=lambda r: (-_request_priority_rank(r), r.get('filedAt') or 0))
        if _start_split(state, director_id, [r['id'] for r in reqs],
                        now_ms, pos_offset=slot):
            slot += 1  # only to spread simultaneous reviews apart in the room
    # Subdirector reviewers: a piece routed to a subdirector awaits THEIR
    # split review (they plan their subteam, propose foundings to their
    # parent, and route pieces). The convene loop walks the roster so an
    # emergent subteam without a teams row is never skipped.
    for row in (state.get('agentRoster') or []):
        if not isinstance(row, dict):
            continue
        rid = row.get('id')
        if not rid or rid in handled or rid in pending_map or row.get('isAdmin'):
            continue
        if not governance.direct_reports(state, rid):
            continue  # not a leader -- nothing to review with
        reqs = _pending_split_requests(state, rid)
        if not reqs:
            continue
        reqs.sort(key=lambda r: (-_request_priority_rank(r), r.get('filedAt') or 0))
        if _start_split(state, rid, [r['id'] for r in reqs], now_ms,
                        pos_offset=slot):
            slot += 1  # only to spread simultaneous reviews apart in the room
