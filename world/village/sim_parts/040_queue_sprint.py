

# ---------------------------------------------------------------------------
# Phase C: sprints + task feed. A sprint is a director-authored, time-boxed body
# of work whose items are TAGGED QUEUE ENTRIES -- each item also lands in the
# shared state['workQueue'] carrying its sprintId, so assignment reuses the
# proven _assign_due_item/_task_cycle path untouched. Progress is DERIVED, not
# stored: resolved by matching a sprint item's title+room against live task
# status + queue presence. Pure state mutation (no serve import at call site),
# unit-testable, run through the serve.py wrapper like everything else.
# ---------------------------------------------------------------------------
def queue_sprint(state, sprint_id, name, goal, owner_id, items, valid_rooms,
                 target_date_ms=None, now_ms=None, team_ids=None, worker_count=None):
    """Create a sprint record and append its valid items to the workQueue, each
    tagged with sprintId. `items` are dicts shaped like queue_work items. Only
    items whose room is in `valid_rooms` (the caller supplies the delegatable
    set -- sim.py stays decoupled from serve.py's constant) are kept; a sprint
    with zero retainable items is not created. `team_ids` records which teams
    the sprint touches (the scrum-master gate is enforced by the CALLER before
    calling here; this just persists the association). `worker_count` is the
    director's chosen sprint headcount (1..MAX_TEAM_MEMBERS): the pool of
    people ALLOWED to work this sprint's cards, enforced strictly at assignment
    (_assign_due_item) -- no pool fallback. Absent/legacy -> the team's full
    complement. Returns the sprint record dict, or None if nothing was created.
    Mutates state['sprints'] + state['workQueue']."""
    valid = {r for r in (valid_rooms or [])}
    kept = []
    for it in items or []:
        if not it or not it.get('title') or not it.get('room'):
            continue
        if it['room'] not in valid:
            continue
        kept.append(dict(it))
    if not kept:
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    sprints = state.setdefault('sprints', {})
    # Sprint headcount cap: the strictest useful bound across the teams this
    # sprint spans (max, so one permissive team doesn't squeeze another) --
    # falls back to the creating owner's team, then the operator value.
    _sq_caps = [team_policy(state, t, 'maxTeamMembers') for t in (team_ids or [])]
    _sq_cap = max(_sq_caps) if _sq_caps else team_policy(state, owner_id, 'maxTeamMembers')
    record = {
        'id': sprint_id,
        'name': name or goal or 'Sprint',
        'goal': goal or '',
        'ownerId': owner_id,
        'createdAt': now_ms,
        'targetDate': target_date_ms or None,
        'teamIds': list(dict.fromkeys(team_ids or [])),
        # The director-chosen sprint headcount (1..maxTeamMembers). Strictly
        # staffed: only this many workers may take this sprint's cards, and
        # only from the sprint's teams (see _sprint_worker_pool).
        'workerCount': max(1, min(_sq_cap, int(worker_count or _sq_cap))),
        'status': 'active',
        'items': [_sprint_item_id(it) for it in kept],
    }
    sprints[sprint_id] = record
    for it in kept:
        it['sprintId'] = sprint_id
    queue_work(state, kept)
    # Sprint rollover: carry the prior closed sprint's unfinished cards into the
    # new sprint (re-tag their queue items + fold their ids into this sprint's
    # items) so the un-landed scope explicitly continues.
    _carry_over_sprint_work(state, record, team_ids)
    # The director starts the sprint with the scoped brief, so monitoring does
    # not depend on them noticing the sprint opened.
    for tid in record.get('teamIds') or []:
        _deliver_supervisor_brief(state, tid, int(now_ms))
    return record


def sprint_progress(state, sprint_id):
    """Derive a sprint's live progress: total items, and counts of queued /
    in-progress / done, resolved by matching each stored (title, room) pair
    against the shared workQueue (still waiting) and the durable tasks map
    (walking / working / done). Also returns `landed`: the titles, in sprint-item
    order, of items that are done -- the "what landed" digest a closed sprint
    reads back as (and an active one shows so far). Returns a dict, or None if
    unknown."""
    record = (state.get('sprints') or {}).get(sprint_id)
    if not record:
        return None
    items = record.get('items') or []  # list of (title, room) tuples
    open_tasks = state.get('tasks') or {}
    # Bot Ops / shadow mode: shadow (dry-run) work must not count toward a
    # sprint -- it ships nothing. Exclude shadow queue items from the still-queued
    # set and shadow tasks from the done/in-progress match.
    still_queued = set(_sprint_item_id(it) for it in (state.get('workQueue') or [])
                       if not it.get('shadow'))
    # Bot Ops / shadow mode: a purely-shadow (dry-run) item must not count toward
    # the sprint at all -- not queued, not in-progress, not done. Track which item
    # ids have ONLY a shadow representation so the fallback below can skip them.
    shadow_ids = set(_sprint_item_id(it) for it in (state.get('workQueue') or [])
                     if it.get('shadow'))
    # Index the live tasks once by (title, room). The old inner scan walked
    # every task for every sprint item, which is O(items * tasks) and showed up
    # on large sprints. This keeps the same identity and shadow semantics.
    tasks_by_key = {}
    for t in open_tasks.values():
        ident = (t.get('title') or '', t.get('room') or '')
        if t.get('shadow'):
            shadow_ids.add(ident)
            continue
        tasks_by_key.setdefault(ident, []).append(t)
    done = in_progress = queued = 0
    landed = []
    for pair in items:
        # After a save/load round-trip tuples arrive as JSON lists -- normalize
        # so hashing against the set of (title, room) tuples always works.
        title, room = tuple(pair)
        ident = (title, room)
        matches = tasks_by_key.get(ident, ())
        if ident in still_queued and not any(
                t.get('status') == 'done' for t in matches):
            # Still in the queue, not yet assigned: queued.
            queued += 1
            continue
        matched_done = any(t.get('status') == 'done' for t in matches)
        matched_active = any(t.get('status') in ('walking', 'working')
                             for t in matches)
        if matched_done:
            done += 1
            landed.append(title)
        elif matched_active:
            in_progress += 1
        elif ident in shadow_ids:
            # The only representation of this item is shadow (dry-run) work --
            # it ships nothing, so it is invisible to the sprint.
            continue
        else:
            queued += 1
    total = len(items)
    pct = round((done / total) * 100) if total else 0
    return {'total': total, 'queued': queued, 'inProgress': in_progress,
            'done': done, 'pct': pct, 'landed': landed}


