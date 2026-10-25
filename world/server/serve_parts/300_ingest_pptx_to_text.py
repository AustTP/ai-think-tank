

def _ingest_pptx_to_text(path):
    """PowerPoint (.pptx) text extraction: every slide's shape text, slide by
    slide, with a per-slide header. Bounded to the first 200 slides like the
    PDF extractor; a huge deck gets a truncation note instead of unbounded
    extraction."""
    if not _pptx:
        return None, 'python-pptx is not installed on the server'
    try:
        prs = _pptx.Presentation(path)
        parts = []
        for i, slide in enumerate(prs.slides):
            if i >= 200:
                parts.append(f'... ({max(0, len(prs.slides) - 200)} more slides not extracted)')
                break
            slide_text = []
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                for para in shape.text_frame.paragraphs:
                    text = ''.join(run.text for run in para.runs).strip()
                    if text:
                        slide_text.append(text)
            if slide_text:
                parts.append(f'--- slide {i + 1} ---')
                parts.extend(slide_text)
        if not parts:
            return '', None
        return '\n'.join(parts), None
    except Exception as e:
        return None, str(e)


def _write_ingested_text(dest_rel, content, results, name):
    target = _safe_library_path(dest_rel)
    if not target:
        results.append({'file': name, 'ok': False, 'note': 'invalid destination path'})
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'w') as f:
        f.write(content[:400_000])
    results.append({'file': name, 'ok': True, 'path': dest_rel})


def _ingest_one_file(src_path, dest_rel_dir, results: list[dict[str, object]]):
    ext = os.path.splitext(src_path)[1].lower()
    name = os.path.basename(src_path)
    try:
        size = os.path.getsize(src_path)
    except OSError as e:
        results.append({'file': name, 'ok': False, 'note': str(e)})
        return
    if size > INGEST_MAX_FILE_BYTES:
        results.append({'file': name, 'ok': False, 'note': f'skipped -- {size} bytes exceeds the {INGEST_MAX_FILE_BYTES}-byte cap'})
        return

    if ext == '.pdf':
        text, err = _ingest_pdf_to_text(src_path)
        if err:
            results.append({'file': name, 'ok': False, 'note': f'PDF extraction failed: {err}'})
            return
        _write_ingested_text(os.path.join(dest_rel_dir, name + '.md'), f'# {name} (PDF, text-extracted)\n\n' + _redact_secrets(text), results, name)

    elif ext in ('.xlsx', '.xls'):
        text, err = _ingest_xlsx_to_text(src_path)
        if err:
            results.append({'file': name, 'ok': False, 'note': f'Excel extraction failed: {err}'})
            return
        _write_ingested_text(os.path.join(dest_rel_dir, name + '.md'), f'# {name} (Excel, extracted)\n\n' + _redact_secrets(text), results, name)

    elif ext == '.docx':
        text, err = _ingest_docx_to_text(src_path)
        if err:
            results.append({'file': name, 'ok': False, 'note': f'Word extraction failed: {err}'})
            return
        _write_ingested_text(os.path.join(dest_rel_dir, name + '.md'), f'# {name} (Word, text-extracted)\n\n' + _redact_secrets(text), results, name)

    elif ext in ('.pptx', '.ppt'):
        text, err = _ingest_pptx_to_text(src_path)
        if err:
            results.append({'file': name, 'ok': False, 'note': f'PowerPoint extraction failed: {err}'})
            return
        _write_ingested_text(os.path.join(dest_rel_dir, name + '.md'), f'# {name} (PowerPoint, text-extracted)\n\n' + _redact_secrets(text), results, name)

    elif ext in INGEST_TEXT_EXTENSIONS:
        try:
            with open(src_path, 'r', errors='replace') as f:
                text = f.read(INGEST_MAX_FILE_BYTES)
        except Exception as e:
            results.append({'file': name, 'ok': False, 'note': str(e)})
            return
        _write_ingested_text(os.path.join(dest_rel_dir, name), _redact_secrets(text), results, name)

    elif ext in INGEST_IMAGE_EXTENSIONS:
        try:
            with open(src_path, 'rb') as f:
                raw = f.read()
        except Exception as e:
            results.append({'file': name, 'ok': False, 'note': str(e)})
            return
        dest_rel = os.path.join(dest_rel_dir, name)
        target = _safe_library_path(dest_rel)
        if not target:
            results.append({'file': name, 'ok': False, 'note': 'invalid destination path'})
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as f:
            f.write(raw)
        # Copied as-is, not auto-described -- per an earlier cost-
        # conscious decision, an eager vision call on every ingested image
        # would spend money whether or not anyone ever needed it. An
        # agent can call the same real vision review on it later, on
        # demand, exactly when a task actually needs to look at it.
        results.append({'file': name, 'ok': True, 'path': dest_rel, 'note': 'image copied -- use a vision review on it when a task actually needs to see it'})

    else:
        results.append({'file': name, 'ok': False, 'note': f'unsupported file type ({ext or "no extension"}), skipped'})


