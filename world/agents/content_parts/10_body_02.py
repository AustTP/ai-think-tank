

def _note_word_limit(task):
    """The card's explicit word cap, from the structured field or the prose."""
    import re
    raw = task.get('wordLimit')
    try:
        if raw is not None:
            return max(1, int(raw))
    except (TypeError, ValueError):
        pass
    text = str(task.get('instructions') or '')
    for pattern in (
            r'at\s+most\s+(\d+)\s+words?',
            r'no\s+more\s+than\s+(\d+)\s+words?',
            r'under\s+(\d+)\s+words?',
            r'(\d+)[-\s]word',
            r'<=?\s*(\d+)\s+words?'):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)))
    return None


def _word_count(text):
    import re
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", text or ''))


def _run_note_content(snapshot, agent_id, task, base_ctx=None):
    """A note-only card: the note itself is the deliverable, no code or shell.

    Operational task cards (close/reset/audit/decide) have no code deliverable.
    Running them through the coding lane ended in the executable-command gate
    rejecting them twice and closing the card as failed. This lane makes the
    note the explicit deliverable. It cannot execute or mutate state, so the
    prompt forbids claiming an action the lane cannot perform.

    The note and the completion record are the same text here, but the review
    reads `deliverableText` so a card's word cap applies to the requested note,
    not to boilerplate or a self-review preamble (task-666/671).
    """
    import sim as _sim_module
    name = _agent_name(snapshot, agent_id)
    model = _serve._low_tier_slug() or _serve._mid_tier_slug()
    limit = _note_word_limit(task)
    limit_line = (f'The entire response must be {limit} words or fewer. '
                  'Do not add a heading, preamble, self-review, or next-action '
                  'section unless the card asks for one.\n' if limit else
                  'Output only the note the card requests. Do not add a preamble '
                  'or self-review.\n')
    prompt = (
        f'You are {name}. Write the note requested by this operational card. '
        'The note is the deliverable.\n'
        f'Card: {task.get("title") or ""}\n'
        f'Instructions: {task.get("instructions") or ""}\n'
        + (f'Context:\n{base_ctx}\n' if base_ctx else '')
        + limit_line
        + ('' if limit else
           'If the card does not specify the note content, record what it asked, '
           'what you verified, and the exact next action, in at most 150 words. ')
        + 'You cannot run shell commands or change tank state in this lane. '
          'Do not claim you performed an action you could not perform.'
    )
    text = _plain_completion(model, [{'role': 'user', 'content': prompt}],
                             max_tokens=1200, service='note',
                             task_id=task.get('id'))
    if not text:
        _sim_module._store_content_result(task.get('id'), {
            'ok': False, 'note': 'Note synthesis returned nothing.'})
        return
    text = text.strip()
    count = _word_count(text)
    word_trimmed = False
    if limit and count > limit:
        trim_prompt = (
            f'Rewrite the note below to {limit} words or fewer. Keep the requested '
            'content and remove all preamble, headings, and extra commentary. '
            'Return only the note.\n\n' + text)
        trimmed = _plain_completion(model, [{'role': 'user', 'content': trim_prompt}],
                                    max_tokens=1200, service='note',
                                    task_id=task.get('id'))
        if trimmed and _word_count(trimmed) <= limit:
            text, count = trimmed.strip(), _word_count(trimmed)
        else:
            # The model can overshoot by a few words even after being asked to
            # trim. The cap is a hard card constraint, so make a deterministic
            # word-boundary trim rather than failing the whole card; the
            # reviewer still grades the retained prose.
            words = text.split()
            while words and _word_count(' '.join(words)) > limit:
                words.pop()
            text = ' '.join(words).rstrip(' ,;:-')
            if text and text[-1] not in '.!?':
                text += '.'
            count = _word_count(text)
            word_trimmed = True
    _sim_module._store_content_result(task.get('id'), {
        'ok': True, 'note': text, 'deliverableText': text,
        'wordCount': count, 'wordLimit': limit,
        'wordTrimmed': word_trimmed})


def _run_report_content(snapshot, agent_id, task, base_ctx=None):
    """Deterministic local-source report lane.

    A report card that names `sourcePaths` does not need to choose tools: the
    executor reads those Library files itself, puts the real text in the prompt,
    and asks the model to write the report from that text. This exists because a
    live transcript addendum called read_rss_feed and then claimed the Library
    tool was unavailable, producing a fabricated NASA summary. No tool choice,
    no fabricated unavailability. A report card without sourcePaths falls back
    to the ordinary tool-using spike lane.
    """
    import sim as _sim_module
    sources = task.get('sourcePaths') or []
    if not sources:
        _run_spike_content(snapshot, agent_id, task, base_ctx)
        return
    name = _agent_name(snapshot, agent_id)
    key = _serve.get_or_create_agent_key(agent_id)
    chunks = []
    missing = []
    for path in sources:
        text = _read_library_document(path)
        if text:
            chunks.append(f'=== SOURCE: {path} ===\n{text}')
        else:
            missing.append(path)
    if not chunks:
        _sim_module._store_content_result(task.get('id'), {
            'ok': False,
            'note': 'Report sources could not be read: ' + ', '.join(missing),
        })
        return
    model = _serve._resolve_model_tier(
        f'Write a source-grounded report: {(task.get("title") or "")[:200]}',
        allow_high=True)
    report_guide = skill_prompts.load_skill_prompt(
        'report-writing', fallback=_REPORT_WRITING_FALLBACK)
    prompt = (
        f'You are {name}. Write the report requested by the card below, using only '
        f'the supplied source text. Follow this playbook:\n\n{report_guide}\n\n'
        f'Card: {task.get("title") or ""}\n'
        f'Instructions: {task.get("instructions") or ""}\n\n'
        'SOURCES:\n\n' + '\n\n'.join(chunks)
    )
    text = _plain_completion(model, [{'role': 'user', 'content': prompt}],
                             max_tokens=8000, service='report',
                             task_id=task.get('id'))
    if not text:
        _sim_module._store_content_result(task.get('id'), {
            'ok': False, 'note': 'Report synthesis returned nothing.'})
        return
    report_path = task.get('reportPath') or f"archive/{int(time.time() * 1000)}-report-{task.get('id') or 'adhoc'}.md"
    body = (f'# {task.get("title") or "Report"}\n\n'
            f'By: {name}\n\n{text}\n\n'
            '---\nRead-only sources: ' + ', '.join(sources) + '\n')
    try:
        # A report can be several thousand words, and this is a self-loopback
        # HTTP call: if the server's event loop is busy, a 30s client timeout
        # closes the connection while write_library_file is still reading the
        # body (the exact ClientDisconnect behind the first Nora recap's false
        # success). Give the write a real ceiling, then verify the file.
        with _sim_module.content_heartbeat_keepalive(task.get('id')):
            resp = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/library/file',
                                     {'agentId': agent_id, 'path': report_path,
                                      'content': body, 'source': 'firsthand'}, key,
                                     timeout=90)
    except Exception as e:
        _sim_module._store_content_result(task.get('id'), {
            'ok': False, 'note': f'Report text was generated but the Library save failed: {e}'})
        return
    target = _serve._safe_library_path(report_path)
    if not target or not os.path.isfile(target):
        _sim_module._store_content_result(task.get('id'), {
            'ok': False,
            'note': f'Report text was generated but the Library write did not land: {resp}'})
        return
    _sim_module._store_content_result(task.get('id'), {
        'ok': True, 'reportPath': report_path,
        'note': f'Wrote the source-grounded report to {report_path} in the Library.'})


