

def _dispatch_content_work(executor, state, agent_id, task, now):
    """Fire the registered content executor on a background thread for this
    arriving agent's task, if one applies. Returns True if real content work was
    dispatched (so the task gets the long content timeout instead of the workUntil
    placeholder), else False (slice-1 short-circuit). The executor runs against a
    snapshot copy of `state` so its long network calls never race our sync pass;
    results are merged via _content_results by the next _task_cycle.

    Room-agnostic: `executor` is a router (serve.py provides it) who decides by
    task.room/task flags which real work applies and stores the result itself.
    The generic long budget is installed HERE; an executor that needs a
    different ceiling shortens task['workUntil'] in its snapshot. A timeout here
    is a hard floor, not special-cased per room."""
    task_id = task.get('id')
    if not task_id or not executor:
        return False
    # Give the content run a generous ceiling; mark the task so the completion
    # loop knows real work is in flight (not just the placeholder). The task's
    # OWN budgetMs (when larger) wins up to the cap -- a player-authored spike
    # asking for a 5-minute work box must not be silently flattened to 120s and
    # cut mid-synthesis (see TASK_CONTENT_TIMEOUT_MAX_S).
    budget_s = (float(task.get('budgetMs') or 0)) / 1000.0
    ceiling = min(max(TASK_CONTENT_TIMEOUT_S, budget_s), TASK_CONTENT_TIMEOUT_MAX_S)
    task['workUntil'] = now + ceiling
    task['_contentInFlight'] = True
    # Fresh run: drop any heartbeat/grace residue a prior occupant of this
    # (reusable) task id may have left, so this run earns grace only from its
    # OWN bumps (see TASK_CONTENT_GRACE_*).
    task['_contentHeartbeat'] = None
    task['_contentGraceUsed'] = 0.0
    snapshot = json.loads(json.dumps(state))
    # Item 1: situational awareness. Build the perception block here, on the
    # main thread, from the FROZEN snapshot + the live task dict -- never inside
    # the worker thread, where a read of the still-mutating live `state` would
    # race the sync pass. Falsy (an empty perception) keeps the executor's
    # pre-existing prompt exactly.
    perception = _build_agent_perception(snapshot, agent_id, task)

    def _run():
        try:
            executor(snapshot, agent_id, task, base_ctx=perception)
        except Exception as e:  # never let a content failure strand the agent
            if getattr(e, 'credits_paused', False):
                # The work did not fail; the account is out of credits. Keep the
                # card intact and requeue it, and hold the task cycle until the
                # balance is restored.
                _store_content_result(task_id, {
                    'creditsPaused': True,
                    'note': 'OpenRouter credits are exhausted; the task is paused and will resume automatically.',
                    'seenUrls': (task.get('research') or {}).get('seenUrls') or [],
                })
                return
            # W4: a crashed content run is a FAILURE, not a silent success.
            # Mark ok=False so the fail-closed quality gate treats it like a red
            # pipeline (card sent back / fix re-issued), and the worker-stuck
            # help signal can see it -- a crash must never complete as 'done'.
            _store_content_result(task_id, {'ok': False, 'note': f'Content execution failed: {e}', 'seenUrls': (task.get('research') or {}).get('seenUrls') or []})

    threading.Thread(target=_run, daemon=True).start()
    return True


def room_desk_capacity(collision):
    """Port of tasks.js _roomDeskCapacity: number of interactables for a room's
    collision layout. Only counts feed overflow resolution (assignTask), so the
    exact counts are what matter, not the desk geometry. Mirrors the actual
    ROOM_COLLISIONS array lengths in rooms.js/terminals.js. Unknown layout ->
    no invented limit (Infinity), same as the JS."""
    counts = {
        'workstations': 6,   # ROOM_COLLISIONS.workstations
        'library': 6,        # ROOM_COLLISIONS.library
        'bank': 3,           # ROOM_COLLISIONS.bank
        'postoffice': 1,     # ROOM_COLLISIONS.postoffice
    }
    return counts.get(collision, float('inf'))


# ROOMS collision key per building -- mirrors tasks.js reach into rooms.js.
_ROOM_COLLISION = {
    'pressoffice': 'workstations', 'media': 'workstations',
    'weatherstation': 'workstations', 'observatory': 'workstations',
    'library': 'library', 'bank': 'bank', 'postoffice': 'postoffice',
}
# The single overflow pair -- tasks.js ROOM_OVERFLOW_TARGET: Work Room overflows
# into Research Center when the Work Room's six desks are full.
_ROOM_OVERFLOW_TARGET = {'pressoffice': 'observatory'}


def room_occupancy(state, room):
    """Port of tasks.js _roomOccupancy: how many busy agents are in this room."""
    n = 0
    for a in (state.get('agents') or {}).values():
        if a and a.get('busy') and a.get('inRoom') == room:
            n += 1
    return n


def resolve_room_with_overflow(state, room):
    """Port of tasks.js _resolveRoomWithOverflow: redirect to the overflow target
    only when THIS room is full AND the target has a free desk; otherwise stays.
    Idempotent."""
    overflow_to = _ROOM_OVERFLOW_TARGET.get(room)
    if not overflow_to:
        return room
    if room_occupancy(state, room) < room_desk_capacity(_ROOM_COLLISION.get(room)):
        return room
    if room_occupancy(state, overflow_to) < room_desk_capacity(_ROOM_COLLISION.get(overflow_to, 'workstations')):
        return overflow_to
    return room
