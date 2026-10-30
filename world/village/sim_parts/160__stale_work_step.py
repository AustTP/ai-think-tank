

def _stale_work_step(state, now, now_ms):
    """SM gap 1: stale in-flight work oversight. Bugs already have a restore
    alarm (RESTORE_TIMEOUT_MS via _escalation_step); a NON-bug task wedged in
    'walking'/'working' had NO age alarm -- if the completion loop and the
    orphan/stall repairs all miss it, it holds its worker forever.

    A coarse-cadence sweep finds tasks whose in-flight age is definitively past
    any legitimate budget:
      - 'working': still 'working' long after workUntil elapsed (the completion
        loop finishes every held working task within a pass, so still-working at
        workUntil + grace means the wedge won self-resolve);
      - 'walking': walking for longer than a generous ceiling (never arrived).
    A first staleness re-queues the card fresh (fast re-issue, priority bump,
    holder released). A repeat offender -- re-planned STALE_WORK_MAX_REPLANS
    times for the same (title, room) -- is routed to the owning team's scrum
    master as a work-request (the "SM re-plan"): refinement's accept/reject
    decides whether the work survives, so the loop is bounded and never spins.
    The replan counter lives in `_staleWorkReplans` keyed by a JSON-safe repr
    of (title, room): the map is part of kv_state, which is serialized on every
    save, so a tuple key would make the whole save fail.
    Never touches bugs (own alarm), shadow dry-runs, or review/fix subtasks
    (their deadlock lives in the stuck-gate watchdog). Pure state mutation;
    runs before the idle gate so a quiet tank still re-plans a wedged card.
    Returns the number of cards re-planned (requeued + SM-routed)."""
    if now_ms - (state.get('lastStaleWorkSweep') or 0) < STALE_WORK_CADENCE_MS:
        return 0
    state['lastStaleWorkSweep'] = now_ms
    tasks = state.get('tasks')
    if not isinstance(tasks, dict) or not tasks:
        return 0
    agents = state.get('agents') or {}
    replan_map = state.setdefault('_staleWorkReplans', {})
    # One roster index for this sweep. agent_team_ref scans the whole roster
    # per task; on a large in-flight set that becomes tasks * roster.
    team_by_agent = {
        d.get('id'): d.get('director')
        for d in (state.get('agentRoster') or [])
        if isinstance(d, dict)
    }
    replanned = 0
    for tid, task in list(tasks.items()):
        if not isinstance(task, dict):
            continue
        if task.get('status') not in ('walking', 'working'):
            continue
        if task.get('taskType') == 'bug' or task.get('incident') or task.get('shadow'):
            continue  # bugs own an alarm; shadows never ship; reviews -> stuck-gate
        if task.get('reviewOf'):
            continue  # review/fix subtasks are the stuck-gate watchdog's domain
        if task.get('_suspendedForScheduled'):
            continue  # parked for a scheduled item -- budget restored on resume
        opened = task.get('openedAt') or task.get('assignedAt') or task.get('createdAt')
        if opened is None:
            continue  # no start stamp -> cannot judge age (pre-existing behavior)
        _sref = task.get('teamId') or team_by_agent.get(task.get('assignedTo'))
        if task.get('status') == 'walking':
            age_ms = _task_active_age_ms(state, task, now_ms)
            if age_ms is None or age_ms < team_policy(state, _sref, 'staleWorkTimeoutMs'):
                continue
        else:  # 'working'
            grace_ms = team_policy(state, _sref, 'staleWorkBudgetGraceS') * 1000
            if now_ms <= _effective_work_deadline_ms(state, task, now_ms) + grace_ms:
                continue
        # Genuinely wedged non-bug in-flight card. Release any holder so the
        # worker is never pinned by a task that will no longer resolve.
        for aid, a in list(agents.items()):
            if isinstance(a, dict) and a.get('task') == tid:
                a['task'] = None
                a['busy'] = False
                a['inRoom'] = None
                break
        key = repr((task.get('title') or '', task.get('room') or ''))
        replans = replan_map.get(key, 0)
        if replans < STALE_WORK_MAX_REPLANS:
            replan_map[key] = replans + 1
            _requeue_stale_work(state, task)
            _log_governance(state, 'admin', 'stale_work_requeued',
                            {'task': tid, 'title': task.get('title')[:120],
                             'room': task.get('room'), 'replans': replans + 1})
        else:
            # Terminal re-plan: hand to the owning team's scrum master.
            if _sm_replan_stale_work(state, task, now_ms):
                _log_governance(state, 'admin', 'stale_work_sm_replan',
                                {'task': tid, 'title': task.get('title')[:120],
                                 'room': task.get('room'), 'replans': replans + 1})
            else:
                _log_governance(state, 'admin', 'stale_work_dropped',
                                {'task': tid, 'title': task.get('title')[:120],
                                 'room': task.get('room')})
        del tasks[tid]
        replanned += 1
    return replanned


def _requeue_stale_work(state, task):
    """Fast-path re-plan: put the swept card back on the work queue so a fresh
    assignment re-issues it, bumped to at least 'high' priority (it already
    waited long enough once) and marked so a future sweep can find it fast."""
    room = task.get('room') or _resolve_assignment_room(task)
    queue_work(state, [{
        'title': task.get('title'),
        'room': room,
        'instructions': (
            (task.get('instructions') or '') +
            '\n\n[re-planned] This card was swept as stale in-flight work and re-issued '
            'so it is not lost.'
            if task.get('instructions') else
            f"Re-issued stale work: {task.get('title')}"
        ),
        'goal': task.get('goal'),
        'taskType': task.get('taskType') or 'code',
        'sizeEstimate': task.get('sizeEstimate'),
        'teamId': task.get('teamId'),
        'productId': task.get('productId'),
        'featureId': task.get('featureId'),
        'backlogItemId': task.get('backlogItemId'),
        'sprintId': task.get('sprintId'),
        'priority': WORK_PRIORITY['high'],
    }])


