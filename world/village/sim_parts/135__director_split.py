

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

_PRIORITY_NAMES = ('low', 'normal', 'high', 'urgent')


def _split_director_for_request(state, req):
    """The director whose review owns a split-pending request: the receiving
    team's director. Shared requests have no team and never await a split."""
    team_id = req.get('teamId')
    if not team_id:
        return None
    return (_team_row(state, team_id) or {}).get('directorId') or None


def _pending_split_requests(state, director_id):
    """Pending breakdown requests awaiting THIS director's split review."""
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
    own = _commitment(own_team_id)
    org = [{'teamId': own_team_id, 'leader': director_id,
            'leaderName': (agents.get(director_id) or {}).get('name') or director_id,
            'village': village_of_agent(state, director_id),
            'members': governance.direct_reports(state, director_id),
            'sprint': own['sprint'], 'dedicatedTo': own['dedicatedTo']}]
    for sid in governance.subdirectors_of(state, director_id):
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


def _split_instructions(state, req, org, director_name):
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
        "- \"newSubdirectors\": each {\"seedWorker\": an existing member id to promote "
        "(omit to promote your most proven contributor), \"handOver\": how many workers "
        "move under them (default 2), \"purpose\": one line} -- founded in listed order; "
        "pieces tagged \"team\": \"new\" bind to them in that order.\n"
        "Keep every ask to one short sentence. A small ask may be ONE piece for one "
        "subteam; never fragment work that belongs together. Respond with ONLY valid "
        "JSON, no other text, no markdown fences, in exactly this shape: "
        '{"pieces":[{"team":"own|shared|new|<team id>","ask":"short ask",'
        '"priority":"low|normal|high|urgent","notes":"omit-ok"}],'
        '"newSubdirectors":[{"seedWorker":"omit-ok","handOver":2,"purpose":"one line"}]}'
    )


def _found_subdirector(state, director_id, seed_worker=None, hand_over=2, purpose=None):
    """Promote an existing worker into a NEW subdirector under `director_id`:
    a subdirector is EMERGENT from the reporting graph (a direct report of the
    director who themselves leads people), so founding one hands `hand_over`
    of the director's plain workers to the seed (the promote-subdirector
    endpoint's exact mechanic). No new agents are spawned and no name is
    minted -- the roster's existing people reorganize. Returns the subdirector
    id, or None when the org is at the subdirector ceiling, the seed is
    invalid, or there are no workers to hand over (a leader with no reports
    is not a subdirector)."""
    roster = state.get('agentRoster') or []
    index = {d.get('id'): d for d in roster if isinstance(d, dict) and d.get('id')}
    director_row = index.get(director_id)
    if not director_row or director_row.get('isAdmin'):
        return None
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
    governance.rebalance_under_subdirectors(state, director_id)
    return seed_worker


def _resolve_split(state, pending, now_ms, decider=None):
    """When the split window elapses, the director partitions each pending
    ask: pieces file as child large-requests (each bound for its subteam's
    normal breakdown ceremony), shared pieces file team-less, and
    newSubdirectors are founded first so pieces can bind to them. The parent
    is accepted with splitPieces provenance. On an outage or an unusable
    plan the parent drops awaitingSplit and becomes a NORMAL breakdown
    request for the director's own team -- the current behavior, so the ask
    is never dropped. Idempotent if a request vanished."""
    decider = decider or _director_split_decider
    agents = state.get('agents') or {}
    director_id = pending.get('directorId')
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
        data = decider(state, _split_instructions(state, req, org, director_name), goal)
        pieces, raw_pieces, new_subs = [], [], []
        if isinstance(data, dict):
            raw_pieces = [p for p in (data.get('pieces') or [])
                          if isinstance(p, dict) and (p.get('ask') or '').strip()]
            raw_subs = [s for s in (data.get('newSubdirectors') or [])
                        if isinstance(s, dict)]
            new_subs = raw_subs[:3]  # a review founds a capacity step, not a hiring spree
        founded_ids = []
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
            if team_raw == 'shared':
                child_team = None
            elif team_raw == 'new':
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
            child = file_large_request(
                state, director_id, (p.get('ask') or '').strip(), child_team,
                now_ms=now_ms, constraints=req.get('playerConstraints'),
                priority=priority)
            if not child:
                continue
            child['splitFrom'] = req.get('id')
            child['splitBy'] = director_id
            if p.get('notes'):
                child['splitNotes'] = str(p['notes'])[:200]
            pieces.append({'team': child_team or 'shared', 'ask': child['title'],
                           'priority': child.get('priority') or 'normal',
                           'request': child['id']})
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
            # director's own team exactly as it would have before this layer
            # existed. Never drop the ask.
            req.pop('awaitingSplit', None)
            req['splitFallback'] = True
            _log_governance(state, director_id, 'large_request_split_fallback',
                            {'request': req.get('id'), 'goal': goal[:120],
                             'team': own_team_id})
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, _SPLIT_MEET_MS)
    state.setdefault('pendingDirectorSplits', {}).pop(director_id, None)


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


def _director_split_step(state, now, now_ms, decider=None):
    """One director-split pass, called from _task_cycle ungated next to the
    breakdown step. A pass (a) advances every in-flight split review
    (embark on one pass, RESOLVE on the next), and (b) convenes a review for
    every director who has split-pending requests and is free+on-duty. Never
    convenes when the director is mid-work: the review is the director's own
    judgment, and no borrowed stand-in may partition someone else's org."""
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
    for team in (state.get('teams') or []):
        director_id = team.get('directorId')
        if not director_id or director_id in pending_map:
            continue
        reqs = _pending_split_requests(state, director_id)
        if not reqs:
            continue  # nothing to review -- don't convene an empty meeting
        reqs.sort(key=lambda r: (-_request_priority_rank(r), r.get('filedAt') or 0))
        if _start_split(state, director_id, [r['id'] for r in reqs],
                        now_ms, pos_offset=slot):
            slot += 1  # only to spread simultaneous reviews apart in the room
