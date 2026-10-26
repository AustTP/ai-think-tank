def _park_idle_wanderers(state):
    """An agent is only woken by task assignment or the waking
    intents -- but a woken agent who is never *assigned* (e.g. the queue
    emptying between its wake and assignment, or a reload force-shadowing an
    on-duty agent via agents.js's `if (!offDuty) visible = true`) lingers
    on-duty and visible forever with nothing to do. Per the player's rule
    ("unless he has scheduled work or is active, an agent should not appear"),
    park every fully-idle, on-duty, non-admin agent back off duty so it
    vanishes in place. Assignment still wakes it on demand, so this cannot
    strand the think tank. Pure/mutating on `state`; no-op when nothing to park.
    Returns the number parked."""
    agents = state.get('agents')
    if not isinstance(agents, dict) or not agents:
        return 0
    roster = state.get('agentRoster') or []
    parked = 0
    for d in roster:
        if d.get('isAdmin'):
            continue
        a = agents.get(d.get('id'))
        if not isinstance(a, dict) or a.get('offDuty'):
            continue
        room_def = a.get('inRoom')
        if not _is_fully_idle(a, room_def):
            continue
        # Wake-on-mail: an off-duty agent woken to act on action-needed mail must
        # not be parked in the same tick before she has routed to the work
        # (see _mail_action_step, which clears _mailAwake once she's acting).
        if a.get('_mailAwake'):
            continue
        a['offDuty'] = True
        a['visible'] = False
        a['path'] = None
        a['pathIndex'] = 0
        a['pathTarget'] = None
        a['pathActive'] = False
        a['stuckTimer'] = 0
        a['replanCount'] = 0
        parked += 1
    return parked


def automation_paused(state):
    """True when the operator has put the tank in drain/idle mode. Automatic
    card factories must not refill an empty board while this is set."""
    mode = str(((state or {}).get('automation') or {}).get('mode') or '').lower()
    return mode in ('drain', 'idle', 'paused', 'off', 'manual')


def _feature_affinity(state, agent_id, pick):
    """Feature affinity score: how many PRIOR tasks `agent_id` completed that
    match the picked work's feature signature -- same room, same feature (the
    task's projectLabel, which assignment also folds the goal into), or same
    product. A cheap counter over the durable tasks map, no model call. The
    assignment loop uses it as a SOFT pull toward the person who already knows
    the corner of the think tank this item belongs to."""
    tasks = state.get('tasks') or {}
    room = pick.get('room')
    feature = pick.get('projectLabel') or pick.get('goal')
    product_id = pick.get('productId')
    score = 0
    for t in tasks.values():
        if t.get('status') != 'done' or t.get('assignedTo') != agent_id:
            continue
        if room and t.get('room') == room:
            score += 1
        if feature and (t.get('projectLabel') == feature or t.get('goal') == feature):
            score += 1
        if product_id and t.get('productId') == product_id:
            score += 1
    return score


# A tiny stopword list for onboarding description-affinity: enough to strip
# connective noise so the overlap counter keys on real content words. A fuller
# NLP approach is overkill -- this is a SOFT pull, never a lock.
_DESCRIPTION_STOPWORDS = frozenset({
    'the', 'a', 'an', 'and', 'or', 'to', 'of', 'for', 'with', 'on', 'in',
    'at', 'by', 'your', 'their', 'our', 'you', 'them', 'from', 'into', 'via',
    'this', 'that', 'they', 'work', 'who', 'what', 'pick', 'up', 'picking',
})


def _description_affinity(state, agent_id, pick):
    """Onboarding affinity: how well `agent_id`'s DESCRIPTION matches the picked
    work. A brand-new hire has no completed-work history, so feature affinity
    (_feature_affinity) can never route them -- they fall to pure round-robin.
    This scores the agent's role / mission / profile instructions / access grant
    against the task's own text (title + instructions + goal + projectLabel +
    room) by meaningful-token overlap, so a new team member is handed the work
    that fits their description instead of being chosen last. Cheap + deterministic
    (no model call); the assignment loop uses it as a soft pull exactly like
    feature affinity."""
    roster_def = next((d for d in (state.get('agentRoster') or [])
                       if d.get('id') == agent_id), None)
    if not roster_def:
        return 0
    desc_parts = [
        roster_def.get('role') or '',
        roster_def.get('description') or '',
        roster_def.get('specialty') or '',
    ]
    profile = roster_def.get('profile') or {}
    if isinstance(profile, dict):
        desc_parts.append(profile.get('mission') or '')
        instr = profile.get('instructions')
        if isinstance(instr, list):
            desc_parts.extend(str(i) for i in instr)
        elif instr:
            desc_parts.append(str(instr))
    desc_tokens = set()
    for part in desc_parts:
        desc_tokens.update(_description_tokens(part))
    if not desc_tokens:
        return 0
    task_tokens = set()
    for field in ('title', 'instructions', 'goal', 'projectLabel'):
        task_tokens.update(_description_tokens(pick.get(field)))
    task_tokens.update(_description_tokens(pick.get('room')))
    if not task_tokens:
        return 0
    return len(task_tokens & desc_tokens)


