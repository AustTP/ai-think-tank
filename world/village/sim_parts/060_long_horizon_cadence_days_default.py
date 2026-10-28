LONG_HORIZON_CADENCE_DAYS_DEFAULT = 30.0


def next_topic_id(state):
    """Server-side monotonic counter for researchTopics ids (topic-1, topic-2,
    ...), mirroring next_issue_key's cold-starts-at-1/never-collides shape.
    Topics aren't team-scoped, so this is one global counter, not per-prefix."""
    n = (state.get('researchTopicCounter') or 0) + 1
    state['researchTopicCounter'] = n
    return f'topic-{n}'


def add_research_topic(state, topic, start_url, cadence_ms, now_ms=None,
                       link_keyword=None, page_keyword=None, depends_on_task=None,
                       enabled=True, created_by=None):
    """The creator side of the standing research-topic cadence: _check_schedules
    (below) has always been able to FIRE a due topic, but nothing ever appended
    one to state['researchTopics'] -- it was seeded empty at boot and never
    written to again. This is that missing write path.

    Fails closed rather than guessing: rejects an empty topic, a start_url that
    doesn't parse as a real absolute URL (scheme + host), and clamps cadence_ms
    to MIN_RESEARCH_CADENCE_MS so a bad interval can't turn into a queue-flood.
    `depends_on_task` (a task id) adds the dependency gate: the topic does not
    fire until that task reaches 'done' -- and its cadence marker is NOT
    advanced while it waits, so the crawl fires on the first pass after the
    dependency lands. Returns the new record, or None if rejected. `lastRunAt`
    starts at 0 so the first crawl fires on the very next _check_schedules pass,
    matching the intuitive "start checking X" request rather than waiting a
    full cadence."""
    topic = (topic or '').strip()
    start_url = (start_url or '').strip()
    if not topic or not start_url:
        return None
    parsed = urllib.parse.urlparse(start_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    cadence_ms = max(int(cadence_ms or 0), MIN_RESEARCH_CADENCE_MS)
    record = {
        'id': next_topic_id(state),
        'topic': topic,
        'startUrl': start_url,
        'cadenceMs': cadence_ms,
        'lastRunAt': 0,
        'seenUrls': [],
        'linkKeyword': link_keyword or None,
        'pageKeyword': page_keyword or None,
        'dependsOnTask': depends_on_task or None,
        'enabled': enabled is not False,
        'createdBy': created_by or None,
    }
    state.setdefault('researchTopics', []).append(record)
    return record


def next_pipeline_id(state):
    """Server-side monotonic counter for pipelines (pl-1, pl-2, ...), mirroring
    next_topic_id's cold-starts-at-1/never-collides shape. Pipelines aren't
    team-scoped, so this is one global counter, not per-prefix."""
    n = (state.get('pipelineCounter') or 0) + 1
    state['pipelineCounter'] = n
    return f'pl-{n}'


def add_pipeline(state, name, cadence_ms, steps, now_ms=None, depends_on_task=None):
    """The creator side of an ORDERED pipeline: a named sequence of steps that
    fires in strict order, each step gated on its predecessor's completion (the
    player-facing scheduling lane -- see _check_pipelines below). This is the
    write path _check_schedules has always been able to FIRE for (the
    research-topic lane) but nothing ever appended for a multi-step sequence.

    Fails closed rather than guessing: rejects an empty name, a non-list or
    empty `steps`, and any step without a title/room, and clamps cadence_ms to
    MIN_PIPELINE_CADENCE_MS so a bad interval can't turn into a queue-flood.
    Each step may carry {title, room, offsetMs, instructions, tool, args};
    offsetMs is a minimum delay AFTER the previous step's completion (not from
    the run's start -- strict ordering dominates timing). `depends_on_task` (a
    task id) adds the dependency gate: the run does not START until that task
    reaches 'done' (a mid-run sequence keeps firing its remaining steps once it
    has started). Returns the new record, or None if rejected. `lastRunAt`
    starts at 0 so the first step fires on the very next _check_schedules pass."""
    name = (name or '').strip()
    if not name or not isinstance(steps, list) or not steps:
        return None
    clean_steps = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        title = (s.get('title') or '').strip()
        room = (s.get('room') or '').strip()
        if not title or not room:
            continue
        try:
            offset_ms = max(0, int(s.get('offsetMs') or 0))
        except (TypeError, ValueError):
            offset_ms = 0
        clean_steps.append({
            'title': title,
            'room': room,
            'offsetMs': offset_ms,
            'instructions': (s.get('instructions') or '').strip() or title,
            'tool': (s.get('tool') or '').strip() or None,
            'args': dict(s.get('args') or {}),
        })
    if not clean_steps:
        return None
    cadence_ms = max(int(cadence_ms or 0), MIN_PIPELINE_CADENCE_MS)
    record = {
        'id': next_pipeline_id(state),
        'name': name,
        'cadenceMs': cadence_ms,
        'lastRunAt': 0,
        'createdAt': now_ms if now_ms is not None else int(time.time() * 1000),
        'steps': clean_steps,
        'runId': 0,          # incremented at each run start; disambiguates the
                             # same stepIndex across runs (old done tasks persist)
        'runStepIndex': 0,   # next step index to fire in the current run
        'lastStepCompletedAt': None,
        'dependsOnTask': depends_on_task or None,
    }
    state.setdefault('pipelines', []).append(record)
    return record


_DEVICE_CHECKIN_HISTORY_MAX = 50  # bounded ring buffer -- no consumer yet, but never unbounded growth
_DEVICE_CHECKIN_RETENTION_DAYS = 30


def record_device_checkin(state, fields, now_ms=None):
    """Record one check-in from the player's phone (POST /api/device/checkin,
    serve.py). Pure state mutation, no consumer wired up yet --
    the whole point was landing a clean, generic ingestion pipe first and
    deciding what to build on top once data is actually flowing. `fields`
    is whatever the caller received (location/battery/focus/wifi/trigger
    today, deliberately open to more later); None values are dropped rather
    than stored as noise. Keeps only the last _DEVICE_CHECKIN_HISTORY_MAX
    entries -- state['deviceCheckins'] lives in the same hot kv_state blob
    every other piece of state does, so this must never grow unbounded."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    entry = {k: v for k, v in (fields or {}).items() if v is not None}
    entry['receivedAt'] = now_ms
    bucket = state.setdefault('deviceCheckins', {'last': None, 'history': []})
    bucket['last'] = entry
    history = bucket.setdefault('history', [])
    history.append(entry)
    if len(history) > _DEVICE_CHECKIN_HISTORY_MAX:
        del history[:-_DEVICE_CHECKIN_HISTORY_MAX]
    cutoff = now_ms - max(1, _DEVICE_CHECKIN_RETENTION_DAYS) * 86400 * 1000
    history[:] = [row for row in history
                  if int(row.get('receivedAt') or 0) >= cutoff]
    return entry


def _skill_review_has_pending():
    """Content gate for the standing skill-review sweep: is anything actually
    waiting under library/pending_review/skills/? Mirrors the executor's own
    determinism (content.py `_run_skill_review_content` lists /api/library and
    keeps paths whose first segment is pending_review/skills). When nothing is
    waiting, the 30-minute ceremony must not queue a task -- picking an agent
    and burning a Jev grade call against an empty queue is the waste this gate
    closes. Traversal stays under pending_review/skills/ only (review-trace/,
    the failed-review lessons, are included exactly as the executor includes
    them)."""
    import serve
    pending_root = os.path.join(serve.LIBRARY_DIR, 'pending_review', 'skills')
    if not os.path.isdir(pending_root):
        return False
    for _root, _dirs, filenames in os.walk(pending_root):
        if any(fn for fn in filenames if not fn.startswith('.')):
            return True
    return False


def _skill_review_in_flight(state):
    """Stagger gate for the standing skill-review ceremony: at most ONE
    skill-review task may be queued or in progress at a time, so the ceremony
    never stacks overlapping reviews for the whole think tank at once. Any
    queued work item carrying `skillReview`, or any live task (not 'done')
    carrying `skillReview`, blocks the next sweep."""

    def _active(t):
        return bool(t.get('skillReview')) and t.get('status') != 'done'

    for item in (state.get('workQueue') or []):
        if item.get('skillReview'):
            return True
    for t in (state.get('tasks') or {}).values():
        if _active(t):
            return True
    return False


def _distill_has_new_archives(since_ms):
    """Content gate for the standing distillation sweep: are there archive
    findings newer than `since_ms`? Mirrors the executor's own archive scan
    (content.py `_run_distill_content`: only plain files directly in
    LIBRARY_ARCHIVE_DIR, strictly newer than the last distillation). Nothing
    new since the last pass -> the ceremony stays quiet instead of re-picking
    an agent to fold nothing in."""
    import serve
    archive_dir = serve.LIBRARY_ARCHIVE_DIR
    if not os.path.isdir(archive_dir):
        return False
    try:
        for fn in os.listdir(archive_dir):
            full = os.path.join(archive_dir, fn)
            if not os.path.isfile(full):
                continue
            if int(os.path.getmtime(full) * 1000) > since_ms:
                return True
    except OSError:
        return False
    return False


def _ensure_long_horizon_topic(state, now_ms):
    """Bell-style long-horizon problem: seed ONE standing research topic per
    village whose crawl cadence is far in the future (LONG_HORIZON_CADENCE_DAYS),
    so the think tank always has something on the table that is NOT due this
    sprint -- a horizon beyond the 6s tick and the weekly ceremonies. Idempotent
    and self-cleaning: `state['longHorizonSeeded']` records which villages were
    seeded, so a topic deleted by the player is never silently re-created, while
    a partially-failed seed (bad .env URL) falls back to the safe defaults and is
    retried next pass. `lastRunAt` is seeded to now so the first crawl fires one
    full cadence out, not on the next pass. Pure state mutation, no Jev spend."""
    villages = state.get('villages') or []
    if not villages:
        return
    try:
        import serve as _serve_mod
        env = _serve_mod._load_env()
        topic = (env.get('LONG_HORIZON_TOPIC') or '').strip() or LONG_HORIZON_TOPIC_DEFAULT
        url = (env.get('LONG_HORIZON_TOPIC_URL') or '').strip() or LONG_HORIZON_TOPIC_URL_DEFAULT
        cadence_days = float(env.get('LONG_HORIZON_CADENCE_DAYS') or 0) or LONG_HORIZON_CADENCE_DAYS_DEFAULT
    except Exception:
        topic, url, cadence_days = (LONG_HORIZON_TOPIC_DEFAULT,
                                    LONG_HORIZON_TOPIC_URL_DEFAULT,
                                    LONG_HORIZON_CADENCE_DAYS_DEFAULT)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        url = LONG_HORIZON_TOPIC_URL_DEFAULT
    cadence_ms = max(int(cadence_days * 24 * 3600 * 1000), MIN_RESEARCH_CADENCE_MS)
    seeded = state.setdefault('longHorizonSeeded', {})
    for v in villages:
        vid = v.get('id') or DEFAULT_VILLAGE
        if seeded.get(vid):
            continue
        state.setdefault('researchTopics', []).append({
            'id': next_topic_id(state),
            'topic': topic,
            'startUrl': url,
            'cadenceMs': cadence_ms,
            'lastRunAt': now_ms,
            'seenUrls': [],
            'villageId': vid,
            'longHorizon': True,
        })
        seeded[vid] = True
        _log_governance(state, None, 'long_horizon_topic_seeded',
                        {'villageId': vid, 'topic': topic, 'cadenceDays': cadence_days})


def _sync_research_watchlist(state):
    """Live-reconcile state['researchTopics'] against the operator-edited
    world/watchlist.json. The boot seed only ran at startup, so an edit to the
    file required a restart before; this runs on every _check_schedules pass
    and applies an operator edit live: a topic added to the file is appended
    to state (same add_research_topic validation + cadence clamp, so it fires
    on the next pass), and a standing topic removed from the file is dropped
    from state so it stops firing. Village long-horizon topics live only in
    state (never in the watchlist), so they are never removed. Gated on
    config.ALLOW_WATCHLIST_WRITE (the real server boot turns it on; tests never
    run lifespan, so the file stays inert there -- exactly the write-back gate
    discipline). Removal is skipped if the file cannot be read, so a transient
    bad parse cannot wipe the schedule. Returns (added, removed)."""
    import core.config as _config
    if not _config.ALLOW_WATCHLIST_WRITE:
        return 0, 0
    watchlist, ok = _config.get_research_watchlist()
    topics = state.setdefault('researchTopics', [])
    by_key = {(t.get('topic'), t.get('startUrl')) for t in topics}
    added = 0
    for t in watchlist:
        key = (t.get('topic'), t.get('startUrl'))
        if key in by_key:
            continue
        record = add_research_topic(state, t.get('topic'), t.get('startUrl'),
                                    t.get('cadenceMs') or 0,
                                    link_keyword=t.get('linkKeyword'),
                                    page_keyword=t.get('pageKeyword'),
                                    enabled=t.get('enabled', True),
                                    created_by='watchlist')
        if record is not None:
            by_key.add(key)
            added += 1
    removed = 0
    if ok:
        wanted = {(t.get('topic'), t.get('startUrl')) for t in watchlist}
        kept = [t for t in topics
                if t.get('longHorizon') or (t.get('topic'), t.get('startUrl')) in wanted]
        removed = len(topics) - len(kept)
        if removed:
            state['researchTopics'] = kept
    return added, removed


def _standing_task_sweep(state, now, now_ms):
    """The standing-task (cron scheduling) sweep, ported into
    the same _check_schedules pass as the research-topic
    cadence: every state['standingTasks'] declaration is ticked
    through the module's StandingScheduler, and the scheduler's
    verdict becomes sim behavior -- a 'start' queues a real
    queue_work card (title/room/instructions/standingTaskId/
    teamId, kbClass 'changes_how_we_work'), a 'stop' is logged,
    continue/idle are silent (the running work is already
    represented by the card the start queued). The scheduler
    owns per-task runtime state in-process; the declarations in
    state are the durable records.

    Malformed declarations (an unknown mode, a window without
    times, a weekly without days) raise inside the StandingTask
    constructor -- the sweep skips them and keeps ticking the
    rest rather than wedging every standing task behind one bad
    record. `now` is a float epoch (the _check_schedules
    convention) or a datetime; both are accepted."""
    tasks = []
    for declaration in (state.get('standingTasks') or []):
        if not isinstance(declaration, dict) or not declaration.get('taskId'):
            continue
        try:
            tasks.append(schedule.StandingTask(
                task_id=declaration['taskId'],
                name=declaration.get('name') or declaration['taskId'],
                mode=declaration.get('mode') or schedule.ALWAYS,
                agent_count=declaration.get('agentCount'),
                duration_hours=declaration.get('durationHours'),
                start_time=declaration.get('startTime'),
                end_time=declaration.get('endTime'),
                days_of_week=set(declaration.get('daysOfWeek') or ()),
                day_of_month=declaration.get('dayOfMonth'),
                yearly_date=(tuple(declaration['yearlyDate'])
                             if declaration.get('yearlyDate') else None),
                max_occurrences=declaration.get('maxOccurrences'),
                max_consecutive_hours=declaration.get('maxConsecutiveHours'),
                max_total_hours=declaration.get('maxTotalHours'),
                consecutive_reset_after=(
                    datetime.timedelta(
                        hours=declaration['consecutiveResetAfterHours'])
                    if declaration.get('consecutiveResetAfterHours') is not None
                    else None),
                team=declaration.get('team'),
            ))
        except (TypeError, ValueError):
            continue  # a malformed declaration never wedges the sweep
    if not tasks:
        return
    now_dt = (now if isinstance(now, datetime.datetime)
              else datetime.datetime.fromtimestamp(now))
    results = _STANDING_SCHEDULER.tick_all(tasks, now_dt)
    for task in tasks:
        result = results.get(task.task_id)
        if result is None:
            continue
        if result.action == 'start':
            declaration = next((d for d in (state.get('standingTasks') or [])
                                if d.get('taskId') == task.task_id), {})
            if declaration.get('script'):
                cron_scripts.run_standing(state, declaration, _log_governance,
                                          queue_work, now_ms)
                continue
            crew = (result.agent_count
                    if result.agent_count is not None else 0)
            queue_work(state, [{
                'title': f"Standing task: {task.name}",
                'room': declaration.get('room') or 'observatory',
                'instructions': (
                    f'Standing task "{task.name}" is due -- its schedule '
                    f'just opened ({result.reason}). Work it with a crew '
                    f'of {crew}; the lead role for a crew that size is '
                    f'{result.lead_role}.'),
                'standingTaskId': task.task_id,
                'teamId': task.team or None,
                'kbClass': 'changes_how_we_work',
            }])
            _log_governance(state, (state.get('sim') or {}).get('owner')
                            or 'server', 'standing_task_tick', {
                                'action': 'start', 'taskId': task.task_id,
                                'reason': result.reason,
                                'agentCount': crew,
                                'leadRole': result.lead_role,
                            })
        elif result.action == 'stop':
            _log_governance(state, (state.get('sim') or {}).get('owner')
                            or 'server', 'standing_task_tick', {
                                'action': 'stop', 'taskId': task.task_id,
                                'reason': result.reason,
                            })


def _check_schedules(state, now, now_ms):
    """Port of tasks.js checkResearchSchedule + checkSkillReviewSchedule: queue
    due standing work, stamping lastRunAt/lastSkillReviewAt BEFORE assignment so
    a due topic isn't re-picked. Mutates state['researchTopics'] in place.

    The two standing ceremonies (skill review + distillation) are ALSO content-
    gated: a due cadence only queues work when there is actually something to
    review/merge. With no content the marker is deliberately NOT advanced, so
    the sweep fires on the first later pass where content appears."""
    import serve
    # Keep state['researchTopics'] a live mirror of the operator-edited
    # watchlist.json first, so an edit takes effect without a restart.
    _sync_research_watchlist(state)
    # Research topics.
    # Long-horizon seeding first: a village's standing far-future topic is
    # created before due topics fire, so a fresh village always has its horizon
    # problem on the table from its first pass.
    _ensure_long_horizon_topic(state, now_ms)
    for topic in (state.get('researchTopics') or []):
        if topic.get('enabled') is False:
            continue
        if now_ms - (topic.get('lastRunAt') or 0) < topic.get('cadenceMs', 0):
            continue
        # Composed schedule+dependency: a topic with a `dependsOnTask` waits for
        # that task to reach 'done'. The marker is deliberately NOT advanced
        # while it waits, so the crawl fires on the first pass AFTER the
        # dependency lands (same marker-held-while-gated rule as the content
        # gates below) instead of silently burning a cadence cycle.
        if topic.get('dependsOnTask') and not _dependency_landed(state, topic['dependsOnTask']):
            continue
        previous_run_at = topic.get('lastRunAt') or 0
        topic['lastRunAt'] = now_ms
        # The Research Desk's 24h overlap: search the period since the last
        # successful run MINUS the overlap, so a discovery that landed just
        # after the previous window is caught on this run instead of waiting a
        # full cadence. First run (previous_run_at 0) searches the whole
        # window. Mirrors evidence.search_since_ms / serve.RESEARCH_OVERLAP_MS.
        since_ms = serve.search_since_ms(previous_run_at,
                                         serve.RESEARCH_OVERLAP_MS)
        queue_work(state, [{
            'title': f"Scheduled research: {topic.get('topic')}",
            'room': 'observatory',
            'instructions': (f'Crawl starting from {topic.get("startUrl")} and update the '
                             f'"{topic.get("topic")}" skill file with anything genuinely new '
                             f'since last time (searching a 24h-overlapped window for late '
                             f'discoveries). Run three search passes and answer each one: '
                             f'announcements (what changed), practical examples (someone '
                             f'actually using it, with inspectable material), and limitations '
                             f'(documented restrictions, corrections, availability problems). '
                             f'Separate what a source supports from our inference. A repost is '
                             f'not a second independent source. Do not manufacture a finding '
                             f'when there is none. Record each detected change as a before/after '
                             f'observation via POST /api/evidence/observation (claimId, field, '
                             f'before, after, sourceUrl, note) and end the run by folding the '
                             f'GET /api/research-desk/ops numbers into your summary (see the '
                             f'research-desk skill).'),
            'goal': topic.get('topic'),
            'research': {'topicId': topic.get('id'), 'since': since_ms},
            'kbClass': 'changes_how_we_work',
        }])
    # Skill review sweep. `_cadence_due` treats the explicit CADENCE_NEVER marker
    # as never-due and normalizes any legacy far-future TEST sentinel (1e18) that
    # leaked into live state, so the sweep can neither be muted by a real date nor
    # disabled by a ghost of the old test macro. Content-gated: nothing waiting in
    # pending_review/skills/ -> no task queued and the marker NOT advanced (the
    # sweep fires on a later pass the moment content appears).
    if _cadence_due(state, 'lastSkillReviewAt', SKILL_REVIEW_CADENCE_MS, now_ms=now_ms) \
            and _skill_review_has_pending() and not _skill_review_in_flight(state):
        state['lastSkillReviewAt'] = now_ms
        queue_work(state, [{
            'title': 'Review pending skill files',
            'room': 'observatory',
            'instructions': 'Review whatever is waiting in pending_review/skills/ and decide, file by file, whether each one is accurate and worth keeping as real reference material.',
            'skillReview': True,
            'kbClass': 'changes_how_we_work',
        }])
    # Hive-mind distillation sweep. Same shape as the skill-review sweep: stamp
    # the marker BEFORE assignment (a due run mustn't be re-picked) and let the
    # task-cycle's idle gate decide whether the think tank is even working right
    # now (no spend when idle). `since` is the previous run's stamp, so the
    # executor only folds in findings archived after the last distillation.
    # Also content-gated: due + nothing new since the last pass -> quiet, marker
    # kept (a leaked legacy 1e18 TEST sentinel is normalized to 0 so it doesn't
    # poison the gate's comparison window and block the first real distillation).
    if _cadence_due(state, 'lastDistillAt', DISTILL_CADENCE_MS, now_ms=now_ms):
        previous_distill_at = state.get('lastDistillAt') or 0
        if previous_distill_at - now_ms > _CADENCE_LEGACY_FAR_FUTURE_MS:
            previous_distill_at = 0
        if _distill_has_new_archives(previous_distill_at):
            state['lastDistillAt'] = now_ms
            queue_work(state, [{
                'title': 'Distill recent think tank knowledge',
                'room': 'observatory',
                'instructions': ('Merge the findings archived since the last distillation into the '
                                 'think tank wiki, removing redundancy and extracting what the think tank '
                                 'now knows as a body.'),
                'distill': True,
                'distillSince': previous_distill_at,
                'kbClass': 'changes_how_we_work',
            }])

    # Scrum-master workflow memory: completed requests become card templates,
    # a second clean run promotes them, and any team's scrum master can reuse
    # them at breakdown. Cheap state work; no model call.
    if _cadence_due(state, 'lastWorkflowTemplateAt', WORKFLOW_TEMPLATE_CADENCE_MS,
                    now_ms=now_ms):
        state['lastWorkflowTemplateAt'] = now_ms
        _workflow_template_sweep(state, now_ms)

    # Ordered pipelines (the player-facing scheduling lane): fire the next step
    # of each due pipeline in strict sequence, gating step N+1 on step N's
    # completion. Stamps the run's cadence marker at the START of a run (not per
    # step) so a multi-step sequence gets its full cadence window before re-running.
    _check_pipelines(state, now_ms)

    # Director-gated player-ask / supervisor requirements-met sweeps. Unlike the
    # cadence markers above these run every _check_schedules pass: they spin a
    # PENDING gate (a blocked agent waiting on a verdict), which must resolve as
    # soon as its short window elapses -- not on a 30-minute cadence. Each no-ops
    # (returns None / False) when nothing is due, and only calls Jev when a gate
    # is actually old enough to decide.
    _pending_player_ask_sweep(state, now_ms)
    _supervisor_block_vote_sweep(state, now_ms)

    # Standing tasks (the cron scheduling ask): tick every
    # state['standingTasks'] declaration; a 'start' queues real
    # work through the same queue_work lane as every sweep above.
    _standing_task_sweep(state, now, now_ms)


def _pipeline_step_task(state, pipeline_id, run_id, step_index):
    """The live task mirror carrying this pipeline's step marker, or None.
    `run_id` disambiguates the same stepIndex across runs (old 'done' tasks
    persist in state, so a fresh run must not mistake a previous run's task for
    its own)."""
    for t in (state.get('tasks') or {}).values():
        marker = t.get('pipelineStep') or {}
        if (marker.get('pipelineId') == pipeline_id
                and marker.get('runId') == run_id
                and marker.get('stepIndex') == step_index):
            return t
    return None


def _check_pipelines(state, now_ms):
    """The creator-visible scheduled-pipeline sweep, ported into the same
    _check_schedules pass as the research-topic cadence. Each pipeline is an
    ORDERED sequence: step N+1 fires only after step N's task reaches 'done'
    (strict ordering -- a pending/active predecessor suppresses every later
    step, never skipped). Steps ride the normal queue_work -> assign -> content
    lifecycle; completion is read back from the durable task mirror by matching
    the pipelineStep marker each step was queued with.

    Cadence: `lastRunAt` is stamped at the START of a run (when step 0 fires,
    or when a completed run re-arms), so a multi-step pipeline gets a full
    cadenceMs window between runs -- the run start, not each step, advances the
    marker. `runStepIndex` tracks the next step to fire; a pipeline whose steps
    are all done waits out the remainder of the window.

    Offset: each step's offsetMs is a MINIMUM delay -- for step 0, from the
    run's start; for every later step, after the PREVIOUS step's completion
    (tracked in `lastStepCompletedAt`). Strict ordering dominates timing -- a
    large offset can delay a later step, but never lets it leapfrog an
    unfinished predecessor."""
    for p in (state.get('pipelines') or []):
        steps = p.get('steps') or []
        if not steps:
            continue
        cadence_ms = max(p.get('cadenceMs') or 0, MIN_PIPELINE_CADENCE_MS)
        step_index = p.get('runStepIndex') or 0
        run_id = p.get('runId') or 0
        # Composed schedule+dependency: a pipeline with a `dependsOnTask` gates
        # at the RUN BOUNDARY only -- a never-started pipeline (no lastRunAt)
        # and a completed run waiting to re-arm both hold until the dependency
        # lands (no marker advance, so the run fires on the first pass after it
        # does). A MID-RUN pipeline keeps firing its remaining steps: the
        # dependency gates the run's start, not its tail.
        if p.get('dependsOnTask') and not _dependency_landed(state, p['dependsOnTask']) \
                and (not p.get('lastRunAt') or step_index >= len(steps)):
            continue
        # Pipeline run complete (all steps fired) and waiting out the cadence
        # window before the next run -- idle, no work.
        if step_index >= len(steps):
            if now_ms - (p.get('lastRunAt') or 0) < cadence_ms:
                continue
            # Re-arm the next run: advance the run counter so this run's step
            # markers can never be confused with a previous run's 'done' tasks.
            run_id += 1
            p['runId'] = run_id
            p['runStepIndex'] = 0
            p['lastRunAt'] = now_ms
            p['lastStepCompletedAt'] = None
            step_index = 0
        else:
            # Running (or about to begin). A never-started pipeline is due
            # immediately: stamp the run's start (and its run id) so step 0's
            # own offset floor is measured from now, not from the epoch.
            if not p.get('lastRunAt'):
                p['lastRunAt'] = now_ms
            if not run_id:
                run_id += 1
                p['runId'] = run_id
        # The current step may already be queued/active (queued last pass).
        task = _pipeline_step_task(state, p.get('id'), run_id, step_index)
        if task is not None:
            if task.get('status') != 'done':
                continue  # predecessor in flight -> hold the sequence
            # Done: advance past this step and start the next one's offset
            # clock from this completion.
            step_index += 1
            p['runStepIndex'] = step_index
            p['lastStepCompletedAt'] = now_ms
            if step_index >= len(steps):
                continue  # run complete; next pass gates the cadence window
            task = _pipeline_step_task(state, p.get('id'), run_id, step_index)
        if task is not None:
            continue  # next step already queued/active
        step = steps[step_index]
        # Offset floor: step 0 from the run's start, later steps from the
        # predecessor's completion.
        base_at = p.get('lastRunAt') if step_index == 0 else (p.get('lastStepCompletedAt') or p.get('lastRunAt'))
        if now_ms - (base_at or now_ms) < (step.get('offsetMs') or 0):
            continue
        queue_work(state, [{
            'title': f"[{p.get('name')}] {step.get('title')}",
            'room': step.get('room'),
            'instructions': step.get('instructions') or step.get('title'),
            'pipelineStep': {'pipelineId': p.get('id'), 'runId': run_id, 'stepIndex': step_index},
        }])


# ---------------------------------------------------------------------------
# Phase 4: server-owned governance (auto-hire + auto-firing review).
#
# The browser used to run two setInterval loops (index.html main():
# attemptAutoHire from hiring.js, attemptAutoFiringReview/finishFiringReview
# from firing.js). With no browser open those governance decisions -- who to
# hire for, who to fire -- silently died. This section re-homes them into a
# single cadence pass, `_governance_pass`, folded into the SAME read-modify-
# write as _task_cycle (called from _task_cycle, so it inherits the server-
# owner gate, the think_tank_has_work idle gate, and the process's single pass).
# Pure state mutation only; the Jev decision is resolved through an injectable
# `_governance_decider` so sim.py stays unit-testable with zero network.
# ---------------------------------------------------------------------------
def reports_about(state, about_id):
    """Port of reports.js reportsAbout: all reports filed against an agent."""
    return [r for r in (state.get('reports') or []) if r.get('aboutId') == about_id]


def morale_for(state, agent_id, now_ms=None):
    """Port of morale.js moraleFor -- a pure, 0-100 score. Reads the agent's
    approved/dropped/neglect signals off the durable state so future
    passes re-derive it (no stored score to go stale). Mirrors the reference
    video: the meter reacts to approved work, dropped work, and being spoken
    to -- NOT to reports/notes (those feed firing review, not morale).
    `now_ms` injectable for deterministic tests (falls back to wall-clock)."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return None
    # Morale arithmetic is team-scoped (the agreement may retune weights/caps);
    # resolve the whole mapping once so five keys don't rescan the team list.
    _mor = team_policy(state, agent_team_ref(state, agent_id))
    approved_bonus = min((a.get('approvedCount') or 0) * _mor['moraleApprovedWeight'], _mor['moraleApprovedCap'])
    days_since_hire = days_since(a.get('hiredAt'), now)
    drop_decay = max(0.0, 1 - days_since_hire / MORALE_DROPPED_DECAY_DAYS)
    dropped_penalty = (a.get('droppedCount') or 0) * _mor['moraleDroppedWeight'] * drop_decay
    last_contacted = a.get('lastContactedAt')
    neglect_penalty = (min(days_since(last_contacted, now) * _mor['moraleNeglectWeight'], _mor['moraleNeglectCap'])
                       if last_contacted else _mor['moraleNeglectCap'])
    raw = 100 + approved_bonus - dropped_penalty - neglect_penalty
    return int(max(0, min(100, round(raw))))


def _governance_decider_default(state, instructions, candidates, kind=None):
    """Default Jev resolver for governance, late-importing serve.py so sim.py
    stays importable/unit-testable offline. `candidates` is a list of
    {id, description}; returns a choice id (or None on a Jev outage).
    `kind` threads an explicit decision-tape tag (the worker-pick call site
    passes 'worker_pick'; other governance call sites keep the prompt-sniffed
    fallback)."""
    import serve
    try:
        questions = {'choice': {'type': 'choice', 'instructions': instructions,
                                'criteria': {c['id']: c['description'] for c in candidates}}}
        if kind:
            questions['_kind'] = kind
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}}, questions)
        choice, _, _ = serve._jev_choice(data)
        return choice if any(c['id'] == choice for c in candidates) else None
    except Exception:
        return None