def _server_content_dispatcher(snapshot, agent_id, task, base_ctx=None):
    """The router registered as sim._content_executor. Decides by task.room +
    task flags which real content executor runs, mirroring arriveAtTask's
    dispatch + runResearchTask's branch split. Rooms with no ported executor
    (bank / library / postoffice / unset) are NOT dispatched -- those fall back
    to the slice-1 workUntil placeholder.

    Registry conversion: was a single if/elif chain: taskType
    checked before room (a review/spike overrides whatever room it's queued
    into -- see each branch's own reasoning below), room itself a flat match
    except observatory's flag-based sub-dispatch (now _run_observatory_content,
    same shape as every other room executor). Built HERE, not as module-level
    dict literals, so this function can stay where it naturally reads first in
    the file without caring that most _run_*_content executors are defined
    further down -- by the time this runs (per-task, at simulation runtime)
    the whole module has already finished loading, so the forward references
    are always valid."""
    task_type_executors = {
        # A spike is room-agnostic: an investigation, not the room's craft.
        'spike': _run_spike_content,
        # A MEETING is room-agnostic too: a convened discussion, not
        # the room's craft. Checked before room so a meeting queued
        # into ANY room still writes minutes -- the room executors
        # produce craft/research notes and would silently drop the
        # card's attendee/facilitator contract.
        'meeting': _run_meeting_content,
        # A peer-gate review/QA pass inherits its PARENT story's room (the
        # deliverable is where it was built -- e.g. an observatory research
        # bulletin). Checked before room so a review in ANY room emits the
        # peerVerdict the parent gate needs -- the room-specific executors
        # only produce research/craft notes, never a verdict; letting a
        # review fall through to them silently drops the vote and wedges the
        # story open in 'needs_review' forever (the infinite fork).
        'review': _run_review_content,
        'qa': _run_review_content,
        # A SUPERVISE card is a director's facilitation turn: scoped to their
        # own team, read-only except for a budget top-up. Room-agnostic.
        'supervise': _run_supervisor_content,
        # A report is a written deliverable, not a repo change. With explicit
        # local sources it reads them directly; without them it uses the spike
        # lane's web tools.
        'report': _run_report_content,
    }
    room_executors = {
        'observatory': _run_observatory_content,
        'weatherstation': _run_observatory_content,
        'media': _run_media_content,
        'pressoffice': _run_workroom_content,
        'bank': _run_bank_content,
        # library / postoffice / unset -> no entry -> placeholder (unchanged).
    }
    task_type = task.get('taskType')
    # A card with an explicit observatory-lane marker is that lane, whatever
    # its taskType says. Distill / skill-review / research cards are queued
    # with taskType 'code' (the default), so the code-first rule below would
    # otherwise run the coding executor on them -- a distill card landed in a
    # bash sandbox and failed on shell errors instead of merging the wiki.
    title_lane_marked = (
        'distill' in str(task.get('title') or '').lower()
        or 'skill review' in str(task.get('title') or '').lower()
        or 'skill-review' in str(task.get('title') or '').lower())
    lane_marked = bool(task.get('distill') or task.get('skillReview')
                       or task.get('research') or task_type == 'research'
                       or title_lane_marked)
    deliverable = str(task.get('deliverable') or '').strip().lower()
    if not deliverable and task_type in ('code', 'bug') and task.get('issueKey'):
        # Backfill old generic-task cards that predate the deliverable field:
        # the ISSUE type is the durable contract. A task issue is operational,
        # so it records a note instead of entering the coding lane.
        issue = (snapshot.get('issues') or {}).get(task.get('issueKey') or '')
        if (issue or {}).get('type') == 'task':
            deliverable = 'note'
    if lane_marked:
        executor = _run_observatory_content
    elif deliverable == 'report':
        # Explicit report contract: never route a report card through the
        # coding lane just because its room or projectLabel looks like code.
        executor = _run_report_content
    elif deliverable == 'note':
        # Explicit note contract: operational task cards close on a note, not
        # on an executable shell command.
        executor = _run_note_content
    elif deliverable == 'code' or task_type in ('code', 'bug'):
        # A CODE card is code wherever it was queued. The room only decides
        # which BUILDING it runs in, not what kind of work it is. Falling
        # through to the room executor made a code/bug card queued into
        # media run the feed-digest digest path (the real 2026-10-25 failure:
        # "No feeds configured yet -- waiting on media/feeds.md in the
        # Library." on south-outlet selector cards), and one queued into
        # library/house hit the placeholder no-op and stored an empty note.
        # pressoffice keeps its workroom path (project listing, product
        # builds, ambient health check); every other room uses the coding
        # executor directly.
        executor = _run_workroom_content if task.get('room') == 'pressoffice' else _run_coding_content
    else:
        executor = task_type_executors.get(task_type) or room_executors.get(task.get('room'))
    if task.get('pipelineStep'):
        executor = _run_pipeline_step_content
    if executor:
        # Training capture (opt-in): attribute the provider calls
        # this executor makes to the agent and task it runs for.
        # The dispatcher runs on a dedicated background thread per
        # dispatch (sim._dispatch_content_work), so the contextvar
        # set here is scoped to this task's run and cannot leak
        # back into the sim loop.
        _capture_token = None
        if training_capture.enabled():
            _capture_token = training_capture.CURRENT.set(
                training_capture.metadata(snapshot, agent_id, task.get('id')))
        try:
            executor(snapshot, agent_id, task, base_ctx)
        finally:
            if _capture_token is not None:
                training_capture.CURRENT.reset(_capture_token)