def _requeue_paused_work(state, task, now_ms):
    """Put a credit-paused card back on the queue unchanged.

    The work never ran, so it must not be graded, counted, or reworded as a
    failure. Preserve the original scheduling fields and pin the same agent
    where the card was already pinned.
    """
    room = task.get('room') or _resolve_assignment_room(task)
    queue_work(state, [{
        'title': task.get('title'),
        'room': room,
        'instructions': task.get('instructions'),
        'goal': task.get('goal'),
        'taskType': task.get('taskType') or 'code',
        'projectLabel': task.get('projectLabel'),
        'priority': task.get('priority'),
        'notBefore': now_ms,
        'sizeEstimate': task.get('sizeEstimate'),
        'teamId': task.get('teamId'),
        'productId': task.get('productId'),
        'featureId': task.get('featureId'),
        'backlogItemId': task.get('backlogItemId'),
        'sprintId': task.get('sprintId'),
        'reviewOf': task.get('reviewOf'),
        'reviewAuthorId': task.get('reviewAuthorId'),
        'checklist': task.get('checklist') or [],
        'incident': task.get('incident'),
        'dependsOn': task.get('dependsOn'),
        'distill': task.get('distill'),
        'kbClass': task.get('kbClass'),
        'shadow': task.get('shadow'),
        'budgetMs': task.get('budgetMs'),
        'budgetBand': task.get('budgetBand'),
        'budgetUsd': task.get('budgetUsd'),
        'moonshot': task.get('moonshot'),
        'lane': task.get('lane'),
        'playerAuthored': task.get('playerAuthored'),
        'approvedTools': list(task.get('approvedTools') or []),
        'attendees': task.get('attendees') or [],
        'facilitatorId': task.get('facilitatorId'),
        'assignedTo': task.get('assignedTo'),
        'pipelineStep': task.get('pipelineStep'),
        'adversarialTaskId': task.get('adversarialTaskId'),
        'villageId': task.get('villageId'),
        'userStory': task.get('userStory'),
        'acceptanceCriteria': task.get('acceptanceCriteria'),
        'never': task.get('never'),
        'sourcePaths': task.get('sourcePaths'),
        'reportPath': task.get('reportPath'),
    }])


def _sm_replan_stale_work(state, task, now_ms):
    """Terminal re-plan: route a repeatedly-stale card to the owning team's
    scrum master as a work-request, and make refinement due on the next pass so
    the SM re-plans it promptly (accept -> new story, reject -> dropped, both
    bounded). Returns True when the request was filed for a real team."""
    room = task.get('room') or _resolve_assignment_room(task)
    if room not in VALUED_QUEUE_ROOMS:
        return False
    team_id = task.get('teamId')
    if not team_id and task.get('productId'):
        team_id = _product_director(state, task.get('productId'))
    team = _team_row(state, team_id) if team_id else None
    if not team:
        return False
    # Attribute the re-plan request to the owning team's SM (or its director),
    # so _refinement_team resolves it back to this team for grooming.
    filer = _escalation_scrum_master_for(state, team.get('directorId') or team.get('id'))
    if not filer:
        filer = team.get('directorId') or team.get('id')
    reason = ('Swept as stale in-flight work after repeated re-issues; the scrum '
              'master decides whether this still needs doing. '
              'ABS_REPLAN: this card no longer resolves on its own.')
    if not file_work_request(state, filer, (task.get('title') or 'stale work')[:120],
                             room, reason=reason):
        return False
    kick_refinement_now(state, team.get('directorId') or team.get('id'), now_ms)
    return True


def _sm_help_stuck_worker(state, task, now_ms):
    """W4: route a worker-stuck help signal to the owning team's scrum master.
    A worker whose content execution keeps FAILING is genuinely stuck; the SM
    gets a work-request so they can help/re-plan (accept -> new story, reject ->
    dropped, both bounded). Mirrors _sm_replan_stale_work's routing; returns
    True when the request was filed for a real team."""
    room = task.get('room') or _resolve_assignment_room(task)
    if room not in VALUED_QUEUE_ROOMS:
        return False
    team_id = task.get('teamId')
    if not team_id and task.get('productId'):
        team_id = _product_director(state, task.get('productId'))
    team = _team_row(state, team_id) if team_id else None
    if not team:
        return False
    filer = _escalation_scrum_master_for(state, team.get('directorId') or team.get('id'))
    if not filer:
        filer = team.get('directorId') or team.get('id')
    reason = ('A worker is stuck: repeated content-execution failures on this card. '
              'The scrum master decides whether this still needs doing, or whether '
              'the worker needs help / a different assignment.')
    if not file_work_request(state, filer, (task.get('title') or 'stuck work')[:120],
                             room, reason=reason):
        return False
    kick_refinement_now(state, team.get('directorId') or team.get('id'), now_ms)
    return True


