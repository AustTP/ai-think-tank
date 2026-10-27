

def _breakdown_into_shared_backlog(state, goal, admin_id, constraints=None):
    """WS-14: when every team is busy in a sprint, break a large ask into as
    many stories/spikes as the work actually requires (never a fixed count --
    a small ask may be a single card) and file them in the SHARED unassigned backlog under a
    feature (reusing the feature by name when one already exists). Returns
    {'feature': <record>, 'items': [...]}, or None if the model call fails /
    yields nothing usable -- the caller then falls back to spawning a team.
    The breakdown is attributed to the chief of staff (the standing authority); a team
    pulls the items first-come when a sprint closes. BLOCKING urllib call --
    callers must run it off-thread (asyncio.to_thread) so the /api/chat
    loopback never deadlocks the single-worker event loop.

    `constraints` is the OPTIONAL player-pin dict (same shape as
    sim.file_large_request's); absent/empty means plan freely. Pins are
    CONTEXT for the model's judgment, not mechanical overrides."""
    if not admin_id:
        return None
    import sim as _sim
    pin_text = ''
    budget_field = ''
    if isinstance(constraints, dict) and constraints:
        bits = []
        if constraints.get('budgetUsd'):
            bits.append(f"The player named a total budget of ${float(constraints['budgetUsd']):.2f} "
                        "for this whole ask. Allocate it across the cards with judgment "
                        "-- larger/riskier cards may deserve more, trivial ones less. "
                        "Set each card's \"budgetUsd\" field to its allocated share "
                        "(a positive number).")
            budget_field = '"budgetUsd":number-or-omit,'
        if constraints.get('constraints'):
            bits.append('The player listed these hard prohibitions the team must respect: '
                        + '; '.join(str(c) for c in constraints['constraints']) + '.')
        if constraints.get('roles'):
            bits.append('The player prefers cards suited to these roles/skills: '
                        + '; '.join(str(r) for r in constraints['roles']) + '.')
        if constraints.get('order'):
            bits.append('The player wants this rough sequencing (earlier items '
                        'should be planned before later ones): '
                        + ' -> '.join(str(o) for o in constraints['order']) + '.')
        if constraints.get('mustInclude'):
            bits.append('The player named these tasks as important to cover '
                        '(factor them into the plan): '
                        + '; '.join(str(m) for m in constraints['mustInclude']) + '.')
        if bits:
            pin_text = ' ' + ' '.join(bits)
    system_prompt = (
        'You are the chief of staff of a small think tank. A large task just arrived, but every '
        'team is already committed to an active sprint, so the task cannot be staffed right now.' + pin_text + ' '
        'Break it into as many concrete stories (or spikes for investigation-first work) as the work actually '
        'requires -- at least one, never a fixed count: a small ask may be a single card, a sprawling one may need '
        'many. Each card is a single worker-sized card to be filed in the shared backlog and pulled by a team when '
        'its sprint closes. '
        'Do NOT assign a room to a card -- the rooms are shared across teams and the agent who picks a '
        'card up figures out where the work needs to happen. '
        'Give a short FEATURE name (3-6 words) that groups these cards under one initiative. '
        'Keep every title to ONE short sentence. For each card set "type" to "story" (deliverable work), '
        '"spike" (an investigation with no committed deliverable -- use it when the right approach is '
        'not yet known), or "research" (a fact-gathering/analysis sweep that produces findings, not a '
        'shipped deliverable -- document studies, competitive teardown, desk research), '
        'a one-line "acceptanceCriteria" when the story has a clear test of done, a '
        '"never" list ONLY when the card has hard prohibitions the worker must never do (omit otherwise), and a '
        '"sizeEstimate" of S/M/L. '
        'Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this shape: '
        '{"feature":"short feature name","items":[{"title":"short title","type":"story|spike|research","acceptanceCriteria":"one line or omitted","never":["one hard constraint or omit"],"sizeEstimate":"S|M|L",' + budget_field + '}]}'
    )
    key = get_or_create_agent_key(admin_id)
    # High-stakes planning tier, JEV-gated: a large-request breakdown is exactly
    # the "damn good reason to use high" case -- the whole downstream depends on
    # this one call (same gate as the staffable path below).
    plan_tier = _resolve_model_tier(
        f'Breaking an unstaffable large request into shared-backlog stories: {goal[:200]}',
        allow_high=True)
    r = _http_json('POST', SELF_BASE_URL, '/api/chat',
                   {'model': plan_tier,
                    'messages': [{'role': 'system', 'content': system_prompt},
                                 {'role': 'user', 'content': goal}],
                    'max_tokens': _big_task_max_tokens,
                    'agentId': admin_id}, key, timeout=90)
    if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
        return None
    cleaned = r['reply'].strip()
    cleaned = re.sub(r'^```json\s*|^```\s*|```\s*$', '', cleaned)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    items = [s for s in (parsed.get('items') or [])
             if isinstance(s, dict) and s.get('title')]
    if not items:
        return None
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    feature = _sim.create_or_reuse_feature(state, parsed.get('feature') or goal[:60],
                                           now_ms, created_by=admin_id)
    out = []
    for s in items:
        never = list(s.get('never') or []) if isinstance(s.get('never'), (list, tuple)) else []
        item = _sim.add_backlog_item(
            state, s['title'], feature['id'], admin_id, now_ms,
            item_type=s.get('type') or 'story',
            acceptance_criteria=s.get('acceptanceCriteria'),
            size_estimate=s.get('sizeEstimate'),
            never=never or None)
        if item:
            # Budget: pick up whatever share the model allocated (its
            # judgment, informed by the player's total); a missing share means
            # the model chose not to budget this card.
            raw_budget = s.get('budgetUsd')
            if isinstance(raw_budget, (int, float)) and raw_budget > 0:
                item['budgetUsd'] = float(raw_budget)
            out.append(item)
    if not out:
        return None
    return {'feature': feature, 'items': out}


