

def _file_free_spike(state, agent_id, completed_task, now_ms=None):
    """The spare-time lane: an agent that just finished REAL deliverable work may
    earn one FREE SPIKE this week -- a self-filed, never-groomed, lowest-priority
    investigation of its own choosing. Deterministic intake (no Jev), bounded by
    the per-agent weekly allowance AND the tank-wide weekly cap, and time-boxed.
    A free spike is tagged `moonshot`: protected from rule mining (its failures
    never become operator rules) and already non-gated (spikes don't ship a
    peer-reviewed deliverable). Returns 1 when a free spike was queued, else 0.
    Dedups against an identical pending free spike in the same room."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    room = (completed_task or {}).get('room')
    if not room or room not in VALUED_QUEUE_ROOMS:
        return 0
    a = (state.get('agents') or {}).get(agent_id)
    if not a or a.get('offDuty') or a.get('busy') or a.get('task'):
        return 0
    week = _free_spike_week(now_ms)
    used_map = state.setdefault('freeSpikeUsed', {})
    if not isinstance(used_map, dict):
        used_map = state['freeSpikeUsed'] = {}
    used_week, used = used_map.get(agent_id, (week, 0))
    if used_week != week:
        used = 0
    if used >= FREE_SPIKE_ALLOWANCE_PER_WEEK:
        return 0
    global_map = state.setdefault('freeSpikeGlobal', {})
    if not isinstance(global_map, dict):
        global_map = state['freeSpikeGlobal'] = {}
    # JSON round-trips (save/load) turn every object key into a string, and
    # these maps are keyed by WEEK NUMBERS -- so after one save the keys are
    # strings and the `k < week - 1` age comparison below would raise on
    # str-vs-int. Coerce back to ints at entry so the maps are always usable.
    global_map = state['freeSpikeGlobal'] = _free_spike_week_map(global_map)
    # Roll the tank-wide week bucket + prune stale buckets (bounded growth).
    for stale in [k for k in global_map if k < week - 1]:
        del global_map[stale]
    tank_used = global_map.get(week, 0)
    if tank_used >= FREE_SPIKE_GLOBAL_CAP_PER_WEEK:
        return 0
    # One free exploration per room per week: a pending moonshot spike already
    # filed in this room THIS week is the same question -- don't double-file.
    rooms_map = state.setdefault('freeSpikeRooms', {})
    if not isinstance(rooms_map, dict):
        rooms_map = state['freeSpikeRooms'] = {}
    rooms_map = state['freeSpikeRooms'] = _free_spike_week_map(rooms_map)
    for stale in [k for k in rooms_map if k < week - 1]:
        del rooms_map[stale]
    if room in (rooms_map.get(week) or []):
        return 0
    global_map[week] = tank_used + 1
    used_map[agent_id] = (week, used + 1)
    rooms_map.setdefault(week, []).append(room)
    audit_id = f'free-spike:{week}:{agent_id}:{room}'
    title = f"Free exploration: question an assumption about {room}"
    instructions = (
        f"{FREE_SPIKE_PREMISE_GUIDANCE} You just completed real work in {room} "
        f"('{completed_task.get('title') or 'previous task'}'). This is your spare "
        f"time: pick ONE question about {room} that the committed work does not cover "
        f"-- a technique, a method, or an assumption your team holds that you want to "
        f"challenge or understand better. Investigate honestly and write up what you "
        f"found."
    )
    queue_spike(state, title, room, FREE_SPIKE_BUDGET_MS, now_ms=now_ms,
                instructions=instructions, goal=f"Free exploration in {room}",
                moonshot=True, notBefore=now_ms + FREE_SPIKE_DEFER_MS)
    if state.get('workQueue'):
        state['workQueue'][-1]['auditId'] = audit_id
    _log_governance(state, agent_id, 'free_spike_filed', {
        'room': room, 'week': week, 'agentUsed': used + 1,
        'tankUsed': tank_used + 1, 'after': (completed_task or {}).get('id'),
        'auditId': audit_id,
    })
    return 1


def _maybe_file_followup(state, agent_id, completed_task, now_ms):
    """Deterministic signal generator: an on-duty agent that just COMPLETED a
    task in a delegatable room files a follow-up work-request if that room's
    backlog is thin. Honest, derivable 'I saw a real gap' -- no fake dialogue.
    Guards against churn: never fires for a room with real remaining work, never
    fires off-duty / mid-handoff, and a spike/bug completion doesn't double-file
    (it already IS the incident response)."""
    if not completed_task or completed_task.get('room') not in VALUED_QUEUE_ROOMS:
        return
    a = (state.get('agents') or {}).get(agent_id)
    if not a or a.get('offDuty') or a.get('busy') or a.get('task'):
        return
    if (completed_task.get('taskType') or 'code') in ('spike', 'bug', 'review', 'research', 'meeting'):
        # spike/bug/research already IS the investigation itself (incident
        # response or open-ended question); a review is meta-process work, not
        # a fresh deliverable; a meeting is a conversation whose minutes are
        # the artifact -- none of them should propose the next story
        # (that's the recursion: 'Follow-up ... after completing Review:
        # Follow-up ...' cards grew indefinitely).
        return
    if (completed_task.get('distill') or completed_task.get('skillReview')
            or completed_task.get('research')):
        # Distill and skill-review cards write the wiki or promote files; they
        # are not a fresh room deliverable, and their completions filed dozens
        # of identical 'further observatory work' requests.
        return
    # A follow-up to a follow-up: a completed card that was ITSELF a follow-up
    # (taskType 'code', title prefixed 'Follow-up:') must not propose yet another
    # follow-up. Otherwise each completed follow-up nests its own title into the
    # next one and the backlog churns self-referential cards that mostly get
    # groomed out. Only a REAL deliverable completion proposes the next story.
    prev_title = (completed_task.get('title') or '').strip()
    if prev_title.lower().startswith('follow-up:'):
        return
    room_label = completed_task['room']
    # Measure the room's backlog BEFORE the spare-time lane files anything -- a
    # just-filed free spike must not count against the room's "thin" check (it
    # is spare time, not committed work).
    backlog = _room_backlog_count(state, room_label)
    # Spare-time lane: a finished REAL deliverable earns the agent's weekly free
    # spike (independent of room backlog -- exploration isn't gated on the room
    # thinning). Bounded by _file_free_spike's own allowance/cap.
    _file_free_spike(state, agent_id, completed_task, now_ms)
    if backlog > WORK_REQUEST_ROOM_THIN:
        return
    title = f"Follow-up: further {room_label} work after completing '{completed_task.get('title') or 'previous task'}'"
    # Accepted duplicates do not re-file either. The old check only saw
    # pending duplicates, so one accepted follow-up plus its next completion
    # could mint the same request again every sprint.
    for q in (state.get('backlogRequests') or []):
        if q.get('title') == title and q.get('status') in ('pending', 'accepted'):
            return
    file_work_request(
        state, agent_id, title, room_label,
        reason=f"Completed '{completed_task.get('title') or 'previous task'}' in {room_label} and the remaining backlog there has thinned to {backlog} item(s); this room can absorb another story. {ABSOLUTE_ZERO_SCOPING_GUIDANCE}")


def _refinement_decider_default(instructions, criteria):
    """Default Jev resolver for a single work-request groom: accept (it becomes
    a story) or reject (groomed out). Late-imports serve.py so sim.py stays
    importable/unit-testable offline. Returns a choice id, or None on outage."""
    import serve
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': {c['id']: c['description'] for c in criteria}}})
        choice, _, _ = serve._jev_choice(data)
        return choice if any(c['id'] == choice for c in criteria) else None
    except Exception:
        return None


# Injectable for tests (mirrors _governance_decider / _social_decider); the live
# loop uses the Jev-backed default. The signal generator (_maybe_file_followup)
# is deliberately NOT decider-gated -- filing is deterministic intake, grooming
# is the decision.
_refinement_decider = _refinement_decider_default


def _refinement_team(state, req):
    """Which team owns `req`. Requests filed from a JIRA issue carry an explicit
    `teamId` (the filer may be the player, who reports to nobody) -- honor it
    first so the owning team's scrum master grooms the card. Otherwise fall
    back to the filer's reporting tree (a work-request an agent filed). Returns
    the team dict, or None if the filer isn't a member of any team."""
    if req.get('teamId'):
        t = _team_row(state, req.get('teamId'))
        if t:
            return t
    filed_by = req.get('filedBy')
    for t in (state.get('teams') or []):
        if filed_by in _sim_direct_reports(state, t.get('directorId')):
            return t
    return None