def queue_work(state, items):
    """Port of tasks.js queueWork: whitelist each item's fields into a WORK_QUEUE
    entry. Mutates state['workQueue'] (creates it if absent). Returns new length."""
    work_queue = state.setdefault('workQueue', [])
    if not isinstance(work_queue, list):
        work_queue = state['workQueue'] = []
    for item in items:
        # A room-less card is legitimate: a shared-backlog card is room-free on
        # purpose, and the ASSIGNED agent resolves the room at assignment
        # (_assign_due_item). Every other caller supplies a room.
        if not item or not item.get('title'):
            continue
        work_queue.append({
            'title': item['title'],
            'room': item.get('room') or None,
            'instructions': item.get('instructions') or f"Pick whoever is best suited for: {item['title']}",
            # A player-filed JIRA card's contract: the normalized user story +
            # acceptance criteria survive the queue round trip onto the real task
            # (see _resolve_refinement -> _assign_due_item -> assign_task), so the
            # coding executor's backlog line carries the full spec, not just the
            # one-line summary. Same class of gap as 'checklist'/'distill' below:
            # a whitelist that silently drops a field its consumer needs.
            'userStory': item.get('userStory') or None,
            'acceptanceCriteria': item.get('acceptanceCriteria') or None,
            # A card's NEVER constraints ride the queue round trip onto the real
            # task so the worker's instructions always include them (same class
            # of gap as 'userStory'/'distill': a whitelist that silently drops
            # a field its consumer needs).
            'never': item.get('never') or None,
            'pair': bool(item.get('pair')),
            'notBefore': item.get('notBefore') or None,
            'priority': normalize_priority(item.get('priority')),
            # A breakdown story's size estimate (S/M/L) survives the queue round
            # trip onto the real task and orders same-priority work (an L story
            # starts before an S story -- see pick_next_due_index). Same class
            # of gap as 'userStory'/'distill' below: a whitelist that silently
            # dropped a field its consumer needs.
            'sizeEstimate': normalize_size_estimate(item.get('sizeEstimate')),
            'goal': item.get('goal') or None,
            'projectLabel': item.get('projectLabel') or None,
            'research': item.get('research') or None,
            'taskType': item.get('taskType') or 'code',
            'approvedTools': list(item.get('approvedTools') or []),
            'skillReview': bool(item.get('skillReview')),
            # Hive mind: a distillation content-task that merges recent archive
            # findings into the think tank wiki (see _check_schedules's distill sweep).
            'distill': bool(item.get('distill')),
            'distillSince': item.get('distillSince') or 0,
            # Phase E2b: a SPIKE's work-cycle time budget (ms). A spike has no
            # deliverable -- it completes when the budget elapses, landing a
            # findings artifact instead of a peer-reviewed story.
            'budgetMs': item.get('budgetMs') or None,
            # Deterministic report lane: the executor reads these local sources
            # itself instead of asking the model to choose a Library tool.
            'sourcePaths': item.get('sourcePaths') or None,
            'reportPath': item.get('reportPath') or None,
            # Phase E addendum: a peer-rejected fix returns to the original author.
            'assignedTo': item.get('assignedTo') or None,
            # An explicit lead/player pin: the named worker is the owner, so
            # _assign_due_item wakes them and waits rather than round-robins.
            'directedTo': bool(item.get('directedTo')),
            # Human-in-the-loop: a JIRA issue filed FOR the player (assignedTo
            # 'player') travels with its issueKey so the player task records its
            # provenance -- same class of whitelist as 'distill'/'checklist'.
            'issueKey': item.get('issueKey') or None,
            'requestId': item.get('requestId') or None,
            # Phase E2d: an INCIDENT (bug) is pinned to the owning team's on-call
            # and pin-woken even though it carries no reviewOf.
            'incident': bool(item.get('incident')),
            # Theo routing: a spike the classifier could attribute
            # to a specific team is pinned to that team's on-call worker, same
            # pin-and-wake treatment as an incident -- see _assign_due_item.
            'directRoute': bool(item.get('directRoute')),
            # Gap: filing a story against a specific
            # team never meant it would be WORKED by that team -- assignment
            # (_assign_due_item) is think tank-wide round robin with zero team
            # awareness, only reviewer selection (_pick_reviewer_ids) ever
            # preferred same-team. teamId is the director id owning this item
            # (the queue-item-level analog of a backlogRequest's teamId), used
            # by _assign_due_item to PREFER that team the same soft way
            # _pick_reviewer_ids already does -- never a hard lock like an
            # incident's on-call pin, since a small think tank can't afford to
            # starve one team's queue while another sits idle.
            'teamId': item.get('teamId') or None,
            'sprintId': item.get('sprintId') or None,
            # Adversarial (winter) village: tags every card a side's director
            # files for the shared adversarial task, so completion detection
            # (serve._adversarial_village_tick) can tell when a side has
            # delivered. Same whitelist contract as 'distill'/'checklist'.
            'adversarialTaskId': item.get('adversarialTaskId') or None,
            'villageId': item.get('villageId') or None,
            # WS-14: a story pulled from the shared backlog keeps its provenance
            # -- the FEATURE it belongs to and the SHARED-BACKLOG item id it was
            # claimed from -- so the sprint digest and backlog board can read
            # completion back off the durable task mirror (same whitelist
            # contract as 'distill'/'checklist': silently dropping a field its
            # consumer needs breaks the pull loop).
            'featureId': item.get('featureId') or None,
            'backlogItemId': item.get('backlogItemId') or None,
            # Phase E: a pressoffice task may target a product (its build
            # releases the artifact) and carry optional pre-injected wiki pages.
            'productId': item.get('productId') or None,
            'wikiPageIds': list(item.get('wikiPageIds') or []),
            # Phase E addendum: a task may be a peer-approval review subtask (a
            # vote on a parent story) or a fix subtask (re-opening one). The
            # parent task id travels in `reviewOf`.
            'reviewOf': item.get('reviewOf') or None,
            # Pi Durable-style fork provenance: a delegated card is an
            # independent unit with its own id and checkpoint, linked to the
            # supervisor card that created it.
            'parentTaskId': item.get('parentTaskId') or None,
            'bestEffort': bool(item.get('bestEffort')),
            # Phase E3: the story's original author id (see reviewAuthorId on the
            # review-subtask creation) -- lets an actionable review pin the fix
            # back to the worker who built it.
            'reviewAuthorId': item.get('reviewAuthorId') or None,
            # CS329A takeaway #2: the review checklist -- code/jev/
            # human-typed requirement entries (see content.py's ensemble grading) --
            # must survive the queue round trip or the Python review path can't
            # grade per-requirement. Same class of gap as 'distill' below: a
            # whitelist that silently drops a field its consumer needs.
            'checklist': list(item.get('checklist') or []),
            # Ordered-pipeline marker (see _check_pipelines / add_pipeline): which
            # pipeline + step index this work item belongs to, so the strict-order
            # sweep can read completion back off the durable task mirror. Must
            # survive the round trip or _pipeline_step_task can never match the
            # step and the pipeline wedges on step 0 forever -- the exact same
            # class of whitelist gap 'distill'/'checklist' both hit.
            'pipelineStep': item.get('pipelineStep') or None,
            # Phase E3.6: a task's knowledge-base class. `changes_how_we_work`
            # marks the standing ceremonies whose WHOLE job is maintaining the
            # shared knowledge base (research crawl -> skill file, skill-review
            # curation, distillation -> wiki). Only THAT class carries a KB-write
            # mandate; ordinary deliverables are required to land a completion
            # NOTE (evidence of what was done) but never a KB write.
            'kbClass': item.get('kbClass') or None,
            # Standing scheduler (see _standing_task_sweep): the
            # standingTaskId this card was queued from, so a
            # standing task's completion can be read back off the
            # durable task mirror. Same whitelist contract as
            # 'distill'/'checklist': a field the consumer needs
            # must survive the queue round trip.
            'standingTaskId': item.get('standingTaskId') or None,
            # Dependency cascade (ripple re-review): the task A this card is
            # blocked on (issue['dependsOnTask'], filed via
            # request_block_dependency). When A is re-opened (player veto) the
            # 'done' dependents carry this onto their task so the cascade can
            # re-review them too.
            'dependsOn': item.get('dependsOn') or None,
            # Bot Ops / shadow mode: a SHADOW work item does the work but changes
            # nothing -- on completion its outcome is captured to the append-only
            # state['shadowLedger'] draft instead of shipping (no peer gate, no
            # approvals/credits/completedDeliverables). Survives the queue round
            # trip so the real task knows it's a dry run (same whitelist contract
            # as 'distill'/'checklist').
            'shadow': bool(item.get('shadow')),
            # Item 4: the per-task spend ceiling (USD) + the refinement band
            # ('standard'/'generous') set during grooming survive the queue round
            # trip so the ASSIGNED task carries them (see assign_task). Same
            # whitelist contract as 'distill'/'checklist': silently dropping them
            # would leave the task without the budget the groomer judged it worth.
            'budgetUsd': item.get('budgetUsd') or None,
            'budgetBand': item.get('budgetBand') or None,
            # Bell-style spare-time lane: a MOONSHOT item is a protected
            # exploration lane -- never enters the peer gate (spikes are already
            # non-gated) and its failures are NOT mined into operator rules
            # (see _record_classified_failures). Explicit flag so the protection
            # is auditable, not implicit.
            'moonshot': bool(item.get('moonshot')),
            # Attention lane (build/reading/open/parking-lot): threads coexist
            # instead of one queue pulling. parking-lot cards are never auto-
            # assigned (see _is_parked / pick_next_due_index). Same whitelist
            # contract as 'distill'/'checklist': a lane silently dropped here
            # would let a parked card leak into assignment.
            'lane': normalize_lane(item.get('lane')),
            # Player-authored provenance: True only when the PLAYER wrote the
            # work text (player chat lane / player-filed JIRA issue), never an
            # agent. Rides the queue -> assignment -> task round trip (see
            # _assign_due_item / assign_task) so the JEV gate can tell a task
            # the player vetted from one an agent wrote (an agent-authored
            # story must not be able to manufacture its own work-context
            # bypass by naming a URL in its own text).
            'playerAuthored': bool(item.get('playerAuthored')),
            # A MEETING card's attendee list (concrete roster ids, resolved
            # at grooming -- see _resolve_meeting_attendees) and its
            # facilitator (the agent who convenes it and writes the minutes).
            # Both survive the queue round trip onto the real task (same
            # whitelist contract as 'distill'/'checklist': the meeting
            # executor reads task['attendees']/task['facilitatorId'] to
            # name the participants in the minutes it writes).
            'attendees': list(item.get('attendees') or []),
            'facilitatorId': item.get('facilitatorId') or None,
        })
    return len(work_queue)


