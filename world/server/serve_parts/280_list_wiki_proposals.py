

@app.get('/api/intent/wiki/proposals')
async def list_wiki_proposals(request: Request):
    """Read-only listing of pending wiki proposals (metadata only -- bodies stay
    in the quarantine file until approved). Open to any authenticated client."""
    base = os.path.join(LIBRARY_DIR, 'pending_review', 'wiki')
    proposals = []
    if os.path.isdir(base):
        for root, _dirs, filenames in os.walk(base):
            for fn in filenames:
                if not fn.endswith('.json'):
                    continue
                full = os.path.join(root, fn)
                try:
                    with open(full, 'r') as f:
                        p = json.load(f)
                except (OSError, ValueError):
                    continue
                proposals.append({'id': p.get('id'), 'title': p.get('title'),
                                  'category': p.get('category'),
                                  'summary': p.get('summary') or '',
                                  'proposedBy': p.get('proposedBy'),
                                  'proposedAt': p.get('proposedAt')})
    proposals.sort(key=lambda p: str(p.get('proposedAt')))
    return JSONResponse({'proposals': proposals})


def _find_wiki_proposal(category, page_id):
    rel = f'pending_review/wiki/{category}/{page_id}.json'
    target = _safe_library_path(rel)
    if not target or not os.path.isfile(target):
        return None, rel
    try:
        with open(target, 'r') as f:
            return json.load(f), rel
    except (OSError, ValueError):
        return None, rel


@app.post('/api/intent/wiki/proposal/{page_id}/approve')
@_state_writer
async def approve_wiki_proposal(page_id: str, request: Request):
    """Approve a pending wiki proposal: {category}. Director/admin gated (the
    same authority a direct wiki write needs). Promotes the proposal into the
    live wiki exactly like a direct write (version bump + history, body on disk,
    passport chain), then archives the proposal for provenance."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    category = (body.get('category') or '').strip()
    if not category:
        return JSONResponse({'error': 'category is required'}, status_code=400)
    if any(ch in page_id for ch in '/\\'):
        return JSONResponse({'error': 'invalid page id'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    actor = _resolve_requester(request)
    if not actor or not _is_director_or_admin(state, actor):
        return JSONResponse({'error': 'only a director or the chief of staff may approve a wiki proposal'}, status_code=403)
    proposal, rel_path = _find_wiki_proposal(category, page_id)
    if proposal is None:
        return JSONResponse({'error': f'no pending proposal for {page_id} in {category}'}, status_code=404)
    import sim as _sim
    mfields = _sim.resolve_memory_fields(state, proposal or {}, actor)
    village_id = _sim.village_of_agent(state, actor)
    record, is_new = _sim.wiki_write_page(state, page_id, proposal.get('title') or page_id,
                                          category, proposal.get('body') or '', actor,
                                          village_id=village_id, owner=mfields['owner'],
                                          clock=mfields['clock'], scope=mfields['scope'],
                                          source=mfields['source'],
                                          review_after_ms=mfields['review_after_ms'])
    if record is None:
        return JSONResponse({'error': 'could not write wiki page'}, status_code=400)
    _sim.record_memory_provenance(state, actor, page_id, proposal.get('title') or page_id,
                                  (proposal.get('body') or '')[:2000],
                                  source=mfields['source'], scope=mfields['scope'],
                                  clock=mfields['clock'],
                                  review_after_ms=mfields['review_after_ms'],
                                  village_id=village_id)
    target = _wiki_page_target(category, page_id)
    if not target:
        return JSONResponse({'error': 'invalid destination'}, status_code=400)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _write_file(target, proposal.get('body') or '')
    save_state_to_db(state)
    log_action(actor, 'wiki_page_written', {'id': page_id, 'category': category,
                                            'version': record.get('version'),
                                            'fromProposal': True}, authorized=True)
    _append_passport_decision('wiki_page_written', actor,
                              {'id': page_id, 'category': category,
                               'version': record.get('version'), 'isNew': is_new})
    src = _safe_library_path(rel_path)
    if src:
        archive_rel = f'archive/wiki-proposals/{category}/{page_id}.json'
        archive_target = _safe_library_path(archive_rel)
        if archive_target:
            os.makedirs(os.path.dirname(archive_target), exist_ok=True)
            shutil.move(src, archive_target)
    return JSONResponse({'ok': True, 'page': record, 'isNew': is_new,
                         'proposedBy': proposal.get('proposedBy')})


@app.post('/api/intent/wiki/proposal/{page_id}/reject')
async def reject_wiki_proposal(page_id: str, request: Request):
    """Reject a pending wiki proposal: {category}. Director/admin gated. Moves
    the proposal to rejected/wiki/ -- kept out of the way but not silently
    lost, exactly like library_reject."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    category = (body.get('category') or '').strip()
    if not category:
        return JSONResponse({'error': 'category is required'}, status_code=400)
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    actor = _resolve_requester(request)
    if not actor or not _is_director_or_admin(state, actor):
        return JSONResponse({'error': 'only a director or the chief of staff may reject a wiki proposal'}, status_code=403)
    proposal, rel_path = _find_wiki_proposal(category, page_id)
    if proposal is None:
        return JSONResponse({'error': f'no pending proposal for {page_id} in {category}'}, status_code=404)
    src = _safe_library_path(rel_path)
    dest_rel = f'rejected/wiki/{category}/{page_id}.json'
    dest = _safe_library_path(dest_rel)
    if not src or not dest:
        return JSONResponse({'error': 'invalid destination'}, status_code=400)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(src, dest)
    log_action(actor, 'wiki_proposal_rejected',
               {'id': page_id, 'category': category, 'to': dest_rel}, authorized=True)
    return JSONResponse({'ok': True, 'rejected': {'id': page_id, 'category': category,
                                                  'to': dest_rel}})