# The sprint-close velocity signal is a trailing per-team history: how much of
# a just-closed sprint actually landed (count + size points, when estimated).
# Capped so the digest stays a rolling window, not a whole-of-history ledger.
TEAM_VELOCITY_MAX = 20

# Declarative operator-policy bindings: world/operator_policy.json is the
# DATA; the scalar constants above are the code mirror. bind_policy registers
# (attribute, policy key) pairs and applies them now; every later
# config.reload_policy_config() re-applies them via apply_policy_bindings.
# The rebind runs here, not at the first constant site, because several
# rebound targets (TASK_WORK_DURATION_S, TASK_CONTENT_TIMEOUT_S/_MAX_S,
# TASK_CYCLE_S, MAX_REVIEW_CYCLES, and TEAM_VELOCITY_MAX) are defined above
# this point -- import order must not let their defaults clobber the values.
bind_policy(__name__, [
    ('MAX_TEAM_MEMBERS', 'maxTeamMembers'),
    ('SCRUM_MASTER_MIN_TEAM_SIZE', 'scrumMasterMinTeamSize'),
    ('SMALL_TEAM_STUCK_TRIGGER', 'smallTeamStuckTrigger'),
    ('MAX_ACTIVE_AGENTS', 'maxActiveAgents'),
    ('MAX_TOTAL_AGENTS', 'maxTotalAgents'),
    ('DELIVERABLE_GRADE_FLOOR', 'deliverableGradeFloor'),
    ('SUCCESS_GRADE_FLOOR', 'successGradeFloor'),
    ('HIRE_COOLDOWN_MS', 'hireCooldownMs'),
    ('FIRING_COOLDOWN_MS', 'firingCooldownMs'),
    ('SKILL_REVIEW_CADENCE_MS', 'skillReviewCadenceMs'),
    ('DISTILL_CADENCE_MS', 'distillCadenceMs'),
    ('REFINEMENT_CADENCE_MS', 'refinementCadenceMs'),
    ('ROADMAP_CADENCE_MS', 'roadmapCadenceMs'),
    ('WORK_ITEM_MAX_ATTEMPTS', 'workItemMaxAttempts'),
    ('STALE_WORK_TIMEOUT_MS', 'staleWorkTimeoutMs'),
    ('STALE_WORK_BUDGET_GRACE_S', 'staleWorkBudgetGraceS'),
    ('MAX_BACKLOG_ITEMS', 'maxBacklogItems'),
    ('BACKLOG_PULLS_PER_CLOSE', 'backlogPullsPerClose'),
    ('ESCALATION_MAX_OPEN', 'escalationMaxOpen'),
    ('MORALE_APPROVED_WEIGHT', 'moraleApprovedWeight'),
    ('MORALE_APPROVED_CAP', 'moraleApprovedCap'),
    ('MORALE_DROPPED_WEIGHT', 'moraleDroppedWeight'),
    ('MORALE_NEGLECT_WEIGHT', 'moraleNeglectWeight'),
    ('MORALE_NEGLECT_CAP', 'moraleNeglectCap'),
    ('HIRE_DURATION_MS', 'hireDurationMs'),
    ('FIRING_REVIEW_DURATION_MS', 'firingReviewDurationMs'),
    ('REPORT_STRIKE_WINDOW_MS', 'reportStrikeWindowMs'),
    ('ONBOARD_MEET_DURATION_MS', 'onboardMeetDurationMs'),
    ('ONBOARD_READINESS_TIMEOUT_MS', 'onboardReadinessTimeoutMs'),
    ('SOCIAL_CADENCE_MS', 'socialCadenceMs'),
    ('SOCIAL_MEET_MS', 'socialMeetMs'),
    ('SOCIAL_ADOPT_CONFIDENCE', 'socialAdoptConfidence'),
    ('FAILURE_COOLDOWN_HALF_LIFE_S', 'failureCooldownHalfLifeS'),
    ('FAILURE_COOLDOWN_THRESHOLD', 'failureCooldownThreshold'),
    ('FREE_SPIKE_ALLOWANCE_PER_WEEK', 'freeSpikeAllowancePerWeek'),
    ('FREE_SPIKE_GLOBAL_CAP_PER_WEEK', 'freeSpikeGlobalCapPerWeek'),
    ('FREE_SPIKE_BUDGET_MS', 'freeSpikeBudgetMs'),
    ('FREE_SPIKE_DEFER_MS', 'freeSpikeDeferMs'),
    ('WORKER_HELP_AFTER_FAILS', 'workerHelpAfterFails'),
    ('COACHING_MAX_ROUNDS', 'coachingMaxRounds'),
    ('TEAM_VELOCITY_MAX', 'teamVelocityMax'),
    ('WORK_ITEM_STALL_CEILING', 'workItemStallCeiling'),
    ('TIER_THROTTLE_RATIO', 'tierThrottleRatio'),
    ('TASK_WORK_DURATION_S', 'taskWorkDurationS'),
    ('TASK_CONTENT_TIMEOUT_S', 'taskContentTimeoutS'),
    ('TASK_CONTENT_TIMEOUT_MAX_S', 'taskContentTimeoutMaxS'),
    ('TASK_CYCLE_S', 'taskCycleS'),
    ('MAX_REVIEW_CYCLES', 'maxReviewCycles'),
    ('PLACEMENT_MIN_DIST', 'placementMinDist'),
    ('RULE_MINE_CADENCE_MS', 'ruleMineCadenceMs'),
    ('RUNBOOK_MAX_ENTRIES_PER_PRODUCT', 'runbookMaxEntriesPerProduct'),
    ('STALE_WORK_MAX_REPLANS', 'staleWorkMaxReplans'),
    ('REFINEMENT_MAX_REQUESTS', 'refinementMaxRequests'),
    ('BREAKDOWN_MAX_REQUESTS', 'breakdownMaxRequests'),
    ('TASK_CONTENT_GRACE_MAX_S', 'taskContentGraceMaxS'),
    ('_FEEDBACK_MAX', 'feedbackMax'),
    ('MIN_RESEARCH_CADENCE_MS', 'minResearchCadenceMs'),
    ('MIN_PIPELINE_CADENCE_MS', 'minPipelineCadenceMs'),
    ('_DEVICE_CHECKIN_HISTORY_MAX', 'deviceCheckinHistoryMax'),
    ('_DEVICE_CHECKIN_RETENTION_DAYS', 'deviceCheckinRetentionDays'),
])