async def _dispatch_big_task(state, goal, actor, constraints=None):
    """Core routing for a large request, shared by /api/intent/assign-big-task
    and the x-mention webhook consumer loop: when EVERY existing team is
    committed to an active sprint, the ask is broken down into stories/spikes
    filed in the SHARED backlog under a feature -- any team pulls from it
    (first-come) when a sprint closes, so the work is captured without
    spawning an unbounded number of teams. If the breakdown fails (model
    outage), fall back to spawning a new director + team to take the ask
    (with an employee; the director stands in as scrum master while the team
    is small) and kick its refinement to run on the very next pass.
    Otherwise a free authority (waking a merely-resting one if needed) staffs
    a team from the ask alone and the ask is filed as a pending
    large-request breakdown for that team's breakdown ceremony. No sprint
    record is created and no subtasks are predicted up front: the receiving
    team plans its own work, like a real org.
    Returns (outcome, data), outcome one of 'backlogged', 'new_team',
    'no_authority', 'no_team', 'failed', 'staffed'."""
    import sim as _sim
    if _all_teams_busy_in_sprint(state):
        admin_id_for_new = next((d.get('id') for d in (state.get('agentRoster') or [])
                                 if d.get('isAdmin')), None)
        backlogged = await asyncio.to_thread(_breakdown_into_shared_backlog,
                                             state, goal, admin_id_for_new,
                                             constraints=constraints)
        if backlogged:
            save_state_to_db(state)
            log_action(actor, 'big_task_backlogged',
                       {'goal': goal[:200],
                        'feature': backlogged['feature'].get('id'),
                        'items': [b.get('id') for b in backlogged['items']]},
                       authorized=True)
            return 'backlogged', backlogged
        # Fallback: the model couldn't break it down -- spawn a fresh team.
        new_team = _sim.spawn_new_team_for_request(state, goal, admin_id=admin_id_for_new,
                                                   constraints=constraints)
        if new_team:
            save_state_to_db(state)
            log_action(actor, 'big_task_new_team',
                       {'goal': goal[:200], 'team': new_team.get('id'),
                        'members': new_team.get('members')}, authorized=True)
            return 'new_team', new_team
    authority = _free_authority(state)
    if not authority:
        # tasks.js: "a new request from you wakes the admin." Honor that --
        # if everyone is merely RESTING (offDuty, not busy), wake one so the
        # think tank isn't permanently un-delegable the moment it goes idle.
        authority = _wake_authority_on_request(state)
        if authority:
            save_state_to_db(state)
    if not authority:
        return 'no_authority', {}
    admin_id = authority['id']
    # Sprint staffing: the free authority picks the receiving team from the
    # ask alone -- her OWN team when it's free, else the first free team --
    # and the ask is filed as a pending large-request breakdown for that
    # team's breakdown ceremony (scrum master + workers) to card into
    # stories/spikes on the next pass.
    team = _staffing_team_for_authority(state, admin_id)
    if not team:
        return 'no_team', {}
    team_id = team.get('id') or team.get('directorId')
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    req = _sim.file_large_request(state, admin_id, goal, team_id, now_ms,
                                  constraints=constraints)
    if not req:
        return 'failed', {}
    save_state_to_db(state)
    log_action(actor, 'big_task_staffed',
               {'admin': admin_id, 'goal': goal[:200], 'team': team_id,
                'request': req['id'],
                'pinned': sorted((req.get('playerConstraints') or {}).keys())}, authorized=True)
    return 'staffed', {'admin': admin_id, 'adminName': authority.get('name'),
                       'team': team_id, 'teamName': team.get('name') or team_id,
                       'request': req['id']}


