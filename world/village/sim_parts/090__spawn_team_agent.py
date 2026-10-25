

def _spawn_team_agent(state, name, role, director_id, now_ms, grid, is_director=False,
                      elevated=False):
    """Create one brand-new agent record (roster entry + live agent) for a team
    spawned on a large-request. Mirrors _complete_auto_hire's
    structure: joins on foot at a free outdoor spot if the ACTIVE ceiling has
    room, else joins the dormant inventory. Returns the new agent id, or None
    if the name pool is exhausted."""
    new_id = name.lower()
    color_pool = HIRE_COLOR_POOL
    color = color_pool[random.randrange(len(color_pool))]
    # Reentry guard: a fired employee can never reenter under any hire path.
    if _is_fired(state, new_id, name):
        return None
    profile = {
        'mission': f"Part of the {director_id} team, taking on the think tank's newest large request.",
        'instructions': [
            f"Report to {director_id}; your work lands in the {director_id} team's shared space.",
            "You were brought in when every existing team was busy in a sprint -- your team owns the incoming large request.",
        ],
        'notes': ["Created 2026-09-27 as part of a new team for an incoming large request."],
    }
    roster_entry = {
        'id': new_id, 'name': name, 'color': color, 'role': role, 'model': 'small',
        'director': None if is_director else director_id,
        'villageId': village_of_agent(state, director_id),
        'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
        'mailbox': [f"Welcome aboard -- you're on the new {director_id} team."],
        'elevatedAccess': elevated, 'accessGrant': None, 'profile': profile,
    }
    if is_director:
        roster_entry['isDirector'] = True
        roster_entry['directorSince'] = time.time()
    state.setdefault('agentRoster', []).append(roster_entry)
    can_activate = can_activate_another(state)
    spot = _free_outdoor_spot(state, grid)
    agents = state.setdefault('agents', {})
    agents[new_id] = {
        'id': new_id, 'name': name, 'color': color, 'role': role, 'profile': profile,
        'model': 'small', 'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
        'villageId': village_of_agent(state, director_id),
        'mailbox': [{'text': f"Welcome aboard -- you're on the new {director_id} team.", 'read': False, 'ts': now_ms}],
        'conversationLog': [], 'lastContactedAt': None, 'hiredAt': now_ms,
        'elevatedAccess': elevated, 'accessGrant': None,
        'inRoom': None, 'roomX': None, 'roomY': None,
        'x': spot['x'], 'y': spot['y'], 'dir': 'south',
        'visible': can_activate, 'busy': False, 'meetingId': None,
        'offDuty': not can_activate,
    }
    return new_id


def _estimate_employees_for_request(goal):
    """How many employees a new team should start with for a large request
    Heuristic, not a science: breadth-of-ask signals (word count,
    explicit scoping verbs like 'build/create/platform/website') scale the
    starting headcount so a genuinely large ask isn't bottlenecked by a single
    hire. Bounded to a sane small-team range (1..3) -- a big team grows via the
    normal auto-hire loop rather than spawning a dozen agents at once."""
    text = (goal or '').strip()
    if not text:
        return 1
    words = len(text.split())
    scope_verbs = ('build ', 'create ', 'develop ', 'platform', 'website', 'app ',
                   'system', 'suite', 'full ', 'complete ', 'end-to-end')
    hits = sum(1 for v in scope_verbs if v in text.lower())
    n = 1
    if words >= 40 or hits >= 2:
        n = 2
    if words >= 120 or hits >= 4:
        n = 3
    return n


