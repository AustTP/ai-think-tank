
# Knowledge sources (operator direction, 2026-11-01): the think tank needs
# durable, shared references that outlive any one task --
#
#   sources of truth  canonical documents the player seeds or a team
#                     originates, that agents APPEND to (append-only,
#                     attributed entries; the seed body is never rewritten
#                     by agents). A global truth is readable and appendable
#                     by every team; a team truth is scoped to its team.
#   template repos    starting scaffolds to accumulate knowledge AROUND --
#                     a GitHub repository (like the player's intel forge)
#                     or a player-granted local directory (like the
#                     bug_bounty folder on the Desktop). Templates are
#                     READ-ONLY pointers: a team materializes a copy into
#                     its own workspace and accumulates there, while the
#                     template stays pristine. Global templates are
#                     visible to every team; team templates to their team.
#
# The registry lives in the state blob (`knowledge_sources`), shaped like
# the bookmarks registry: global curation is the player or an admin agent
# (via /api/knowledge, or the knowledge ask tool), team-level curation is
# the team's members, ids mint as ks-<max+1> (the max-scan pattern), and
# mutations run inside a fresh _state_begin transaction. Appends are
# attributed and append-only -- an agent can ADD to a truth, never
# rewrite what the player or another agent wrote.
_KNOWLEDGE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'knowledge',
        'description': "The think tank's knowledge sources: sources of truth "
                       "(canonical documents agents append to) and template "
                       "repositories (starting points to copy into your own "
                       "workspace and build on). Actions: list (yours: global "
                       "+ your team), read <id> (a truth's seed and every "
                       "appended entry, or a template's origin and how to "
                       "materialize it), append <id> <text> (add an attributed "
                       "entry to a source of truth -- appends never rewrite "
                       "existing content).",
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['list', 'read', 'append']},
                'id': {'type': 'string', 'description': 'Source id (ks-N) for read/append.'},
                'text': {'type': 'string', 'description': 'Entry text for append.'},
            },
            'required': ['action'],
        },
    },
}


def _knowledge_next_id(state):
    entries = state.get('knowledge_sources') or []
    top = 0
    for e in entries:
        try:
            top = max(top, int(str(e.get('id', '')).split('-')[-1]))
        except ValueError:
            continue
    return f'ks-{top + 1}'


def _normalize_knowledge_origin(origin, kind, state):
    """Validate the origin the way the matching access layer will read it:
    github origins must be exactly a repo URL (the repo tool's regex),
    local origins must live INSIDE a player-granted file scope, inline
    origins (truth seeds) need text. Returns (origin_dict, error)."""
    origin = origin or {}
    otype = (origin.get('type') or '').strip()
    if otype == 'github':
        url = (origin.get('url') or '').strip()
        if not _GITHUB_REPO_URL_RE.match(url):
            return None, 'github origins must be a https://github.com/<owner>/<repo> url'
        return {'type': 'github', 'url': url}, None
    if otype == 'local':
        path = (origin.get('path') or '').strip()
        if not path:
            return None, 'local origins need a path'
        granted = [g.get('scope') or '' for g in _effective_file_grants(state)]
        real = os.path.realpath(path)
        inside = any(real == os.path.realpath(g) or real.startswith(os.path.realpath(g).rstrip('/') + '/')
                     for g in granted if g)
        if not inside:
            return None, ('local origins must sit inside a player-granted file '
                          'scope (ask the player to grant the folder first)')
        return {'type': 'local', 'path': path}, None
    if otype == 'inline':
        seed = (origin.get('seed') or '').strip()
        if kind == 'truth' and not seed:
            return None, 'inline truths need seed text'
        return {'type': 'inline', 'seed': seed}, None
    return None, "origin.type must be 'github', 'local', or 'inline'"


def _knowledge_visible(entry, team_ids):
    scope = entry.get('scope')
    return scope == 'global' or scope in team_ids