def queue_spike(state, title, room, budget_ms, now_ms=None, goal=None, instructions=None,
                project_label=None, moonshot=False, notBefore=None, assigned_to=None,
                lane=None, player_authored=False, budget_usd=None):
    """Phase E2b: enqueue a time-boxed SPIKE (taskType='spike'). A spike is an
    investigation with no committed deliverable: it's LOWEST priority (fills
    gaps, never blocks committed work), carries a hard `budgetMs` for the work
    cycle, and -- because nothing ships -- its completion does NOT open a peer
    gate (see _peer_gated_lane). Produces a findings artifact, not a release.
    `moonshot` marks a protected spare-time spike (see FREE_SPIKE_*): its
    failures are never mined into operator rules. `notBefore` (epoch ms) gates
    when the spike becomes assignable (spare-time work is deferred). Returns
    the new queue length, or None if the item was rejected (no room or title).
    Human-in-the-loop: `assigned_to='player'` hands the spike to the PLAYER
    instead of an agent -- the player completes it via the player task
    complete endpoint, and anything queued `depends_on_task` on it resumes the
    moment it ships (see _assign_player_task).
    `lane` (build/reading/open/parking-lot) rides the card so it can coexist
    with other threads instead of one queue pulling all the attention (see
    LANES).
    `budget_usd` is the model-spend ceiling (USD). Explicit positive wins over
    the spike type-default ($0.30 in _TASK_BUDGET_USD). Confirmed gap
    2026-10-13: a six-source comparison synthesis genuinely spends ~$0.40 in
    plan+execute+synthesize, so the default $0.30 ceiling made /api/chat
    refuse the final synthesis and the card closed empty with no report
    (task-333/335). A heavier investigation files an explicit ceiling here."""
    return queue_work(state, [{
        'title': title,
        'room': room,
        'instructions': instructions or (f'Spike: investigate and report on: {title}. '
                                          f'Explore the approach, run a short experiment, and '
                                          f'write up concrete findings. Time-boxed to ~{int((budget_ms or 60_000) / 1000)}s.'),
        'goal': goal,
        'projectLabel': project_label,
        'priority': WORK_PRIORITY['low'],
        'taskType': 'spike',
        'budgetMs': budget_ms or 60_000,
        'budgetUsd': budget_usd,
        'moonshot': bool(moonshot),
        'notBefore': notBefore,
        'assignedTo': assigned_to or None,
        'lane': lane,
        # Player-authored provenance: True only when the PLAYER filed this spike
        # (the player chat lane) -- see queue_work's whitelist comment.
        'playerAuthored': bool(player_authored),
    }])


