

def _reclaim_orphaned_walking_tasks(state):
    """A task left in 'walking'/'working' status whose assignee no
    longer holds it (agent.task != task_id, or the assignee vanished) will never
    resolve -- neither _task_cycle's completion loop (iterates AGENTS, so a task
    with no holder is invisible) nor _repair_stalled_walkers (guards on the agent
    holding the task) can see it. Seen live: a review-spawned fix task (task-4)
    stuck 'walking' forever while its assignee was parked idle. Nothing had
    cleared it, so the fix the review loop trusted as pending never landed.

    Reclaim: re-queue the orphan so a fresh assignment re-issues it (same
    reviewOf/assignedTo/incident pins are honored by assignment, which wakes the
    right agent on demand), then drop the dead task dict so it is not double
    counted. Idempotent, cheap, pure on `state` -- no-op when nothing is
    orphaned. Returns count reclaimed."""
    tasks = state.get('tasks')
    if not isinstance(tasks, dict) or not tasks:
        return 0
    agents = state.get('agents') or {}
    work_queue = state.setdefault('workQueue', [])
    if not isinstance(work_queue, list):
        work_queue = state['workQueue'] = []

    reclaimed = 0
    for task_id, task in list(tasks.items()):
        if not isinstance(task, dict) or task.get('status') not in ('walking', 'working'):
            continue
        # Never yank a task whose holder is at the weekly social -- the holder is
        # legitimately mid-event with its `.task` pointer temporarily detached,
        # and _resolve_social will reclaim and extend its budget on restore.
        if task.get('_inSocial'):
            continue
        # A task parked so a due scheduled item could preempt its holder is also
        # legitimately detached -- _resume_suspended_task reclaims it when the
        # scheduled item completes.
        if task.get('_suspendedForScheduled'):
            continue
        # A task is HELD when any agent points its .task at it -- matched by the
        # assignee id when present, but also by scanning, because a task can be
        # genuinely mid-work yet carry no assignedTo (e.g. a legacy/curated
        # fixture omits it). Only a task NOBODY holds is a true orphan.
        holder = agents.get(task.get('assignedTo')) if task.get('assignedTo') else None
        held = holder is not None and holder.get('task') == task_id
        if not held:
            held = any(a.get('task') == task_id for a in agents.values()
                       if isinstance(a, dict))
        if held:
            continue
        # Don't yank a task whose assignee is mid-anything (busy). A busy-hold on
        # a non-working task is a STALE-BUSY case that _task_cycle's own repair
        # clears in the same pass -- treating it as a lingering orphan here would
        # re-queue work whose holder is actively being reconciled. A true orphan
        # is one whose assignee is gone, parked, or free.
        if holder is not None and holder.get('busy'):
            continue
        # Orphaned: no agent is walking/working this. Re-queue it fresh (the
        # whitelist drops nothing assignment needs -- reviewOf/assignedTo pins a
        # re-opened fix to its author, incident to team on-call).
        # Bound the reclaim re-queue: a task that keeps getting
        # orphaned by its assignee must not be re-issued forever. Carry the
        # task's own attempt history forward and shed the item once it hits the
        # same WORK_ITEM_MAX_ATTEMPTS cap the assignment loop enforces, so a
        # genuinely un-workable task ages out instead of churning the queue.
        attempts = int(task.get('attempts') or 0) + 1
        if attempts >= team_policy(state, task.get('teamId') or _task_owner_director(state, task), 'workItemMaxAttempts'):
            from serve import log_action
            try:
                log_action(None, 'work_item_abandoned',
                           {'title': task.get('title'), 'room': task.get('room'),
                            'attempts': attempts, 'reason': 'repeatedly orphaned'}, authorized=True)
            except Exception:
                pass
            _on_work_item_abandoned(state, task, now_ms=int(time.time() * 1000))
            del tasks[task_id]
            reclaimed += 1
            continue
        work_queue.append({
            'title': task.get('title'),
            'room': task.get('room'),
            'instructions': task.get('instructions') or None,
            'pair': bool(task.get('pair')),
            'notBefore': task.get('notBefore') or None,
            'priority': normalize_priority(task.get('priority')),
            'goal': task.get('goal') or None,
            'projectLabel': task.get('projectLabel') or None,
            'research': task.get('research') or None,
            'taskType': task.get('taskType') or 'code',
            'skillReview': bool(task.get('skillReview')),
            # Bug: 'distill' was missing from
            # this whitelist -- the THIRD copy of the exact same gap already
            # fixed in _assign_due_item's `extra` dict and assign_task itself.
            # An orphaned distill-sweep task reclaimed through here lost the
            # flag on re-queue, so its re-assignment produced a fresh task
            # with distill=False, letting it back into the peer-review gate
            # and re-triggering the identical infinite reject/re-fix loop
            # (again as task-236, ~35 duplicate "Distill recent
            # think tank knowledge" queue entries). The comment above claiming
            # "the whitelist drops nothing assignment needs" was the same
            # false assumption both earlier instances of this bug shared.
            'distill': bool(task.get('distill')),
            'budgetMs': task.get('budgetMs') or None,
            'assignedTo': task.get('assignedTo') or None,
            'incident': bool(task.get('incident')),
            'productId': task.get('productId') or None,
            'reviewOf': task.get('reviewOf') or None,
            'reviewAuthorId': task.get('reviewAuthorId') or None,
            'checklist': task.get('checklist') or None,
            'pipelineStep': task.get('pipelineStep') or None,
            'lane': task.get('lane') or None,
            # Player-authored provenance survives the orphan-reclaim round trip
            # (see queue_work / _assign_due_item / assign_task) -- the JEV gate's
            # work-context bypass depends on it, so dropping it here would
            # silently downgrade a player-vetted task to agent-authored.
            'playerAuthored': bool(task.get('playerAuthored')),
            'attempts': attempts,
        })
        # Fault-aware routing memory: this agent's work attempt just failed
        # to complete -- steer the next round-robin pick away from it for a
        # bit (see _agent_failure_score / _assign_due_item).
        record_agent_failure(state, task.get('assignedTo'))
        del tasks[task_id]
        reclaimed += 1
    return reclaimed


