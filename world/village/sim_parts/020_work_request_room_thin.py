# An on-duty agent that just completed a task in a delegatable room files a
# follow-up work-request when the room's queued/in-flight backlog has at most
# this many items left -- "we finished X, and the room is thinning out". Zero
# turns the signal off entirely (opt-in per deployment).
WORK_REQUEST_ROOM_THIN = 1
# Bell-style spare-time lane (2026-10-06): a bounded allowance of FREE SPIKES an
# agent earns per week by finishing real deliverable work. A free spike is
# self-filed, never groomed by refinement, and LOWEST priority (fills gaps,
# never blocks committed work) -- the 'spare time' Bell Labs researchers
# bootlegged. The caps are what keep it from opening the spend flood gates:
# the FIRST real deliverable completion of a week earns one free spike, capped
# per agent AND tank-wide, deterministic (no Jev), and time-boxed.
FREE_SPIKE_ALLOWANCE_PER_WEEK = 1      # free spikes one agent may earn per week
FREE_SPIKE_GLOBAL_CAP_PER_WEEK = 3     # free spikes the whole tank earns per week
FREE_SPIKE_BUDGET_MS = 2 * 60 * 1000   # time-box per free spike (2 minutes)
# Spare time is OPPORTUNISTIC, not a follow-up chore: a free spike becomes
# assignable only after a short deferral, so it fills genuine idle gaps instead
# of instantly re-arming the author the same tick a story reaches review.
FREE_SPIKE_DEFER_MS = 60 * 1000        # 1 sim-minute before a free spike is due
FREE_SPIKE_PREMISE_GUIDANCE = (
    'This is free exploration: your only deliverable is what you actually learn. '
    'QUESTION THE PREMISE first -- state the assumption buried in the question and '
    'ask whether the question is even the right one. The most valuable finding is '
    'often that the frame was wrong. '
    'A finding is not a finding until you can name what would prove it wrong: state '
    'the strongest objection or the concrete observation that would overturn it. If '
    'you cannot, you have not yet separated a real signal from a comfortable assumption.'
)
# Hard cap of filed-but-not-yet-groomed requests in a single ceremony, so a
# churny think tank can't convene a backlog-refinement meeting over a runaway list.
REFINEMENT_MAX_REQUESTS = 20
# Sprint staffing: a staffable large ask is filed as a pending BREAKDOWN request
# for the receiving team's own breakdown ceremony (scrum master + workers) to
# card into stories/spikes on the next pass -- the free authority picks the team
# from the ask alone, never predicting subtasks up front. `BREAKDOWN_MEET_MS` is
# the convene-to-resolve gap (brief decision horizon, never a required wait, like
# refinement); `BREAKDOWN_MAX_REQUESTS` caps how many pending large asks one
# ceremony cards at once.
BREAKDOWN_MEET_MS = 10_000
BREAKDOWN_MAX_REQUESTS = 3
# WS-14 (shared backlog + sprint retrospectives): when every team is committed
# to an active sprint, extra large asks are broken down into stories/spikes in a
# SHARED unassigned backlog under a FEATURE; a team pulls the first eligible item
# (first-come) when a sprint closes, runs a START/STOP/CONTINUE retrospective
# (scrum master + team, director excluded), and refines its backlog at close.
RETRO_MEET_MS = 10_000           # brief decision horizon (never a required wait)
MAX_BACKLOG_ITEMS = 200          # hard cap on shared unassigned stories
BACKLOG_PULLS_PER_CLOSE = 1      # items a closing sprint pulls per team
# SM-committed `blocked` FIELD on issues. The scrum master is the
# single authority who flips issue['blocked']; the judgment ALWAYS happened
# upstream (supervisor/director Jev-pass for a set, or a deterministic
# player-response / self-resolve for an unset), so the SM's commit is MECHANICAL --
# no LLM, no meeting room, just an authoritative field flip + log. One commit per
# server pass (shared-Command-Center convention), gated so a team's SM commits its
# own team's cards and is briefly stamped as the committer.
BLOCK_CHANGE_CADENCE_MS = 5_000          # min gap between SM commits (not a wait)
# Blocked-claims awaiting a supervisor verdict (requirements-met path) are judged
# by the owning director only after this window so the gate isn't hair-trigger.
BLOCK_CLAIM_GATE_MS = 5_000
# Rooms a work-request may target = the delegatable room set (mirrors serve.py's
# _DELEGATABLE_ROOMS so sim.py stays decoupled; keep in sync if the set changes).
VALUED_QUEUE_ROOMS = frozenset(
    {'observatory', 'pressoffice', 'postoffice', 'bank', 'weatherstation', 'library', 'media'})

# Attention lanes (the spine's rule 1: lanes, not sequences). A queue item may
# ride a lane so threads coexist instead of one queue pulling attention away
# from everything else. `parking-lot` is special: the card waits with a
# bookmark and is NEVER auto-assigned -- nothing gets dropped, it just holds
# until someone promotes it. Build > open > reading at equal priority.
LANES = frozenset({'build', 'reading', 'open', 'parking-lot'})
LANE_WEIGHT = {'build': 3, 'open': 2, 'reading': 1}


def normalize_lane(lane):
    """Canonical lane or None (unknown lanes are not a scheduling signal)."""
    if not lane:
        return None
    return lane if lane in LANES else None


def _is_parked(item):
    """A parking-lot card waits with a bookmark -- never auto-assigned."""
    return (item.get('lane') or '') == 'parking-lot'