def _description_tokens(text):
    if not text:
        return []
    out = []
    for tok in str(text).lower().split():
        clean = ''.join(ch for ch in tok if ch.isalnum())
        if clean and clean not in _DESCRIPTION_STOPWORDS and len(clean) > 2:
            out.append(clean)
    return out


def _recruit_pair_navigator(state, driver_id, task, can_wake_off_duty, grid, doors, now_ms):
    """W1: server-owned pair recruitment. The client's assignPairTask recruits a
    navigator and walks her over; the server previously assigned a `pair` card
    SOLO (no navigator ever joined). Recruits the next most-idle eligible
    candidate deterministically (round-robin, zero JEV spend, mirrors
    _assign_due_item), walks her to a free spot beside the driver's door, and
    marks her as the task's navigator (pairWith/pairTaskId/path). The driver's
    own walk was already issued by the caller. Returns the navigator id, or
    None when no second eligible hand exists (the driver proceeds solo, the
    same graceful degradation the client's assignPairTask has when the pool is
    too thin)."""
    agents = state.get('agents') or {}
    candidates = _eligible_candidates(state, can_wake_off_duty)
    ordered = [cid for cid in candidates if cid != driver_id]
    if not ordered:
        return None
    # Round-robin over eligible ids (deterministic, stable roster order).
    rr = state.setdefault('sim', {}).setdefault('rr', {})
    pointer = rr.get('pair', 0)
    idx = pointer % len(ordered)
    nav_id = ordered[idx]
    rr['pair'] = (pointer + 1) % max(1, len(ordered))
    nav = agents.get(nav_id)
    if not nav:
        return None
    if nav.get('offDuty'):
        appear_from_outskirts(state, nav_id, doors)
    room = task.get('room')
    door = (doors or {}).get(room)
    if not door:
        return None
    base_x = door['x'] + door['w'] / 2
    base_y = door['y'] + door['h'] + 4
    # Try several offsets beside the door (mirrors assignPairTask's own
    # "try offsets that clear the walkable strip" loop).
    for dx in PAIR_WALK_OFFSETS:
        path = find_path(nav['x'], nav['y'], base_x + dx, base_y, nav_id, agents, grid)
        if path and len(path) > 0:
            nav['pairWith'] = driver_id
            nav['pairTaskId'] = task['id']
            nav['path'] = path
            nav['pathIndex'] = 0
            nav['pathTarget'] = {'x': base_x + dx, 'y': base_y}
            nav['stuckTimer'] = 0
            nav['replanCount'] = 0
            nav['respawnedForTask'] = False
            task['pairWith'] = nav_id
            return nav_id
    return None


def _sprint_worker_pool(state, sprint_id, worker_count=None):
    """The STRICT assignment pool for a sprint's cards: the non-admin members of
    the sprint's teams -- roster order, deduped across teams -- capped at the
    sprint's `workerCount` (the director chose 1-6 workers; the cap picks which
    of a team's members staff THIS sprint). The designated scrum master is
    excluded from the pool when the chosen headcount is below
    SCRUM_MASTER_MIN_TEAM_SIZE (on a lean sprint the facilitator is not a
    counted worker); at or above that size the scrum master IS a working member
    of the pool. Legacy sprints (no workerCount) default to the team's full
    complement with the scrum master counted. Returns None when `sprint_id` is
    absent (a non-sprint card has no pool), else the pool list -- possibly
    empty, which makes the sprint's cards WAIT (strict staffing: no fallback to
    the think tank at large). `worker_count` overrides the record's stored
    value (used by the staffing-expansion probe to preview a larger pool)."""
    if not sprint_id:
        return None
    record = (state.get('sprints') or {}).get(sprint_id)
    if not record:
        return []
    # Headcount cap: the LARGEST maxTeamMembers among the sprint's teams (a
    # multi-team sprint can staff up to the biggest team's agreement); empty
    # teamIds falls back to the owner's team, then the operator value.
    _cap = (max([team_policy(state, t, 'maxTeamMembers')
                 for t in (record.get('teamIds') or [])], default=0)
            or team_policy(state, record.get('ownerId'), 'maxTeamMembers'))
    if worker_count is None:
        worker_count = int(record.get('workerCount') or _cap)
    worker_count = max(1, min(_cap, worker_count))
    # Team ids on a sprint record may be keyed by the team's `id` OR its
    # director's id (the two keyings team records use) -- resolve each to a
    # director id so _sim_direct_reports can list its members.
    directors = []
    for tid in (record.get('teamIds') or []):
        t = _team_row(state, tid)
        directors.append(t.get('directorId') if t else tid)
    if not directors:
        return []
    # A lean sprint (below the SM-size floor) is worked by the team's workers,
    # never the dedicated facilitator -- she runs ceremonies, not cards. A
    # sprint at/above the floor counts the scrum master as a working member.
    excluded = set()
    _sm_floor = (max([team_policy(state, t, 'scrumMasterMinTeamSize')
                      for t in (record.get('teamIds') or [])], default=0)
                 or team_policy(state, record.get('ownerId'), 'scrumMasterMinTeamSize'))
    if worker_count < _sm_floor:
        for d in directors:
            t = next((x for x in (state.get('teams') or []) if x.get('directorId') == d), None)
            if t and t.get('scrumMasterId'):
                excluded.add(t['scrumMasterId'])
    pool = []
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin'):
            continue
        if d.get('director') not in directors:
            continue
        if d['id'] in excluded:
            continue
        pool.append(d['id'])
    # The headcount cap picks WHICH members staff the sprint, stable roster
    # order (deterministic -- same sprint always names the same pool).
    return pool[:worker_count]