def queue_meeting(state, title, room, facilitator_id, attendees=None,
                  now_ms=None, goal=None, instructions=None,
                  project_label=None, notBefore=None, assigned_to=None,
                  lane=None, player_authored=False, budget_usd=None,
                  budget_ms=900_000):
    """Queue a deliberate MEETING card (taskType='meeting'): a
    conversation with a named facilitator and a resolved attendee
    list, held in a real room (pressoffice by default -- it has a
    door, so the facilitator can actually walk there). A meeting is a
    NON-GATED lane: nothing ships, so no peer gate opens; the
    facilitator's agent convenes the attendees, discusses the topic,
    and writes the minutes into the card's note (the completion
    artifact -- see _run_meeting_content). `attendees` is either a
    list of roster ids (kept as-is, invalid ids dropped at grooming)
    or a scope string ('team' = the owning team's people, 'all' =
    the whole think tank, 'pair' = facilitator + one teammate) --
    resolved to concrete ids by _resolve_meeting_attendees during
    backlog refinement, BEFORE the sprint starts. `facilitator_id`
    is the agent who convenes it; _assign_due_item PINS the card to
    them (waking them off-duty if needed) the same way an incident
    pins to the on-call. `budget_ms` defaults to a 15-minute box --
    the work cycle only needs to cover one minutes-writing call."""
    return queue_work(state, [{
        'title': title,
        'room': room,
        'instructions': instructions or (f'Meeting: {title}. '
                                         f'Convene the listed attendees, '
                                         f'discuss the topic, and write up '
                                         f'the minutes (topic, attendees, '
                                         f'discussion, decisions, action '
                                         f'items with owners) into the '
                                         f'card note.'),
        'goal': goal,
        'projectLabel': project_label,
        'priority': WORK_PRIORITY['low'],
        'taskType': 'meeting',
        'budgetMs': budget_ms,
        'budgetUsd': budget_usd,
        'attendees': list(attendees or []),
        'facilitatorId': facilitator_id or None,
        'notBefore': notBefore,
        'assignedTo': assigned_to or None,
        'lane': lane,
        # Player-authored provenance: True only when the PLAYER filed this
        # meeting (the player chat lane) -- see queue_work's whitelist
        # comment.
        'playerAuthored': bool(player_authored),
    }])