def _knowledge_tool(agent_id, args):
    action = (args or {}).get('action') or ''
    try:
        state = get_state_from_db() or {}
    except Exception as exc:  # noqa: BLE001 -- a state outage is a denial, not a crash
        return f'knowledge failed: state unavailable ({exc})'
    team_ids = _agent_team_ids(agent_id, state)
    entries = [e for e in (state.get('knowledge_sources') or [])
               if _knowledge_visible(e, team_ids)]
    if action == 'list':
        if not entries:
            return ('No knowledge sources yet. The player (or an admin) can '
                    'register sources of truth and template repositories via '
                    'the console or POST /api/knowledge.')
        lines = []
        for e in entries:
            where = 'GLOBAL' if e['scope'] == 'global' else f'team {e["scope"]}'
            origin = e.get('origin') or {}
            detail = origin.get('url') or origin.get('path') or 'inline document'
            if e['kind'] == 'truth':
                n = len(e.get('appends') or [])
                lines.append(f"- [{e['id']}] ({where}) truth: {e['title']} -- "
                             f"{detail} ({n} appended entries)")
            else:
                lines.append(f"- [{e['id']}] ({where}) template: {e['title']} -- {detail}")
        return 'Knowledge sources:\n' + '\n'.join(lines)
    if action == 'read':
        entry = next((e for e in entries if e.get('id') == (args.get('id') or '').strip()), None)
        if not entry:
            return f'knowledge: no source {(args.get("id") or "").strip()!r} visible to you'
        origin = entry.get('origin') or {}
        if entry['kind'] == 'template':
            if origin['type'] == 'github':
                howto = ("Open a read-only working copy with the github_repo tool "
                         f"(action=open, url={origin['url']}), then copy what you "
                         'need into your task workspace and accumulate there -- '
                         'the template itself stays pristine.')
            else:
                howto = ('Browse the granted local folder with the local_file tool '
                         f'(scope={origin["path"]}), copy what you need into your '
                         'task workspace and accumulate there.')
            return (f"[{entry['id']}] template: {entry['title']}\n"
                    f"Origin: {origin.get('url') or origin.get('path')}\n"
                    f"{howto}")
        parts = [f"[{entry['id']}] source of truth: {entry['title']}",
                 '--- seed ---', (origin.get('seed') or '(no inline seed; see origin)')]
        for a in entry.get('appends') or []:
            parts.append(f"--- append by {a.get('by')} at {a.get('at')} ---")
            parts.append(a.get('text') or '')
        return '\n'.join(parts)
    if action == 'append':
        text = (args.get('text') or '').strip()
        entry = next((e for e in entries if e.get('id') == (args.get('id') or '').strip()), None)
        if not text:
            return 'knowledge: append needs text'
        if not entry:
            return f'knowledge: no source {(args.get("id") or "").strip()!r} visible to you'
        if entry['kind'] != 'truth':
            return ('knowledge: templates are read-only pointers -- materialize '
                    'a copy into your workspace and accumulate there')
        # Global truths accept any agent's attributed append; team truths
        # accept their members (the visibility filter already scoped the
        # entries list, but global entries pass it for everyone).
        if entry['scope'] != 'global' and entry['scope'] not in team_ids:
            return 'knowledge: that source belongs to another team'
        new_state = _state_begin()
        if not new_state:
            return 'knowledge failed: state unavailable'
        try:
            live = next((e for e in (new_state.get('knowledge_sources') or [])
                         if e.get('id') == entry['id']), None)
            if not live:
                return 'knowledge failed: source vanished; list again'
            live.setdefault('appends', []).append({
                'by': agent_id,
                'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'text': text[:20000],
            })
            save_state_to_db(new_state)
            log_action(agent_id, 'knowledge_appended',
                       {'id': entry['id'], 'title': entry.get('title')},
                       authorized=True)
            n = len(live['appends'])
            return (f"Appended to [{entry['id']}] {entry['title']} "
                    f"(entry {n}). Read it back with knowledge action=read.")
        finally:
            _state_abort()
    return "knowledge: unknown action -- use list, read, or append"