# Room keywords the charter alignment keys on (the spine's rule 2: point every
# lane at one direction). Each room's craft vocabulary -- if the charter names
# a room's keywords, that room is aligned with the spine.
_ROOM_KEYWORDS = {
    'observatory': ['research', 'science', 'study', 'explore', 'observatory'],
    'pressoffice': ['write', 'writing', 'publish', 'publishing', 'news', 'press', 'article', 'report'],
    'postoffice': ['mail', 'email', 'comms', 'communication', 'outreach', 'post'],
    'bank': ['finance', 'money', 'budget', 'bank', 'treasury', 'fund'],
    'weatherstation': ['forecast', 'weather', 'warning', 'trend', 'signal', 'predict'],
    'library': ['learn', 'learning', 'knowledge', 'book', 'library', 'note', 'teach'],
    'media': ['media', 'video', 'image', 'design', 'creative', 'audio', 'art'],
}
MORALE_APPROVED_WEIGHT = 0.5  # morale.js:16
MORALE_APPROVED_CAP = 15
MORALE_DROPPED_WEIGHT = 6
MORALE_NEGLECT_WEIGHT = 3
MORALE_NEGLECT_CAP = 30
MORALE_DROPPED_DECAY_DAYS = 14  # morale.js:33


# How long an arriving agent "works" before completing, when the server owns
# the lifecycle but the real per-room executor hasn't been ported yet (the
# accepted short-circuit of this slice). Room content execution is next.
# Bound via operator policy 'taskWorkDurationS' (default 8.0).

# Phase 3 slice 2 (research executor): a dispatched content run may take longer
# than the 8s work-budget placeholder -- the JS fires the real async work and
# races it against TASK_DISPATCH_TIMEOUT_MS (the minimum visual floor never
# gates completion, only the work or that ceiling does). Mirror that: an agent
# marked working with a _content_epoch runs up to this budget before the
# completion loop gives up on the content and finalizes the fallback.
# Bound via operator policy 'taskContentTimeoutS' (default 120.0).

# Policy-first knobs: operator_policy.json holds the value and bind_policy
# rebinds the module scalar. A code default still exists so the module has a
# defined value before that first rebind and static analysis sees the binding.
TASK_WORK_DURATION_S = 8.0
TASK_CONTENT_TIMEOUT_S = 120.0

# A content task may carry its OWN work budget (queue_spike's budgetMs is
# documented as "a hard budgetMs for the work cycle"), but _dispatch_content_work
# used to flatten every task to the generic 120s regardless -- so a 5-minute
# assembly spike kept getting cut mid-synthesis and closed with an empty report
# (task-331: reads finished, generation sliced at 120s, no note, no file).
# Dispatch now honors a LARGER per-task budgetMs, capped here so a typo'd or
# hostile budget cannot pin an agent for hours. Executors may still widen
# further from inside (the review/spike +300 pattern) -- this is the floor.
TASK_CONTENT_TIMEOUT_MAX_S = 600.0

# Content-run grace (operator-approved 2026-10-13): the single fixed
# workUntil is consumed by the READ/execute phase, so a long synthesis that
# starts near the ceiling overruns it -- the completion loop then
# force-finishes the task EMPTY and the result the executor stores a moment
# later is orphaned (confirmed live: task-336 workUntil 01:25:09, completed
# 01:25:12, no note, no file, reads had finished 26s earlier). The timeout
# branch now grants bounded grace WHILE the run is provably alive instead of
# abandoning it. Liveness = a recent heartbeat the executor bumps at every
# meaningful step (phase boundaries + every tool call in content.py). Bounds,
# so a genuinely dead/hung thread still falls through to the honest fallback:
#   * per-pass extension step (the loop re-checks every TASK_CYCLE_S);
#   * a TOTAL grace cap per run (never extends past this much past workUntil);
#   * a heartbeat staleness gate (no bump within this window = thread died
#     without storing a result -- same shape as STALE_WORK_BUDGET_GRACE_S).
# A task with no heartbeat field (an executor that never opted in) keeps the
# exact pre-existing behavior: timeout at workUntil, fallback completion.
TASK_CONTENT_GRACE_STEP_S = 60.0
TASK_CONTENT_GRACE_MAX_S = 300.0
TASK_CONTENT_HB_STALE_S = 240.0

# In-process heartbeat fallback. The content thread mutates the task dict it
# was dispatched with; a concurrent whole-blob save from a stale snapshot can
# overwrite that field, which used to make a live synthesis look dead and get
# abandoned empty. The registry is process-local, keyed by task id, and is
# consulted alongside the task field so a save race cannot erase liveness.
_CONTENT_HEARTBEATS = {}


def _bump_content_heartbeat(task_id):
    if not task_id:
        return
    now = time.time()
    _CONTENT_HEARTBEATS[str(task_id)] = now
    if len(_CONTENT_HEARTBEATS) > 500:
        cutoff = now - 3600
        for key, ts in list(_CONTENT_HEARTBEATS.items()):
            if ts < cutoff:
                _CONTENT_HEARTBEATS.pop(key, None)


class _ContentHeartbeatKeepalive:
    """Bump the content heartbeat from a daemon thread for the duration of one
    long synchronous call. A single synthesis completion (no tool calls to bump
    on) can run past TASK_CONTENT_HB_STALE_S, which made a provably live run
    look dead and get abandoned empty -- the real 2026-10-25 task-278 incident:
    the report was already saved, then synthesis ran 306s with no heartbeat and
    the card was closed as abandoned."""

    def __init__(self, task_id, interval_s=30.0):
        self.task_id = task_id
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if self.task_id:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            _bump_content_heartbeat(self.task_id)

    def __exit__(self, *exc):
        self._stop.set()
        _bump_content_heartbeat(self.task_id)
        return False