def _refinement_scrum_master_for_team(state, team_id):
    """The effective scrum master of `team_id`: the designated scrum master, or
    -- for a team below SCRUM_MASTER_MIN_TEAM_SIZE workers -- the team director
    standing in (a small team doesn't need a dedicated facilitator yet, by design). Returns None only for a big team that genuinely
    lacks a designated scrum master. Per-team ceremonies: only a team's OWN
    scrum master grooms that team's requests, never a cross-team stand-in
    (matches a real org where each team refines its own backlog)."""
    for t in (state.get('teams') or []):
        if t.get('id') == team_id or t.get('directorId') == team_id:
            if t.get('scrumMasterId'):
                return t['scrumMasterId']
            director_id = t.get('directorId')
            if (director_id and _team_member_count(state, director_id)
                    < team_policy(state, team_id, 'scrumMasterMinTeamSize')):
                return director_id
            return None
    return None


def _ceremony_facilitator(state, team_id):
    """A FREE, ON-DUTY facilitator for `team_id`'s ceremony -- the one change
    that makes scrum-master absence never block a ceremony. Mirrors the retro's
    fallback ladder (_retro_scrum_master): the effective scrum master when
    free+on-duty (a small team's own director standing in, per
    _refinement_scrum_master_for_team), else the team's OWN director, else a
    non-busy director borrowed from ANOTHER team (the OWN director is preferred
    over a borrowed one -- a team's grooming should be run by someone it works
    with). Returns None only when no facilitator can be found at all (the
    ceremony then waits for a later pass)."""
    teams = {t.get('id'): t for t in (state.get('teams') or [])}
    agents = state.get('agents') or {}

    def _free(aid):
        a = agents.get(aid)
        return bool(a) and not a.get('busy') and not a.get('offDuty')

    sm = _refinement_scrum_master_for_team(state, team_id)
    if sm and _free(sm):
        return sm
    did = (teams.get(team_id) or {}).get('directorId')
    if did and _free(did):
        return did
    # Borrow a free director from another team rather than leaving the team's
    # grooming (or breakdown) stranded on a busy/off-duty own facilitator.
    for t in (state.get('teams') or []):
        other = t.get('directorId')
        if not other or other == did:
            continue
        if _free(other):
            return other
    # Last chair: the chief of staff. The admin role is authority, not a team
    # (it has no team of its own), so it never appears in the loop above, but
    # it can still run the ceremony when every team director is unavailable.
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin') and _free(d.get('id')):
            return d.get('id')
    # No one is on duty. A pending groom is real work the player already
    # asked for, so wake the best available facilitator instead of leaving it
    # queued indefinitely. _start_refinement parks them on duty and restores
    # their prior state afterwards.
    def _wakeable(aid):
        a = agents.get(aid)
        return bool(a) and not a.get('busy')

    if sm and _wakeable(sm):
        return sm
    if did and _wakeable(did):
        return did
    for t in (state.get('teams') or []):
        other = t.get('directorId')
        if not other or other == did:
            continue
        if _wakeable(other):
            return other
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin') and _wakeable(d.get('id')):
            return d.get('id')
    return None


