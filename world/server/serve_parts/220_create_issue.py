

@app.post('/api/intent/issues')
@_state_writer
async def create_issue(request: Request):
    """File a JIRA-style issue for a team. Required: teamId, type, summary,
    feature. reporterId is OPTIONAL (this endpoint is player-only, so it
    defaults to 'player' -- an agent never files issues as itself here).
    Returns the issue record (key TEAM-0128) + the linked
    backlog-request id. Issue type/storyPoints pass through; `description` is
    optional but structured when present -- pass
    {'userStory': ..., 'acceptanceCriteria': ..., 'never': [...]} or a string
    following one of those templates (a trailing "NEVER:" block is parsed as
    constraints). `never` hard prohibitions survive refinement -> queue -> task
    into the worker's instructions. MEETING cards: `attendees` is a list of
    roster agent ids or a scope string ('team'/'all'/'pair', resolved to
    concrete ids at grooming), and `facilitatorId` is the roster agent who
    convenes the meeting and writes the minutes (defaults to the filer, then
    the team's scrum master). Both survive refinement -> queue -> task so the
    meeting executor can name the participants."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    missing = [f for f in _ISSUE_REQUIRED if not (body.get(f) or '').strip()]
    if missing:
        return JSONResponse({'error': f"missing required field(s): {', '.join(missing)}"}, status_code=400)
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    team_id = (body.get('teamId') or '').strip()
    if not _sim._team_row(state, team_id):
        return JSONResponse({'error': f'unknown team: {team_id}'}, status_code=404)
    issue_type = (body.get('type') or '').strip().lower()
    if issue_type not in _sim.ISSUE_TYPES:
        return JSONResponse({'error': f"type must be one of: {', '.join(_sim.ISSUE_TYPES)}"}, status_code=400)
    issue = _sim.file_issue(
        state, team_id, issue_type, (body.get('summary') or '').strip(),
        (body.get('feature') or '').strip(),
        (body.get('reporterId') or 'player').strip(),
        description=(body.get('description') or ''),
        title=(body.get('title') or ''),
        story_points=body.get('storyPoints'),
        assigned_to=body.get('assignedTo'),
        attendees=body.get('attendees'),
        facilitator_id=body.get('facilitatorId'),
        approved_tools=body.get('approvedTools'),
        deliverable=body.get('deliverable'))
    if issue is None:
        return JSONResponse({'error': 'issue could not be filed'}, status_code=400)
    save_state_to_db(state)
    log_action('player', 'issue_created', {'key': issue['key'], 'type': issue_type,
                                           'teamId': team_id, 'summary': issue['summary'][:200]},
               authorized=True)
    _append_passport_decision('issue_created', (body.get('reporterId') or 'player').strip(),
                              {'key': issue['key'], 'type': issue_type, 'teamId': team_id,
                               'summary': issue['summary'][:200]})
    return JSONResponse({'ok': True, 'issue': issue})


@app.post('/api/intent/issues/{key}/status')
@_state_writer
async def set_issue_status(key: str, request: Request):
    """Transition an issue's status (open/in_progress/done/closed). Mirrors the
    change onto any linked pending backlog request."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    issue = _sim.set_issue_status(state, key, (body.get('status') or '').strip())
    if issue is None:
        return JSONResponse({'error': f"unknown issue or invalid status: {key}"}, status_code=404)
    save_state_to_db(state)
    log_action('player', 'issue_status', {'key': key, 'status': issue['status']}, authorized=True)
    return JSONResponse({'ok': True, 'issue': issue})


@app.get('/api/intent/issues/{key}')
async def get_issue_detail(key: str):
    """One issue's full detail (the player's review surface): story, acceptance
    criteria, status, the SM-committed `blocked` field + its provenance, and any
    pending/answered player-inbox questions tied to it."""
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    issue = (state.get('issues') or {}).get(key)
    if not issue:
        return JSONResponse({'error': f'unknown issue: {key}'}, status_code=404)
    detail = dict(issue)
    detail['questions'] = [m for m in (state.get('playerInbox') or [])
                           if m.get('issueKey') == key]
    detail['blockLog'] = [c for c in (state.get('_pendingBlockChanges') or [])
                          if c.get('issueKey') == key and c.get('state') == 'committed'][-8:]
    return JSONResponse({'ok': True, 'issue': detail})