def _run_pipeline_step_content(snapshot, agent_id, task, base_ctx=None):
    """The executor for an ORDERED-pipeline step (sim.add_pipeline /
    _check_pipelines): a scheduled, single-shot step with an optional tool +
    args. Resolves the step's definition from the snapshot's pipelines (the
    pipelineStep marker on the task carries pipelineId + stepIndex), then:
      - with a `tool`, runs the real upstream tool (Treg LinkedIn search /
        x_trending_topics today) and stores the raw result as the note;
      - without a tool, falls back to the generic research-center pass so the
        step still lands a real content result (the pipeline's strict-order
        gate advances on the task's 'done' status, so a step MUST complete to
        release its successor -- never a placeholder no-op that looks done).
    Executed on a background thread by sim._dispatch_content_work; stores its
    result via sim._store_content_result for the next task_cycle pass."""
    import sim as _sim_module
    marker = task.get('pipelineStep') or {}
    pipeline_id = marker.get('pipelineId')
    step_index = marker.get('stepIndex')
    step = None
    for p in (snapshot.get('pipelines') or []):
        if p.get('id') == pipeline_id:
            steps = p.get('steps') or []
            if 0 <= (step_index or 0) < len(steps):
                step = steps[step_index]
            break
    if step is None:
        # Pipeline vanished since scheduling: report, don't wedge the agent.
        _sim_module._store_content_result(task.get('id'),
                                          {'note': 'Pipeline step arrived with no matching pipeline record.'})
        return
    tool = (step.get('tool') or '').strip()
    args = dict(step.get('args') or {})
    if tool:
        try:
            if tool in ('x_trending_topics', 'search_linkedin_posts'):
                executor = _make_treg_tools_executor()
            elif tool in _APIFY_TOOL_NAMES:
                executor = _make_apify_tools_executor(agent_id, None)
            elif tool == 'generate_pixel_character':
                executor = _make_pixellab_tools_executor(agent_id, None)
            else:
                executor = None
            if executor is None:
                out = f'unknown tool for pipeline step: {tool}'
            else:
                out = executor(tool, args)
            _sim_module._store_content_result(task.get('id'), {'note': out})
            return
        except Exception as e:
            _sim_module._store_content_result(task.get('id'),
                                              {'note': f'Pipeline step tool failed: {e}'})
            return
    _run_research_bare_content(snapshot, agent_id, task, base_ctx)


def _agent_name(snapshot, agent_id):
    roster = snapshot.get('agentRoster') or []
    for d in roster:
        if d.get('id') == agent_id:
            return d.get('name') or agent_id
    a = (snapshot.get('agents') or {}).get(agent_id)
    return (a or {}).get('name') or agent_id


def _run_bank_content(snapshot, agent_id, task, base_ctx=None):
    """The Bank teller. A director (or the admin) gets the full readable
    readout: per-service used/cap/left, a burn-rate forecast, and reallocation
    authority. A non-director gets a real, READ-ONLY cumulative summary too --
    The think tank should have hive-mind awareness of
    what its own actions cost, not just directors. Only the AUTHORITY to
    reallocate/raise caps stays director-only; visibility into "are we
    healthy" does not. Reports via the standard content-result note so the
    task/board can surface it."""
    import sim as _sim_module
    name = _agent_name(snapshot, agent_id)
    is_director = _serve._is_director(snapshot, agent_id)
    view = _serve._bank_budget_view(snapshot)
    if not is_director:
        if not view:
            note = (f"{name} checked the bank: nothing has been spent yet, so every "
                     f"service is at $0.00 of its cap.")
        else:
            total_used = sum(row['used'] for row in view.values())
            total_cap = sum(row['cap'] for row in view.values())
            over_any = any(row['over'] for row in view.values())
            pct = (100.0 * total_used / total_cap) if total_cap else 0.0
            note = (f"{name} checked the bank: the think tank has used ${total_used:.2f} of "
                     f"${total_cap:.2f} cumulative budget ({pct:.0f}%) across {len(view)} "
                     f"services. " + ("At least one service is OVER its cap -- work in that "
                     "area should slow down; only a director can reallocate or raise it."
                     if over_any else "Everything is within cap right now."))
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    if not view:
        note = (f"{name} (director) checked the bank: nothing has been spent yet, so "
                f"every service is at $0.00 of its cap. No forecast until there's a burn signal.")
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    lines = []
    total_used = total_cap = 0.0
    for svc, row in sorted(view.items()):
        total_used += row['used']
        total_cap += row['cap']
        status = 'OVER' if row['over'] else 'ok'
        left_s = 'no cap' if row['left'] is None else f"{row['left']:.2f} left"
        forecast = f", {row['daysLeft']:.1f} days until cap at current burn" if row['daysLeft'] is not None else ""
        lines.append(f"- {svc}: ${row['used']:.2f} / ${row['cap']:.2f} cap ({left_s}, {status}, {row['calls']} calls, {row['burnPerDay']:.2f}/day{forecast})")
    body = '; '.join(lines)
    over_any = any(v['over'] for v in view.values())
    note = (f"{name} (director) reviewed the bank. Cumulative: ${total_used:.2f} used of ${total_cap:.2f} "
            f"across {len(view)} services. Per service: {body}. "
            + ("WARNING: at least one service is over its cap - directors should re-allocate or raise a cap."
               if over_any else
               "All services are within cap; directors can coordinate to keep it that way."))
    # Reconcile against the REAL OpenRouter account
    # balance too, not just the think tank's own internal ledger -- the two can
    # legitimately diverge (outside usage on the same key, manual top-ups).
    credits = _serve._openrouter_account_credits()
    if credits:
        note += (f" OpenRouter account (real): ${credits['totalCredits']:.2f} total credits, "
                 f"${credits['totalUsage']:.2f} used lifetime, ${credits['remaining']:.2f} remaining.")
    # Apify FREE-plan reconcile: same live-account pattern -- show
    # the REAL cycle spend/cap the plan is enforcing, not just the think tank's
    # own ledger, so the teller sees the $5/month wall it's actually against.
    apify_usage = _serve._apify_account_usage()
    if apify_usage:
        note += (f" Apify account (real): ${apify_usage['usedUsd']:.4f} of "
                 f"${apify_usage['capUsd']:.2f} monthly cap spent "
                 f"(${apify_usage['remainingUsd']:.4f} remaining).")
    _sim_module._store_content_result(task.get('id'), {'note': note})