class SimEngine:
    """Owns the server-side `sim` section of the kv_state blob.

    Pure-ish: constructed with a state dict, `tick()` mutates and returns it.
    The actual DB round-trip is done by the caller (serve.py loop) so this
    stays unit-testable without a database.
    """

    def __init__(self):
        self._last_tick_wall = 0.0
        self._last_task_wall = 0.0
        self._grid = None
        self._doors = None

    def tick(self, state, now=None):
        if not isinstance(state, dict):
            return state
        now = time.time() if now is None else now
        sim = state.setdefault('sim', {})
        sim['tick'] = int(sim.get('tick', 0)) + 1
        sim['lastTickEpochS'] = now
        sim['owner'] = sim.get('owner', 'client')  # 'server' once Phase 2 owns movement

        # Per-tick identity self-heal (see serve._heal_agent_identity): a
        # boot-only migration wasn't durable against a stale client tab's 5s
        # autosave re-POSTing pre-fix agent records (confirmed via a
        # save_state_to_db stack trace), so this runs every tick, same shape
        # as _reconcile_stranded_agents/_repair_stalled_walkers below.
        import serve
        serve._heal_agent_identity(state)

        # Phase-2 flip: when the server owns movement, this tick DRIVES agent
        # positions (mutating state['agents'] in place via the proven pure
        # port step_agent_movement), not just observes them. The snapshot below
        # then mirrors the already-advanced positions, so /api/sim/agents and
        # the client's render both see post-step truth. Until owner is set to
        # 'server', this stays an observer (client keeps driving) -- the two
        # sides agree on authority by keying off this same flag.
        if sim.get('owner', 'client') == 'server':
            if self._grid is None:
                self._grid, self._doors = _load_outdoor_geometry()
            agents_map = state.get('agents')
            if isinstance(agents_map, dict):
                # SUB-STEP the movement, don't do one 2s step. A single
                # SIM_TICK_S step moves a walker 60*2=120px -- far past a 16px
                # grid cell and bigger than TASK_ARRIVE_DIST, so she overshoots
                # her waypoint and permanently bounces (reproduced live: probe
                # oscillated 600<->718 every tick). The browser moves per-frame
                # (tiny dt) so never hit this; the coarse server tick must chunk
                # the interval into small fixed steps (6px at 0.1s) so arrival
                # and sliding resolve exactly like per-frame movement.
                events = []
                for _ in range(max(1, int(SIM_TICK_S / SIM_SUBSTEP_S))):
                    events += step_agent_movement(SIM_SUBSTEP_S, agents_map,
                                                  self._grid, doors=self._doors)
                # Phase-3 dispatch: arrival/cancel events now feed the task
                # lifecycle (the Phase-2 boundary closed). An agent arriving at
                # a task's door gets marked working (busy/inRoom/workUntil);
                # a cancel (stuck, gave up) releases the agent via _cancel_at_task
                # (or _release_stalled_interaction for a client-owned handoff/
                # pair session) so a walk that can't finish never wedges her.
                # (Off-duty is immediate and in-place now -- see
                # send_agent_off_duty -- so there is no off-duty ARRIVAL event;
                # an idle agent vanishes where she stands.)
                if events:
                    import serve
                    for kind, sub, aid in events:
                        if kind == 'arrive' and sub == 'task':
                            _arrive_at_task(state, aid, now)
                        elif kind == 'arrive' and sub == 'pair':
                            # W1: a pair session's navigator reaching the
                            # driver's door resolves here (navigator joins the
                            # driver's workstation). Previously dropped -- the
                            # navigator stood frozen beside the door forever.
                            _arrive_at_pair(state, aid, now)
                        elif kind == 'arrive' and sub == 'handoff':
                            # W1: a handoff walker reaching her recipient
                            # delivers the handoff and clocks off. Previously
                            # dropped -- the walker froze beside the recipient.
                            _arrive_at_handoff(state, aid, now)
                        elif kind == 'cancel':
                            # Dispatch the cancel instead of dropping it on the
                            # floor. A stuck task-walk that was only logged left
                            # the agent holding task/path/respawn forever: she
                            # re-walked the same unwalkable route and emitted a
                            # cancel every tick (seen live: vela, replanCount
                            # 55,350, one sim_cancel row ~every 1.5s for 36h).
                            # tasks.js calls cancelTask()/cancelHandoff() here;
                            # port that release so a gave-up agent frees her
                            # task and goes idle again. Handoff/pair sessions are
                            # client-owned, so a cancel means that client is
                            # gone -- release her the same way
                            # _repair_stalled_interactions does (park off duty).
                            if sub == 'task':
                                _cancel_at_task(state, aid)
                            elif sub in ('handoff', 'pair'):
                                _release_stalled_interaction(state, aid)
                        serve.log_action(None, 'sim_arrive' if kind == 'arrive'
                                         else 'sim_cancel', {'kind': sub, 'agent': aid},
                                         authorized=True)

                # Phase-3 task lifecycle runs IN the same pass as movement (and
                # the arrival dispatch above), NOT in a separate loop -- so both
                # share ONE read-modify-write of `state`. A second independent
                # loop (separate get_state_from_db) would read a stale snapshot
                # and its save could clobber an arrival this pass just marked,
                # permanently stranding an agent at a door whose event never
                # re-fires. Cadence mirrors the browser's 6s runTaskCycle.
                if now - self._last_task_wall >= TASK_CYCLE_S:
                    self._last_task_wall = now
                    _task_cycle(state, now=now, grid=self._grid, doors=self._doors,
                                task_id_holder=_TASK_ID_HOLDER)

                # Heal agents stranded in unreachable outdoor cells
                # (the reachability-gap bug). Runs AFTER movement + task cycle so
                # it only moves genuinely idle, visible agents -- no-op once
                # everyone is reachable.
                _reconcile_stranded_agents(state, self._grid)
                # Covers the inverse stranding: an agent HOLDING a 'walking' task
                # with no path (path cleared, arrival never fired). Movement skips
                # pathless agents, so without this they freeze mid-task forever.
                _repair_stalled_walkers(state, self._grid, self._doors, now)
                # Covers the class _repair_stalled_walkers explicitly skips: a
                # handoff/pairWith session (client-only lifecycle) stalled
                # with no path and no way to ever resume or resolve on its
                # own. Must run BEFORE the door-squatter eviction below so a
                # released agent is parked off duty (invisible) rather than
                # also getting relocated first.
                _repair_stalled_interactions(state)
        # Server-observed snapshot of every agent's spatial/activity truth.
        # Read-only for the client today; becomes the server's authoritative
        # write target in Phase 2. Fields mirror the client's saveState shape
        # so the snapshot is directly comparable to what the browser holds.
        agents = state.get('agents')
        snapshot = {}
        if isinstance(agents, dict):
            for aid, agent in agents.items():
                if not isinstance(agent, dict):
                    continue
                off_duty = bool(agent.get('offDuty'))
                # Invariant: an off-duty agent is never busy. A stale
                # busy+offDuty ghost (an abandoned firing review / assignment
                # marks busy, then the page reloads before finishTask clears it)
                # otherwise wedges ALL delegation and hiring -- the reload
                # reconcile clears busy client-side, but applyServerPositions
                # snaps busy back to the server's word every 1s poll, and
                # send_off_duty refuses to wake a busy agent, so nothing can
                # ever recover. Reporting busy=False for anyone off duty lets
                # the client persist it back and self-heals the wedge.
                snapshot[aid] = {
                    'x': agent.get('x'),
                    'y': agent.get('y'),
                    'dir': agent.get('dir'),
                    'busy': False if off_duty else bool(agent.get('busy')),
                    'task': agent.get('task'),
                    'inRoom': agent.get('inRoom'),
                    'offDuty': off_duty,
                    'pathActive': bool(agent.get('path')),
                    # This snapshot never carried
                    # `visible` at all, so applyServerPositions (sim_bridge.js)
                    # had nothing to sync it from -- a client's copy is set ONCE
                    # from the full /api/state GET at page load and then frozen
                    # for the rest of that browser session. Any agent who goes
                    # invisible afterward (entering a room, being parked off
                    # duty) keeps rendering as a "ghost" at her last known
                    # outdoor position on every client that was already open --
                    # this, not a stalled walk, is why one kept appearing
                    # outside the observatory door even while legitimately
                    # working inside it.
                    'visible': bool(agent.get('visible', True)),
                }
        sim['agents'] = snapshot
        self._last_tick_wall = now
        return state

    def status(self, state):
        sim = state.get('sim', {}) if isinstance(state, dict) else {}
        return {
            'running': bool(sim),
            'tick': sim.get('tick', 0),
            'lastTickEpochS': sim.get('lastTickEpochS'),
            'owner': sim.get('owner', 'client'),
            'ageS': (time.time() - sim.get('lastTickEpochS', 0)) if sim.get('lastTickEpochS') else None,
        }


