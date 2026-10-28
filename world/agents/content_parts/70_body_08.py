
_GITHUB_SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'github_search_code',
        'description': (
            "Search REAL public GitHub code by query (e.g. a library name, a config pattern, a "
            "function). Returns matching repos/files with URLs. Use this to find how real "
            "projects actually do something before copying or referencing a pattern. Search API "
            "is rate-limited harder than the core API (10/min), so keep code searches few."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'A GitHub code-search query, e.g. "openrouter in:file language:python".'},
                'limit': {'type': 'integer', 'description': 'Max results to return (default 5, max 10).'},
            },
            'required': ['query'],
        },
    },
}

_GITHUB_TOOL_NAMES = ('github_get_repo', 'github_list_issues', 'github_get_issue', 'github_search_code')


def _make_github_tools_executor(agent_id, key):
    """github_get_repo / github_list_issues / github_get_issue / github_search_code
    -- thin wrappers over the shared _serve._api_execute chokepoint (registry-
    driven base URL + server-side credential injection + response wrapping).
    No struck_tools / no spend accrual: read-only, rate-limited (not gated),
    and free like Google."""

    def _owner_repo(args):
        owner = (args.get('owner') or '').strip()
        repo = (args.get('repo') or '').strip()
        return owner, repo

    def _get(url, purpose):
        # The credential and auth come from the registry, injected by
        # _api_execute; the executor only supplies the URL + purpose.
        out = _serve._api_execute(agent_id, key, url, 'GET', None,
                                  {'Accept': 'application/vnd.github+json'}, purpose)
        if out.get('ok') is not True:
            return None, out.get('reason') or out.get('error') or 'failed'
        return out.get('data'), None

    def execute_tool(name, args):
        args = args or {}
        base = _serve._api_base_url('api.github.com')
        q = _serve.urllib.parse.quote
        if name == 'github_get_repo':
            owner, repo = _owner_repo(args)
            if not owner or not repo:
                return 'owner and repo are required'
            data, error = _get(f'{base}/repos/{q(owner, safe="")}/{q(repo, safe="")}', 'read a GitHub repo')
            if error:
                return f'Could not read the repo: {error}'
            return json.dumps({
                'full_name': data.get('full_name'),
                'description': data.get('description'),
                'stars': data.get('stargazers_count'),
                'forks': data.get('forks_count'),
                'language': data.get('language'),
                'topics': data.get('topics'),
                'default_branch': data.get('default_branch'),
                'open_issues': data.get('open_issues_count'),
                'pushed_at': data.get('pushed_at'),
                'html_url': data.get('html_url'),
            }, indent=1)[:4000]
        if name == 'github_list_issues':
            owner, repo = _owner_repo(args)
            if not owner or not repo:
                return 'owner and repo are required'
            state = (args.get('state') or 'open').strip()
            limit = min(int(args.get('limit') or 10), 30)
            data, error = _get(
                f'{base}/repos/{q(owner, safe="")}/{q(repo, safe="")}/issues?state={state}&per_page={limit}',
                'list GitHub issues')
            if error:
                return f'Could not list issues: {error}'
            items = data if isinstance(data, list) else []
            rows = [{'number': it.get('number'), 'title': it.get('title'),
                     'state': it.get('state'), 'labels': [l.get('name') for l in (it.get('labels') or [])],
                     'comments': it.get('comments'), 'html_url': it.get('html_url')}
                    for it in items if 'pull_request' not in it]
            return json.dumps(rows, indent=1)[:4000]
        if name == 'github_get_issue':
            owner, repo = _owner_repo(args)
            number = args.get('issue_number')
            if not owner or not repo or number is None:
                return 'owner, repo, and issue_number are required'
            repo_base = f'{base}/repos/{q(owner, safe="")}/{q(repo, safe="")}'
            data, error = _get(f'{repo_base}/issues/{int(number)}', 'read a GitHub issue')
            if error:
                return f'Could not read the issue: {error}'
            if data.get('pull_request'):
                return f'#{number} is a pull request, not an issue -- use the issues list.'
            comments_data, comments_error = _get(f'{repo_base}/issues/{int(number)}/comments?per_page=20', 'read issue comments')
            comments = []
            if not comments_error and isinstance(comments_data, list):
                comments = [{'user': (c.get('user') or {}).get('login'), 'body': c.get('body')[:1000]}
                            for c in comments_data]
            return json.dumps({
                'number': data.get('number'), 'title': data.get('title'),
                'state': data.get('state'), 'labels': [l.get('name') for l in (data.get('labels') or [])],
                'body': (data.get('body') or '')[:2000], 'html_url': data.get('html_url'),
                'top_comments': comments,
            }, indent=1)[:5000]
        if name == 'github_search_code':
            query = (args.get('query') or '').strip()
            if not query:
                return 'query is required'
            limit = min(int(args.get('limit') or 5), 10)
            data, error = _get(f'{base}/search/code?q={q(query, safe="")}&per_page={limit}', 'search GitHub code')
            if error:
                return f'Could not search code: {error}'
            items = (data or {}).get('items') or []
            rows = [{'repo': ((it.get('repository') or {}).get('full_name')),
                     'path': it.get('path'), 'html_url': it.get('html_url')} for it in items]
            return json.dumps({'total_count': data.get('total_count'), 'matches': rows}, indent=1)[:4000]
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