def _ingest_walk(root_dir, dest_root, results: list[dict[str, object]]):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in INGEST_SKIP_DIRS]
        for fn in filenames:
            if fn == '.DS_Store':
                continue
            if count >= INGEST_MAX_FILES:
                results.append({'file': fn, 'ok': False, 'note': f'skipped -- ingest cap of {INGEST_MAX_FILES} files reached'})
                continue
            full = os.path.join(dirpath, fn)
            rel_dir = os.path.relpath(dirpath, root_dir)
            dest_dir = dest_root if rel_dir == '.' else os.path.join(dest_root, rel_dir)
            _ingest_one_file(full, dest_dir, results)
            count += 1


@app.post('/api/library/ingest')
async def ingest_library(request: Request):
    # Player-only -- see the module comment above. The gate is a REAL player
    # session, not the body's self-declared agentId: an agent key bearer must
    # not be able to point this at arbitrary host paths (~/.ssh/id_rsa, .env).
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    agent_id = body.get('agentId', 'player')
    # Deliberately not agent-callable -- see the module comment above.
    if agent_id != 'player':
        return JSONResponse({'error': 'ingestion is player-only'}, status_code=403)
    source_path = os.path.expanduser((body.get('sourcePath') or '').strip())
    dest_prefix = (body.get('destPath') or '').strip().strip('/')
    if not source_path:
        return JSONResponse({'error': 'sourcePath is required'}, status_code=400)
    if not os.path.exists(source_path):
        return JSONResponse({'error': f'{source_path} does not exist'}, status_code=404)

    default_name = os.path.basename(source_path.rstrip('/')) or 'ingested'
    results: list[dict[str, object]] = []

    if os.path.isfile(source_path):
        ext = os.path.splitext(source_path)[1].lower()
        if ext == '.zip':
            dest_dir = dest_prefix or f'ingested/{default_name}'
            try:
                with zipfile.ZipFile(source_path) as zf:
                    total = sum(i.file_size for i in zf.infolist())
                    # A real zip-bomb guard -- checked against the
                    # DECLARED uncompressed size before ever extracting
                    # anything, not discovered partway through.
                    if total > INGEST_MAX_FILE_BYTES * 10:
                        return JSONResponse({'error': f'zip contents ({total} bytes uncompressed) exceed the ingest cap'}, status_code=400)
                    with tempfile.TemporaryDirectory() as tmp:
                        zf.extractall(tmp)
                        _ingest_walk(tmp, dest_dir, results)
            except zipfile.BadZipFile:
                return JSONResponse({'error': 'not a valid zip file'}, status_code=400)
        else:
            _ingest_one_file(source_path, dest_prefix or 'ingested', results)
    else:
        dest_dir = dest_prefix or f'ingested/{default_name}'
        _ingest_walk(source_path, dest_dir, results)

    ok_count = sum(1 for r in results if r.get('ok'))
    log_action(agent_id, 'library_ingest', {'sourcePath': source_path, 'fileCount': len(results), 'okCount': ok_count})
    return JSONResponse({'ok': True, 'results': results, 'okCount': ok_count, 'totalCount': len(results)})


@app.get('/api/memory-ledger')
async def memory_ledger(request: Request):
    """Read-only audit trail of shared-memory writes (wiki pages + taste folds):
    what the team learned, where, who owns it, its clock, and when to recheck.
    This is the provenance ledger that makes a stale or wrong memory findable
    and correctable (claim / source / owner / scope / review date)."""
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    entries = list(reversed(state.get('memoryLedger') or []))
    return JSONResponse({'ledger': entries})