def queue_once(state, title, at_ms, room=None, instructions=None, task_type=None,
               goal=None, priority=None, project_label=None, depends_on_task=None,
               assigned_to=None, lane=None):
    """Queue a single work item to fire ONCE at an absolute wall-clock time
    (`at_ms`, epoch milliseconds). The item rides the same `notBefore` gate
    the rest of the queue already honors (see is_work_item_due): it sits in
    the work queue untouched until `at_ms`, then runs through the normal
    assign -> work -> complete lifecycle exactly once. `depends_on_task` (a
    task id) adds the dependency gate: even once `at_ms` passes, the item is
    NOT assigned until that task reaches 'done' (see _work_item_dependency_met)
    -- the composed "run X once the dependency lands" scheduling lane. Returns
    the queued work item dict, or None if rejected (no title, or `at_ms` is
    not a positive future epoch-ms).
    Human-in-the-loop: `assigned_to='player'` hands the item to the PLAYER
    instead of an agent -- combined with `depends_on_task` this is the
    "give the player X once story T lands" lane (see _assign_player_task).
    `lane` (build/reading/open/parking-lot) rides the card so threads coexist
    (see LANES); `parking-lot` defers it with a bookmark -- never auto-assigned."""
    at_ms = int(at_ms or 0)
    if not title or at_ms <= 0:
        return None
    item = {
        'title': title,
        'room': room or None,
        'instructions': instructions or f'Handle this one-off request once: {title}',
        'notBefore': at_ms,
        'taskType': task_type or None,
        'goal': goal or None,
        'priority': normalize_priority(priority),
        'projectLabel': project_label or None,
        'dependsOn': depends_on_task or None,
        'assignedTo': assigned_to or None,
        'lane': lane,
    }
    queue_work(state, [item])
    return item


def think_tank_has_work(state, now_ms):
    """Port of tasks.js thinkTankHasWork: a due queue item, or any agent currently
    task/handoff/pair/busy. A due item whose `dependsOn` dependency hasn't
    landed is NOT work yet -- it waits (no agent spend) until the dependency
    ships, same gate pick_next_due_index applies at assignment.
    Human-in-the-loop: a due PLAYER card (assignedTo 'player') counts as work
    too -- the idle gate must not short-circuit a pass that is supposed to
    deliver a card into the player's hands (see _assign_player_task)."""
    work_queue = state.get('workQueue') or []
    if any(is_work_item_due(item, now_ms) and _work_item_dependency_met(state, item)
           and not _is_parked(item)
           for item in work_queue):
        return True
    for a in (state.get('agents') or {}).values():
        if a and (a.get('task') or a.get('handoff') or a.get('pairWith') or a.get('busy')):
            return True
    return False


def _team_oncall_members(state, director_id):
    """The pool an owning team can draw an on-call from: the director's direct
    reports, minus the standing scrum master (who facilitates, not works -- the
    same exclusion `_team_member_count` applies). Admins are also excluded so the
    on-call is a real worker. Returns a list of agent ids in stable roster order."""
    scm = None
    for t in (state.get('teams') or []):
        if t.get('directorId') == director_id:
            scm = t.get('scrumMasterId')
            break
    members = _sim_direct_reports(state, director_id)
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    pool = []
    for m in members:
        if m == scm:
            continue
        if (roster.get(m) or {}).get('isAdmin'):
            continue
        pool.append(m)
    return pool


def _oncall_order(state, director_id):
    """The persistent on-call rotation ORDER for director_id's team. Seeded from
    the derived roster order on first use; a backup who actually served a sprint
    is rotated to the END of this order at sprint close (_rotate_served_oncalls)
    so she does not get paged again next sprint before the rotation catches up.
    New hires are appended at the end (they start as backups); fired agents are
    dropped. Mutates state['_oncallOrder'] only on seed/reconcile."""
    orders = state.setdefault('_oncallOrder', {})
    order = orders.get(director_id)
    pool = _team_oncall_members(state, director_id)
    if not order:
        orders[director_id] = list(pool)
        return orders[director_id]
    alive = [a for a in order if a in (state.get('agents') or {})]
    for a in pool:
        if a not in alive:
            alive.append(a)
    orders[director_id] = alive
    return alive