@app.post('/api/intent/assign-big-task')
@_state_writer
async def intent_assign_big_task(request: Request):
    """Player intent: 'delegate / big task'. Server-side assignBigTask. Requires
    a logged-in session (the player). Returns {admin, subtasks:[...]} like the
    client's assignBigTask, so the UI can report what was queued."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    goal = (body.get('goal') or '').strip()
    if not goal:
        return JSONResponse({'error': 'no goal given'}, status_code=400)

    # OPTIONAL player pins (all absent = the tank plans freely, the default).
    # Only fields the player actually filled in are honored; a malformed pin
    # is dropped, never a reason to refuse the whole ask.
    constraints = None
    raw_c = body.get('constraints')
    if isinstance(raw_c, dict) and raw_c:
        constraints = raw_c

    # Logged-in player (the /api/intent prefix is auth-protected above; the
    # session is a player login, not an agent -- there's no agent id on it, so
    # the action is attributed to the player).
    player_id = 'player'

    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)

    # Large-request routing is shared with the webhook consumer loop: see
    # _dispatch_big_task (backlog breakdown / new-team / staffing).
    outcome, data = await _dispatch_big_task(state, goal, player_id,
                                             constraints=constraints)
    if outcome == 'backlogged':
        return JSONResponse({'ok': True, 'backlogged': True,
                             'feature': data['feature'].get('id'),
                             'featureName': data['feature'].get('name'),
                             'items': len(data['items']),
                             'note': 'Every existing team was busy in a sprint, so this request was broken down and filed in the shared backlog under a feature. A team pulls from it (first-come) when a sprint closes.'})
    if outcome == 'new_team':
        return JSONResponse({'ok': True, 'newTeam': True,
                             'team': data.get('id'),
                             'teamName': data.get('name'),
                             'members': data.get('members'),
                             'note': 'Every existing team was busy in a sprint, so a new director + team was created to take this request. It will start a backlog refinement immediately.'})
    if outcome == 'no_authority':
        return JSONResponse({'error': 'No admin or director is free right now -- try again shortly.'})
    if outcome == 'no_team':
        return JSONResponse({'error': 'No free team is available to take this request right now -- try again shortly.'})
    if outcome == 'failed':
        return JSONResponse({'error': "Couldn't file the request -- try again shortly."}, status_code=500)
    return JSONResponse({'ok': True, 'staffed': True,
                         'admin': data['admin'],
                         'team': data['team'],
                         'teamName': data['teamName'],
                         'request': data['request'],
                         'note': f'{data["adminName"]} filed this with the {data["teamName"]} team. Its scrum master and workers will break it into concrete stories at their next planning session.'})


@app.post('/api/intent/story/{task_id}/reject')
@_state_writer
async def intent_reject_story(task_id: str, request: Request):
    """Phase E3.4 player quality-veto: the player (session holder) sends a
    closed story back to its ORIGINAL AUTHOR for rework. Reuses the same
    gate-reopen machinery a reviewer rejection uses (_enter_peer_review), which
    PREFERS the reviewers who already know the story (see _enter_peer_review's
    `preferred`) -- a rejection is a request to verify the flagged problem was
    actually fixed, which a cold stranger cannot do. If either reviewer returns
    it actionable, the fix routes back to the author who built it (via
    reviewAuthorId -- the c1bcc27 routing fix).

    Only a 'done' story is rejectable; a story already back in needs_review
    isn't (that's the reviewers' call, and idempotency guards against double
    veto). This is a rare, heavy principal signal -- the player is the EXTERNAL
    gauge the internal peer gate can never be, since agents only approve agents.
    """
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = (body.get('reason') or '').strip()

    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)

    task = (state.get('tasks') or {}).get(task_id)
    if not task:
        return JSONResponse({'error': f'unknown story: {task_id}'}, status_code=404)
    if task.get('status') != 'done':
        return JSONResponse(
            {'error': f'"{task.get("title")}" is not closed -- only a delivered story can be sent back to its author.'},
            status_code=409)
    author = task.get('assignedTo')
    if not author:
        return JSONResponse({'error': 'This story has no recorded author, so it can\'t be sent back.'}, status_code=409)

    import sim as _sim
    now_ms = int(time.time() * 1000)
    gate = _sim._enter_peer_review(state, task, now_ms)
    if gate is None:
        # No eligible reviewers right now (tiny/unready think tank). Fail closed --
        # leave the story done rather than silently dropping it into limbo.
        return JSONResponse({'error': 'No reviewer is available to re-check this right now -- try again shortly.'},
                            status_code=409)

    # Ripple re-review (dependency cascade): the veto re-opened a DELIVERED
    # story, so any story that DEPENDED on it was built on the now-questioned
    # output. Send shipped dependents back through their own peer gates too.
    cascaded = _sim._cascade_rereview(state, task_id, now_ms)

    # Veto is a first-class actor action: file a mailbox note to the author so
    # they know it's the PLAYER (not a peer) sending their work back, and chain
    # the veto into the passport alongside sprint/product actions. Routed through
    # _append_mailbox so the player-veto write honors MAILBOX_KEEP_COUNT like every
    # other mailbox append (a direct append here would grow the blob unboundedly).
    _sim._append_mailbox((state.get('agents') or {}).get(author, {}), {
        'kind': 'player_veto',
        'about': task_id,
        'title': task.get('title'),
        'text': ('The player reviewed your delivered work on "{}" and sent it back to you for rework.'
                 + (' They said: "{}".' if reason else '')).format(task.get('title'), reason),
    })

    save_state_to_db(state)
    player_id = 'player'
    log_action(player_id, 'story_vetoed',
               {'taskId': task_id, 'title': (task.get('title') or '')[:200],
                'author': author, 'reason': reason[:300], 'cascaded': cascaded},
               authorized=True)
    _append_passport_decision('story_vetoed', player_id,
                              {'taskId': task_id, 'title': (task.get('title') or '')[:200],
                               'author': author, 'reason': reason[:300], 'cascaded': cascaded})
    return JSONResponse({'ok': True, 'taskId': task_id,
                         'author': author,
                         'reviewers': gate['reviewerIds'],
                         'cascaded': cascaded})


# Player-triggered publish destination. Configurable per install -- each
# deployment pushes to ITS OWN repo, so no one's account is baked into the
# tree. Set AI_THINK_TANK_PUBLISH_REPO to "owner/repo" in .env (create the target
# private repo first -- the app never creates it). When unset, the publish
# endpoint returns a clear error rather than guessing.
PUBLISH_REPO = (_load_env().get('AI_THINK_TANK_PUBLISH_REPO') or '').strip()
PUBLISH_REMOTE_URL = ('https://github.com/' + PUBLISH_REPO + '.git') if PUBLISH_REPO else ''
PUBLISH_STAGING = os.path.join(LIBRARY_DIR, 'publish-staging')
PUBLISH_TIMEOUT_S = 60


def _stage_released_work(state):
    """Freeze the think tank's PRODUCED output into a staging dir for one publish
    pass: per-project snapshots under projects/, plus wiki pages and skills if
    any. The staging dir is gitignored so it never pollutes the main repo. Only
    released, player-facing work is staged -- internal queues (archive,
    downloads, pending_review, shared) are excluded."""
    shutil.rmtree(PUBLISH_STAGING, ignore_errors=True)
    os.makedirs(PUBLISH_STAGING, exist_ok=True)

    staged = []
    # Released product snapshots + their dev history (LOG, RETROSPECTIVE, playtest
    # rounds, research, qa/review). Each project dir is copied whole.
    projects_dir = os.path.join(LIBRARY_DIR, 'projects')
    for project in sorted(os.listdir(projects_dir)):
        src = os.path.join(projects_dir, project)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(PUBLISH_STAGING, 'projects', project)
        shutil.copytree(src, dst)
        staged.append(os.path.join('projects', project))

    # Wiki pages: library/wiki/<category>/<id>.md (if the think tank has written any).
    wiki_dir = os.path.join(LIBRARY_DIR, 'wiki')
    if os.path.isdir(wiki_dir):
        for category in sorted(os.listdir(wiki_dir)):
            cat_src = os.path.join(wiki_dir, category)
            if not os.path.isdir(cat_src):
                continue
            cat_dst = os.path.join(PUBLISH_STAGING, 'wiki', category)
            os.makedirs(cat_dst, exist_ok=True)
            for fname in sorted(os.listdir(cat_src)):
                if fname.endswith('.md'):
                    shutil.copy2(os.path.join(cat_src, fname), os.path.join(cat_dst, fname))
                    staged.append(os.path.join('wiki', category, fname))

    # Skills the think tank has authored.
    skills_dir = os.path.join(LIBRARY_DIR, 'skills')
    if os.path.isdir(skills_dir):
        for fname in sorted(os.listdir(skills_dir)):
            src = os.path.join(skills_dir, fname)
            if os.path.isfile(src) and fname.endswith('.md'):
                shutil.copy2(src, os.path.join(PUBLISH_STAGING, 'skills', fname))
                staged.append(os.path.join('skills', fname))

    return staged


def _write_publish_readme(staged):
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    lines = [
        '# ai-think-tank publish',
        '',
        'Released work produced by the ai-think-tank multi-agent simulation.',
        'These are frozen snapshots of what the think tank\'s agents actually built',
        'and shipped -- released product revisions, wiki pages, and authored',
        'skills. Each publish is a player-triggered, passport-chained export.',
        '',
        f'Last published: {now}',
        '',
        '## Contents',
    ]
    for p in sorted(staged):
        lines.append(f'- {p}')
    lines.append('')
    _write_file(os.path.join(PUBLISH_STAGING, 'README.md'), '\n'.join(lines))


@app.post('/api/intent/publish')
async def intent_publish(request: Request):
    """Player-triggered push of the think tank's released work to the configured
    publish repo (AI_THINK_TANK_PUBLISH_REPO). Nothing leaves the machine unless
    the player (session holder) explicitly asks -- agents stay scoped to their
    own work. Stages released projects/wiki/skills into a gitignored staging
    dir, commits with a passport-linked message, and pushes."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)

    if not PUBLISH_REPO:
        return JSONResponse(
            {'error': 'Publish is not configured -- set AI_THINK_TANK_PUBLISH_REPO=owner/repo in .env (create the target private repo first).'},
            status_code=409)
    staged = _stage_released_work(state)
    if not staged:
        return JSONResponse(
            {'error': 'Nothing produced yet -- the think tank has not released any project or wiki page to publish.'},
            status_code=409)
    _write_publish_readme(staged)

    try:
        _run_git_sync(PUBLISH_STAGING, ['init', '-q'])
        _run_git_sync(PUBLISH_STAGING, ['config', 'user.email', 'thinktank@ai-think-tank.local'])
        _run_git_sync(PUBLISH_STAGING, ['config', 'user.name', 'AI Think Tank'])
        _run_git_sync(PUBLISH_STAGING, ['add', '-A'])
        _run_git_sync(PUBLISH_STAGING, ['commit', '-q', '-m',
                                        f'publish think tank work ({len(staged)} entries)'])
        # Push over https using the authenticated gh token; the staging .git is
        # removed right after, so the token never persists in the main repo.
        token = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True).stdout.strip()
        authed = PUBLISH_REMOTE_URL.replace('https://', f'https://x-access-token:{token}@')
        _run_git_sync(PUBLISH_STAGING, ['remote', 'add', 'origin', authed])
        # The push is a real network round-trip; give it its own (longer) budget
        # than the 10s sandbox-backup timeout _run_git_sync uses by default.
        push = subprocess.run(
            ['git', 'push', '-u', 'origin', 'HEAD:main'], cwd=PUBLISH_STAGING,
            capture_output=True, text=True, timeout=PUBLISH_TIMEOUT_S)
        if push.returncode != 0:
            # Push reached git but the remote rejected it; surface git's own
            # stderr (e.g. non-fast-forward, auth) rather than claiming success.
            detail = (push.stderr or push.stdout or 'push failed').strip()[-400:]
            return JSONResponse({'error': f'push rejected: {detail}'}, status_code=502)
    except subprocess.TimeoutExpired:
        shutil.rmtree(PUBLISH_STAGING, ignore_errors=True)
        return JSONResponse({'error': 'publish timed out pushing to GitHub -- try again.'}, status_code=502)
    except Exception as exc:  # noqa: BLE001 - surface a clean 502 to the player
        shutil.rmtree(PUBLISH_STAGING, ignore_errors=True)
        return JSONResponse({'error': f'publish failed: {exc}'}, status_code=502)
    finally:
        # The staging .git now holds an ephemeral token in its remote URL; drop
        # the whole staging tree so neither it nor the token survives.
        shutil.rmtree(PUBLISH_STAGING, ignore_errors=True)

    player_id = 'player'
    log_action(player_id, 'artifact_published',
               {'repo': PUBLISH_REPO, 'entries': len(staged), 'staged': staged}, authorized=True)
    _append_passport_decision('artifact_published', player_id,
                              {'repo': PUBLISH_REPO, 'entries': len(staged), 'staged': staged})
    return JSONResponse({'ok': True, 'repo': PUBLISH_REPO,
                         'entries': len(staged), 'staged': staged,
                         'push': (push.stdout or '')[-200:] or (push.stderr or '')[-200:]})


