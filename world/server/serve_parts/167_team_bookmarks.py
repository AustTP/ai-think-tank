
# Team + global bookmarks (operator direction, 2026-11-01): a small shared
# link library the think tank curates together. Each agent TEAM owns its own
# bookmark list; GLOBAL bookmarks are visible to every team. The player
# seeds and curates globals through the /api/bookmarks endpoints; agents
# reach the same store through the `bookmarks` ask-lane tool, where they can
# read global + their own team's links and add/delete links for teams they
# belong to. Admin agents (the chief of staff tier) may also add and delete
# globals from the tool, because they act as the player's delegate.
#
# Storage is the state blob (`bookmarks` list) -- a bookmarks library stays
# in the dozens, not the thousands, so a row-extracted section would be
# ceremony without a payoff. IDs are minted bm-<max+1> (the max-scan
# pattern: a plain len+1 collides when a delete leaves gaps, the exact bug
# that collapsed five backlog filings into one wrq-218 row).
import json
import re
import time
import urllib.parse

_BOOKMARK_URL_RE = re.compile(r'^https?://\S+$')


def _normalize_bookmark_url(url):
    """Accept a real http(s) URL; return (url, host) or (None, reason)."""
    url = (url or '').strip()
    if not _BOOKMARK_URL_RE.match(url):
        return None, 'url must be a real http(s) link'
    try:
        host = (urllib.parse.urlparse(url).hostname or '').strip().lower()
    except ValueError:
        return None, 'url does not parse'
    if not host or '.' not in host:
        return None, 'url has no usable host'
    return url, host


def _bookmark_next_id(state):
    top = 0
    for bookmark in state.get('bookmarks') or []:
        match = re.fullmatch(r'bm-(\d+)', str(bookmark.get('id') or ''))
        if match:
            top = max(top, int(match.group(1)))
    return f'bm-{top + 1}'


def _agent_team_ids(agent_id, state):
    """Every team the agent belongs to: named member, or the team's
    director/scrum-master row (all three are the team, per the spawn
    contract)."""
    ids = set()
    for team in state.get('teams') or []:
        if agent_id in (team.get('members') or []) \
                or agent_id == team.get('directorId') \
                or agent_id == team.get('scrumMasterId'):
            if team.get('id'):
                ids.add(team['id'])
    return ids


def _agent_is_admin(agent_id, state):
    for record in (state.get('agentRoster') or []):
        if record.get('id') == agent_id and record.get('isAdmin'):
            return True
    return bool((state.get('agents') or {}).get(agent_id, {}).get('isAdmin'))


def _bookmarks_visible(agent_id, state):
    mine = _agent_team_ids(agent_id, state)
    return [b for b in (state.get('bookmarks') or [])
            if b.get('scope') == 'global' or b.get('scope') in mine]


def _add_bookmark(state, url, title, scope, added_by):
    """Append one bookmark to the state list. Returns (bookmark, reason,
    created) -- (record, None, True) for a new entry, the EXISTING record
    with created=False for a same-scope duplicate, or (None, reason, False)
    on a rejection. Callers must only persist/log when created is True: a
    duplicate is a no-op, not a write."""
    normalized, host = _normalize_bookmark_url(url)
    if not normalized:
        return None, host, False
    if scope != 'global' and not any(t.get('id') == scope
                                     for t in (state.get('teams') or [])):
        return None, f'unknown team scope: {scope}', False
    for bookmark in state.get('bookmarks') or []:
        if bookmark.get('scope') == scope \
                and (bookmark.get('url') or '').rstrip('/') == normalized.rstrip('/'):
            return bookmark, None, False  # idempotent: same link, same scope
    record = {'id': _bookmark_next_id(state), 'scope': scope,
              'url': normalized, 'host': host,
              'title': (title or '').strip() or host,
              'addedBy': added_by, 'addedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    state.setdefault('bookmarks', []).append(record)
    return record, None, True


_BOOKMARKS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'bookmarks',
        'description': 'The think tank\'s shared link library. Every team has '
                       'its own bookmarks; GLOBAL bookmarks are visible to all '
                       'teams. Actions: list (your team\'s links plus all '
                       'global ones), add (save a link to your team -- pass '
                       'scope="global" only if you are an admin; duplicate '
                       'links in the same scope return the existing entry '
                       'instead of erroring), delete (remove one of your '
                       'team\'s links by id; admins may delete globals). '
                       'Titles and notes here are shared with the whole '
                       'team -- keep them factual.',
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['list', 'add', 'delete']},
                'url': {'type': 'string', 'description': 'The http(s) link (add).'},
                'title': {'type': 'string', 'description': 'A short human label (add; defaults to the host).'},
                'scope': {'type': 'string', 'description': '"global" or your team id (add); defaults to your single team when you belong to exactly one.'},
                'id': {'type': 'string', 'description': 'The bookmark id (delete).'},
            },
            'required': ['action'],
        },
    },
}