def _refinement_attendees(state, req_ids, scrum_master_id):
    """The ceremony's attendees: the scrum master + every AGENT who FILED a
    pending request this round (req_ids are work-request ids; the filers are
    resolved through the backlogRequests records). Non-agent filers (e.g. the
    player filing a JIRA issue) cannot attend a room ceremony and must not
    stall it -- they are dropped from the attendee list, and the scrum master
    still grooms their card. ONLY the scrum master must be free to convene: a
    BUSY filer's card is still groomed this round but she does not attend (her
    card's acceptance never needed her in the room), so one busy worker can no
    longer stall the whole team's grooming. An off-duty attendee is parked and
    free, and _start_refinement wakes her (snapshot + restore, exactly like the
    Social's off-duty Hangout attendees)."""
    agents = state.get('agents') or {}
    sm = agents.get(scrum_master_id)
    if not sm or sm.get('busy'):
        return None  # the facilitator must be free to run the meeting
    filer_ids = []
    for rid in req_ids:
        r = next((x for x in (state.get('backlogRequests') or []) if x.get('id') == rid), None)
        if r and r.get('filedBy') and r['filedBy'] not in filer_ids \
                and r['filedBy'] in agents:
            filer_ids.append(r['filedBy'])
    ids = [scrum_master_id] + [f for f in filer_ids if f != scrum_master_id
                               and not (agents.get(f) or {}).get('busy')]
    return ids


def _start_refinement(state, team_id, req_ids, scrum_master_id, now_ms, pos_offset=0):
    """Convene the backlog-refinement ceremony at the Command Center: snapshot
    each attendee's prior state into `pending['people']`, mark them busy at a
    spread, and schedule the resolve REFINEMENT_MEET_MS later. Returns True if
    convened (attendees healthy), False to defer and retry next pass.

    `pos_offset` spreads simultaneous per-team ceremonies apart at the Command
    Center (each team is its own ceremony slot; the shared room must not fully
    overlap its attendees' positions -- see _refinement_step)."""
    ids = _refinement_attendees(state, req_ids, scrum_master_id)
    if not ids:
        return False
    agents = state.get('agents') or {}
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
    state.setdefault('pendingRefinements', {})[team_id] = {
        'at': now_ms + REFINEMENT_MEET_MS, 'embarked': True,
        'scrumMasterId': scrum_master_id, 'reqIds': req_ids, 'people': snap,
        'teamId': team_id,
    }
    _log_governance(state, scrum_master_id, 'refinement',
                    {'action': 'meeting_start', 'requests': req_ids})
    return True