@app.post('/api/intent/spike/{task_id}/promote')
@_state_writer
async def intent_promote_spike(task_id: str, request: Request):
    """Phase E3.5 spike->triage: the player promotes a completed spike's findings
    into a REAL, queued deliverable story. A spike (E2b) lands a findings note and
    then dead-ends -- this is the loop-closing link: 'investigate -> player decides
    -> real work'. The player is the principal; agents never self-direct follow-ups.

    The spike task stays 'done' (an immutable record of the investigation); a NEW
    deliverable story is queued (default pressoffice/code) whose instructions embed
    the finding, so the follow-up rides the normal peer-gate path -- same quality
    bar as any committed work, not a silent pass. Only a DONE spike is promotable
    (a working one isn't finished); 409 otherwise keeps the player from half-baking
    an investigation into real work.
    """
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)

    task = (state.get('tasks') or {}).get(task_id)
    if not task:
        return JSONResponse({'error': f'unknown spike: {task_id}'}, status_code=404)
    if task.get('taskType') != 'spike':
        return JSONResponse({'error': f'Task "{task_id}" is not a spike, so it has no findings to promote.'},
                            status_code=409)
    if task.get('status') != 'done':
        return JSONResponse({'error': f'"{task.get("title")}" is not finished -- promote it once the spike lands.'},
                            status_code=409)

    try:
        body = await request.json()
    except Exception:
        body = {}
    room = (body.get('room') or 'pressoffice').strip()
    if room not in _DELEGATABLE_ROOMS:
        room = 'pressoffice'
    # Gap: this used to default a missing
    # taskType to 'code' -- a real test promotion of a purely informational
    # finding (no concrete "build X" recommendation at all) got silently
    # promoted into a taskType='code' story anyway, and burned 45+ review/
    # fix cycles because no reviewer could ever find real code that made
    # sense of a non-actionable finding. The player is the principal for
    # this decision (per this endpoint's own docstring) -- silently
    # defaulting took that decision away. Now REQUIRED, not defaulted;
    # 'spike' is a real, valid choice for a finding that just needs deeper
    # investigation, not a deliverable -- it stays out of the peer gate
    # entirely (NON_GATED_LANES), exactly the case that looped before.
    task_type = (body.get('taskType') or '').strip()
    if task_type not in ('code', 'review', 'qa', 'spike', 'research'):
        return JSONResponse({
            'error': ('taskType is required and must be one of "code", "review", "qa", "spike", or "research". '
                     'Choose "spike" when the finding is informational only (no concrete '
                     'recommendation to build) -- it skips peer review entirely rather than '
                     'looping on code no reviewer can approve. Choose "research" for open-ended '
                     'investigation of a question (routed to the observatory, never the code pipeline).'),
        }, status_code=400)

    import sim as _sim
    spike_title = task.get('title') or 'spike'
    # Gap: task.note is deliberately a SHORT
    # pointer ("see the Library entry just filed"), by design -- the FULL
    # findings (source lists, CSVs, feasibility data) only ever lived in the
    # Library file. Promoting a spike used to hand the new story's author
    # that vague pointer, never the actual research. Read the real file
    # directly (in-process, same as read_library_file -- no self-loopback
    # hop needed) when the spike recorded its exact path; fall back to the
    # short note for older spikes that predate this, or one with no file.
    finding = None
    library_path = task.get('libraryPath')
    if library_path:
        target = _safe_library_path(library_path)
        if target and os.path.isfile(target):
            try:
                with open(target, 'r', errors='replace') as f:
                    finding = f.read(20_000).strip()
            except OSError:
                finding = None
    if not finding:
        finding = (task.get('note') or '').strip() or '(the spike recorded no written findings)'
    if task_type == 'spike':
        # A deeper investigation, not a deliverable -- same shape queue_spike
        # itself builds, so this rides the real spike pipeline (plan-execute-
        # synthesize, library review tools, etc.), not a bare free-text task.
        new_item = {
            'title': f'Follow up: {spike_title}',
            'room': room,
            'instructions': (f'This is a follow-up to the spike "{spike_title}". Its finding was: {finding}. '
                             'Investigate further -- this does not need to produce a deliverable.'),
            'goal': task.get('goal') or spike_title,
            'taskType': 'spike',
            'budgetMs': task.get('budgetMs') or 60_000,
        }
    else:
        new_item = {
            'title': f'Follow up: {spike_title}',
            'room': room,
            'instructions': (f'This is a follow-up to the spike "{spike_title}". Its finding was: {finding}. '
                             'Pursue the recommendation into real work.'),
            'goal': task.get('goal') or spike_title,
            'taskType': task_type,
        }
    new_item['playerAuthored'] = True
    _sim.queue_work(state, [new_item])
    save_state_to_db(state)
    player_id = 'player'
    log_action(player_id, 'spike_promoted',
               {'spikeId': task_id, 'spikeTitle': spike_title[:200],
                'queuedTitle': new_item['title'], 'room': room,
                'taskType': task_type}, authorized=True)
    _append_passport_decision('spike_promoted', player_id,
                              {'spikeId': task_id, 'spikeTitle': spike_title[:200],
                               'queuedTitle': new_item['title'], 'room': room})
    return JSONResponse({'ok': True, 'queued': {
        'title': new_item['title'], 'room': room, 'taskType': task_type,
        'goal': task.get('goal') or spike_title,
    }})