def _write_wiki_server(page_id, title, category, content):
    """Server-authority wiki write (hive-mind distillation path).

    The /api/intent/wiki/page endpoint is director/admin-gated -- which is right
    for a player-authored page -- but the distillation loop is the THINK TANK
    learning as a body, not an agent authoring. It runs as the server (actor
    'distill'), so it bypasses the director gate while still persisting the body
    to disk, bumping the version/history, logging to action_log, and chaining
    pinned into the passport exactly like any other wiki write (server-owned
    state: the think tank's synthesized knowledge must survive the client autosave,
    the same reason products/wiki-releases are server-owned).

    Returns the sim record dict (or None on failure) so the executor can report
    what it wrote."""
    import sim as _sim
    try:
        state = _state_begin()
        if not state:
            return None
        categories = state.setdefault('wiki', {}).setdefault('categories', {})
        if category not in categories:
            if category != 'think_tank':
                return None
            categories['think_tank'] = {'label': 'Think Tank', 'order': 0}
        record, _is_new = _sim.wiki_write_page(state, page_id, title, category,
                                               content, 'distill')
        _sim.record_memory_provenance(state, 'distill', page_id, title, content[:2000],
                                      source=None, scope='team', clock=_sim.SLOW_CLOCK,
                                      review_after_ms=None, village_id=_sim.DEFAULT_VILLAGE)
        if record is None:
            return None
        target = _wiki_page_target(category, page_id)
        if not target:
            return None
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _write_file(target, content)
        save_state_to_db(state)
        log_action('distill', 'wiki_page_written',
                   {'id': page_id, 'category': category, 'version': record.get('version')},
                   authorized=True)
        _append_passport_decision('wiki_page_written', 'distill',
                                  {'id': page_id, 'category': category,
                                   'version': record.get('version'), 'isNew': _is_new})
        return record
    finally:
        _state_abort()