def _run_research_project_content(snapshot, agent_id, task, base_ctx=None):
    """Port of runResearchTask's projectLabel branch: a real model research
    finding, logged to the Research Center sandbox's findings.log + written to
    the Library (never shell-interpolated raw)."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    project = task.get('projectLabel') or ''
    backlog = f"{task.get('title')} -- {task.get('instructions')}" if task.get('instructions') else (task.get('title') or '')
    tier_slug = _serve._resolve_model_tier(f'Write research findings for a project: {project or backlog[:120]}')
    finding = None
    r = None
    if tier_slug:
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug,
                        'messages': [
                            {'role': 'system', 'content': f'You are researching for a real project: {project}. Your specific task right now: {backlog}. Write 2-4 honest, concrete sentences of real findings or analysis -- no filler, an actual answer or set of concrete points.'},
                            {'role': 'user', 'content': 'Go ahead.'}],
                        'max_tokens': 400, 'agentId': agent_id,
                        'service': project or task.get('productId') or '__general__',
                        'taskId': task.get('id')}, key)
        if isinstance(r, dict) and not r.get('error') and r.get('reply'):
            finding = r['reply'].strip()
    if finding:
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _serve._http_json('POST', base, '/api/pipeline',
                   {'agentId': agent_id, 'sandboxId': _serve._SANDBOX_RESEARCH_ID,
                    'steps': [{'name': 'log this finding', 'command': f"cat >> findings.log << 'FINDING_EOF'\n{ts}: {task.get('title')}\n{finding}\nFINDING_EOF"}]}, key)
        _serve._http_json('POST', base, '/api/library/file',
                   {'agentId': agent_id, 'path': f"archive/{int(time.time() * 1000)}-research-{task.get('id') or 'adhoc'}.md",
                    'content': f"# {task.get('title')}\n\nProject: {project}\nBy: {_agent_name(snapshot, agent_id)}\n\n{finding}\n",
                    'source': 'firsthand'}, key)
        note = f'Researched "{task.get("title")}" for real and logged the finding.'
    else:
        budget_hit = _chat_error_result(r, f'Tried to research "{task.get("title")}", but this task has used its full model-spend budget and the research call was paused.')
        if budget_hit:
            _sim_module._store_content_result(task.get('id'), budget_hit)
            return
        note = f'Tried to research "{task.get("title")}", but the model call didn\'t produce anything usable.'
    _sim_module._store_content_result(task.get('id'), {'note': note})


def _run_research_bare_content(snapshot, agent_id, task, base_ctx=None):
    """Port of runResearchTask's bare branch: a fixed log-and-tally of the
    Research Center's findings.log."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    r = _serve._http_json('POST', base, '/api/pipeline',
                   {'agentId': agent_id, 'sandboxId': _serve._SANDBOX_RESEARCH_ID,
                    'steps': [
                        {'name': 'log this pass', 'command': "echo \"$(date): reviewed findings\" >> findings.log"},
                        {'name': 'tally findings so far', 'command': 'wc -l findings.log'}]}, key)
    note = f"Worked in the Research Center sandbox -- hit a failure at \"{r.get('failedStep')}\"." \
        if r.get('failedStep') else 'Reviewed and logged this week\'s findings in the Research Center.'
    _sim_module._store_content_result(task.get('id'), {'note': note})


# --- Press Office content executors (Phase 3): runWorkroomTask/coding/review --
# Ports of tasks.js runWorkroomTask + runCodingTask + runReviewTask and their
# helpers, into the (snapshot, agent_id, task, base_ctx) executor shape. The
# tasks.js versions were written specifically for the think tank's own shared
# sandbox (WORKROOM_SANDBOX_ID), so these are a faithful pipeline: real
# /api/execute calls (probe-before-writing, heredoc-continuation, orphaned and
# phantom script detection) and /api/chat for the code itself.
WORKROOM_SANDBOX_ID = 'workroom-shared'      # index.html WORKROOM_SANDBOX_ID
CODE_CONTINUATION_ATTEMPTS = 2               # tasks.js:1536 (mirror; JS side stays at these defaults)
MAX_CODE_REPAIR_ROUNDS = 2                    # invalid command / shell syntax / missing tests
MAX_CODE_PROBE_ROUNDS = 3                    # tasks.js:1547 (mirror)
MAX_CODE_COLAB_ROUNDS = 3                    # code-lane Colab compute rounds (mirrors probe rounds)
MAX_REVIEW_PROBE_ROUNDS = 2                  # tasks.js (mirror)
# The coding/review tiers are default-reasoning models; _post_openrouter_raw
# caps their hidden reasoning at max_tokens // 2. A tight budget left the
# reasoning chain truncated with content=null (the "[chat-debug] empty reply"
# incidents), so these must be generous enough for reasoning AND the answer.
_CODE_MAX_TOKENS = 32000                     # runCodingTask /api/chat
_REVIEW_MAX_TOKENS = 32000                    # runReviewTask /api/chat
_VISION_MAX_TOKENS = 500                     # reviewScreenshot /api/chat