def _rotate_served_oncalls(state, team_ids):
    """At sprint close: any on-call BACKUP who actually served this sprint's
    pages is rotated to the END of her team's on-call queue, so a backup who
    covered does not get paged again next sprint before the rotation catches up.
    Mutates state['_oncallOrder'] and clears state['_oncallServed'].
    A team is matched by its `id` or its director's id (the two keyings teams
    records use in the codebase); a sprint record's teamIds may carry either."""
    teams = {t.get('id'): t for t in (state.get('teams') or [])}
    served = state.get('_oncallServed') or {}
    orders = state.setdefault('_oncallOrder', {})
    for tid in team_ids or []:
        t = teams.get(tid)
        if t is None:
            t = next((x for x in (state.get('teams') or [])
                      if x.get('directorId') == tid), None)
        if not t:
            continue
        director_id = t.get('directorId') or t.get('id')
        if not director_id:
            continue
        for aid in served.get(director_id) or []:
            order = orders.get(director_id)
            if order and aid in order:
                order.remove(aid)
                order.append(aid)
        served[director_id] = []


def on_call_agent(state, director_id, sprint_id=None, now_ms=None):
    """Phase E2d: which agent on `director_id`'s team is on-call right now.
    Deterministic per-sprint rotation over the team's non-scrum-master, non-admin
    members -- derived from the persistent on-call ORDER (_oncall_order, seeded
    from roster order), so the same sprint id always hands back the same on-call
    across restarts and the order shifts across sprints. A backup who actually
    served a sprint is rotated to the end of the order at sprint close
    (_rotate_served_oncalls) rather than paged again next sprint.

    Within a rotation the pager prefers an AVAILABLE member (on-duty and not
    mid-work): if the primary is busy, the BACKUP is the next slot in rotation
    order and the second backup the slot after that -- so an on-call who is
    already working hands the pager to her backup instead of double-booking
    her. A question/incident NOT tied to a sprint has no natural advance point
    (the sprint counter is static), so the slot also shifts with the UTC day --
    the pager actually moves even on a sprint-less team.

    Deadfalls back to the primary slot when the whole team is unavailable, so
    the pin still names a concrete owner (assignment re-derives if it can and
    otherwise falls back to the team's best free worker).
    Returns an agent id or None if the team has no rousable members."""
    pool = _oncall_order(state, director_id)
    if not pool:
        return None
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    seed = sprint_id or 'default'
    # stable hash across restarts; fold in a running sprint counter so rotations
    # actually advance even if sprint ids were reused.
    counter = len([s for s in (state.get('sprints') or {}).values()
                   if s.get('teamIds') and director_id in s.get('teamIds')])
    h = 0
    for ch in str(seed):
        h = (h * 31 + ord(ch)) & 0xffffffff
    idx = (h + counter) % len(pool)
    if not sprint_id:
        # No sprint anchor (player question, unscheduled incident) -> shift the
        # slot by the UTC day so the pager moves even when a team's sprint list
        # is static. Still derived: a pure function of state + now_ms.
        idx = (idx + now_ms // 86400000) % len(pool)
    agents = state.get('agents') or {}
    def _available(aid):
        a = agents.get(aid)
        return bool(a) and not a.get('offDuty') and not a.get('busy') \
            and not a.get('task') and not a.get('pairWith')
    # Prefer an available member in ROTATION order: the primary slot, then its
    # backup (next slot), then the second backup (the one after that), ...
    chosen = None
    for i in range(len(pool)):
        cand = pool[(idx + i) % len(pool)]
        if _available(cand):
            chosen = cand
            break
    if chosen is None:
        chosen = pool[idx]
    # Track a BACKUP who actually served a sprint-anchored page, so the sprint-
    # close ceremony can rotate her to the end of the queue.
    if sprint_id and chosen != pool[idx]:
        served = state.setdefault('_oncallServed', {}).setdefault(director_id, [])
        if chosen not in served:
            served.append(chosen)
    return chosen


def queue_bug(state, product_id, title, now_ms=None, room=None, reported_by=None,
              sprint_id=None, instructions=None):
    """Phase E2d: file a BREAKAGE report (taskType='bug') for a product and route
    it to the owning team's current on-call agent as a pinned, high-priority,
    NON-GATED incident. product_id resolves to its owning team (product.teamId ->
    director_id) -> on_call_agent(...). Returns the new queue length, or None if
    the incident can't be routed (no owning team, no on-call, or the team already
    has an open bug -- the one-in-flight cap)."""
    products = state.get('products') or {}
    prod = products.get(product_id)
    if not prod:
        return None
    director_id = prod.get('teamId')
    if not director_id:
        return None
    oc = on_call_agent(state, director_id, sprint_id, now_ms)
    if not oc:
        return None
    # One-in-flight cap per owning team + product: a gutted on-call must not
    # accumulate incidents while a downstream team floods.
    for item in (state.get('workQueue') or []):
        if item.get('taskType') == 'bug' and item.get('productId') == product_id:
            return None
    for task in (state.get('tasks') or {}).values():
        if task.get('taskType') == 'bug' and task.get('productId') == product_id:
            return None
    # A broken product is software: the fix lands in Press Office (the only
    # room whose executor actually writes code), so the bug produces a real fix
    # artifact rather than a chore. Products carry no room field today.
    room = room or 'pressoffice'
    instructions = instructions or (f'INCIDENT: {title}. A downstream team reports '
                                    f'this product is broken. You are on-call. Fix it, '
                                    f'land the fix artifact, and close it. No peer review '
                                    f'needed -- this is incident response, not feature work.')
    return queue_work(state, [{
        'title': title,
        'room': room,
        'instructions': instructions,
        'goal': None,
        'projectLabel': None,
        'priority': WORK_PRIORITY['high'],
        'taskType': 'bug',
        'productId': product_id,
        'assignedTo': oc,
        'incident': True,
        'sprintId': sprint_id,
    }])


def _dependency_landed(state, task_id):
    """True when the dependency task `task_id` has reached 'done' in the durable
    task mirror. A dependency only 'lands' when a real done task exists under
    that id (old done tasks persist in state, so this stays true after
    completion). Absent / walking / working / needs_review -> not landed."""
    t = (state.get('tasks') or {}).get(task_id)
    return isinstance(t, dict) and t.get('status') == 'done'


def _work_item_dependency_met(state, item):
    """True when a queue item's dependency (item['dependsOn']) has landed --
    i.e. the referenced task reached 'done' in the durable mirror. An item
    with no dependency is always met. pick_next_due_index / think_tank_has_work
    use this to keep a gated card IN the queue (never assigned, never abandoned
    on the attempts cap) until its dependency actually ships."""
    dep = item.get('dependsOn')
    if not dep:
        return True
    return _dependency_landed(state, dep)


def _band_for_work_item(item):
    """Map a work item to the model band it would most likely use at call
    time. Code/review/qa tasks route to the coding band (deterministic in
    _resolve_model_tier); spikes and everything else land on low/mid/high
    via the Jev decision -- mid is the proxy since most general work ends
    there. Used only by the tier throttle; a wrong guess just means the
    item is held or admitted one band off, never lost."""
    tt = item.get('taskType') or 'code'
    if tt in ('code', 'review', 'qa'):
        return 'coding'
    return 'mid'


def _tier_value_boost(item, tier_ratios):
    """1 when the item's likely band is in a cheap-value window (today's
    score-per-dollar is well ABOVE the band's trailing median), else 0. Used
    only as an ordering tiebreak between the lane weight and the size estimate
    -- see pick_next_due_index. Never reorders across urgency (priority stays
    the first key) and never reorders across lanes. Neutral (0) when history
    is thin or no ratios are supplied."""
    if not tier_ratios:
        return 0
    ratio = tier_ratios.get(_band_for_work_item(item), 1.0)
    return 1 if ratio >= GOOD_VALUE_RATIO else 0


def _tier_throttled(item, tier_ratios):
    """True when the item's likely band is in a contracted state (today's
    score-per-dollar is well below the band's trailing median) and the
    item is not exempt. Exemptions always admit regardless of tier
    economics: high/urgent priority, incidents, peer-review subtasks,
    sprint cards, scheduled one-offs (notBefore), and player cards."""
    if not tier_ratios:
        return False
    if (item.get('priority') or WORK_PRIORITY['normal']) >= WORK_PRIORITY['high']:
        return False
    if item.get('incident') or item.get('reviewOf') or item.get('sprintId'):
        return False
    if item.get('notBefore') is not None:
        return False
    if (item.get('assignedTo') or '') == 'player':
        return False
    # Player/JIRA work and an explicit lead pin are commitments, not
    # price-elastic backlog. Never hold them on a bad tier-value day.
    if item.get('issueKey') or item.get('directedTo'):
        return False
    ratio = tier_ratios.get(_band_for_work_item(item), 1.0)
    return ratio < TIER_THROTTLE_RATIO


def pick_next_due_index(work_queue, now_ms, exclude_items, state=None, tier_ratios=None):
    """Port of tasks.js _pickNextDueIndex: highest due priority wins; at EQUAL
    priority, the larger size estimate (L > M > S) starts first -- an L story
    needs more wall-time than an S story, so it is pulled ahead of same-priority
    siblings (effort is a tiebreak, never an urgency override). strict >
    preserves arrival order at equal priority + equal size; skip items in
    exclude_items (this call's attemptedThisCycle). When `state` is supplied
    (the live assignment loop), an item whose `dependsOn` dependency hasn't
    landed is skipped too -- a gated card waits for its dependency, never
    leaks into assignment. Returns index or -1.

    Attention lanes (see LANES): a `parking-lot` card is never auto-picked --
    it waits with a bookmark, so no booking pulls it in. The lane weight
    (build 3 / open 2 / reading 1 / none 0) breaks priority ties, never
    overrides urgency: urgent always beats normal regardless of lane. With
    `state`, a `reading` card is rate-limited to ONE active per room -- the
    slow lane moves one thread at a time, not a flood.

    Price-aware throttle: when `tier_ratios` is supplied (the live
    assignment loop computes it once per cycle), non-urgent items whose
    likely band is in a bad-value day are skipped -- see _tier_throttled --
    and items whose band is in a CHEAP-value window get a small ordering
    boost between the lane weight and the size estimate (see
    _tier_value_boost), so a good-value band's backlog is taken on more
    readily while the window is open. Callers that pass None (tests, one-off
    checks) get unthrottled, unboosted behavior, same as before."""
    best_index, best_score = -1, None
    for i, item in enumerate(work_queue):
        if not is_work_item_due(item, now_ms):
            continue
        # exclude_items is a Set of QUEUE-ITEM IDENTITIES (id(item)), not the
        # dicts themselves -- queue items are mutable dicts and unhashable.
        if exclude_items and id(item) in exclude_items:
            continue
        # Parking-lot: the bookmark lane. Skip on EVERY call (with or without
        # state) -- an auto-pick must never leak a parked card into assignment.
        if _is_parked(item):
            continue
        if state is not None and not _work_item_dependency_met(state, item):
            continue
        if state is not None and _reading_lane_busy(state, item):
            continue
        if tier_ratios and _tier_throttled(item, tier_ratios):
            continue
        priority = item.get('priority', WORK_PRIORITY['normal'])
        lane_weight = LANE_WEIGHT.get(item.get('lane') or '', 0)
        score = (priority, lane_weight, _tier_value_boost(item, tier_ratios),
                 size_estimate_weight(item.get('sizeEstimate')))
        if best_score is None or score > best_score:
            best_score, best_index = score, i
    return best_index


def _reading_lane_busy(state, item):
    """The reading lane is rate-limited to one ACTIVE card per room -- a slow
    lane moves one chapter/paper thread at a time (see LANES). True when
    another reading-lane task is currently walking/working in the item's room,
    so a fresh reading card waits its turn instead of stacking on top."""
    if (item.get('lane') or '') != 'reading':
        return False
    room = item.get('room')
    for t in (state.get('tasks') or {}).values():
        if not isinstance(t, dict):
            continue
        if t.get('lane') != 'reading':
            continue
        if t.get('status') not in ('walking', 'working'):
            continue
        if room and t.get('room') != room:
            continue
        if t.get('id') == item.get('id'):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Clarify router (player -> admin -> on-call -> KB-first -> completing agent).
# The player asks the tank about how completed work was done; we route to the
# owning team's current ON-CALL agent (whether they worked on it or not), who
# answers KNOWLEDGE-BASE-FIRST -- search the library for the product + question
# before ever reaching the completing agent. If the on-call genuinely cannot
# answer from the KB, the plan escalates to the completing agent. Pure/derived
# (no serve import, no model call): the caller performs the actual KB search +
# LLM call. Mirrors on_call_agent's derived-not-stored precedent.
# ---------------------------------------------------------------------------
def clarify_router_plan(state, product_id, question, sprint_id=None):
    """Resolve who should answer the player's clarifying question about a
    product's completed work. Returns a plan dict or None if nothing routable:

        {'onCall': agent_id,            # answers first (KB-first)
         'completing': agent_id|None,   # fallback if on-call can't answer
         'product': {...}               # the product record (may be {})
         'onCallFallback': bool         # True when the director answered in
                                        # the on-call's place (empty pool)
    }
    `completing` is the most recent agent who landed a deliverable matching the
    product name/alias -- derived from completedDeliverables. None on-call => no
    way to answer (nobody rousable on the owning team)."""
    products = state.get('products') or {}
    prod = products.get(product_id) or {}
    director_id = prod.get('teamId')
    oc = None
    fallback = False
    if director_id:
        oc = on_call_agent(state, director_id, sprint_id)
    if not oc and director_id:
        # Last resort: nobody on the owning team is rousable for the standing
        # rotation (e.g. a team of just an admin + scrum master). A PLAYER
        # question is worth the team's own director answering even though
        # admins never join the rotation -- admins don't do the work, but an
        # unanswered question is worse than an admin answer. Incident routing
        # keeps the stricter rule (queue_bug never falls back like this).
        director = (state.get('agents') or {}).get(director_id)
        if director and not director.get('offDuty'):
            oc = director_id
            fallback = True
    completing = _completing_agent_for_product(state, product_id, prod)
    return {
        'onCall': oc,
        'completing': completing,
        'product': prod,
        'onCallFallback': fallback,
    }


def _product_aliases(prod):
    """Terms a clarify question about `prod` should match against in the
    knowledge base + completing-agent history: the product id itself, its name,
    and any name keeps. Lowercased, minimal."""
    terms = set()
    pid = (prod.get('id') or '').strip()
    name = (prod.get('name') or '').strip()
    if pid:
        terms.add(pid.lower())
    if name:
        terms.add(name.lower())
    for keep in (prod.get('nameKeeps') or []):
        k = (keep or '').strip().lower()
        if k:
            terms.add(k)
    return terms


def _text_matches_any(text, terms):
    text = (text or '').lower()
    return any(t and t in text for t in terms)


def _completing_agent_for_product(state, product_id, prod=None):
    """Most recent agent who landed a completed deliverable associated with
    this product -- the completing agent a clarify can fall back to. Matches
    completedDeliverables entries whose title/room mentions the product id,
    name, or an alias; most recently graded (gradedAt) wins. Returns the agent
    id or None (nothing completed for it yet)."""
    prod = prod if prod is not None else (state.get('products') or {}).get(product_id)
    terms = _product_aliases(prod)
    if not terms:
        return None
    best, best_at = None, -1
    for d in (state.get('completedDeliverables') or []):
        title = (d.get('title') or '')
        room = (d.get('room') or '')
        if not _text_matches_any(f'{title} {room}', terms):
            continue
        at = d.get('gradedAt') or 0
        if at >= best_at:
            best_at, best = at, d.get('agentId')
    return best