def close_sprint(state, sprint_id, now_ms=None):
    """Mark a sprint closed (a container action -- already-queued items finish
    or age out normally; closing doesn't cancel work). Sprint ROLLOVER: any card
    still waiting in the queue (tagged with this sprint, not yet done) is
    recorded on the record as `rolledOver` (id + title), so a follow-up sprint
    created for the same team carries it over explicitly instead of silently
    losing the un-landed scope. Sprint VELOCITY: on the active->closed
    transition the record gains `velocity` (landed/total/pct/rolledOver/points/
    elapsedMs) and is folded into the per-team trailing history
    state['teamVelocity'] -- the signal a director reads to size the next
    sprint. Idempotent: re-closing an already-closed sprint recomputes nothing
    (no double velocity, no closedAt overwrite). Returns the updated record,
    or None if the sprint doesn't exist."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    sprints = state.get('sprints') or {}
    record = sprints.get(sprint_id)
    if not record:
        return None
    if record.get('status') != 'closed':
        record['rolledOver'] = [
            {'id': _sprint_item_id(it), 'title': it.get('title')}
            for it in _unfinished_sprint_items(state, sprint_id)]
        record['closedAt'] = now_ms
        _record_sprint_velocity(state, record, now_ms)
    record['status'] = 'closed'
    # Sprint complete is one of the two moments the player should hear from
    # the tank (the other is a player-assigned story/spike landing).
    vel = record.get('velocity') or {}
    _queue_player_email(
        state, 'sprint_complete',
        f'Sprint complete: {record.get("name") or sprint_id}',
        (f'All {vel.get("total", "?")} items in sprint "{record.get("name") or sprint_id}" '
         f'({record.get("teamId") or "team"}) are done and approved.\n'
         f'Landed: {vel.get("landed", vel.get("done", "?"))}/{vel.get("total", "?")} '
         f'({vel.get("pct", 100):.0f}%).\n'
         + (f'Rolled over: {", ".join(r.get("title", "") for r in record.get("rolledOver") or [])}\n'
            if record.get('rolledOver') else '')
         + '\nNew backlog is ready for refinement.'))
    return record


def _sprint_item_estimate(state, sprint_id, title, room):
    """The size-estimate WEIGHT of one sprint item, looked up from the live
    state (an unassigned card still in the queue carries it; an assigned one
    rides on its durable task). 0 when the card has no numeric estimate --
    'not estimated' is honest, never guessed as a small size."""
    for it in (state.get('workQueue') or []):
        if it.get('sprintId') == sprint_id and it.get('title') == title \
                and (it.get('room') or None) == room:
            return size_estimate_weight(it.get('sizeEstimate'))
    for t in (state.get('tasks') or {}).values():
        if t.get('shadow'):
            continue
        if t.get('title') == title and (t.get('room') or None) == room:
            return size_estimate_weight(t.get('sizeEstimate'))
    return 0


def _sprint_item_done(state, title, room):
    """True when a sprint item has a matching DONE, non-shadow task -- the same
    'landed' test sprint_progress derives (a peer-gated story counts only once
    its gate closed)."""
    return any(t.get('status') == 'done' and t.get('title') == title
               and (t.get('room') or None) == room and not t.get('shadow')
               for t in (state.get('tasks') or {}).values())


def _record_sprint_velocity(state, record, now_ms):
    """Compute the velocity snapshot of a sprint at close and fold it into the
    per-team trailing history (state['teamVelocity'], capped at
    TEAM_VELOCITY_MAX per team). `landed`/`total`/`pct` mirror sprint_progress;
    `rolledOver` mirrors the close record; `pointsLanded`/`pointsTotal` sum the
    size-estimate weights (S=1 / M=2 / L=3) and are None when the sprint carried
    no numeric estimates at all. `elapsedMs` is wall-time since creation. Pure
    state mutation; called once from close_sprint on the active->closed
    transition (idempotent -- a re-close never re-folds)."""
    items = record.get('items') or []
    points_total = 0
    points_estimated = False
    landed_points = 0
    landed = 0
    for pair in items:
        title, room = tuple(pair)
        w = _sprint_item_estimate(state, record.get('id'), title, room)
        if w:
            points_estimated = True
            points_total += w
        if _sprint_item_done(state, title, room):
            landed += 1
            if w:
                landed_points += w
    total = len(items)
    pct = round((landed / total) * 100) if total else 0
    velocity = {
        'landed': landed,
        'total': total,
        'pct': pct,
        'rolledOver': len(record.get('rolledOver') or []),
        'pointsLanded': landed_points if points_estimated else None,
        'pointsTotal': points_total if points_estimated else None,
        'elapsedMs': max(0, int(now_ms) - int(record.get('createdAt') or 0)),
    }
    record['velocity'] = velocity
    history = state.setdefault('teamVelocity', {})
    for tid in (record.get('teamIds') or []):
        bucket = history.setdefault(tid, [])
        bucket.append(velocity)
        _vel_cap = team_policy(state, tid, 'teamVelocityMax')
        if len(bucket) > _vel_cap:
            del bucket[:len(bucket) - int(_vel_cap)]
    return velocity


def _unfinished_sprint_items(state, sprint_id):
    """The sprint's cards still waiting in the queue (not yet done): queued
    workQueue items tagged with this sprint that have no matching 'done' task.
    These are exactly what a sprint close carries over into the next sprint
    (in-flight cards keep flowing -- closing never cancels work -- and are not
    re-planned)."""
    tasks = state.get('tasks') or {}
    unfinished = []
    for it in (state.get('workQueue') or []):
        if it.get('sprintId') != sprint_id:
            continue
        if any(t.get('status') == 'done' and t.get('title') == it.get('title')
               and t.get('room') == it.get('room')
               for t in tasks.values()):
            continue
        unfinished.append(it)
    return unfinished


def _carry_over_sprint_work(state, record, team_ids):
    """Sprint rollover: when a new sprint is created, carry the PRIOR closed
    sprint's unfinished cards (its `rolledOver` set) into the new sprint. Their
    queued items are re-tagged with the new sprint_id and their ids are added to
    the new sprint's `items`, so the next sprint's scope and progress include the
    work that didn't land last time. First-come: the most recent closed sprint
    whose teams overlap the new sprint is the carry source. Pure state mutation;
    returns the count of cards carried over."""
    if not team_ids:
        return 0
    prior = None
    for s in (state.get('sprints') or {}).values():
        if s.get('id') == record.get('id') or s.get('status') != 'closed':
            continue
        if not (s.get('teamIds') or []) or not any(t in s['teamIds'] for t in team_ids):
            continue
        if prior is None or (prior.get('createdAt') or 0) < (s.get('createdAt') or 0):
            prior = s
    if not prior:
        return 0
    rolled = {r.get('id') for r in (prior.get('rolledOver') or [])}
    if not rolled:
        return 0
    items = record.setdefault('items', [])
    carried = 0
    for it in (state.get('workQueue') or []):
        if it.get('sprintId') != prior.get('id'):
            continue
        ident = _sprint_item_id(it)
        if ident not in rolled:
            continue
        it['sprintId'] = record['id']
        if ident not in items:
            items.append(ident)
        carried += 1
    return carried


def _auto_close_completed_sprints(state, now_ms=None):
    """Close any ACTIVE sprint whose entire body of work is done AND approved.
    'Done and approved' is exactly what sprint_progress counts as `done`: for a
    peer-gated story that means 2 clean peer approvals have closed its gate
    (_close_gated_story flips status to 'done' only on that), and for a
    non-gated task it means finish_task completed it. A sprint made only of
    non-gated items closes as soon as those tasks finish. So done == total
    is the "fully completed and approved for the entire sprint" signal, and an
    active sprint at that point is automatically closed instead of waiting for
    the player to tap close. Returns the list of sprint ids closed."""
    sprints = state.get('sprints') or {}
    closed = []
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    for sid, record in list(sprints.items()):
        if record.get('status') != 'active':
            continue
        progress = sprint_progress(state, sid)
        if progress and progress.get('total') and progress['done'] >= progress['total']:
            close_sprint(state, sid, now_ms)
            record['closedAt'] = now_ms
            record['autoClosed'] = True
            closed.append(sid)
            # WS-14 sprint-close ceremony: retrospective + shared-backlog pull +
            # refinement kick happen the moment the sprint fully lands.
            _on_sprint_closed(state, record, now_ms)
    return closed
# ---------------------------------------------------------------------------
# WS-14: shared backlog + features. When every team is already committed to an
# active sprint, extra large asks are broken down by an authority into STORIES /
# SPIKES filed in a SHARED unassigned backlog under a FEATURE (created when none
# exists). Any team is eligible to pull at sprint close (first-come); the pull
# claims the item for the team with a team-scoped story key (reusing the JIRA
# issue counters) and queues it as real work. Pure state mutation -- serve.py
# does the model-call breakdown and keeps the decoupled constants.
# ---------------------------------------------------------------------------


def _next_feature_id(state):
    """Monotonic feature id (feat-1, feat-2, ...), so a cold state starts at 1
    and a hot one never collides (mirrors next_sprint_id / next_product_id)."""
    n = int(state.get('featureCounter') or 0) + 1
    state['featureCounter'] = n
    return f'feat-{n}'


def _next_backlog_id(state):
    """Monotonic shared-backlog item id (bl-1, bl-2, ...)."""
    n = int(state.get('backlogCounter') or 0) + 1
    state['backlogCounter'] = n
    return f'bl-{n}'


def _find_feature(state, name):
    """The feature whose name matches `name` (case-insensitive), or None."""
    name = (name or '').strip()
    if not name:
        return None
    for f in (state.get('features') or {}).values():
        if f.get('name') and f['name'].lower() == name.lower():
            return f
    return None


def create_or_reuse_feature(state, name, now_ms, quarter=None, target_date_ms=None,
                            created_by=None):
    """Pure: the feature record for `name`, reusing the existing one when a
    feature by that name is already on the board (a feature groups the backlog
    stories of one initiative; breakdowns of the same initiative share it).
    Returns the record. Mutates state['features'] + state['featureCounter']."""
    f = _find_feature(state, name)
    if f:
        return f
    fid = _next_feature_id(state)
    f = {
        'id': fid,
        'name': (name or '').strip() or f'Feature {fid}',
        'quarter': quarter or None,
        'targetDate': target_date_ms or None,
        'status': 'open',  # open / in_progress / done
        'storyIds': [],
        'createdAt': now_ms,
        'createdBy': created_by or None,
    }
    state.setdefault('features', {})[fid] = f
    return f


def add_backlog_item(state, title, feature_id, created_by, now_ms,
                     item_type='story', acceptance_criteria=None,
                     size_estimate=None, goal=None, instructions=None,
                     blocked_by=None, never=None):
    """Pure: file one unassigned STORY/SPIKE in the SHARED backlog under
    `feature_id`. Returns the item dict, or None if the cap is reached or the
    item is malformed. Mutates state['backlog'] + state['backlogCounter'] +
    the feature's storyIds. Unassigned until a team pulls it (teamId + storyKey
    are assigned at pickup).

    A card carries NO room: the buildings are shared across teams, so the room
    is an EXECUTION detail -- the agent who picks the card up figures out where
    the work needs to happen (_assign_due_item resolves it at assignment)."""
    title = (title or '').strip()
    if not title:
        return None
    backlog = state.setdefault('backlog', [])
    if len(backlog) >= MAX_BACKLOG_ITEMS:
        return None
    bid = _next_backlog_id(state)
    item = {
        'id': bid,
        'title': title,
        'type': item_type if item_type in ('spike', 'research') else 'story',
        'featureId': feature_id or None,
        'status': 'blocked' if blocked_by else 'ready',  # ready / blocked / picked
        'blockedBy': blocked_by or None,
        'acceptanceCriteria': acceptance_criteria or None,
        'sizeEstimate': size_estimate or None,
        'goal': goal or None,
        'instructions': instructions or None,
        'never': never or None,
        'createdBy': created_by or None,
        'createdAt': now_ms,
        'teamId': None,   # assigned at pickup
        'storyKey': None,  # assigned at pickup (DEV-1, ...)
    }
    backlog.append(item)
    if item['featureId']:
        f = (state.get('features') or {}).get(item['featureId'])
        if f:
            f.setdefault('storyIds', []).append(bid)
    return item


def backlog_ready_items(state):
    """Eligible shared-backlog items: READY (not blocked-by-dependency) and
    unassigned (no teamId yet), in FIFO order. A blocked item waits for its
    dependency to clear."""
    return [b for b in (state.get('backlog') or [])
            if b.get('status') == 'ready' and not b.get('teamId')]


def pull_backlog_item(state, team_id, now_ms):
    """Atomic first-come pull for `team_id`: claim the oldest eligible item,
    assign teamId + a team-scoped story key, flip status -> 'picked'. Returns
    the claimed item, or None if nothing is eligible."""
    ready = backlog_ready_items(state)
    if not ready:
        return None
    item = ready[0]
    item['teamId'] = team_id
    item['storyKey'] = next_issue_key(state, team_id)
    item['status'] = 'picked'
    item['pickedAt'] = now_ms
    return item


def _resolve_assignment_room(pick):
    """The room a ROOM-LESS work card is routed to when an agent picks it up.
    A shared-backlog card is room-free on purpose (the buildings are shared; the
    room is an execution detail), so the ASSIGNED AGENT figures out where the
    work needs to happen -- realized deterministically by the card's nature: an
    investigation spike goes to the observatory, a deliverable story to the
    press office (real files get written there)."""
    return 'observatory' if pick.get('taskType') in ('spike', 'research') else 'pressoffice'


def _pull_backlog_for_team(state, team_id, now_ms):
    """Pull the first eligible shared-backlog card for `team_id` and queue it as
    real work, tagged with its feature + backlog item + team so the sprint digest
    and backlog board can read completion back off the durable task mirror. The
    card is queued ROOM-LESS on purpose: the agent who picks it up figures out
    where the work needs to happen (_assign_due_item resolves the room at
    assignment). Returns the pulled card, or None when nothing is eligible."""
    item = pull_backlog_item(state, team_id, now_ms)
    if not item:
        return None
    is_spike = item.get('type') == 'spike'
    is_research = item.get('type') == 'research'
    instructions = item.get('instructions')
    if not instructions:
        instructions = (f"({item.get('storyKey') or item['id']}) {item.get('title')}")
        if item.get('acceptanceCriteria'):
            instructions += f"\n\nAcceptance criteria:\n{item['acceptanceCriteria']}"
        if item.get('never'):
            instructions += f"\n\nNEVER:\n{item['never']}"
    queue_work(state, [{
        'title': item['title'],
        'room': None,  # the assigned agent decides where the work happens
        'instructions': instructions,
        'goal': item.get('goal'),
        'teamId': team_id,
        'taskType': ('spike' if is_spike else ('research' if is_research else 'code')),
        # Both investigation-class lanes (spike and research) get the same
        # short time-box: neither has a committed deliverable to ship.
        'budgetMs': 60_000 if (is_spike or is_research) else None,
        # A breakdown story's size estimate survives the shared backlog onto the
        # pulled card (same contract as _resolve_breakdown's direct queue path).
        'sizeEstimate': item.get('sizeEstimate'),
        'featureId': item.get('featureId'),
        'backlogItemId': item['id'],
        'never': item.get('never') or None,
    }])
    _log_governance(state, team_id, 'backlog_pull',
                    {'item': item['id'], 'storyKey': item.get('storyKey'),
                     'feature': item.get('featureId'), 'team': team_id})
    return item


def _team_in_active_sprint(state, team_id):
    """True when `team_id` is tied to any ACTIVE sprint. WS-14: a team refines
    its backlog at sprint close, never mid-sprint -- the active sprint is the
    committed work, and refinement waits until the team is free."""
    for s in (state.get('sprints') or {}).values():
        if s.get('status') == 'active' and team_id in (s.get('teamIds') or []):
            return True
    return False


def _on_sprint_closed(state, record, now_ms):
    """WS-14 sprint-close ceremony, run the moment a sprint auto-closes: queue
    the team's START/STOP/CONTINUE retrospective (a ceremony on a later pass),
    pull the first eligible shared-backlog item for each team that sprint
    touched (first-come), and kick each team's backlog refinement so the scrum
    master re-plans at close. Any on-call backup who actually SERVED pages this
    sprint is rotated to the end of her team's on-call queue so she isn't paged
    again next sprint before the rotation catches up. Pure state mutation;
    nothing here blocks."""
    team_ids = [t for t in (record.get('teamIds') or []) if _team_row(state, t)]
    _rotate_served_oncalls(state, team_ids)
    retros = state.setdefault('pendingSprintRetros', [])
    if record['id'] not in retros:
        retros.append(record['id'])
    # Director failure review: every card that failed this sprint is read on a
    # later pass, root-caused, and turned into a bug card when the cause is a
    # real defect rather than infrastructure or process noise.
    state.setdefault('pendingFailureReviews', {})[record['id']] = {
        'at': now_ms + FAILURE_REVIEW_DELAY_MS,
        'sprintId': record['id'],
        'teamIds': team_ids,
    }
    for tid in team_ids:
        # Cross-team borrowing: loans to this team end at sprint close unless
        # the team still has pending work (the feature-need case keeps them).
        _end_loans_for_team(state, tid, now_ms)
        for _ in range(team_policy(state, tid, 'backlogPullsPerClose')):
            _pull_backlog_for_team(state, tid, now_ms)
        kick_refinement_now(state, tid, now_ms)
    # Director gap 1: team-health review at sprint close -- each director grades
    # the trailing health of the workers under them and coaches anyone below the
    # delivery floor (never directors). Pure, cheap, idempotent.
    _team_health_review(state, team_ids, now_ms)


# ---------------------------------------------------------------------------
# Phase E: products + wiki. A PRODUCT is a named, director-authored artifact
# with a spec/owner/contributors/revision log whose "release" freezes its
# sandbox repo into library/projects/<id>/v<N>/ and flips status to
# 'released'. Products + wiki are both SERVER-OWNED state (like teams/sprints)
# so the client's 5s autosave can never revert a release or a wiki edit.
# sim.py stays pure (no filesystem, no serve import at call site): these
# helpers mutate the state CATALOG; the serve.py wrapper does the actual
# sandbox snapshot + hashed-passport chaining around them. The revision CONTENT
# lives on disk; state[products][..]['revisions'] is the findable catalog.
# ---------------------------------------------------------------------------

PRODUCT_STATUSES = ('draft', 'in_progress', 'review', 'released')
def create_product(state, product_id, name, summary, spec, owner_id, sandbox_id,
                   team_id=None, contributor_ids=None, handles=None):
    """Create a product catalog entry. Pure: returns the record dict, or None
    if an id/owner/sandbox/name clash means nothing was created. Sandbox
    existence is validated by the CALLER (serve.py knows the real dirs); this
    just persists the association. Mutates state['products']."""
    products = state.setdefault('products', {})
    if product_id in products:
        return None
    name = (name or '').strip()
    if not name:
        return None
    record = {
        'id': product_id,
        'name': name,
        'summary': (summary or '').strip(),
        'spec': (spec or '').strip(),
        'ownerId': owner_id or None,
        'sandboxId': sandbox_id or None,
        'teamId': team_id or None,
        'contributorIds': list(contributor_ids or []),
        'handles': list(handles or []),
        'status': 'draft',
        'revisions': [],
        'nextRevision': 1,
    }
    products[product_id] = record
    return record


def next_product_revision(state, product_id):
    """Pure: the next (revision_no, relative_path) for a product, WITHOUT
    mutating. Serve.py lays the snapshot down at library/projects/<id>/<path>,
    then calls product_release_record to catalog it. Returns (n, relpath) or
    (None, None) for an unknown product."""
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return None, None
    n = int(record.get('nextRevision') or 1)
    return n, f'v{n}'


def product_release_record(state, product_id, revision_no, released_by,
                           note=None, now_ms=None, target_path=None):
    """Pure: append a revision to a product's catalog and flip status ->
    'released'. Called by serve.py AFTER the sandbox snapshot landed on disk,
    so a filesystem/passport failure never half-catalogs. Returns the appended
    revision record, or None for an unknown product. Mutates state['products']."""
    import time as _time
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return None
    now_ms = now_ms if now_ms is not None else int(_time.time() * 1000)
    rev = {
        'n': revision_no,
        'releasedBy': released_by or None,
        'at': now_ms,
        'note': (note or '').strip(),
        'path': target_path or f'v{revision_no}',
    }
    record.setdefault('revisions', []).append(rev)
    record['nextRevision'] = revision_no + 1
    record['status'] = 'released'
    return rev


def set_product_status(state, product_id, status):
    """Pure: transition a product between draft/in_progress/review. 'released'
    is NOT reachable here -- it only flips via product_release_record. Returns
    the updated record, or None for unknown product / invalid status."""
    if status not in PRODUCT_STATUSES or status == 'released':
        return None
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return None
    record['status'] = status
    return record


# --- Wiki: structured, director-structurable knowledge ----------------------
# Pages carry category + version + history; the BODY is markdown written to
# library/wiki/<category>/<id>.md by serve.py. Reads are open; writes bump the
# version and append to history. state['wiki']['pages'] holds metadata only --
# never the body -- so the catalog stays lightweight and the merge stays safe.

WIKI_CATEGORY_ROOMS = {  # default category -> room affinity for read-before-act
    'workroom': 'pressoffice',
    'research': 'observatory',
    'ops': 'bank',
    'think_tank': None,
    'product': None,
}


def wiki_write_page(state, page_id, title, category, body, edited_by,
                    edited_at_ms=None, village_id=None, owner=None,
                    clock=SLOW_CLOCK, review_after_ms=None,
                    source=None, scope='team'):
    """Pure: create or update a wiki page's METADATA + return the lined-up
    record for serve.py to persist (serve writes page['body'] to disk only for
    pre-existing/new pages; the metadata carries version + history). Rejects
    too-long bodies and unknown categories. Returns (record, is_new).

    Memory-engineering fields: owner (who is accountable for the claim),
    clock (SLOW/FAST -- fast-clock pages must be re-fetched, not trusted),
    review_after_ms (expiry -- an expired page is no longer injected), source
    (where the claim can be verified), scope (who may reuse it). These are the
    review-card fields that make a stale or wrong memory findable and
    correctable."""
    import time as _time
    edited_at_ms = edited_at_ms if edited_at_ms is not None else int(_time.time() * 1000)
    categories = (state.get('wiki') or {}).get('categories') or {}
    if category not in categories:
        return None, False
    if clock not in MEMORY_CLOCKS:
        clock = SLOW_CLOCK
    body = body or ''
    if len(body) > 200_000:
        return None, False
    pages = ensure_wiki(state)
    prev = pages.get(page_id)
    version = (int(prev.get('version') or 0)) + 1 if prev else 1
    history = list((prev or {}).get('history') or [])
    if prev:
        history = history + [{'version': prev.get('version'), 'editedBy': prev.get('editedBy'),
                              'editedAt': prev.get('editedAt')}]
    record = {
        'id': page_id,
        'title': title or page_id,
        'category': category,
        'villageId': village_id or DEFAULT_VILLAGE,
        'clock': clock,
        'owner': owner or edited_by or None,
        'scope': scope,
        'source': source or None,
        'reviewAfterMs': int(review_after_ms) if review_after_ms else None,
        'version': version,
        'editedBy': edited_by or None,
        'editedAt': edited_at_ms,
        'history': history,
    }
    pages[page_id] = record
    return record, prev is None


def _page_room_affinity(state, page):
    """A task room matches a page's category via the default affinity map
    (overridable per-category in state), else the page is think tank/product-wide."""
    cat = (page or {}).get('category')
    mapping = (state.get('wiki') or {}).get('categoryRooms') or WIKI_CATEGORY_ROOMS
    return mapping.get(cat)


def wiki_read_pages(state, room, max_pages=3, village_id=DEFAULT_VILLAGE, now_ms=None):
    """Pure: the subset of wiki pages relevant to a task in `room`, chosen by
    category-room affinity AND the reader's village. Only pages affined to THIS
    room (or to the think tank as a whole -- a None affinity) AND written by
    THIS village are injected; a page affined to a different room, or from a
    different village, is never pulled in. "Read before acting" means reading
    the pages about the work you're about to do in your own village, not a
    random archive or a neighbor's memory. Returns metadata-only records
    (id/title/category/version/villageId/clock) so serve.py can fetch the
    bodies. Stable order (recency, then version) for deterministic injection.
    A page whose reviewAfterMs has passed is treated as expired: it is NOT
    injected as trusted context (the "recheck before acting" gate)."""
    pages = ensure_wiki(state)
    now_ms = now_ms if now_ms is not None else _time_ms()
    scored = []
    for page_id, rec in pages.items():
        if (rec.get('villageId') or DEFAULT_VILLAGE) != village_id:
            continue  # a different village's memory is not this reader's
        review_after = rec.get('reviewAfterMs')
        if review_after and int(review_after) < now_ms:
            continue  # expired -- do not inject as trusted knowledge
        affinity = _page_room_affinity(state, rec)
        if affinity != room and affinity is not None:
            continue  # affined to another room -> irrelevant here
        score = 0 if affinity == room else 1  # exact room beats think tank-wide
        scored.append((score, -(int(rec.get('editedAt') or 0)), page_id, rec))
    scored.sort()
    return [r for _s, _a, _pid, r in scored[:max_pages]]


def _time_ms():
    import time as _t
    return int(_t.time() * 1000)


def _page_is_expired(rec, now_ms):
    review_after = rec.get('reviewAfterMs')
    return bool(review_after and int(review_after) < now_ms)


def record_memory_provenance(state, actor, page_id, title, claim, source,
                             scope, clock, review_after_ms, village_id):
    """Pure: append a memory-write provenance entry to state['memoryLedger'].
    This is the audit trail (rari's external ledger) that answers "what did the
    team learn, where, who owns it, and when to recheck" -- independent of the
    wiki body so a stale or wrong memory can be found and corrected later.
    Best-effort; a ledger append never blocks the write."""
    entry = {
        'ts': _time_ms(),
        'actor': actor,
        'pageId': page_id,
        'title': title,
        'claim': (claim or '')[:2000],
        'source': source or None,
        'owner': None,
        'scope': scope or 'team',
        'clock': clock,
        'villageId': village_id,
        'reviewAfterMs': int(review_after_ms) if review_after_ms else None,
    }
    state.setdefault('memoryLedger', []).append(entry)
    return entry


def resolve_memory_fields(state, body, actor):
    """Resolve the memory-engineering fields (clock, owner, scope, source,
    review_after) for a wiki/taste write from a request body + the acting
    agent. Defaults: slow clock, actor as owner, team scope, default review
    window. Returns a dict of kwargs for the write."""
    import time as _t
    clock = (body.get('clock') or '').strip().lower()
    if clock not in MEMORY_CLOCKS:
        clock = SLOW_CLOCK
    owner = (body.get('owner') or '').strip() or actor
    scope = (body.get('scope') or '').strip() or 'team'
    source = (body.get('source') or '').strip() or None
    review_days = body.get('reviewAfterDays')
    if review_days is None:
        review_days = DEFAULT_REVIEW_DAYS
    try:
        review_days = max(0, int(review_days))
    except (TypeError, ValueError):
        review_days = DEFAULT_REVIEW_DAYS
    review_after_ms = int(_t.time() * 1000) + int(review_days * 24 * 3600 * 1000)
    return {'clock': clock, 'owner': owner, 'scope': scope,
            'source': source, 'review_after_ms': review_after_ms}