def _restore_refinement_agent(state, aid, snap, now_ms, duration_ms):
    """Return an attendee to their pre-ceremony state. A mid-task worker resumes
    the same task with its work budget extended by the ceremony length; an
    off-duty agent returns to off-duty (vanished); an idle on-duty one returns to
    idle. Mirrors _restore_social_agent exactly."""
    agents = state.get('agents') or {}
    a = agents.get(aid)
    if not a:
        return
    a['dir'] = snap.get('dir')
    a['x'] = snap.get('x'); a['y'] = snap.get('y')
    if snap.get('offDuty'):
        a['offDuty'] = True; a['visible'] = False
        a['task'] = None; a['busy'] = False; a['inRoom'] = None
        return
    if snap.get('task'):
        t = (state.get('tasks') or {}).get(snap['task'])
        a['task'] = snap['task']; a['busy'] = True; a['inRoom'] = snap.get('inRoom')
        a['visible'] = snap.get('visible')
        if t and t.get('status') in ('walking', 'working'):
            t['workUntil'] = (t.get('workUntil') or 0) + (duration_ms // 1000)
        return
    a['task'] = None; a['busy'] = False; a['inRoom'] = None
    a['visible'] = snap.get('visible'); a['offDuty'] = snap.get('offDuty')


def _reassign_rolled_over_cards(state, team_id):
    """Refinement re-plan (sprint rollover): a card that rolled over from one of
    THIS team's closed sprints (still queued, tagged with a closed sprint whose
    teamIds include this team) is re-assigned to the team's least-loaded free
    member -- the carried-over work gets a fresh, explicit owner for the next
    sprint instead of silently resuming with whoever the global round-robin last
    pointed it at. The pin is soft (see _assign_due_item): honored while the
    target is free, else the generic pool picks up. Deterministic, no model
    call. Returns the count of cards re-planned."""
    team_director = None
    for t in (state.get('teams') or []):
        if t.get('id') == team_id or t.get('directorId') == team_id:
            team_director = t.get('directorId')
            break
    members = _sim_direct_reports(state, team_director) if team_director else []
    agents = state.get('agents') or {}
    members = [m for m in members if agents.get(m)]
    if not members:
        return 0
    closed_ids = {s.get('id') for s in (state.get('sprints') or {}).values()
                  if s.get('status') == 'closed' and team_id in (s.get('teamIds') or [])}
    if not closed_ids:
        return 0
    tasks = state.get('tasks') or {}
    picked = []
    for it in (state.get('workQueue') or []):
        if it.get('sprintId') not in closed_ids:
            continue
        if any(t.get('status') == 'done' and t.get('title') == it.get('title')
               and t.get('room') == it.get('room')
               for t in tasks.values()):
            continue
        picked.append(it)
    if not picked:
        return 0
    free = [m for m in members
            if not (agents[m].get('busy') or agents[m].get('offDuty'))]
    if not free:
        return 0
    # Least-loaded first: fewest open tasks, tie-break by roster order.
    def _load(m):
        return sum(1 for t in tasks.values()
                   if t.get('assignedTo') == m
                   and t.get('status') in ('walking', 'working'))
    free.sort(key=lambda m: (_load(m), members.index(m)))
    target = free[0]
    for it in picked:
        it['_reassignedTo'] = target
    return len(picked)


def _queue_refined_meeting(state, req, pending):
    """Groom a MEETING card out of a refinement request: resolve the
    facilitator (the named facilitator if they're a real roster agent,
    else the filer, else the grooming scrum master) and the attendee
    list (explicit roster ids, or a 'team'/'all'/'pair' scope resolved
    against the card's team -- see _resolve_meeting_attendees), then
    queue the meeting task. The facilitator is who the minutes task
    pins to (_assign_due_item wakes even an off-duty facilitator), so a
    named scribe convenes the meeting regardless of the team's load.
    The spend ceiling + band were already priced onto the request by
    the groomer's budget calc."""
    roster_ids = {d.get('id') for d in (state.get('agentRoster') or [])
                  if d.get('id')}
    filed_by = req.get('filedBy')
    scrum_master_id = pending.get('scrumMasterId')
    facilitator = req.get('facilitatorId') or None
    if facilitator not in roster_ids:
        facilitator = filed_by if filed_by in roster_ids else scrum_master_id
    attendees = _resolve_meeting_attendees(state, req.get('attendees'),
                                           req.get('teamId'), facilitator)
    agenda = f"Filed by {filed_by} during backlog refinement. {req.get('reason')}"
    story = req.get('userStory')
    if story:
        agenda += f"\n\nUser story: {story}"
    never = req.get('never')
    if never:
        agenda += f"\n\nNEVER:\n{never}"
    queue_work(state, [{
        'title': req['title'],
        'room': req.get('room') or 'pressoffice',
        'goal': req.get('title'),
        'instructions': agenda,
        'teamId': req.get('teamId'),
        'issueKey': req.get('issueKey') or None,
        'taskType': 'meeting',
        'attendees': attendees,
        'facilitatorId': facilitator,
        'budgetUsd': req.get('budgetUsd'),
        'budgetBand': req.get('budgetBand'),
        'lane': req.get('lane'),
        'priority': WORK_PRIORITY['low'],
        'playerAuthored': bool(filed_by == 'player'),
    }])
    _log_governance(state, facilitator, 'meeting_scheduled', {
        'request': req.get('id'),
        'issueKey': req.get('issueKey') or None,
        'teamId': req.get('teamId'),
        'title': (req.get('title') or '')[:120],
        'facilitatorId': facilitator,
        'attendees': attendees,
    })


def _refinement_assigned_worker(state, req, filed_by, pending):
    """The worker a refining card should stay assigned to, or None.

    'player' is always preserved. An agent id is preserved only when a
    director or the team's scrum master filed the request and named a real
    non-director worker. Otherwise the normal round-robin assignment stands.
    """
    assigned = (req.get('assignedTo') or '').strip()
    if assigned == 'player':
        return 'player'
    if not assigned:
        return None
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])
              if isinstance(d, dict)}
    target = roster.get(assigned)
    filer = roster.get(filed_by) or {}
    team = _team_row(state, req.get('teamId') or pending.get('teamId'))
    is_lead = bool(filer.get('isDirector') or filer.get('isAdmin')
                   or (team and (team.get('scrumMasterId') == filed_by
                                 or team.get('directorId') == filed_by)))
    # A director may name another director for a direct, non-code ask (spike,
    # research, report, meeting). The old blanket ban sent those cards back to
    # team round-robin, and an all-dormant team could then abandon them.
    card_type = (req.get('issueType') or req.get('taskType') or '').strip().lower()
    code_like = card_type in governance.CODE_TASK_TYPES or card_type in ('story', '')
    direct_ok = card_type in ('spike', 'research', 'report', 'meeting', 'supervise')
    if target and is_lead and (
            (not target.get('isDirector') and not target.get('isAdmin'))
            or direct_ok):
        return assigned
    # A player-filed request is explicit work. Preserve its named worker even
    # when the player is not a roster "lead". The one exception is a code card
    # pinned to a director/admin, who cannot work code by policy.
    if target and filed_by == 'player':
        if (not (target.get('isDirector') or target.get('isAdmin'))
                or not code_like):
            return assigned
    return None