@app.post('/api/intent/issues/{key}/claim-met')
@_state_writer
async def claim_issue_met(key: str, request: Request):
    """An agent (or the player) files that issue `key` is requirements-met. The
    owning director/supervisor must approve before the scrum master commits the
    blocked field. Pure: returns the claim status; the supervisor vote runs on a
    server cadence. Body: {'agentId': ..., 'context': ...}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if (state.get('issues') or {}).get(key) is None:
        return JSONResponse({'error': f'unknown issue: {key}'}, status_code=404)
    agent_id = (body.get('agentId') or 'player').strip()
    if not _sim._issue_director(state, key):
        return JSONResponse({'error': 'issue has no owning director to approve'}, status_code=400)
    ok = _sim.request_block_claim_met(state, key, agent_id,
                                      context=(body.get('context') or ''))
    save_state_to_db(state)
    if not ok:
        return JSONResponse({'error': 'a claim is already pending on this issue'}, status_code=409)
    log_action(agent_id, 'issue_claim_met', {'key': key}, authorized=True)
    return JSONResponse({'ok': True, 'key': key, 'state': 'pending_supervisor'})


@app.post('/api/intent/issues/{key}/block-dependency')
@_state_writer
async def block_issue_dependency(key: str, request: Request):
    """Agent A files that issue `key` is blocked on another agent's task
    (`dependsOnTask`). No Jev gate on this path (naming the dependency is enough);
    the SM commits the blocked field. Body: {'agentId','dependsOnTask','reason'}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if (state.get('issues') or {}).get(key) is None:
        return JSONResponse({'error': f'unknown issue: {key}'}, status_code=404)
    agent_id = (body.get('agentId') or '').strip()
    dep = (body.get('dependsOnTask') or '').strip()
    if not agent_id or not dep:
        return JSONResponse({'error': 'agentId and dependsOnTask are required'}, status_code=400)
    rid = _sim.request_block_dependency(state, key, agent_id, dep,
                                        reason=(body.get('reason') or ''))
    save_state_to_db(state)
    if not rid:
        return JSONResponse({'error': 'a block-change is already pending on this issue'}, status_code=409)
    log_action(agent_id, 'issue_block_dependency', {'key': key, 'dependsOnTask': dep},
               authorized=True)
    return JSONResponse({'ok': True, 'key': key, 'requestId': rid, 'state': 'pending_sm'})


@app.get('/api/player-inbox')
async def get_player_inbox(request: Request):
    """The player's inbox: every question an agent asked you (director-approved),
    most recent first, with status (awaiting_input / answered / superseded)."""
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    msgs = sorted((state.get('playerInbox') or []),
                  key=lambda m: m.get('createdAt', 0), reverse=True)
    return JSONResponse({'ok': True, 'messages': msgs})


@app.post('/api/player-inbox/{message_id}/respond')
@_state_writer
async def respond_player_inbox(message_id: str, request: Request):
    """The player answers an awaiting inbox question. The reply is stamped onto
    the agent's task as context and the SM clears the blocked field. Body: {'answer'}.
    PLAYER-only -- a valid agent key is NOT a credential here, because answering
    one of your own questions would unblock yourself around the human-in-the-loop
    gate this inbox exists to enforce."""
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    answer = (body.get('answer') or '').strip()
    if not answer:
        return JSONResponse({'error': 'answer is required'}, status_code=400)
    m = _sim.resolve_player_ask(state, message_id, answer)
    save_state_to_db(state)
    if not m:
        return JSONResponse({'error': 'unknown or already-answered message'}, status_code=404)
    log_action('player', 'player_inbox_respond', {'messageId': message_id}, authorized=True)
    return JSONResponse({'ok': True, 'message': m})