def content_heartbeat_keepalive(task_id, interval_s=30.0):
    return _ContentHeartbeatKeepalive(task_id, interval_s)


def _content_heartbeat_at(task):
    hb = task.get('_contentHeartbeat')
    task_id = task.get('id')
    reg = _CONTENT_HEARTBEATS.get(str(task_id)) if task_id else None
    if isinstance(reg, (int, float)) and (not isinstance(hb, (int, float)) or reg > hb):
        return reg
    return hb


def _content_run_is_live(task, now):
    """True when a still-in-flight content run has heartbeat evidence it is
    alive and grace remains. A missing heartbeat (executor never opted in)
    is NOT live -- that preserves the pre-existing timeout semantics for
    every other lane."""
    hb = _content_heartbeat_at(task)
    if not isinstance(hb, (int, float)):
        return False
    if (task.get('_contentGraceUsed') or 0) >= TASK_CONTENT_GRACE_MAX_S:
        return False
    return (now - hb) < TASK_CONTENT_HB_STALE_S


def _grant_content_grace(task, now):
    """Extend workUntil by up to TASK_CONTENT_GRACE_STEP_S, bounded by the
    total TASK_CONTENT_GRACE_MAX_S. Returns seconds granted (0.0 when the cap
    is spent)."""
    remaining = TASK_CONTENT_GRACE_MAX_S - (task.get('_contentGraceUsed') or 0)
    if remaining <= 0:
        return 0.0
    step = min(TASK_CONTENT_GRACE_STEP_S, remaining)
    task['workUntil'] = (task.get('workUntil') or now) + step
    task['_contentGraceUsed'] = (task.get('_contentGraceUsed') or 0) + step
    return step

# Server task-loop cadence -- mirrors the browser's runTaskCycle setInterval
# (index.html, 6s). Distinct from the movement tick (SIM_TICK_S=2).
TASK_CYCLE_S = 6.0

# Mailbox retention: mail items live in kv_state, so an unbounded
# mailbox makes every save rewrite a bigger blob forever. Keep the most recent
# entries per agent; anything older is dropped. Generous enough that a long
# player 1:1 thread and pending review requests survive, tight enough that the
# state blob can't grow without bound.
MAILBOX_KEEP_COUNT = 200
# Trimmed entries are not silently destroyed: keep a small recoverable tail so
# an operator can see a notice that fell out of the live mailbox window.
MAILBOX_ARCHIVE_KEEP_COUNT = 50

# ---------------------------------------------------------------------------
# Item 4: per-task model-spend ceiling. A card gets a hard budgetUsd at
# ASSIGNMENT (stamped onto the durable task). The /api/chat gate (serve.py)
# REFUSES further model calls for the task once its ledger spend crosses the
# budget; the sim loop sees the refusal (result['budgetExhausted']) and fails
# the card closed + notifies the owning director, who can re-open with more
# budget. The budget is set per story/spike during backlog refinement (the
# grooming choice also picks a band) or defaults by task type.
# ---------------------------------------------------------------------------

# USD ceiling per task TYPE (a card of a type with no entry uses the default).
# Coding-class work is the expensive lane; research/observation and media are
# cheap crawls; distill/skillReview are single-call ceremonies. Kept small
# because this is a per-card ceiling, not a daily budget.
_TASK_BUDGET_USD = {
    'code': 0.50,
    '3d': 0.50,
    '3d-modeling': 0.50,
    'bug': 0.50,
    'review': 0.40,
    'qa': 0.40,
    'spike': 0.30,
    'research': 0.15,
    'report': 0.25,
    'media': 0.15,
    'distill': 0.10,
    'skillReview': 0.10,
    # A MEETING is a single minutes-writing completion call (no tool loop,
    # no plan/synthesize bookends -- see _run_meeting_content), so it shares
    # the cheapest ceremony tier with distill/skillReview.
    'meeting': 0.10,
}
_TASK_BUDGET_USD_DEFAULT = 0.25

# Refinement bands: the groomer's choice scales the type-default budget (a
# HIGH-VALUE story accepted as 'accept_generous' gets twice the standard
# ceiling; 'standard' is 1x). An explicit per-card `budgetUsd` always wins.
_REFINEMENT_BAND_MULTIPLIER = {'standard': 1.0, 'generous': 2.0}


def budget_usd_for_task(task_type, budget_usd=None, budget_band=None,
                        overrun_factor=None):
    """The model-spend ceiling for a task, in USD. Explicit positive
    `budget_usd` wins outright (a player/director can always name a
    number); otherwise the type tier applies, and the refinement
    `budget_band` scales ONLY that tier (a generous-band story gets 2x
    the type default -- the band is a judgment about worth, never a
    reason to shrink an explicit budget). An optional `overrun_factor`
    (budget.overrun_factor, from this room/team/product's overrun
    history) scales the type-derived ceiling too -- the mechanism that
    lets past overruns raise future budgets for similar work; it never
    applies to an explicit budget. Returns a non-negative float; a card
    with no ceiling at all is 0.0 (gate off)."""
    if isinstance(budget_usd, (int, float)) and budget_usd > 0:
        return float(budget_usd)
    tier = _TASK_BUDGET_USD.get((task_type or '').strip().lower())
    base = _TASK_BUDGET_USD_DEFAULT if tier is None else tier
    mult = _REFINEMENT_BAND_MULTIPLIER.get((budget_band or 'standard').strip().lower(), 1.0)
    factor = (float(overrun_factor)
              if isinstance(overrun_factor, (int, float)) and overrun_factor > 0
              else 1.0)
    return max(0.0, base * mult * factor)