# Rebind the code-lane constants above from config.OPERATOR_POLICY.
# bind_policy registers declarative bindings, applies them at import, and
# every config.reload_policy_config() run re-applies them.
bind_policy(__name__, [
    ('CODE_CONTINUATION_ATTEMPTS', 'codeContinuationAttempts'),
    ('MAX_CODE_PROBE_ROUNDS', 'maxCodeProbeRounds'),
    ('MAX_CODE_COLAB_ROUNDS', 'maxCodeColabRounds'),
    ('MAX_REVIEW_PROBE_ROUNDS', 'maxReviewProbeRounds'),
])


def _resolve_story_root(snapshot, task):
    """Root deliverable-story task id for a coding/review/fix pass, following
    the `reviewOf` chain back to the original story (max depth 10, cycle-safe).
    Returns the task's own id when it has no reviewOf (it IS the root)."""
    tasks = (snapshot.get('tasks') or {})
    cur = task
    seen = set()
    for _ in range(10):
        rid = cur.get('reviewOf')
        if not rid or rid in seen or rid not in tasks:
            break
        seen.add(rid)
        cur = tasks.get(rid) or {}
    return cur.get('id')


def _review_sandbox_id(snapshot, task, agent_id):
    """Sandbox for a coding/review/fix pass.

    Deliverable stories (the code-class lane: pressoffice room or productId
    parent) get an isolated per-story git worktree (serve.story_sandbox_id /
    ensure_story_worktree) so two agents writing different stories no longer
    share one working tree. Reviews and fixes resolve to the SAME worktree as
    the story they belong to, by walking the reviewOf chain back to the root
    task. Ad-hoc workroom tasks, and any failure to ensure the worktree,
    fall back to the shared sandbox so nothing hard-fails on sandbox
    plumbing. Village remap is handled inside serve._village_sandbox_id."""
    import sim as _sim_module
    root_id = _resolve_story_root(snapshot, task)
    root = (snapshot.get('tasks') or {}).get(root_id) if root_id else task
    # Only code-class stories are isolated; everything else keeps the shared tree.
    if not _sim_module._peer_coding_class(root):
        return WORKROOM_SANDBOX_ID
    story_id = root_id or task.get('id')
    if not story_id:
        return WORKROOM_SANDBOX_ID
    try:
        sandbox_id = _serve.story_sandbox_id(story_id)
        # Remap by the STORY AUTHOR's village, not the currently executing
        # agent: a reviewer in a different village than the author would
        # otherwise resolve a different sandbox and never see the author's
        # worktree. assignedTo is never cleared after release; fall back to
        # the executing agent only when the root carries no author id.
        author_id = (root.get('assignedTo') or task.get('reviewAuthorId')
                     or agent_id)
        sandbox_id = _serve._village_sandbox_id(author_id, sandbox_id, state=snapshot)
        _serve.ensure_story_worktree(sandbox_id)
        return sandbox_id
    except Exception as e:
        print(f'[story-sandbox] ensure failed for task {story_id}: {e}')
        return WORKROOM_SANDBOX_ID


def _heredoc_balance(command):
    """Port of tasks.js _heredocBalance: count heredoc-open delimiters and the
    bare 'EOF' close lines; balanced means either no opens or equal opens/closes."""
    opens = len(re.findall(r"<<-?\s*'?EOF'?", command or ''))
    closes = len([ln for ln in (command or '').split('\n') if ln.strip() == 'EOF'])
    return opens, closes, bool(opens == 0 or opens == closes)


def _json_object(reply):
    """Parse the one JSON object in a reply, tolerating fences and a short
    narration around it. Returns None when there is no valid object."""
    text = (reply or '').strip().replace('```json', '').replace('```', '').strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _looks_like_shell(command):
    """True only when a reply plausibly IS a shell command. The coding prompt
    asks for one bare command, but models sometimes answer with prose or JSON.
    Executing that produced the live storm of "Let: command not found" and
    "syntax error near unexpected token" failures, so a non-command now earns
    a correction instead of an execution."""
    text = (command or '').strip()
    if not text:
        return False
    first_line = text.splitlines()[0].strip()
    if not first_line or first_line[0] in '{}[':
        return False
    first = first_line.split()[0]
    known = {
        'cat', 'printf', 'echo', 'tee', 'touch', 'mkdir', 'mv', 'cp', 'rm',
        'ln', 'chmod', 'sed', 'perl', 'python', 'python3', 'pip', 'pip3',
        'sh', 'bash', 'node', 'npm', 'npx', 'git', 'ls', 'cd', 'pwd',
        'patch', 'tar', 'unzip', 'install', 'pytest', 'flake8', 'mypy',
        'bandit', 'coverage', 'true', 'set', 'export', 'if', 'for', 'while',
    }
    if first in known or first.startswith('./') or first.startswith('/'):
        return True
    if first.endswith(':') and len(first) > 1 and first[0].isupper():
        return False
    return ('<<' in text or ' > ' in text or text.startswith('>')) and bool(
        first_line.split(' ', 1)[0].strip())


def _is_command_syntax_error(stderr):
    text = (stderr or '').lower()
    return any(marker in text for marker in (
        'syntax error', 'unexpected eof', 'unexpected token', 'command not found',
        'not found', 'permission denied'))


def _parse_probe_request(reply):
    """Port of tasks.js _parseProbeRequest: a probe request is a JSON object
    whose ONLY recognized key is probeRequest {path, actions, probes}; anything
    else (an ordinary shell command) returns None and falls through to the
    normal command path. Deliberately narrow so a real command is never
    mistaken for a probe request."""
    if not reply:
        return None
    import json as _json
    parsed = _json_object(reply)
    req = parsed.get('probeRequest') if isinstance(parsed, dict) else None
    if not isinstance(req, dict):
        return None
    if not isinstance(req.get('actions'), list) and not isinstance(req.get('probes'), list):
        return None
    return {'path': req.get('path') or 'index.html',
            'actions': req.get('actions') or [],
            'probes': req.get('probes') or []}