# Module-level singleton, like serve.py's other loops.
_engine = SimEngine()


def _sim_loop_pass():
    # Returns the updated state after one tick, or None if nothing to do.
    # Runs on a worker thread (the loop calls us via asyncio.to_thread).
    import serve
    # Cheap housekeeping: drop expired external-capability handles and
    # sublow/JEV experiment grants on each tick so a stale nonce can never be
    # presented. All idempotent; no-op when empty.
    serve._expire_handles()
    serve.expire_sublow_grants()
    serve.expire_jev_grants()
    # Sleep-not-die gate: while DORMANT (idle auto-sleep armed), do NOT run the
    # engine tick. That single tick drives movement, the task cycle, the content
    # executors, and every model/OpenRouter spend -- skipping it is exactly what
    # "paused while sleeping" means (no bill, no churn), while the process stays
    # alive and port-bound so a remote request can wake it. The DB is left
    # untouched, so the checkpoint backup loop / _expire_handles still run.
    if serve._dormant():
        return None
    try:
        state = serve._state_begin()
        if not state:
            return None
        # Record dormancy/downtime as paused time BEFORE the tick advances
        # sim.lastTickEpochS, so stale-work aging excludes it.
        _record_tick_gap(state, int(time.time() * 1000))
        state = _engine.tick(state)
        try:
            governance.supervisor_tick(state)
        except Exception as e:
            print(f'[sim] supervisor tick error: {e}', flush=True)
        # Deliver any finished drained player-ask answers INSIDE this single
        # read-modify-write (same two-phase discipline as _content_results: the
        # model call ran on its own thread and stashed an in-memory result; this
        # pass delivers it durably). No-op when nothing finished.
        try:
            serve._apply_pending_ask_results(state)
        except Exception as e:
            print(f'[sim] ask apply error: {e}', flush=True)
        # Peer reviews run INSIDE the sim's single read-modify-write -- on this
        # same `state` object, right before the one save -- so the report it files
        # can never be clobbered by a concurrent whole-blob save (the race that
        # made freshly-filed reports vanish and the old standalone peer thread
        # re-flag the same worker every 90s forever). Cadence-gated; no-op when not
        # due. serve._peer_review_tick reads only this in-hand state + the
        # action_log (read-only), then mutates state in place.
        try:
            serve._peer_review_tick(state)
        except Exception as e:
            print(f'[sim] peer review tick error: {e}', flush=True)
        # Adversarial (winter) village lifecycle: when both sides have delivered
        # the shared adversarial task, complete + disable (drain the winter team
        # back to the main village). Same in-place mutation inside the single
        # read-modify-write; no-op when disabled.
        try:
            serve._adversarial_village_tick(state)
        except Exception as e:
            print(f'[sim] adversarial village tick error: {e}', flush=True)
        # Consume ONE pending sim-watchdog failure event (R6): stamped on the
        # module side channel by serve's watchdog thread, delivered here
        # INSIDE the single read-modify-write so the mailbox/work-queue writes
        # ride the same save. Clear-on-read: a re-stamp during delivery waits
        # for the next tick; a stamp landing while DORMANT waits for the wake
        # (the dormant early-return above leaves it untouched).
        global _SIM_WATCHDOG_EVENT
        if _SIM_WATCHDOG_EVENT is not None:
            _watchdog_evt = _SIM_WATCHDOG_EVENT
            _SIM_WATCHDOG_EVENT = None
            try:
                _deliver_watchdog_event(state, _watchdog_evt)
            except Exception as e:
                print(f'[sim] watchdog event deliver error: {e}', flush=True)
        # Completion recovery: story lineage, delivery visibility, and bounded
        # automatic repair/investigation. The activation stamp keeps this from
        # sweeping historical failures on the first tick; only stories created
        # after activation are recovered unless recoverHistorical is explicitly
        # enabled.
        try:
            import agents.story_tracking as story_tracking
            import agents.completion_watchdog as completion_watchdog
            now_ms = int(time.time() * 1000)
            control = state.setdefault('completionControl', {})
            if not control.get('pass3ActivatedAt'):
                control.update(enabled=True, pass3ActivatedAt=now_ms,
                               recoverySince=now_ms, recoverHistorical=False,
                               maxConcurrentStories=5, maxRecoveryAttempts=3,
                               retryBaseMs=30000, maxRecoveriesPerTick=2,
                               transportRecoveryAttempts=3)
            story_tracking.refresh(state, serve._spend_ledger_read() or {})
            completion_watchdog.step(state, now_ms, observe_only=False)
        except Exception as e:
            print(f'[sim] completion recovery error: {e}', flush=True)
        serve.save_state_to_db(state)
        # Stamp the watchdog heartbeat only after a fully successful pass
        # (tick + save). A pass that dies mid-way leaves the old stamp so the
        # watchdog can notice repeated failures.
        global _LAST_TICK_WALL_EPOCH
        _LAST_TICK_WALL_EPOCH = time.time()
        return state
    finally:
        serve._state_abort()