def _overrun_factor_for(state, room=None, scope=None):
    """budget.overrun_factor for an assignment's room/team/product/type
    scope, guarded so an analysis hiccup never blocks the assignment
    itself. `scope` is the queue-work extra dict (or a backlog req)
    carrying teamId/productId/taskType."""
    scope = scope or {}
    try:
        import core.budget as _budget
        return _budget.overrun_factor(
            state, room=room,
            team_id=scope.get('teamId'),
            product_id=scope.get('productId'),
            task_type=scope.get('taskType'))
    except Exception:
        return 1.0

# Server in-memory task-id counter (mirrors tasks.js nextTaskId). Not persisted
# -- on restart it resets. state['tasks'] IS durable, so a bare counter would
# re-mint task-1..task-N over live stories that survived the restart (seen live:
# a restart re-assigned task-9..task-20 and clobbered 10 peer-gated wrq-8
# parents). _next_task_id skips any id already held.
_TASK_ID_HOLDER = [0]


def _next_task_id(state, task_id_holder):
    """Mint the next task-N id, skipping any id already in state['tasks'].
    The counter is in-memory and resets on restart; the task dict is durable,
    so collision-skipping is mandatory, not cosmetic."""
    tasks = state.get('tasks') or {}
    while True:
        task_id_holder[0] += 1
        task_id = 'task-' + str(task_id_holder[0])
        if task_id not in tasks:
            return task_id

# Phase 3 slice 2: completed content-executor results, keyed by task id. A real
# per-room executor (research for now) runs its NETWORK work on a background
# thread -- it must never write the DB directly (a second writer would clobber
# the movement/arrival pass's in-flight state, the race slice 1's single-pass
# design exists to prevent). Instead it stashes its result here
# ({'note': str, 'seenUrls': [...]}) and the NEXT _task_cycle pass, which owns
# the single read-modify-write, merges it into the durable state and finalizes.
# In-memory like _TASK_ID_HOLDER: a restart mid-content abandons in-flight work
# (same as the browser's in-memory TASKS on page reload).
_content_results = {}
_content_results_lock = threading.Lock()


def _store_content_result(task_id, result):
    from core.style import sanitize_text
    stored = dict(result)
    if isinstance(stored.get('note'), str):
        stored['note'] = sanitize_text(stored['note'])
    with _content_results_lock:
        _content_results[task_id] = stored


# Bounded review-cycle escalation: a promoted
# follow-up story cycled through review->fix->review 45+ times in
# under 20 minutes before settling on its own, with no bound at all. Two
# SEPARATE mechanisms were each re-entering the gate with no shared cap: a
# genuine 'actionable' rejection (_apply_content_result below) and
# _sweep_stuck_gates's own "review vanished with no verdict" rescue -- both
# re-queue another review/fix round indefinitely. One shared counter on the
# gate, checked from both places, bounds the total regardless of WHICH
# mechanism keeps re-triggering it.
MAX_REVIEW_CYCLES = 8


def _maybe_escalate_stuck_gate(state, parent, gate, reason):
    """Bump the gate's cycle counter; once it crosses MAX_REVIEW_CYCLES,
    auto-close the story (ship what exists) instead of looping or paging the
    player. Returns True if the gate is (now, or already) finished -- the
    caller must skip its normal re-queue action in that case.
    Idempotent: only finishes once per gate."""
    if gate.get('escalated'):
        return True
    gate['cycleCount'] = gate.get('cycleCount', 0) + 1
    if gate['cycleCount'] < team_policy(state, parent.get('teamId') or _task_owner_director(state, parent), 'maxReviewCycles'):
        return False
    gate['escalated'] = True
    title = parent.get('title') or 'a story'
    try:
        from serve import log_action
        log_action(None, 'task_auto_closed',
                   {'taskId': parent.get('id'), 'title': title,
                    'cycles': gate['cycleCount'], 'reason': reason},
                   authorized=True)
    except Exception:
        pass
    _notify_card_failure(
        state, parent,
        f'closed after {gate["cycleCount"]} review cycles: {reason}',
        category='auto-closed', now_ms=int(time.time() * 1000))
    _close_gated_story(state, parent)
    return True


def _resolve_worker_issue_team(state, wish, task):
    """W3: the owning team RECORD (matched by its `id`) for a spike-filed issue
    wish, or None. `file_issue` requires the team `id` and _team_row only ever
    matches that key, but teams are keyed two ways in the codebase (by `id` and
    by `directorId` -- see _refinement_scrum_master_for_team). Candidate chain:
    the wish's teamId -> the task's teamId -> the task's productId (via
    _product_director) -> the worker's own roster `director` pointer (serve.py
    sets it at roster build; _sim_direct_reports reads it). Each candidate is
    matched against the team list by id OR directorId. Returns the team dict
    (its `id` is what file_issue wants), or None."""
    candidates = []
    if wish.get('teamId'):
        candidates.append(wish['teamId'])
    if task.get('teamId'):
        candidates.append(task['teamId'])
    if task.get('productId'):
        director = _product_director(state, task.get('productId'))
        if director:
            candidates.append(director)
    roster = next((d for d in (state.get('agentRoster') or [])
                   if d.get('id') == task.get('assignedTo')), None)
    if roster and roster.get('director'):
        candidates.append(roster['director'])
    for cand in candidates:
        if not cand:  # pragma: no cover -- all candidates are pre-filtered truthy
            continue
        team = _team_row(state, cand)
        if team:
            return team
        team = next((t for t in (state.get('teams') or [])
                     if t.get('directorId') == cand), None)
        if team:
            return team
    return None


