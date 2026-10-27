

def inject_wiki_context(state, task, village_id=None, now_ms=None):
    """Pure: build the 'before you act, here's what the think tank knows' context
    block for a task, from the wiki pages for its room. Returns a non-empty
    string only when there ARE relevant pages; empty when the wiki has none
    (caller still runs the executor with a blank context).

    Expiry: a page whose reviewAfterMs has passed is skipped (recheck before
    acting). Two-clock: a FAST-clock page is still injected (so the agent knows
    the knowledge exists) but explicitly flagged to re-fetch the live source
    rather than trust the copy -- the "fast clock should be fetched" rule."""
    room = (task or {}).get('room')
    if not room:
        return ''
    now_ms = now_ms if now_ms is not None else _time_ms()
    if village_id is None:
        # Resolve the reader's village from the task's agent so a worker only
        # ever sees their own village's memory.
        village_id = village_of_agent(state, (task or {}).get('agentId'))
    page_ids = (task or {}).get('wikiPageIds')
    if page_ids:
        pages = [(ensure_wiki(state)).get(pid) for pid in page_ids
                 if (ensure_wiki(state)).get(pid)
                 and ((ensure_wiki(state)).get(pid).get('villageId') or DEFAULT_VILLAGE) == village_id
                 and not _page_is_expired((ensure_wiki(state)).get(pid), now_ms)]
    else:
        pages = wiki_read_pages(state, room, village_id=village_id, now_ms=now_ms)
    if not pages:
        return ''
    lines = ['The think tank knowledge base has these entries relevant to this work:', '']
    for rec in pages:
        line = f"- {rec.get('title')} (category: {rec.get('category')}, v{rec.get('version')})"
        if (rec.get('clock') or SLOW_CLOCK) == FAST_CLOCK:
            line += ' [LIVE DATA: re-check the current source before acting on this]'
        lines.append(line)
    return '\n'.join(lines) + '\n'


def release_uses_handle(state, product_id):
    """Pure: does this product's build carry a Phase D capability handle? The
    build executor uses it to call an external service without ever holding the
    raw secret. Returns the first handle (a nonce string) or None."""
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return None
    handles = record.get('handles') or []
    return handles[0] if handles else None


def active_agent_count(state):
    """Port of tasks.js activeAgentCount: agents not off-duty (admins included)."""
    count = 0
    for aid, a in (state.get('agents') or {}).items():
        if a and not a.get('offDuty'):
            count += 1
    return count


def can_activate_another(state):
    """Port of tasks.js canActivateAnother."""
    return active_agent_count(state) < MAX_ACTIVE_AGENTS


def _awake_idle_count(state):
    """Port of tasks.js runTaskCycleBody._awakeIdleCount: non-admin, on-duty,
    not busy/task/pairWith/offDuty."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    n = 0
    for d in roster:
        if d.get('isAdmin'):
            continue
        a = agents.get(d.get('id'))
        if a and not a.get('busy') and not a.get('task') and not a.get('pairWith') and not a.get('offDuty'):
            n += 1
    return n


def _any_available_including_off_duty(state):
    """Port of tasks.js runTaskCycleBody._anyAvailableIncludingOffDuty: a non-admin
    agent not busy/task/pairWith; if off-duty, only counts when the active ceiling
    has room to wake her."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    for d in roster:
        if d.get('isAdmin'):
            continue
        a = agents.get(d.get('id'))
        if not a or a.get('busy') or a.get('task') or a.get('pairWith'):
            continue
        if a.get('offDuty'):
            if not can_activate_another(state):
                continue
        return True
    return False