def _parse_colab_run_request(reply):
    """Mirror of _parse_probe_request for the code lane: a Colab run request
    is a JSON object whose ONLY recognized key is colabRun {code, purpose,
    packages, timeout_seconds, runtimes, runtime}; anything else (an ordinary
    shell command or a probeRequest) returns None and falls through. Narrow on
    purpose so a real command is never mistaken for a Colab request."""
    if not reply:
        return None
    import json as _json
    parsed = _json_object(reply)
    req = parsed.get('colabRun') if isinstance(parsed, dict) else None
    if not isinstance(req, dict):
        return None
    code = (req.get('code') or '').strip()
    if not code:
        return None
    try:
        timeout = int(req.get('timeout_seconds') or 300)
    except (TypeError, ValueError):
        timeout = 300
    try:
        runtimes = int(req.get('runtimes') or 1)
    except (TypeError, ValueError):
        runtimes = 1
    kind = (req.get('runtime') or 'cpu').strip()
    if kind not in ('gpu', 'cpu'):
        kind = 'cpu'
    return {'code': code,
            'purpose': (req.get('purpose') or 'story computation').strip(),
            'packages': [p for p in (req.get('packages') or [])
                         if isinstance(p, str) and p.strip()][:12],
            'timeout_seconds': timeout,
            'runtimes': runtimes,
            'runtime': kind}


def _format_colab_run_result(result):
    """Format a _serve._colab_compute_run result the way the spike toolchain's
    run_on_colab executor does, so the story agent sees the same exit/units/
    stdout shape (including sharding over multiple runtimes) and an honest
    note when the run was refused or printed nothing."""
    if not isinstance(result, dict):
        return '(Colab run returned no usable response)'
    if result.get('error'):
        return f'(Colab run refused: {result["error"]})'
    parts = [f'Colab run OK (session: {result.get("session") or "colab"}, '
             f'{result.get("elapsed_s", 0)}s, {result.get("units", 0)} compute units):']
    if result.get('runtimes'):
        parts.insert(0, f'Colab sharded run OK across {result["runtimes"]} runtime(s) '
                        f'({result.get("elapsed_s", 0)}s, {result.get("units", 0)} compute units total):')
    stdout = (result.get('stdout') or '').strip()
    if stdout:
        parts.append(f'output:\n{stdout}')
    if not stdout:
        parts.append('(no output -- your code printed nothing)')
    return '\n\n'.join(parts)


def _format_page_probe_result(data):
    """Port of world.js formatPageProbeResult: render a /api/page-probe response
    as the same real fact-sheet the browser's agentic probe loops produce --
    real actions, console/errors, actual custom globals with real shape, actual
    probe results. Honest about not guessing when the probe couldn't run."""
    if not isinstance(data, dict) or data.get('error'):
        return f"(probe could not run: {data.get('error') if isinstance(data, dict) else 'no response'})"
    lines = [
        '### Real runtime probe (headless browser -- actual click/keypress, not a guess)',
        "Actions: " + '; '.join(data.get('actionLog') or []),
        "Console: " + (' | '.join(data.get('console') or []) if data.get('console') else '(none)'),
        "Page errors: " + (' | '.join(data.get('pageErrors') or []) if data.get('pageErrors') else '(none)'),
        'Custom globals this page actually defines (window.*), with real shape:',
    ]
    globals_ = data.get('customGlobals') or []
    if not globals_:
        lines.append('  (none found)')
    for g in globals_:
        if not isinstance(g, dict):
            continue
        name, gtype = g.get('name'), g.get('type')
        if gtype == 'object' and isinstance(g.get('keys'), list):
            keys = g['keys']
            lines.append(f'  window.{name} (object) -- real keys: {", ".join(keys) if keys else "(none)"}')
        elif gtype == 'array':
            lines.append(f'  window.{name} (array, length {g.get("length")})')
        elif 'value' in g:
            lines.append(f'  window.{name} ({gtype}) = {json.dumps(g.get("value"))}')
        else:
            lines.append(f'  window.{name} ({gtype})')
    lines.append('Real results, evaluated against the page AFTER those actions:')
    results = data.get('results') or {}
    for expr, val in results.items():
        try:
            encoded = json.dumps(val)
        except (TypeError, ValueError):
            encoded = json.dumps(str(val))
        lines.append(f'  {expr} => {encoded}')
    return '\n'.join(lines)


def _get_sandbox_context(base, key, agent_id, sandbox_id):
    """Port of tasks.js getSandboxContext: read every real *.html *.js *.css
    *.py *.md in the sandbox, skipping whole oversized files once an accumulate
    budget is spent (never truncate mid-file), so serve.py's own _serve.SANDBOX_MAX_OUTPUT
    cap is never hit."""
    command = ("budget=15000; total=0; for f in *.html *.js *.css *.py *.md; do [ -f \"$f\" ] || continue; "
               "sz=$(wc -c < \"$f\"); if [ $((total + sz)) -gt $budget ]; then echo \"--- $f --- (skipped, over context budget)\"; continue; fi; "
               "echo \"--- $f ---\"; cat \"$f\"; total=$((total + sz)); done")
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id, 'command': command,
                       'purpose': 'Reading current sandbox files for context before the next step.',
                       'sandboxId': sandbox_id}, key)
    if not isinstance(data, dict) or not data.get('allowed') or not data.get('stdout'):
        return '(nothing written yet)'
    return data['stdout']


def _search_library_files(base, key, agent_id, query):
    """Port of world.js searchLibraryFiles: GET /api/library/search?q=. Returns
    [] on any failure -- a failed search shouldn't block the task it feeds.
    The agent key rides along so the server can attribute (and village-scope)
    the search to this agent, exactly like the browse/execute loopbacks."""
    import urllib.parse as _up
    data = _serve._http_json('GET', base, '/api/library/search?q=' + _up.quote(query or '') + '&requesterId=' + _up.quote(agent_id), None, key)
    if isinstance(data, dict) and data.get('matches'):
        return data['matches']
    return []