# ---------------------------------------------------------------------------
# Bot Ops / shadow mode. A shadow (dry-run) work item does its work but ships
# nothing: on completion its outcome lands in the append-only state['shadowLedger']
# draft (see sim._complete_shadow_task) with NO peer gate, credits, deliverable,
# or dependency unblock. The PLAYER reviews the draft and, when satisfied, promotes
# an entry into REAL queued work -- the loop-closing link mirroring spike->triage.
# The player is the principal; agents never self-promote their own drafts.
# ---------------------------------------------------------------------------
@app.get('/api/shadow')
async def get_shadow_ledger(request: Request):
    """Read the shadow-mode draft ledger: every dry-run outcome captured, newest
    first, with its promotion status. Read-only review surface."""
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    entries = list(reversed(state.get('shadowLedger') or []))
    return JSONResponse({'ok': True, 'shadow': entries, 'count': len(entries)})


@app.post('/api/shadow/{idx}/promote')
@_state_writer
async def promote_shadow_entry(idx: str, request: Request):
    """Promote one shadow-ledger draft into a REAL, queued deliverable story. The
    dry-run outcome (its note/libraryPath) becomes the new task's instructions, so
    the follow-up rides the normal peer-gate path -- same quality bar as any
    committed work, not a silent pass. The ledger entry stays (marked 'promoted',
    an immutable record of the dry run); a NEW task is queued. Only a non-promoted
    entry is promotable; a 409 keeps the player from double-queueing the same draft.
    Body (optional): {'room', 'taskType'} to override the dry-run's defaults."""
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    ledger = state.get('shadowLedger') or []
    try:
        index = int(idx)
    except (TypeError, ValueError):
        return JSONResponse({'error': f'bad shadow index: {idx}'}, status_code=400)
    if index < 0 or index >= len(ledger):
        return JSONResponse({'error': f'unknown shadow entry: {idx}'}, status_code=404)
    entry = ledger[index]
    if entry.get('promoted'):
        return JSONResponse({'error': f'Shadow draft "{entry.get("title")}" was already promoted -- it is already real work.'}, status_code=409)

    try:
        body = await request.json()
    except Exception:
        body = {}
    room = (body.get('room') or entry.get('room') or 'pressoffice').strip()
    if room not in _DELEGATABLE_ROOMS:
        room = 'pressoffice'
    task_type = (body.get('taskType') or entry.get('taskType') or 'code').strip()
    if task_type not in ('code', 'review', 'qa', 'spike', 'research'):
        task_type = 'code'
    finding = (entry.get('note') or '').strip()
    library_path = entry.get('libraryPath')
    if library_path:
        target = _safe_library_path(library_path)
        if target and os.path.isfile(target):
            try:
                with open(target, 'r', errors='replace') as f:
                    finding = f.read(20_000).strip()
            except OSError:
                finding = None
    if not finding:
        finding = '(the shadow run recorded no written findings)'

    import sim as _sim
    if task_type == 'spike':
        new_item = {
            'title': f'Follow up: {entry.get("title")}',
            'room': room,
            'instructions': (f'This is the real follow-up to a shadow dry-run of "{entry.get("title")}". '
                             f'Its draft finding was: {finding}. Investigate further -- this does not need '
                             'to produce a deliverable.'),
            'goal': entry.get('projectLabel') or entry.get('title'),
            'taskType': 'spike',
            'budgetMs': 60_000,
        }
    else:
        new_item = {
            'title': f'Follow up: {entry.get("title")}',
            'room': room,
            'instructions': (f'This is the real follow-up to a shadow dry-run of "{entry.get("title")}". '
                             f'Its draft finding was: {finding}. Pursue the recommendation into real work.'),
            'goal': entry.get('projectLabel') or entry.get('title'),
            'taskType': task_type,
        }
    new_item['playerAuthored'] = True
    _sim.queue_work(state, [new_item])
    entry['promoted'] = True
    entry['promotedAt'] = int(time.time() * 1000)
    save_state_to_db(state)
    player_id = 'player'
    log_action(player_id, 'shadow_promoted',
               {'shadowIndex': index, 'title': (entry.get('title') or '')[:200],
                'queuedTitle': new_item['title'], 'room': room, 'taskType': task_type},
               authorized=True)
    _append_passport_decision('shadow_promoted', player_id,
                              {'shadowIndex': index, 'title': (entry.get('title') or '')[:200],
                               'queuedTitle': new_item['title'], 'room': room})
    return JSONResponse({'ok': True, 'promoted': index, 'queued': {
        'title': new_item['title'], 'room': room, 'taskType': task_type,
    }})