def _resolve_refinement(state, pending, now_ms, decider=None, reader=None):
    """When the ceremony window elapses, the scrum master grooms every pending
    request: the injectable refinement decider picks accept/reject per request,
    accepted ones become real `queue_work` stories (source:'refinement' +
    groomedBy), and everyone returns to their prior state. Before judging, the
    injectable comprehension reader (comprehension.py) reads each request and
    writes a short structured read-back: it is threaded into the decider's
    instructions and the refinement digest, and it is CONTEXT for the judgment
    -- a needs-clarification read-back never mechanically rejects a request.
    Marks the requests accepted/rejected, resets the cadence stamp, logs the
    digest, pops the pending record. Idempotent if a request vanished."""
    decider = decider or _refinement_decider
    agents = state.get('agents') or {}
    scrum_master_id = pending.get('scrumMasterId')
    scrum_master = (agents.get(scrum_master_id) or {})
    sm_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == scrum_master_id), {})
    sm_name = sm_def.get('name') or scrum_master.get('name') or scrum_master_id
    # Pre-refinement comprehension (ask 2): before the decider
    # judges, read every request this ceremony will groom and
    # record what the scrum master understood -- a short summary,
    # the readiness verdict, and the gaps. Stored on the pending
    # record so the digest (and any later pass) can read it
    # back, and logged as governance.
    due_requests = [req for req in (state.get('backlogRequests') or [])
                    if req.get('id') in pending.get('reqIds', [])
                    and req.get('status') == 'pending']
    comprehensions = {request_id: comp.to_dict() for request_id, comp in
                      comprehension.comprehend_batch(due_requests,
                                                   reader=reader).items()}
    pending['comprehensions'] = comprehensions
    if comprehensions:
        _log_governance(state, scrum_master_id, 'refinement_comprehension',
                        {'team': pending.get('teamId'),
                         'comprehensions': comprehensions})
    accepted = []
    for req in (state.get('backlogRequests') or []):
        if req.get('id') not in pending.get('reqIds', []):
            continue
        if req.get('status') != 'pending':
            continue
        filed_by = req.get('filedBy')
        author = (agents.get(filed_by) or {})
        pc = req.get('playerConstraints') or {}
        # Player pins are CONTEXT for the scrum master's judgment, not
        # mechanical overrides -- woven in the same way the reason text is.
        pin_bits = []
        if pc.get('budgetUsd'):
            pin_bits.append(f"The player named a total budget of ${pc['budgetUsd']:.2f} "
                            "for this work -- factor it into whether/how to card it.")
        if pc.get('constraints'):
            pin_bits.append('The player listed these hard prohibitions: '
                            + '; '.join(pc['constraints']) + '.')
        if pc.get('roles'):
            pin_bits.append('The player prefers this card to suit these roles/skills: '
                            + '; '.join(pc['roles']) + '.')
        if pc.get('mustInclude'):
            pin_bits.append('The player named these tasks as important to cover: '
                            + '; '.join(pc['mustInclude']) + '.')
        pin_text = (' ' + ' '.join(pin_bits)) if pin_bits else ''
        card_type = (req.get('issueType') or req.get('taskType') or 'story').strip().lower()
        instructions = (f"{sm_name} is the scrum master running backlog refinement for {filed_by}'s "
                        f"({author.get('name') or filed_by} / {author.get('role') or 'worker'}) work request "
                        f"in the {req.get('room')} room. Requested card: '{req.get('title')}'. "
                        f"Card type: {card_type}. Reason: {req.get('reason')}.{pin_text}")
        ctx = _refinement_context_for_room(state, req.get('room'))
        if ctx:
            instructions += f" Roadmap context for this room (prioritization only, not a scope gate): {ctx}."
        instructions += (" The room is where the work happens, not a scope gate. A spike or research "
                         "card can run in pressoffice; a low room trailing grade is context for "
                         "prioritization, never a reason by itself to reject a player-filed investigation.")
        if filed_by == 'player':
            instructions += (" The player explicitly asked for this card; default to accept unless it is "
                             "a duplicate, unsafe, or clearly outside the tank's charter.")
        # A reporter appeal rides the record: the reply text is the exact new
        # information the card came back with, so the ceremony judges it in
        # the open instead of past the requester.
        if req.get('appeal'):
            instructions += (f" APPEAL: the reporter appealed this card's earlier decline "
                             f'with this new information: "{req["appeal"]}" '
                             f'(one appeal per card — a second decline closes the '
                             f'reply path).')
        # The scrum master's own read-back of this request,
        # written down before the judgment: what they understood
        # of it, and whether it looks ready to card or needs
        # clarification first.
        read_back = comprehensions.get(req.get('id'))
        if read_back:
            instructions += (f" Read-back: \"{read_back['summary']}\" "
                             f"(readiness: {read_back['readiness']}).")
        instructions += (" Turn it into a real card (the card type says story, spike, or research) "
                         "or groom it back to the requester because it's not ready / not needed / "
                         "doesn't fit the roadmap (reject). "
                         + ABSOLUTE_ZERO_SCOPING_GUIDANCE)
        criteria = [
            {'id': 'accept', 'description': f"Create '{req.get('title')}' as a real {card_type} card in {req.get('room')} -- it's a genuine, well-scoped, MEDIUM-difficulty gap worth a card."},
            {'id': 'accept_generous', 'description': f"Create '{req.get('title')}' as a real {card_type} card in {req.get('room')} -- it's a genuine, well-scoped, HIGH-VALUE gap worth a card AND a generous spend budget (a bigger investigation or deliverable than the standard card)."},
            {'id': 'reject', 'description': "Groom this request out -- it's a duplicate, low-value, trivial/filler, over-ambitious/un-scoped, out-of-scope, or the roadmap already covers it."},
        ]
        suppression = _card_pattern_suppressed(
            state, {'issueKey': req.get('issueKey'), 'title': req.get('title')}, now_ms)
        if suppression:
            # The same issue/pattern already failed three times in 24h. Do not
            # mint another card from it: the contract is wrong, and a fourth
            # copy only repeats the failure.
            choice = 'reject'
        else:
            choice = decider(instructions, criteria)
            if choice is None:
                # Outage fallback: accept only a request that names a real
                # delegatable-room gap (same determinism as the governance fallback --
                # better to ship one well-scoped card than to silently drop a filed gap).
                choice = 'accept' if req.get('room') in VALUED_QUEUE_ROOMS else 'reject'
        # Item 4: the grooming choice also picks the spend band. 'accept_generous'
        # (a HIGH-VALUE story) doubles the type-default ceiling; 'accept' is the
        # standard band; the rejected branch never touches it. An explicit
        # req['budgetUsd'] (player-named, from file_issue) always wins over the
        # band -- the band only scales the TYPE default, never an explicit number.
        band = 'generous' if choice == 'accept_generous' else 'standard'
        req['budgetBand'] = band
        # A meeting card prices off the 'meeting' tier -- a meeting
        # issue carries no taskType (file_issue leaves it unset).
        groom_type = ('meeting'
                      if (req.get('issueType') or '').strip().lower() == 'meeting'
                      else (req.get('taskType') or 'code'))
        req['budgetUsd'] = budget_usd_for_task(
            groom_type,
            req.get('budgetUsd'),
            band,
            # Wave 3: the room/team/product/type's overrun history
            # scales the type-derived ceiling (never an explicit one).
            _overrun_factor_for(state, req.get('room'), req))
        if choice in ('accept', 'accept_generous'):
            req['status'] = 'accepted'
            meeting_card = (req.get('issueType') or '').strip().lower() == 'meeting'
            if meeting_card:
                # Meeting cards are scheduled directly (facilitator +
                # resolved attendees), not via the generic story queue
                # below -- their minutes close the card.
                _queue_refined_meeting(state, req, pending)
            # queue_work whitelists fields, so the provenance (filer/groomer/
            # room/title) lives on the durable `backlogRequests` record + the
            # digest -- not on the queue item. teamId is the one exception
            # _assign_due_item needs it to PREFER this team at
            # assignment time (see queue_work's own comment on the field) --
            # req['teamId'] is already known here (the grooming ceremony
            # itself is per-team), it just never survived onto the real task.
            # A player-filed JIRA card (req carries the normalized userStory +
            # acceptanceCriteria from file_issue) is handed to the WORKER as the
            # full contract: the story and criteria are embedded in the task's
            # instructions (the coding executor builds its backlog line from
            # them), and forwarded as structured fields through queue_work /
            # _assign_due_item / assign_task so they survive onto the task.
            story = req.get('userStory')
            criteria = req.get('acceptanceCriteria')
            never = req.get('never')
            instructions = (f"Filed by {filed_by} during backlog refinement. {req.get('reason')}")
            if story:
                instructions += f"\n\nUser story: {story}"
            if criteria:
                instructions += f"\n\nAcceptance criteria:\n{criteria}"
            if never:
                instructions += f"\n\nNEVER:\n{never}"
            # Dependency cascade provenance: a card whose ISSUE was filed blocked
            # on another task (issue['dependsOnTask']) carries that dependency
            # onto its real task, so re-opening the dependency ripples a
            # re-review to this story once it has shipped (see _cascade_rereview).
            depends_on = None
            linked_issue = (state.get('issues') or {}).get(req.get('issueKey'))
            if linked_issue and linked_issue.get('dependsOnTask'):
                depends_on = linked_issue['dependsOnTask']
            if not meeting_card:
                assigned_worker = _refinement_assigned_worker(
                    state, req, filed_by, pending)
                directed = bool(assigned_worker and assigned_worker != 'player')
                # The issue type IS the lane. Without this, a spike/jira card
                # defaulted to 'code', directors were filtered out, and a
                # directed pin was silently dropped.
                queue_task_type = (card_type if card_type in
                                   ('spike', 'research', 'meeting', 'review', 'qa', 'bug')
                                   else 'code')
                # An explicit report deliverable needs a lane that writes a
                # report, even when the issue was typed as a bug/story. This is
                # the mint-time half of the contract; the completion gate reads
                # the same field.
                if req.get('deliverable') == 'report' and queue_task_type not in ('spike', 'report', 'research'):
                    queue_task_type = 'spike'
                queue_work(state, [{
                    'title': req['title'], 'room': req['room'], 'goal': req.get('title'),
                    'instructions': instructions,
                    'taskType': queue_task_type,
                    'deliverable': req.get('deliverable') or None,
                    'approvedTools': list(req.get('approvedTools') or []),
                    'teamId': req.get('teamId'),
                    'userStory': story,
                    'acceptanceCriteria': criteria,
                    'never': never,
                    'dependsOn': depends_on,
                    'issueKey': req.get('issueKey') or None,
                    # A chief-of-staff directive marks the request high priority
                    # so the groomed card is picked before ordinary backlog work.
                    'priority': req.get('priority'),
                    # Human-in-the-loop: a card the filer marked for the PLAYER
                    # (assignedTo 'player') stays the player's -- _assign_due_item
                    # hands it to the player instead of an agent. A director or
                    # scrum master may also name a worker; that assignment is
                    # preserved so the SM creates the card AND staffs it, instead
                    # of telling the player to find a worker.
                    'assignedTo': assigned_worker,
                    # An explicit director/lead pin is not a suggestion. It
                    # wakes that worker and waits for them instead of falling
                    # through to round-robin or abandoning the card.
                    'directedTo': directed,
                    # The card's attention lane survives grooming onto the queue
                    # (queue_work normalizes), so a build/reading/parking-lot card
                    # keeps its lane through refinement.
                    'lane': req.get('lane'),
                    # Item 4: the groomer-judged spend ceiling + band ride the queue
                    # item (whitelisted) so the ASSIGNED task carries them.
                    'budgetUsd': req.get('budgetUsd'),
                    'budgetBand': req.get('budgetBand'),
                    # Player-authored provenance: a backlog request the PLAYER filed
                    # (filedBy 'player' -- the player chat lane, or a player-filed
                    # JIRA issue) is player-vetted; every agent-filed request is
                    # not. Threaded so the JEV gate's work-context bypass trusts
                    # only these tasks.
                    'playerAuthored': bool(filed_by == 'player'),
                }])
            accepted.append(req)
            # BP-62: triage mail -- the reporter learns their card entered a
            # sprint. Self-filed cards (the SM grooming their own request)
            # stay quiet.
            if filed_by != scrum_master_id:
                _mail_card_update(
                    state, filed_by, 'request_carded', req.get('issueKey'),
                    req.get('title'),
                    (_team_row(state, pending.get('teamId')) or {}).get('name'))
        else:
            req['status'] = 'rejected'
            # BP-62: the decline is also the response the requester asked for.
            if filed_by != scrum_master_id:
                _mail_card_update(
                    state, filed_by, 'request_declined', req.get('issueKey'),
                    req.get('title'),
                    (_team_row(state, pending.get('teamId')) or {}).get('name'),
                    extra=('suppressed: this issue/pattern already failed '
                           'three times in 24h; fix the contract before re-filing'
                           if suppression else
                           'judged duplicate, trivial, out of scope, or '
                           'already covered by the roadmap'))
        details = {'request': req['id'], 'choice': choice, 'filedBy': filed_by,
                   'room': req['room'], 'title': req['title'][:120]}
        if suppression:
            details['patternSuppressed'] = suppression
        if choice == 'reject' and not suppression:
            # Absolute Zero rejection signal: a self-proposed work-request was
            # groomed OUT. Durable, queryable (the health check counts these) --
            # a rising reject rate is the think tank's own early warning that its
            # proposals are trending trivial/ill-scoped.
            details['selfProposedRejected'] = True
        _log_governance(state, scrum_master_id, 'refinement_carryaway', details)
    # Sprint rollover re-plan: cards that rolled over from this team's closed
    # sprint are re-assigned to a fresh, least-loaded free member so the carried
    # work has an explicit owner for the next sprint.
    team_id = pending.get('teamId')
    if team_id:
        replanned = _reassign_rolled_over_cards(state, team_id)
        if replanned:
            _log_governance(state, scrum_master_id, 'refinement_carryaway',
                            {'action': 'rollover_reassign', 'team': team_id,
                             'reassigned': replanned})
    # Restore attendees; reset the cadence stamp event-to-event. Like the Social,
    # the work budget given back is the full ceremony length, so the meeting
    # burned none of a mid-task attendee's work time.
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, REFINEMENT_MEET_MS)
    if team_id:
        state.setdefault('teamRefinementAt', {})[team_id] = now_ms
    else:
        state['lastBacklogRefinementAt'] = now_ms
    from serve import log_refinement_digest
    try:
        log_refinement_digest(state, agents, {'scrumMasterId': scrum_master_id,
                                               'accepted': accepted,
                                               'comprehensions': comprehensions})
    except Exception:
        pass
    state.setdefault('pendingRefinements', {}).pop(team_id, None) if team_id else state.pop('_pendingRefinement', None)