def _one_off_routing_applies(pick):
    """Does the one-off/scheduled routing rule cover this item? It
    covers a fire-at-a-time card (`notBefore` set -- queue_once
    one-offs and work scheduled for a specific time) and the
    standing cadence sweeps (research / skillReview / distill,
    the schedule lane's recurring work). Ordinary backlog cards
    keep the generic round-robin. A sprint card never reaches the
    rule at all: its hard pool restriction (sprint staffing always
    wins for its own cards) is checked by the caller."""
    return bool(pick.get('notBefore') or pick.get('research')
                or pick.get('skillReview') or pick.get('distill'))


def _agent_sprint_work_done(state, agent_id):
    """Has this agent completed all sprint work? She is staffed
    (via _sprint_worker_pool, the same strict pool the sprint
    itself uses) on no ACTIVE sprint that still has a queued
    card -- every sprint she is on has drained its queue, so only
    in-flight work can remain. Vacuously true when she is staffed
    on no active sprint at all. Pure state reads, no JEV."""
    work_queue = state.get('workQueue') or []
    for sid, record in (state.get('sprints') or {}).items():
        if record.get('status') != 'active':
            continue
        if agent_id not in (_sprint_worker_pool(state, sid) or []):
            continue
        if any(it.get('sprintId') == sid for it in work_queue):
            return False
    return True


def _on_item_team(state, agent_id, team_id):
    """Is this agent on the team that owns this item's work? A
    direct report of the team's director (a team is identified by
    its director id, the same membership _sim_direct_reports
    gives), or an agent loaned to the team (roster `loan` tag)
    for the loan's duration -- the same membership the generic
    team preference uses. Teamless items have no owning team."""
    if not team_id:
        return False
    if agent_id in _sim_direct_reports(state, team_id):
        return True
    return any(d.get('id') == agent_id and d.get('loan')
               and d['loan'].get('teamId') == team_id
               for d in (state.get('agentRoster') or []))