async def _sim_loop():
    # 2026-10-13 hardening. The live village froze mid-sprint: the tick
    # stopped, every executor worker sat idle, CPU ~1% (a block, not a spin),
    # and NOTHING was logged. Post-mortem: this asyncio task had DIED
    # silently. Two structural holes made it invisible --
    #   (a) `except Exception` cannot catch BaseException subclasses
    #       (CancelledError, KeyboardInterrupt, or anything else that skips
    #       Exception), so such an escape ended `while True` with no log;
    #   (b) the lifespan holds the only reference to this task and never
    #       awaits it, so Python's "Task exception was never retrieved"
    #       warning never fires -- a fully invisible death.
    # Fix: every await is guarded with BaseException (re-raising
    # CancelledError so shutdown still works), and serve.py's sim watchdog
    # supervises the task object itself (recreate on done / tick-stale).
    while True:
        try:
            await asyncio.sleep(SIM_TICK_S)
        except _SimCancelledError:
            raise
        except BaseException as e:
            # Keep looping; the while re-enters sleep next iteration. Never
            # let a stray BaseException end the task without a trace.
            print(f'[sim] sleep error: {e!r}', flush=True)
            continue
        try:
            await asyncio.to_thread(_sim_loop_pass)
        except _SimCancelledError:
            raise
        except BaseException as e:
            print(f'[sim] loop error: {e!r}', flush=True)
        # Drain any queued player emails OUTSIDE the tick (its own thread) so a
        # slow SMTP round-trip never stalls the think tank tick. Best-effort -- the
        # sender fail-closes and the drain clears the outbox regardless.
        try:
            await asyncio.to_thread(_drain_emails_from_db)
        except _SimCancelledError:
            raise
        except BaseException as e:
            print(f'[sim] email drain error: {e!r}', flush=True)


def _drain_emails_from_db():
    """Load state, drain its emailOutbox via the serve sender, save. Runs on its
    own thread (not the tick) so SMTP latency doesn't block the sim. Reads the
    same authoritative DB row, so it sees whatever the tick last persisted."""
    try:
        import serve
    except Exception:
        return []
    try:
        state = serve._state_begin()
        if not state:
            return []
        results = []
        try:
            results = _drain_email_outbox_sync(state)
        except Exception as e:  # pragma: no cover
            print(f'[sim] email drain failed: {e}')
            return []
        if results:
            try:
                serve.save_state_to_db(state)
            except Exception as e:  # pragma: no cover
                print(f'[sim] email drain save failed: {e}')
        return results
    finally:
        serve._state_abort()