def _file_spike_issue(state, wish, task, now_ms):
    """W3: file the issue a spike worker proposed, against the LIVE state inside
    the tick's single read-modify-write. The executor thread only ever reported
    a WISH (it runs against a snapshot); THIS is where the real filing happens.
    Resolves the owning team from the wish (or the task), dedups against an
    already-open issue by the same reporter + same summary, files via the same
    file_issue the player/telegram endpoints use (which also appends the
    backlogRequests record + kicks refinement now), and logs governance. Fail
    safe to no-filing on any inconsistency (unknown team, malformed wish,
    duplicate) -- a worker's stray idea must never wedge the backlog."""
    reporter = task.get('assignedTo')
    issue_type = (wish.get('issueType') or '').strip().lower()
    summary = (wish.get('summary') or '').strip()
    feature = (wish.get('feature') or '').strip()
    if not reporter or issue_type not in ISSUE_TYPES or not summary or not feature:
        return False
    team = _resolve_worker_issue_team(state, wish, task)
    if not team:
        return False
    team_id = team.get('id')
    # Dedup: don't re-file the same gap every time its spike re-runs. An issue
    # by the same reporter with the same summary that has NOT reached a
    # terminal state is the same finding.
    for issue in (state.get('issues') or {}).values():
        if (issue.get('reporterId') == reporter
                and issue.get('status') not in ('done', 'closed')
                and (issue.get('summary') or '') == summary):
            return False
    issue = file_issue(state, team_id, issue_type, summary, feature, reporter,
                       description=wish.get('description') or '',
                       title=wish.get('title') or None,
                       now_ms=now_ms)
    if not issue:
        return False
    _log_governance(state, reporter, 'worker_filed_issue',
                    {'issue': issue.get('key'), 'team': team_id,
                     'type': issue_type, 'summary': summary[:120],
                     'fromTask': task.get('id')})
    return True


def _peer_coding_class(parent):
    """Item 7: is this card a CODING-CLASS parent -- the lane whose done gate
    needs objective evidence that the quality pipeline really ran on the code?
    A pressoffice story (the workroom delivers code there) or any product-backed
    card (has a productId) counts. Pure-research lanes (observatory, etc.) are
    NOT coding-class: their clean vote rests on pipelineOk alone (they don't run
    the flake8/mypy/bandit/pytest-cov stack the way a code review does)."""
    if not isinstance(parent, dict):
        return False
    return parent.get('room') == 'pressoffice' or bool(parent.get('productId'))