def _bookmarks_tool(agent_id, args):
    """Dispatch a `bookmarks` tool call. Mutations run a fresh state
    transaction (the ask loop executes tools on an unlocked snapshot; saving
    the stale snapshot back would clobber the tick)."""
    args = args or {}
    action = args.get('action') or ''
    if action == 'list':
        state = get_state_from_db() or {}
        visible = _bookmarks_visible(agent_id, state)
        if not visible:
            return 'No bookmarks yet. Ask the player to seed globals, or add one with action=add.'
        lines = []
        for bookmark in visible:
            label = 'global' if bookmark.get('scope') == 'global' \
                else f"team {bookmark.get('scope')}"
            lines.append(f"- [{bookmark.get('id')}] {label}: "
                         f"{bookmark.get('title')} -- {bookmark.get('url')}")
        return 'Bookmarks:\n' + '\n'.join(lines)
    if action == 'add':
        url = args.get('url') or ''
        scope = (args.get('scope') or '').strip()
        state = _state_begin()
        if not state:
            return 'bookmarks failed: state unavailable'
        try:
            if not scope:
                mine = sorted(_agent_team_ids(agent_id, state))
                if len(mine) == 1:
                    scope = mine[0]
                else:
                    return ('bookmarks failed: pass scope -- "global" (admin '
                            f'only) or one of your teams: {", ".join(mine) or "(none)"}.')
            if scope == 'global' and not _agent_is_admin(agent_id, state):
                return 'Denied: only an admin agent can add a global bookmark.'
            if scope != 'global' and scope not in _agent_team_ids(agent_id, state):
                return f'Denied: you are not a member of team {scope}.'
            record, reason, created = _add_bookmark(state, url, args.get('title') or '',
                                                    scope, agent_id)
            if record is None:
                return f'bookmarks failed: {reason}'
            if not created:
                # Same link already in this scope: report it, change nothing.
                return (f"Already saved [{record['id']}] "
                        f"({'global' if record['scope'] == 'global' else 'team ' + record['scope']}): "
                        f"{record['title']} -- {record['url']}")
            save_state_to_db(state)
            log_action(agent_id, 'bookmark_added',
                       {'id': record['id'], 'scope': scope, 'url': record['url']},
                       authorized=True)
            return (f"Saved [{record['id']}] ({'global' if scope == 'global' else 'team ' + scope}): "
                    f"{record['title']} -- {record['url']}")
        finally:
            _state_abort()
    if action == 'delete':
        bookmark_id = (args.get('id') or '').strip()
        state = _state_begin()
        if not state:
            return 'bookmarks failed: state unavailable'
        try:
            found = next((b for b in (state.get('bookmarks') or [])
                          if b.get('id') == bookmark_id), None)
            if not found:
                return f'Unknown bookmark: {bookmark_id or "(none)"}'
            if found.get('scope') == 'global':
                if not _agent_is_admin(agent_id, state):
                    return 'Denied: only an admin agent can delete a global bookmark.'
            elif found.get('scope') not in _agent_team_ids(agent_id, state):
                return f"Denied: bookmark {bookmark_id} belongs to another team."
            state['bookmarks'] = [b for b in state.get('bookmarks') or []
                                  if b.get('id') != bookmark_id]
            save_state_to_db(state)
            log_action(agent_id, 'bookmark_deleted',
                       {'id': bookmark_id, 'scope': found.get('scope')},
                       authorized=True)
            return f'Deleted bookmark {bookmark_id} ({found.get("url")}).'
        finally:
            _state_abort()
    return 'Unknown bookmarks action. Actions: list, add, delete.'


@app.get('/api/bookmarks')
async def list_bookmarks_endpoint(request: Request):
    """Operator-gated read of the whole library (player overview; agents see
    their slice through the bookmarks tool)."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = get_state_from_db() or {}
    return JSONResponse({'bookmarks': state.get('bookmarks') or []})


@app.post('/api/bookmarks')
@_state_writer
async def add_bookmark_endpoint(request: Request):
    """Player-or-admin bookmark add. Body: {url, title?, scope='global'|teamId}."""
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
    # The player session OR an admin agent curates -- exactly the tool's
    # rule; the endpoint must not be stricter than the tool it mirrors.
    if actor != 'player' and not _agent_is_admin(actor, state):
        return JSONResponse({'error': 'only the player or an admin agent can curate bookmarks'}, status_code=403)
    scope = (body.get('scope') or 'global').strip()
    if scope != 'global' and not any(t.get('id') == scope
                                     for t in (state.get('teams') or [])):
        return JSONResponse({'error': f'unknown team scope: {scope}'}, status_code=404)
    record, reason, created = _add_bookmark(state, body.get('url') or '',
                                            body.get('title') or '', scope,
                                            actor or 'player')
    if record is None:
        return JSONResponse({'error': reason}, status_code=400)
    if created:
        save_state_to_db(state)
        log_action('player', 'bookmark_added',
                   {'id': record['id'], 'scope': scope, 'url': record['url']},
                   authorized=True)
    return JSONResponse({'ok': True, 'created': created, 'bookmark': record})


@app.post('/api/bookmarks/delete')
@_state_writer
async def delete_bookmark_endpoint(request: Request):
    """Player-or-admin bookmark delete. Body: {id}."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    bookmark_id = (body.get('id') or '').strip()
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if actor != 'player' and not _agent_is_admin(actor, state):
        return JSONResponse({'error': 'only the player or an admin agent can curate bookmarks'}, status_code=403)
    found = next((b for b in (state.get('bookmarks') or [])
                  if b.get('id') == bookmark_id), None)
    if not found:
        return JSONResponse({'error': f'unknown bookmark: {bookmark_id}'}, status_code=404)
    state['bookmarks'] = [b for b in state.get('bookmarks') or []
                          if b.get('id') != bookmark_id]
    save_state_to_db(state)
    log_action(actor or 'player', 'bookmark_deleted',
               {'id': bookmark_id, 'scope': found.get('scope')}, authorized=True)
    return JSONResponse({'ok': True, 'deleted': bookmark_id})