# ---------------------------------------------------------------------------
# Phase 3, slice 1: server-owned task lifecycle -- PLANN-phase3-task-lifecycle.md
#
# The Phase-2 flip (sim.owner='server') made the server the authoritative
# SPATIAL writer, but task ASSIGNMENT stayed browser-driven (runTaskCycle
# timer in index.html -> tasks.js). With no browser open, the persistent
# workQueue sat in the DB unattended: nothing picked items off it, nothing
# assigned agents, nothing completed tasks. This section re-homes the LIFE-
# CYCLE core -- queue consumption, deterministic assignment, arrive, complete,
# off-duty -- into pure Python functions driven by a server loop, so the
# think tank does tasks end-to-end with no browser.
#
# Scope discipline (confirmed with user): the per-room CONTENT executors
# (runWorkroomTask/runResearchTask/runMediaDigestTask/...) are the NEXT slice.
# Here, an arriving agent is marked working and, after a short simulated work
# budget, completed -- a stub, replaced by real per-room execution later.
# Assignment is DETERMINISTIC (round-robin over eligible candidates, zero JEV
# spend), matching the "idle think tank spends nothing" ethos.
#
# These are PURE / mutate-state: they take a `state` dict and return/log into
# it, no DB, no serve import -- so they're unit-testable and run on a thread
# via the serve.py wrapper, exactly like _sim_loop_pass/_peer_review_loop_pass.
# ---------------------------------------------------------------------------

# Mirrors tasks.js -- copied constants, not drift-prone redefinitions.
# WORK_PRIORITY / _WORK_PRIORITY_VALUES now live in sim_helpers.py (imported
# at the top of this module) -- see normalize_priority.
WORK_ITEM_MAX_ATTEMPTS = 3
# A strictly-staffed item (peer-review subtask, sprint card) is exempt from the
# NORMAL cap above -- it blocks its story, so shedding it would be wrong. But
# "retry forever" is how a wedged item silently burned 21k+ attempts across 38
# hours in the 2026-10-13 review with nobody noticing. Player cards still wait
# forever (they wait for a HUMAN; there is no agent-availability failure to
# shed them for). Review/sprint items get a generous stall ceiling instead, so
# a real wedge surfaces as work_item_abandoned + a director help signal.
WORK_ITEM_STALL_CEILING = 600  # ~60 min at TASK_CYCLE_S = 6
# Price-aware admission throttle: when a tier band's score-per-dollar
# today is below TIER_THROTTLE_RATIO of its trailing 7-day median (a
# bad-value day for that band), non-urgent work mapped to that band is
# held from assignment. Bands above the ratio (good-value days) admit
# normally. Exemptions that always bypass the throttle: high/urgent
# priority, incidents, peer reviews, sprints, scheduled one-offs
# (notBefore), and player cards. Neutral (1.0) when history is thin.
TIER_THROTTLE_RATIO = 0.7
# The mirror of the throttle: when a band's score-per-dollar TODAY is well
# ABOVE its trailing 7-day median -- a cheap-value window for that band -- its
# non-urgent backlog is pulled AHEAD of same-priority siblings so the tank
# takes on that work while the window is open, instead of only refusing work on
# bad days. Sits between the lane weight and the size estimate: urgency and
# lane order are untouched (a parked/reading lane still yields), but within a
# lane the good-value band's cards start first. Neutral when history is thin.
GOOD_VALUE_RATIO = 1.3
MAX_ACTIVE_AGENTS = 25  # hiring.js:41
MAX_TOTAL_AGENTS = 500  # hiring.js:40 -- total inventory cap, vs MAX_ACTIVE_AGENTS
# Per-SPRINT staffing cap: at most 6 workers may staff one sprint
# (scrum master excluded). This is NOT a lifetime team-size limit -- a director
# may hire unlimited workers over time; only a single sprint is capped at 6.
# See _sprint_worker_pool / _sprint_staffing_step for enforcement.
MAX_TEAM_MEMBERS = 6
# Scrum-master scaling: a scrum master is a standing
# facilitator a small team doesn't need yet -- only once a team reaches this
# many workers does a dedicated scrum master become REQUIRED. Below it, the
# team's director stands in as the groomer/facilitator for ceremonies.
SCRUM_MASTER_MIN_TEAM_SIZE = 4
# A lean team normally has its director stand in as facilitator. But a small
# team that accumulates stuck cards has outgrown implicit facilitation; at that
# point a real scrum master (or an explicit director-facilitation acknowledgement)
# is required. The `directorFacilitation` flag on the team record is the
# acknowledgement that keeps a lean team exempt while its director owns
# ceremonies.
SMALL_TEAM_STUCK_TRIGGER = 3


def _team_member_count(state, director_id):
    """The number of non-scrum-master members currently on director_id's team,
    derived from the `director` graph. The scrum master (a standing role on the
    team's record) splits the facilitator off from the work count, per the user's
    "no larger than 6, not including the scrum master" cap."""
    members = _sim_direct_reports(state, director_id)
    # A director is not their own team member (matches _derive_team_members).
    team_scm = None
    for t in (state.get('teams') or []):
        if t.get('directorId') == director_id:
            team_scm = t.get('scrumMasterId')
            break
    out = 0
    for m in members:
        if m != team_scm:
            out += 1
    return out


def _team_under_cap(state, director_id, extra=0):
    """True if adding `extra` more members keeps the team at or under the
    MAX_TEAM_MEMBERS cap (scrum master excluded). NOTE: this is now only used
    to check per-SPRINT staffing capacity, NOT to gate hiring -- directors may
    hire unlimited workers; MAX_TEAM_MEMBERS only limits how many staff a
    single sprint (see _sprint_worker_pool)."""
    return _team_member_count(state, director_id) + extra <= team_policy(state, director_id, 'maxTeamMembers')