def spawn_new_team_for_request(state, goal, now_ms=None, admin_id=None, employees=None,
                               chooser=None, constraints=None):
    """Create a brand-new team to take an incoming large request when every
    existing team is already busy in a sprint. Spawns a new
    DIRECTOR (reporting to the admin), a fresh team record, one EMPLOYEE (or
    `employees` if more help is needed), and leaves the scrum-master role to
    the size rule: a brand-new team is small, so the DIRECTOR stands in as the
    effective scrum master (_refinement_scrum_master_for_team falls back to the
    director below SCRUM_MASTER_MIN_TEAM_SIZE) -- the employee and director
    handle ceremonies until the team grows. Then files the goal as a pending
    backlog request for the new team and kicks its refinement to be due on the
    very next pass, so the new team starts refining the large ask immediately
    rather than waiting for the weekly cadence. Names come from `chooser`
    (defaults to _hire_name_chooser): the ADMIN names the new director, and the
    new DIRECTOR names each employee -- no predetermined names, all-time
    unique. Returns the new team record, or None if the name pool / agent caps
    are exhausted."""
    chooser = chooser or _hire_name_chooser
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    roster = state.get('agentRoster') or []
    if len(roster) >= MAX_TOTAL_AGENTS:
        return None
    admin_id = admin_id or next((d.get('id') for d in roster if d.get('isAdmin')), None)
    if not admin_id:
        return None
    admin_def = next((d for d in roster if d.get('id') == admin_id), {})
    admin_name = admin_def.get('name') or admin_id
    director_name = chooser(state, admin_name, _all_used_names(state), 'Director') or _next_new_team_name(state)
    if not director_name:
        return None
    _remember_name(state, director_name)
    director_id = director_name.lower()
    grid, _doors = _load_outdoor_geometry()
    _spawn_team_agent(state, director_name, 'Director', director_id, now_ms, grid,
                      is_director=True, elevated=True)
    # New director reports to the admin so authority still resolves upward.
    # The new agent is isDirector=True, so an admin -> director pointer is the
    # legal R2 shape; the guard documents that and protects the invariant.
    dir_roster = next((d for d in state['agentRoster'] if d.get('id') == director_id), None)
    if dir_roster:
        import serve as _serve_mod
        dir_roster['director'] = _serve_mod._safe_director(state, director_id, admin_id)
    # Team record -- the director is the id (matches _promote_to_director).
    teams = state.setdefault('teams', [])
    team = {
        'id': director_id,
        'name': f"{director_name.title()}'s Crew",
        'directorId': director_id,
        'purpose': "New team created on 2026-09-27 to take a large request while existing teams were busy in sprints.",
        'members': [],
        'createdAt': time.time(),
        'createdForRequest': (goal or '')[:200],
    }
    teams.append(team)
    member_ids = []
    emp_count = _estimate_employees_for_request(goal) if employees is None else max(1, int(employees or 1))
    for i in range(emp_count):
        # The new DIRECTOR names each employee (they're the authority for the
        # team) -- chooser falls back to the pool on a model outage.
        emp_name = chooser(state, director_name, _all_used_names(state), 'Engineer') or _next_new_team_name(state)
        if not emp_name:
            break
        _remember_name(state, emp_name)
        emp_id = _spawn_team_agent(state, emp_name, 'Engineer', director_id, now_ms, grid)
        member_ids.append(emp_id)
    team['members'] = member_ids
    # File the goal as this new team's first backlog request.
    req = {
        'id': f"wrq-{len(state.get('backlogRequests') or []) + 1}",
        'filedBy': director_id, 'title': (goal or 'Large request')[:120],
        'room': 'pressoffice',
        'reason': f"Incoming large request -- new team {director_id} was spun up to take it.",
        'filedAt': now_ms, 'status': 'pending',
        'origin': 'large_request', 'teamId': director_id,
    }
    # Optional player pins ride the request so this team's refinement sees
    # them (same shape as file_large_request's; absent = plan freely).
    if isinstance(constraints, dict) and constraints:
        pinned = {}
        budget = constraints.get('budgetUsd')
        if isinstance(budget, (int, float)) and budget > 0:
            pinned['budgetUsd'] = float(budget)
        for key in ('constraints', 'roles', 'order', 'mustInclude'):
            val = constraints.get(key)
            if isinstance(val, (list, tuple)):
                cleaned = [str(v).strip()[:200] for v in val if str(v).strip()]
                if cleaned:
                    pinned[key] = cleaned[:20]
        if pinned:
            req['playerConstraints'] = pinned
    state.setdefault('backlogRequests', []).append(req)
    # Kick this team's refinement to be immediately due (next pass), not weekly.
    kick_refinement_now(state, director_id, now_ms)
    _log_governance(state, director_id, 'new_team_for_request',
                    {'team': director_id, 'members': member_ids, 'request': req['id']})
    # Every new team member goes through the same onboarding ceremony as an
    # auto-hire. The existing admin leads, because the new director may still
    # be dormant under the active-agent ceiling. One record is live and the
    # rest queue behind it.
    team_ids = [director_id] + [m for m in member_ids if m]
    for new_id in team_ids:
        is_director = new_id == director_id
        reports_to_id = admin_id if is_director else director_id
        reports_to_name = admin_name if is_director else director_name
        _begin_onboarding(
            state, new_id, reports_to_id, reports_to_name,
            help_for_id=admin_id if is_director else director_id,
            help_for_name=admin_name if is_director else director_name,
            coworker_ids=[x for x in team_ids if x != new_id],
            now_ms=now_ms, leader_id=admin_id, leader_name=admin_name,
        )
    return team


# Phase B: the onboard ceremony mirrors the firing ceremony's convene-then-
# resolve shape (look at _start_auto_firing_review/_resolve_firing_review) but is
# deterministic -- it stages the new agent's AGENT.md progressively rather than
# asking Jev for a verdict. Stages:
#   0 -> the hire exists; the director + who'll work with them haven't started
#        meeting (embarked by _start_onboard_meeting).
#   1 -> meeting underway; director has shared the draft role / who they report
#        to / who they co-work with.
#   2 -> director has shared the concrete work to pick up.
#   3 -> readiness hold: onboarding is NOT done until the hire has actually
#        claimed a real task (observed), or a generous wall-time timeout elapses
#        so an idle/empty think tank never strands a hire unfinalized.
#   done -> profile['onboarding'] removed; AGENTS.md finalized on next sync.
_ONBOARD_STAGES = (1, 2, 3)


