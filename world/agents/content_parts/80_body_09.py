

def _run_spike_content(snapshot, agent_id, task, base_ctx=None):
    """Phase E2b: a SPIKE is a time-boxed investigation with no committed
    deliverable. Writes a concise findings artifact to the Library and stores
    a content result. A spike never opens a peer gate and never releases a
    product -- it answers a question, that's all.

    This used to be a single free-text /api/chat completion with
    NO tool access at all -- the model just guessed from training knowledge
    and called it "findings" (see the SECURITY_TEST_TOOLS comment in serve.py
    for the fabricated-report incident that exact pattern already caused
    once, for the security-test role). Then got real search_web/browse_page
    tool access via the shared _make_web_tools_executor -- fixed the
    fabrication, but a real, harder DreyX request ("list every source ever
    used, assess replicating each daily") showed the NEXT gap: a single
    non-reasoning model in one flat tool loop settles too early on genuinely
    open-ended, multi-step work, because it can't reliably judge "have I
    covered this exhaustively." Real, explicit fix, PLAN
    (reasoning tier, one call, a concrete checklist) -> EXECUTE (mid tier,
    the existing many-iteration tool loop, now following that checklist) ->
    SYNTHESIZE (reasoning tier, one call, given the FULL gathered transcript,
    write the complete report against the checklist). The reasoning tier's
    much higher per-token cost is paid twice per investigation this way, not
    once per tool call."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    name = _agent_name(snapshot, agent_id)
    backlog = task.get('title') or ''
    instructions = task.get('instructions')
    budget = task.get('budgetMs')

    # Liveness heartbeat (see sim.TASK_CONTENT_GRACE_*): `task` here is the
    # LIVE task dict (the dispatch passes it, not the snapshot's copy), so
    # these bumps are what the completion loop reads when workUntil expires.
    # Bumped at every phase boundary AND every tool call -- the execute phase
    # is minutes of small steps, synthesis is one long call, so between them
    # the gap never exceeds a single model call (well under
    # TASK_CONTENT_HB_STALE_S). A thread that dies stops bumping, which is
    # exactly what stops the grace extension.
    def _hb():
        try:
            task['_contentHeartbeat'] = time.time()
            _sim_module._bump_content_heartbeat(task.get('id'))
        except Exception:
            pass

    _hb()
    # Item 4 gate up front: a spike that has already used its full model-spend
    # budget is failed closed BEFORE it resolves tiers or makes any call --
    # no further money is spent, and the sim sees budgetExhausted and runs its
    # notify + fail-closed branch. (The serve-side gate inside every /api/chat
    # and _plain_completion call backs this up; this check just fails fast and
    # stores the canonical result.)
    if _serve._task_budget_exhausted(task.get('id')):
        spent, attempts = _serve._task_budget_spent(task.get('id'))
        _sim_module._store_content_result(task.get('id'), {
            'ok': False,
            'budgetExhausted': True,
            'taskSpendUsd': spent,
            'taskSpendAttempts': attempts,
            'note': f'Spike "{backlog}" was paused before it started: this task already used its full model-spend budget (${spent:.4f} spent).',
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike paused on budget: {backlog[:80]}',
                             'body': f'{name} tried to spike "{backlog}" but the task already used its full model-spend budget (${spent:.4f} spent, {attempts} model call(s)) -- no further calls were made.'},
        })
        return
    # A spike finishing used to generate no notice on either channel; the
    # think tank was reactive-only. notifyPlayer is the (safe, indirect --
    # _apply_content_result actually queues it) way any executor asks to be
    # notified on completion, success or failure alike, so silence never
    # reads as "still working" when it already gave up.
    best_effort = bool(task.get('bestEffort'))
    tier_slug = _serve._resolve_model_tier(
        f'Run a time-boxed web investigation (spike): {backlog[:200]}',
        best_effort=best_effort)
    # The plan/synthesize bookends do the genuine reasoning of a spike. No
    # separate reasoning band needed: the JEV tier gate already
    # routes consequential work to mid/high, whose models are reasoning-
    # capable. Gate with allow_high so a hard investigation can spend up.
    reasoning_slug = _serve._resolve_model_tier(
        f'Plan and synthesize a time-boxed investigation, judging own completeness: {backlog[:200]}',
        allow_high=True, best_effort=best_effort)
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but no model tier is configured yet.', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike stalled: {backlog[:80]}',
                             'body': f'{name} tried to spike "{backlog}" but no model tier is configured yet -- nothing was actually attempted.'},
        })
        return
    # Real multi-tool investigation (a browse_page render=true call launches
    # a real headless browser per fetch) can genuinely take longer than the
    # generic 120s content ceiling _dispatch_content_work installs -- widen
    # it here so a slow-but-real result isn't dropped by that timeout's
    # fallback-completion path before this thread finishes.
    task['workUntil'] = time.time() + _spike_work_window_s(task)

    # Executor-level checkpoint: reuse the plan from a prior attempt after a
    # crash/restart instead of paying for the same plan twice. The plan is
    # cleared on a successful close; a failed run keeps it for the retry.
    plan_text = ''
    resume_transcript = None
    resume_pending = []
    plan_checkpoint = _serve.load_task_checkpoint(task.get('id'))
    if (isinstance(plan_checkpoint, dict)
            and plan_checkpoint.get('title') == backlog):
        if plan_checkpoint.get('phase') == 'planned':
            plan_text = str(plan_checkpoint.get('plan') or '')
        elif (plan_checkpoint.get('phase') == 'execute'
              and isinstance(plan_checkpoint.get('transcript'), list)):
            # A prior process died mid-execute. Resume from the last completed
            # tool batch instead of re-running the whole investigation. Any
            # call that was in flight is answered below according to its
            # replay-safety label.
            plan_text = str(plan_checkpoint.get('plan') or '')
            resume_transcript = plan_checkpoint.get('transcript')
            resume_pending = plan_checkpoint.get('pending') or []

    # PLAN -- a short, concrete checklist of sub-goals, from the stronger
    # reasoning tier. Best-effort: an empty plan just means the execute step
    # falls back to its own generic instructions below, not a failure.
    _hb()
    if not plan_text:
        plan_text = _plain_completion(reasoning_slug, [
            {'role': 'system', 'content': (
                'You are planning a real, tool-driven web investigation. Given the question below, '
                + PREMISE_QUESTIONING_GUIDANCE + ' '
                'First pin a short RESEARCH CONTRACT (one line each, in your plan): QUESTION (the exact '
                'thing to answer), AS OF (today\'s date, and how fresh the answer must be -- a live fact '
                'like pricing/availability/leadership needs a same-week source, a timeless explanation '
                'does not), SCOPE (what is in and out of bounds), COMPARE (which alternatives/axes matter, '
                'if any), STOP WHEN (the concrete evidence that makes the answer good enough -- a '
                'fixed round budget is not a stop condition, verified evidence is), and PROVE WRONG (the '
                'single most likely objection to the expected answer and the concrete observation that '
                'would overturn it -- if you cannot name what would prove it false, the question is not '
                'yet an answerable claim). A question without a contract is the #1 cause of '
                'broad-but-wrong research, so this comes first.\n'
                'Then write a short numbered checklist (3-7 items) of concrete sub-goals needed to answer it '
                'thoroughly and honestly -- e.g. which pages/categories to visit, what to extract from '
                'each, and what a real answer must cover. The FIRST item must always be a search_web '
                'query for what OTHER sites say about the subject -- a subject\'s own pages never '            'disclose everything about it (methodology, reputation, who else covers it), and a '
                'JS-heavy site may not even be readable by a plain fetch. If the subject has a large '
                'list of items (a directory, a catalog), include a step to sample a few individual item '
                'pages, not just the top-level listing.\n\n'
                'INTERNAL PRIOR ART -- if this question is about reviewing, comparing against, or '
                'building on work already done inside this think tank (another team\'s finished story, an '
                'earlier spike, prior research), the FIRST item must instead be a search_library query '
                '(not search_web) -- read what was actually done internally before looking externally, '
                'and follow a promising hit with read_library_file for the full content. If '
                'search_library finds nothing relevant, say so explicitly in the plan rather than '
                'assuming what the other team probably did.\n\n'
                'DELIVERABLE DECISION -- make this call explicitly: does answering this question well '
                'mean listing/comparing multiple items with attributes (sources, tools, options, prices, '
                'anything enumerable), or computing/transforming something from what gets found? If yes, '
                'the plan MUST include a MANDATORY step, worded exactly like this shape: "Use '
                'execute_script to write <filename>.csv with columns: <col1>, <col2>, ... -- one row per '
                '<item>." -- with REAL column names and a REAL filename you choose for this question, not '
                'a placeholder, and REAL columns for what the answer needs to be a real answer rather than '
                'a page description (e.g. a source/feasibility question needs columns like '
                'source_name, url, retrieval_method, api_or_rss_available -- not just source_name). Make '
                'this the LAST step, run only after the earlier research steps found real data to put in '
                'it. Say explicitly that describing the CSV in prose instead of actually building it does '
                'NOT satisfy this step. If the question is a simple yes/no or single-fact lookup with '
                'nothing to enumerate, skip this and say so -- do not force a CSV that doesn\'t fit.\n\n'
                'VERIFICATION HONESTY -- real gap caught live (2026-09-26): a CSV with a per-item judgment '
                'column (feasibility, difficulty, recommendation, anything requiring assessment, not just a '
                'fact copied from a page) looks equally authoritative whether each row was actually checked '
                'or just guessed from general knowledge -- there was no way to tell which from the output. '
                'If the CSV has a judgment column like this, the plan MUST require: (1) a status column '
                '(e.g. "basis") on EVERY row stating either "verified" (actually checked via a tool this '
                'run) or "estimated" (general knowledge, not directly checked this run) -- never leave this '
                'ambiguous; (2) an explicit step to ACTUALLY VERIFY a small sample (3-5 representative '
                'rows, or all of them if there are fewer than 6) via search_web/browse_page/execute_script '
                'before finalizing, so the CSV isn\'t 100% estimated -- pick rows that matter most to the '
                'question, not arbitrary ones. This does not apply to columns that are plain facts already '
                'confirmed during research (a name, a URL actually seen) -- only to judgment/assessment '
                'columns.\n\n'
                'This is a plan for another agent who will actually browse_page/search_web/execute_script '
                '-- do not answer the question yourself, do not invent facts, just plan the investigation.')},
            {'role': 'user', 'content': f'Question: {backlog}'
                                         f'{(" Method/constraints: " + instructions) if instructions else ""}'},
        ], max_tokens=500, task_id=task.get('id'))
        if plan_text:
            _serve.save_task_checkpoint(task.get('id'), 'planned',
                                        {'title': backlog, 'plan': plan_text})

    # EXECUTE -- the existing many-iteration real tool loop (mid tier: cheap
    # enough to spend on up to 18 round trips), now following the plan above
    # when one was produced.
    system = (
        f'You are {name}, running a time-boxed SPIKE (~{(budget or 60_000) / 1000:.0f}s). '
        f'Question: {backlog}.'
        f'{" Method/constraints: " + instructions if instructions else ""} '
        + (f'Follow this plan:\n{plan_text}\n\n' if plan_text else '')
        + 'This is REAL investigative work, not a guess from memory -- if the question depends on the '
        'actual current content of a specific website or any other live/current fact you do not '
        'already know for certain, you MUST use the search_web/browse_page tools to actually look it '
        'up before answering. If the question is about reviewing or building on work another team '
        'inside this think tank already did, use search_library/read_library_file to actually read '
        'their real findings first -- never describe another team\'s work from a guess; if '
        'search_library turns up nothing, say plainly that no internal record was found. Never '
        'invent specifics (numbers, names, sources, quotes) you did not '
        'actually get back from a tool -- if you can\'t find something real, say so instead of '
        'guessing. If the question is about Facebook or Instagram public content, do NOT use '
        'browse_page on those hosts and do NOT file an allowlist request for them: use '
        'request_credential_access(service="facebook" or "instagram", purpose=...) while working '
        'this task only if a page is actually gated, then browser_act op=login with the returned '
        'handle. Public Facebook group and post permalinks often load logged out: use search_web to '
        'find the group URL, then browser_act goto/read on https://www.facebook.com/groups/<id> and '
        'the individual post links. Do not request credentials just because the host is Facebook. '
        'Social hosts are '
        'hard read-only: fill, click, and submit are refused outside that login. Many sites load '
        'their real content via JavaScript/AJAX -- if a plain browse_page '
        'fetch comes back as an empty shell or just navigation/boilerplate with no real content, '
        'retry the SAME url with render=true to get the real rendered page. If the question needs '
        'looking at multiple items (several posts/articles on a site, not just the front page), '
        'follow real links you found on the page and browse_page the individual pages too -- do not '
        'stop at the homepage. If a page lists a large catalog/directory of items, browse_page at '
        'least 2-3 of the individual item pages too, not just the top-level listing -- a category '
        'name is not the same as what that specific item actually is. Only ever follow a URL you '
        'actually saw returned by search_web or in a page\'s real links list -- never invent or guess '
        'a URL path. '
        + 'For every important claim, note WHICH source supports it and AS OF WHEN you checked it '
        '(a live fact like pricing/availability/leadership carries its check date, and you must say '
        'explicitly when a source may be stale). "Two sources confirm it" is weak if both copied the '
        'same press release -- when a claim matters, trace it to the primary source and, if one '
        'exists, at least one genuinely independent source. Mark each claim supported, contested, '
        'unverified, or stale rather than leaving it implicit, and when sources disagree do not '
        'average them -- record the disagreement and which one answers the question. Stop when the '
        'evidence is sufficient for the question, not when it "feels complete." '
        + ("If a plan step or the question genuinely needs real computation this machine cannot do "
            "-- CUDA/GPU work, a fine-tuning experiment, a heavy numeric job -- use run_on_colab to "
            "run complete Python on a real Colab GPU and read its output, rather than skipping it or "
            "claiming the think tank can't do it. It is a metered cost against the think tank's Colab "
            "compute budget shown in the Bank; the Colab here is the FREE tier, so keep runs small "
            "-- a run must finish within its own timeout, an idle session gets recycled, and GPU "
            "slots are not guaranteed. IT RUNS AS THE PLAYER'S IDENTITY: you must NEVER use it for "
            "Google Drive, GCS/cloud APIs, credentials, mining/bulk media/torrents, or any data "
            "exfiltration site -- only raw compute. " if _serve.COLAB_CLI_AVAILABLE else "")
        + 'A spike never opens a peer-review gate, but it CAN still produce a real '
        'artifact -- if your plan has an execute_script/CSV step, that step is MANDATORY, not '
        'optional: you must actually call execute_script and write the real file. Describing what '
        'the CSV would contain, in prose, is NOT the same as building it and does NOT satisfy that '
        'plan item -- if you catch yourself writing "the CSV would include..." instead of actually '
        'writing csv rows to a real file, stop and go build it for real. Only build it from data you '
        'actually collected above -- if research fell short of what a row needs, use "unknown" for '
        'that cell rather than inventing a plausible-looking value. If your plan requires a "basis" '
        'column and a verification sample, that is ALSO mandatory: mark each row "verified" only if '
        'you actually checked it this run (a real search_web/browse_page/execute_script result), '
        '"estimated" otherwise -- do not mark a row "verified" just because you\'re confident about it '
        'from general knowledge. Work through every item in your '
        'plan before concluding -- if you genuinely cannot complete one, say so explicitly rather than '
        'skipping it silently. When you are done investigating, summarize what you actually found -- '
        'a later step will turn this into the final report, so completeness here matters more than '
        'polish. If you produced a file, cat its full contents out before you finish so the final '
        'report can include it verbatim.'
    )
    if _serve._google_is_configured():
        system += _GOOGLE_QUOTA_AWARENESS_BLOCK
    if ('self_edit' in (task.get('approvedTools') or [])
            or 'self_edit' in (instructions or '').lower()
            or 'change request' in (backlog + ' ' + (instructions or '')).lower()):
        system += (
            '\n\nGOVERNED EDIT TASK: This is not a feasibility study. You have '
            'explicit approval for self_edit and schedule_dormant_apply. First call '
            'selfedit_read with path "*listing*" to list the available world files, '
            'then choose an existing target named in the task (for a pressoffice UI '
            'report, world/index.html is the default). Call self_edit with one exact '
            'anchor and a small, real change, then call schedule_dormant_apply with '
            'the named validator. Do not finish with a report saying the pipeline is '
            'blocked unless self_edit actually returned an error. A missing target '
            'path must be named exactly, not guessed as world/pressoffice.py.'
        )
    system += '\n\n' + evidence_policy.INSTRUCTIONS
    if resume_transcript is not None:
        messages = list(resume_transcript)
        # A checkpoint saved mid-turn can carry a tool call whose JSON
        # arguments were cut off at the token limit. Replaying it verbatim
        # makes every upstream request 400 ("Assistant tool call
        # function.arguments must be valid JSON"), so the task could never
        # recover -- it failed the same way on every retry. Repair the saved
        # transcript on the way in; the model still sees that it asked for
        # that tool, and its result message below still explains what happened.
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get('role') != 'assistant' or not msg.get('tool_calls'):
                continue
            cleaned, repaired = _serve._sanitize_tool_call_arguments(msg.get('tool_calls'))
            if repaired:
                fixed = dict(msg)
                fixed['tool_calls'] = cleaned
                messages[idx] = fixed
        for call in resume_pending:
            name = ((call.get('function') or {}).get('name') or '').strip()
            if tool_replay_safety(name) == 'safe':
                note = ('This read-only call was interrupted by a process restart. It is safe to '
                        'retry; re-issue the same call if you still need it.')
            else:
                note = ('This side-effecting call was interrupted by a process restart and was NOT '
                        'replayed. Do not assume it happened. Verify the external state before '
                        'retrying.')
            messages.append({'role': 'tool', 'tool_call_id': call.get('id'), 'content': note})
    else:
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': 'Go ahead.'}]
    # One-strike-per-tool: a shared set across BOTH sub-executors, so a policy
    # denial on either browse_page or execute_script is remembered and the
    # SAME tool is refused (no network call at all) if the model tries it
    # again this investigation -- it can still try a genuinely different tool.
    struck_tools = set()
    # Work-context-aware gating: the player-filed task itself (title --
    # instructions) is threaded into every browser-like tool so the Jev gate
    # judges URLs against the assigned work, not in isolation. A host the
    # player explicitly named in the task bypasses Jev like the allowlist;
    # everything else is judged with the work context in the Jev prompt.
    work_context = _build_work_context(backlog, instructions, task.get('never'))
    # Player-authored provenance (see queue_work's playerAuthored): the Jev
    # gate's work-context HOST BYPASS (a host named in the work skips Jev like
    # the allowlist) fires ONLY for tasks the PLAYER actually wrote. An
    # agent-authored story can name any URL in its own text, so its work
    # context must always go through the story-aware Jev prompt instead --
    # otherwise the story text becomes a laundering vector, not a vetted
    # allowlist. The block categories are absolute either way.
    work_context_trusted = bool(task.get('playerAuthored'))
    web_tool = _serve._make_web_tools_executor(agent_id, key, default_query=backlog, struck_tools=struck_tools, work_context=work_context, work_context_trusted=work_context_trusted, task_id=task.get('id'))
    sandbox_id = f"spike-{task.get('id') or 'adhoc'}"
    sandbox_tool = _make_spike_sandbox_executor(agent_id, key, sandbox_id, struck_tools=struck_tools)
    browser_act_execute = _make_browser_act_executor(agent_id, key, sandbox_id, struck_tools=struck_tools, work_context=work_context, work_context_trusted=work_context_trusted, task_id=task.get('id'))
    browser_step_execute = _make_browser_step_executor(agent_id, key, sandbox_id, work_context=work_context, work_context_trusted=work_context_trusted, task_id=task.get('id'))
    captcha_solve_tool = _make_captcha_solve_executor(agent_id, key)
    list_handles_tool = _make_list_handles_executor(agent_id, key)
    library_tool = _make_library_tools_executor(agent_id, struck_tools=struck_tools)
    skill_publisher = _make_skill_publisher(agent_id, key)
    # Canonical report path for THIS task, computed once here (not only at the
    # close below) so write_spike_report can write to the SAME file the closer
    # files -- a long report is written in full via the tool, then the closer's
    # own short synthesis still lands on this path (see the report_path
    # reference in the close block).
    report_path = f"archive/{int(time.time() * 1000)}-spike-{task.get('id') or 'adhoc'}.md"
    spike_report_writer = _make_spike_report_writer(agent_id, key, report_path)
    treg_tool = _make_treg_tools_executor()
    pixellab_tool = _make_pixellab_tools_executor(agent_id, key)
    google_tool = _make_google_tools_executor(agent_id)
    github_tool = _make_github_tools_executor(agent_id, key)
    apify_tool = _make_apify_tools_executor(agent_id, key)
    youtube_tool = _make_youtube_transcript_executor(agent_id, key)
    # Colab agent compute (run_on_colab) -- offered only when the colab CLI
    # is actually on this machine; absent means the tool is simply absent.
    colab_compute_tool = None
    if _serve.COLAB_CLI_AVAILABLE:
        colab_compute_tool = _make_colab_compute_executor(agent_id, struck_tools=struck_tools)
    spike_tools = _serve.AGENT_ASK_TOOLS + list(_serve._PULSE_TOOLS) + [_SPIKE_SANDBOX_TOOL, _LIBRARY_SEARCH_TOOL, _LIBRARY_READ_TOOL,
                                            _BROWSER_ACT_TOOL, _BROWSER_STEP_TOOL, _AGENT_READINESS_TOOL,
                                            _PROJECT_BOARD_TOOL, _CAPTCHA_SOLVE_TOOL, _LIST_HANDLES_TOOL,
                                            _TREG_X_TRENDING_TOOL, _TREG_LINKEDIN_SEARCH_TOOL,
                                            _PIXELLAB_CHARACTER_TOOL,
                                            _GOOGLE_SHEETS_READ_TOOL, _GOOGLE_SHEETS_APPEND_TOOL,
                                            _GOOGLE_CALENDAR_LIST_TOOL, _GOOGLE_CALENDAR_CREATE_TOOL,
                                            _GMAIL_SEARCH_TOOL, _GMAIL_READ_TOOL, _GMAIL_CREATE_DRAFT_TOOL,
                                             _GOOGLE_DOCS_READ_TOOL, _GOOGLE_DOCS_CREATE_TOOL,
                                             _PUBLISH_SKILL_TOOL, _WRITE_SPIKE_REPORT_TOOL]
    # Scoped player-folder + Notes access (world/fs.py): offered to spike
    # workers under the same condition as the ask lane -- FILE_ACCESS_ENABLED
    # AND a grant from state OR the operator-edited world/grants.json. Every
    # call is still gated by the grant (containment + trash rule) inside fs.py.
    # `read_store` (the generic SQLite + gzip + protobuf read) rides the same
    # grant.
    if _serve.FILE_ACCESS_ENABLED and _serve._effective_file_grants(snapshot):
        spike_tools = spike_tools + [_serve._LOCAL_FILE_TOOL, _serve._READ_STORE_TOOL]
    # App control via AppleScript (world/fs.py): offered under APP_ACCESS_ENABLED
    # AND a grant from state OR world/grants.json. Every call is gated inside
    # fs.py: the bundleId must be granted and the script body validated (no
    # shell / URL /
    # escalation / cross-app primitives).
    if _serve.APP_ACCESS_ENABLED and _serve._effective_app_grants(snapshot):
        spike_tools = spike_tools + [_serve._APP_SCRIPT_TOOL]
    # YouTube transcripts: offered only when APIFY_API_KEY is set -- the route
    # ALWAYS downloads the audio via the Apify actor on the Colab runtime (no
    # captions fast-path), so unset = tool simply absent, same
    # conditional-availability rule as the Apify scraping tools below.
    if _serve.APIFY_API_KEY:
        spike_tools += [_YOUTUBE_TRANSCRIPT_TOOL]
    # GitHub read tools: offered only when GITHUB_TOKEN is set --
    # same conditional-availability rule as search_web (unset = tool simply
    # absent, so the surface never advertises something that would fail).
    if _serve.GITHUB_TOKEN:
        spike_tools += [_GITHUB_REPO_TOOL, _GITHUB_ISSUES_TOOL, _GITHUB_ISSUE_TOOL, _GITHUB_SEARCH_TOOL]
    # Apify scraping tools: offered only when APIFY_API_KEY is set --
    # same conditional-availability rule (unset = tool simply absent).
    if _serve.APIFY_API_KEY:
        spike_tools += [_APIFY_RUN_ACTOR_TOOL, _APIFY_GET_DATASET_ITEMS_TOOL]
    # Colab agent compute: offered only when the colab CLI is
    # actually installed on this machine -- same conditional-availability rule.
    if _serve.COLAB_CLI_AVAILABLE:
        spike_tools += [_COLAB_RUN_TOOL]
    _GOOGLE_TOOL_NAMES = ('read_google_sheet', 'append_google_sheet_row',
                          'list_calendar_events', 'create_calendar_event',
                          'search_gmail_messages', 'read_gmail_message', 'create_gmail_draft',
                          'read_google_doc', 'create_google_doc')

    def browser_act_tool(tool_name, args):
        output = browser_act_execute(tool_name, args)
        try:
            web_tool.discovered.observe(output)
        except Exception:
            pass
        return output

    def _execute_tool_raw(tool_name, args):
        _hb()  # every tool call is live progress the completion loop can see
        if tool_name in struck_tools:
            return (f'{tool_name} was already blocked once this investigation (one-strike) -- do '
                     'not call it again, use a different tool or approach instead.')
        if tool_name == 'execute_script':
            return sandbox_tool(tool_name, args)
        if tool_name == 'browser_act':
            return browser_act_tool(tool_name, args)
        if tool_name == 'browser_step':
            return browser_step_execute(tool_name, args)
        if tool_name == 'audit_agent_readiness':
            return audit_agent_readiness(web_tool, args)
        if tool_name == 'project_board':
            return project_board(args)
        if tool_name == 'solve_captcha':
            return captcha_solve_tool(tool_name, args)
        if tool_name == 'list_capability_handles':
            return list_handles_tool(tool_name, args)
        if tool_name in ('search_library', 'read_library_file'):
            return library_tool(tool_name, args)
        if tool_name in ('pulse_watch', 'read_pulse'):
            return execute_pulse_tool(agent_id, tool_name, args)
        if tool_name == 'publish_skill':
            return skill_publisher(tool_name, args)
        if tool_name == 'write_spike_report':
            return spike_report_writer(tool_name, args)
        if tool_name in ('x_trending_topics', 'search_linkedin_posts'):
            return treg_tool(tool_name, args)
        if tool_name == 'youtube_transcript':
            return youtube_tool(tool_name, args)
        if tool_name == 'generate_pixel_character':
            return pixellab_tool(tool_name, args)
        if tool_name in _GOOGLE_TOOL_NAMES:
            return google_tool(tool_name, args)
        if tool_name in _GITHUB_TOOL_NAMES:
            return github_tool(tool_name, args)
        if tool_name in _APIFY_TOOL_NAMES:
            return apify_tool(tool_name, args)
        if tool_name == 'run_on_colab' and colab_compute_tool is not None:
            return colab_compute_tool(tool_name, args)
        if tool_name == 'local_file':
            return _serve._local_file_tool(agent_id, args)
        if tool_name == 'read_store':
            return _serve._read_store_tool(agent_id, args)
        if tool_name == 'app_script':
            return _serve._app_script_tool(agent_id, args)
        return web_tool(tool_name, args)

    def execute_tool(tool_name, args):
        return _serve.idempotent_tool_call(agent_id, task.get('id'), _execute_tool_raw,
                                           tool_name, args)

    # 18/900 (was 10/600): a "list every X across the whole site" question
    # (real request, "a full list of sources ever used on
    # DreyX") needs many more browse_page round trips than a single-fact
    # lookup. This turn's own text no longer has to BE the final report
    # (synthesize does that from the full transcript below), so its token
    # budget stays modest -- it only needs room for a working summary plus
    # each tool call's own arguments.
    # Forcing search_web specifically (not just force_first_tool=True's
    # "any tool") -- gap: force_first_tool=True alone still
    # ALWAYS reached for browse_page on the target's own pages and never
    # called search_web at all, missing facts that only live in OTHER sites'
    # coverage of the target (a manual search surfaced DreyX's named upstream
    # sources that 3 rounds of browsing dreyx.com itself never found).
    #
    # Same fix, second application: an internal-prior-art
    # question needs search_library forced first for the identical reason --
    # a PLAN-prompt instruction to "search the library first" was NOT
    # reliably followed in a real live run (see _spike_wants_internal_review's
    # own docstring for the exact incident). Checked before the search_web
    # default so an internal-review spike doesn't reach for the outside web
    # before it has even looked at what the think tank already knows.
    #
    # Same fix, third application: a real X-trending or
    # LinkedIn-search question needs its own specific tool forced first for
    # the identical reason -- built preemptively this time, on day one,
    # rather than after a live miss, now that the pattern is proven twice.
    first_tool = None if resume_transcript is not None else _spike_first_tool(backlog, instructions)
    # Chunked into rounds of 4 with a reflection self-check between them
    # (see _run_spike_tool_loop_with_reflection) -- same 50-call total
    # budget (raised 18->30->50, just spent with a chance to
    # course-correct partway through instead of only finding out it went
    # sideways at the end.
    def _checkpoint_before(tool_calls, checkpoint_messages):
        try:
            _serve.save_task_checkpoint(task.get('id'), 'execute', {
                'title': backlog, 'plan': plan_text,
                'transcript': checkpoint_messages, 'pending': tool_calls})
        except Exception:
            pass

    def _checkpoint_after(checkpoint_messages):
        try:
            _serve.save_task_checkpoint(task.get('id'), 'execute', {
                'title': backlog, 'plan': plan_text,
                'transcript': checkpoint_messages, 'pending': []})
        except Exception:
            pass

    try:
        execute_text, transcript = _run_spike_tool_loop_with_reflection(
            tier_slug, reasoning_slug, messages, spike_tools, execute_tool,
            # 2400 -> 4096: an ~8KB report passed as write_spike_report's JSON
            # `content` string needs more than 2400 tokens including the JSON
            # envelope. At 2400 the reply was cut mid-string, which produced an
            # unterminated tool call and (before the loop repaired them) a
            # poisoned transcript. The closer below already allows 8000.
            total_iterations=50, max_tokens=4096, force_first_tool=first_tool,
            escalate_model=[_serve._mid_tier_slug(), _serve._high_tier_slug()],
            task_id=task.get('id'),
            village_id=_sim_village_of_agent(snapshot, agent_id),
            conversation_policy=task_tool_policy(task),
            on_before_tools=_checkpoint_before,
            on_after_tools=_checkpoint_after)
    except Exception as e:
        # A model-call failure (circuit-breaker RuntimeError, a 4xx, a network
        # blip) must never crash the spike executor or leave a half-baked
        # result -- record an honest not-ok like the empty-investigation path
        # below does, and bail. (without this, a tripped circuit
        # breaker propagated out of the tool loop and blew up the whole task.)
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but the investigation could not run (model call failed): {e}', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike came up empty: {backlog[:80]}',
                             'body': f'{name} tried to spike "{backlog}" but the model call failed: {e}'},
        })
        return
    # Ground truth from the transcript itself (not a side-channel counter) --
    # true whenever execute_tool actually ran at least once.
    investigated = any(m.get('role') == 'tool' for m in (transcript or []))
    if not investigated:
        # force_first_tool=True should make this unreachable in practice --
        # kept as an explicit, honest failure rather than synthesizing a
        # report from a transcript that never actually investigated anything.
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but the model call returned nothing usable.', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike came up empty: {backlog[:80]}',
                             'body': f'{name} looked into "{backlog}" but the model call returned nothing usable. You may want to ask again or rephrase it.'},
        })
        return

    # SYNTHESIZE -- the stronger reasoning tier, given the FULL transcript
    # (every real page fetched and every real search result), writes the
    # actual final report against the plan. Falls back to the execute step's
    # own closing text if synthesis itself comes back empty.
    #
    # max_tokens: 8000 (was 1800). Confirmed bug 2026-10-12 (task-282): a
    # five-section comparison is ~4-6k tokens of prose, so the old 1800 cap
    # cut every long report mid-sentence, and the closer filed that truncation
    # verbatim as the whole deliverable (see the library_path write below).
    # This step is a PLAIN COMPLETION -- _plain_completion takes no tools --
    # so the old prompt line telling the model to call write_spike_report here
    # was asking the impossible. The real fix is to let the document fit. The
    # tool stays for the EXECUTE phase, where a worker that wants to stage a
    # long report in passes can still do so; the closer honors it via the
    # _already_written guard below.
    synth_prompt = (
        'Based on everything above -- the plan, and everything actually found via the tools -- write '
        'the complete final findings report. If the question asked for a full/exhaustive/complete list '
        'of something, state every distinct item actually found, not a sample. Only report facts that '
        'were actually returned by a tool above; if something could not be determined, say so plainly '
        'rather than guessing. If different tool results above disagree on a fact (e.g. two different '
        'counts or conflicting claims), state the discrepancy explicitly instead of silently picking '
        'one. If any item from the plan was never actually completed, list it under a short "Not '
        'covered" section rather than omitting it. If an execute_script call above produced a real '
        'artifact (a CSV, a data file, a script) and its contents were cat\'d out, include the FULL '
        'contents verbatim in a fenced code block in your report, EXACTLY as produced (including any '
        '"basis"/verified-vs-estimated column) -- this is the actual deliverable, not something to '
        'summarize or clean up. If the CSV has a basis column, briefly note in your own summary how '
        'many rows were actually verified vs estimated, so that distinction isn\'t buried in a table '
        'nobody reads closely. Organize the answer clearly (a short list or numbered sections is '
        'fine). No filler, no release notes. Write the COMPLETE report here -- do not abbreviate, '
        'summarize, or write a stub promising the full document later; this reply IS the deliverable '
        'and it is filed to the Library verbatim.'
    )
    # Synthesis is the one long silent call -- bump right before it so its
    # whole duration counts as live (bounded by the call timeout, well under
    # TASK_CONTENT_HB_STALE_S).
    _hb()
    finding = _plain_completion(
        reasoning_slug, transcript + [{'role': 'user', 'content': synth_prompt}], max_tokens=8000,
        task_id=task.get('id'))
    if not finding:
        finding = (execute_text or '').strip()
    if not finding:
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but the model call returned nothing usable.', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike came up empty: {backlog[:80]}',
                             'body': f'{name} looked into "{backlog}" but the model call returned nothing usable. You may want to ask again or rephrase it.'},
        })
        return
    # Deterministic safety net, not a prompt: confirmed
    # that the model doesn't reliably follow through on the VERIFICATION
    # HONESTY plan requirement above even when the plan itself calls for
    # it -- a per-item judgment CSV can still come back with no basis
    # column, silently looking equally confident whether checked or
    # guessed. Rather than trying to block execute_script from writing a
    # non-compliant CSV (fragile -- too many ways to write a CSV to
    # reliably detect in a shell command), catch it AFTER the fact and
    # disclose it, so the player is never silently left without knowing.
    if _plan_requires_verification_basis(plan_text) and not _finding_shows_basis_column(finding):
        finding = (
            'NOTE (added automatically): this investigation\'s plan called for a "basis" column '
            '(verified vs estimated) on judgment/assessment data, but the report below does not '
            'appear to include one. Treat any feasibility/quality/recommendation-style assessments '
            'below as UNVERIFIED -- general knowledge, not independently checked this run -- unless '
            'stated otherwise.\n\n' + finding
        )
    # Second deterministic safety net: confirmed in the
    # SAME investigation -- a real file got cat'd into the transcript (a
    # genuine execute_script result, real data), but synthesis summarized it
    # in prose instead of including it verbatim, so the actual produced
    # artifact never reached the report at all. Append the raw output
    # whenever it exists and isn't already substantially present, so a real
    # produced file is never silently lost to a lossy summary.
    for output in _extract_execute_script_outputs(transcript):
        if len(output) > 40 and output[:80] not in finding:
            finding += (f'\n\n---\n\nRaw output from execute_script (added automatically -- not '
                       f'already included in the report above):\n\n```\n{output[:4000]}\n```')
    # W3: a spike worker who spots a real gap now has a model-driven path to
    # propose it. This is only a WISH -- the executor thread runs against a
    # snapshot and must never mutate shared state, so _spike_file_issue_wish
    # returns a fileIssue wish dict (or None) and the sim side (inside the
    # tick's single read-modify-write) does the real filing. Most spikes are
    # informational, so the deterministic pre-filter + Jev verdict means no
    # wish (and no Jev spend) for the common "investigated, all fine" case.
    file_issue_wish = None
    try:
        with _heartbeat_keepalive(task.get('id')):
            file_issue_wish = _spike_file_issue_wish(finding, task, name, backlog)
    except Exception:
        file_issue_wish = None
    library_path = report_path
    # write_spike_report already saved a full, deliberately-long report to this
    # exact path -- do NOT clobber it with this one short synthesis call (which
    # is capped at max_tokens=1800 and would truncate a real document). Only
    # when the tool was never used does the short closing text become the filed
    # report, same as before the tool existed. Existence check via the read
    # executor (in-process, no extra HTTP), so a report already written wins.
    _already_written = False
    try:
        _probe = library_tool('read_library_file', {'path': library_path})
        _already_written = isinstance(_probe, str) and not _probe.startswith('Not found:')
    except Exception:
        _already_written = False
    if not _already_written:
        _serve._http_json('POST', base, '/api/library/file',
                   {'agentId': agent_id,
                    'path': library_path,
                    'content': f'# Spike: {backlog}\n\nBy: {name}\n\n{finding}\n',
                    'source': 'firsthand'}, key)
    # Data-minimization audit finding (Hermes Town comparison):
    # this was the ONE content executor (of 13) that duplicated the FULL
    # finding text into the task note -- which _apply_content_result (sim.py)
    # copies verbatim onto the durable task record AND the agent's own
    # profile.notes, so the same content ended up stored three times (the
    # library file above, task.note in the hot kv_state blob serialized on
    # every save, and the agent's profile). Every other executor already uses
    # a short summary here; the full finding's one real home is the library
    # file just written. The notification email/Telegram push below is a
    # THIRD, deliberate exception to that same rule -- the player asking to
    # be told what was found is a genuinely different need than the durable
    # task record staying lean, not a regression of the same bug.
    discovered_urls = sorted(getattr(getattr(web_tool, 'discovered', None), 'urls', set()) or set())
    _serve.clear_task_checkpoint(task.get('id'))
    _sim_module._store_content_result(task.get('id'), {
        'note': f'{name} spiked "{backlog}" -- see the Library entry just filed.', 'ok': True,
        'sourceUrls': discovered_urls,
        # Gap: /api/intent/spike/{id}/promote
        # (turning a spike into a real followup story) only ever had the
        # short note above to work with -- recording the exact path lets it
        # pull the REAL findings (source lists, CSVs, feasibility data)
        # forward instead of a vague pointer.
        'libraryPath': library_path,
        'fileIssue': file_issue_wish,
        'notifyPlayer': {'kind': 'spike_done',
                         'subject': f'[AI Think Tank] Spike done: {backlog[:80]}',
                         # 3600 (was 1500): a real list-style answer (e.g. "every
                         # source used") needs more room than a 2-4 sentence
                         # summary; kept under Telegram's 4096-char hard message
                         # cap once the subject/prefix overhead is added.
                         'body': f'{name} looked into "{backlog}":\n\n{finding[:3600]}'},
    })


def _run_workroom_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runWorkroomTask: the Press Office's dispatcher. A real
    review/QA taskType goes to the review executor; a projectLabel (real project)
    goes to the coding executor (with a real listing of the sandbox's current
    files as context instead of a guessed one); a productId routes to the Phase E
    product build/release executor; a bare ambient task falls back to the fixed
    shared-tooling health-check."""
    if task.get('productId'):
        _run_product_build_content(snapshot, agent_id, task, base_ctx)
        return
    project_label = task.get('projectLabel')
    if project_label and (task.get('taskType') == 'review' or task.get('taskType') == 'qa'):
        _run_review_content(snapshot, agent_id, task, base_ctx)
        return
    if project_label:
        backlog = f"{task.get('title') or ''} -- {task.get('instructions')}" if task.get('instructions') else (task.get('title') or '')
        context_summary = ''
        # Same per-story worktree the coding executor below will write into
        # (walks reviewOf), so the file listing is the story's tree, not the
        # shared one.
        ls_sandbox = _review_sandbox_id(snapshot, task, agent_id)
        ls = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/execute',
                        {'agentId': agent_id, 'sandboxId': ls_sandbox, 'command': 'ls -la'},
                        _serve.get_or_create_agent_key(agent_id))
        if isinstance(ls, dict) and ls.get('allowed') and ls.get('stdout'):
            where = ('this story\'s worktree' if ls_sandbox != WORKROOM_SANDBOX_ID
                     else 'shared Work Room sandbox')
            context_summary = f'Current files in the {where}:\n{ls["stdout"]}'
        # Item 1: the sim's perception (base_ctx) must survive this path -- the
        # file listing is extra context, not a replacement for who/where/why.
        if base_ctx:
            context_summary = '\n\n'.join(p for p in (base_ctx, context_summary) if p)
        # Read-before-act: the folded design taste doc for this project's
        # design work, so a labeled build reasons over the player's taste,
        # scoped to the agent's village.
        design_ctx = _design_context_for_task(snapshot, task, agent_id)
        if design_ctx:
            context_summary = f'{design_ctx}\n\n{context_summary}'
        _run_coding_content(snapshot, agent_id,
                            {**task, 'projectLabel': project_label,
                             'title': backlog or task.get('title', ''),
                             'instructions': None}, base_ctx=context_summary)
        return
    # Bare ambient task -> the fixed shared-tooling health check.
    import sim as _sim_module
    r = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/pipeline',
                   {'agentId': agent_id, 'sandboxId': WORKROOM_SANDBOX_ID,
                    'steps': [
                        {'name': 'update helper script', 'command': 'echo \'print("helper script checked and working")\' > helper.py'},
                        {'name': 'run it', 'command': 'python3 helper.py'}]},
                   _serve.get_or_create_agent_key(agent_id))
    note = (f'Worked in the shared sandbox -- hit a failure at "{r.get("failedStep")}".'
            if isinstance(r, dict) and r.get('failedStep')
            else 'Checked and ran the shared tooling in the Work Room -- all good.')
    _sim_module._store_content_result(task.get('id'), {'note': note})