_SPIKE_REPORT_DELIVERABLE_SIGNALS = (
    'library file', 'saved file', 'saved deliverable', 'saved artifact',
    'missing deliverable', 'missing report', 'report is missing',
    'write the file', 'save the file', 'save a file', 'write a report',
    'saved report', 'deliverable is missing', 'artifact is missing',
)

_SPIKE_ISSUE_PROBLEM_SIGNALS = (
    'gap', 'problem', 'missing', 'broken', 'needs', 'fails', 'failed', 'unable',
    'should', 'recommend', 'issue', 'risk', 'limitation', 'error', 'bug',
    'outdated', 'stale', 'vulnerability', 'unmet', 'incomplete', 'crashed',
    'crash', 'not work', 'doesn\'t work', 'can\'t', 'inconsisten', 'incorrect',
    'defect', 'regression', 'worrying', 'concern', 'concerned', 'critical',
)


def _spike_file_issue_wish(finding, task, name, backlog):
    """W3: does this spike's finding identify a real, actionable gap worth
    filing as an issue, and if so, what should the wish look like?

    Workers have almost no autonomous self-proposal path (file_issue is
    HTTP-endpoint-only). A worker who spots a real gap mid-spike currently has
    no model-driven way to propose it. This is the model-driven probe: run in
    the executor thread against the snapshot, it returns a fileIssue WISH dict
    -- it NEVER files anything itself (file_issue mutates shared state and must
    only run inside the tick's single read-modify-write, in sim.py). The sim
    side resolves the owning team against LIVE state, dedups, and files.

    Cheap first: a deterministic problem-signal pre-filter on the finding
    text. No signal -> 'none' -> no Jev spend at all (most spikes are
    informational and should stay that way -- a spike worker who files on
    every investigation would be spamming the backlog). Only when a signal is
    present do we ask Jev for an explicit file/no-file verdict (file_bug /
    file_story / none / inconclusive -- the fourth option exists because a
    large share of real findings report "could not verify" rather than "all
    fine", and forcing that class into the none-vs-bug binary was the single
    biggest driver of low-confidence triage answers)."""
    signals = _SPIKE_ISSUE_PROBLEM_SIGNALS
    if not (finding or '').strip():
        return None
    haystack = ' '.join((finding or '').split()).lower()
    if not any(sig in haystack for sig in signals):
        return None
    headline = next((line.strip() for line in (finding or '').splitlines() if line.strip()), None)
    if not headline:
        headline = (backlog or '').strip() or 'gap found during spike'  # pragma: no cover -- the empty-finding pre-filter above guarantees a non-blank first line
    prompt = (
        f'{name} just finished a time-boxed spike on "{backlog}" and reported this '
        f'finding:\n\n"{finding[:1500]}"\n\n'
        f'Does this finding identify at least one concrete, real, actionable gap '
        f'-- a bug, a missing capability, a broken integration, or a real '
        f'limitation worth filing as a backlog issue? Or is it just an '
        f'informational investigation into something that is actually fine?')
    decision = _serve._call_openrouter_decision_sync(
        _serve._jev_model(), {'messages': [], 'signals': {}},
        {'choice': {'type': 'choice', 'instructions': prompt,
                    'criteria': {
                        'file_bug': 'Yes -- it names a concrete defect or breakage that should be FIXED (something behaves wrongly or not at all).',
                        'file_story': 'Yes -- it names a real missing capability or gap worth BUILDING or adding (not a defect, but something that should exist).',
                        'none': 'No -- the finding is informational, says things are fine, or only surfaces expected limitations with nothing worth filing.',
                        'inconclusive': 'No -- the finding could not gather enough evidence to say either way (unverified, blocked access, empty workspace): not worth filing until someone re-runs it with working access.'}},
             '_kind': 'finding_triage'})
    choice, _, _ = _serve._jev_choice(decision)
    if choice not in ('file_bug', 'file_story'):
        return None
    feature = (task.get('projectLabel') or task.get('productId') or task.get('room') or 'pressoffice') or 'pressoffice'
    title = headline[:140]
    summary = f'{name} ({backlog[:120]}): {headline}'[:300]
    # A finding whose remedy is a saved document is a report card, not a code
    # fix. Mint it as a spike with an explicit report deliverable so the
    # completion gate has a contract it can satisfy (task-417 looped because
    # this became a bug card that could never publish code).
    report_deliverable = any(sig in haystack for sig in _SPIKE_REPORT_DELIVERABLE_SIGNALS)
    issue_type = 'bug' if choice == 'file_bug' else 'story'
    deliverable = 'code'
    if report_deliverable:
        issue_type = 'spike'
        deliverable = 'report'
    return {
        'issueType': issue_type,
        'deliverable': deliverable,
        'summary': summary,
        'title': title,
        'feature': feature,
        'description': (finding or '')[:3000],
        'teamId': task.get('teamId'),
    }