@app.post('/api/intent/wiki/category')
@_state_writer
async def write_wiki_category(request: Request):
    """Create or set a wiki category's label + relative order: {id, label?,
    order?, room?}. Director/admin gated. Chains wiki_category_set."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    cat_id = (body.get('id') or '').strip()
    if not cat_id:
        return JSONResponse({'error': 'category id required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    actor = _resolve_requester(request)
    if not actor or not _is_director_or_admin(state, actor):
        return JSONResponse({'error': 'only a director or the chief of staff may set wiki categories'}, status_code=403)
    wiki = state.setdefault('wiki', {})
    cats = wiki.setdefault('categories', {})
    existing = cats.get(cat_id) or {}
    cats[cat_id] = {
        'label': body.get('label') or existing.get('label') or cat_id,
        'order': body.get('order') if isinstance(body.get('order'), (int, float)) else existing.get('order', 0),
    }
    # Optional per-category room affinity for read-before-act injection.
    if 'room' in body and (body.get('room') is None or isinstance(body.get('room'), str)):
        wiki.setdefault('categoryRooms', {})[cat_id] = body.get('room')
    save_state_to_db(state)
    log_action(actor, 'wiki_category_set', {'id': cat_id}, authorized=True)
    _append_passport_decision('wiki_category_set', actor, {'id': cat_id})
    return JSONResponse({'ok': True, 'categories': cats})


# ---------------------------------------------------------------------------
# Room definitions (player-facing + director-editable). The room key set
# (_DELEGATABLE_ROOMS) and the geometry are code-graph invariants -- that's
# what makes the map walkable. But the planner-facing description of what each
# room does is knowledge, not code, so it lives in DB state
# (state.roomDefinitions) and is editable by directors the same way role
# templates are. Reads are open (the player renders/looks at rooms); the
# purpose edit is gated to directors/admin. Labels are NOT editable -- they
# are stable names rendered on the map.
# ---------------------------------------------------------------------------
@app.get('/api/rooms')
async def list_rooms(request: Request):
    # Open read, like GET /api/templates: the player views rooms and the
    # planner reads purposes; no agent-key needed just to look.
    state = get_state_from_db() or {}
    return JSONResponse({'rooms': _room_definitions(state)})


# ---------------------------------------------------------------------------
# Teams. Open read (the player + any agent may VIEW a team and its members);
# writes (name/purpose edit, promote-to-director spawn) are gated to the team
# director or the admin. Members are always derived from the `director` graph,
# never read from a stored list.
# ---------------------------------------------------------------------------
@app.get('/api/teams')
async def list_teams(request: Request):
    state = get_state_from_db() or {}
    out = []
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    import sim as _sim
    for t in state.get('teams', []):
        tcopy = dict(t)
        tcopy['sharedDir'] = _team_shared_dir(t.get('id'))
        # Teamwork agreement: `agreement` is the team's stored overrides (or
        # None when the team set nothing), `agreementResolved` is the full
        # 57-key map the sim actually uses (agreement -> operator policy ->
        # code default, ceilings already clamped at read time).
        tcopy['agreement'] = t.get('agreement') or None
        tcopy['agreementResolved'] = _sim.team_policy(state, t.get('id'))
        # Members are always DERIVED live from the `director` graph, never read
        # from the stored snapshot -- so a promotion updates membership
        # immediately without a backfill pass.
        tcopy['members'] = _derive_team_members(state, t.get('id'))
        # Scrum master is exposed as both id and display name (for the board).
        scm = t.get('scrumMasterId')
        tcopy['scrumMaster'] = None if not scm else {
            'id': scm,
            'name': (roster.get(scm) or {}).get('name') or scm,
        }
        out.append(tcopy)
    return JSONResponse({'teams': out})


@app.get('/api/backlog')
async def list_backlog(request: Request):
    """WS-14 read view: the shared backlog board -- features, unassigned/picked
    items, and completed retrospectives -- so the player can see what a
    saturated think tank captured and which teams pulled what."""
    state = get_state_from_db() or {}
    backlog = []
    roster = {d.get('id'): d.get('name') for d in (state.get('agentRoster') or [])}
    for b in (state.get('backlog') or []):
        copy = dict(b)
        tid = copy.get('teamId')
        copy['teamName'] = roster.get(tid) if isinstance(tid, str) else None
        backlog.append(copy)
    return JSONResponse({
        'features': list((state.get('features') or {}).values()),
        'backlog': backlog,
        'retrospectives': list((state.get('retrospectives') or {}).values()),
    })


@app.put('/api/teams/{team_id}')
@_state_writer
async def update_team(team_id: str, request: Request):
    """Edit a team's name/purpose and/or its teamwork agreement.

    Credentials:
    - Agent path: X-Agent-Key identity + _can_write_team (unchanged).
    - Player path: a real player session may issue an edit that CONTAINS an
      `agreement` key, with no agent key; agreement authority is
      _can_set_team_agreement (player/admin/this-team's-director), and once
      the player is in, name/purpose in the same body are honored too --
      the player is the operator of the whole village.
    - A session without `agreement` in the body still 401s (no agent key),
      so this endpoint never becomes a general player write surface.

    Body agreement values: an object of TEAM_AGREEABLE_KEYS (merged over the
    stored overrides, fail-closed per key via config.validate_team_agreement)
    or null to clear the overrides entirely."""
    body = None
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        body = None
    is_agreement_edit = isinstance(body, dict) and 'agreement' in body
    requester = _resolve_requester(request)
    if not requester:
        if is_agreement_edit and _require_player_session(request):
            requester = 'player'
        else:
            return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    if requester != 'player' and verify_agent_key(requester, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    teams = state.get('teams', [])
    t = next((x for x in teams if x.get('id') == team_id), None)
    if not t:
        return JSONResponse({'error': 'unknown team'}, status_code=404)
    if is_agreement_edit and not _can_set_team_agreement(state, requester, team_id):
        return JSONResponse({'error': "only this team's director, the admin, or the player may edit the teamwork agreement"}, status_code=403)
    if requester != 'player' and not _can_write_team(state, requester, team_id):
        return JSONResponse({'error': 'only a team director, a chain-above director, or the chief of staff may edit this team'}, status_code=403)
    if body is None:
        body = {}
    changed = False
    if 'name' in body:
        name = (body.get('name') or '').strip()
        if name and name != t.get('name'):
            t['name'] = name
            changed = True
    if 'purpose' in body:
        purpose = (body.get('purpose') or '').strip()
        if purpose != t.get('purpose'):
            t['purpose'] = purpose
            changed = True
    if is_agreement_edit:
        import core.config as _config
        raw = body.get('agreement')
        if raw is None:
            if t.get('agreement') is not None:
                t['agreement'] = None
                changed = True
        elif isinstance(raw, dict):
            base = t.get('agreement') if isinstance(t.get('agreement'), dict) else {}
            cleaned = _config.validate_team_agreement({**base, **raw}) or None
            if cleaned != (t.get('agreement') or None):
                t['agreement'] = cleaned
                changed = True
        else:
            return JSONResponse({'error': 'agreement must be an object of teamwork-agreement keys or null to clear it'}, status_code=400)
    if changed:
        t['updatedBy'] = requester
        t['updatedAt'] = int(time.time() * 1000)
        save_state_to_db(state)
        log_action(requester, 'team_update', {'team': team_id}, authorized=True)
    return JSONResponse({'ok': True, 'team': t})


@app.post('/api/teams/{team_id}/promote')
@_state_writer
async def promote_to_director(team_id: str, request: Request):
    """Promote a MEMBER of this team to director. The promoter must be the
    team's director (or the admin). The promoted agent leaves the old team's
    daily work, becomes the director of a brand-new team they can hire for, and
    sits directly under the chief of staff -- the same flat standing as every other
    director, never under the promoting director. Body: {promoteeId}."""
    requester = _resolve_requester(request)
    if not requester:
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    if verify_agent_key(requester, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    teams = state.get('teams', [])
    t = next((x for x in teams if x.get('id') == team_id), None)
    if not t:
        return JSONResponse({'error': 'unknown team'}, status_code=404)
    if not _can_write_team(state, requester, team_id):
        return JSONResponse({'error': 'only this team\'s director or a chain-above director may promote'}, status_code=403)
    body = await request.json()
    promotee_id = (body.get('promoteeId') or '').strip()
    if not promotee_id or promotee_id not in (t.get('members') or []):
        return JSONResponse({'error': 'promotee must be a current member of this team'}, status_code=400)
    promoter_id = t.get('directorId') or requester
    new_team = _promote_to_director(state, promotee_id, promoter_id)
    if not new_team:
        return JSONResponse({'error': 'could not promote'}, status_code=400)
    save_state_to_db(state)
    log_action(requester, 'promote_director', {'promotee': promotee_id, 'team': team_id, 'newTeam': new_team.get('id')}, authorized=True)
    return JSONResponse({'ok': True, 'promoted': promotee_id, 'team': new_team})


@app.post('/api/teams/{team_id}/scrum-master')
@_state_writer
async def set_team_scrum_master(team_id: str, request: Request):
    """Designate (or clear) a team's SCRUM MASTER -- the standing facilitator
    who runs that team's sprint ceremonies. Gate: the team director, a
    chain-above director, or the chief of staff (same ACL as team edits/promote). Body:
    {scrumMasterId} -- must be a current member of the team (the director and
    any derived member are valid). Send an empty string to unset. The scrum
    master is a SPRINT-CRITICAL role: no sprint involving a team can be created
    while that team lacks one (enforced in /api/intent/sprint)."""
    requester = _resolve_requester(request)
    if not requester:
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    if verify_agent_key(requester, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    teams = state.get('teams', [])
    t = next((x for x in teams if x.get('id') == team_id), None)
    if not t:
        return JSONResponse({'error': 'unknown team'}, status_code=404)
    if not _can_write_team(state, requester, team_id):
        return JSONResponse({'error': 'only this team\'s director, a chain-above director, or the chief of staff may designate its scrum master'}, status_code=403)
    body = await request.json()
    sm_id = (body.get('scrumMasterId') or '').strip()
    if sm_id:
        members = _derive_team_members(state, team_id)
        # Valid: the team director themself, a derived member, or the admin.
        if sm_id != t.get('directorId') and sm_id not in members and not _is_admin(state, sm_id):
            return JSONResponse({'error': 'the scrum master must be a current member of this team (or its director)'}, status_code=400)
    t['scrumMasterId'] = sm_id or None
    t['scrumMasterSetBy'] = requester
    t['scrumMasterSetAt'] = int(time.time() * 1000)
    save_state_to_db(state)
    log_action(requester, 'scrum_master_set', {'team': team_id, 'scrumMaster': sm_id or None},
               authorized=True)
    return JSONResponse({'ok': True, 'team': _team_record(state, team_id)})


@app.post('/api/rooms/{room}/purpose')
@_state_writer
async def update_room_purpose(room: str, request: Request):
    requester = _resolve_requester(request)
    if not requester:
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    authorized = verify_agent_key(requester, request.headers.get('X-Agent-Key'))
    if authorized is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    if room not in _DELEGATABLE_ROOMS:
        return JSONResponse({'error': 'unknown room'}, status_code=400)
    body = await request.json()
    purpose = (body.get('purpose') or '').strip()
    if not purpose:
        return JSONResponse({'error': 'purpose is required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _is_director_or_admin(state, requester):
        return JSONResponse({'error': 'only directors and the chief of staff may edit room descriptions'}, status_code=403)
    defs = _room_definitions(state)
    # The label is locked -- only the description changes. Never let the player
    # rename what the code graph keys off.
    defs[room]['label'] = (defs[room].get('label')
                           or _DEFAULT_ROOM_DEFINITIONS[room]['label'])
    defs[room]['purpose'] = purpose
    defs[room]['updatedBy'] = requester
    defs[room]['updatedAt'] = int(time.time() * 1000)
    state['roomDefinitions'] = defs
    save_state_to_db(state)
    log_action(requester, 'room_purpose_update', {'room': room}, authorized=True)
    return JSONResponse({'ok': True, 'room': room, 'label': defs[room]['label'], 'purpose': purpose})


# ---------------------------------------------------------------------------
# Director-owned role templates. Any authenticated agent may READ the library;
# only directors and the chief of staff (see _is_director_or_admin) may create, edit, or
# delete. Edits retroactively re-stamp every live agent holding that role.
# ---------------------------------------------------------------------------
@app.get('/api/templates')
async def list_templates(request: Request):
    # Reads are open to anyone with the app open -- the House/Board is a
    # player-driven dialog, and the player is not an agent, so a requester
    # (agent-key) must NOT be required just to VIEW the library. Only writing
    # (POST/DELETE below) is gated to directors/admin.
    state = get_state_from_db() or {}
    templates = _templates_from_db(state)
    return JSONResponse({'templates': templates})


@app.get('/api/templates/{role}')
async def get_template(role: str, request: Request):
    state = get_state_from_db() or {}
    templates = _templates_from_db(state)
    if role not in templates:
        # Still let an authenticated agent see the fallback so they know a role
        # exists without a bespoke template yet.
        return JSONResponse({'role': role, 'profile': _profile_for_role(state, role), 'seeded': role in _SEED_PROFILES})
    return JSONResponse({'role': role, 'profile': templates[role], 'seeded': role in _SEED_PROFILES})


@app.post('/api/templates')
@_state_writer
async def upsert_template(request: Request):
    requester = _resolve_requester(request)
    if not requester:
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    authorized = verify_agent_key(requester, request.headers.get('X-Agent-Key'))
    if authorized is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    body = await request.json()
    role = (body.get('role') or '').strip()
    mission = (body.get('mission') or '').strip()
    instructions = body.get('instructions') or []
    notes = body.get('notes') or []
    if not role:
        return JSONResponse({'error': 'role is required'}, status_code=400)
    if not isinstance(instructions, list):
        return JSONResponse({'error': 'instructions must be a list'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _is_director_or_admin(state, requester):
        return JSONResponse({'error': 'only directors and the chief of staff may author templates'}, status_code=403)
    template = {
        'mission': mission,
        'instructions': [str(i) for i in instructions],
        'notes': [str(n) for n in notes],
        'updatedBy': requester,
        'updatedAt': int(time.time() * 1000),
    }
    templates = _templates_from_db(state)
    is_new = role not in templates
    templates[role] = template
    state['templates'] = templates
    applied = _apply_template_to_role(state, role, template, requester)
    log_action(requester, 'template_create' if is_new else 'template_update', {'role': role, 'appliedTo': len(applied)}, authorized=True)
    _append_passport_decision('template_' + ('create' if is_new else 'update'), requester, {'role': role})
    return JSONResponse({'ok': True, 'role': role, 'created': is_new, 'appliedTo': applied})


@app.post('/api/templates/{role}/apply')
@_state_writer
async def apply_template(role: str, request: Request):
    # Explicit re-stamp without changing the template -- useful after a hire or
    # a roster change, so a director can push an existing template to the agents
    # currently in that role even though the upsert already does this.
    requester = _resolve_requester(request)
    if not requester:
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    authorized = verify_agent_key(requester, request.headers.get('X-Agent-Key'))
    if authorized is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _is_director_or_admin(state, requester):
        return JSONResponse({'error': 'only directors and the chief of staff may apply templates'}, status_code=403)
    templates = _templates_from_db(state)
    if role not in templates:
        return JSONResponse({'error': 'no template for that role'}, status_code=404)
    applied = _apply_template_to_role(state, role, templates[role], requester)
    log_action(requester, 'template_applied', {'role': role, 'appliedTo': len(applied)}, authorized=True)
    return JSONResponse({'ok': True, 'role': role, 'appliedTo': applied})


@app.delete('/api/templates/{role}')
@_state_writer
async def delete_template(role: str, request: Request):
    requester = _resolve_requester(request)
    if not requester:
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    authorized = verify_agent_key(requester, request.headers.get('X-Agent-Key'))
    if authorized is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _is_director_or_admin(state, requester):
        return JSONResponse({'error': 'only directors and the chief of staff may delete templates'}, status_code=403)
    templates = _templates_from_db(state)
    if role not in templates:
        return JSONResponse({'error': 'no template for that role'}, status_code=404)
    del templates[role]
    state['templates'] = templates
    save_state_to_db(state)
    log_action(requester, 'template_delete', {'role': role}, authorized=True)
    return JSONResponse({'ok': True, 'role': role})


@app.post('/api/task-budget')
@_state_writer
async def grant_task_budget(request: Request):
    """Item 4 runtime override: a director/admin grants a budget-exhausted card
    (status 'failed' + budgetExhausted) more model-spend money and re-opens it.
    The sim's _director_grant_budget_reopen refuses unless the new ceiling is a
    real raise past what was already spent (else the /api/chat gate would refuse
    the very next call), and re-queues the card under the new budget."""
    requester = _resolve_requester(request)
    if not requester:
        return JSONResponse({'error': 'unauthenticated'}, status_code=401)
    authorized = verify_agent_key(requester, request.headers.get('X-Agent-Key'))
    if authorized is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    body = await request.json()
    task_id = (body.get('taskId') or '').strip()
    new_budget = body.get('budgetUsd')
    if not task_id:
        return JSONResponse({'error': 'taskId is required'}, status_code=400)
    if not isinstance(new_budget, (int, float)) or new_budget <= 0:
        return JSONResponse({'error': 'budgetUsd must be a positive number'}, status_code=400)
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _is_director_or_admin(state, requester):
        return JSONResponse({'error': 'only directors and the chief of staff may grant task budget'}, status_code=403)
    result = _sim._director_grant_budget_reopen(
        state, task_id, new_budget, director_id=requester)
    if not result.get('ok'):
        return JSONResponse({'error': result.get('error', 'could not re-open the task')}, status_code=400)
    save_state_to_db(state)
    log_action(requester, 'task_budget_granted',
               {'taskId': task_id, 'budgetUsd': result.get('budgetUsd'),
                'spentUsd': result.get('spentUsd'), 'attempts': result.get('attempts')},
               authorized=True)
    return JSONResponse({'ok': True, **result})


def _safe_library_path(rel_path):
    # Path-traversal guard -- must resolve to strictly inside LIBRARY_DIR.
    # A shared, agent-writable directory is exactly the kind of thing a
    # "../../.env" style path needs to be defended against explicitly.
    base = os.path.abspath(LIBRARY_DIR)
    target = os.path.normpath(os.path.join(base, rel_path))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


def _owns_library_path(rel_path):
    # Maps a Library-relative path to the agent who owns it, or None if it's
    # shared commons. The per-agent personal space is downloads/<agent_id>/ in
    # the promoted tree and pending_review/downloads/<agent_id>/ before
    # promotion (see _download_dest_rel_path). Everything else -- shared/,
    # skills/, projects/, think tank/, archive/, rejected/ -- is the commons any
    # agent can write. Returns the owning agent id, or None for commons.
    norm = rel_path.strip('/').split('/')
    if len(norm) < 2:
        return None
    if norm[0] == 'downloads' and len(norm) >= 2:
        return norm[1]
    if norm[0] == 'pending_review' and len(norm) >= 3 and norm[1] == 'downloads':
        return norm[2]
    return None


def _library_path_village(rel_path, state=None):
    """The village a Library-relative path belongs to. Non-main villages live
    under library/villages/<vid>/...; design-references/<village>/ and the
    per-agent downloads/<agent>/ / pending_review/downloads/<agent>/ trees
    already encode a village (the owning agent's). Anything else (the flat,
    legacy tree) belongs to 'main'. Falls back to 'main' for unknown agents."""
    norm = rel_path.strip('/').split('/')
    if norm and norm[0] == 'villages':
        return norm[1] if len(norm) >= 2 else 'main'
    if norm and norm[0] == 'design-references' and len(norm) >= 2:
        v = norm[1]
        if v not in ('main', 'general', 'root', ''):
            return v
    if state:
        import village.sim_helpers as _sh
        if norm[0] == 'downloads' and len(norm) >= 2:
            return _sh.village_of_agent(state, norm[1]) or 'main'
        if norm[0] == 'pending_review' and len(norm) >= 3 and norm[1] == 'downloads':
            return _sh.village_of_agent(state, norm[2]) or 'main'
    return 'main'


def _requester_villages(requester):
    """The set of village ids `requester` may access, or None for ALL.
    Player (None/empty/'player') and the chief of staff cross the village boundary; any
    other agent gets only their own village. The returned None means "every
    village" so _scope_allows stays a single two-way check."""
    if not requester or requester == 'player':
        return None
    state = get_state_from_db()
    if not state:
        # No state means no agents and no village data to isolate against.
        return None
    if _is_admin(state, requester):
        return None
    import village.sim_helpers as _sh
    return {_sh.village_of_agent(state, requester)}


def _scope_allows(allowed, path_village):
    # None == all villages (player/admin); a set == an explicit allowlist.
    if allowed is None:
        return True
    return path_village in allowed


def _library_namespace_write(agent_id, rel_path, state=None):
    """Namespace a library WRITE so it lands in the writer's own village.

    Main stays flat (legacy layout, all existing content + consumers expect
    it). A non-main, non-chief-of-staff, non-player agent writing COMMONS (skills/,
    shared/, ...) gets it placed under villages/<vid>/ so it never leaks into
    main's flat tree and a different village can't see it. Paths that already
    encode a village (villages/<vid>/, design-references/<village>/,
    downloads/<agent>/ -- scoped by the owning agent) are returned unchanged.
    Admin (Theo) and the player are not namespaced: they cross the boundary."""
    if not rel_path or agent_id in (None, 'player'):
        return rel_path
    state = state if state is not None else get_state_from_db()
    if not state or _is_admin(state, agent_id):
        return rel_path
    import village.sim_helpers as _sh
    vid = _sh.village_of_agent(state, agent_id)
    if not vid or vid == 'main':
        return rel_path
    # Already village-scoped by path or owner.
    if rel_path.startswith('villages/') or _library_path_village(rel_path, state) != 'main':
        return rel_path
    return 'villages/' + vid + '/' + rel_path.lstrip('/')


def _split_village_prefix(rel_path):
    """Split a library-relative path into (village_id_or_None, rest). Paths under
    villages/<vid>/... yield the village + the remainder; flat/legacy paths
    yield (None, the whole path)."""
    norm = rel_path.strip('/').split('/')
    if norm and norm[0] == 'villages' and len(norm) >= 3:
        return norm[1], '/'.join(norm[2:])
    return None, rel_path


@app.get('/api/library')
async def list_library(request: Request):
    state = get_state_from_db()
    requester = _resolve_requester(request)
    allowed = _requester_villages(requester)
    os.makedirs(LIBRARY_ARCHIVE_DIR, exist_ok=True)
    rows = []  # (modified_float, rel_path, size) -- typed tuple avoids dict-value widening
    for root, _dirs, filenames in os.walk(LIBRARY_DIR):
        # Gap (found while adding trail-based ranking
        # to search): nothing here skipped dotfiles, so .passport.json (the
        # hash-chain ledger, LIBRARY_DIR's own reserved file) was listed and
        # content-searched right alongside real agent-authored knowledge --
        # an agent asking search_library about e.g. "decision" could get its
        # own passport ledger back as a "finding." Same _agent_rel_path_is_
        # visible dotfile-hiding convention agent-files already applies.
        filenames = [fn for fn in filenames if not fn.startswith('.')]
        for fn in filenames:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, LIBRARY_DIR)
            # Village isolation: only surface files the requester's village may
            # see (player/admin cross; an agent sees only its own village).
            if not _scope_allows(allowed, _library_path_village(rel, state)):
                continue
            rows.append((os.path.getmtime(full), rel, os.path.getsize(full)))
    rows.sort(key=lambda r: -r[0])
    files = [{'path': p, 'size': s, 'modified': m} for m, p, s in rows]
    return JSONResponse({'files': files})