_AGENT_READINESS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'audit_agent_readiness',
        'description': (
            'Audit a public page for agent-readiness using the five-action checklist: '
            'plain-text offers, bot policy, agent channel, pay-rung approval, and ten '
            'buying questions. Fetches the page through the normal allowlisted web path '
            'and returns a deterministic score, found/missing signals, and next actions. '
            'This is a surface scan, not a checkout test and not a legal opinion.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'url': {'type': 'string', 'description': 'Public http(s) page to scan.'},
                'render': {'type': 'boolean', 'description': 'Use a rendered fetch for JS-heavy pages.'},
                'purpose': {'type': 'string', 'description': 'One short sentence: why this page.'},
            },
            'required': ['url'],
        },
    },
}


_AGENT_READINESS_CHECKS = (
    ('plain_text_offers', 'Plain-text offers',
     ('price', 'pricing', 'cost', '$', 'fee', 'subscription', 'plan')),
    ('shipping_returns', 'Shipping and returns',
     ('shipping', 'delivery', 'return', 'refund', 'warranty')),
    ('bot_policy', 'Bot and agent policy',
     ('bot', 'crawler', 'agent', 'automation', 'terms', 'robots.txt', 'scrap')),
    ('agent_channel', 'Agent channel',
     ('api', 'mcp', 'webhook', 'feed', 'developer', 'integration', 'agent')),
    ('pay_rung_approval', 'Pay-rung approval',
     ('approval', 'authorize', 'authorization', 'confirm', 'checkout', 'payment', 'security')),
    ('structured_offer', 'Structured offer data',
     ('json-ld', 'schema.org', 'sku', 'inventory', 'availability', 'variant')),
)