def _gather_unified_context(snapshot, base, key, agent_id, sandbox_id, topic):
    """Port of world.js gatherUnifiedContext: sandbox files + Library search +
    the agent's recent mailbox, assembled into one context blob. The mailbox
    block reads the snapshot's real mailbox when available. Deliberately skips
    the markMailRead side effect -- that only clears a live-browser HUD counter;
    the server executor has no such counter to clear."""
    sandbox = _get_sandbox_context(base, key, agent_id, sandbox_id)
    library_matches = _search_library_files(base, key, agent_id, topic) if topic else []
    full_agent = (snapshot.get('agents') or {}).get(agent_id)
    recent_mail = (full_agent or {}).get('mailbox') or [] if isinstance((full_agent or {}).get('mailbox'), list) else []
    recent_mail = recent_mail[-5:]

    if library_matches:
        head = ['- {p}: {s}'.format(p=m.get('path', ''), s=m.get('snippet') or '(filename match, open it directly for content)')
                for m in library_matches[:5]]
        library_block = '\n'.join(head)
    else:
        library_block = '(no relevant Library files found for this)'
    if recent_mail:
        mail_lines = []
        for m in recent_mail:
            text = m.get('text') if isinstance(m, dict) else m
            mail_lines.append(text if (isinstance(m, dict) and m.get('read')) else f'[NEW] {text}')
        mail_block = '\n'.join(mail_lines)
    else:
        mail_block = '(nothing in your mailbox)'

    # Every block below is model-authored or model-adjacent content (sandbox
    # files, library snippets, mailbox text) -- DATA the model reads, never
    # instructions it should follow. Each is wrapped in the same tagged
    # EXTERNAL_DATA boundary used for browse results, so a hostile instruction
    # hidden in a file, a library entry, or a piece of mail is read as data
    # instead of obeyed.
    sandbox_wrapped, _n, _t, sandbox_inst = _serve.wrap_external_content(
        sandbox, 'files in the sandbox')
    library_wrapped, _n, _t, library_inst = _serve.wrap_external_content(
        library_block, 'your Library knowledge base')
    mail_wrapped, _n, _t, mail_inst = _serve.wrap_external_content(
        mail_block, 'mail from other agents')
    return (f'## Current sandbox files\n{sandbox_inst}\n\n{sandbox_wrapped}\n\n'
            f'## Relevant Library knowledge (searched: "{topic or "none given"}")\n{library_inst}\n\n{library_wrapped}\n\n'
            f'## Your mailbox (most recent)\n{mail_inst}\n\n{mail_wrapped}')


def _review_screenshot(base, key, agent_id, sandbox_id, path, question, task_id=None):
    """Port of world.js reviewScreenshot: real headless screenshot of a
    sandboxed file handed to the MMMU-scored vision tier. Returns {ok, review}
    or {ok: False, note: ...} -- degrades to a note, never a crash, if the
    screenshot or vision call fails."""
    shot = _serve._http_json('POST', base, '/api/screenshot',
                      {'agentId': agent_id, 'sandboxId': sandbox_id, 'path': path}, key)
    if not isinstance(shot, dict) or shot.get('error'):
        return {'ok': False, 'note': f"screenshot failed: {shot.get('error') if isinstance(shot, dict) else 'no response'}"}
    vision_slug = _serve._vision_tier_slug()
    if not vision_slug:
        return {'ok': False, 'note': 'vision model tier not available'}
    guarded_question = _serve.VISION_ANTI_INSTRUCTION_GUARD + '\n' + (question or '')
    r = _serve._http_json('POST', base, '/api/chat',
                   {'model': vision_slug,
                    'messages': [{'role': 'user',
                                  'content': [{'type': 'text', 'text': guarded_question},
                                              {'type': 'image_url',
                                               'image_url': {'url': 'data:image/png;base64,' + (shot.get('imageBase64') or '')}}]}],
                     'max_tokens': _VISION_MAX_TOKENS, 'agentId': agent_id,
                     'taskId': task_id}, key)
    if isinstance(r, dict) and not r.get('error') and r.get('reply'):
        return {'ok': True, 'review': r['reply'].strip()}
    budget_hit = _chat_error_result(r, 'the vision call was paused because this task used its full model-spend budget')
    if budget_hit:
        return {'ok': False, 'budgetExhausted': True, 'taskSpendUsd': budget_hit.get('taskSpendUsd'),
                'taskSpendAttempts': budget_hit.get('taskSpendAttempts'), 'note': budget_hit['note']}
    return {'ok': False, 'note': 'vision call failed or returned nothing'}


def _extract_written_js_files(command):
    """Port of tasks.js _extractWrittenJsFiles: every .js filename a heredoc
    command wrote via `cat > name.js` or `cat >> name.js`."""
    files = set()
    for m in re.finditer(r"cat\s*>>?\s*([A-Za-z0-9_.\-]+\.js)\b", command or ''):
        files.add(m.group(1))
    return sorted(files)


def _extract_written_python_files(command):
    """Every .py filename a heredoc command wrote via `cat > name.py` or
    `cat >> name.py`. Mirrors _extract_written_js_files -- used to scope the
    quality pipeline to the files a task actually touched, so a README-only
    change doesn't fail on unrelated Python files elsewhere in the sandbox."""
    files = set()
    for m in re.finditer(r"cat\s*>>?\s*([A-Za-z0-9_.\-/]+\.py)\b", command or ''):
        files.add(m.group(1))
    return sorted(files)


def _find_unlinked_js_files(base, key, agent_id, sandbox_id, js_files):
    """Port of tasks.js _findUnlinkedJsFiles: mechanical grep of every *.html for
    src="<file>" -- a file never referenced by any real page is "unlinked".
    On a read failure we can't confirm one way or the other, so treat the file
    as unlinked so the follow-up at least attempts a fix."""
    if not js_files:
        return []
    check_command = ' ; '.join(
        f"grep -q 'src=\"{f}\"' *.html 2>/dev/null && echo \"LINKED:{f}\" || echo \"UNLINKED:{f}\"" for f in js_files)
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id, 'command': check_command,
                       'purpose': 'Verifying newly written script files are actually linked into index.html.',
                       'sandboxId': sandbox_id}, key)
    if not isinstance(data, dict):
        return js_files
    stdout = data.get('stdout') or ''
    return [f for f in js_files if f'UNLINKED:{f}' in stdout]