# Per-agent file browsing (issue #9): agents view one another's files (READ
# for any agent -- the think tank is transparent about who did what) but cannot
# WRITE another agent's directory unless they're that agent's director/above
# or the admin. Reads are served read-only here; writes only happen through
# the server's own materialization (sync_agent_directories) and the gated
# write paths, so the write-ACL at the endpoints is the enforcement point.
# Deliberately only the materialized, non-secret mirror is listed -- the
# conversation/state/profile files are already what any think tank member is
# meant to see, and nothing under agents/<id>/ holds server credentials.
# Skip the VCS internals (prototypes/ is itself a git repo) and OS junk --
# nobody's "own files" include .git plumbing or .DS_Store.
_AGENT_FILE_SKIP = {'conversations', '.git', '.DS_Store'}


def _agent_rel_path_is_visible(parts):
    # A path component is invisible if it's a skipped dir name or a
    # hidden/dotfile (leading '.').
    for part in parts:
        if part in _AGENT_FILE_SKIP or part.startswith('.'):
            return False
    return True


def path_components(root, base):
    # The path segments from base to root ('' when root === base), used to
    # detect whether a walked file sits under a specific subdir (e.g. reports/).
    rel = os.path.relpath(root, base)
    if rel == '.':
        return []
    return rel.split(os.sep)