# ---------------------------------------------------------------------------
# Adversarial (winter) village.
# ---------------------------------------------------------------------------
# The winter map (think_tank_winter.png) is the main boundary map flipped
# horizontally, connected to the main village along the right-edge path band.
# It is NOT always on: Theo (the admin) toggles it via the `adversarial_village`
# tool. Enabling spawns a winter director + small team and files the same task
# to BOTH the main and winter directors (tagged with a shared adversarialTaskId)
# so the two sides work the same goal adversarially. When both sides have
# delivered, the sim tick (_adversarial_village_tick) completes it and disables
# the winter village (its team returns to the main village; the client pulls the
# player back when `enabled` flips false). The whole record is a top-level
# kv_state key so _merge_server_owned carries it across client autosaves.
_ADV_VILLAGE_KEY = 'adversarialVillage'
_ADV_VILLAGE_ID = 'winter'
_ADV_VILLAGE_NAME = 'Winter Village'
_ADV_TEAM_SIZE = 3
_ADV_DELIVERED_STATUSES = ('done', 'closed', 'needs_review')


def _adversarial_state(state):
    st = state.setdefault(_ADV_VILLAGE_KEY, {
        'enabled': False, 'status': 'disabled', 'villageId': _ADV_VILLAGE_ID,
        'taskGoal': None, 'taskId': None, 'winterDirectorId': None,
        'launchedAt': None, 'mainRequestId': None, 'winterRequestId': None,
    })
    st.setdefault('status', 'disabled')
    return st


def _spawn_winter_team(state, admin_id, goal, now_ms):
    """Create the 'winter' village and spawn a fresh director + small team in
    it, reassigning every new agent to villageId 'winter'. Returns the winter
    director id, or None on failure (name pool / agent cap exhausted)."""
    import sim as _sim
    villages = state.setdefault('villages', [])
    if not any(v.get('id') == _ADV_VILLAGE_ID for v in villages):
        villages.append({'id': _ADV_VILLAGE_ID, 'name': _ADV_VILLAGE_NAME})
    team = _sim.spawn_new_team_for_request(state, goal, now_ms=now_ms, admin_id=admin_id,
                                           employees=_ADV_TEAM_SIZE)
    if not team:
        return None
    director_id = team.get('directorId') or team.get('id')
    for mid in [director_id] + list(team.get('members') or []):
        if mid:
            _sim.set_agent_village(state, mid, _ADV_VILLAGE_ID)
    return director_id


def _adv_side_delivered(state, task_id, village_id):
    """A side of the adversarial task has delivered when every task tagged
    (task_id, village_id) is in a terminal-delivered state, and at least one
    real task exists for that side. No tasks yet = not delivered."""
    tasks = state.get('tasks') or {}
    side = [t for t in tasks.values()
            if t.get('adversarialTaskId') == task_id
            and (t.get('villageId') or 'main') == village_id]
    if not side:
        return False
    return all(t.get('status') in _ADV_DELIVERED_STATUSES for t in side)


def _disable_adversarial_village(state, reason='completed'):
    """Drain + disable: reassign the winter team back to the main village,
    drop the winter village from the registry, and flip enabled off. The client
    pulls the player back to the main map when it sees enabled=false."""
    import sim as _sim
    st = _adversarial_state(state)
    director_id = st.get('winterDirectorId')
    teams = state.get('teams') or []
    team = next((t for t in teams if t.get('id') == director_id), None)
    for mid in [director_id] + list((team or {}).get('members') or []):
        if mid:
            _sim.set_agent_village(state, mid, 'main')
    state['teams'] = [t for t in teams if t.get('id') != director_id]
    # Failsafe: no agent may remain tagged winter once the village is gone.
    for a in (state.get('agents') or {}).values():
        if (a.get('villageId') or 'main') == _ADV_VILLAGE_ID:
            a['villageId'] = 'main'
    state['villages'] = [v for v in (state.get('villages') or [])
                         if v.get('id') != _ADV_VILLAGE_ID]
    st['enabled'] = False
    st['status'] = 'disabled'
    st['winterDirectorId'] = None
    st['lastStatus'] = reason
    log_action('system', 'adversarial_village_disabled', {'reason': reason})
    return st