def _refinement_cadence_due_for(state, team_id, now_ms, legacy=None):
    """Is team `team_id`'s weekly refinement window due? Reads the per-team stamp
    map (or the legacy flat stamp while converting a cold state). Treats a
    far-future stale TEST sentinel as unset."""
    stamps = state.get('teamRefinementAt') or {}
    last = stamps.get(team_id)
    if last is None:
        last = legacy if legacy is not None else 0
    if last - now_ms > 10 * 365 * 24 * 3600 * 1000:
        last = 0
    return now_ms - last >= team_policy(state, team_id, 'refinementCadenceMs')


def kick_refinement_now(state, team_id, now_ms=None):
    """Make a team's backlog refinement due on the VERY next pass instead of
    waiting for the weekly cadence -- large requests kick refinement
    the moment they arrive). Safe against the kick + weekly-cadence double-groom
    trap: it is a no-op if the team already has an in-flight ceremony (that
    ceremony will groom the new request) or its cadence is already due (the next
    pass will pick it up anyway). Returns True if the kick armed, False if it
    was already covered."""
    if not team_id:
        return False
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    # In-flight ceremony already covers this team -- don't stack a second one.
    if team_id in (state.get('pendingRefinements') or {}):
        return False
    if _refinement_cadence_due_for(state, team_id, now_ms):
        return False  # already due -- next pass picks it up
    state.setdefault('teamRefinementAt', {})[team_id] = 0
    return True