def _run_meeting_content(snapshot, agent_id, task, base_ctx=None):
    """A MEETING card: the facilitator (task['facilitatorId'], falling
    back to the assigned agent) convenes the meeting and writes the
    minutes into the card's note -- the note IS the deliverable, and
    the card closes on it (taskType 'meeting' is a non-gated lane, so
    no peer gate opens). One non-tool completion: a meeting is a
    single deliberation, not a multi-phase investigation. The
    facilitator role-plays the convened attendees discussing the
    agenda, then records topic / attendees / discussion / decisions /
    action items with owners. Fail-closed like every executor: a
    meeting that cannot write minutes still lands an explicit note
    (completion evidence is mandatory -- never a silent empty card)."""
    import sim as _sim_module
    name = _agent_name(snapshot, agent_id)
    title = task.get('title') or ''
    agenda = (task.get('instructions') or '').strip()
    full_transcript = '[FULL TRANSCRIPT]' in agenda.upper()
    if full_transcript:
        agenda = agenda.replace('[FULL TRANSCRIPT]', '').replace('[full transcript]', '').strip()
    facilitator_id = task.get('facilitatorId')
    facilitator_name = _agent_name(snapshot, facilitator_id) if facilitator_id else name
    attendee_names = [_agent_name(snapshot, aid)
                      for aid in (task.get('attendees') or [])]
    if facilitator_name not in attendee_names:
        attendee_names = [facilitator_name] + attendee_names

    # Item 4 gate up front: a meeting that has already used its full
    # model-spend budget is failed closed BEFORE it resolves a tier or
    # makes any call -- no further money is spent, and the sim sees
    # budgetExhausted and runs its notify + fail-closed branch.
    if _serve._task_budget_exhausted(task.get('id')):
        spent, attempts = _serve._task_budget_spent(task.get('id'))
        _sim_module._store_content_result(task.get('id'), {
            'ok': False,
            'budgetExhausted': True,
            'taskSpendUsd': spent,
            'taskSpendAttempts': attempts,
            'note': f'Meeting "{title}" was paused before it started: this task '
                    f'already used its full model-spend budget (${spent:.4f} spent).',
        })
        return
    tier_slug = _serve._resolve_model_tier(
        f'Facilitate a team meeting and write up the minutes: {title[:200]}')
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Held "{title}", but no model tier is configured yet.',
            'ok': False,
        })
        return
    if full_transcript:
        system = (
            'You are writing the full transcript of a panel discussion in an '
            'AI think tank. The meeting has already happened in-world. Write '
            'the complete conversation turn by turn, with each attendee '
            'speaking in their own voice. Include real disagreement, evidence '
            'from the source, and a final decisions/action-items section. Do '
            'not summarize or compress the discussion into minutes. Do not '
            'invent facts that are not in the agenda or source.'
        )
        user = (f"Panel: {title}\n\nAgenda and source material:\n{agenda or '(none given)'}\n\n"
                f"Facilitator: {facilitator_name}\n"
                f"Attendees: {', '.join(attendee_names) or '(none)'}\n\n"
                'Write the full panel conversation now, with a speaker label on every turn.')
        max_tokens = 7000
    else:
        minutes_guide = skill_prompts.load_skill_prompt(
            'meeting-minutes', fallback=_MEETING_MINUTES_FALLBACK)
        system = (
            'You are the scribe for a team meeting in an AI think tank. The '
            'meeting has already happened in-world: the facilitator convened the '
            'listed attendees, they discussed the agenda, and you are writing the '
            'minutes now. Follow this playbook:\n\n'
            f'{minutes_guide}'
        )
        user = (f"Meeting: {title}\n\nAgenda:\n{agenda or '(none given)'}\n\n"
                f"Facilitator: {facilitator_name}\n"
                f"Attendees: {', '.join(attendee_names) or '(none)'}\n\n"
                'Write the minutes now.')
        max_tokens = 1200
    minutes = _plain_completion(tier_slug, [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ], max_tokens, service='meeting', task_id=task.get('id'))
    if minutes:
        _sim_module._store_content_result(task.get('id'), {
            'note': minutes, 'ok': True,
        })
        return
    _sim_module._store_content_result(task.get('id'), {
        'note': f'Meeting "{title}" was convened by {facilitator_name} with '
                f"{', '.join(attendee_names) or 'no attendees'}, but the "
                'minutes could not be written (the model call failed).',
        'ok': False,
    })