def _launch_adversarial_village(state, admin_id, task, now_ms=None):
    """Enable the adversarial village: spawn the winter team and file the same
    goal to BOTH the main and winter directors, tagged with one shared
    adversarialTaskId. Returns (ok, message)."""
    import sim as _sim
    st = _adversarial_state(state)
    if st.get('enabled'):
        return False, 'The adversarial village is already active.'
    task = (task or '').strip()
    if not task:
        return False, 'A task/goal is required to launch the adversarial village.'
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    task_id = st.get('taskId') or ('adv-' + str(int(time.time() * 1000)))
    st['taskId'] = task_id
    st['taskGoal'] = task
    winter_director = _spawn_winter_team(state, admin_id, task, now_ms)
    if not winter_director:
        return False, 'Could not spawn the winter team (name pool or agent cap exhausted).'
    st['winterDirectorId'] = winter_director
    st['launchedAt'] = now_ms
    st['enabled'] = True
    st['status'] = 'active'
    # File the same goal to the MAIN village's free authority team.
    authority = _free_authority(state) or admin_id
    team = _staffing_team_for_authority(state, authority)
    main_team_id = (team.get('id') or team.get('directorId')) if team else authority
    main_req = _sim.file_large_request(state, authority, task, main_team_id, now_ms=now_ms)
    if main_req:
        main_req['adversarialTaskId'] = task_id
        main_req['villageId'] = 'main'
        st['mainRequestId'] = main_req['id']
    # The winter side was already filed by spawn_new_team_for_request; tag it.
    for req in state.get('backlogRequests') or []:
        if req.get('teamId') == winter_director:
            req['adversarialTaskId'] = task_id
            req['villageId'] = _ADV_VILLAGE_ID
            st['winterRequestId'] = req['id']
            break
    log_action(admin_id, 'adversarial_village_enabled',
               {'task': task, 'taskId': task_id, 'winterDirector': winter_director})
    return True, (f'Adversarial village launched. Winter director: {winter_director}. '
                  f'The same goal is now in both the main village and the winter village.')


def _adversarial_village_tick(state):
    """Called from the sim loop each tick. Advances the adversarial village's
    lifecycle: when BOTH sides have delivered the shared task, complete and
    disable the winter village. No-op when disabled. Mutates `state` in place
    (runs inside the sim's single read-modify-write, before the one save)."""
    st = _adversarial_state(state)
    if not st.get('enabled') or st.get('status') != 'active':
        return
    task_id = st.get('taskId')
    if not task_id:
        return
    main_done = _adv_side_delivered(state, task_id, 'main')
    winter_done = _adv_side_delivered(state, task_id, _ADV_VILLAGE_ID)
    if main_done and winter_done:
        log_action('system', 'adversarial_village_completed',
                   {'taskId': task_id, 'taskGoal': st.get('taskGoal')})
        _disable_adversarial_village(state, reason='completed')


@app.get('/api/adversarial-village')
async def adversarial_village_status(request: Request):
    """Adversarial (winter) village status, polled by the client so it knows
    whether the winter map is walkable. Read-only, any authenticated client."""
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    st = _adversarial_state(state)
    return JSONResponse({
        'enabled': bool(st.get('enabled')),
        'status': st.get('status'),
        'taskGoal': st.get('taskGoal'),
        'winterDirector': st.get('winterDirectorId'),
    })


@app.get('/api/villages')
async def list_villages(request: Request):
    """Read-only village listing. Shows the registry + which agents belong to
    each village (for the boundary UI). Any authenticated client can read it;
    only the player may mutate it."""
    state = get_state_from_db()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    import sim as _sim
    villages = _sim.ensure_villages(state)
    agents = state.get('agents') or {}
    roster = {d.get('id'): d.get('name', '') for d in (state.get('agentRoster') or [])}
    village_spend = _village_spend_view()
    out = []
    for v in villages:
        vid = v.get('id')
        out.append({
            'id': vid,
            'name': v.get('name'),
            'members': sorted(aid for aid, a in agents.items()
                              if (a.get('villageId') or _sim.DEFAULT_VILLAGE) == vid),
            'memberNames': {aid: roster.get(aid, aid) for aid in agents
                            if (agents[aid].get('villageId') or _sim.DEFAULT_VILLAGE) == vid},
            'spend': village_spend.get(vid, {'used': 0.0, 'calls': 0}),
        })
    return JSONResponse({'villages': out})