# ---------------------------------------------------------------------------
# Teamwork agreements: per-team overrides of operator-policy keys. A team
# record may carry an `agreement` dict (written through PUT /api/teams/{id}).
# Resolution order is team -> operator_policy.json -> shipped code default, so
# an unset key (and an empty/missing agreement) behaves exactly like today.
# config.TEAM_AGREEABLE_KEYS is the allowlist; config.TEAM_CEILING_KEYS are
# clamped to min(team, operator) at READ time so the operator's cap stays
# live without rewriting state.
# ---------------------------------------------------------------------------
def team_agreement(state, team_ref):
    """The raw `agreement` dict of the team matching team_ref (a team id OR a
    directorId -- teams are keyed by their director), or {} when nothing
    matches or the stored value is not a dict (fail closed, never trust the
    record's type)."""
    if team_ref is None:
        return {}
    for t in (state.get('teams') or []):
        if not isinstance(t, dict):
            continue
        if t.get('id') == team_ref or t.get('directorId') == team_ref:
            agr = t.get('agreement')
            return agr if isinstance(agr, dict) else {}
    return {}


def agent_team_ref(state, agent_id):
    """The team ref for an agent: their roster `director` pointer (a team is
    identified by its director). None when the agent is unrostered or is a
    director without a director of their own."""
    for d in (state.get('agentRoster') or []):
        if isinstance(d, dict) and d.get('id') == agent_id:
            return d.get('director')
    return None


def team_policy(state, team_ref, key=None):
    """Resolve operator-policy value(s) with the team's agreement applied.

    team_ref: a team id, a directorId, or None (plain operator value, so
    call sites can stay uniform). key: one policy key, or None to get the
    full resolved mapping of every operator-policy key.

    Per key: a team-set value is re-coerced against the schema at read time
    (an invalid/stale stored value falls back to the operator value), then
    ceiling keys are clamped to min(team, operator). Non-agreeable keys
    always resolve to the operator value. Reads config.OPERATOR_POLICY
    (refreshed in place by reload) instead of get_operator_policy() so a
    hot sim loop does not stat the policy file on every call."""
    import core.config as _cfg
    op_policy = _cfg.OPERATOR_POLICY or _cfg.get_operator_policy()
    defaults = _cfg.OPERATOR_POLICY_DEFAULT
    agr = team_agreement(state, team_ref)

    def _resolve(k):
        val = op_policy.get(k, defaults.get(k))
        if k in _cfg.TEAM_AGREEABLE_KEYS and k in agr:
            coerced = _cfg._coerce_operator_value(k, agr.get(k))
            if coerced is not None:
                val = coerced
        if k in _cfg.TEAM_CEILING_KEYS:
            op_val = op_policy.get(k, defaults.get(k))
            try:
                if val > op_val:
                    val = op_val
            except TypeError:
                val = op_val
        return val

    if key is None:
        return {k: _resolve(k) for k in defaults}
    return _resolve(key)


SKILL_REVIEW_CADENCE_MS = 30 * 60 * 1000

# Hive mind: how often the think tank distills recent archive findings into its
# wiki. Same cadence as skill-review -- a standing ceremony that runs only when
# the think tank actually has work (the idle gate), costing ~1 mid-tier call each.
DISTILL_CADENCE_MS = 30 * 60 * 1000

# An EXPLICIT "this ceremony is never due" marker. Cadence stamps historically
# used a far-future TEST sentinel (1e18 == SQLite "year 33658") to silence a
# ceremony from tests; that leaked into live state and the code had to GUESS
# "is this a sentinel or a real date?" by magnitude. That guess misfires on any
# genuinely far-future date. Going forward the ONLY way to say "never due" is
# this named constant; see `_cadence_due`.
CADENCE_NEVER = 9e18

# Legacy far-future TEST-sentinel threshold (1e18 stamps leaked into live
# state). Anything this far beyond now is treated as unset -- by _cadence_due
# (a leaked sentinel must not mute a standing ceremony) and by the distill
# content gate's `since` cutoff (a leaked sentinel must not gate the ceremony
# out either). See _check_schedules.
_CADENCE_LEGACY_FAR_FUTURE_MS = 10 * 365 * 24 * 3600 * 1000