# ---------------------------------------------------------------------------
# Phase C: sprints + task feed. A sprint is a director-authored, time-boxed
# body of work whose items are tagged into the shared workQueue (see the
# queue_sprint/sprint_progress helpers in sim.py). The player (session holder)
# seeds a sprint on behalf of the acting authority (admin, else senior-most
# director, identical to assign-big-task); progress is derived server-side from
# live task + queue state. Creating/completing is chained into the hashed
# product-passport. The read endpoint (get_sprints) powers the board modal.
# /api/intent is protected by default (deny-by-default auth middleware).
# ---------------------------------------------------------------------------
@app.post('/api/intent/sprint')
@_state_writer
async def intent_sprint(request: Request):
    """Seed a new sprint: {name?, goal, items:[...], targetDate?}. Returns the
    sprint record + per-item queue confirmations so the UI can report."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    goal = (body.get('goal') or '').strip()
    items = body.get('items')
    if not goal or not isinstance(items, list) or not items:
        return JSONResponse({'error': 'goal and a non-empty items list are required'}, status_code=400)
    player_id = 'player'
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    authority = _free_authority(state)
    if not authority:
        return JSONResponse({'error': 'No admin or director is free right now, so nobody can sponsor this sprint. Try again shortly.'})
    owner_id = authority['id']
    # Scrum-master gate: a sprint may only involve teams that already have a
    # designated scrum master (the standing facilitator for their ceremonies).
    # `teamIds` names the teams this sprint touches; any one lacking a scrum
    # master blocks creation -- report exactly which teams to fix.
    team_ids = (body.get('teamIds') or [])
    if not isinstance(team_ids, list):
        team_ids = []
    team_ids = [str(x).strip() for x in team_ids if str(x).strip()]
    if team_ids:
        missing = _teams_missing_scrum_master(state, team_ids)
        if missing:
            names = ', '.join(f"{m['name']} ({m['id']})" for m in missing)
            return JSONResponse({'error': f'Every team in a sprint needs a scrum master or explicit director facilitation. Designate one (POST /api/teams/{missing[0]["id"]}/scrum-master) or set directorFacilitation: missing for {names}.', 'missingTeams': missing}, status_code=409)
    # targetDate -> epoch ms (ISO 8601 or numeric; malformed degrades to None).
    target_date = body.get('targetDate')
    if target_date:
        try:
            if isinstance(target_date, (int, float)):
                target_date = int(target_date)
            else:
                dt = datetime.datetime.fromisoformat(str(target_date).replace('Z', '+00:00'))
                target_date = int(dt.timestamp() * 1000)
        except Exception:
            target_date = None
    else:
        target_date = None
    import sim as _sim
    # workerCount: the director's chosen sprint headcount (1..MAX_TEAM_MEMBERS),
    # strictly staffed -- only this many of the sprint's team members may take
    # its cards (see _sprint_worker_pool in sim.py). Optional: absent/legacy
    # defaults to the team's full complement. Must be a whole number in range;
    # anything else is a 400. (queue_sprint clamps as defense in depth; the
    # strict check here gives the player a clean error.)
    worker_count = body.get('workerCount')
    if worker_count is not None:
        _wc_cap = (max([_sim.team_policy(state, t, 'maxTeamMembers')
                        for t in team_ids], default=0)
                   or _sim.team_policy(state, owner_id, 'maxTeamMembers'))
        try:
            worker_count = int(worker_count)
        except (TypeError, ValueError):
            return JSONResponse({'error': f'workerCount must be a whole number from 1 to {_wc_cap}'}, status_code=400)
        if worker_count < 1 or worker_count > _wc_cap:
            return JSONResponse({'error': f'workerCount must be a whole number from 1 to {_wc_cap}'}, status_code=400)
    from sim import next_sprint_id
    sprint_id = next_sprint_id(state)
    sprint = _sim.queue_sprint(
        state, sprint_id, (body.get('name') or '').strip(), goal, owner_id,
        items, _DELEGATABLE_ROOMS, target_date_ms=target_date, team_ids=team_ids,
        worker_count=worker_count)
    if sprint is None:
        return JSONResponse({'error': 'No sprint item had a valid title and a delegatable room, so nothing was queued.'}, status_code=400)
    save_state_to_db(state)
    log_action(player_id, 'sprint_created', {
        'id': sprint_id, 'goal': goal[:200], 'itemCount': len(items)}, authorized=True)
    _append_passport_decision('sprint_created', owner_id, {
        'id': sprint_id, 'goal': goal[:200], 'itemCount': len(items),
        'targetDate': target_date, 'name': sprint['name']})
    queue_conf = []
    for it in items:
        if it.get('room') in _DELEGATABLE_ROOMS and it.get('title'):
            queue_conf.append({'title': it['title'], 'room': it['room']})
    return JSONResponse({'ok': True, 'sprint': sprint, 'owner': owner_id,
                         'queuedItems': queue_conf})


@app.get('/api/intent/sprints')
async def get_sprints(request: Request):
    """Read-only sprint listing (board modal). Returns {sprints: {id: {record, progress}}}."""
    state = get_state_from_db()
    if not state:
        return JSONResponse({'sprints': {}})
    import sim as _sim
    sprints = state.get('sprints') or {}
    out = {}
    for sid, record in sprints.items():
        progress = _sim.sprint_progress(state, sid)
        out[sid] = {'record': record, 'progress': progress}
    return JSONResponse({'sprints': out})


@app.post('/api/intent/sprint/{sprint_id}/close')
@_state_writer
async def close_sprint(sprint_id: str, request: Request):
    """Close a sprint container. Queued items keep flowing; close is a status,
    not a cancel. Chains the close into the product-passport."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    import sim as _sim
    record = _sim.close_sprint(state, sprint_id)
    if record is None:
        return JSONResponse({'error': f'unknown sprint: {sprint_id}'}, status_code=404)
    save_state_to_db(state)
    log_action('player', 'sprint_closed', {'id': sprint_id}, authorized=True)
    _append_passport_decision('sprint_closed', (record.get('ownerId') or 'player'),
                              {'id': sprint_id, 'goal': (record.get('goal') or '')[:200]})
    return JSONResponse({'ok': True, 'sprint': record})


# ---------------------------------------------------------------------------
# JIRA-like issue register (TEAM-0128). A first-class, durable, per-team-issued
# ticket store fed by POST /api/intent/issues. Required fields: teamId, type
# (story|spike|bug|task|meeting), summary, reporterId. Listing + status transitions also
# live here. Filing dually writes into the backlogRequests pipe (tagged with
# issueKey + teamId), so the owning team's scrum master grooms the card into a
# sprint and the think tank actually works it -- not a dead ledger.
# ---------------------------------------------------------------------------
_ISSUE_REQUIRED = ('teamId', 'type', 'summary', 'feature')


@app.get('/api/intent/issues')
async def get_issues(request: Request):
    """List issues, optionally filtered by ?teamId=. Newest first."""
    import sim as _sim
    team_id = (request.query_params.get('teamId') or '').strip() or None
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    return JSONResponse({'ok': True, 'issues': _sim.list_issues(state, team_id)})