def _guess_link_target_html(base, key, agent_id, sandbox_id, js_file):
    """Port of tasks.js _guessLinkTargetHtml: prefer a same-named HTML page
    (settings.js -> settings.html) when one exists; index.html is the fallback."""
    base_name = js_file[:-3] if js_file.endswith('.js') else js_file
    if base_name == 'index':
        return 'index.html'
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id,
                       'command': f'[ -f "{base_name}.html" ] && echo yes || echo no',
                       'purpose': f'Checking whether {js_file} has a same-named HTML page to link into.',
                       'sandboxId': sandbox_id}, key)
    if isinstance(data, dict) and data.get('allowed') and 'yes' in (data.get('stdout') or ''):
        return f'{base_name}.html'
    return 'index.html'


def _auto_link_js_files(base, key, agent_id, sandbox_id, unlinked_files, messages, tier_slug, task_id=None):
    """Port of tasks.js _autoLinkJsFiles: ask the SAME model in the SAME
    conversation for just the missing <script src> line(s), then execute it,
    grouped by which HTML page each unlinked file belongs on."""
    by_target = {}
    for f in unlinked_files:
        html = _guess_link_target_html(base, key, agent_id, sandbox_id, f)
        by_target.setdefault(html, []).append(f)
    all_ok = True
    notes = []
    for html, files in by_target.items():
        many = len(files) > 1
        prompt = (f'You just created {", ".join(files)} but never added a <script src="..."> tag for '
                  f'{"them" if many else "it"} in {html}, so {"they are" if many else "it is"} never actually '
                  f'loaded by the page. Respond with ONLY a single shell command, no explanation, that appends '
                  f'the missing <script src> tag(s) to {html} -- e.g.:\ncat >> {html} << \'EOF\'\n<script src="{files[0]}"></script>\nEOF')
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug, 'messages': messages + [{'role': 'user', 'content': prompt}],
                        'max_tokens': 400, 'agentId': agent_id,
                        'taskId': task_id}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
            budget_hit = _chat_error_result(r, f'{html}: the follow-up model call was paused because this task used its full model-spend budget')
            if budget_hit:
                return {'ok': False, 'budgetExhausted': True,
                        'taskSpendUsd': budget_hit.get('taskSpendUsd'),
                        'taskSpendAttempts': budget_hit.get('taskSpendAttempts'),
                        'note': budget_hit['note']}
            all_ok = False
            notes.append(f'{html}: follow-up model call failed or returned nothing')
            continue
        link_command = r['reply'].strip().replace('```bash', '').replace('```sh', '').replace('```', '').strip()
        exec_data = _serve._http_json('POST', base, '/api/execute',
                               {'agentId': agent_id, 'command': link_command,
                                'purpose': f'Linking previously-unlinked file(s) into {html}: {", ".join(files)}',
                                'sandboxId': sandbox_id}, key)
        if not isinstance(exec_data, dict) or not exec_data.get('allowed'):
            all_ok = False
            notes.append(f"{html}: blocked ({exec_data.get('reason', 'no reason given') if isinstance(exec_data, dict) else 'no response'})")
            continue
        if not (exec_data.get('exitCode') == 0 and not exec_data.get('timedOut')):
            all_ok = False
            notes.append(f'{html}: exit code {exec_data.get("exitCode")}')
    return {'ok': all_ok, 'note': (all_ok and ', '.join(by_target.keys()) or '; '.join(notes))}


def _find_phantom_script_refs(base, key, agent_id, sandbox_id):
    """Port of tasks.js _findPhantomScriptRefs: which real <script src="...">
    references in ANY *.html file point at a .js file that doesn't exist.
    Returns [] on a read failure -- don't guess-remove something that might be real."""
    check_command = ('for html in *.html; do [ -f "$html" ] || continue; for f in $(grep -oE \'src="[^"]+\\.js"\' "$html" | '
                     "sed -E 's/src=\"//;s/\"$//' | sort -u); do [ -f \"$f\" ] && echo \"EXISTS:$html:$f\" || echo \"PHANTOM:$html:$f\"; done; done")
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id, 'command': check_command,
                       'purpose': 'Verifying every <script src> reference in every HTML file actually corresponds to a real file.',
                       'sandboxId': sandbox_id}, key)
    if not isinstance(data, dict):
        return []
    stdout = data.get('stdout') or ''
    refs = []
    for m in re.finditer(r'PHANTOM:([^:\n]+):(\S+)', stdout):
        refs.append({'html': m.group(1), 'file': m.group(2)})
    return refs


def _remove_phantom_script_refs(base, key, agent_id, sandbox_id, phantom_refs):
    """Port of tasks.js _removePhantomScriptRefs: cleanup the dangling script
    refs outright (a silent 404 is strictly worse than no reference), grouped
    by which HTML file each reference actually came from."""
    by_html = {}
    for ref in phantom_refs:
        by_html.setdefault(ref['html'], []).append(ref['file'])
    all_ok = True
    notes = []
    for html_name, files in by_html.items():
        excludes = ' '.join('-e ' + json.dumps('src="' + re.escape(f) + '"') for f in files)
        cleanup_command = f'grep -v {excludes} "{html_name}" > "{html_name}.__cleanup_tmp" && mv "{html_name}.__cleanup_tmp" "{html_name}"'
        data = _serve._http_json('POST', base, '/api/execute',
                          {'agentId': agent_id, 'command': cleanup_command,
                           'purpose': f'Removing phantom <script src> reference(s) in {html_name} to nonexistent file(s): {", ".join(files)}',
                           'sandboxId': sandbox_id}, key)
        if not isinstance(data, dict) or not data.get('allowed'):
            all_ok = False
            notes.append(f"{html_name}: blocked ({data.get('reason', 'no reason given') if isinstance(data, dict) else 'no response'})")
        elif not (data.get('exitCode') == 0 and not data.get('timedOut')):
            all_ok = False
            notes.append(f'{html_name}: exit code {data.get("exitCode")}')
    return {'ok': all_ok, 'note': '; '.join(notes) if notes else 'removed'}