def _worker_stuck_help_signal(state, agent_id, task, result_ok, now_ms):
    """W4: worker stuck/help signal. Tracks CONSECUTIVE content-execution
    failures per worker (a content result with ok=False -- a crash, or a red
    pipeline). A clean result resets the streak. Once the streak crosses
    WORKER_HELP_AFTER_FAILS, route a help signal to the owning team's scrum
    master (_sm_help_stuck_worker) and log governance, then reset the streak so
    the signal fires once per consecutive-failure run (the SM request dedups
    anyway, so no spam). Pure state mutation; called from the content-result
    branch of _task_cycle."""
    if result_ok:
        a = (state.get('agents') or {}).get(agent_id)
        if a:
            a['_contentFailStreak'] = 0
        return
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return
    streak = a.get('_contentFailStreak', 0) + 1
    a['_contentFailStreak'] = streak
    if streak < team_policy(state, task.get('teamId') or agent_team_ref(state, agent_id),
                            'workerHelpAfterFails'):
        return
    a['_contentFailStreak'] = 0  # re-arm only after a fresh clean result
    routed = _sm_help_stuck_worker(state, task, now_ms)
    _log_governance(state, 'admin', 'worker_stuck_help',
                    {'agent': agent_id, 'task': task.get('id'),
                     'title': (task.get('title') or '')[:120],
                     'failures': streak, 'routed': routed})


def _coaching_loop_step(state, now_ms):
    """W5: daily coaching catch-up sweep. _coach_low_grade only fires when a
    low-graded deliverable actually lands; a worker who then goes quiet could
    absorb one coaching note and never improve, with the loop never checking
    back. On a coarse cadence, this sweep re-checks every agent who HAS a
    coaching plan (low_grade or team_health) applied: if their real trailing
    grade is STILL below the floor, the coaching hasn't landed. Re-coaches
    (round-aware, same cap as _coach_low_grade) or escalates through the same
    bounded path -- a worker can't silently absorb an ignored plan forever.
    Bounded: the cap limits rounds, and escalation dedups via the SM work-
    request, so the sweep can't spin. Pure state mutation; runs before the
    idle gate on the cadence, no-op otherwise."""
    if now_ms - (state.get('lastCoachingLoop') or 0) < COACHING_LOOP_CADENCE_MS:
        return 0
    state['lastCoachingLoop'] = now_ms
    plans = state.get('growthPlans') or {}
    checked = 0
    for agent_id, agent_plans in list(plans.items()):
        if not isinstance(agent_plans, list):
            continue
        if not any(p.get('kind') in ('low_grade', 'team_health') for p in agent_plans):
            continue
        grade = _agent_trailing_grade(state, agent_id)
        if (grade is None
                or grade >= team_policy(state, agent_team_ref(state, agent_id),
                                        'deliverableGradeFloor')):
            continue
        a = (state.get('agents') or {}).get(agent_id) or {}
        room = (a.get('room') or 'pressoffice') if isinstance(a.get('room'), str) else 'pressoffice'
        if room not in VALUED_QUEUE_ROOMS:
            room = 'pressoffice'
        _coach_low_grade(state, agent_id, room, grade, now_ms, 'follow-up review')
        checked += 1
    return checked