@app.post('/api/villages')
@_state_writer
async def create_village_endpoint(request: Request):
    """Create a new village (player-only). A village is the top-level identity
    + memory boundary: its agents only ever read their own wiki and their own
    design taste. {name} required; optionally {agentIds} to seed membership."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    name = (body.get('name') or '').strip()
    if not name:
        return JSONResponse({'error': 'a village name is required'}, status_code=400)
    record = _sim.create_village(state, name)
    if record is None:
        return JSONResponse({'error': 'could not create the village (blank or duplicate name)'}, status_code=409)
    agents = state.get('agents') or {}
    for aid in (body.get('agentIds') or []):
        if aid in agents:
            _sim.set_agent_village(state, aid, record['id'])
    save_state_to_db(state)
    log_action('player', 'village_created', {'id': record['id'], 'name': name}, authorized=True)
    return JSONResponse({'ok': True, 'village': record})


@app.post('/api/villages/assign')
@_state_writer
async def assign_agent_to_village(request: Request):
    """Move an agent into a village (player-only). The boundary is enforced at
    read time: after this, the agent's wiki + design-taste context come only
    from the target village."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    agent_id = (body.get('agentId') or '').strip()
    village_id = (body.get('villageId') or '').strip()
    import sim as _sim
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if not _sim.set_agent_village(state, agent_id, village_id):
        return JSONResponse({'error': 'unknown agent or village'}, status_code=400)
    save_state_to_db(state)
    log_action('player', 'village_assign', {'agentId': agent_id, 'villageId': village_id}, authorized=True)
    return JSONResponse({'ok': True, 'agentId': agent_id, 'villageId': village_id})