def _refinement_step(state, now, now_ms, decider=None, reader=None):
    """One backlog-refinement pass, called from _task_cycle ungated (each team's
    ceremony is the point even on a quiet think tank). EVENT-DRIVEN: there is no
    weekly cadence gate -- a ceremony convenes as soon as a team has pending
    requests, a free+on-duty facilitator (see _ceremony_facilitator), and no
    active sprint. Per-team ceremonies run CONCURRENTLY: each team has its OWN
    ceremony slot (`pendingRefinements[team_id]`), so one team no longer blocks
    another at the shared Command Center. A pass (a) advances every in-flight
    ceremony (embark on one pass, RELEASE on the next -- no required fixed
    hold; the facilitator is RE-RESOLVED at embark so a stored id that has gone
    busy never blocks), and (b) convenes a new ceremony for every team with
    pending requests AND a free facilitator. Never convenes an empty meeting;
    defers a team in an active sprint (it refines at close)."""
    pending_map = state.setdefault('pendingRefinements', {})
    # (a) Advance every in-flight ceremony. Embark on one pass, then RELEASE on
    # the immediately-following pass -- the scrum master groomed everything
    # synchronously in the resolve, so there's no clock to wait out. The
    # facilitator is re-resolved HERE so a stored scrum master who has gone
    # busy/off-duty is swapped for the team's own/borrowed director instead of
    # stranding the embarked meeting.
    for team_id in list(pending_map.keys()):
        pending = pending_map[team_id]
        if not pending.get('embarked'):
            facilitator = _ceremony_facilitator(state, team_id)
            if not facilitator:
                continue  # no free+on-duty facilitator right now -- defer a pass
            _start_refinement(state, team_id, pending.get('reqIds', []),
                              facilitator, now_ms)
        else:
            _resolve_refinement(state, pending, now_ms, decider=decider,
                                reader=reader)
    # (b) Convene a ceremony for every team that has groomable requests and a
    # free facilitator. Each team gets its own slot, so multiple teams can
    # refine in the same pass (spread apart in the room).
    slot = 0
    for team in (state.get('teams') or []):
        team_id = team.get('id') or team.get('directorId')
        if team_id in pending_map:
            continue  # this team already has an in-flight ceremony
        facilitator = _ceremony_facilitator(state, team_id)
        if not facilitator:
            continue  # this team has no free+on-duty facilitator right now
        if _team_in_active_sprint(state, team_id):
            # WS-14: a team in an active sprint refines at CLOSE, never
            # mid-sprint -- the sprint is the committed work.
            continue
        # This team's OWN pending requests -- a ceremony must never groom a
        # request that isn't filed by one of THIS team's members (per-team
        # isolation: dev's scrum master refines only dev's cards).
        req_ids = []
        for r in _pending_work_requests(state):
            if r.get('breakdown'):
                # Large-request breakdowns are groomed by the BREAKDOWN ceremony
                # (the receiving team's scrum master + workers plan the stories),
                # never by this refinement groom -- two ceremonies, two kinds.
                continue
            t = _refinement_team(state, r)
            if t and (t.get('id') == team_id or t.get('directorId') == team_id):
                req_ids.append(r['id'])
        if not req_ids:
            continue  # nothing to groom -- don't convene an empty meeting
        req_ids = req_ids[:REFINEMENT_MAX_REQUESTS]
        state.setdefault('teamRefinementAt', {})[team_id] = now_ms
        pending_map[team_id] = {
            'at': now_ms + REFINEMENT_MEET_MS, 'embarked': False,
            'scrumMasterId': facilitator, 'reqIds': req_ids,
            'teamId': team_id, 'people': {},
        }
        slot += 1  # only to spread simultaneous ceremonies apart in the room