@app.post('/api/player-inbox/{message_id}/feedback')
@_state_writer
async def feedback_player_inbox(message_id: str, request: Request):
    """The player's feedback on a COMPLETED work item the think tank shared
    with them -- historically a `card_report` inbox message (the sim no
    longer creates those; closed cards are silent and the sprint retro is
    the close signal). Body: {rating: 1-5?, comment: '...'?}, at
    least one required. The feedback is stamped onto the message (so the player
    sees it in the inbox) and pushed into the completing agent's feedback
    buffer (_append_feedback), so the agent carries it into its next task.
    Re-submitting overwrites the previous feedback on that work item. PLAYER-
    only, and only for completed work -- feedback on a chat reply or a pending
    question is rejected, by design."""
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    messages = state.get('playerInbox') or []
    m = next((x for x in messages if x.get('id') == message_id), None)
    if not m:
        return JSONResponse({'error': 'unknown message'}, status_code=404)
    if m.get('kind') != 'card_report':
        return JSONResponse({'error': 'feedback is only for completed work shared with you, '
                                      'not for chat replies or pending questions'}, status_code=400)
    rating = body.get('rating')
    if rating is not None:
        if isinstance(rating, bool) or not isinstance(rating, (int, float, str)):
            return JSONResponse({'error': 'rating must be an integer 1-5'}, status_code=400)
        try:
            rating_f = float(rating)
        except (TypeError, ValueError):
            return JSONResponse({'error': 'rating must be an integer 1-5'}, status_code=400)
        if rating_f != int(rating_f):
            return JSONResponse({'error': 'rating must be an integer 1-5'}, status_code=400)
        rating = int(rating_f)
        if rating < 1 or rating > 5:
            return JSONResponse({'error': 'rating must be an integer 1-5'}, status_code=400)
    comment = (body.get('comment') or '').strip()
    if rating is None and not comment:
        return JSONResponse({'error': 'rating and/or comment is required'}, status_code=400)
    now_ms = int(time.time() * 1000)
    agent_id = m.get('agentId')
    title = m.get('title') or 'a completed card'
    feedback = {'rating': rating, 'comment': comment, 'at': now_ms,
                'agentId': agent_id, 'taskId': m.get('taskId')}
    m['feedback'] = feedback
    text = f'Player feedback on "{title}": '
    if rating is not None:
        text += f'{rating}/5'
    if comment:
        text += (f' -- {comment}' if rating is not None else comment)
    if agent_id:
        _sim._append_feedback(state, agent_id, text, source='player_feedback')
    save_state_to_db(state)
    log_action('player', 'player_work_feedback',
               {'messageId': message_id, 'taskId': m.get('taskId'),
                'rating': rating, 'agentId': agent_id}, authorized=True)
    return JSONResponse({'ok': True, 'feedback': feedback})


# Effective grants = state grants (added by the player through the /api
# endpoints, persisted in think_tank.db) UNION operator-edited grants from
# world/grants.json (config.get_grants_config, re-read live on mtime change).
# Both are player-authored sources; an operator edit takes effect without
# restarting the server. fs.py's grant checks call these, so every scoped
# tool honors the JSON grants too.
def _merge_grants(primary, extra):
    seen = set()
    out = []
    for g in list(primary) + list(extra):
        gid = g.get('id')
        if gid:
            if gid in seen:
                continue
            seen.add(gid)
        out.append(g)
    return out


def _effective_file_grants(state):
    return _merge_grants(state.get('fileGrants') or [], get_grants_config()['fileGrants'])


def _effective_app_grants(state):
    return _merge_grants(state.get('appGrants') or [], get_grants_config()['appGrants'])


def _grant_actor(request):
    """Player session or a scrum master/director/admin agent key."""
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return 'player'
    presented = request.headers.get('X-Agent-Key')
    if not isinstance(presented, str) or not presented:
        return None
    requester = _resolve_requester(request)
    if requester and verify_agent_key(requester, presented) is True:
        state = get_state_from_db() or {}
        if governance.may_grant_entitlements(state, requester):
            return requester
    return None


@app.get('/api/file-grants')
async def list_file_grants(request: Request):
    """The player's current file-access grants (scope + capability set),
    including operator-edited grants from world/grants.json. PLAYER-only.
    Read-only."""
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    grants = _effective_file_grants(state)
    return JSONResponse({'ok': True, 'grants': [
        {'id': g.get('id'), 'scope': g.get('scope'),
         'label': g.get('label'), 'caps': g.get('caps') or {}} for g in grants]})