_AGENT_READINESS_QUESTIONS = (
    'What exactly is for sale, in plain text?',
    'What is the total delivered price, including fees and tax?',
    'What is in stock right now, and how fresh is that answer?',
    'How long does delivery take to my location?',
    'What is the return and refund policy?',
    'Can an agent request a quote without a browser session?',
    'Can an agent reserve or buy with an explicit approval step?',
    'What is the stable identifier for this exact offer?',
    'What is the machine-readable endpoint or feed?',
    'What is the escalation path when an agent is unsure?',
)


def audit_agent_readiness(fetch, args):
    args = args or {}
    url = (args.get('url') or '').strip()
    if not url:
        return json.dumps({'ok': False, 'error': 'url is required'})
    page = fetch('browse_page', {
        'url': url,
        'render': bool(args.get('render')),
        'purpose': args.get('purpose') or 'agent-readiness surface scan',
    })
    page = str(page or '')
    # Drop the injection-boundary preamble; scan only the page body so terms
    # like "agent" in the wrapper cannot inflate the readiness score.
    if '\n\n' in page:
        page = page.split('\n\n', 1)[1]
    low = page.lower()
    if not page or low.startswith(('could not', 'blocked', 'error')):
        return json.dumps({'ok': False, 'error': page[:500] or 'empty page', 'url': url})
    checks = {}
    missing = []
    for key, label, terms in _AGENT_READINESS_CHECKS:
        found = [t for t in terms if t in low]
        checks[key] = {'label': label, 'found': bool(found),
                       'signals': found[:6]}
        if not found:
            missing.append(label)
    score = sum(1 for c in checks.values() if c['found'])
    next_actions = []
    for key, label, _terms in _AGENT_READINESS_CHECKS:
        if checks[key]['found']:
            continue
        if key == 'plain_text_offers':
            next_actions.append('Publish the offer, price, and fees as plain text, not only in images or scripts.')
        elif key == 'shipping_returns':
            next_actions.append('Publish delivery, return, and refund terms in plain text.')
        elif key == 'bot_policy':
            next_actions.append('Publish a clear bot and agent policy, including robots.txt and terms.')
        elif key == 'agent_channel':
            next_actions.append('Publish a stable API, feed, or MCP endpoint for agents.')
        elif key == 'pay_rung_approval':
            next_actions.append('Add an explicit approval or confirmation step before payment.')
        elif key == 'structured_offer':
            next_actions.append('Expose structured offer data (JSON-LD, schema.org, SKU, availability).')
    return json.dumps({
        'ok': True,
        'url': url,
        'score': score,
        'maxScore': len(_AGENT_READINESS_CHECKS),
        'checks': checks,
        'missing': missing,
        'nextActions': next_actions,
        'questions': list(_AGENT_READINESS_QUESTIONS),
        'note': 'Surface scan only. It does not test checkout, payment, or legal compliance.',
    }, sort_keys=True)


_PROJECT_BOARD_TOOL = {
    'type': 'function',
    'function': {
        'name': 'project_board',
        'description': (
            'Read the shared project board: open and done cards with owner, team, budget, '
            'spend, acceptance criteria, report path, and whether the artifact exists. '
            'Filter by team, project label, and status. Read-only; it never changes a card.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'team': {'type': 'string', 'description': 'Team or director id.'},
                'project': {'type': 'string', 'description': 'Project label or title substring.'},
                'status': {'type': 'string', 'enum': ['open', 'done', 'all']},
                'limit': {'type': 'integer', 'description': 'Max cards (default 30, max 100).'},
            },
            'required': [],
        },
    },
}