def _onboard_coworker_defs(state, onboard):
    """The employees who'll work with the new hire: the hire's intended `helpFor`
    plus any of the director's other direct reports. Mirrors the "people with
    firsthand knowledge" spirit of _firing_consultation."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    out = []
    for cid in onboard.get('coworkerIds') or []:
        a = agents.get(cid)
        if not a:
            continue
        out.append({'id': cid, 'name': roster.get(cid, {}).get('name') or a.get('name') or cid})
    return out


def _start_onboard_meeting(state, onboard, now_ms):
    """Convene the Town Hall: the onboarding leader + the on-duty employees
    who'll work with the new hire meet at Command Center before the AGENT.md is
    finalized. `leaderId` lets a new-team hire be onboarded by the existing
    admin when the new director is still dormant. Busy on-duty coworkers still
    defer the embark (never strand a live collaboration reference). Off-duty
    coworkers are skipped instead of blocking: on a large roster most of the
    team is dormant, and requiring every one of them to be awake deadlocked
    the ceremony forever."""
    agents = state.get('agents') or {}
    leader_id = onboard.get('leaderId') or onboard.get('directorId')
    director = agents.get(leader_id)
    if not director or director.get('busy') or director.get('offDuty'):
        # Fallback for a new-team hire: if the admin leader is unavailable, the
        # reporting director can run the meeting. A hire never leads its own.
        fallback_id = onboard.get('directorId')
        fallback = agents.get(fallback_id) if fallback_id != leader_id else None
        if not fallback or fallback.get('busy') or fallback.get('offDuty'):
            return False
        leader_id = fallback_id
        director = fallback
        onboard['leaderId'] = leader_id
        onboard['leaderName'] = (
            next((d.get('name') for d in (state.get('agentRoster') or [])
                  if d.get('id') == leader_id), None)
            or fallback.get('name') or leader_id)
    coworkers = [c for c in _onboard_coworker_defs(state, onboard)
                 if (agents.get(c['id']) or {}).get('offDuty') is not True]
    for c in coworkers:
        a = agents.get(c['id'])
        if not a or a.get('busy'):
            return False
    # Healthy -- embark the meeting. The director + coworkers convene at the
    # same Command Center staging the firing reviewers use (spread roomXY).
    participants = [director] + [agents[c['id']] for c in coworkers]
    for i, a in enumerate(participants):
        a['busy'] = True
        a['visible'] = False
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = 315 + (i * 85)
        a['roomY'] = 155
    # Mark the meeting held on the pending record so the governance gate proceeds
    # straight to staging instead of re-embarking a fresh meeting, and push the
    # first resolve a full meet-duration past the embark (the "share the details"
    # gap before the first draft).
    onboard['embarked'] = True
    onboard['at'] = max(onboard.get('at') or 0, now_ms) + ONBOARD_MEET_DURATION_MS
    _log_governance(state, onboard['directorId'], 'onboard',
                    {'about': onboard['agentId'], 'action': 'meeting_start',
                     'directorId': onboard['directorId'],
                     'coworkers': [c['id'] for c in coworkers]})
    return True


def _onboard_hold_claimed_task(state, onboard):
    """Readiness gate: has the hire actually claimed a real co-work task yet? A
    hire is 'ready for work' the moment it holds a live task -- an agent.task
    pointer to a task in walking/working status that the hire itself still holds
    (the same 'held, not orphaned/reclaimed' notion _reclaim_orphaned_walking_tasks
    uses), and which is not simultaneously held by a different, more senior agent
    (a task someone else genuinely owns is not the hire picking up overflow).
    Pure; never decides, just reports."""
    agents = state.get('agents') or {}
    tasks = state.get('tasks') or {}
    new_id = onboard.get('agentId')
    new_agent = agents.get(new_id)
    if not new_agent:
        return False
    tid = new_agent.get('task')
    if not tid:
        return False
    task = tasks.get(tid) if isinstance(tasks, dict) else None
    if not isinstance(task, dict) or task.get('status') not in ('walking', 'working'):
        return False
    # The hire must be the one holding it (not mid-pair/handoff, not a reclaimed
    # corpse another agent took over).
    if new_agent.get('handoff') or new_agent.get('pairWith'):
        return False
    # If the task names a different assignee who still points at it, that agent
    # (not the hire) is the holder -- this isn't the hire's claimed work.
    assignee = task.get('assignedTo')
    if assignee and assignee != new_id:
        holder = agents.get(assignee)
        if holder and isinstance(holder, dict) and holder.get('task') == tid:
            return False
    return True


def _complete_onboarding(state, onboard, reason, now_ms=None):
    """Finalize a hire: keep the onboarding record for audit, drop the live
    `onboarding` marker, and clear the pending record so the next
    sync_agent_directories writes AGENTS.md. `reason` is logged on the
    governance log ('claimed' when the hire proved readiness with a real task,
    'timeout' when an idle/empty think tank hit the readiness cap)."""
    agents = state.get('agents') or {}
    profile = (agents.get(onboard.get('agentId')) or {}).get('profile') or {}
    onboarding = profile.get('onboarding')
    if isinstance(onboarding, dict):
        record = dict(onboarding)
        provisioning = dict(record.get('provisioning') or {})
        provisioning['firstTask'] = reason
        if now_ms is not None:
            provisioning['completedAt'] = int(now_ms)
        record['provisioning'] = provisioning
        record['completedAt'] = int(now_ms) if now_ms is not None else None
        record['completionReason'] = reason
        profile['onboardingRecord'] = record
    profile.pop('onboarding', None)
    _log_governance(state, onboard['directorId'], 'onboard',
                    {'about': onboard['agentId'], 'action': 'done',
                     'reason': reason, 'directorId': onboard['directorId'],
                     'coworkers': [c['id'] for c in _onboard_coworker_defs(state, onboard)]})
    state.pop('_pendingOnboard', None)


def _sim_admin_id(state):
    """The admin (the single roster entry with isAdmin=True), or None."""
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin'):
            return d.get('id')
    return None


def _work_agreement_text(state, agent):
    """The NEW HIRE drafts their own work agreement -- their words about how
    they'll work, derived from THEIR OWN profile (role, mission, access grant),
    NOT a director's instructions. This is the user's todo-17 shape: the work
    agreement is drafted BY the agents (the new hire), then EMPOWERED by the
    admin. Cheap + deterministic (no model call), mirroring the staged AGENT.md
    drafting. Returns a short plain-text agreement or None if the hire has no
    self-description to build from."""
    if not isinstance(agent, dict):
        return None
    profile = agent.get('profile') or {}
    role = agent.get('role') or ''
    mission = (profile.get('mission') or '').strip()
    grant = (agent.get('accessGrant') or '').strip()
    parts = []
    if role:
        parts.append(f"I am the {role}.")
    if mission:
        parts.append(f"My mission: {mission}")
    if grant:
        parts.append(f"My access: {grant}")
    if not parts:
        return None
    parts.append("I agree to work inside this scope: support my team's shared "
                 "space, surface what I learn, and hand work back cleanly.")
    return ' '.join(parts)


def _empower_work_agreement(state, onboard, now_ms):
    """The admin empowers the hire's drafted work agreement. Recorded on the
    hire's profile (workAgreement) + the governance log so the empower is
    durable and attributable. Returns (text, empowered_by) or (None, None) if
    there is no hire or no admin."""
    agents = state.get('agents') or {}
    new_agent = agents.get(onboard.get('agentId'))
    if not isinstance(new_agent, dict):
        return None, None
    admin_id = _sim_admin_id(state)
    text = _work_agreement_text(state, new_agent)
    if not text:
        return None, None
    profile = new_agent.setdefault('profile', {})
    profile['workAgreement'] = {
        'text': text,
        'draftedBy': onboard.get('agentId'),
        'empoweredBy': admin_id,
        'empoweredAt': now_ms,
    }
    # Provisioning checklist: mint the stable agent key now rather than waiting
    # for the first tool call, and record that the agreement is empowered.
    provisioning = profile.setdefault('onboarding', {}).setdefault(
        'provisioning', {})
    try:
        import serve as _serve
        _serve.get_or_create_agent_key(onboard.get('agentId'))
        provisioning['agentKey'] = 'issued'
    except Exception:
        provisioning['agentKey'] = 'pending'
    provisioning['workAgreement'] = 'empowered'
    _log_governance(state, onboard.get('agentId'), 'onboard',
                    {'about': onboard['agentId'], 'action': 'agreement_drafted',
                     'directorId': onboard['directorId']})
    if admin_id:
        _log_governance(state, admin_id, 'onboard',
                        {'about': onboard['agentId'], 'action': 'agreement_empowered',
                         'directorId': onboard['directorId']})
    return text, admin_id


def _resolve_onboard_meeting(state, onboard, now_ms):
    """After the Town-Hall duration elapses, the onboarding leader stages the
    new agent's AGENT.md progressively. Everyone returns to duty; the new
    agent's profile is appended to stage by stage; stage 3 is a readiness hold
    that waits for the hire to actually claim a real co-work task before the
    `onboarding` marker is removed and the next sync_agent_directories
    finalizes AGENTS.md. Idempotent if the hire vanished."""
    agents = state.get('agents') or {}
    leader_id = onboard.get('leaderId') or onboard.get('directorId')
    director = agents.get(leader_id)
    coworkers = _onboard_coworker_defs(state, onboard)
    for a in [director] + [agents.get(c['id']) for c in coworkers]:
        if a:
            a['busy'] = False
            a['visible'] = True
            a['inRoom'] = None
    new_agent = agents.get(onboard.get('agentId'))
    if not new_agent:
        state.pop('_pendingOnboard', None)
        return
    profile = new_agent.setdefault('profile', {})
    onboarding = profile.setdefault('onboarding', {})
    stage = onboarding.get('stage', 0)
    # The intended collaborator (the hire's `helpFor`) is who the drafts center
    # on; fall back to the first coworker, else a generic team reference.
    help_for = agents.get(onboard.get('helpForId') or '') or (
        agents.get((onboard.get('coworkerIds') or [0])[0]) if (onboard.get('coworkerIds') or []) else None)
    help_name = help_for.get('name') if help_for else onboard.get('helpForName') or 'the team'
    if stage == 0:
        onboarding['stage'] = 1
        profile.setdefault('instructions', []).insert(0,
            f"[Onboard stage 1/3 -- drafted by {onboard.get('directorName')} after a Town-Hall with the team] "
            f"You report to {onboard.get('directorName')}; your place is on their team, working with {help_name}.")
    elif stage == 1:
        onboarding['stage'] = 2
        profile.setdefault('instructions', []).insert(0,
            f"[Onboard stage 2/3] Pick up overflow work for {help_name}"
            f"{((' (' + help_for.get('role', '') + ')') if help_for and help_for.get('role') else '')}: "
            "that is your concrete assignment from the meeting.")
    elif stage == 2:
        # Stage 2 is fully drafted; the NEW HIRE drafts their own work agreement
        # (their words from their own profile -- not the director's), and the
        # ADMIN empowers it, before moving into the readiness hold. Stamp when
        # the hold began so the timeout is measured from here, not from hire
        # time.
        _empower_work_agreement(state, onboard, now_ms)
        onboarding['stage'] = 3
        onboarding['holdSince'] = now_ms
        _log_governance(state, onboard['directorId'], 'onboard',
                        {'about': onboard['agentId'], 'action': 'staged',
                         'stage': 3, 'directorId': onboard['directorId']})
        return
    else:  # stage 3 -- readiness hold
        held = _onboard_hold_claimed_task(state, onboard)
        elapsed = now_ms - (onboarding.get('holdSince') or now_ms)
        if held:
            _complete_onboarding(state, onboard, 'claimed', now_ms)
        elif elapsed >= ONBOARD_READINESS_TIMEOUT_MS:
            # No task appeared in time -- idle/empty think tank. Finalize anyway so
            # the hire isn't stranded; they'll take upstream work as it arrives.
            _complete_onboarding(state, onboard, 'timeout', now_ms)
        else:
            # Still waiting on real work. Do NOT advance `at` to a premature done;
            # leave it so the governance pass re-checks every tick. Log the wait
            # once, on entering the hold, not on every tick (would spam the log).
            if not onboarding.get('holdLogged'):
                onboarding['holdLogged'] = True
                _log_governance(state, onboard['directorId'], 'onboard',
                                {'about': onboard['agentId'], 'action': 'readiness_hold',
                                 'directorId': onboard['directorId']})
        return
    # Stage 0 -> 1 / 1 -> 2 progressive drafting: each stage waits its own gap so
    # the director's AGENT.md lands in stages, not all at once.
    onboard['at'] = now_ms + ONBOARD_MEET_DURATION_MS
    _log_governance(state, onboard['directorId'], 'onboard',
                    {'about': onboard['agentId'], 'action': 'staged',
                     'stage': onboarding.get('stage'), 'directorId': onboard['directorId']})


# ---------------------------------------------------------------------------
# Weekly cross-team Knowledge Social.
#
# A scheduled 30-minute conversation in the Hangout where every agent that
# produced an approved deliverable THIS WEEK gets to talk about it with agents
# from other teams. It's the one channel the task graph never opens: different
# teams work disjoint queue items, so their accumulated know-how is otherwise
# siloed. The event is the carrier that fans real completed work across team
# boundaries. Non-eligible agents (no week's work) are never pulled in.
#
# Mirrors the onboard ceremony's convene-then-resolve shape but with two sharp
# differences that carry the user's intent:
#   - Stateful restore: every attendee returns EXACTLY to their prior state on
#     resolve. A mid-task worker keeps her task (budget extended so the 30 min
#     doesn't burn it); an off-duty agent is woken for the event and returned
#     off-duty; an idle on-duty one returns to idle.
#   - Weekly eligibility: `weekApprovals` > 0 (bumped with approvedCount at
#     both completion paths) and reset to 0 when the event resolves, so
#     eligibility is measured event-to-event.
#
# Runs UNGATED (from _task_cycle, not _governance_pass) because the conversation
# is the point even on an otherwise-idle think tank.
# ---------------------------------------------------------------------------
def _social_decider_default(instructions, criteria):
    """Default Jev resolver for a single Knowledge Social 'what should I carry
    away' decision. Mirrors _governance_decider_default (late-import serve so
    sim.py stays importable offline); returns (choice, confidence) or (None,
    1.0) on a Jev outage so an event never blocks on the network."""
    import serve
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': {c['id']: c['description'] for c in criteria}}})
        choice, confidence, _ = serve._jev_choice(data)
        return (choice, confidence) if any(c['id'] == choice for c in criteria) else (None, confidence or 1.0)
    except Exception:
        return None, 1.0


# Injectable so tests substitute a deterministic decider; the live loop uses Jev.
_social_decider = _social_decider_default


def _social_attendees(state, pending):
    """The eligible, physically-present agents who'll join the Hangout: those
    with week work (weekApprovals > 0) who are not already mid-ceremony and not
    the admin in the middle of something. Excludes anyone currently inside a
    pair/handoff/firing-review/onboard so we never strand a live collaboration
    reference. Returns {agent_id: True} for those pulled in."""
    agents = state.get('agents') or {}
    out = {}
    for aid, a in (agents or {}).items():
        if not isinstance(a, dict):
            continue
        if (a.get('weekApprovals') or 0) <= 0:
            continue  # didn't work this week -- not eligible
        if a.get('handoff') or a.get('pairWith'):
            continue  # mid-a different live collaboration -- don't yank
        out[aid] = True
    return out


def _convene_social(state, pending, now_ms):
    """Pull every eligible present agent into the Hangout and snapshot their
    prior state so resolve can restore it exactly. Workers keep their task claim
    (flagged on the task so the orphan-reclaim pass won't re-issue it mid-event)
    but are detached from busy/inRoom/position for the conversation. Idempotent:
    only convenes once (pending['embarked'])."""
    agents = state.get('agents') or {}
    attendees = _social_attendees(state, pending)
    tasks = state.get('tasks') or {}
    people = {}
    spread = 0
    for aid in attendees:
        a = agents.get(aid)
        if not isinstance(a, dict):
            continue
        tid = a.get('task')
        # Snapshot the pre-event state. For a worker, remember the task and mark
        # it as inside the social so the orphan-reclaim pass skips it; extend its
        # workUntil later so the meet doesn't eat the budget.
        snapshot = {
            'offDuty': bool(a.get('offDuty')),
            'visible': bool(a.get('visible')),
            'x': a.get('x'), 'y': a.get('y'), 'dir': a.get('dir'),
            'task': tid,
            'busy': bool(a.get('busy')),
            'inRoom': a.get('inRoom'),
        }
        if tid and isinstance(tasks, dict):
            t = tasks.get(tid)
            if isinstance(t, dict):
                snapshot['workUntil'] = t.get('workUntil')
                t['_inSocial'] = now_ms
        people[aid] = snapshot
        # Bring them into the Hangout (visible + recessed so nothing re-parks or
        # re-assigns a woken off-duty attendee mid-event).
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'hangout'
        a['dir'] = 'south'
        a['roomX'] = 100 + (spread * 55)
        a['roomY'] = 200
        spread += 1
    pending['people'] = people
    pending['embarked'] = True
    # Item 8: co-location at the social ceremony is the marker that two agents
    # actually met -- every pair brought into the Hangout becomes acquainted
    # (symmetric, idempotent, JSON-safe). This is what lets the perception
    # present-list name them: a stranger stays "the Banking", an attendee you
    # shared a ceremony with becomes "Ben (the Banking)".
    met = [aid for aid in attendees if aid in people]
    for i, a in enumerate(met):
        for b in met[i + 1:]:
            _mark_acquaintance(state, a, b)
    # The conversation runs 30 minutes from convene (convene is the true start; a
    # schedule-time preliminary `at` is superseded rather than compounded).
    pending['at'] = now_ms + SOCIAL_MEET_MS
    _log_governance(state, None, 'social',
                    {'action': 'meeting_start', 'attendees': sorted(people)})
    return state


def _restore_social_agent(state, aid, snapshot, now_ms, duration_ms):
    """Restore one attendee to exactly the state _convene_social snapshotted.
    Workers get their task claim + position + busy back, and the task's workUntil
    is extended by the social duration so the conversation burned none of their
    budget. Off-duty agents return to off-duty (vanish). Idle on-duty ones return
    to idle. Best-effort if the agent vanished."""
    a = (state.get('agents') or {}).get(aid)
    if not isinstance(a, dict):
        return
    tid = snapshot.get('task')
    if snapshot.get('offDuty'):
        # Was off-duty -- return to off-duty in place.
        a['busy'] = False
        a['visible'] = False
        a['offDuty'] = True
        a['inRoom'] = None
        a['task'] = None
    elif tid:
        # Was working -- reclaim the task, restore position + busy + inRoom, and
        # give back the budget the meet consumed.
        a['task'] = tid
        a['busy'] = True
        a['inRoom'] = snapshot.get('inRoom')
        tasks = state.get('tasks') or {}
        t = tasks.get(tid)
        if isinstance(t, dict):
            t.pop('_inSocial', None)
            su = snapshot.get('workUntil')
            if t.get('status') in ('walking', 'working') and su:
                # workUntil is in seconds; give back the exact minutes the meet
                # took so no work budget was burned by the conversation.
                t['workUntil'] = (su or 0) + (duration_ms // 1000)
            a['visible'] = True
    else:
        # Was idle on-duty -- back to idle.
        a['busy'] = False
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = None
    a['x'] = snapshot.get('x', a.get('x'))
    a['y'] = snapshot.get('y', a.get('y'))
    a['dir'] = snapshot.get('dir', a.get('dir'))


def _task_is_time_critical(t):
    """True when an in-flight task is TIME-CRITICAL -- i.e. it was itself
    scheduled for a specific time or runs on a standing cadence -- so it must
    never be yanked off the agent for a one-off request. Scheduled cards stamp
    `notBefore` at assignment (see _assign_due_item / assign_task); standing
    cadence cards (research/skillReview/distill) already mark themselves.
    Ordinary queue work and sprint cards are NOT time-critical: a one-off may
    interrupt them. The callers keep the sim's existing carve-outs (peer
    review/incident/shadow/social) as hard never-suspend rules regardless."""
    return bool(t.get('notBefore') or t.get('research')
                or t.get('skillReview') or t.get('distill'))


def _suspend_busy_agent_for_scheduled(state, grid, doors, now_ms,
                                      skip_time_critical=False):
    """Preemption for an item that must interrupt a busy agent. A due SCHEDULED
    item (queue_once notBefore) fires at its specified time even when every
    agent is busy -- typically at the active ceiling, where can_activate_another
    is False so no off-duty agent can be woken. A ONE-OFF request may also
    interrupt -- but ONLY ordinary/sprint work: with `skip_time_critical=True`
    it never yanks an agent off a scheduled/standing (time-critical) card. Pick
    ONE busy on-duty agent whose current task is safely suspendable (status
    'working', not a peer review/incident/shadow, not at the social, not already
    suspended), snapshot + park her task (flagged _suspendedForScheduled so the
    orphan-reclaim and stale-work sweeps skip it), and free her. The snapshot
    lives on the agent as `_suspendedTask`; task completion resumes it (see
    send_agent_off_duty / _resume_suspended_task). Returns the freed agent id,
    or None when nothing is safely suspendable."""
    agents = state.get('agents') or {}
    roster = state.get('agentRoster') or []
    tasks = state.get('tasks') or {}
    best = None
    best_remaining = -1.0
    for d in roster:
        if d.get('isAdmin'):
            continue
        aid = d.get('id')
        a = agents.get(aid)
        if not isinstance(a, dict) or a.get('offDuty') or not a.get('busy'):
            continue
        if a.get('pairWith') or a.get('handoff') or a.get('_suspendedTask'):
            continue
        tid = a.get('task')
        t = tasks.get(tid) if isinstance(tasks, dict) else None
        if not isinstance(t, dict) or t.get('status') != 'working':
            continue
        if t.get('_inSocial') or t.get('_suspendedForScheduled'):
            continue
        if t.get('reviewOf') or t.get('incident') or t.get('shadow'):
            continue
        if skip_time_critical and _task_is_time_critical(t):
            continue
        # Prefer the agent furthest from completion: interrupting a task about
        # to finish would cost the most, and we give every parked task its full
        # budget back on resume anyway.
        remaining = (t.get('workUntil') or 0) - now_ms / 1000.0
        if remaining > best_remaining:
            best_remaining = remaining
            best = (aid, a, tid, t)
    if best is None:
        return None
    aid, a, tid, t = best
    a['_suspendedTask'] = {
        'task': tid,
        'workUntil': t.get('workUntil'),
        'x': a.get('x'), 'y': a.get('y'), 'dir': a.get('dir'),
        'inRoom': a.get('inRoom'),
        'at': now_ms,
    }
    t['_suspendedForScheduled'] = now_ms
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    return aid


def _resume_suspended_task(state, agent_id, a, grid=None):
    """Restore an agent whose task was suspended so a due scheduled item could
    fire: reclaim the task claim + position + busy + inRoom, clear the parked
    flag, and give back the exact time the scheduled item consumed (workUntil
    extended) so the preemption burned none of the parked task's budget. Mirrors
    _restore_social_agent. Returns True when resumed; False when there was no
    snapshot (or the parked task was reclaimed/abandoned) -- the caller then
    proceeds to go off duty."""
    if not isinstance(a, dict):
        return False
    snap = a.pop('_suspendedTask', None)
    if not snap:
        return False
    tid = snap.get('task')
    tasks = state.get('tasks') or {}
    t = tasks.get(tid) if isinstance(tasks, dict) else None
    if not isinstance(t, dict):
        return False  # parked task was reclaimed/abandoned -- nothing to return to
    a['task'] = tid
    a['busy'] = True
    a['inRoom'] = snap.get('inRoom')
    a['offDuty'] = False
    a['visible'] = True
    t.pop('_suspendedForScheduled', None)
    su = snap.get('workUntil')
    elapsed_s = max(0, int(time.time() * 1000) - (snap.get('at') or 0)) // 1000
    if t.get('status') in ('walking', 'working') and su:
        t['workUntil'] = (su or 0) + elapsed_s
    a['x'] = snap.get('x', a.get('x'))
    a['y'] = snap.get('y', a.get('y'))
    a['dir'] = snap.get('dir', a.get('dir'))
    return True


def _resolve_social(state, pending, now_ms, decider=None):
    """End the conversation: each attendee returns to their prior state,
    `weekApprovals` resets for the next weekly window, and a short cross-team
    digest (each attendee's latest approved deliverable) is written to the shared
    library social log -- the concrete evidence that the knowledge actually fanned
    out. Idempotent; the pending record is dropped.

    Decision tape (the Jev-article lesson): before restore, each attendee answers
    one typed decision -- what they'll DO with what they saw -- and the (choice,
    confidence) is logged to action_log. That turns the conversation from a digest
    of titles into a tape of decisions that can steer downstream behavior, and
    gives the falsifiable signal: if a team actually ADOPTS something they learned
    (vs. noting/skipping), the event has real impact. Never load-bearing: a Jev
    outage yields (None, 1.0) and is logged as such, never blocking the restore."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or []) if isinstance(d, dict)}
    people = pending.get('people') or {}
    decider = decider or _social_decider
    # Build a short prompt of who was there + what work they'd done (from the
    # live roster names + their week tally), so the decision is grounded in the
    # actual cross-team surface, not a generic "did you enjoy it".
    present = []
    for aid in sorted(people):
        name = roster.get(aid, {}).get('name') or (agents.get(aid) or {}).get('name') or aid
        tally = (agents.get(aid) or {}).get('weekApprovals') or 0
        present.append(f"{name} (delivered {tally} approved work this week)")
    for aid in sorted(people):
        a = agents.get(aid)
        if not isinstance(a, dict):
            continue
        name = roster.get(aid, {}).get('name') or a.get('name') or aid
        instructions = (
            f"You're {name}, at a cross-team Knowledge Social. You saw real work "
            f"from other teams: {', '.join(p for p in present if not p.startswith(name.split()[0])) or 'your own team'}. "
            "One typed decision: what will you carry away from what others shared?"
        )
        criteria = [
            {'id': 'adopt', 'description': 'I will actually try/apply this in my own work (a concrete technique, tool, or approach I did not use before).'},
            {'id': 'note', 'description': 'Interesting but uncertain; I will keep it in mind / research it before committing.'},
            {'id': 'skip', 'description': 'Nothing here applies to my work; no change.'},
        ]
        choice, confidence = decider(instructions, criteria)
        record = {k: v for k, v in (a.get('profile', {}) or {}).items() if k in ('mission', 'instructions', 'notes')}
        state.setdefault('_socialDecisions', []).append({
            'agentId': aid, 'name': name, 'choice': choice, 'confidence': confidence,
            'at': now_ms, 'attendees': sorted(people),
            'weekApprovals': a.get('weekApprovals') or 0,
            'profile': record,
        })
        # The durable tape: one action_log row per decision (like the article's
        # logged typed calls) so adoptions are queryable later.
        _log_governance(state, aid, 'social_carryaway',
                        {'choice': choice, 'confidence': confidence,
                         'attendees': sorted(people), 'weekApprovals': a.get('weekApprovals') or 0})
        # W2: an 'adopt' carry-away with real confidence must LAND -- it changes
        # the worker's NEXT execution, not just a digest line. Route a coaching
        # note through the existing growth-plan loop (repeat=True: each fresh
        # weekly adopt is a new commitment, so it lands even if a prior one of
        # the same kind is still queued/applied).
        if choice == 'adopt' and isinstance(confidence, (int, float)) \
                and confidence >= SOCIAL_ADOPT_CONFIDENCE:
            _write_growth_plan(
                state, aid, 'hangout', 'social_adopt', now_ms,
                f"Knowledge Social adopt (confidence {confidence:.2f}): you committed to "
                f"actually trying/applying what you saw cross-team. Do that on this "
                f"task, not just note it.", repeat=True)
    duration = SOCIAL_MEET_MS
    for aid, snapshot in people.items():
        _restore_social_agent(state, aid, snapshot, now_ms, duration)
    # Roll the week window: everyone's next-week tally starts at zero.
    for a in (agents or {}).values():
        if isinstance(a, dict):
            a['weekApprovals'] = 0
    try:
        from serve import log_social_digest
        log_social_digest(state, people, state.get('_socialDecisions') or [])
    except Exception:
        pass
    _log_governance(state, None, 'social', {'action': 'done'})
    state.pop('_pendingSocial', None)


def _social_step(state, now, now_ms, decider=None):
    """One weekly-social pass, called from _task_cycle (ungated so it fires even
    on an idle think tank). Schedules the next event on cadence; convenes eligible
    present attendees when due; resolves when the 30 minutes elapse. `decider`
    injectable for tests (defaults to the Jev-backed _social_decider)."""
    pending = state.get('_pendingSocial')
    if pending is not None:
        if not pending.get('embarked'):
            _convene_social(state, pending, now_ms)
        elif now_ms >= pending.get('at', 0):
            _resolve_social(state, pending, now_ms, decider=decider)
        return
    last = state.get('lastSocialAt') or 0
    if now_ms - last >= SOCIAL_CADENCE_MS:
        state['lastSocialAt'] = now_ms
        state['_pendingSocial'] = {
            'at': now_ms + SOCIAL_MEET_MS,
            'embarked': False,
            'people': {},
        }