def _apply_content_result(state, task, result, now_ms=None):
    """Merge a completed content-executor result into the durable state, inside
    the task_cycle's single read-modify-write. Writes the research topic's
    grown seenUrls back (so a future run dedups against it) and records the
    agent's real note (mirrors runResearchTask's notes.push). Mutates `state`;
    the note is attached to the task record so the board/UI can surface it.
    `now_ms` is threaded from the caller (the tick already has it); a caller
    that omits it falls back to wall-clock so the function stays standalone."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    note = result.get('note')
    if note:
        task['note'] = note
    # Review diagnostics and workspace handoff survive the result channel.
    for field in ('reviewDiagnostics', 'reviewFailureKind', 'reviewContract',
                  'stagedWorkspace', 'previousWorkspace', 'resumedFromWorkspace',
                  'sourceUrls'):
        if result.get(field) is not None:
            task[field] = result[field]
    if result.get('reviewContract') and task.get('reviewOf'):
        parent_contract_task = (state.get('tasks') or {}).get(task['reviewOf'])
        if parent_contract_task is not None and not parent_contract_task.get('reviewContract'):
            parent_contract_task['reviewContract'] = result['reviewContract']
    if result.get('sourceUrls') and task.get('reviewOf'):
        parent_sources = (state.get('tasks') or {}).get(task['reviewOf'])
        if parent_sources is not None:
            parent_sources['sourceUrls'] = sorted(set(
                parent_sources.get('sourceUrls') or []) | set(result['sourceUrls']))
    # Persist the .py files this content pass wrote (coding executor's
    # `writtenPy`). A gate review recovers the parent story's list from here
    # to scope ITS quality pipeline to the story's own files instead of
    # whole-tree linting the shared sandbox. When this task is itself a
    # fix/QA pass (has reviewOf), merge the new filenames onto the parent
    # story too, so the story's scope tracks the latest fix (new files a fix
    # adds get linted by the next review; same-name overwrites are a no-op).
    written_py = result.get('writtenPy')
    if written_py:
        task['writtenPy'] = sorted(set(task.get('writtenPy') or []) | set(written_py))
        parent = (state.get('tasks') or {}).get(task.get('reviewOf') or '')
        if parent is not None:
            parent['writtenPy'] = sorted(set(parent.get('writtenPy') or []) | set(written_py))
    # Infrastructure-failure tag: a model-call/API failure (not the agent's
    # quality) rides the content result channel so _send_back_after_failure
    # can distinguish it -- no coaching note, no "failed the quality
    # pipeline" wording, no grade penalty (see leo's round-3 review).
    if result.get('infra'):
        task['_infraFailure'] = True
    # Gap: a spike's FULL findings only ever
    # lived in the Library file -- task.note is deliberately kept lean (a
    # short pointer, "see the Library entry just filed"), by design (see the
    # data-minimization comment in content.py's spike executor). But
    # /api/intent/spike/{id}/promote (turning a spike into a real followup
    # story) only ever had task.note to work with, so promoting a spike
    # handed the new story's author a vague pointer, never the actual
    # research (source lists, CSVs, feasibility data). Recording the exact
    # path here (not a task-id glob -- task ids get REUSED across unrelated
    # investigations, confirmed, so a glob could match the wrong
    # finding) lets promotion pull the real content forward.
    library_path = result.get('libraryPath')
    if library_path:
        task['libraryPath'] = library_path
    # Pass 2 repeat-failure control: two failures with the same normalized
    # failure, evidence and repair plan are not a new attempt. Block the story
    # for a real change in input or plan instead of spending on another copy.
    if result.get('ok') is False or result.get('peerVerdict') == 'actionable':
        try:
            import agents.repeat_work as repeat_work
            repeat_work.record(state, task, result, now_ms)
        except Exception as e:
            print(f'[sim] repeat work record error: {e}', flush=True)
    # Merge grown seenUrls back into the topic (dedup is stored per-topic).
    seen = result.get('seenUrls')
    if seen:
        for t in (state.get('researchTopics') or []):
            if t.get('id') == (task.get('research') or {}).get('topicId'):
                existing = t.setdefault('seenUrls', [])
                for u in seen:
                    if u not in existing:
                        existing.append(u)
                break
    # Mirror runResearchTask's notes.push onto the agent's profile.
    aid = task.get('assignedTo')
    if aid and note:
        a = (state.get('agents') or {}).get(aid)
        if a and isinstance(a.get('profile'), dict):
            notes = a['profile'].setdefault('notes', [])
            notes.append(note)
            if len(notes) > 5:
                del notes[:-5]
    # Item 2: feedback injection. A content executor reports `feedback` when its
    # run hit a block or failure (a blocked command, a red pipeline -- see
    # content.py's coding executor); it rides the result channel into the tick's
    # single read-modify-write here, where `state` is live, so it lands in the
    # author's feedback buffer (_build_agent_perception renders it on the next
    # dispatch). Never written from the executor thread itself, which only has a
    # snapshot.
    fb = result.get('feedback')
    if fb and aid:
        if isinstance(fb, str):
            _append_feedback(state, aid, fb, source='executor')
        elif isinstance(fb, dict):
            _append_feedback(state, aid, fb.get('text') or '', source=fb.get('source') or 'executor')
    # Phase 3 pressoffice: a review/QA pass that found real problems enqueues a
    # follow-up fix task (mirrors runReviewTask's queueWork on an 'actionable'
    # Jev verdict). Must go through the tick's single read-modify-write, not a
    # direct DB write, so queue it here from the executor's result.
    qf = result.get('queueFix')
    if qf:
        # A gate review that must go BACK to the same author + the same gate
        # carries the parent id; a free-form pressoffice fix does not.
        queue_work(state, [qf])
    # W3: a worker's spike can PROPOSE an issue (a Jev verdict that the spike's
    # finding names a real, actionable gap). The executor reports a WISH -- the
    # same safe indirection as queueFix/notifyPlayer: it runs off-thread against
    # a read-only snapshot, so it must never call file_issue directly. Filing
    # happens HERE, inside the tick's single read-modify-write, where `state`
    # is live and the owning team is resolved fresh (wish teamId -> task teamId
    # -> product director -> roster director, each matched by id OR directorId).
    wish = result.get('fileIssue')
    if wish:
        _file_spike_issue(state, wish, task, now_ms)
    # A content executor (currently just spikes)
    # can ask to notify the player when it lands, same safe indirection as
    # queueFix above -- executors run off-thread with only a read-oriented
    # snapshot, so they report the WISH here rather than calling
    # _queue_player_email directly; this runs inside the tick's real
    # read-modify-write, where state is actually the live, mutable object.
    notify = result.get('notifyPlayer')
    if notify and notify.get('kind') and notify.get('subject') and notify.get('body'):
        _queue_player_email(state, notify['kind'], notify['subject'], notify['body'])
        task['_notifyQueued'] = notify.get('kind')
    # Peer-approval gate (Phase E addendum): when the completing task is a
    # review subtask carrying a `reviewOf` pointer, fold its Jev verdict into
    # the PARENT task's gate -- clean = +1 approval (distinct reviewers only),
    # actionable = reset the gate + requeue a fix to the author. The parent is
    # never finished here; _task_cycle decides close (it needs `now`).
    if task.get('reviewOf'):
        parent = _resolve_review_parent(state, task)
        if parent is not None:
            verdict = result.get('peerVerdict')
            reviewer = task.get('assignedTo')
            gate = parent['_peerGate']
            if (task.get('taskType') in ('review', 'qa') and task.get('createdAt')
                    and task['createdAt'] < (gate.get('enteredMs') or 0)):
                return  # an older artifact result cannot change this gate
            if result.get('reviewFormatError'):
                kind = result.get('reviewFailureKind') or 'invalid_response'
                parent['reviewFailureKind'] = kind
                parent['reviewDiagnosis'] = {'category': kind, 'failure': result.get('note')}
                parent['lastReviewDiagnosticsTask'] = task['id']
                counter = ('serviceFailures' if kind == 'service_unavailable'
                           else 'inputFailures' if kind == 'missing_inputs' else 'formatFailures')
                failures = gate[counter] = gate.get(counter, 0) + 1
                if failures >= 3 or kind == 'missing_inputs':
                    gate['escalated'] = True
                    parent.update(manualAttention=True, blockedReason=kind,
                                  failNote='Review attempts stopped: ' + str(result.get('note') or 'No usable verdict.')[:500])
                return
            if verdict == 'clean':
                import agents.review_quality as review_quality
                approval_errors = review_quality.clean_vote_errors(
                    result,
                    require_acceptance=result.get('acceptanceAssessment') is not None)
                if approval_errors:
                    gate['approvals'] = 0
                    gate['approvers'] = []
                    gate['acceptanceVerified'] = False
                    parent.setdefault('peerReviewErrors', []).append(
                        {'atMs': now_ms, 'reviewer': reviewer, 'errors': approval_errors[:12]})
                    if review_quality.known_failure(result):
                        verdict = 'actionable'
                    else:
                        kind = 'invalid_response'
                        parent['reviewFailureKind'] = kind
                        parent['reviewDiagnosis'] = {'category': kind,
                                                     'failure': 'Approval refused: ' + '; '.join(approval_errors)[:1000]}
                        parent['lastReviewDiagnosticsTask'] = task['id']
                        gate['formatFailures'] = gate.get('formatFailures', 0) + 1
                        if gate['formatFailures'] >= 3:
                            gate['escalated'] = True
                            parent.update(manualAttention=True, blockedReason=kind,
                                          failNote='Approval refused: ' + '; '.join(approval_errors)[:500])
                        return
            # Item 6: fold the reviewer's structured verdict onto the parent so
            # the author and player see WHY the vote went the way it did, not
            # just the binary -- the summary, the checks actually verified, and
            # the remaining risks. Best-effort; a missing structured block
            # changes nothing.
            if result.get('peerSummary') or result.get('peerChecks') or result.get('peerRisks'):
                parent.setdefault('peerReviews', []).append({
                    'reviewer': reviewer,
                    'verdict': verdict,
                    'summary': result.get('peerSummary') or '',
                    'checks': list(result.get('peerChecks') or []),
                    'risks': list(result.get('peerRisks') or []),
                    'atMs': now_ms,
                })
            if verdict == 'actionable':
                if not _maybe_escalate_stuck_gate(state, parent, gate, 'repeated rejections'):
                    gate['approvals'] = 0
                    gate['approvers'] = []
                    _sim_notify_author(state, parent, reviewer, rationale=task.get('note'))
            elif verdict == 'clean':
                # Cut 4 hard gate: a review that finds the work clean can only
                # count as an APPROVAL if its quality pipeline objectively
                # passed (flake8/mypy/bandit/pytest-cov all green). A reviewer's
                # "looks solid" cannot approve code that fails the standard -- so
                # a clean verdict with a red or missing pipeline is downgraded to
                # the actionable path (reset the gate + send it back to the
                # author) exactly as if the reviewer had rejected it.
                if not result.get('pipelineOk'):
                    verdict = 'actionable'
                # Item 7 evidence-based done gate: for a CODING-CLASS parent (a
                # pressoffice story or any product-backed card), a clean vote also
                # needs OBJECTIVE evidence that the quality pipeline actually ran
                # and exercised the code -- the executor files the real per-step
                # output (coverage line, flake8/mypy/bandit tails) in
                # result['evidence']. A 'looks solid' with no evidence cannot
                # approve code any more than a red pipeline can; without this a
                # hollow clean vote would close a card that was never really
                # tested. Non-coding lanes (observatory etc.) stay on the
                # pipelineOk check alone -- deliberately: they can
                # legitimately conclude with no deliverable (see
                # library/design-references/stop-condition.md).
                elif _peer_coding_class(parent) and not (result.get('evidence') or '').strip():
                    verdict = 'actionable'
                if verdict == 'actionable':
                    if not _maybe_escalate_stuck_gate(state, parent, gate, 'repeated red-pipeline rejections'):
                        gate['approvals'] = 0
                        gate['approvers'] = []
                        _sim_notify_author(state, parent, reviewer, rationale=task.get('note'))
                elif reviewer and reviewer not in gate['approvers']:
                    gate['approvers'].append(reviewer)
                    gate['approvals'] += 1
                    if result.get('acceptanceAssessment') is not None:
                        gate['acceptanceVerified'] = bool(
                            (result.get('acceptanceAssessment') or {}).get('verified'))
    # BP-31 close probe: an incident card is judged here, inside the tick's
    # single read-modify-write, before the completion flow closes it -- a
    # bare "fixed it" (no landed files, no verification note) closes loud
    # instead of silent. Stories/reviews/spikes never touch this path.
    if task.get('taskType') == 'bug':
        _verify_incident_restore(state, task, result, now_ms)


def _take_content_result(task_id):
    with _content_results_lock:
        return _content_results.pop(task_id, None)


# Item 2: per-agent feedback buffer -- the durable 'what came back at me'
# channel _build_agent_perception renders into the next dispatch's context.
# Capped so it stays a recent-memory window, not a ledger.
_FEEDBACK_MAX = 10


def _append_feedback(state, agent_id, text, source=None):
    """Record one feedback entry for `agent_id` (a rejection rationale, a
    pipeline failure, or an executor-reported block). Pure state mutation;
    JSON-safe (a list of small dicts). Written ONLY inside the tick's single
    read-modify-write (or a pure helper it calls) -- never from an executor
    thread, which has only a snapshot.

    Dedup: when the newest entry has the same `source` AND its text shares the
    same first 40 characters (the task-title prefix both pipeline and
    peer-review notices carry), the old entry is REPLACED, not stacked. Agents
    reported the same rejection flooding their feedback buffer 3-5 times with
    no way to tell which copy was current. Deliberately scoped to the
    rejection sources (pipeline, peer_review, executor) -- player_feedback is
    excluded because each player comment on the same card is intentional new
    information (e.g. upgrading a rating), not a duplicate notice."""
    text = (text or '').strip()
    if not agent_id or not text:
        return
    bucket = state.setdefault('_feedback', {}).setdefault(agent_id, [])
    _dedup_sources = ('pipeline', 'peer_review', 'executor')
    if source in _dedup_sources and bucket:
        last = bucket[-1]
        if last.get('source') == source and last.get('text', '')[:40] == text[:40]:
            last['text'] = text[:500]
            return
    entry = {'text': text[:500]}
    if source:
        entry['source'] = source
    bucket.append(entry)
    if len(bucket) > _FEEDBACK_MAX:
        del bucket[:len(bucket) - _FEEDBACK_MAX]


def _acquaintance_set(state, agent_id):
    """The ids `agent_id` is acquainted with. Stored as JSON-safe lists (never
    sets), so `_acquaintances` survives the kv_state blob round trip."""
    return (state.get('_acquaintances') or {}).get(agent_id) or []


def _mark_acquaintance(state, a, b):
    """Record that `a` and `b` met, symmetrically (co-location at a social
    ceremony is the marker -- see the social sweep). A self-introduction or a
    missing id is a no-op. Pure state mutation; item 8's gate."""
    if not a or not b or a == b:
        return
    acq = state.setdefault('_acquaintances', {})
    for x, y in ((a, b), (b, a)):
        lst = acq.setdefault(x, [])
        if y not in lst:
            lst.append(y)