def _task_cycle(state, now=None, grid=None, doors=None, task_id_holder=None):
    """The server-side heart of Phase 3 slice 1: one consumption pass over the
    persistent workQueue, mirroring the browser's runTaskCycleBody (tasks.js:350),
    PLUS the completion/off-duty ISSUING that the browser's finishTask/
    sendAgentOffDuty handle. Pure: mutates `state`, no DB, no serve import at
    call site (imports serve lazily only for logging drive).

    ARRIVAL is handled by SimEngine.tick (movement): when step_agent_movement
    emits ('arrive','task',aid) that tick calls `_arrive_at_task` (marks busy/
    inRoom/working, sets the work budget). (Off-duty is immediate and in-place --
    send_agent_off_duty -- so there is no off-duty ARRIVAL event.) Movement and
    lifecycle run on SEPARATE threads (sim loop 2s, task loop 6s), so this pass
    never sees the walk itself -- only its post-arrival state (task.status==
    'working' + workUntil set). This pass: schedules standing work,
    completes tasks whose budget elapsed, issues off-duty walks after completion,
    and consumes the queue.

    Returns state. Idle gate: no due work AND nobody active -> returns with
    zero mutations (zero spend, mirroring the JS live-return)."""
    now = time.time() if now is None else now
    now_ms = int(now * 1000)
    sim = state.get('sim') or {}
    if sim.get('owner') != 'server':
        return state  # browser still drives; stay inert (revert = set owner back)

    agents = state.get('agents') or {}
    # Credit pause: hold all model-dependent work, keep in-flight tasks alive,
    # and resume the same queue when the probe sees the balance restored.
    try:
        import serve as _serve
        if _serve.credits_pause_active():
            for a in agents.values():
                if not isinstance(a, dict) or not a.get('task'):
                    continue
                task = (state.get('tasks') or {}).get(a['task'])
                if isinstance(task, dict) and task.get('status') == 'working':
                    task['workUntil'] = max(task.get('workUntil') or 0, now + 300)
            return state
    except Exception:
        pass
    _check_schedules(state, now, now_ms)
    # Phase 4: server-owned governance (auto-hire + auto-firing review) runs in
    # the same single read-modify-write, gated on the server-owner + idle rule
    # inside _governance_pass itself.
    _governance_pass(state, now=now, now_ms=now_ms, grid=grid)

    # Phase E3.1: stuck-at-review escalation watchdog, on its own coarse cadence.
    # A gated story whose locked reviewer pair can't advance is re-picked to a
    # REACHABLE pair so the gate always has a viable vote path. Bounded internals
    # (grace + rescue cooldown) live inside _sweep_stuck_gates.
    if now_ms - (state.get('lastStuckGateSweep') or 0) >= STUCK_GATE_CADENCE_MS:
        state['lastStuckGateSweep'] = now_ms
        _sweep_stuck_gates(state, now_ms)

    # Bound the append-only tails of the kv_state blob (completedDeliverables,
    # resolved backlogRequests, answered playerInbox, closed issues, applied
    # growthPlans) on a coarse hourly cadence so the 1s autosave write doesn't
    # grow without bound. Live/open/pending entries are always kept.
    if now_ms - (state.get('lastStatePrune') or 0) >= _STATE_PRUNE_CADENCE_MS:
        state['lastStatePrune'] = now_ms
        _prune_state_blob(state, now_ms)

    # Reclaim a 'walking'/'working' task whose assignee no longer
    # holds it (the orphaned-fix wedge). Runs BEFORE the idle gate so a quiet
    # think tank that simply has a stale in-flight task re-issues it rather than
    # treating the think tank as done. Cheap: no-op unless such a task exists.
    _reclaim_orphaned_walking_tasks(state)

    # SM gap 1: age-based stale in-flight work sweep. A held non-bug card
    # wedged past any legitimate budget (walking forever, or still working long
    # after workUntil) is re-queued once, then routed to the owning scrum master
    # to re-plan. Runs before the idle gate on a coarse cadence (no-op unless a
    # card is genuinely wedged).
    _stale_work_step(state, now, now_ms)

    # W5: daily coaching catch-up sweep. Re-checks agents with an applied
    # coaching plan whose real trailing grade is still below the floor, and
    # re-coaches/escalates (bounded) -- coaching can't be absorbed once and
    # ignored forever. Runs before the idle gate on a coarse cadence (no-op
    # unless someone is still under the floor).
    _coaching_loop_step(state, now_ms)

    # Backlog refinement: the scrum master grooms agent-filed work-requests into
    # real stories at the Command Center. EVENT-DRIVEN (no cadence): convenes
    # whenever a team has pending requests, a free+on-duty facilitator, and no
    # active sprint. Runs ungated like the other ceremonies (the grooming is the
    # point even on a quiet think tank) but no-ops when there's nothing to groom
    # or no facilitator to run it.
    _refinement_step(state, now, now_ms)

    # Sprint staffing: the breakdown ceremony -- a staffable large ask filed by
    # the free authority is carded into stories/spikes by the receiving team's
    # scrum master + workers. Runs ungated like its ceremony siblings (a staffed
    # large ask is the point even on a quiet think tank); no-ops with nothing
    # pending or no scrum master to run it, and defers a team in an active
    # sprint until it's free.
    _breakdown_step(state, now, now_ms)

    # WS-14: sprint retrospectives -- a just-closed sprint's team meets to capture
    # START / STOP / CONTINUE (director excluded) on a later pass. Runs ungated
    # like the Social / refinement, so a closed sprint never strands its retro.
    _retro_step(state, now, now_ms)

    # Sprint failure review: a just-closed sprint's failed cards are root-caused
    # by the owning director and a real bug becomes an on-call card. Runs
    # ungated next to the retro so a quiet tank still closes the loop.
    _failure_review_step(state, now, now_ms)

    # Supervisor brief: while a sprint runs, each director gets a scoped,
    # state-only brief every 15 minutes (own team, budget, process failures,
    # where to look). Mail, not a task: no spend, no queue.
    _supervisor_brief_step(state, now, now_ms)

    # First-week onboarding review: mail the reporting director once when the
    # hire's checklist comes due, so orientation has a measured follow-up.
    _first_week_onboard_review_step(state, now_ms)

    # Cut 2 roadmap: weekly silent recompute of per-room priority from trailing
    # deliverable grades + delivery volume. Cheap (state-only, no ceremony, no
    # Jev spend); runs before the gate so a quiet think tank still keeps its
    # roadmap current for the next grooming.
    _roadmap_step(state, now_ms)

    # Consensus relay: weekly silent update of the one-line 'current direction'
    # the next cycle reads first (auto-co-meta's read -> act -> update -> repeat).
    # Derives from the roadmap just recomputed; runs after it so the relay is
    # always the LATEST consensus, not a stale one.
    _consensus_relay_step(state, now_ms)

    # Weekly rule mining: recurring classified failures become operator rule
    # proposals. Same shape as the roadmap step -- state stamp + file-backed
    # derivation, no ceremony, no Jev spend.
    _rule_mine_step(state, now_ms)

    # Weekly archive distill: fold the archived decision tape into the wiki so
    # the think tank's institutional memory compounds instead of being pruned.
    _archive_distill_step(state, now_ms)

    # Weekly success mining: recurring high-graded deliverables become operator
    # success-lesson proposals (the positive mirror of rule mining -- propagate
    # what worked, not just what failed). Same cadence/shape as _rule_mine_step.
    _success_mine_step(state, now_ms)

    # Cut 3 on-call escalation: advance an in-flight escalation ceremony, or
    # sweep open incident tasks past the restore window and hand the ones no
    # one could restore to the product team's scrum master. Ungated like its
    # ceremony siblings -- an unrecovered outage is the point on an idle think tank.
    _escalation_step(state, now, now_ms)

    # SM-committed `blocked` field: the scrum master flips issue['blocked'] for
    # any queued block-change (requirements-met / stuck-on-player / dependency).
    # Mechanical (no LLM, no meeting) -- all judgment is upstream. One per pass.
    _block_step(state, now, now_ms)

    # Wake-on-mail: route an off-duty agent woken by action-needed mail to the
    # work it references, so she acts then returns offline normally (instead of
    # being parked-idle in this same tick before she can process it).
    _mail_action_step(state, now, now_ms)

    work_queue = state.get('workQueue')
    if not isinstance(work_queue, list):
        work_queue = state['workQueue'] = []
    # Repair stale busy tokens BEFORE the idle gate short-circuits.
    # A `busy` flag is a hold on an IN-FLIGHT ('working') task; if the referenced
    # task already ended (done / needs_review) or is missing, or the agent is
    # busy with no task at all, the hold is stale and -- left alone -- wedges the
    # agent permanently (the sim's read-side invariant in the snapshot builder
    # only HIDES this; it must be repaired, or the roster bleeds idle workers over
    # time). Server restarts mid-flight + task-completion-out-of-band are the
    # usual culprits: an agent's busy persists in the DB while its task's status
    # moved to done independently. Clearing a non-'working' hold is safe: a
    # 'working' task is the only legitimate reason to be busy.
    for aid, a in list(agents.items()):
        if not isinstance(a, dict) or not a.get('busy'):
            continue
        tid = a.get('task')
        tasks = state.get('tasks') or {}
        # W1: a PAIR NAVIGATOR is busy with no task of her own -- she rides
        # along on the driver's (pairTaskId). Clearing her busy as a stale
        # token would yank her out of a live pair session mid-work. Only a
        # true stale hold (busy referencing a gone/non-working task, or busy
        # with no task AND no pair session) is a legitimate repair target.
        nav_tid = a.get('pairTaskId')
        if (nav_tid and not a.get('task')
                and (tasks.get(nav_tid) or {}).get('status') == 'working'):
            continue  # a paired navigator mid-session -- leave the hold alone
        if tid and (tasks.get(tid) or {}).get('status') in ('working',):
            continue  # legitimately mid-task -- leave the hold alone
        a['busy'] = False
        a['task'] = None
        a['inRoom'] = None
    # Park fully-idle on-duty wanderers once the queue has no work
    # to hand them. Nothing queued (or only not-yet-due work) + nobody active is
    # exactly the residue case: a woken-but-never-assigned agent lingers visible
    # with nothing to do, and the player just asked that only scheduled/active
    # agents appear. Runs BEFORE the idle gate so a quiet think tank still re-parking
    # its wanderers converges to zero visible idle sprites (assignment re-wakes
    # on demand, so this cannot deadlock the queue).
    if not work_queue and not _any_agent_active(agents):
        _park_idle_wanderers(state)
        # Nothing queued and nobody's mid-task: idle, don't even scan.
        return state
    if not think_tank_has_work(state, now_ms):
        _park_idle_wanderers(state)
        return state

    # Load geometry once per call for this pass (cheap; the movement tick has
    # its own cached copy via self in SimEngine.tick).
    if grid is None:
        grid, doors = _load_outdoor_geometry()
    if task_id_holder is None:
        task_id_holder = _TASK_ID_HOLDER

    # Completion first (a task that finishes frees a worker the assignment loop
    # below can then use). Arrival is guaranteed because movement set
    # status='working' + workUntil; a 'walking' task just keeps walking.
    # After completion the agent goes off duty in place (send_agent_off_duty).
    for aid, a in list(agents.items()):
        if not isinstance(a, dict):
            continue
        if a.get('task'):
            task = (state.get('tasks') or {}).get(a['task'])
            if not task or task.get('status') != 'working' or not task.get('workUntil'):
                continue
            # Phase 3 slice 2: if real content work is in flight (research),
            # merge its completed result (note + seenUrls) as soon as it lands,
            # finalizing before the timeout; if it already exhausted the budget,
            # fall through to the fallback completion. If a content result
            # arrived, turn OFF the early-no-wait so the note is real.
            result = _take_content_result(task['id']) if task.get('_contentInFlight') else None
            if result is not None:
                # Content landed -- mirror the JS Promise.race: finalize as soon
                # as the real work resolves (the min-visual floor is satisfied by
                # the network round-trips), not when the timeout ceiling hits.
                try:
                    import agents.transport_recovery as transport_recovery
                    transport_outcome = transport_recovery.queue(state, task, result, now_ms)
                except Exception as e:
                    print(f'[sim] transport recovery error: {e}', flush=True)
                    transport_outcome = False
                if transport_outcome:
                    task['note'] = result.get('note')
                    task['_contentInFlight'] = False
                    _release_agent_after_failure(state, aid, task)
                    if transport_outcome == 'queued':
                        import agents.story_tracking as story_tracking
                        story_tracking.root(state, task)['status'] = 'repairing'
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                _apply_content_result(state, task, result, now_ms)
                if result.get('creditsPaused'):
                    # No credits, no work. Release the worker and put the card
                    # back exactly as it was; the pause guard stops the next
                    # assignment until the balance is restored.
                    task['_contentInFlight'] = False
                    _requeue_paused_work(state, task, now_ms)
                    tid = task.get('id')
                    if tid in (state.get('tasks') or {}):
                        del state['tasks'][tid]
                    a['task'] = None
                    a['busy'] = False
                    a['inRoom'] = None
                    a['visible'] = True
                    _log_governance(state, 'admin', 'credits_pause_requeue',
                                    {'task': tid, 'title': (task.get('title') or '')[:120]})
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                # Bot Ops / shadow mode: a dry-run task ships nothing. Capture the
                # outcome to the shadow ledger and release the agent, skipping the
                # peer gate / quality gate / credits entirely.
                if task.get('shadow'):
                    _complete_shadow_task(state, aid, task, now_ms, grid)
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                # Item 4: FAIL-CLOSED budget-exhausted branch. The /api/chat gate
                # (serve.py) refuses the task's model calls once its ledger spend
                # crosses the ceiling and folds that refusal into its content
                # result (budgetExhausted + taskSpendUsd/taskSpendAttempts). Here
                # the card is closed as 'failed' -- NOT sent back / re-fixed: a fix
                # would re-hit the same empty budget and loop forever. The agent is
                # released INLINE (a budget-exhausted card must not bump approvals
                # or ship anything), the owning director is notified once with the
                # diagnostic, and the card waits for her to re-open it with more
                # budget or close the work. Never touches a review/fix subtask's
                # parent -- the gate refuses the task whose budget ran out.
                if result.get('budgetExhausted'):
                    task['budgetExhausted'] = True
                    task['status'] = 'failed'
                    task['failedAt'] = now_ms
                    task['failNote'] = (result.get('note') or task.get('note')
                                        or 'task model-spend budget exhausted').strip()[:300]
                    # Budget overrun intelligence: evaluate what had
                    # landed, classify the cause, and record it in the
                    # durable history that scales future budgets for
                    # similar work (budget.py). Computed once here and
                    # shared with the director notification below.
                    import core.budget as _budget
                    analysis = _budget.analyze_overrun(
                        task, result.get('taskSpendUsd'),
                        result.get('taskSpendAttempts'))
                    completed = _budget.evaluate_completed_work(task)
                    _budget.record_overrun(
                        state, task,
                        spent=result.get('taskSpendUsd'),
                        attempts=result.get('taskSpendAttempts'),
                        analysis=analysis, completed=completed)
                    a['task'] = None
                    a['busy'] = False
                    a['inRoom'] = None
                    a['visible'] = True
                    _notify_task_budget_exhausted(
                        state, task,
                        spent=result.get('taskSpendUsd'),
                        attempts=result.get('taskSpendAttempts'),
                        analysis=analysis, completed=completed)
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                # Fail-closed quality gate: a content result that FAILED the
                # quality pipeline (the coding executor stores ok=False on a red
                # flake8/mypy/bandit/pytest-cov run) must NEVER advance to peer
                # review or re-arm a gate. A review executor always reports
                # ok=True (its verdict rides in peerVerdict), so this check never
                # disturbs a normal gate vote.
                result_ok = bool(result.get('ok', True))
                # W4: worker stuck/help signal. A content result with ok=False
                # (a content-executor crash, or a red pipeline) is a FAILURE --
                # track consecutive failures per worker and route a help signal
                # to the owning team's scrum master once the streak crosses the
                # threshold, so a wedged worker is surfaced, not silently
                # churning. Runs for every landed content result (reviews always
                # report ok=True, so they never trip it).
                _worker_stuck_help_signal(state, aid, task, result_ok, now_ms)
                # Completion evidence is mandatory for ALL content work: a result
                # that reports success but carries NO completion note (no evidence
                # of what was actually done, or where it lives) is not completed.
                # The card goes back to its author as a gap (see
                # _send_back_after_failure) -- never a silent empty 'done'. Review
                # subtasks are exempt (their verdict folds in during
                # _apply_content_result and an empty note there is not a missing
                # deliverable).
                if result_ok and not (task.get('note') or '').strip() \
                        and task.get('_contentInFlight') and not task.get('reviewOf'):
                    _release_agent_after_failure(state, aid, task)
                    _send_back_after_failure(
                        state, task,
                        fail_note='missing completion evidence: the work reported success but produced no note of what was done or where it lives')
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                # Phase E addendum: completion is gated for deliverable rooms.
                # A review/fix subtask (has reviewOf) folds its vote already in
                # _apply_content_result; a primary deliverable task that has NEVER
                # been gated enters the peer gate instead of completing.
                if task.get('reviewOf'):
                    parent = _resolve_review_parent(state, task)
                    if parent is not None and task.get('taskType') == 'review':
                        # Vote worker finished; close the parent if 2 clean votes
                        # (or 1 + timeout) are in.
                        _parent_close_from_vote(state, parent, now_ms)
                    elif parent is not None and _peer_gated_lane(parent) and result_ok:
                        # A fix subtask finished its (re)work and the pipeline is
                        # clean -> re-arm the same gate so the fixed story gets
                        # re-review, not a done. Never re-arms a FROZEN (already
                        # escalated) gate -- an escalated story stays paused for
                        # manual attention, not silently re-entered.
                        if not (parent.get('_peerGate') or {}).get('escalated'):
                            _enter_peer_review(state, parent, now_ms,
                                               force_rearm=True)
                    elif parent is not None and _peer_gated_lane(parent):
                        # Fail-closed: the FIX itself failed the pipeline. Do NOT
                        # re-arm the gate; the story stays 'failed' and another
                        # fix is queued back to the author (or it escalates once
                        # the shared cycle cap is hit). The infra cause rides on
                        # the FIX task (where _apply_content_result stamped it),
                        # NOT the parent -- pass it explicitly or the resend
                        # words itself around a stale parent tag.
                        _release_agent_after_failure(state, aid, task)
                        _send_back_after_failure(state, parent, fail_note=task.get('note'),
                                                 is_infra=bool(task.get('_infraFailure')))
                        send_agent_off_duty(state, aid, doors, grid)
                        continue
                    elif parent is not None:
                        # Parent isn't supposed to be gated (skillReview/distill)
                        # but somehow already has a stale _peerGate -- clear it
                        # instead of perpetuating the loop, and let it close
                        # normally next pass.
                        parent['_peerGate'] = None
                        if parent.get('status') == 'needs_review':
                            parent['status'] = 'working'
                    finish_task(state, aid, grid)
                    send_agent_off_duty(state, aid, doors, grid)
                elif _peer_gated_lane(task) and not task.get('_peerGate') and result_ok:
                    gate = _enter_peer_review(state, task, now_ms)
                    if gate:
                        # Story now waits on peer approval; release the author
                        # but do NOT mark it done.
                        _release_agent_gated(state, aid, grid)
                        send_agent_off_duty(state, aid, doors, grid)
                    else:
                        finish_task(state, aid, grid)
                        send_agent_off_duty(state, aid, doors, grid)
                elif _peer_gated_lane(task) and not task.get('_peerGate'):
                    # Fail-closed: a red-pipeline primary deliverable must NOT
                    # enter the peer gate. Release the agent (no approval/grade)
                    # and send the story back -- or escalate once the shared
                    # cycle cap is hit. (_send_back_after_failure notifies the
                    # author.)
                    _release_agent_after_failure(state, aid, task)
                    _send_back_after_failure(state, task, fail_note=task.get('note'))
                    send_agent_off_duty(state, aid, doors, grid)
                else:
                    finish_task(state, aid, grid)
                    send_agent_off_duty(state, aid, doors, grid)
            elif now >= task['workUntil']:
                # No content result within the budget (timeout OR the slice-1
                # placeholder). BEFORE falling back: if the run is provably
                # still alive (recent heartbeat + grace remaining), extend
                # workUntil and give it another pass -- a slow-but-real
                # synthesis must finish rather than be abandoned empty
                # (task-336). Grace is bounded (see TASK_CONTENT_GRACE_*) and
                # only available to executors that heartbeat; everyone else
                # lands on the fallback exactly as before.
                if task.get('_contentInFlight') and _content_run_is_live(task, now):
                    granted = _grant_content_grace(task, now)
                    if granted:
                        try:
                            from serve import log_action
                            log_action(aid, 'content_grace_extended',
                                       {'taskId': task['id'], 'grantedS': granted,
                                        'graceUsedS': task.get('_contentGraceUsed'),
                                        'workUntil': task.get('workUntil')}, authorized=False)
                        except Exception:
                            pass
                        continue
                # Fallback completion. Same gate branching.
                if task.get('_contentInFlight'):
                    # Last-second recovery: a report can land between the
                    # executor's final heartbeat and this fallback. If the
                    # exact task file is already on disk, attach it and let
                    # the next pass complete the card instead of abandoning
                    # work that actually finished.
                    recovered = _find_recent_content_report(task, now)
                    if recovered:
                        _attach_recovered_report(task, recovered)
                        task['_contentInFlight'] = False
                        task['_contentGraceUsed'] = 0.0
                        task['_contentHeartbeat'] = None
                        task['workUntil'] = now
                        try:
                            from serve import log_action
                            log_action(aid, 'content_run_report_recovered',
                                       {'taskId': task['id'], 'path': recovered},
                                       authorized=False)
                        except Exception:
                            pass
                        continue
                    # An in-flight run reaching the fallback means the thread
                    # died without storing a result (or spent all grace) --
                    # log it so an abandoned investigation is visible instead
                    # of silently reading as a clean 'done'.
                    try:
                        from serve import log_action
                        log_action(aid, 'content_run_abandoned',
                                   {'taskId': task['id'],
                                    'heartbeatAgeS': (round(now - _content_heartbeat_at(task))
                                                      if isinstance(_content_heartbeat_at(task),
                                                                    (int, float)) else None),
                                    'graceUsedS': task.get('_contentGraceUsed')}, authorized=False)
                        _notify_card_failure(
                            state, task,
                            'the content run stopped heartbeating and never produced a result',
                            category='run-abandoned', now_ms=now_ms)
                    except Exception:
                        pass
                # Bot Ops / shadow mode: capture the dry-run outcome to the shadow
                # ledger and release, never entering a gate.
                if task.get('shadow'):
                    _complete_shadow_task(state, aid, task, now_ms, grid)
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                if task.get('reviewOf') or not _peer_gated_lane(task):
                    finish_task(state, aid, grid)
                    send_agent_off_duty(state, aid, doors, grid)
                else:
                    gate = _enter_peer_review(state, task, now_ms)
                    if gate:
                        _release_agent_gated(state, aid, grid)
                        send_agent_off_duty(state, aid, doors, grid)
                    else:
                        finish_task(state, aid, grid)
                        send_agent_off_duty(state, aid, doors, grid)
        # (Off-duty walks that already ARRIVED were finalized by the movement
        # tick's ('arrive','offduty') dispatch; nothing left to do here.)

    # A sprint whose every item is done AND approved closes itself --
    # no need for the player to tap close once the whole sprint has landed.
    _auto_close_completed_sprints(state, now_ms)

    # Sprint staffing expansion: an active sprint below the six-worker cap whose
    # cards are queued while every pool member is busy grows its staff by one
    # (the director's "agents need help" signal, deterministic -- see
    # _sprint_staffing_step). Runs before assignment so the grown pool can take
    # cards this same pass.
    _sprint_staffing_step(state, now_ms)

    # Assignment loop (bounded by roster size, like the JS runTaskCycleBody).
    # Price-aware throttle: compute per-band tier value ratios once per cycle
    # so the pick loop can hold non-urgent work in bands having a bad-value
    # day (today's score-per-dollar well below the band's trailing median).
    try:
        from serve import tier_value_ratios
        _tier_ratios = tier_value_ratios()
    except Exception:
        _tier_ratios = None
    roster = state.get('agentRoster') or []
    roster_size = sum(1 for d in roster if not d.get('isAdmin'))
    attempted = set()
    for _ in range(max(1, roster_size)):
        due_index = pick_next_due_index(work_queue, now_ms, attempted, state, tier_ratios=_tier_ratios)
        if due_index == -1:
            break
        pick = work_queue[due_index]
        attempted.add(id(pick))
        # A peer-approval review/fix subtask is pinned to a specific agent (its
        # reviewer/author) and must reach them even when the generic wake rule
        # (wake only enough agents to keep one awake-idle) would skip the second
        # of a pair of off-duty reviewers. So a reviewOf item is ALWAYS
        # wakeable -- its pinned agent is woken inside _assign_due_item.
        # Sprint staffing: a sprint card is likewise always wakeable -- its pool
        # is a specific set (see _sprint_worker_pool), and off-duty pool members
        # must be woken to staff the sprint even when the generic wake rule
        # would otherwise keep the think tank thin.
        _pinned_review = bool(pick.get('reviewOf') or pick.get('sprintId'))
        # The wake trigger measures CAPABLE supply, not raw supply: idle
        # directors awake must not suppress waking a resting worker pool for
        # a card they are forbidden to take (2026-10-30 operator-card stall).
        awake_supply = _awake_eligible_count(state, pick)
        can_wake_off_duty = (_pinned_review or bool(pick.get('notBefore')) or awake_supply == 0) \
            and can_activate_another(state)
        # A PLAYER card needs no awake agent at all -- the assign step delivers
        # it into the player's inbox. Only the agent-work guard below can stop
        # it, so the wake-guard break must never fire for one.
        preempted_for_scheduled = None
        if not (pick.get('assignedTo') or '') == 'player' \
                and awake_supply == 0 \
                and not (can_wake_off_duty and _any_available_including_off_duty(state)):
            # Interrupt a busy agent so this due item can land. A SCHEDULED item
            # (queue_once notBefore) must still fire at its specified time even
            # when every agent is busy -- typically at the active ceiling, where
            # can_activate_another is False so no off-duty agent can be woken.
            # A ONE-OFF may also interrupt -- but ONLY ordinary/sprint work:
            # skip_time_critical refuses to yank an agent off a scheduled/
            # standing (time-critical) card (see _task_is_time_critical). In
            # both cases the parked task resumes when the interrupting item
            # completes (see _suspend_busy_agent_for_scheduled /
            # send_agent_off_duty).
            skip_time_critical = not bool(pick.get('notBefore'))
            preempted_for_scheduled = _suspend_busy_agent_for_scheduled(
                state, grid, doors, now_ms, skip_time_critical=skip_time_critical)
            if preempted_for_scheduled is None:
                break  # nobody who could take this right now, of any kind
        work_queue.pop(due_index)
        assigned = _assign_due_item(state, pick, can_wake_off_duty, grid, doors, now_ms, task_id_holder)
        if assigned is _ASSIGN_WAIT:
            # A card that must wait for its named agent is not a failed
            # assignment: put it back without incrementing attempts, or the
            # attempt cap would abandon a perfectly valid direct-route card.
            work_queue.append(pick)
            continue
        if not assigned:
            # A preemption that didn't land: put the parked task straight back so
            # an agent is never left floating free with a suspended task hanging.
            if preempted_for_scheduled is not None:
                a2 = (state.get('agents') or {}).get(preempted_for_scheduled)
                if isinstance(a2, dict) and a2.get('_suspendedTask'):
                    _resume_suspended_task(state, preempted_for_scheduled, a2, grid)
            pick['attempts'] = (pick.get('attempts') or 0) + 1
            # A peer-approval review/fix subtask BLOCKS its whole story: never
            # abandon it on the normal attempt cap (which exists to shed stray
            # normal work). It retries until a reviewer is assignable; the
            # needs_review timeout is the safety net if a reviewer never can be.
            # Sprint staffing: a sprint card is STRICTLY staffed -- it waits
            # for a member of its chosen pool to free up and is never shed to
            # the think-tank-wide fallback or abandoned.
            # Human-in-the-loop: a PLAYER card is likewise never abandoned --
            # it waits for the player, not for an agent, and the delivery path
            # has no agent-availability failure to shed it for.
            # The two "waits forever" cases are capped by WORK_ITEM_STALL_CEILING
            # rather than exempt outright: a genuinely wedged review/sprint item
            # must surface as work_item_abandoned (+ the director help signal
            # _on_work_item_abandoned raises) instead of retrying invisibly --
            # the failure mode that hid a 21k-attempt wedge for 38 hours.
            _is_player_card = (pick.get('assignedTo') or '') == 'player'
            _is_requested_work = bool(pick.get('issueKey') or pick.get('directedTo'))
            _strictly_staffed = bool(pick.get('reviewOf') or pick.get('sprintId'))
            _pick_ref = pick.get('teamId') or _task_owner_director(state, pick)
            _cap = (team_policy(state, _pick_ref, 'workItemStallCeiling')
                    if _strictly_staffed
                    else team_policy(state, _pick_ref, 'workItemMaxAttempts'))
            if _is_player_card or _is_requested_work or pick['attempts'] < _cap:
                work_queue.append(pick)
            else:
                from serve import log_action
                log_action(None, 'work_item_abandoned',
                           {'title': pick.get('title'), 'room': pick.get('room'),
                            'attempts': pick['attempts']}, authorized=True)
                _notify_card_failure(
                    state, pick,
                    f'could not be assigned after {pick["attempts"]} attempts',
                    category='abandoned', now_ms=now_ms)
                _on_work_item_abandoned(state, pick, now_ms)
    return state


def _any_agent_active(agents):
    for a in agents.values():
        if a and (a.get('task') or a.get('handoff') or a.get('pairWith') or a.get('busy')):
            return True
    return False