@app.post('/api/design-reference')
async def design_reference(request: Request):
    """Designmind-style design brief from an uploaded image.
    Operator-gated (player session or a director/admin agent -- a human
    or the org's own management handing the tank a file is a different,
    narrower risk than letting a worker choose files). Saves the
    image into library/design-references/, runs the vision tier over it, and
    writes a <name>.txt brief next to it that any agent can read via
    /api/library/file and find via /api/library/search. Returns the brief and
    the library-relative paths."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    # The authenticated operator is the actor; a body agentId claim is
    # honored only when it names the verified requester itself, never an
    # arbitrary agent (an operator cannot forge another agent's action).
    claimed = (body.get('agentId') or '').strip() or None
    if claimed and claimed != actor:
        return JSONResponse({'error': 'design references require the operator itself'}, status_code=403)
    agent_id = claimed or actor
    name = _safe_design_reference_name(body.get('name'))
    image_b64 = (body.get('imageBase64') or '').strip()
    project = _design_project_rel(body.get('project'), village=body.get('village') or 'main')
    if not name:
        return JSONResponse({'error': 'a safe name is required (letters, digits, dashes, underscores)'}, status_code=400)
    if not image_b64:
        return JSONResponse({'error': 'imageBase64 is required'}, status_code=400)
    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception:
        return JSONResponse({'error': 'imageBase64 is not valid base64'}, status_code=400)
    if len(image_bytes) > 12 * 1024 * 1024:
        return JSONResponse({'error': 'image is too large (max 12 MB)'}, status_code=400)
    try:
        design_dir = _safe_library_path(project)
        if not design_dir:
            return JSONResponse({'error': 'invalid design-references path'}, status_code=400)
        os.makedirs(design_dir, exist_ok=True)
    except Exception:
        return JSONResponse({'error': 'could not create the design-references directory'}, status_code=500)
    ext = '.png'
    for magic, candidate in ((b'\x89PNG\r\n\x1a\n', '.png'),
                             (b'\xff\xd8\xff', '.jpg'),
                             (b'RIFF', '.webp')):
        if image_bytes[:len(magic)] == magic:
            ext = candidate
            break
    image_rel = os.path.join(project, f'{name}{ext}')
    image_path = _safe_library_path(image_rel)
    if not image_path:
        return JSONResponse({'error': 'invalid name'}, status_code=400)
    brief = _design_brief_from_image(image_b64, f'uploaded image {name}', context=body.get('context'))
    if not brief.get('ok'):
        return JSONResponse({'error': brief.get('note', 'could not generate the design brief')}, status_code=502)
    header = (f'# Design reference: {name}\n\n'
              f'- Source: uploaded image ({ext.lstrip(".")})\n'
              f'- Project: {project}\n'
              f'- Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n'
              f'- Image file: {image_rel}\n\n')
    try:
        with open(image_path, 'wb') as f:
            f.write(image_bytes)
        brief_rel = os.path.join(project, f'{name}.txt')
        brief_path = _safe_library_path(brief_rel)
        if brief_path:
            _write_file(brief_path, header + brief['brief'] + '\n')
    except Exception as e:
        return JSONResponse({'error': f'could not write the design reference: {e}'}, status_code=500)
    log_action(agent_id, 'design_reference', {'name': name, 'image': image_rel, 'brief': brief_rel, 'project': project})
    return JSONResponse({'ok': True, 'name': name, 'image': image_rel, 'briefPath': brief_rel,
                         'text': header + brief['brief']})



def _photo_review_rel(project):
    """Library-relative directory for player photo reviews. Personal media:
    .gitignore keeps this tree out of the repo, unlike design-references."""
    slug = _safe_design_reference_name(project) or 'general'
    if slug in ('general', 'root', ''):
        return 'photo-reviews'
    return os.path.join('photo-reviews', slug)


def _photo_review_from_image(image_b64, source_label, context='', mode='general'):
    """A grounded text note for one player photo. `mode='design'` reuses the
    design-brief prompt; `general` (the default) describes the photo, reads
    its text, and suggests concrete uses. Never crashes: a missing vision
    tier or failed call returns {ok: False, note}."""
    if mode == 'design':
        return _design_brief_from_image(image_b64, source_label, context=context)
    vision_slug = _vision_tier_slug()
    if not vision_slug:
        return {'ok': False, 'note': 'vision model tier not available'}
    context_block = f'\nContext: {context}' if context else ''
    prompt = (
        VISION_ANTI_INSTRUCTION_GUARD + '\n'
        'You are reviewing one photo the player took and chose to submit. '
        'Produce a concise, concrete text note with these exact sections:\n'
        'SUBJECT: what the photo is of, in one or two sentences.\n'
        'DETAILS: the visible details that matter (objects, people, setting, '
        'colors, condition, quantities). No speculation.\n'
        'TEXT: transcribe every legible piece of text exactly; write "none" '
        'when there is none.\n'
        'IDEAS: two or three concrete ways this could inform the player\'s '
        'projects or thinking, grounded only in what is visible.\n'
        'Only report what is actually visible. If something is unclear or '
        'cut off, say so instead of guessing.'
        f'{context_block}'
    )
    messages = [{'role': 'user', 'content': [
        {'type': 'text', 'text': prompt},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + image_b64}},
    ]}]
    try:
        result = _call_openrouter_sync(vision_slug, messages, 900)
    except Exception as e:
        return {'ok': False, 'note': f'photo review vision call failed: {e}'}
    try:
        reply = (result.get('choices') or [{}])[0].get('message', {}).get('content') or ''
    except Exception:
        reply = ''
    if not reply.strip():
        return {'ok': False, 'note': 'photo review vision call returned no reply'}
    return {'ok': True, 'brief': reply.strip()[:8000]}



def _photo_review_auth(request):
    """Player session or X-Photo-Key matching PHOTO_REVIEW_KEY in .env. Agent
    keys are refused: personal photos are owner-scoped, even for a director."""
    authed, actor = _operator_gate(request)
    if authed and actor == 'player':
        return True, actor
    expected = (_load_env().get('PHOTO_REVIEW_KEY') or '').strip()
    presented = (request.headers.get('X-Photo-Key') or '').strip()
    if expected and presented and secrets.compare_digest(presented, expected):
        return True, 'player'
    return False, None


def _photo_manifest_path():
    return _safe_library_path('photo-reviews/.reviewed.json')


def _load_photo_manifest():
    path = _photo_manifest_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_photo_manifest(data):
    path = _photo_manifest_path()
    if not path:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_file(path, json.dumps(data, indent=1, sort_keys=True) + '\n')
    return True



PHOTO_WATCH_SETTING = 'photo_album_watch'
PHOTO_WATCH_INTERVAL_S = 30
PHOTO_WATCH_BATCH = 3
PHOTO_REVIEW_MAX_ATTEMPTS = 3
PHOTO_REVIEW_RETENTION_DAYS = int(_load_env().get('PHOTO_REVIEW_RETENTION_DAYS', '30') or 30)
_PHOTO_REVIEW_LOCK = threading.Lock()


def _photo_needs_review(manifest, key):
    """True only for assets that still need a vision pass. baseline and done
    entries are final; a failed review retries a bounded number of times; a
    cloud-only asset is re-checked every tick but costs no model call."""
    value = manifest.get(key)
    if value is None:
        return True
    if isinstance(value, dict):
        status = value.get('status')
        if status in ('baseline', 'done'):
            return False
        if status == 'waiting-local':
            return True
        if status == 'error':
            return int(value.get('attempts') or 0) < PHOTO_REVIEW_MAX_ATTEMPTS
        return True
    return False


def _photo_watch_config():
    raw = _get_setting(PHOTO_WATCH_SETTING)
    if not raw:
        return {'enabled': False}
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) else {'enabled': False}
    except Exception:
        return {'enabled': False}


def _photo_save_watch_config(cfg):
    _set_setting(PHOTO_WATCH_SETTING, json.dumps(cfg, sort_keys=True))
    return cfg


def _photo_baseline_album(album):
    """Mark every current asset in the album as seen without reviewing it.
    This is what makes 'only new photos' true for a pre-populated album."""
    assets = _apple_photos.album_assets(album)
    manifest = _load_photo_manifest()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for asset in assets:
        manifest[f'{album}::{asset["uuid"]}'] = {'status': 'baseline', 'at': now}
    _save_photo_manifest(manifest)
    return len(assets)


def _prune_photo_reviews_sync():
    """Delete reviewed images and notes older than the retention window. The
    manifest stays as a dedup record; the personal media does not."""
    root = _safe_library_path('photo-reviews')
    if not root or not os.path.isdir(root):
        return 0
    cutoff = time.time() - max(1, PHOTO_REVIEW_RETENTION_DAYS) * 86400
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith('.') or not name.lower().endswith(
                    ('.jpg', '.jpeg', '.png', '.webp', '.txt')):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    if removed:
        try:
            log_action(None, 'photo_review_pruned',
                       {'removed': removed, 'days': PHOTO_REVIEW_RETENTION_DAYS},
                       authorized=False)
        except Exception:
            pass
    return removed


def _review_album_batch_sync(album, project, mode, context, limit, agent_id,
                             assets=None):
    """Review up to `limit` pending assets from one album. Shared by the
    manual endpoint and the watcher; the lock keeps the two from reviewing the
    same asset twice. Returns a summary dict, never raises for one bad photo."""
    if not _PHOTO_REVIEW_LOCK.acquire(blocking=False):
        return {'ok': False, 'busy': True, 'reviewed': [], 'reviewedCount': 0}
    try:
        assets = assets if assets is not None else _apple_photos.album_assets(album)
        manifest = _load_photo_manifest()
        pending = [a for a in assets
                   if _photo_needs_review(manifest, f'{album}::{a["uuid"]}')]
        if not pending:
            return {'ok': True, 'total': len(assets), 'pending': 0,
                    'reviewed': [], 'reviewedCount': 0}
        rel_dir = _photo_review_rel(project)
        photo_dir = _safe_library_path(rel_dir)
        if not photo_dir:
            return {'ok': False, 'error': 'invalid photo-reviews path',
                    'reviewed': [], 'reviewedCount': 0}
        os.makedirs(photo_dir, exist_ok=True)
        reviewed = []
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for asset in pending[:limit]:
            key = f'{album}::{asset["uuid"]}'
            base_name = f'photo-{asset["uuid"][:8]}-{asset["date"][:10]}'
            if not asset['local']:
                manifest[key] = {'status': 'waiting-local', 'at': now}
                reviewed.append({'name': base_name, 'uuid': asset['uuid'],
                                 'error': 'not downloaded to this Mac yet (iCloud '
                                          'Optimize Storage); open it in Photos or '
                                          'download the original'})
                continue
            tmp_dir = None
            try:
                jpeg_path, tmp_dir = _apple_photos.normalize_to_jpeg(asset['localPath'])
                with open(jpeg_path, 'rb') as fh:
                    image_bytes = fh.read()
                image_b64 = base64.b64encode(image_bytes).decode('ascii')
                result = _photo_review_from_image(
                    image_b64, f'Photos album {album}, {asset["filename"]}',
                    context=context, mode=mode)
                if not result.get('ok'):
                    prior = manifest.get(key)
                    attempts = (int(prior.get('attempts') or 0)
                                if isinstance(prior, dict) else 0) + 1
                    manifest[key] = {'status': 'error', 'attempts': attempts,
                                     'at': now,
                                     'note': (result.get('note') or '')[:300]}
                    reviewed.append({'name': base_name, 'uuid': asset['uuid'],
                                     'error': result.get('note', 'review failed')})
                    continue
                image_rel = os.path.join(rel_dir, f'{base_name}.jpg')
                brief_rel = os.path.join(rel_dir, f'{base_name}.txt')
                image_path = _safe_library_path(image_rel)
                brief_path = _safe_library_path(brief_rel)
                if not image_path or not brief_path:
                    reviewed.append({'name': base_name, 'uuid': asset['uuid'],
                                     'error': 'invalid output path'})
                    continue
                header = (f'# Photo review: {base_name}\n\n'
                          f'- Source: Photos album {album} ({asset["filename"]})\n'
                          f'- Taken: {asset["date"]}\n'
                          f'- Directory: {rel_dir}\n'
                          f'- Mode: {mode}\n'
                          f'- Reviewed: {now}\n'
                          f'- Image file: {image_rel}\n\n')
                with open(image_path, 'wb') as fh:
                    fh.write(image_bytes)
                _write_file(brief_path, header + result['brief'] + '\n')
                manifest[key] = {'status': 'done', 'at': now}
                reviewed.append({'name': base_name, 'uuid': asset['uuid'],
                                 'image': image_rel, 'briefPath': brief_rel,
                                 'text': header + result['brief']})
                log_action(agent_id, 'photo_review_album',
                           {'album': album, 'uuid': asset['uuid'],
                            'brief': brief_rel, 'mode': mode})
            except Exception as e:
                prior = manifest.get(key)
                attempts = (int(prior.get('attempts') or 0)
                            if isinstance(prior, dict) else 0) + 1
                manifest[key] = {'status': 'error', 'attempts': attempts,
                                 'at': now, 'note': str(e)[:300]}
                reviewed.append({'name': base_name, 'uuid': asset['uuid'],
                                 'error': str(e)[:300]})
            finally:
                if tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
        _save_photo_manifest(manifest)
        _prune_photo_reviews_sync()
        done = sum(1 for r in reviewed if not r.get('error'))
        remaining = sum(1 for a in assets
                        if _photo_needs_review(manifest, f'{album}::{a["uuid"]}'))
        return {'ok': True, 'total': len(assets), 'pending': remaining,
                'reviewedCount': done, 'reviewed': reviewed}
    finally:
        _PHOTO_REVIEW_LOCK.release()


def _photo_watch_tick_sync():
    cfg = _photo_watch_config()
    if not cfg.get('enabled') or not cfg.get('album'):
        return None
    try:
        summary = _review_album_batch_sync(
            cfg['album'], cfg.get('project') or 'photos',
            cfg.get('mode') or 'general', cfg.get('context') or '',
            int(cfg.get('limit') or PHOTO_WATCH_BATCH), 'player')
    except Exception as e:
        cfg['lastError'] = str(e)[:300]
        cfg['lastCheckAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _photo_save_watch_config(cfg)
        return None
    cfg['lastCheckAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if summary and summary.get('ok'):
        cfg['pending'] = summary.get('pending', 0)
        if summary.get('reviewedCount'):
            cfg['reviewedCount'] = int(cfg.get('reviewedCount') or 0) + summary['reviewedCount']
            cfg['lastReviewedAt'] = cfg['lastCheckAt']
            cfg.pop('lastError', None)
    _photo_save_watch_config(cfg)
    return summary


async def _photo_album_watch_loop():
    """Poll the dedicated album for photos added since the baseline. Only
    unreviewed assets are analyzed; a ticking run with nothing new costs one
    read-only SQLite query. The loop starts after the first interval so a
    boot does not race the registries."""
    while True:
        await asyncio.sleep(PHOTO_WATCH_INTERVAL_S)
        try:
            await asyncio.to_thread(_photo_watch_tick_sync)
        except Exception as e:
            print(f'[photo-watch] tick failed: {e}', flush=True)


@app.get('/api/photo-review/albums')
async def photo_review_albums(request: Request):
    """Album names available in the Mac Photos library, for the picker."""
    authed, _actor = _photo_review_auth(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        return JSONResponse({'albums': _apple_photos.list_albums()})
    except FileNotFoundError as e:
        return JSONResponse({'error': str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({'error': f'could not read the Photos library: {e}'}, status_code=502)