@app.post('/api/file-grants')
@_state_writer
async def add_file_grant(request: Request):
    """Grant the think tank scoped access to a folder. The body carries the
    absolute `scope`, an optional `label`, and the `caps` map.
    `emptyTrash` is never accepted -- the trash rule is absolute. The scope must
    be an existing absolute directory. PLAYER-only."""
    actor = _grant_actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- player, scrum master, or director required'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    scope = (body.get('scope') or '').strip()
    label = (body.get('label') or '').strip() or scope
    caps = body.get('caps') or {}
    if not scope or not os.path.isabs(scope):
        return JSONResponse({'error': 'scope must be an absolute path'}, status_code=400)
    if not os.path.isdir(scope):
        return JSONResponse({'error': 'scope must be an existing directory'}, status_code=400)
    if caps.get('emptyTrash'):
        return JSONResponse({'error': 'emptyTrash is never granted'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    root = os.path.realpath(scope)
    for g in state.get('fileGrants') or []:
        if os.path.realpath(g.get('scope') or '') == root:
            return JSONResponse({'error': 'a grant for this scope already exists'}, status_code=409)
    grant = {
        'id': f'g-{len(state.get("fileGrants") or []) + 1}',
        'scope': root,
        'label': label,
        'caps': {'read': bool(caps.get('read', True)),
                 'write': bool(caps.get('write', True)),
                 'delete': bool(caps.get('delete', True)),
                 'emptyTrash': False},
    }
    state.setdefault('fileGrants', []).append(grant)
    save_state_to_db(state)
    log_action(actor, 'file_grant_added',
               {'grantId': grant['id'], 'scope': root, 'caps': grant['caps']}, authorized=True)
    return JSONResponse({'ok': True, 'grant': grant})


@app.delete('/api/file-grants/{grant_id}')
@_state_writer
async def revoke_file_grant(grant_id: str, request: Request):
    """Revoke a file-access grant."""
    actor = _grant_actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- player, scrum master, or director required'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    grants = state.get('fileGrants') or []
    before = len(grants)
    state['fileGrants'] = [g for g in grants if g.get('id') != grant_id]
    if len(state['fileGrants']) == before:
        return JSONResponse({'error': 'unknown grant'}, status_code=404)
    save_state_to_db(state)
    log_action(actor, 'file_grant_revoked', {'grantId': grant_id}, authorized=True)
    return JSONResponse({'ok': True})


@app.get('/api/app-grants')
async def list_app_grants(request: Request):
    """The player's current app-control grants (bundleId + capability set),
    including operator-edited grants from world/grants.json. PLAYER-only.
    Read-only."""
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    grants = [{'id': g.get('id'), 'bundleId': g.get('bundleId'),
               'label': g.get('label'), 'caps': g.get('caps') or {}}
              for g in _effective_app_grants(state)]
    return JSONResponse({'ok': True, 'grants': grants})


@app.post('/api/app-grants')
@_state_writer
async def add_app_grant(request: Request):
    """Grant the think tank AppleScript control of ONE macOS app."""
    actor = _grant_actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- player, scrum master, or director required'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    bundle_id = (body.get('bundleId') or '').strip()
    label = (body.get('label') or '').strip() or bundle_id
    caps = body.get('caps') or {}
    if not _VALID_BUNDLE_ID_RE.match(bundle_id):
        return JSONResponse({'error': 'bundleId must be a dotted macOS bundle id (e.g. com.apple.Notes)'},
                            status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    for g in _effective_app_grants(state):
        if (g.get('bundleId') or '').strip() == bundle_id:
            return JSONResponse({'error': 'a grant for this app already exists'}, status_code=409)
    grant = {
        'id': f'a-{len(state.get("appGrants") or []) + 1}',
        'bundleId': bundle_id,
        'label': label,
        'caps': {'use': bool(caps.get('use', True))},
    }
    state.setdefault('appGrants', []).append(grant)
    save_state_to_db(state)
    log_action(actor, 'app_grant_added',
               {'grantId': grant['id'], 'bundleId': bundle_id, 'caps': grant['caps']}, authorized=True)
    return JSONResponse({'ok': True, 'grant': grant})


@app.delete('/api/app-grants/{grant_id}')
@_state_writer
async def revoke_app_grant(grant_id: str, request: Request):
    """Revoke an app-control grant."""
    actor = _grant_actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- player, scrum master, or director required'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    grants = state.get('appGrants') or []
    before = len(grants)
    state['appGrants'] = [g for g in grants if g.get('id') != grant_id]
    if len(state['appGrants']) == before:
        return JSONResponse({'error': 'unknown grant'}, status_code=404)
    save_state_to_db(state)
    log_action(actor, 'app_grant_revoked', {'grantId': grant_id}, authorized=True)
    return JSONResponse({'ok': True})


@app.post('/api/intent/player-task/{task_id}/complete')
@_state_writer
async def complete_player_task_endpoint(task_id: str, request: Request):
    """Human-in-the-loop: the player marks an assigned-to-them task done. The
    task was handed to the player by the think tank (assignedTo 'player', a
    'needs_player' card -- see _assign_player_task); marking it done flips it
    to 'done', which releases anything queued depends_on_task on it: agent
    cards become assignable on the next task cycle, and any issue committed
    blocked on it is auto-unblocked with its waiting agent woken. PLAYER-only
    -- an agent key is NOT a credential here (an agent answering for you would
    defeat the gate)."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    import sim as _sim
    task = _sim.complete_player_task(state, task_id)
    save_state_to_db(state)
    if not task:
        return JSONResponse({'error': 'unknown, not player-owned, or already-completed task'}, status_code=404)
    return JSONResponse({'ok': True, 'task': {'id': task['id'], 'title': task['title'],
                                              'status': task['status']}})


@app.get('/api/intent/charter')
async def get_charter_endpoint(request: Request):
    """Read the player's charter -- the tank-level spine (goal + interests) the
    roadmap recompute and refinement weigh, and every task's instructions open
    with. PLAYER-only: the charter is player-owned by design (you decide, the
    tank plans)."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    import sim as _sim
    return JSONResponse({'ok': True, 'charter': _sim.get_charter(state)})


@app.post('/api/intent/charter')
@_state_writer
async def set_charter_endpoint(request: Request):
    """Set the player's charter (the spine). Body: {'goal': str (required),
    'interests': [str], 'notes': str}. PLAYER-only. A missing/blank goal is a
    400 -- the tank never invents a direction for you. Stamps the governance
    log + passport so the setting is attributable."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    import sim as _sim
    goal = (body.get('goal') or '').strip()
    if not goal:
        return JSONResponse({'error': '`goal` is required (a vague goal gets a vague plan)'}, status_code=400)
    interests = body.get('interests') or []
    if not isinstance(interests, list):
        return JSONResponse({'error': '`interests` must be a list of strings'}, status_code=400)
    notes = (body.get('notes') or '').strip()
    charter = _sim.set_charter(state, goal, interests=interests, notes=notes)
    save_state_to_db(state)
    log_action('player', 'charter_set', {'goal': goal, 'interests': interests}, authorized=True)
    _append_passport_decision('charter_set', 'player', {'goal': goal[:120]})
    return JSONResponse({'ok': True, 'charter': charter})


@app.post('/api/player-email/credential')
async def provision_player_email_endpoint(request: Request):
    """Provision the Gmail app-password used for action-needed notification
    emails. PLAYER-only (an agent key is rejected). Validates a Gmail app-
    password (16 chars, 4 groups of 4), stores it sealed in the vault, and fires
    a self-test send so provisioning is verified, not assumed. The password is
    never returned or logged."""
    if _resolve_requester(request):
        return JSONResponse({'error': 'email provisioning is player-only'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    res = provision_player_email(body.get('appPassword'))
    if not res.get('ok'):
        return JSONResponse({'error': res.get('error')}, status_code=400)
    log_action('player', 'player_email_provisioned', {'testSent': res.get('test_ok')},
               authorized=True)
    _append_passport_decision('player_email_provisioned', 'player',
                              {'testSent': res.get('test_ok')})
    return JSONResponse({'ok': True, 'testSent': res.get('test_ok')})


@app.post('/api/player-email/test')
async def test_player_email(request: Request):
    """Send a self-test notification email using the currently-provisioned
    credential. PLAYER-only."""
    if _resolve_requester(request):
        return JSONResponse({'error': 'email test is player-only'}, status_code=403)
    ok = _send_player_email_sync(
        '[AI Think Tank] Email still working',
        'This is a self-test from your AI Think Tank. Notifications are delivered to this address.')
    log_action('player', 'player_email_test', {'sent': bool(ok)}, authorized=True)
    return JSONResponse({'ok': True, 'sent': bool(ok)})


@app.get('/api/device/key')
async def device_key_get(request: Request):
    """Player-only retrieval of the phone check-in bearer. The key is
    auto-generated on first boot; this makes it visible without digging
    through startup logs."""
    authed, actor = _operator_gate(request)
    if not authed or actor != 'player':
        return JSONResponse({'error': 'device key access is player-only'},
                            status_code=403)
    return JSONResponse({'key': DEVICE_API_KEY})


@app.post('/api/device/key')
async def device_key_rotate(request: Request):
    """Rotate the phone check-in bearer and reload it live."""
    authed, actor = _operator_gate(request)
    if not authed or actor != 'player':
        return JSONResponse({'error': 'device key rotation is player-only'},
                            status_code=403)
    key = secrets.token_urlsafe(24)
    try:
        _set_env_keys({'DEVICE_API_KEY': key})
    except Exception as e:
        return JSONResponse({'error': f'could not write .env: {e}'}, status_code=500)
    _reload_env_config()
    log_action(None, 'device_key_rotated', {}, authorized=True)
    return JSONResponse({'ok': True, 'key': key,
                         'note': 'shown once; update the Shortcut now'})


@app.post('/api/device/checkin')
@_state_writer
async def device_checkin(request: Request):
    """A phone (an iOS Shortcut, to start) reports its own current location/
    battery/Focus/Wi-Fi. Self-guarded like /api/chat -- listed in
    AUTH_PUBLIC_PATHS because the deny-by-default middleware only recognizes
    a player session or an agent key, neither of which a Shortcut can hold,
    so it checks its own bearer credential instead. Body is deliberately
    open-ended -- every field
    optional, no fixed schema -- so a future field or device never needs this
    endpoint redesigned, just a new key in the same dict. No LLM/Jev call
    involved (pure data storage), so unlike task-driving endpoints this is
    NOT gated by _dormant() -- a check-in should land even while the think tank
    is asleep."""
    presented = request.headers.get('X-Device-Key')
    if not presented or not secrets.compare_digest(presented, DEVICE_API_KEY):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not body:
        return JSONResponse({'error': 'empty body'}, status_code=400)
    loc = body.get('location')
    if loc is not None and not (isinstance(loc, dict) and isinstance(loc.get('lat'), (int, float))
                                 and isinstance(loc.get('lon'), (int, float))):
        return JSONResponse({'error': 'location, if present, must be {lat, lon}'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    import sim as _sim
    entry = _sim.record_device_checkin(state, {
        'location': loc, 'battery': body.get('battery'),
        'focus': body.get('focus'), 'wifi': body.get('wifi'),
        'trigger': body.get('trigger'),
    })
    save_state_to_db(state)
    log_action('player', 'device_checkin', {'trigger': body.get('trigger'),
              'hasLocation': loc is not None}, authorized=True)
    return JSONResponse({'ok': True, 'storedAt': entry['receivedAt']})


@app.post('/api/webhook/{channel}')
async def webhook_channel(channel: str, request: Request):
    """Accept an inbound event for any external watcher and store it for
    processing. Self-guarded like /api/device-checkin -- listed in
    AUTH_PUBLIC_PATHS because the deny-by-default middleware only
    recognizes a player session or an agent key, neither of which a
    Google Apps Script watcher can hold, so it checks its own bearer
    credential instead. One shared WEBHOOK_API_KEY covers every channel;
    the channel in the path is the scoping, not the credential. No LLM/Jev
    call involved (pure data storage), so unlike task-driving endpoints
    this is NOT gated by _dormant() -- a loom request should land even
    while the think tank is asleep."""
    if channel not in WEBHOOK_CHANNELS:
        return JSONResponse({'error': 'unknown channel'}, status_code=404)
    presented = request.headers.get('X-Webhook-Key')
    if not presented or not secrets.compare_digest(presented, WEBHOOK_API_KEY):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not body:
        return JSONResponse({'error': 'empty body'}, status_code=400)
    entry = {
        'channel': channel,
        'receivedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'payload': body,
    }
    with open(WEBHOOKS_PATH, 'a') as f:
        f.write(json.dumps(entry) + '\n')
    log_action('external', 'webhook_channel', {'channel': channel,
                'id': body.get('id')}, authorized=True)
    return JSONResponse({'ok': True})




@app.post('/api/teams/{team_id}/prefix')
@_state_writer
async def set_team_prefix(team_id: str, request: Request):
    """Set (or clear via empty string) an explicit issue prefix for a team.
    Operator-gated (like promote/scrum-master): a player session or a
    director/admin agent. Returns the applied prefix."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _team_record(state, team_id):
        return JSONResponse({'error': f'unknown team: {team_id}'}, status_code=404)
    prefix = (body.get('prefix') or '')
    import sim as _sim
    applied = _sim.set_team_prefix(state, team_id, prefix)
    if applied is None:
        return JSONResponse({'error': 'prefix must be 1-5 alphanumeric characters'}, status_code=400)
    save_state_to_db(state)
    log_action(actor, 'team_prefix', {'teamId': team_id, 'prefix': applied}, authorized=True)
    return JSONResponse({'ok': True, 'teamId': team_id, 'prefix': applied})


# ---------------------------------------------------------------------------
# Clarify (player ask -> on-call agent -> KB-first -> completing agent). The
# player asks how completed work was done; unlike a sprint (which POSTS new
# work), this ANSWERS a question about work already landed. Routing is derived,
# not stored: product -> owning team's current ON-CALL agent (whether they
# worked on it or not), who answers KNOWLEDGE-BASE-FIRST via the same library
# search the agents use, and only falls back to the completing agent if they
# genuinely can't answer. Fail-closed on auth like every intent handler.
# ---------------------------------------------------------------------------
_CLARIFY_ESCALATE_TOKEN = '__CLARIFY_ESCALATE__'


def _agent_record_for(state, agent_id):
    for d in (state.get('agentRoster') or []):
        if d.get('id') == agent_id:
            return d
    return (state.get('agents') or {}).get(agent_id) or {}


def _sim_village_for_agent(agent_id):
    """Resolve an agent's village from the live DB state, for spend
    attribution. Best-effort: an unknown agent or a state read failure falls
    back to the default village so a ledger hiccup never throws in the money
    path."""
    import sim as _sim
    try:
        state = get_state_from_db()
        if state:
            return _sim.village_of_agent(state, agent_id)
    except Exception:
        return _sim.DEFAULT_VILLAGE
    return _sim.DEFAULT_VILLAGE


def _clarify_in_character_messages(agent, kb_matches, product_name, question):
    """The in-character prompt for the on-call answering a player's clarify, in
    the same voice as index.html's requestAgentReply. KB matches are injected as
    the knowledge the on-call answers from; the ESCALATE token is the only way
    to hand off to the completing agent -- no answering past 'I don't know'."""
    name = agent.get('name') or agent.get('id') or 'think tank member'
    role = agent.get('role') or 'worker'
    mission = ((agent.get('profile') or {}).get('mission')) or ''
    if kb_matches:
        kb_block = '\n'.join(f"- {m['path']}: {m['snippet']}" for m in kb_matches[:6])
    else:
        kb_block = '(no relevant Library files found for this product)'
    # The knowledge snippets are model/player-authored content from the Library
    # tree -- DATA, not instructions. Tag them with the same EXTERNAL_DATA
    # boundary used for browse results so a hostile instruction planted in a
    # library file is read as data, not obeyed, while still answering
    # knowledge-first.
    kb_wrapped, _n, _t, kb_inst = wrap_external_content(
        kb_block, 'your Library knowledge base')
    system = (
        f"You are {name}, working as {role} in a small think tank. "
        f"Your mission: {mission} "
        f"You are the current ON-CALL agent. The player asks a factual question about "
        f"how the product '{product_name}' was built. ANSWER KNOWLEDGE-BASE-FIRST: reason "
        f"only from the Library knowledge below. Keep the answer short (2-4 sentences), "
        f"casual, in character, and specific. Never invent details not in the knowledge. "
        f"If you genuinely cannot answer from the knowledge, reply with EXACTLY the single "
        f"line {_CLARIFY_ESCALATE_TOKEN} and nothing else -- do not fabricate."
    )
    user = (
        f"## Library knowledge (searched for this product + your question)\n{kb_inst}\n\n{kb_wrapped}\n\n"
        f"## Player's question about '{product_name}'\n{question}"
    )
    # Prose answer to the player -- inherit the plain-writing directive so the
    # reply stays short and filler-free (fewer tokens, same information).
    return _apply_plain_writing([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])


@app.post('/api/intent/clarify')
async def intent_clarify(request: Request):
    """Player asks the think tank to clarify how completed work was done.
    Body: {productId, question, sprintId?}. Resolves the owning team's on-call
    agent, answers KNOWLEDGE-BASE-FIRST (real Library search), and only
    escalates to the completing agent if the on-call can't answer. Returns
    {reply, onCall, completing, escalatedTo}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    if not check_rate_limit(ASK_LANE_RATE_LIMIT_KEY):
        return JSONResponse({'error': 'Rate limit hit -- too many questions at once. Wait a minute and try again.'}, status_code=429)
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    product_id = (body.get('productId') or '').strip()
    question = (body.get('question') or '').strip()
    sprint_id = (body.get('sprintId') or '').strip() or None
    if not product_id or not question:
        return JSONResponse({'error': 'productId and question are required'}, status_code=400)

    import sim as _sim
    plan = _sim.clarify_router_plan(state, product_id, question, sprint_id)
    on_call = plan.get('onCall')
    if not on_call:
        return JSONResponse({'error': 'No on-call agent is free on that product\'s team to answer right now. Try again shortly.'}, status_code=404)

    agent = _agent_record_for(state, on_call)
    product_name = (plan.get('product') or {}).get('name') or product_id
    # KB-first: the same library search the agents use, on the product + question.
    kb_query = f'{product_name} {question}'[:200]
    kb_matches = _library_search_matches(kb_query, allowed=_requester_villages(on_call))

    completing = plan.get('completing')
    escalated_to = None
    # Grounding: with ZERO knowledge-base matches the on-call would be
    # answering from memory alone (a hallucination window). When a DIFFERENT
    # agent actually landed the work, skip the router and ask that agent
    # directly -- they are strictly more grounded. (Same-agent case: the
    # on-call IS the completing agent, so their knowledge is first-hand.)
    if not kb_matches and completing and completing != on_call:
        escalated_to = completing
        agent = _agent_record_for(state, completing)
        comp_name = (agent.get('name') or completing)
        messages = _clarify_in_character_messages(
            agent, kb_matches, product_name,
            f'{question}\n\n(The on-call agent had no knowledge-base material to '
            f'answer from; you landed the work so the player is asking you directly.)')
    else:
        messages = _clarify_in_character_messages(agent, kb_matches, product_name, question)

    # Clarify is an in-character answer about completed work -- judgment-heavy
    # but not code, so it goes through the JEV gate (default low, light mid).
    model = _resolve_model_tier(f'Answer an on-call agent clarifying a question about completed work: {question[:200]}')
    try:
        data = await asyncio.to_thread(_call_openrouter_sync, model, messages,
                                       int(body.get('max_tokens', 300)))
        # The clarify lane spends real model money (mid tier, one or two calls
        # per question) but was never accrued -- the cap and the Bank were
        # undercounting every clarify answer. Dedicated bucket so its cost is
        # visible separately from the ask lane.
        _accrue_spend('__clarify__', ((data or {}).get('usage') or {}).get('cost', 0.0),
                      village_id=_sim_village_for_agent(on_call))
        reply = (data['choices'][0]['message']['content'] or '').strip()
    except Exception as e:
        if escalated_to:
            comp_name = (agent.get('name') or completing)
            reply = (f"I couldn't reach {comp_name}, who landed this work. "
                     f"Ask again shortly or check with the admin.")
            return JSONResponse({
                'reply': reply, 'onCall': on_call,
                'completing': completing, 'escalatedTo': escalated_to,
                'onCallFallback': plan.get('onCallFallback', False)})
        return JSONResponse({'error': f'clarify failed: {e}'}, status_code=500)

    # Escalation: on-call explicitly can't answer -> hand off to the completing
    # agent (whether they worked on it or not is the on-call's routing; the
    # completing agent actually landed it). Never leak the token into the reply.
    if not escalated_to and _CLARIFY_ESCALATE_TOKEN in reply:
        if completing and completing != on_call:
            comp_agent = _agent_record_for(state, completing)
            comp_name = (comp_agent.get('name') or completing)
            comp_messages = _clarify_in_character_messages(
                comp_agent, kb_matches, product_name,
                f'{question}\n\n(The on-call agent could not answer this from knowledge; '
                f'you landed the work so the player is asking you directly.)')
            try:
                cdata = await asyncio.to_thread(_call_openrouter_sync, model, comp_messages,
                                                int(body.get('max_tokens', 300)))
                _accrue_spend('__clarify__', ((cdata or {}).get('usage') or {}).get('cost', 0.0))
                reply = (cdata['choices'][0]['message']['content'] or '').strip()
                escalated_to = completing
            except Exception:
                reply = (f"I couldn't reach {comp_name}, who landed this work. "
                         f"Ask again shortly or check with the admin.")
                escalated_to = completing
    reply = reply.replace(_CLARIFY_ESCALATE_TOKEN, '').strip()

    log_action('player', 'clarify', {
        'productId': product_id, 'question': question[:200],
        'onCall': on_call, 'escalatedTo': escalated_to}, authorized=True)
    _append_passport_decision('clarify', on_call, {
        'productId': product_id, 'question': question[:200],
        'escalatedTo': escalated_to})
    return JSONResponse({
        'reply': reply, 'onCall': on_call,
        'completing': plan.get('completing'), 'escalatedTo': escalated_to,
        'onCallFallback': plan.get('onCallFallback', False)})