def _sprint_staffing_step(state, now_ms=None):
    """Sprint staffing expansion: a director grows an ACTIVE sprint's staff when
    the agents need help. "Needs help" is modeled deterministically (no JEV
    spend, per the keep-deterministic-things-deterministic rule): a sprint below
    the six-worker cap whose cards are STILL queued while every current pool
    member is busy (task/pair/busy -- even a member who could otherwise be woken)
    gets ONE more worker, on each pass it stays saturated, until the queue drains
    or the cap is hit. The next member in roster order joins the pool (at the
    SCRUM_MASTER_MIN_TEAM_SIZE threshold the scrum master becomes a counted
    worker). A team with fewer real members than the target can't expand further.
    Pure state mutation; returns the sprint ids expanded this pass."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    sprints = state.get('sprints') or {}
    agents = state.get('agents') or {}
    expanded = []
    for sid, record in sprints.items():
        if record.get('status') != 'active':
            continue
        _cap = (max([team_policy(state, t, 'maxTeamMembers')
                     for t in (record.get('teamIds') or [])], default=0)
                or team_policy(state, record.get('ownerId'), 'maxTeamMembers'))
        wc = int(record.get('workerCount') or _cap)
        if wc >= _cap:
            continue  # already at the team's cap -- nothing to add
        # Can the pool actually grow? The next count must name a real member
        # beyond the current pool (a team with only 3 members can't staff 6).
        next_pool = _sprint_worker_pool(state, sid, wc + 1)
        if not next_pool or len(next_pool) <= len(_sprint_worker_pool(state, sid, wc)):
            continue
        # Sprint cards still waiting to be handed out (done cards left the
        # queue; sprint-tagged queue entries are exactly the unfinished set).
        if not [it for it in (state.get('workQueue') or []) if it.get('sprintId') == sid]:
            continue
        # "Agents need help": every current pool member is occupied. Off-duty
        # members still count as available (a sprint card is always wakeable),
        # so expansion only fires when nobody in the pool can take work at all.
        available = [m for m in _sprint_worker_pool(state, sid, wc)
                     if not (agents.get(m) or {}).get('busy')
                     and not (agents.get(m) or {}).get('task')
                     and not (agents.get(m) or {}).get('pairWith')]
        if available:
            continue
        record['workerCount'] = wc + 1
        _log_governance(state, record.get('ownerId') or sid, 'sprint_staffing',
                        {'sprint': sid, 'workers': wc + 1, 'action': 'expanded'})
        expanded.append(sid)
    return expanded


def _assign_player_task(state, pick, now_ms, task_id_holder=None):
    """Assign one due queue item to the PLAYER (a human-in-the-loop card): mint
    the durable state['tasks'] record with status 'needs_player' and delivered
    to the player's inbox + email, instead of walking any agent. Returns the
    task dict, or None on failure (-> caller requeues/attempt++).

    The player card is a REAL task in the durable mirror, so the existing
    dependency machinery works unchanged in BOTH directions:
      - an agent card queued `depends_on_task=<this id>` stays unassigned until
        the player marks it done (see _work_item_dependency_met / _dependency_landed);
      - this card's own `dependsOn` (an agent story the player's work waits on)
        is honored by pick_next_due_index, so it is not even delivered until
        that story lands.
    `task_id_holder` is the mutable init([count]) nextTaskId counter, shared
    with assign_task so ids never collide."""
    title = (pick.get('title') or '').strip()
    if not title:
        return None
    task_id = _next_task_id(state, task_id_holder)
    task = {
        'id': task_id,
        'title': title,
        'room': pick.get('room') or None,
        'instructions': pick.get('instructions') or '',
        'projectLabel': pick.get('projectLabel') or pick.get('goal') or None,
        'taskType': pick.get('taskType') or 'code',
        'assignedTo': 'player',
        'status': 'needs_player',  # the player's to do; -> 'done' via complete_player_task
        'createdAt': now_ms,
        'dependsOn': pick.get('dependsOn') or None,
        'userStory': pick.get('userStory') or None,
        'acceptanceCriteria': pick.get('acceptanceCriteria') or None,
        'never': pick.get('never') or None,
        'issueKey': pick.get('issueKey') or None,
        'budgetMs': pick.get('budgetMs') or None,
        'lane': pick.get('lane') or None,
    }
    state.setdefault('tasks', {})[task_id] = task
    # Deliver to the player: an inbox card (rendered by the UI with a Mark-done
    # button) + a real email so the wait is visible even when the player isn't
    # looking at the sim.
    mid = f'ptask-{int(now_ms)}'
    task['playerInboxId'] = mid
    inbox = state.setdefault('playerInbox', [])
    inbox.append({
        'id': mid, 'kind': 'player_task', 'taskId': task_id,
        'title': title, 'instructions': task['instructions'],
        'status': 'needs_player', 'createdAt': now_ms,
        'dependsOn': task['dependsOn'], 'issueKey': task['issueKey'],
        'lane': task['lane'],
    })
    depends_line = ''
    if task['dependsOn']:
        depends_line = f"\n\nNote: this task waits on '{task['dependsOn']}' being done before it starts."
    _queue_player_email(
        state, 'player_task',
        f"[AI Think Tank] A task is waiting on YOU: {title}",
        (f"The think tank has handed you this task and is waiting on your hands before "
         f"it resumes the work queued behind it:\n\n"
         f"{title}\n\n{task['instructions'] or '(no further instructions)'}\n\n"
         f"Mark it done in the player-inbox when you've completed it."
         f"{depends_line}"),
        now_ms=now_ms)
    from serve import log_action
    log_action('player', 'task_assigned',
               {'taskId': task_id, 'title': title, 'room': task['room']}, authorized=False)
    return task


_ASSIGN_WAIT = object()


def _assign_due_item(state, pick, can_wake_off_duty, grid, doors, now_ms, task_id_holder=None):
    """Deterministic assignment of one due queue item: pick the most-idle
    eligible candidate (round-robin, zero JEV spend), wake her if off-duty,
    assign. Returns the task dict or None. Mirrors assignTaskViaJev's
    fallback-to-first-eligible but replaces JEV with a deterministic pick."""
    request_id = pick.get('requestId')
    sequence = pick.get('sequence')
    if request_id and sequence is not None:
        # One request, one active card at a time, in the breakdown's order.
        # Without this, gather/write/save/verify all started together and the
        # save card ran before the brief existed.
        for other in (state.get('tasks') or {}).values():
            if other.get('requestId') != request_id:
                continue
            if other.get('status') in ('walking', 'working', 'repairing', 'needs_review'):
                return _ASSIGN_WAIT
        for other in (state.get('workQueue') or []):
            if other is pick or other.get('requestId') != request_id:
                continue
            other_seq = other.get('sequence')
            if other_seq is not None and other_seq < sequence:
                return _ASSIGN_WAIT
    agents = state.get('agents') or {}
    # Human-in-the-loop: a card pinned to the PLAYER (assignedTo 'player') is
    # never an agent's job. Assign it to the player (durable 'needs_player'
    # task + inbox/email delivery) and return -- no door path, no roster pick,
    # no spend. Anything queued depends_on_task on it resumes when the player
    # marks it done.
    if (pick.get('assignedTo') or '') == 'player':
        return _assign_player_task(state, pick, now_ms, task_id_holder or _TASK_ID_HOLDER)
    # A shared-backlog card is queued ROOM-LESS on purpose: the ASSIGNED agent
    # figures out where the work needs to happen. Resolve the room here, at the
    # moment of assignment, and persist it onto the pick so the walk/gate/grade
    # lifecycle (which routes by task['room']) sees a concrete room.
    # Same rescue for a room that DOES have a value but no door: a producer can
    # name any string it likes, and a string with no door makes assign_task
    # return None forever (that is exactly the "Act on mail (harold)" wedge: a
    # phantom 'townsquare', retried every 6s for 38 hours, 21k+ attempts, no
    # agent ever able to take it). `doors` is the real geometry map in
    # production; some callers (and tests) pass a placeholder object instead,
    # so only judge "is this a real room?" when it actually is a non-empty
    # mapping -- then route anything that does not resolve to a real door
    # through the normal room resolver.
    _room = pick.get('room')
    _real_doors = doors if (isinstance(doors, dict) and doors) else None
    if not _room or (_real_doors is not None and _room not in _real_doors):
        pick['room'] = _resolve_assignment_room(pick)
    # Sanitize a misused reviewOf (see _requeue_task_for_agent): help_signal
    # mail stamped `about` -- an AGENT id -- into reviewOf. Everywhere else
    # reviewOf means "peer-review subtask of THAT task", so an agent id both
    # lies to the lifecycle and claims the attempts exemption that exists for
    # genuine review subtasks. Drop exactly that case (a real task id, however
    # synthesized, is left alone so the reviewer pin keeps working).
    if pick.get('reviewOf') and pick['reviewOf'] in (state.get('agents') or {}):
        pick.pop('reviewOf', None)
    candidates = _eligible_candidates(state, can_wake_off_duty)
    # Directors and the admin delegate code; they never pick up code/bug/qa
    # work themselves. They can still file stories and spikes.
    if (pick.get('taskType') in governance.CODE_TASK_TYPES
            and not pick.get('reviewOf')):
        candidates = [cid for cid in candidates
                      if governance.may_work_code(state, cid)]
    # Sprint staffing: a sprint card is assigned ONLY to the sprint's chosen
    # worker pool (the director's 1-6). This is a HARD restriction, not the
    # soft team preference below -- a lean sprint's cards wait (empty pool ->
    # None -> the caller requeues) rather than leaking to the think tank at
    # large, by design. `sprint_pool` is None for non-sprint cards (no pool).
    sprint_pool = _sprint_worker_pool(state, pick.get('sprintId'))
    if sprint_pool is not None:
        candidates = [cid for cid in candidates if cid in sprint_pool]
        if not candidates:
            return None
    # Phase E addendum: a re-opened fix may be pinned to its original author, and
    # a gate review is pinned to its reviewer (pick['assignedTo']). Honor that --
    # and WAKE an off-duty pinned agent even when the generic wake rule (which
    # wakes only enough agents to keep one awake-idle) would otherwise skip them.
    # Without this, the SECOND reviewer of a pair (both off-duty) never gets
    # woken and the gate deadlocks at one approval.
    # Phase E2d: an INCIDENT (bug) is likewise pinned to the owning team's
    # on-call -- same wake semantics as a reviewer pin, even with no reviewOf.
    # Theo routing: directRoute is the same pin/wake treatment for
    # a spike the classifier attributed to a specific team's worker.
    # Sprint rollover: a card re-planned by refinement carries `_reassignedTo`,
    # a fresh explicit owner for the next sprint -- honored as a (soft) pin.
    # MEETING cards pin to their facilitator -- the agent who convenes the
    # meeting and writes the minutes -- with the same wake semantics (an
    # off-duty facilitator is woken at the meeting room's door).
    pin_kind = (pick.get('reviewOf') or pick.get('incident')
                or pick.get('_mailResume') or pick.get('directRoute')
                or pick.get('directedTo')
                or pick.get('_reassignedTo') or pick.get('facilitatorId')
                # A supervisor card belongs to the named director. Without this
                # pin it fell through to think-tank-wide round-robin and could
                # be handed to a worker, who has no supervisor scope at all.
                or pick.get('taskType') == 'supervise')
    pinned = (pick.get('assignedTo') or pick.get('facilitatorId')
              or pick.get('_reassignedTo')) if pin_kind else None
    if (pick.get('taskType') in governance.CODE_TASK_TYPES
            and not pick.get('reviewOf') and pinned
            and not governance.may_work_code(state, pinned)):
        pinned = None
    # Sprint staffing: a pin to someone OUTSIDE the sprint's worker pool is
    # dropped -- strict staffing wins over the soft reassignment pin (a card
    # re-planned by refinement pins to the team's least-loaded member, but if
    # that member isn't on this sprint's roster, the card waits for the pool).
    if pinned and sprint_pool is not None and pinned not in sprint_pool:
        pinned = None
    if pinned and agents.get(pinned):
        pinned_agent = agents.get(pinned)
        if not pinned_agent.get('busy') and not pinned_agent.get('task') and not pinned_agent.get('pairWith'):
            chosen_id = pinned
            # A reviewer/incident pin wakes AT THE TARGET ROOM'S DOOR (short,
            # reliable walk to judge the work); a refinement re-plan pin is not
            # tied to a specific desk, so it wakes via the generic outskirts
            # placement (see appear_from_outskirts below) -- same reachability
            # guarantee, no door-geometry coupling.
            if pinned_agent.get('offDuty') and not pick.get('_reassignedTo'):
                # Wake a reviewer at the REVIEW TARGET'S OWN door approach: the reviewer
                # is judging the author's work at that desk, so placing her at
                # the room's own door gives a short, reliable path.
                _spawn_at_room_door(state, pinned, pick.get('room'), agents, grid, doors)
        elif pinned in candidates:
            chosen_id = pinned
        elif pick.get('incident'):
            # The on-call is mid-work (busy/task/pairWith) so the pin can't be
            # honored. Re-derive the owning team's current on-call -- the
            # rotation already fell through to the backup / second backup -- so
            # the incident stays on the owning team instead of leaking to a
            # global round-robin pick on an unrelated team. Falls to None
            # (generic round-robin) only when the whole team is unavailable.
            chosen_id = None
            director_id = _product_director(state, pick.get('productId'))
            if director_id:
                backup = on_call_agent(state, director_id, pick.get('sprintId'), now_ms)
                if backup and backup != pinned and agents.get(backup):
                    chosen_id = backup
                    if agents.get(backup).get('offDuty'):
                        _spawn_at_room_door(state, backup, pick.get('room'), agents, grid, doors)
        else:
            chosen_id = None
    else:
        chosen_id = None
    if pick.get('taskType') == 'supervise' and chosen_id is None:
        # Director-only card: wait for the named supervisor instead of leaking
        # it to an unrelated worker on the global round-robin.
        return _ASSIGN_WAIT
    if pick.get('directedTo') and chosen_id is None:
        # The lead named this worker. Wait for them (and wake them when they
        # are dormant) rather than handing their card to someone else.
        return _ASSIGN_WAIT
    if pick.get('issueKey') and chosen_id is None and not candidates:
        # Player/JIRA work is never shed for lack of an available agent. It
        # waits in the backlog until someone can take it. When candidates DO
        # exist, fall through to the round-robin below -- the old unconditional
        # check stranded every unpinned issue card before assignment.
        return _ASSIGN_WAIT
    if (pick.get('taskType') == 'report' and pick.get('directRoute')
            and chosen_id is None):
        # A directly-routed report belongs to the named agent, not to whoever
        # is next in the round-robin. Wait for that agent to free up; a
        # report about a specific person's takeaway must not be written by a
        # different person (or an unrelated team's worker).
        return _ASSIGN_WAIT
    if chosen_id is None:
        if not candidates:
            return _ASSIGN_WAIT if pick.get('issueKey') else None
        # Round-robin over eligible ids (deterministic, stable order of roster).
        rr = state.setdefault('sim', {}).setdefault('rr', {})
        pointer = rr.get('task', 0)
        # First eligible index at/after the pointer.
        ordered = [cid for cid in candidates if cid in agents]
        # One-off and scheduled work (a fire-at-a-time card
        # from queue_once, or a standing cadence sweep) goes
        # to an OFF-DUTY agent: first one who has completed
        # all her sprint work -- the owning team's finisher,
        # or any finisher for teamless work -- else an
        # off-duty agent on a team OTHER than the work's
        # own, so a focus team mid-sprint is never disturbed
        # by borrowed work. SOFT preference, never a hard
        # lock: when no off-duty candidate qualifies the
        # generic preferences below pick as before. Sprint
        # cards never reach this branch -- their hard pool
        # restriction already narrowed `candidates`.
        one_off_routed = False
        if _one_off_routing_applies(pick) and sprint_pool is None:
            off_duty = [cid for cid in ordered
                        if (agents.get(cid) or {}).get('offDuty')]
            if off_duty:
                item_team = pick.get('teamId')
                finished = [cid for cid in off_duty
                            if _agent_sprint_work_done(state, cid)
                            and (not item_team
                                 or _on_item_team(state, cid, item_team))]
                if finished:
                    ordered = finished
                    one_off_routed = True
                elif item_team:
                    borrowed = [cid for cid in off_duty
                                if not _on_item_team(state, cid, item_team)]
                    if borrowed:
                        ordered = borrowed
                        one_off_routed = True
        # Gap: a story filed against a specific team
        # (pick['teamId']) had zero team preference at assignment -- only
        # reviewer selection (_pick_reviewer_ids) ever preferred same-team.
        # SOFT preference only, same shape as that precedent: narrow to the
        # team's own free members when any exist, else fall back to the full
        # think tank pool unchanged. Never a hard lock (unlike an incident's
        # on-call pin) -- a small think tank can't afford to starve one team's
        # queue while another sits idle just because its own members are busy.
        team_id = pick.get('teamId')
        if team_id and not one_off_routed:
            team_members = set(_sim_direct_reports(state, team_id))
            # Cross-team borrowing: an agent loaned to this team (roster `loan`
            # tag) is part of the assignment pool for the loan's duration, so
            # the borrower team's sprint items are soft-preferred to them too.
            for d in (state.get('agentRoster') or []):
                if d.get('loan') and d['loan'].get('teamId') == team_id:
                    team_members.add(d['id'])
            team_ordered = [cid for cid in ordered if cid in team_members]
            if team_ordered:
                ordered = team_ordered
        if not ordered:
            return _ASSIGN_WAIT if pick.get('issueKey') else None
        # Fault-aware routing: soft-prefer a candidate with no
        # recent unfaded failure signal over the plain round-robin order --
        # same "soft preference, never a hard lock" shape as the team
        # preference just above. If EVERY eligible candidate is currently
        # cooling down, fall back to the full ordered list rather than ever
        # blocking a real assignment over a transient signal.
        cool = [cid for cid in ordered if _agent_failure_score(state, cid, now_ms) < FAILURE_COOLDOWN_THRESHOLD]
        if cool:
            ordered = cool
        # Feature affinity: SOFT pull toward an agent who has completed similar
        # work before (same room / feature / product), so a team's backlog items
        # gravitate to the person who already knows that corner of the tank.
        # Same "soft preference, never a hard lock" shape: only narrow when
        # someone has a nonzero history, ties preserve roster order, and a
        # candidate with no matching history is never forced off the queue.
        scored = [(cid, _feature_affinity(state, cid, pick)) for cid in ordered]
        best_affinity = max((s for _, s in scored), default=0)
        if best_affinity > 0:
            ordered = [cid for cid, s in scored if s == best_affinity]
        else:
            # Onboarding: with nobody having prior work that matches this item
            # (a brand-new team member has NO history, so feature affinity can
            # never route to them -- they'd fall to pure round-robin), soft-pull
            # toward the candidate whose DESCRIPTION fits the work. Same soft,
            # never-a-lock shape: only narrow when someone's description actually
            # matches, and ties keep roster order.
            desc = [(cid, _description_affinity(state, cid, pick)) for cid in ordered]
            best_desc = max((s for _, s in desc), default=0)
            if best_desc > 0:
                ordered = [cid for cid, s in desc if s == best_desc]
        rr_pick = ordered[pointer % len(ordered)]
        rr['task'] = (pointer + 1) % max(1, len(ordered))
        ranked = policies.rank_assignees(state, pick, ordered)
        policy_pick = ranked[0]['agentId'] if ranked else None
        chosen_id = policy_pick if (policies.apply_enabled() and policy_pick) else rr_pick
        policies.record_shadow('assignment', {
            'chosenByPolicy': policy_pick,
            'roundRobin': rr_pick,
            'ranked': ranked[:5],
        }, {'title': (pick.get('title') or '')[:120],
            'taskType': pick.get('taskType'),
            'room': pick.get('room')})
    # Sprint staffing invariant: whoever was chosen must be inside the sprint's
    # pool. Candidates were already narrowed above, so this only trips for a
    # path that bypassed the narrowed list (e.g. an incident backup re-derive);
    # returning None makes the card wait rather than hand a sprint card to a
    # non-staffed worker.
    if sprint_pool is not None and chosen_id not in sprint_pool:
        return None
    chosen = agents.get(chosen_id)
    if chosen and chosen.get('offDuty'):
        appear_from_outskirts(state, chosen_id, doors)
    extra = {
        'research': pick.get('research'),
        'taskType': pick.get('taskType'),
        'approvedTools': pick.get('approvedTools'),
        'skillReview': pick.get('skillReview'),
        # CS329A takeaway #2: thread the review checklist through
        # to the assigned task so the Python review executor can grade each
        # requirement (code/jev/human) instead of a single all-or-nothing
        # verdict. Same class of gap as 'distill' two lines down.
        'checklist': pick.get('checklist'),
        # Bug: 'distill' was missing from this
        # whitelist entirely, same class of gap as skillReview -- a distill
        # sweep task assigned through here never carried the flag onto the
        # real task object, so _peer_gated_lane's distill exemption (added to
        # fix the exact same infinite-review-loop bug skillReview already hit)
        # silently never applied. See assign_task's own 'distill' field below,
        # which was equally missing.
        'distill': pick.get('distill'),
        'kbClass': pick.get('kbClass'),
        'dependsOn': pick.get('dependsOn'),
        # Phase E2b: a SPIKE's advisory time-budget (the executor keeps its
        # single model call short to honor it).
        'budgetMs': pick.get('budgetMs'),
        # A breakdown story's size estimate rides onto the task so executors and
        # dashboards can see effort (see queue_work / assign_task).
        'sizeEstimate': pick.get('sizeEstimate'),
        'reviewOf': pick.get('reviewOf'),
        'reviewAuthorId': pick.get('reviewAuthorId'),
        'issueKey': pick.get('issueKey'),
        'requestId': pick.get('requestId'),
        'sequence': pick.get('sequence'),
        'templateId': pick.get('templateId'),
        # Phase E2d: an INCIDENT routes to the owning team's on-call (taskType
        # 'bug' + productId + assignedTo travel so the fix is attributable and
        # the cap can be enforced against the open task set).
        'incident': bool(pick.get('incident')),
        # Keep the owning team on the assigned task (see assign_task).
        'teamId': pick.get('teamId'),
        'sourcePaths': pick.get('sourcePaths'),
        'reportPath': pick.get('reportPath'),
        'productId': pick.get('productId'),
        # Adversarial (winter) village tags thread onto the task (see
        # assign_task / queue_work) so completion can be read per side.
        'adversarialTaskId': pick.get('adversarialTaskId'),
        'villageId': pick.get('villageId'),
        # A player-filed card's contract survives onto the task (see assign_task).
        'userStory': pick.get('userStory'),
        'acceptanceCriteria': pick.get('acceptanceCriteria'),
        'never': pick.get('never') or None,
        # Ordered-pipeline marker (see queue_work / assign_task): threads the
        # pipelineStep onto the real task so the strict-order sweep can read
        # completion back (and the content dispatcher can route the step).
        'pipelineStep': pick.get('pipelineStep'),
        # Bot Ops / shadow mode: thread the dry-run flag onto the assigned task
        # (see assign_task) -- same class of whitelist as 'distill'/'checklist'.
        'shadow': pick.get('shadow'),
        # Item 4: the groomer-judged spend ceiling + band survive onto the task
        # (see assign_task, which resolves the budget). Same whitelist class as
        # 'distill'/'checklist': dropping them would leave the task unbudgeted.
        'budgetUsd': pick.get('budgetUsd'),
        'budgetBand': pick.get('budgetBand'),
        # Bell-style spare-time lane: the protected-exploration flag rides onto
        # the assigned task (see assign_task) so review/failure paths can honor
        # it -- same whitelist contract as 'distill'/'checklist'.
        'moonshot': bool(pick.get('moonshot')),
        # Attention lane (build/reading/open/parking-lot) rides onto the task so
        # the board and the reading rate-limit can see it (see assign_task).
        'lane': pick.get('lane'),
        # Player-authored provenance rides onto the assigned task (see
        # assign_task) -- the JEV gate's work-context bypass trusts only tasks
        # the PLAYER wrote, so dropping this here would silently downgrade a
        # player-vetted task to agent-authored.
        'playerAuthored': pick.get('playerAuthored'),
        # A scheduled item's notBefore rides onto the assigned task so the
        # preemption picker (_suspend_busy_agent_for_scheduled) can tell
        # TIME-CRITICAL work (this card was scheduled for a specific time; the
        # agent must not be yanked off it for a one-off) from ordinary
        # work. Same whitelist contract as 'distill'/'checklist'.
        'notBefore': pick.get('notBefore'),
        # MEETING card fields ride onto the assigned task (see assign_task)
        # so the minutes executor names the resolved participants and the
        # convening facilitator -- dropping them here would leave the task
        # with an empty attendee list even though queue_work preserved them.
        'attendees': pick.get('attendees'),
        'facilitatorId': pick.get('facilitatorId'),
    }
    # Cut 2 coaching + runbook injection: append the chosen agent's pending
    # growth-plan note (applied once) and any product runbook knowledge to the
    # task's instructions -- manager feedback / prior-incident learning changes
    # THIS task's execution.
    instructions = _augment_task_instructions(
        state, chosen_id, pick.get('productId'), pick.get('instructions'),
        refocus=bool(pick.get('reviewOf') or pick.get('incident')),
        scoped=bool(pick.get('playerAuthored') or pick.get('reviewOf')
                    or pick.get('issueKey')))
    task = assign_task(state, chosen_id, pick.get('title'), pick.get('room'),
                       instructions, pick.get('projectLabel') or pick.get('goal'), extra,
                       grid, doors, now_ms, task_id_holder or _TASK_ID_HOLDER)
    if not task:
        # A chosen agent with no path is a temporary map/crowd failure, not a
        # reason to destroy the card. The old 3-attempt cap burned in seconds
        # and silently abandoned real work (including the first expansion
        # stories). Wait a minute and retry; never shed the work here.
        pick['notBefore'] = now_ms + 60_000
        return _ASSIGN_WAIT
    # W1: a `pair` card recruits a navigator server-side (deterministic
    # round-robin, zero JEV). The driver keeps the solo walk when no second
    # eligible hand exists -- graceful degradation, never a stuck card.
    if task and pick.get('pair') and not pick.get('reviewOf') and not pick.get('incident'):
        _recruit_pair_navigator(state, chosen_id, task, can_wake_off_duty,
                                grid, doors, now_ms)
    return task

# ---------------------------------------------------------------------------
# Operator-policy binding runs LAST: every policy-backed literal above is
# defined by this point, so the rebind sees the module's own defaults and
# then overrides them from config.OPERATOR_POLICY (in-place dict, refreshed
# by get_operator_policy / reload_policy_config). bind_policy registered the
# declarative pairs above; this end-of-import rebind catches any policy-backed
# constant re-defaulted between that point and module end, and every
# config.reload_policy_config() run re-applies the whole registered set.
# ---------------------------------------------------------------------------
apply_policy_bindings('sim')