def is_under_reports(rel_path):
    # True when the first path segment is 'reports/' -- the on-disk directory
    # where peer reports about an agent are materialized.
    return rel_path.split('/')[0] == 'reports'


def _owns_reports_dir(agent_id, requester_id):
    # An agent must NOT be able to view or modify
    # its OWN reports/ directory -- peer reports about it are for others to
    # read, not for it to sanitize or delete. The target agent whose reports/
    # is being requested is 'agent_id'; 'requester_id' is the identity behind
    # the request. Returns True only when a real agent is asking about ITS OWN
    # reports. The player (requester None / 'player') keeps full ops visibility.
    if not requester_id or requester_id in ('player', 'unknown'):
        return False
    return requester_id == agent_id


def _agent_reports_off_limits(requester_id):
    # Reviewer independence (2026-10-06): NO agent may read the reports/
    # directory of ANY employee through the file API -- reviews are write-only
    # from an agent's perspective. An employee who wants to write a review must
    # not first read what has already been filed about that same employee: that
    # keeps every verdict independent (no herding off prior reviews) and stops
    # an adversarial actor from reading what is said about them to tailor a
    # denial. The player/ops (requester None / 'player') keeps full visibility;
    # the firing reviewers already receive the reports through the state
    # (_firing_consultation), never through the file API, so nothing they need
    # is taken away.
    return bool(requester_id) and requester_id not in ('player', 'unknown')