# ---------------------------------------------------------------------------
# Sprint staffing: the breakdown ceremony. A staffable large ask is filed as a
# pending breakdown request (origin:'large_request', breakdown:True) by the free
# authority, tagged with the receiving team. That team's OWN breakdown ceremony
# -- scrum master + the team's workers -- meets at the Command Center and cards
# the ask into concrete stories/spikes, which are queued as REAL work tagged
# with the team + the goal. No sprint record is created up front and no subtasks
# are predicted synchronously: the team plans its own work, like a real org.
# Same convene-then-resolve ceremony shape as refinement; the decider is
# injectable like every other Jev decision, and the ask is never dropped -- an
# outage degrades to a single card titled the goal.
# ---------------------------------------------------------------------------


def _pending_breakdown_requests(state, team_id):
    """Pending large-request breakdowns targeted at `team_id` (breakdown:True
    records only) -- the breakdown ceremony's intake. Distinct from the
    refinement ceremony's `_pending_work_requests`, which must never groom
    these: the two ceremonies own different request kinds."""
    return [r for r in (state.get('backlogRequests') or [])
            if r.get('status') == 'pending' and r.get('breakdown')
            and r.get('teamId') == team_id]


def _breakdown_attendees(state, req_ids, scrum_master_id, team_id):
    """The breakdown ceremony's attendees: the team's scrum master + its
    non-director workers (the people who will WORK the stories -- a breakdown is
    a team planning session). The director attends only when she IS the
    effective scrum master (small teams where the director stands in). A busy
    attendee defers the whole ceremony (never pull an agent out of a live
    collaboration); an off-duty one is parked and free, and _start_breakdown
    wakes her (snapshot + restore, exactly like refinement). Returns the
    attendee ids, or None if any attendee is busy."""
    agents = state.get('agents') or {}
    ids = [scrum_master_id]
    t = _team_row(state, team_id) or {}
    director_id = t.get('directorId')
    for aid in _sim_direct_reports(state, director_id):
        if aid not in ids and aid != director_id:
            ids.append(aid)
    for aid in ids:
        a = agents.get(aid)
        if not a or a.get('busy'):
            return None
    return ids