def _fire_parity_decider(state, instructions, candidates):
    """Two-provider agreement (the parity-bit analog) on the fire/keep
    verdict: the primary governance decider plus one independent call on a
    DIFFERENT provider's model. Agreement decides; disagreement files an
    owner-safety escalation carrying both verdicts and acts on neither; a
    failed second call degrades to the single verdict (availability over
    strictness, with a logged marker). Firing decisions are rare, so the
    extra call costs nothing measurable and buys a second opinion on an
    irreversible action."""
    import serve as _serve
    first = _governance_decider(state, instructions, candidates)
    if first is None:
        return None
    try:
        second_model = _serve._parity_second_model()
        if not second_model:
            return first
        data = _serve._call_openrouter_decision_sync(
            second_model, {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': {c['id']: c['description'] for c in candidates}}})
        second, _, _ = _serve._jev_choice(data)
    except Exception as e:
        try:
            _serve.log_action(None, 'fire_parity_second_unavailable', {'error': str(e)[:200]})
        except Exception:
            pass
        return first
    if second not in {c['id'] for c in candidates}:
        try:
            _serve.log_action(None, 'fire_parity_second_unparsed',
                              {'model': second_model, 'choice': second})
        except Exception:
            pass
        return first
    if second == first:
        return first
    try:
        _serve.log_action(None, 'fire_parity_disagreement', {'first': first, 'second': second})
        _serve.create_escalation(
            'owner safety decision',
            f'Fire/keep parity check DISAGREED: primary verdict "{first}", independent '
            f'second opinion "{second}". No action was taken; a human decides. '
            f'Review context: {instructions[:600]}')
    except Exception:
        pass
    return None


# Injectable so tests substitute a deterministic decider; the live loop uses
# the default. Mirrors how _content_executor is injected.
_governance_decider = _governance_decider_default


def _senior_director(state):
    """firing.js firingReviewers: the senior-most director -- a roster entry
    that isDirector, not the admin, and has no `director` of its own (the top
    of the chain that stands in for the admin on approvals)."""
    roster = state.get('agentRoster') or []
    for d in roster:
        if d.get('isDirector') and not d.get('isAdmin') and not d.get('director'):
            return d
    return None


def _firing_reviewers(state):
    admin = None
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin'):
            admin = d
            break
    senior = _senior_director(state)
    if not admin or not senior:
        return []
    return [admin, senior]


def _review_is_stale(state, agent_id, now_ms):
    """firing.js reviewIsStale: a previous 'keep' verdict skips re-reviewing
    the EXACT same evidence until morale or the report count actually move."""
    a = (state.get('agents') or {}).get(agent_id)
    last = a and a.get('lastFiringReview')
    if not last or last.get('verdict') != 'keep':
        return False
    same_morale = morale_for(state, agent_id, now_ms) == last.get('morale')
    same_reports = len(reports_about(state, agent_id)) == last.get('reportCount')
    return same_morale and same_reports


def _has_firing_signal(state, agent_id, now_ms=None):
    """Firing keys off a real, corroborated personnel
    problem, NOT the composite morale score. Morale folds in neglect (never
    contacted = low score) and approved-work noise that say 'send this agent
    help / they might be underutilized' -- the hiring/load-spreading concern --
    but are NOT grounds to fire someone. The legitimate firing signals are
    (a) two serious/severe negative reviews that landed INSIDE the strike
    grace window (REPORT_STRIKE_WINDOW_MS), or a single severe one -- their
    peers flagged a real problem, and it's recent enough to matter -- and (b) a
    true drop-off -- they were handed work and dropped more of it than a third
    of what they approved (falling behind, not loafing). A lone negative review
    is not a signal: it may expire out of the window, and it is never grounds
    to convene a firing review by itself."""
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return False
    strikes = _report_strikes(state, agent_id, now_ms)
    if strikes['count'] >= 2 or strikes['severe'] >= 1:
        return True
    dropped = a.get('droppedCount') or 0
    approved = a.get('approvedCount') or 0
    return dropped > approved * 0.3


def _firing_signal_strength(state, agent_id, now_ms=None):
    """Comparable strength of an agent's firing signal for who_needs_review:
    (severity-weighted strike count within the grace window, overload).
    Severe=3, serious=2 per strike; overload = dropped / (approved + 1), the
    same ratio _has_firing_signal thresholds at 0.3. Primary sort = the report
    evidence, secondary = how far behind they've fallen -- so a
    reported+overloaded agent beats a report-only one, and an unreported
    overload alone ranks below any real report. 'minor'/'major' reports carry
    no firing weight (see _report_strikes)."""
    strikes = _report_strikes(state, agent_id, now_ms)
    score = strikes['severe'] * 3 + (strikes['count'] - strikes['severe']) * 2
    a = (state.get('agents') or {}).get(agent_id) or {}
    dropped = a.get('droppedCount') or 0
    approved = a.get('approvedCount') or 0
    return score, dropped / (approved + 1)


def who_needs_review(state, now_ms):
    """firing.js whoNeedsReview: the worker (non-admin, non-director) with the
    strongest firing signal -- a negative report or a real drop-off -- who is
    not already settled. Re-keyed OFF the raw morale score (a
    neglected-but-otherwise-fine agent is a hiring/help target, not a firing
    one); see _has_firing_signal. Among multiple signal-holders the review
    targets the strongest evidence first (_firing_signal_strength), not roster
    order. Same idle guard -- never pick someone mid-task/pair/handoff, so a
    fire can't strand a live collaboration ref."""
    best = None
    best_strength = None
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin') or d.get('isDirector'):
            continue
        if _review_is_stale(state, d.get('id'), now_ms):
            continue
        a = (state.get('agents') or {}).get(d.get('id'))
        if not a or a.get('busy') or a.get('task') or a.get('pairWith') or a.get('handoff'):
            continue
        if not _has_firing_signal(state, d.get('id'), now_ms):
            continue
        strength = _firing_signal_strength(state, d.get('id'), now_ms)
        if best is None or strength > best_strength:
            best = d
            best_strength = strength
    return best


def _firing_consultation(state, candidate_id):
    """firing.js firingConsultation: the people with real firsthand knowledge of
    the candidate -- distinct agents who filed reports against them, plus anyone
    currently paired with them or handing off work to them."""
    reports = reports_about(state, candidate_id)
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    reported_by = set(r.get('fromId') for r in reports if r.get('fromId') and r.get('fromId') != 'player')
    reporters = []
    for rid in reported_by:
        rdef = next((x for x in roster if x.get('id') == rid), None)
        own = [r for r in reports if r.get('fromId') == rid]
        reporters.append({
            'id': rid, 'name': (rdef or {}).get('name') or rid,
            'quote': ' | '.join(r.get('quote') or '' for r in own),
            'note': ' | '.join(r.get('note') or '' for r in own),
            'severity': ','.join(r.get('severity') or 'unclassified' for r in own),
        })
    coworkers = []
    for other in roster:
        if other.get('id') == candidate_id:
            continue
        o = agents.get(other.get('id'))
        if not o:
            continue
        if o.get('pairWith') == candidate_id or o.get('handoff') == candidate_id:
            coworkers.append({'id': other.get('id'), 'name': other.get('name') or other.get('id')})
    return {'reporters': reporters, 'coworkers': coworkers}


def _is_negative_severity(severity):
    s = (severity or '').lower()
    return 'severe' in s or 'serious' in s or 'major' in s


def _is_strike_severity(severity):
    """A report severity that COUNTS toward the two-strike firing bar. Only
    serious/severe -- real harm or real misconduct. 'minor' (no harm) and
    'major' (friction, underperformance) never count as a strike, so small
    things can't snowball into a firing; underperformance is a drop-off signal
    and a help target, not a strike."""
    s = (severity or '').lower()
    return 'severe' in s or 'serious' in s


def _report_strikes(state, agent_id, now_ms=None):
    """The firing-relevant review evidence for an agent, windowed: serious and
    severe negative reports that landed INSIDE REPORT_STRIKE_WINDOW_MS of the
    moment the evidence is weighed. A report without a `ts` is treated as fresh
    (state-seeded reports and pre-window records both count), so a hard fire
    never slips through a missing timestamp. Returns {'count', 'severe',
    'freshest'} where 'count' is the total strikes in the window and 'severe'
    the subset that were severe. `now_ms` injectable for deterministic tests."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    strikes = []
    severe = 0
    freshest = 0
    for r in reports_about(state, agent_id):
        if not _is_strike_severity(r.get('severity')):
            continue
        ts = r.get('ts')
        if ts is not None and now - ts > REPORT_STRIKE_WINDOW_MS:
            continue
        strikes.append(r)
        if _is_severe(r.get('severity')):
            severe += 1
        freshest = max(freshest, ts or 0)
    return {'count': len(strikes), 'severe': severe, 'freshest': freshest}


def _is_severe(severity):
    s = (severity or '').lower()
    return 'severe' in s


def _consultation_blocks_firing(state, candidate_id, now_ms):
    """firing.js consultationBlocksFiring: two independent guardrails against
    premature firing -- an active collaborator, or no corroborated strike
    evidence inside the grace window (a severe strike alone justifies; two
    serious/serious+ strikes are required otherwise)."""
    info = _firing_consultation(state, candidate_id)
    if info['coworkers']:
        return True, info
    strikes = _report_strikes(state, candidate_id, now_ms)
    if not strikes['count']:
        return True, info
    if strikes['severe']:
        return False, info
    return strikes['count'] < 2, info