def _are_acquainted(state, a, b):
    """True when `a` and `b` have been co-located at a social ceremony. Nobody
    is a stranger to themselves."""
    if not a or not b:
        return False
    if a == b:
        return True
    return b in _acquaintance_set(state, a)


def _describe_agent(state, agent_id, viewer_id, fallback_name=None):
    """How `viewer_id` should refer to `agent_id`: by name once they are
    acquainted, otherwise by role ('the Research analyst') -- a stranger's name
    is not a name the viewer can honestly use. Falls back to the raw id."""
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])
              if isinstance(d, dict)}
    rec = roster.get(agent_id) or {}
    name = rec.get('name') or fallback_name or agent_id
    role = rec.get('role') or ''
    if _are_acquainted(state, viewer_id, agent_id):
        return name + (f' (the {role})' if role else '')
    if role:
        return f'the {role}'
    return 'a colleague you have not met'


def _build_agent_perception(state, agent_id, task=None):
    """Compose the agent's situational awareness into a compact markdown block
    (base_ctx) folded into the content executor's system prompt: who am I, where
    am I, who's around me, what am I working on, and what feedback came back at
    me -- so a worker reasons like a teammate walking into the room, not a
    stateless API call. Pure read of `state` (a frozen snapshot in production,
    so no race with the sync pass); returns '' when nothing coherent can be
    said, and executors treat a falsy base_ctx as 'no perception' so their
    existing unit-test prompts stay byte-identical."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])
              if isinstance(d, dict)}
    me = agents.get(agent_id) or {}
    rec = roster.get(agent_id) or {}
    parts = []
    identity = f'You are {rec.get("name") or me.get("name") or agent_id}.'
    role = rec.get('role') or ''
    if role:
        identity += f' Your role on the team: {role}.'
    parts.append(identity)
    room = me.get('inRoom') or (task or {}).get('room') or ''
    if room:
        parts.append(f'You are currently in the {room}.')
    present = [a for a in agents.values()
               if a and a.get('id') != agent_id and a.get('inRoom') == room
               and not a.get('offDuty')]
    if present:
        who = ', '.join(_describe_agent(state, a['id'], agent_id)
                        for a in present[:8])
        parts.append(f'People in the room with you: {who}.')
    if task:
        title = (task.get('title') or '').strip()
        if title:
            line = f'Your current task: {title}'
            instructions = task.get('instructions')
            if instructions:
                line += f' -- {instructions}'
            parts.append(line + '.')
        ac = task.get('acceptanceCriteria')
        if ac:
            parts.append(f'Acceptance criteria: {ac}')
        never = task.get('never')
        if never:
            parts.append(f'NEVER:\n{never}')
        if task.get('reviewOf'):
            parts.append(f'This is a peer-review of task {task["reviewOf"]} '
                         f'-- your verdict counts on the parent story.')
    feedback = (state.get('_feedback') or {}).get(agent_id) or []
    if feedback:
        lines = []
        for f in feedback[-5:]:
            if isinstance(f, dict):
                text = f.get('text') or f.get('guidance') or ''
            else:
                text = str(f)
            if text:
                lines.append(f'- {text}')
        if lines:
            parts.append('Feedback on your recent work:\n' + '\n'.join(lines))
    return '\n\n'.join(p for p in parts if p)


# The content executor for the CURRENT task, set by serve.py so sim.py stays
# pure/unit-testable (no network, no serve import). Signature:
#   executor(state_snapshot, agent_id, task, topic, base_ctx) -> None
# It runs its network work (browse/chat/library), then calls
# _store_content_result(task_id, {'note': str, 'seenUrls': [...]}). None =
# no real content executor ported for this room -> the workUntil fallback.
_content_executor = None