def _spike_first_tool(backlog, instructions):
    """Pick the one tool a spike must call before it may settle. Kept as a
    named function so the social path is directly testable: a Facebook or
    Instagram investigation must mint its read-only handle first, not spend
    turn one on the curated RSS feeds."""
    if _spike_wants_social_login(backlog, instructions):
        return 'search_web' if _serve.TAVILY_API_KEY else True
    if _spike_wants_internal_review(backlog, instructions):
        return 'search_library'
    if _spike_wants_x_trending(backlog, instructions):
        return 'x_trending_topics'
    if _spike_wants_linkedin_search(backlog, instructions):
        return 'search_linkedin_posts'
    if _spike_wants_github(backlog, instructions):
        return 'github_get_repo'
    if _spike_wants_ais_feed(backlog, instructions):
        return 'read_ais_feed'
    if _spike_wants_rss(backlog, instructions):
        return 'read_rss_feed'
    if _spike_wants_market(backlog, instructions):
        return 'read_market_feed'
    return 'search_web' if _serve.TAVILY_API_KEY else True


def _spike_work_window_s(task):
    """Wall-clock window for one spike attempt, in seconds.

    The generic spike window is 300s. A transcript spike can block inside
    /api/youtube-transcript for up to 600s (Apify actor + Colab Whisper), so
    that path gets 900s. An explicit budgetMs always wins and is clamped to
    5..30 minutes so a malformed value cannot pin a task forever.
    """
    budget = (task or {}).get('budgetMs')
    try:
        budget_s = float(budget) / 1000.0 if budget else 0.0
    except (TypeError, ValueError):
        budget_s = 0.0
    if budget_s <= 0:
        instructions = (task or {}).get('instructions') or ''
        budget_s = 900.0 if 'youtube_transcript' in instructions.lower() else 300.0
    return max(300.0, min(1800.0, budget_s))