def _eligible_candidates(state, include_off_duty):
    """Port of tasks.js assignTaskViaJev's candidate filter: non-admin, not
    busy/task/pairWith, and (unless include_off_duty) not off-duty.
    Returns list of agent ids in roster order (deterministic base for RR)."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    out = []
    for d in roster:
        if d.get('isAdmin'):
            continue
        a = agents.get(d.get('id'))
        if not a or a.get('busy') or a.get('task') or a.get('pairWith'):
            continue
        if not include_off_duty and a.get('offDuty'):
            continue
        out.append(d['id'])
    return out


def appear_from_outskirts(state, agent_id, doors=None, grid=None):
    """Wake an off-duty agent ANYWHERE there's free, reachable space. An agent
    waking up appears at a random free outdoor spot reachable from SPAWN
    (pick_free_spot's reachability guarantee) -- the outskirts trailhead doors
    are gone, so she rests/wakes in the main think tank. `doors` is retained for
    call-compat (`_spawn_at_room_door` still prefers a room door for short
    reviewer walks); `grid` may be supplied or loaded lazily."""
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return
    a['offDuty'] = False
    a['visible'] = True
    a['inRoom'] = None
    # An agent being woken is, by definition, newly ready for work: clear any
    # stale busy/task so a ghost (busy+offDuty, e.g. theo) can't wake up still
    # flagged busy -- which would make her visible but ineligible for assignment.
    a['busy'] = False
    a['task'] = None
    if grid is None:
        try:
            grid, _ = _load_outdoor_geometry()
        except Exception:
            grid = None
    if grid is not None:
        agents_map = state.get('agents') or {}
        occupied = [{'x': v['x'], 'y': v['y']} for v in agents_map.values()
                    if isinstance(v, dict) and v.get('visible') and v.get('id') != agent_id]
        spot = pick_free_spot(grid, avoid_points=occupied)
        a['x'], a['y'] = spot['x'], spot['y']


def _spawn_at_room_door(state, agent_id, room, agents, grid, doors):
    """Wake an off-duty agent (a gate reviewer) at the APPROACH of the target
    room's door, standing clear of the building edge -- the same spot assign_task
    walks an assigned agent to. Using the room's own door keeps the reviewer's
    walk short and reliable regardless of the think tank's reachability gaps. Falls
    back to appear_from_outskirts if the room has no door geometry."""
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return
    door = (doors or {}).get(room)
    if not door:
        appear_from_outskirts(state, agent_id, doors)
        return
    a['offDuty'] = False
    a['visible'] = True
    a['inRoom'] = None
    a['busy'] = False
    a['task'] = None
    # Pick a free approach cell, not a fixed nudge. The old nudge pushed the
    # second reviewer into the building wall, so assign_task could not find a
    # path and the review card retried forever (the 2026-10-26 task-649 gate).
    base_x = door['x'] + door['w'] / 2
    base_y = door['y'] + door['h'] + 4
    if not grid:
        a['x'], a['y'] = base_x, base_y
        return
    occupied = {
        (round(other.get('x')), round(other.get('y')))
        for oid, other in (agents or {}).items()
        if oid != agent_id and isinstance(other, dict) and other.get('visible')
        and other.get('x') is not None and other.get('y') is not None
    }
    step = AGENT_W + 8
    candidates = [(base_x, base_y)]
    for ring in (1, -1, 2, -2):
        candidates.append((base_x + ring * step, base_y))
        candidates.append((base_x + ring * step, base_y + AGENT_H + 8))
    chosen = None
    for x, y in candidates:
        if (round(x), round(y)) in occupied:
            continue
        if _blocked_at({'x': x, 'y': y, 'w': AGENT_W, 'h': AGENT_H}, grid):
            continue
        if (x, y) == (base_x, base_y):
            chosen = (x, y)
            break
        if find_path(x, y, base_x, base_y, agent_id, agents, grid):
            chosen = (x, y)
            break
        if find_path(x, y, base_x, base_y, agent_id, {}, grid):
            chosen = (x, y)
            break
    a['x'], a['y'] = chosen or (base_x, base_y)


def assign_task(state, agent_id, title, room, instructions, project_label,
                extra, grid, doors, now_ms, task_id_holder):
    """Port of tasks.js assignTask (deterministic -- no JEV, the chosen agent is
    passed in by the caller; this is the walk+bookkeeping half). Validates the
    agent/room, resolve overflow, finds a door-front path, commits the agent's
    task/path and the durable state['tasks'] mirror, logs task_assigned.
    Returns the task dict, or None on failure (-> caller requeues/attempt++).
    `task_id_holder` is a mutable init([count]) carrying the server's
    nextTaskId counter across ticks (server in-memory, like the JS module var)."""
    room = resolve_room_with_overflow(state, room)
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    door = (doors or {}).get(room)
    if not a or a.get('busy') or a.get('task') or not door:
        return None

    target_x = door['x'] + door['w'] / 2
    target_y = door['y'] + door['h'] + 4
    path = find_path(a['x'], a['y'], target_x, target_y, agent_id, agents, grid)
    if not path:
        # An idle agent standing in a corridor must not permanently block the
        # room. Retry against the STATIC map only. This still honors walls and
        # furniture, but lets a worker walk through a crowd (the same overlap
        # already allowed at a contested door). Without this, a few visible
        # idlers wedged the whole pressoffice queue and every card retried or
        # was abandoned.
        path = find_path(a['x'], a['y'], target_x, target_y, agent_id, {}, grid)
    if not path:
        # A pinned agent woken AT the target door is already standing on the
        # destination. find_path returns no steps, but that is a valid arrival,
        # not an assignment failure. A one-node path lets movement emit the
        # normal 'arrive' event on the next tick.
        if abs(a.get('x', 0) - target_x) < 1 and abs(a.get('y', 0) - target_y) < 1:
            path = [{'x': target_x, 'y': target_y}]
        else:
            return None

    task_id = _next_task_id(state, task_id_holder)
    rest_x = path[-1]['x']
    rest_y = path[-1]['y']
    task = {
        'id': task_id,
        'title': title,
        'room': room,
        'instructions': instructions,
        'projectLabel': project_label,
        'research': (extra or {}).get('research'),
        'taskType': (extra or {}).get('taskType', 'code'),
        'deliverable': (extra or {}).get('deliverable') or None,
        'wordLimit': (extra or {}).get('wordLimit') or None,
        # A scheduled item's notBefore rides onto the task (see _assign_due_item)
        # so the preemption picker can recognize TIME-CRITICAL work: a card that
        # was scheduled for a specific time must not be yanked off the agent for
        # a one-off. Standing cadence cards already mark themselves via
        # research/skillReview/distill; notBefore covers queue_once one-offs.
        'notBefore': (extra or {}).get('notBefore'),
        'skillReview': bool((extra or {}).get('skillReview')),
        'distill': bool((extra or {}).get('distill')),
        'kbClass': (extra or {}).get('kbClass') or None,
        'dependsOn': (extra or {}).get('dependsOn') or None,
        'budgetMs': (extra or {}).get('budgetMs'),
        'sizeEstimate': (extra or {}).get('sizeEstimate'),
        'reviewOf': (extra or {}).get('reviewOf'),
        'reviewAuthorId': (extra or {}).get('reviewAuthorId'),
        'issueKey': (extra or {}).get('issueKey'),
        'requestId': (extra or {}).get('requestId'),
        'sequence': (extra or {}).get('sequence'),
        'templateId': (extra or {}).get('templateId'),
        # CS329A takeaway #2: checklist survives onto the task
        # object itself (see _assign_due_item's extra dict) -- the review
        # executor grades per-requirement from here.
        'checklist': list((extra or {}).get('checklist') or []),
        'incident': bool((extra or {}).get('incident')),
        # The owning team must survive onto the durable task: supervision,
        # team policy, and per-team budget attribution all read task.teamId.
        'teamId': (extra or {}).get('teamId'),
        'sourcePaths': (extra or {}).get('sourcePaths'),
        'reportPath': (extra or {}).get('reportPath'),
        'productId': (extra or {}).get('productId'),
        # Adversarial (winter) village tags (see queue_work / _assign_due_item)
        # -- stamped so serve._adversarial_village_tick can detect a side done.
        'adversarialTaskId': (extra or {}).get('adversarialTaskId'),
        'villageId': (extra or {}).get('villageId'),
        # A player-filed card's contract: the user story + acceptance criteria
        # ride onto the task so the coding executor's prompt includes the full
        # spec (content.py folds them into the backlog line it builds from
        # task.instructions).
        'userStory': (extra or {}).get('userStory'),
        'acceptanceCriteria': (extra or {}).get('acceptanceCriteria'),
        'never': (extra or {}).get('never') or None,
        'pipelineStep': (extra or {}).get('pipelineStep'),
        # Bot Ops / shadow mode: a dry-run task (see queue_work). Stamped so the
        # completion path (_task_cycle) captures to the shadow ledger instead of
        # shipping.
        'shadow': bool((extra or {}).get('shadow')),
        # Item 4: the per-task model-spend ceiling, resolved AT ASSIGNMENT and
        # stamped onto the durable task. Explicit budgetUsd wins; else the task
        # type's tier; else the type default, scaled by the refinement band. The
        # /api/chat gate (serve.py) reads THIS number as the ceiling; the sim
        # loop's budgetExhausted branch is the enforcement that fails the card
        # closed + notifies the owning director. budgetUsd 0.0 = no ceiling.
        'budgetBand': (extra or {}).get('budgetBand') or 'standard',
        'budgetUsd': budget_usd_for_task(
            (extra or {}).get('taskType', 'code'),
            (extra or {}).get('budgetUsd'),
            (extra or {}).get('budgetBand'),
            # Wave 3: this room/team/product/type's overrun history
            # scales the type-derived ceiling (never an explicit one).
            _overrun_factor_for(state, room, extra)),
        # Bell-style spare-time lane: the protected-exploration flag (a free
        # spike's failures are never mined into operator rules).
        'moonshot': bool((extra or {}).get('moonshot')),
        # Attention lane (build/reading/open/parking-lot) -- see LANES. Rides
        # onto the durable task so the board can show it and the reading
        # rate-limit can count it as the room's one active slow thread.
        'lane': (extra or {}).get('lane'),
        # Player-authored provenance: True only when the PLAYER wrote this
        # task's work text (see queue_work / _assign_due_item). The JEV gate's
        # work-context bypass trusts only these tasks; an agent-authored task
        # must never be able to manufacture a bypass by naming a URL in its
        # own story text.
        'playerAuthored': bool((extra or {}).get('playerAuthored')),
        # MEETING card fields (queue_work whitelists them): the resolved
        # attendee list the minutes name, and the facilitator who convenes
        # (the assignment pin -- see _assign_due_item).
        'attendees': list((extra or {}).get('attendees') or []),
        'facilitatorId': (extra or {}).get('facilitatorId') or None,
        'assignedTo': agent_id,
        'approvedTools': list((extra or {}).get('approvedTools') or []),
        'status': 'walking',
        'createdAt': now_ms,
        'entryX': rest_x,
        'entryY': rest_y,
    }
    state.setdefault('tasks', {})[task_id] = task
    a['task'] = task_id
    a['path'] = path
    a['pathIndex'] = 0
    a['pathTarget'] = {'x': target_x, 'y': target_y}
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    from serve import log_action
    log_action(agent_id, 'task_assigned',
               {'taskId': task_id, 'title': title, 'room': room}, authorized=True)
    return task


def _cancel_at_task(state, agent_id):
    """Port of tasks.js cancelTask (tasks.js:1197): an agent whose task-walk is
    genuinely impossible gives up -- task marked 'cancelled', agent fully
    released (task/path/respawn cleared) so she goes idle and is available for
    the next assignment instead of looping forever on a route that can't work.
    This is the dispatch half of the movement 'cancel' event (previously logged
    and dropped, leaving the agent stuck -- vela, replanCount 55,350), and the
    terminal release for _repair_stalled_walkers' unreachable cases. Unlike
    finish_task/_release_agent_gated it does NOT bump approvals or record a
    completed room -- a gave-up walk is not shipped work. Mirrors the JS: the
    cancelled task is NOT requeued (a human can re-file it; requeueing an
    unwalkable task would just churn agents into the same wall). Idempotent,
    pure on `state`. Returns the cancelled task (or None)."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return None
    task_id = a.get('task')
    tasks = state.get('tasks') or {}
    task = tasks.get(task_id) if task_id else None
    if task:
        task['status'] = 'cancelled'
    a['task'] = None
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    return task


def _arrive_at_task(state, agent_id, now=None):
    """Port of tasks.js arriveAtTask's positioning/lifecycle half (no per-room
    content dispatch -- that's the next slice, short-circuited via workUntil)."""
    now = time.time() if now is None else now
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    task = (state.get('tasks') or {}).get(a.get('task'))
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    if not task:
        return
    a['visible'] = False
    a['busy'] = True
    a['inRoom'] = task['room']
    # Position at the room's first interactable, else room center -- ROOM_NATIVE
    # coordinates. First workstations interactable zone is the top-left desk's
    # front strip: {x:84, y:210, w:120, h:15} (desk0 {84,105,120,105} +
    # TERMINAL_ZONE_DEPTH=15). Mirrors tasks.js arriveAtTask's
    # ROOM_INTERACTABLES[collision][0].zone logic. Stage X: enough to be inside
    # the room at a desk; exact per-room desk data is data-only, not behavioral.
    zone_x, zone_w = 84, 120      # first workstations desk x/w (ROOM_COLLISIONS)
    zone_y_top = 105 + 105        # desk0.y + desk0.h  -> zone top = 210
    a['roomX'] = zone_x + zone_w / 2
    a['roomY'] = zone_y_top - 10
    a['dir'] = 'south'
    task['status'] = 'working'
    # Content dispatch. Phase 3 slice 2: for a room with a ported content
    # executor registered on the engine (research for now), fire the REAL async
    # work on a background thread and give it TASK_CONTENT_TIMEOUT_S rather than
    # the slice-1 placeholder budget; its completed result is merged by the next
    # task_cycle pass. Rooms without an executor keep the workUntil
    # short-circuit (an agent "works" the budget then completes -- the accepted
    # interim behavior, slice 1). Uses the SAME clock as the tick/completion
    # loop (caller's `now`): a mixed clock would never let a task complete.
    # `workUntil` is ALWAYS set: the content path installs its own (longer)
    # budget, everything else the short placeholder.
    dispatched = _dispatch_content_work(_content_executor, state, agent_id, task, now)
    if not dispatched:
        task['workUntil'] = now + TASK_WORK_DURATION_S


def _arrive_at_pair(state, agent_id, now=None):
    """W1: server-owned pair arrival. The movement engine already emits
    ('arrive','pair',aid) when a navigator holding `pairWith` reaches her
    path's end -- but the server dispatch dropped it (only ('arrive','task')
    resolved), so a pair session's navigator simply never joined the driver:
    she stood at the door-front forever, invisible to the park/heal sweeps
    (which skip pairWith holders). Mirrors the client's arriveAtPair: the
    navigator arrives at the shared workstation, becomes busy beside the
    driver, and the driver's task is what she rides along on. Idempotent,
    pure on `state`. Returns True when the pair session actually resolved."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return False
    driver_id = a.get('pairWith')
    task_id = a.get('pairTaskId')
    task = (state.get('tasks') or {}).get(task_id) if task_id else None
    driver = agents.get(driver_id) if driver_id else None
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    if not driver or not task:
        # The session's other half is gone (driver released / task vanished);
        # release the navigator cleanly instead of parking her forever.
        _release_stalled_interaction(state, agent_id)
        return False
    a['visible'] = False
    a['busy'] = True
    a['inRoom'] = task.get('room')
    # Right beside the driver at the same desk -- mirrors arriveAtPair's own
    # "nav.roomX = driver.roomX + 25" convention.
    a['roomX'] = (driver.get('roomX') or 0) + 25
    a['roomY'] = driver.get('roomY') or 0
    a['dir'] = 'south'
    try:
        from serve import log_action
        log_action(agent_id, 'pair_arrived', {'taskId': task_id, 'driver': driver_id},
                   authorized=True)
    except Exception:
        pass
    return True


def _arrive_at_handoff(state, agent_id, now=None):
    """W1: server-owned handoff arrival. The movement engine already emits
    ('arrive','handoff',aid) when a walker holding `handoff` reaches her
    recipient -- but the server dispatch dropped it, so a client-started (or
    server-started) handoff walk arriving server-side was never delivered: the
    walker froze beside the recipient, invisible to the heal sweeps (which skip
    handoff holders). Mirrors the client's arriveAtHandoff's release half:
    the walker delivers the handoff (recorded, the finished-title line as the
    message), then clocks off duty. Idempotent, pure on `state`. Returns True
    when the handoff was actually delivered."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return False
    handoff = a.get('handoff')
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    a['handoff'] = None
    a['dir'] = 'south'
    # Clocking off is what finishTask defers to let the handoff play out --
    # happens regardless of whether the recipient is still around.
    a['offDuty'] = True
    a['visible'] = False
    if not isinstance(handoff, dict):
        return False
    recipient_id = handoff.get('toId')
    title = handoff.get('title') or 'handoff'
    recipient = agents.get(recipient_id) if recipient_id else None
    # The delivered line is the finished-title (the client would run a model
    # call to phrase it; the server records the deterministic same fact, so the
    # dependency IS the message -- no invented phrasing).
    delivered = {
        'fromId': agent_id, 'toId': recipient_id, 'title': title,
        'ts': int((now if now is not None else time.time()) * 1000),
    }
    state.setdefault('handoffs', []).append(delivered)
    if recipient and isinstance(recipient, dict):
        recipient['contactedAt'] = int((now if now is not None else time.time()) * 1000)
    try:
        from serve import log_action
        log_action(agent_id, 'handoff_delivered',
                   {'to': recipient_id, 'title': (title or '')[:120]}, authorized=True)
    except Exception:
        pass
    return True




def _revoke_task_access(task_id):
    """Best-effort per-story capability-grant cleanup: when a story ships, the
    temp access grant tied to it dies with it. Mirrors _revoke_agent_credentials'
    lazy serve import so sim.py stays unit-testable offline; a DB failure must
    never block the completion path."""
    if not task_id:
        return
    try:
        import serve
        serve.revoke_task_access(task_id)
    except Exception:
        pass


def roster_view_from_state(state):
    """A roster.Roster view of the sim state, or None when
    the state carries no agentRoster at all.

    agentRoster supplies team membership (a team is keyed
    by its director), each agent's offDuty flag supplies
    duty status, and each team record's optional `focus`
    and `doNotDisturb` keys supply the disturbance policy
    that focused and do-not-disturb teams enforce. Callers
    that get None fall back to a weaker gate; the view is
    rebuilt per use so it always reflects the live state."""
    entries = state.get('agentRoster')
    if not isinstance(entries, list) or not entries:
        return None
    view = roster.Roster()
    for team in (state.get('teams') or []):
        if not isinstance(team, dict) or not team.get('id'):
            continue
        view.add_team(
            team['id'],
            focused_task=team.get('focus'),
            do_not_disturb=bool(team.get('doNotDisturb')),
        )
    agents = state.get('agents')
    agents = agents if isinstance(agents, dict) else {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        agent_id = entry.get('id')
        team = entry.get('director')
        if not agent_id or not team:
            continue
        if view.agent(agent_id) is None:
            view.add_agent(agent_id, team)
        agent = agents.get(agent_id)
        off_duty = bool(agent.get('offDuty')) if isinstance(agent, dict) else False
        view.set_status(agent_id, roster.OFF_DUTY if off_duty else roster.ON_DUTY)
    return view


def _signal_plans_from_state(state):
    """Rebuild the durable signal plans (state['signalPlans'])
    as signals.WakePlan objects. Each plan's scenario dicts
    carry their own `armed` flag -- the durable home of arming
    -- so a restart resumes exactly where the think tank left
    off: a scenario that fired stays disarmed, a rearm-marked
    one fires again on the next match."""
    plans = []
    for declaration in (state.get('signalPlans') or []):
        if not isinstance(declaration, dict) or not declaration.get('planId'):
            continue
        scenarios = []
        for scenario in (declaration.get('scenarios') or []):
            if not isinstance(scenario, dict) or not scenario.get('scenarioId'):
                continue
            scenarios.append(signals.Scenario(
                scenario_id=scenario['scenarioId'],
                name=scenario.get('name') or scenario['scenarioId'],
                condition=scenario.get('condition') or '',
                action=scenario.get('action') or '',
                exit_strategy=scenario.get('exitStrategy') or '',
                armed=bool(scenario.get('armed', True)),
                rearm=bool(scenario.get('rearm')),
            ))
        plans.append(signals.WakePlan(
            plan_id=declaration['planId'],
            agent_id=declaration.get('agentId') or '',
            team=declaration.get('team') or '',
            subject=declaration.get('subject') or '',
            task=declaration.get('task'),
            active_when=declaration.get('activeWhen'),
            scenarios=scenarios,
        ))
    return plans


def _write_back_signal_plans(state, plans):
    """Persist scenario arming back into state['signalPlans']
    (the durable home): a scenario that fired without `rearm`
    stays disarmed across restarts. Declarations and scenarios
    that vanished between the rebuild and the write are left
    untouched."""
    by_id = {plan.plan_id: plan for plan in plans}
    for declaration in (state.get('signalPlans') or []):
        if not isinstance(declaration, dict):
            continue
        plan = by_id.get(declaration.get('planId'))
        if plan is None:
            continue
        scenario_declarations = {s.get('scenarioId'): s for s in
                                 (declaration.get('scenarios') or [])
                                 if isinstance(s, dict) and s.get('scenarioId')}
        for scenario in plan.scenarios:
            target = scenario_declarations.get(scenario.scenario_id)
            if target is not None:
                target['armed'] = scenario.armed


def _signal_condition_matches(data_text, condition):
    """The wiring's condition evaluator: token containment.
    The data string is a flat line of key=value tokens, so a
    multi-token condition like 'status=done room=observatory'
    must match a completion whose data names BOTH tokens, in
    any order, with other tokens in between -- which a raw
    substring test cannot express. An empty condition matches
    everything (as an empty substring would)."""
    tokens = set(data_text.split())
    return all(token in tokens for token in condition.split())


def _task_completed_data(task):
    """The signal data string a completed task emits: a flat
    key=value line. Condition matching is token containment
    (the evaluator emit_signals installs), so a scenario
    condition like 'status=done room=observatory' fires on
    exactly the completions whose data names both."""
    bits = ['status=done',
            f"title={task.get('title') or ''}",
            f"room={task.get('room') or ''}"]
    if task.get('teamId'):
        bits.append(f"team={task['teamId']}")
    if task.get('taskType'):
        bits.append(f"taskType={task['taskType']}")
    return ' '.join(bits)


def task_completed_data(task):
    """Public alias for _task_completed_data: the signal
    data line a completed task emits. serve.py exposes
    it to signal feeds (a trading ticker watcher, a
    cron) that name a task instead of a raw data
    string."""
    return _task_completed_data(task)


def emit_signals(state, subject, data):
    """Evaluate every durable signal plan against one emission
    (subject + data string) and wake the matching agents. The
    plans are rebuilt from state and evaluated by a SignalWaker
    constructed from THOSE SAME plan objects -- a second
    rebuild would make the arming write-back below a no-op,
    since the disarming a fired scenario performs happens on
    the objects the waker holds. Every wake is logged as
    governance. Never raises: a signal failure must not break
    the emission point (a task completion, a browse, ...).
    Returns the WakeEvents fired."""
    try:
        agents = state.get('agents') or {}
        plans = _signal_plans_from_state(state)
        if not plans:
            return []

        view = roster_view_from_state(state)

        def _wakeable(agent_id, _task):
            agent = agents.get(agent_id)
            if agent is None or not _is_fully_idle(agent, None):
                return False
            # A roster view adds the disturbance policy: signals
            # wake OFF-DUTY agents only, and a focused or
            # do-not-disturb team is not disturbed for work it
            # did not focus on. The plan's task field is the
            # work tag a focused team matches on. No roster in
            # state keeps the weaker idle-only gate.
            if view is not None:
                return view.wakeable(agent_id, _task)
            return True

        waker = signals.SignalWaker(
            evaluator=_signal_condition_matches,
            wakeable=_wakeable,
        )
        for plan in plans:
            waker.arm(plan)
        events = waker.evaluate(subject, data)
        _write_back_signal_plans(state, plans)
        for event in events:
            _log_governance(state, event.agent_id, 'signal_wake', {
                'planId': event.plan_id, 'agentId': event.agent_id,
                'team': event.team, 'subject': subject,
                'task': event.task, 'scenarioId': event.scenario_id,
                'action': event.action, 'exitStrategy': event.exit_strategy,
            })
        return events
    except Exception:
        return []


_DELIVERABLE_TASK_TYPES = frozenset({'code', 'bug', 'research', 'spike', 'report'})


def _completion_has_evidence(state, task):
    """A primary deliverable card may not close on an empty work channel.

    A code/story card needs a saved Library artifact. A spike/research/report
    needs both the artifact and its findings note. A bug needs a landed file,
    a restore check, or a Library artifact. Meetings, supervision, review/QA,
    avatars, and shadow runs close on their own records. A recorded zero grade
    is always a refusal."""
    if not task:
        return True
    ttype = str(task.get('taskType') or '')
    if task.get('shadow'):
        return True
    if task.get('noop'):
        # A recorded legitimate no-op (no feeds configured, nothing new to
        # distill) has no artifact to require -- demanding one looped the same
        # card forever.
        return True
    # Peer-review and QA subtasks land their verdict in the note; their
    # evidence is the vote, not a Library file. A REPAIR task also carries
    # reviewOf, but its taskType is the original lane (spike/code/...), so it
    # must still prove it saved a corrected deliverable. Treating every
    # reviewOf task as evidence-exempt let a "NOT COMPLETED" repair report
    # close as done (BEN-6).
    if task.get('taskType') in ('review', 'qa'):
        return True
    if ttype in ('meeting', 'supervise', 'avatar', 'review', 'qa'):
        return True
    # Explicit deliverable contract (file_issue -> refinement -> queue ->
    # task). A card says what it must produce, so the gate never has to infer
    # from the title. A bug that is really a missing report is minted as a
    # spike with deliverable='report'; a code bug keeps deliverable='code'.
    deliverable = str(task.get('deliverable') or '').strip().lower()
    if deliverable in ('meeting', 'supervision', 'review'):
        return True
    if deliverable in ('report', 'note', 'code'):
        if deliverable == 'report':
            ok = bool(task.get('libraryPath') or task.get('reportPath')) and bool(
                str(task.get('note') or '').strip())
        elif deliverable == 'note':
            ok = bool(str(task.get('note') or '').strip())
        else:
            ok = bool(task.get('libraryPath') or task.get('writtenPy')
                      or task.get('restoreCheck') or task.get('codePublished'))
        if not ok:
            return False
        grade = task.get('grade')
        if isinstance(grade, (int, float)) and grade <= 0:
            return False
        return True
    if (ttype == 'research' or task.get('research') or task.get('distill')
            or task.get('skillReview')):
        # Scheduled research, distill, and skill-review cards record their
        # outcome in the task note (a distill writes the wiki, a skill review
        # promotes/rejects files); none of them files a per-task Library
        # artifact. Require the note, not a fabricated path. They are queued
        # as taskType 'code', so the generic code rule below would otherwise
        # refuse every successful distillation.
        return bool(str(task.get('note') or '').strip()) or bool(task.get('wikiPage'))
    if ttype == 'bug':
        # codePublished is the coding lane's validated-publish signal. A bug
        # fix that shipped a real staged change must not be refused for a
        # missing Library record, the same way code/story cards are accepted.
        evidence = bool(task.get('libraryPath') or task.get('writtenPy')
                        or task.get('restoreCheck') or task.get('codePublished'))
        if not evidence:
            return False
    elif ttype in ('spike', 'report'):
        # Report-lane tasks record reportPath (the file they wrote), not the
        # generic libraryPath; refusing that completion made a successful
        # report look like a missing deliverable and spawned a repair loop.
        artifact = task.get('libraryPath') or task.get('reportPath')
        if not artifact or not str(task.get('note') or '').strip():
            return False
    elif ttype in _DELIVERABLE_TASK_TYPES or task.get('_peerGate'):
        # A published code change is itself the deliverable. The Library
        # record is documentation; a failed Library write must not turn a
        # real, validated publish into a repair card (the live code smoke
        # test refused once for that reason before the record landed).
        if not (task.get('libraryPath') or task.get('codePublished')):
            return False
    grade = task.get('grade')
    if isinstance(grade, (int, float)) and grade <= 0:
        return False
    return True


def _completion_evidence_gap(task):
    """Name the missing evidence so the refusal message matches the gate."""
    ttype = str(task.get('taskType') or '')
    if (task.get('distill') or task.get('research') or task.get('skillReview')
            or ttype == 'research'):
        return ('the task note is empty; record what the run produced in the note '
                '(or the wiki page for a distill)')
    deliverable = str(task.get('deliverable') or '').strip().lower()
    if deliverable == 'report':
        return 'no reportPath or task note'
    if deliverable == 'note':
        return 'the task note is empty'
    if deliverable == 'code':
        return ('no published change, written .py file, restore check, '
                'or Library artifact')
    if ttype == 'bug':
        return ('no published change, written .py file, restore check, '
                'or Library artifact')
    if ttype in ('spike', 'report'):
        return 'no reportPath or task note'
    return 'no saved Library artifact'


def finish_task(state, agent_id, grid=None):
    """Port of tasks.js finishTask: clear task/busy/inRoom, return to entry point
    (with the full-agent-width step-aside deadlock avoidance), bump approvedCount,
    log. The caller then sends the agent off duty (send_agent_off_duty), like the
    JS finishTask does."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    task = (state.get('tasks') or {}).get(a.get('task'))
    completion_refused = False
    completion_gap = None
    if task and task.get('status') not in ('blocked', 'failed', 'cancelled', 'abandoned'):
        if not _completion_has_evidence(state, task):
            completion_refused = True
            completion_gap = _completion_evidence_gap(task)
            task['status'] = 'failed'
            task['failNote'] = f'Completion blocked: {completion_gap}.'
            task['nextAction'] = 'Save or record the deliverable and re-run the acceptance checks.'
            task['deliveryState'] = 'blocked'
            task['manualAttention'] = False
            task['_peerRepairPending'] = True
        else:
            task['status'] = 'done'
            # A landed task satisfies any dependency-block keyed on it: queue
            # the SM's auto-unblock so the cards waiting on this work free up.
            _auto_clear_dependency_blocks(state, task.get('id'),
                                          now_ms=int(time.time() * 1000))
    # W1: a PAIR task's navigator rides along on the driver's task -- when the
    # driver finishes, release her too (pairWith/pairTaskId/busy/inRoom) so she
    # doesn't sit busy at the shared desk forever after the work shipped.
    if task and task.get('pairWith'):
        _release_pair_navigator(state, task.get('pairWith'), task.get('id'))
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True
    if not completion_refused:
        a['approvedCount'] = (a.get('approvedCount') or 0) + 1
        a['weekApprovals'] = (a.get('weekApprovals') or 0) + 1
    # The story shipped: any per-story capability grant dies with it.
    _revoke_task_access(task.get('id') if task else None)
    if not completion_refused:
        _note_completed_room(state, agent_id, task)
        # Backlog refinement intake: the agent just shipped a real deliverable and
        # saw whether its room still has work. If the room is thinning, file a
        # follow-up work-request so the scrum master can turn the observed gap into
        # a story at the next ceremony.
        _maybe_file_followup(state, agent_id, task, int(time.time() * 1000))
        # Signal wake-ups (ask 7): a completed task is an emission
        # point. The event data describes the landed work; any armed
        # signal plan whose scenario condition appears in it wakes
        # its agent (when that agent is fully idle). Best-effort --
        # emit_signals never raises, so a signal failure can never
        # break task completion.
        if task:
            emit_signals(state, 'task.completed', _task_completed_data(task))
    if task and task.get('entryX') is not None:
        ex, ey = task['entryX'], task['entryY']
        for oid, other in agents.items():
            if oid == agent_id:
                continue
            if other and other.get('visible') and other.get('x') == ex and other.get('y') == ey:
                ex += AGENT_W + 8
                break
        a['x'], a['y'] = ex, ey
    else:
        if grid is not None:
            occupied = [{'x': v['x'], 'y': v['y']} for v in agents.values()
                        if v.get('visible') and v.get('id') != agent_id]
            spot = pick_free_spot(grid, occupied, spawn=SPAWN,
                                  ground_w=GROUND_W, ground_h=GROUND_H)
        else:
            spot = {'x': SPAWN['x'], 'y': SPAWN['y']}
        a['x'], a['y'] = spot['x'], spot['y']
    from serve import log_action
    if completion_refused:
        log_action(agent_id, 'task_completion_refused',
                   {'taskId': task['id'] if task else None,
                    'title': task['title'] if task else None,
                    'taskType': task.get('taskType') if task else None,
                    'evidence': {
                        'libraryPath': bool(task and task.get('libraryPath')),
                        'reportPath': bool(task and task.get('reportPath')),
                        'writtenPy': bool(task and task.get('writtenPy')),
                        'restoreCheck': bool(task and task.get('restoreCheck')),
                        'codePublished': bool(task and task.get('codePublished')),
                        'wikiPage': bool(task and task.get('wikiPage')),
                        'note': bool(task and str(task.get('note') or '').strip()),
                    },
                    'detail': completion_gap,
                    'reason': 'missing_saved_deliverable'}, authorized=True)
        _notify_card_failure(
            state, task or {}, completion_gap or 'the completion contract was not met',
            category='refused')
    else:
        log_action(agent_id, 'task_completed', {'taskId': task['id'] if task else None,
                                                'title': task['title'] if task else None},
                   authorized=True)
        # Card close reports no longer reach the player on any
        # channel -- the sprint retro is the player's close signal.
        # The call keeps its once-per-card idempotency flag.
        _deliver_card_report(state, task, 'completed')


def send_agent_off_duty(state, agent_id, doors, grid):
    """Send an agent off duty. Refuse if busy/task/pair/handoff (safe to call on
    anyone). An agent VANISHES WHERE SHE STANDS -- the outskirts trailhead doors
    are gone, so resting is immediate and in-place. The `doors`/`grid` args are
    kept for call-compat but off-duty needs neither."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    if a.get('busy') or a.get('task') or a.get('pairWith') or a.get('handoff'):
        return
    # Scheduled-item preemption: every task-completion path funnels here to go
    # off duty -- if this agent's own task was suspended so a due scheduled item
    # could fire, reclaim the parked task instead of resting (the scheduled
    # item just finished and handed the agent back).
    if a.get('_suspendedTask'):
        if _resume_suspended_task(state, agent_id, a, grid):
            return
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['headingOffDuty'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['offDuty'] = True
    a['visible'] = False


def _complete_shadow_task(state, agent_id, task, now_ms, grid=None):
    """Bot Ops / shadow mode completion: the dry-run task did its work, so capture
    its outcome to the append-only state['shadowLedger'] draft -- then release the
    agent WITHOUT any real-world side effect. Nothing ships: no peer gate, no
    approvedCount/weekApprovals bump, no completedDeliverables entry, no
    completedRooms/grade/runbook, no follow-up filing, no dependency unblock. The
    work happened; the world didn't move. Returns the ledger entry."""
    task['status'] = 'done'
    task['shadowDoneAt'] = now_ms
    entry = {
        'title': task.get('title'),
        'room': task.get('room'),
        'instructions': task.get('instructions'),
        'projectLabel': task.get('projectLabel'),
        'taskType': task.get('taskType'),
        'note': task.get('note'),
        'libraryPath': task.get('libraryPath'),
        'agentId': agent_id,
        'completedAt': now_ms,
        'promoted': False,
    }
    state.setdefault('shadowLedger', []).append(entry)
    a = (state.get('agents') or {}).get(agent_id)
    if a:
        a['task'] = None
        a['busy'] = False
        a['inRoom'] = None
        a['visible'] = True
    try:
        from serve import log_action
        log_action(agent_id, 'shadow_task_completed',
                   {'taskId': task.get('id'), 'title': task.get('title')}, authorized=True)
    except Exception:
        pass
    return entry


MIN_RESEARCH_CADENCE_MS = 5 * 60 * 1000  # floor: a misparsed "every second" can't spam the queue
MIN_PIPELINE_CADENCE_MS = 60 * 60 * 1000  # floor for an ordered pipeline: a misparsed "every second" can't spam the queue

# Bell-style long-horizon problem (2026-10-06): one standing research topic per
# village whose crawl cadence is far in the future -- the think tank always has
# something on the table that is NOT due this sprint, so the org keeps a horizon
# beyond the 6s tick and the weekly ceremonies. Configurable via .env
# (LONG_HORIZON_TOPIC / LONG_HORIZON_TOPIC_URL / LONG_HORIZON_CADENCE_DAYS);
# the defaults are a safe self-questioning frame.
LONG_HORIZON_TOPIC_DEFAULT = 'Re-examine our own assumptions: what does the think tank believe, and what would falsify it'
LONG_HORIZON_TOPIC_URL_DEFAULT = 'https://en.wikipedia.org/wiki/Falsifiability'