def _cross_village_agent_denied(state, requester, target_agent_id):
    """True when `requester` is a real, non-chief-of-staff agent from a DIFFERENT village
    than the target agent -- i.e. the per-agent-file read must be refused under
    village isolation. Player (None/'player') and the chief of staff cross the boundary."""
    if not requester or requester == 'player':
        return False
    if not state or _is_admin(state, requester):
        return False
    import village.sim_helpers as _sh
    rv = _sh.village_of_agent(state, requester)
    tv = _sh.village_of_agent(state, target_agent_id)
    return rv != tv


def _agent_id_for_key(presented_key):
    """Reverse lookup: the agent_id that owns a bearer key, or None. A key that
    matches nothing (or no key) is not an agent identity -- the reverse of
    `verify_agent_key`, used to derive WHO a request is from when only a key is
    presented (the server's content executors loopback with a key, no session)."""
    if not presented_key:
        return None
    with _db() as conn:
        row = conn.execute('SELECT agent_id FROM agent_keys WHERE secret_key = ?',
                           (presented_key,)).fetchone()
    return row[0] if row else None


def _resolve_requester(request):
    # Resolve who a request is being made AS. The authenticated PRINCIPAL is
    # authoritative -- never the caller-supplied requesterId, which is a
    # narrowing hint only:
    #   - a valid PLAYER session -> the player (None = all-village / ops). The
    #     UI may still narrow with a verified requesterId, but the session alone
    #     is enough to be the player;
    #   - otherwise the presented X-Agent-Key's OWNER is the requester (an agent
    #     can only ever be itself -- a misclaimed requesterId is ignored, not
    #     trusted);
    #   - nothing valid -> None. Callers must treat that None as
    #     UNAUTHENTICATED, not as the all-village player (the fail-open that let
    #     an agent with a key but no requesterId read every village).
    session_ok = verify_session(request.cookies.get(SESSION_COOKIE_NAME))
    key_owner = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    claimed = (request.query_params.get('requesterId') or '').strip() or None
    if claimed in ('player', 'unknown'):
        claimed = None
    if session_ok:
        # A real player session. Honor an explicit, verified agent claim (the
        # UI asks for a specific agent with the session present), else player.
        if claimed and key_owner == claimed:
            return claimed
        return None
    if key_owner:
        # No session: the key's owner is the requester. An explicit claim that
        # differs is ignored -- the key is what authenticates, not the body.
        return claimed if claimed == key_owner else key_owner
    return None