@app.get('/api/knowledge')
async def list_knowledge_endpoint(request: Request):
    """Operator-gated read of every knowledge source (all scopes)."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    return JSONResponse({'sources': state.get('knowledge_sources') or []})


@app.post('/api/knowledge')
@_state_writer
async def register_knowledge_endpoint(request: Request):
    """Player-or-admin registration of a knowledge source.
    Body: {title, kind: 'truth'|'template', scope: 'global'|teamId,
    origin: {type: 'github'|'local'|'inline', url|path|seed}, notes?}."""
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
    if actor != 'player' and not _agent_is_admin(actor, state):
        return JSONResponse({'error': 'only the player or an admin agent can register sources'}, status_code=403)
    kind = (body.get('kind') or '').strip()
    if kind not in ('truth', 'template'):
        return JSONResponse({'error': "kind must be 'truth' or 'template'"}, status_code=400)
    scope = (body.get('scope') or 'global').strip()
    if scope != 'global' and scope not in {t.get('id') for t in (state.get('teams') or [])}:
        return JSONResponse({'error': f'unknown team scope: {scope}'}, status_code=404)
    origin, err = _normalize_knowledge_origin(body.get('origin') or {}, kind, state)
    if err:
        return JSONResponse({'error': err}, status_code=400)
    entry = {'id': _knowledge_next_id(state), 'kind': kind, 'scope': scope,
             'title': (body.get('title') or '').strip()[:200] or 'untitled',
             'origin': origin, 'addedBy': actor or 'player',
             'addedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    if kind == 'truth':
        entry['appends'] = []
    notes = (body.get('notes') or '').strip()
    if notes:
        entry['notes'] = notes[:2000]
    state.setdefault('knowledge_sources', []).append(entry)
    save_state_to_db(state)
    log_action(actor or 'player', 'knowledge_registered',
               {'id': entry['id'], 'kind': kind, 'scope': scope}, authorized=True)
    return JSONResponse({'ok': True, 'source': entry})


@app.post('/api/knowledge/delete')
@_state_writer
async def delete_knowledge_endpoint(request: Request):
    """Player-or-admin removal. Body: {id}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    source_id = (body.get('id') or '').strip()
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if actor != 'player' and not _agent_is_admin(actor, state):
        return JSONResponse({'error': 'only the player or an admin agent can curate sources'}, status_code=403)
    found = next((e for e in (state.get('knowledge_sources') or [])
                  if e.get('id') == source_id), None)
    if not found:
        return JSONResponse({'error': f'unknown source: {source_id}'}, status_code=404)
    state['knowledge_sources'] = [e for e in (state.get('knowledge_sources') or [])
                                  if e.get('id') != source_id]
    save_state_to_db(state)
    log_action(actor or 'player', 'knowledge_deleted', {'id': source_id}, authorized=True)
    return JSONResponse({'ok': True, 'deleted': source_id})


@app.post('/api/knowledge/append')
@_state_writer
async def append_knowledge_endpoint(request: Request):
    """Append an attributed entry to a truth. The player session or an
    admin appends anywhere; a team member appends to their team's truths.
    Body: {id, text}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    source_id = (body.get('id') or '').strip()
    text = (body.get('text') or '').strip()
    if not text:
        return JSONResponse({'error': 'text is required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    entry = next((e for e in (state.get('knowledge_sources') or [])
                  if e.get('id') == source_id), None)
    if not entry:
        return JSONResponse({'error': f'unknown source: {source_id}'}, status_code=404)
    if entry.get('kind') != 'truth':
        return JSONResponse({'error': 'templates are read-only pointers'}, status_code=400)
    if actor == 'player' or _agent_is_admin(actor, state):
        pass  # the player's delegate may append anywhere
    elif entry.get('scope') == 'global':
        pass  # any authenticated agent may append to a global truth
    elif entry.get('scope') not in _agent_team_ids(actor, state):
        return JSONResponse({'error': 'that source belongs to another team'}, status_code=403)
    state['knowledge_sources'] = state.get('knowledge_sources') or []
    entry.setdefault('appends', []).append({
        'by': actor or 'player',
        'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'text': text[:20000],
    })
    save_state_to_db(state)
    log_action(actor or 'player', 'knowledge_appended', {'id': source_id}, authorized=True)
    return JSONResponse({'ok': True, 'entries': len(entry['appends'])})