def _cadence_due(state, key, cadence_ms, now_ms=None, legacy_far_future_ms=_CADENCE_LEGACY_FAR_FUTURE_MS):
    """Is a cadence ceremony (keyed by `key`) due? A stamp exactly equal to
    CADENCE_NEVER is INTENTIONALLY silent forever (explicit, not inferred). A
    legacy far-future stamp (the old 1e18 TEST macro that leaked into live
    DBs) is normalized to unset so it can't come back and mute the ceremony --
    the same self-heal guarantee `test_self_heal.py` pins, now expressed as an
    explicit migration instead of a magnitude guess. Everything else is a real
    timestamp evaluated against `cadence_ms`. `now_ms` is the caller's injected
    clock (kept for test determinism); falls back to wall clock when absent."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    last = state.get(key)
    if last is None:
        last = 0
    if last == CADENCE_NEVER:
        return False
    if legacy_far_future_ms is not None and last - now_ms > legacy_far_future_ms:
        last = 0
    return now_ms - last >= cadence_ms

# Phase 4 governance cadence + decision constants (ported from hiring.js /
# firing.js / morale.js -- copied constants, not drift-prone redefinitions).
HIRE_COOLDOWN_MS = 45000      # hiring.js:24
HIRE_DURATION_MS = 8000       # hiring.js:25 (review completion now deferral)
HIRE_COLOR_POOL = ['#f6b26b', '#76a5af', '#a4c2f4', '#d5a6bd', '#b6d7a8', '#ffe599']
FIRING_COOLDOWN_MS = 60000    # firing.js:21
FIRING_REVIEW_DURATION_MS = 10000  # deferral between start + resolution
# Personnel-strike grace window (2026-10-06): a serious/severe negative review
# counts toward the two-strike firing bar only when it lands INSIDE this window
# of the moment the evidence is weighed. A single negative review no longer
# hangs over an employee forever -- two honest mistakes spaced months apart
# never accumulate into a firing. 'minor'/'major' reports (friction, no harm,
# underperformance) never count as strikes at all.
REPORT_STRIKE_WINDOW_MS = 14 * 24 * 3600 * 1000  # 14 days
# Phase B: the staging gap between the ceremony embarking the director + the
# employees who'll work with a new hire at Command Center and the first staged
# AGENT.md draft. Long enough to read as a real Town-Hall pre-meeting, short
# enough to clear under the governance idle gate in one or two passes.
ONBOARD_MEET_DURATION_MS = 10000
# Stage-3 readiness hold: onboarding stays open until the new hire has actually
# claimed a real co-work task (a task HE holds, matching their helpFor overflow).
# An idle/empty think tank (workQueue drained) will never assign one, so the hold is
# capped -- after this much wall-time the hire is finalized anyway rather than
# stranded in limbo. 9x the ceremony; live think tanks observe readiness long before,
# offline/empty ones stop lingering within a bounded, testable window.
ONBOARD_READINESS_TIMEOUT_MS = 15 * 60 * 1000

# Weekly cross-team Knowledge Social: a 30-minute scheduled conversation in the
# Hangout (the designated empty social room behind the Town Hall) where every
# agent that PRODUCED an approved deliverable this week gets to talk about it
# with agents from other teams. Non-eligible agents (no week's work) are never
# pulled in. Attendees return exactly to their prior state when it ends: a worker
# returns to (and keeps) their in-flight task with its budget extended, an
# off-duty agent is woken for the event and returned off-duty, an idle on-duty
# one returns to idle. Cadence is event-to-event: `weekApprovals` resets when the
# event resolves, so "worked during the week" is measured between events.
SOCIAL_CADENCE_MS = 7 * 24 * 3600 * 1000      # weekly (stamped + cadence)
SOCIAL_MEET_MS = 30 * 60 * 1000               # 30-minute conversation
# W2: a Knowledge Social 'adopt' carry-away ("I will actually try/apply this")
# is only a logged tape today -- it never changes next execution. A decision of
# 'adopt' at or above this confidence lands as a coaching note routed to that
# worker's NEXT task (via the growth-plan/_coaching_note_for loop), so the
# adoption actually steers behavior instead of staying a digest line.
SOCIAL_ADOPT_CONFIDENCE = 0.6
# Backlog refinement: a weekly ceremony where the team's scrum master grooms
# the work-requests agents filed into real stories. Cadence is event-to-event
# (stamp advances when a ceremony starts), never a wall-clock ipso. `REFINEMENT
# _MEET_MS` is the convene-to-resolve gap (the scrum master reads the requests
# and the Jev refinement decision resolves what becomes a story).
REFINEMENT_CADENCE_MS = 7 * 24 * 3600 * 1000  # weekly
# Different from the Social: refinement is a DECISION, not a conversation, so
# nobody is held for a fixed slot. The scrum master grooms the team's requests
# in one synchronous pass and everyone is released on the immediately-following
# tick -- a team that finishes early leaves early, never waits out a clock. This
# value is just an upper bound / backward-compat fallback, not a required hold.
REFINEMENT_MEET_MS = 10_000                     # brief decision horizon (never a required wait)
# Cut 2 (three missing processes): deliverable grading + roadmap, the coaching
# loop (growth plan -> next task), and incident runbooks. Graded deliveries and
# runbook entries are written FIRE-AND-FORGET on task completion -- they never
# block the completion path. Coaching/runbook notes are appended to a task's
# instructions at ASSIGNMENT, never at grooming.
DELIVERABLE_GRADE_FLOOR = 5.0      # graded below this -> coaching-worthy
# A deliverable graded at/above this is a SUCCESS worth mining (the positive
# half of the rule-mining ledger -- "agents get smarter" by propagating what
# worked, not just by avoiding what failed). Mirrors serve.SUCCESS_GRADE_FLOOR.
SUCCESS_GRADE_FLOOR = 8.0
ROADMAP_CADENCE_MS = 7 * 24 * 3600 * 1000   # weekly silent priority recompute
# Rule mining: weekly silent pass that turns recurring classified draft
# failures (the review ledger) into operator rule PROPOSALS. Same shape as the
# roadmap recompute -- pure derivation, no ceremony, no Jev spend.
RULE_MINE_CADENCE_MS = 7 * 24 * 3600 * 1000   # weekly
# A runbook's most recent entry, once written for a product/room, gets pulled
# into every FUTURE incident task on that product as a "prior incident said:"
# note, so handlers learn instead of re-discovering.
RUNBOOK_MAX_ENTRIES_PER_PRODUCT = 20
# Cut 3: on-call escalation. When the on-call agent CANNOT restore
# a broken product's work -- an incident is repeatedly unassignable, or a picked
# up bug stays open past the restore window -- the escalation hands the problem
# to the owning team's scrum master, who files a backlog STORY (root cause
# scoped) or a SPIKE (root cause unknown) into the pipeline. Composes with the
# Cut-1 refinement ceremony for story creation.
RESTORE_TIMEOUT_MS = 24 * 3600 * 1000   # open bug older than this -> unrestored
ESCALATION_MEET_MS = 10_000             # brief decision horizon (never a required wait)
ESCALATION_MAX_OPEN = 3                 # cap concurrent escalations per owning team
# Stale in-flight work (SM gap 1): a NON-bug task wedged in 'walking'/'working'
# has no age alarm (bugs have RESTORE_TIMEOUT_MS). A coarse sweep re-plans it:
# re-queue once for a fast re-issue, then route the repeat offender to the owning
# team's scrum master (SM gap 1's "SM re-plan") instead of letting it wedge forever.
STALE_WORK_CADENCE_MS = 20_000          # coarse sweep cadence (like STUCK_GATE)
STALE_WORK_TIMEOUT_MS = 3 * 3600 * 1000  # 'walking' older than this -> never arrived
STALE_WORK_BUDGET_GRACE_S = 900         # 'working' still working this far past workUntil -> wedged
STALE_WORK_MAX_REPLANS = 2              # re-queues per (title,room) before the SM re-plan
# Paused-time accounting. Dormancy (and server downtime) stops the sim, so a
# task's WALL age keeps growing while no work can happen. Aging on wall time
# then reports a card as wedged purely because the tank slept. The sim records
# each tick gap (dormancy, restart, or a long stall) as a pause window, and the
# age-based checks subtract the paused overlap from a task's life. The windows
# are persisted with the state so a restart does not lose the accounting.
SIM_PAUSE_MIN_GAP_MS = 60_000           # tick gaps shorter than this are normal jitter
SIM_PAUSE_WINDOWS_MAX = 64              # keep the tail bounded (state is whole-blob saved)


def _record_tick_gap(state, now_ms):
    """Persist the gap since the previous tick as a paused window, when it is
    long enough to be a dormancy/restart/stall rather than normal tick jitter.
    Called BEFORE the engine tick advances sim.lastTickEpochS, so `prev` is the
    last tick that actually ran. Pure state mutation."""
    sim = state.get('sim') if isinstance(state, dict) else None
    prev = (sim or {}).get('lastTickEpochS')
    try:
        prev_ms = int(float(prev) * 1000)
    except (TypeError, ValueError):
        return
    if prev_ms <= 0 or now_ms - prev_ms < SIM_PAUSE_MIN_GAP_MS:
        return
    windows = state.get('_simPauseWindows')
    if not isinstance(windows, list):
        windows = []
    windows.append([prev_ms, int(now_ms)])
    del windows[:-SIM_PAUSE_WINDOWS_MAX]
    state['_simPauseWindows'] = windows


def _paused_overlap_ms(state, start_ms, end_ms):
    """Total pause time overlapping [start_ms, end_ms]. Windows may overlap
    (a crash before the tick saves can record the same gap again), so clip,
    sort, and merge them before summing."""
    if end_ms <= start_ms or not isinstance(state, dict):
        return 0
    spans = []
    for w in state.get('_simPauseWindows') or []:
        try:
            ws, we = int(w[0]), int(w[1])
        except (TypeError, ValueError, IndexError):
            continue
        lo = max(int(start_ms), ws)
        hi = min(int(end_ms), we)
        if hi > lo:
            spans.append((lo, hi))
    if not spans:
        return 0
    spans.sort()
    total = 0
    cur_lo, cur_hi = spans[0]
    for lo, hi in spans[1:]:
        if lo <= cur_hi:
            cur_hi = max(cur_hi, hi)
        else:
            total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
    total += cur_hi - cur_lo
    return min(total, end_ms - start_ms)


def _task_active_age_ms(state, task, now_ms):
    """A task's age excluding paused time, or None when it has no start stamp.
    Mirrors the sweep/health lookup order for createdAt/openedAt."""
    opened = task.get('openedAt') or task.get('assignedAt') or task.get('createdAt')
    if opened is None:
        return None
    try:
        opened = int(opened)
    except (TypeError, ValueError):
        return None
    age = int(now_ms) - opened - _paused_overlap_ms(state, opened, now_ms)
    return max(0, age)


def _effective_work_deadline_ms(state, task, now_ms):
    """workUntil in epoch ms, extended by any paused time that fell after the
    deadline. A task paused mid-budget keeps its remaining active time."""
    try:
        deadline = int(float(task.get('workUntil') or 0) * 1000)
    except (TypeError, ValueError):
        deadline = 0
    return deadline + _paused_overlap_ms(state, deadline, now_ms)


# Worker stuck/help signal (W4): a worker whose content execution keeps FAILING
# (a content-executor crash, or a red-pipeline ok=False) is genuinely stuck, and
# today has no signal -- a crash is swallowed into a note. Count CONSECUTIVE
# content failures per worker; once the streak crosses this threshold, route a
# help signal to the owning team's scrum master (file_work_request +
# kick_refinement_now, the same SM-routing path as the stale-work sweep) so the
# SM re-plans or helps instead of the worker silently churning. A clean result
# resets the streak. Dovetails with SM gap 1: stale work is a wedged CARD, W4 is
# a wedged WORKER.
WORKER_HELP_AFTER_FAILS = 2
# W1: server-owned pairing/handoff. The client's assignPairTask/attemptHandoff
# own these interactions entirely; server-side, the movement engine emits
# ('arrive','pair'/'handoff') events but the task dispatch dropped them, and a
# `pair` card was assigned SOLO (no navigator ever recruited). Offsets tried
# when walking a navigator/handoff-walker up beside someone, mirrors the
# client's own "try several offsets that clear the walkable strip" loop.
PAIR_WALK_OFFSETS = [10, -10, 6, -6, 0]
# Coaching loop (W5): _write_growth_plan dedups by kind, so a worker who closes
# BELOW the floor repeatedly was coached ONCE and then silently absorbed the
# same note forever -- no re-coach, no escalation. Round-aware re-coaching:
# each low close re-lands a fresh, escalating note (repeat=True) up to
# COACHING_MAX_ROUNDS; past that the problem escalates to the owning director
# (a work-request + governance entry) so a weak worker can't just keep
# absorbing identical coaching -- either the coaching works or it surfaces.
COACHING_MAX_ROUNDS = 3
COACHING_LOOP_CADENCE_MS = 24 * 3600 * 1000  # daily catch-up sweep (like stale work)
