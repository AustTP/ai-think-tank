

def _prune_broken_story_refs(base_dir):
    """Delete loose `story/*` refs that point at missing objects. A killed
    worktree or a half-written branch leaves these behind, and a single bad
    ref makes every `git fetch` into the base fail ("bad object refs/heads/
    story/task-...") -- which is what blocked the base sync. Returns the number
    of refs removed."""
    removed = 0
    # Packed refs first: a bad entry there survives deleting the loose file and
    # keeps breaking every fetch.
    packed = os.path.join(base_dir, '.git', 'packed-refs')
    if os.path.isfile(packed):
        try:
            with open(packed, encoding='utf-8') as stream:
                lines = stream.read().splitlines()
        except OSError:
            lines = []
        kept = []
        for line in lines:
            if line.startswith('#') or line.startswith('^'):
                kept.append(line)
                continue
            parts = line.split(' ', 1)
            if len(parts) != 2:
                kept.append(line)
                continue
            sha, ref = parts
            if ref.startswith('refs/heads/story/') and _run_git_sync(
                    base_dir, ['cat-file', '-e', sha]).returncode != 0:
                removed += 1
                name = ref.rsplit('/', 1)[-1]
                reflog = os.path.join(base_dir, '.git', 'logs', 'refs', 'heads', 'story', name)
                if os.path.exists(reflog):
                    try:
                        os.remove(reflog)
                    except OSError:
                        pass
                continue
            kept.append(line)
        if removed:
            tmp = packed + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as stream:
                stream.write('\n'.join(kept) + '\n')
            os.replace(tmp, packed)
    refs_dir = os.path.join(base_dir, '.git', 'refs', 'heads', 'story')
    if not os.path.isdir(refs_dir):
        return removed
    for name in os.listdir(refs_dir):
        path = os.path.join(refs_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8') as stream:
                sha = stream.read().strip()
        except OSError:
            continue
        if not sha:
            continue
        if _run_git_sync(base_dir, ['cat-file', '-e', sha]).returncode == 0:
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        reflog = os.path.join(base_dir, '.git', 'logs', 'refs', 'heads', 'story', name)
        if os.path.exists(reflog):
            try:
                os.remove(reflog)
            except OSError:
                pass
        removed += 1
    return removed


def _sync_base_workroom_to_live(base_dir):
    """Bring the shared sandbox base repo up to the LIVE project's committed
    HEAD before a NEW story branch is cut.

    Why: story worktrees branch from this base, and the base was seeded once and
    then drifted. A story could start from a months-old tree (the live incident:
    a 67-line page presented as an expansion of a 561-line page) and could carry
    unrelated junk files from prior tasks. Syncing on new-story creation makes
    every new story start from the code the project actually has.

    Safe by construction: the base is snapshotted into a tagged commit first, so
    nothing is lost; an existing story branch is never touched (resume keeps its
    own tree); and a failure to sync simply leaves the old base in place.
    Returns True when the base matches the live HEAD afterwards."""
    if not config.ALLOW_WORKSPACE_SYNC:
        return False
    _prune_broken_story_refs(base_dir)
    live_head = _live_repo_head()
    if not live_head:
        return False
    current = _run_git_sync(base_dir, ['rev-parse', 'HEAD'])
    if current.returncode == 0 and (current.stdout or '').strip() == live_head:
        return True
    # Snapshot whatever the base has now (tracked + untracked) so the old tree
    # survives in history even after the reset.
    _run_git_sync(base_dir, ['add', '-A'])
    _run_git_sync(base_dir, ['commit', '-q', '-m',
                             'snapshot before syncing base workroom to live HEAD'])
    _run_git_sync(base_dir, ['tag', '-f', f'pre-sync-{int(time.time())}', 'HEAD'])
    branch_done = _run_git_sync(THINK_TANK_DIR, ['rev-parse', '--abbrev-ref', 'HEAD'])
    branch = (branch_done.stdout or '').strip() or 'main'
    fetch = _run_git_sync(base_dir, ['fetch', THINK_TANK_DIR, branch])
    if fetch.returncode != 0:
        return False
    reset = _run_git_sync(base_dir, ['reset', '--hard', 'FETCH_HEAD'])
    if reset.returncode != 0:
        _run_git_sync(base_dir, ['reset', '--hard', 'HEAD'])
        return False
    # Drop untracked junk from prior tasks (the old tree is in the snapshot
    # commit above, so nothing is lost). This is what kept stale `_fix_*.py`
    # files and old test artifacts in every new story.
    _run_git_sync(base_dir, ['clean', '-fdx'])
    return _live_repo_head() == live_head


def ensure_story_worktree(sandbox_id, base_sandbox_id=None):
    """Create (idempotently) the story worktree `sandbox_id` as a linked
    worktree of `base_sandbox_id` on branch `story/<task-id>`, branching from
    the base repo's current main. Returns the absolute worktree path.

    Safe to call on every coding/review/fix pass for the same story: the
    second call finds the linked worktree and returns it unchanged, so a fix
    cycle and its reviews keep working in the SAME tree the author used.
    Falls back to raising ValueError on a bad id; the caller decides whether
    to degrade to the shared sandbox."""
    base_dir = _sandbox_dir_for(base_sandbox_id or _base_sandbox_for_story(sandbox_id))
    _ensure_sandbox_git_repo(base_dir)
    if not _git_identity_configured(base_dir):
        _run_git_sync(base_dir, ['config', 'user.email', 'sandbox-backup@ai-think-tank.local'])
        _run_git_sync(base_dir, ['config', 'user.name', 'AI Think Tank sandbox backup'])
    # The base must have at least one commit for worktree/branch to point at.
    if _run_git_sync(base_dir, ['rev-parse', '--verify', 'HEAD']).returncode != 0:
        _run_git_sync(base_dir, ['add', '-A'])
        _run_git_sync(base_dir, ['commit', '--allow-empty', '-q', '-m', 'seed workroom repo for story worktrees'])
    worktree_dir = os.path.join(SANDBOXES_DIR, sandbox_id)
    branch = _story_branch_name(sandbox_id)
    if _worktree_is_linked(worktree_dir, base_dir):
        return worktree_dir
    # An existing but unlinked dir (e.g. a pre-worktree story sandbox, or a
    # half-created one): refuse rather than clobber possible real work.
    if os.path.isdir(worktree_dir) and os.listdir(worktree_dir) and not os.path.exists(os.path.join(worktree_dir, '.git')):
        raise ValueError(f'story sandbox dir exists but is not a linked worktree: {worktree_dir}')
    os.makedirs(os.path.dirname(worktree_dir), exist_ok=True)
    # A NEW story starts from the live project's committed HEAD. An existing
    # story branch is a resume and keeps its own tree untouched.
    branch_exists = _run_git_sync(base_dir, ['rev-parse', '--verify', branch]).returncode == 0
    if not branch_exists:
        _sync_base_workroom_to_live(base_dir)
    # Branch from the base's current HEAD so the story starts with the shared
    # workroom's existing files (not an empty tree). If the branch already
    # exists (story resumed), attach a new worktree to it instead of -b.
    if branch_exists:
        result = _run_git_sync(base_dir, ['worktree', 'add', worktree_dir, branch], )
    else:
        result = _run_git_sync(base_dir, ['worktree', 'add', '-b', branch, worktree_dir, 'HEAD'])
    if result.returncode != 0:
        # 2026-10-30 Tier-2: a crashed story run can leave the dir deleted
        # while its worktree registration survives ("is a missing but already
        # registered worktree; use add -f to override"). That stale
        # registration then poisons EVERY retry of the task. Prune and retry
        # once before giving up.
        retry = _run_git_sync(base_dir, ['worktree', 'prune'])
        if retry.returncode == 0:
            if branch_exists:
                result = _run_git_sync(base_dir, ['worktree', 'add', worktree_dir, branch])
            else:
                result = _run_git_sync(base_dir, ['worktree', 'add', '-b', branch, worktree_dir, 'HEAD'])
    if result.returncode != 0:
        raise ValueError(f'failed to create story worktree {sandbox_id}: {(result.stderr or result.stdout or "").strip()}')
    if not _git_identity_configured(worktree_dir):
        _run_git_sync(worktree_dir, ['config', 'user.email', 'sandbox-backup@ai-think-tank.local'])
        _run_git_sync(worktree_dir, ['config', 'user.name', 'AI Think Tank sandbox backup'])
    return worktree_dir


def _merge_story_branch(sandbox_id, base_sandbox_id=None):
    """Merge a story branch back into the base workroom's current branch.
    Auto-resolves textual conflicts in favor of the story branch (the story's
    reviewed work wins) and commits the merge; if git still fails, leaves the
    branch unmerged and reports the reason so the caller can escalate."""
    story_id = _story_id_from_sandbox(sandbox_id)
    if not story_id:
        return {'ok': False, 'storyId': None, 'reason': f'not a story sandbox id: {sandbox_id!r}'}
    branch = _story_branch_name(sandbox_id)
    base_dir = _sandbox_dir_for(base_sandbox_id or _base_sandbox_for_story(sandbox_id))
    if not os.path.exists(os.path.join(base_dir, '.git')):
        return {'ok': False, 'storyId': story_id, 'reason': f'base repo missing: {base_dir}'}
    if _run_git_sync(base_dir, ['rev-parse', '--verify', branch]).returncode != 0:
        return {'ok': False, 'storyId': story_id, 'reason': f'branch {branch} does not exist in {base_dir}'}
    if _git_identity_configured(base_dir) is False:
        _run_git_sync(base_dir, ['config', 'user.email', 'sandbox-backup@ai-think-tank.local'])
        _run_git_sync(base_dir, ['config', 'user.name', 'AI Think Tank sandbox backup'])
    # Commit any uncommitted base-tree work first so the merge doesn't refuse
    # on a dirty tree and nothing real is lost.
    _run_git_sync(base_dir, ['add', '-A'])
    _run_git_sync(base_dir, ['commit', '-q', '-m', f'snapshot before merging {branch}'])
    merge = _run_git_sync(base_dir, ['merge', '--no-ff', '--no-edit', '-m',
                                     f'merge {branch} (peer gate satisfied for task-{story_id})', branch])
    if merge.returncode != 0:
        # Real conflict: take the story side for every conflicted path, then
        # complete the merge. The story branch is the reviewed deliverable;
        # the base side is whatever else was in the shared workroom.
        _run_git_sync(base_dir, ['checkout', '--theirs', '--', '.'])
        _run_git_sync(base_dir, ['add', '-A'])
        finish = _run_git_sync(base_dir, ['commit', '-q', '--no-edit'])
        if finish.returncode != 0:
            _run_git_sync(base_dir, ['merge', '--abort'])
            return {'ok': False, 'storyId': story_id, 'conflict': True,
                    'reason': (merge.stderr or merge.stdout or 'merge conflict').strip()}
    return {'ok': True, 'storyId': story_id, 'branch': branch, 'base': base_dir,
            'worktree': os.path.join(SANDBOXES_DIR, sandbox_id)}


@app.get('/api/keys/credentials')
async def list_credentials(request: Request):
    # Session-protected + player-only (agents never get to enumerate the vault).
    if not _require_player_session(request):
        return JSONResponse({'error': 'credential vault is player-only'}, status_code=403)
    return JSONResponse({'credentials': _list_credentials()})


@app.post('/api/keys/credentials')
async def add_credential(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'credential vault is player-only'}, status_code=403)
    body = await request.json()
    name = (body.get('name') or '').strip()
    service = (body.get('service') or name).strip()
    if not name:
        return JSONResponse({'error': 'name is required'}, status_code=400)
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        return JSONResponse({'error': 'name must be [a-zA-Z0-9_-] (a slug)'}, status_code=400)
    # Two generic kinds: 'token' (an API key, the legacy single-value shape) and
    # 'login' (username + password for an interactive account).
    kind = (body.get('kind') or 'token').strip().lower()
    if kind == 'login':
        username = (body.get('username') or '').strip()
        password = body.get('password')
        if not username or not password:
            return JSONResponse({'error': 'username and password are required for login credentials'}, status_code=400)
        payload = {'kind': 'login', 'username': username, 'password': password}
    else:
        value = body.get('value') or body.get('token')
        if not value:
            return JSONResponse({'error': 'value (the token) is required'}, status_code=400)
        payload = {'kind': 'token', 'token': value}
    _store_credential(name, service, payload, status='active', proposed_by='player')
    log_action('player', 'credential_stored', {'name': name, 'service': service, 'kind': payload['kind']}, authorized=True)
    _append_passport_decision('credential_stored', 'player', {'name': name, 'service': service, 'kind': payload['kind']})
    return JSONResponse({'ok': True, 'credential': {'name': name, 'service': service, 'kind': payload['kind']}})


@app.delete('/api/keys/credentials/{name}')
async def delete_credential(name: str, request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'credential vault is player-only'}, status_code=403)
    _delete_credential(name)
    log_action('player', 'credential_deleted', {'name': name}, authorized=True)
    _append_passport_decision('credential_deleted', 'player', {'name': name})
    return JSONResponse({'ok': True})


@app.get('/api/credentials')
async def credential_list(request: Request):
    """Operator management view of the credential vault: name, service, kind,
    status, and a MASKED preview per entry -- never a full value. Gated to a
    player session or a director/admin (agents never enumerate the vault)."""
    authorized, _actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'credential vault is player/director/admin-only'}, status_code=401)
    entries = []
    for cred in _list_credentials():
        entry = dict(cred)
        entry['preview'] = _mask_credential_preview(_credential_payload(cred['name']) or {})
        entries.append(entry)
    return JSONResponse({'credentials': entries})


@app.post('/api/credentials/propose')
async def credential_propose(request: Request):
    """Agent-facing proposal of a new credential (API token or username +
    password). The value is encrypted at rest the moment it lands, and the row
    is status='proposed' -- unusable until a director/admin or the player
    approves it via /api/credentials/approve. The full runtime story: an agent
    can register a new account's credentials with no restart, and approval is
    the gate that keeps the vault player/director-controlled."""
    body = await request.json()
    agent_id = (body.get('agentId') or 'player').strip() or 'player'
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'credential-propose'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if not (verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is True
            or _require_player_session(request)):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    name, service, payload, error = _validate_credential_body(body)
    if error:
        return JSONResponse({'error': error}, status_code=400)
    existing = _feed_registry_status('external_credentials', name, pk_col='name')
    if existing == 'active':
        return JSONResponse({'error': f'a credential named "{name}" is already active'}, status_code=409)
    if existing == 'proposed':
        return JSONResponse({'error': f'a proposal for credential "{name}" is already pending review'}, status_code=409)
    _store_credential(name, service, payload, status='proposed', proposed_by=agent_id)
    log_action(agent_id, 'credential_propose', {'name': name, 'service': service,
                                                'kind': payload['kind']})
    return JSONResponse({'requested': True, 'credential': {'name': name, 'service': service,
                                                           'kind': payload['kind']},
                         'message': f'Proposal for credential "{name}" stored encrypted. A director/admin '
                                    'or the player approves or denies it via /api/credentials/approve.'})


@app.post('/api/credentials/approve')
async def credential_approve(request: Request):
    """Director/admin/player approval or denial of a proposed credential. The
    stored value is decrypted, re-validated, and returned to the approver only
    as a masked preview. An approved credential becomes mintable immediately --
    no restart."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a director, the admin, or the player may approve credentials'}, status_code=403)
    body = await request.json()
    name = (body.get('name') or '').strip()
    decision = (body.get('decision') or 'approve').strip().lower()
    if not name:
        return JSONResponse({'error': 'name is required'}, status_code=400)
    existing = _feed_registry_status('external_credentials', name, pk_col='name')
    if existing is None:
        return JSONResponse({'error': f'no credential row "{name}"'}, status_code=404)
    if existing == 'active':
        return JSONResponse({'error': f'credential "{name}" is already active'}, status_code=409)
    payload = _credential_payload(name)
    if payload is None:
        return JSONResponse({'error': 'stored credential is unreadable'}, status_code=400)
    with _db() as conn:
        row = conn.execute('SELECT service FROM external_credentials WHERE name = ?', (name,)).fetchone()
    service = row[0] if row else name
    # Re-validate the stored payload at approval time (never trust a forged or
    # hand-edited row to bypass the validator).
    _n, _s, normalized, error = _validate_credential_body(
        {'name': name, 'service': service, 'kind': payload['kind'], **payload})
    if error:
        return JSONResponse({'error': f'proposal no longer validates: {error}'}, status_code=400)
    status = 'active' if decision != 'deny' else 'denied'
    _store_credential(name, service, normalized, status=status, proposed_by=actor)
    log_action(actor, 'credential_approve', {'name': name, 'service': service,
                                             'kind': normalized['kind'], 'status': status})
    return JSONResponse({'ok': True, 'credential': {'name': name, 'service': service,
                                                    'kind': normalized['kind'],
                                                    'preview': _mask_credential_preview(normalized)}})


@app.post('/api/credentials/remove')
async def credential_remove(request: Request):
    """Director/admin/player removal of a credential. Marks the row 'removed'
    (unusable, un-mintable) and voids every outstanding handle to it."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a director, the admin, or the player may remove credentials'}, status_code=403)
    body = await request.json()
    name = (body.get('name') or '').strip()
    if not name:
        return JSONResponse({'error': 'name is required'}, status_code=400)
    if _feed_registry_status('external_credentials', name, pk_col='name') is None:
        return JSONResponse({'error': f'no credential row "{name}"'}, status_code=404)
    with _db() as conn:
        conn.execute('DELETE FROM capability_handles WHERE credential_name = ?', (name,))
        conn.execute('UPDATE external_credentials SET status = ?, updated_at = ? WHERE name = ?',
                     ('removed', time.time(), name))
    log_action(actor, 'credential_remove', {'name': name})
    return JSONResponse({'ok': True, 'name': name, 'status': 'removed'})


@app.post('/api/credentials/login')
async def credential_login_action(request: Request):
    """Perform ONE scoped action using a login credential, capability-gated.

    The agent presents a capability handle minted against a login-kind
    credential (username + password). The server resolves the handle against
    the ACTION url (right agent, purpose, host scope, method scope, TTL),
    verifies both the action URL and the adapter's declared login URL are
    public https (SSRF), then dispatches to the service's login adapter in a
    worker thread. The adapter runs the interactive login and the single
    action, keeps the session in-process, and its result is scrubbed of
    secret-shaped keys before it is returned. The password never appears in
    logs or responses.

    Adapters are added as code when a real account exists: register an entry
    in _LOGIN_ADAPTERS[service] = {'login_url': ..., 'fn': ...}. No adapter
    yet returns 501 with the exact shape to fill in.
    """
    body = await request.json()
    agent_id = (body.get('agentId') or '').strip()
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'credential-login'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if not verify_agent_key(agent_id, request.headers.get('X-Agent-Key')):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    capability_handle = (body.get('capabilityHandle') or '').strip()
    service = (body.get('service') or '').strip().lower()
    action = body.get('action') or {}
    method = (action.get('method') or 'GET').upper()
    url = (action.get('url') or '').strip()
    action_body = action.get('body')
    if not capability_handle or not service or not url:
        return JSONResponse({'error': 'capabilityHandle, service, and action.url are required'}, status_code=400)
    # Resolving the handle against the action URL enforces grantee, purpose,
    # host scope, method scope, and TTL in one step.
    grant = resolve_capability_handle(agent_id, capability_handle, method, url)
    if grant is None:
        log_action(agent_id, 'credential_login', {'service': service, 'action': f'{method} {url}'[:200],
                                                  'decision': 'blocked', 'reason': 'handle invalid, expired, or out of scope'}, authorized=True)
        return JSONResponse({'error': 'That capability handle is not valid for this action (expired, wrong agent, or out-of-scope host/method).'}, status_code=403)
    if grant.get('kind') != 'login':
        return JSONResponse({'error': 'This handle is for a token credential. Use api_call for header-injected tokens.'}, status_code=400)
    if not _login_safe_url(url):
        return JSONResponse({'error': 'action URL must be a public https URL'}, status_code=400)
    adapter = _LOGIN_ADAPTERS.get(service)
    if not adapter:
        return JSONResponse({'error': f'No login adapter is registered for service "{service}". '
                                      'Adapters are added as code once an account exists: '
                                      f'_LOGIN_ADAPTERS["{service}"] = {{"login_url": "...", "fn": callable}}'},
                             status_code=501)
    login_url = adapter.get('login_url') or ''
    if not _login_safe_url(login_url):
        return JSONResponse({'error': f'adapter for "{service}" declares a non-public login_url; refusing'}, status_code=500)
    fn = adapter.get('fn')
    if not callable(fn):
        return JSONResponse({'error': f'adapter for "{service}" has no callable fn'}, status_code=500)
    try:
        raw = await asyncio.to_thread(fn, grant, {'method': method, 'url': url, 'body': action_body})
    except Exception as e:  # noqa: BLE001 -- an adapter failure must surface as a clean API error
        log_action(agent_id, 'credential_login', {'credential': grant['credential_name'],
                                                  'service': grant['service'], 'scope': grant['purpose'],
                                                  'action': f'{method} {url}'[:200], 'ok': False,
                                                  'error': str(e)[:200]}, authorized=True)
        return JSONResponse({'ok': False, 'error': f'login adapter failed: {e}'}, status_code=502)
    if not isinstance(raw, dict):
        return JSONResponse({'ok': False, 'error': 'adapter returned a non-dict result'}, status_code=502)
    ok = bool(raw.get('ok', True))
    status = raw.get('status', 200 if ok else 500)
    result = _login_scrub_result(raw)
    log_action(agent_id, 'credential_login', {'credential': grant['credential_name'],
                                              'service': grant['service'], 'scope': grant['purpose'],
                                              'action': f'{method} {url}'[:200], 'ok': ok}, authorized=True)
    _append_passport_decision('credential_login', agent_id, {
        'credential': grant['credential_name'], 'service': grant['service'],
        'scope': grant['purpose'], 'action': f'{method} {url}'[:200], 'ok': ok})
    return JSONResponse({'ok': ok, 'status': status, 'result': result})


@app.post('/api/download')
async def download_file(request: Request):
    """Scoped file download into the agent's own filespace, under a hard size
    cap. The URL must be public https (SSRF); the stream aborts and deletes
    the partial file the instant the cap is crossed; the filename is
    sanitized so it cannot escape the agent's directory; nothing downloaded
    is ever executed. An optional capability handle attaches a token
    credential and is scope-checked against the URL like api_call. The URL is
    also judged by the work-context-aware Jev gate (same as /api/browse):
    allowlist/player-named hosts skip it, everything else gets the quorum
    classify + escalate. `workContext` / `workContextTrusted` mirror the
    browse endpoint's contract."""
    body = await request.json()
    agent_id = (body.get('agentId') or '').strip()
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'download'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    if not authorized:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    url = (body.get('url') or '').strip()
    filename = (body.get('filename') or '').strip()
    purpose = (body.get('purpose') or '').strip()
    if not url:
        return JSONResponse({'error': 'url is required'}, status_code=400)
    if not _login_safe_url(url):
        return JSONResponse({'error': 'url must be a public https URL'}, status_code=400)
    try:
        max_bytes = int(body.get('maxBytes') or MAX_DOWNLOAD_BYTES)
    except (TypeError, ValueError):
        max_bytes = MAX_DOWNLOAD_BYTES
    max_bytes = max(1024, min(max_bytes, MAX_DOWNLOAD_BYTES))
    headers: dict = {}
    # Optional capability handle: attach a token credential and enforce the
    # handle's host/method scope against this exact URL.
    capability_handle = (body.get('capabilityHandle') or '').strip()
    if capability_handle:
        grant = resolve_capability_handle(agent_id, capability_handle, 'GET', url)
        if grant is None:
            log_action(agent_id, 'download', {'url': url[:200], 'purpose': purpose,
                                              'decision': 'blocked', 'reason': 'handle invalid, expired, or out of scope'}, authorized=True)
            return JSONResponse({'error': 'That capability handle is not valid for this download (expired, wrong agent, or out-of-scope host/method).'}, status_code=403)
        if grant.get('kind') == 'login':
            return JSONResponse({'error': 'This handle is for a login credential, which cannot be attached to a download.'}, status_code=400)
        for hdr_name, hdr_value in _capability_auth_headers(grant['credential_name'], grant['secret']).items():
            headers.setdefault(hdr_name, hdr_value)
    # Content/purpose judgment, work-context aware -- same gate as /api/browse.
    # A player-vetted host (allowlist, or named in a PLAYER-authored task's
    # work) skips Jev entirely; everything else gets the quorum classify +
    # escalate. NEVER constraints ride inside work_context (built NEVER-FIRST
    # by the executors -- see content._build_work_context), so they survive the
    # 600-char slice and read as hard prohibitions. Block categories always win.
    parsed = urllib.parse.urlparse(url)
    work_context = (body.get('workContext') or '').strip()
    work_context_trusted = _work_trust_from_body(body)
    trace_id = None
    if _is_allowlisted_host(parsed.hostname) or (work_context_trusted and _is_work_context_host(work_context, parsed.hostname)):
        log_action(agent_id, 'download', {'url': url[:200], 'purpose': purpose,
                                          'decision': 'allowed_by_allowlist', 'workContext': bool(work_context)}, authorized=True)
    else:
        criteria = {
            'allow': 'The URL and stated purpose look like ordinary, legitimate file downloads (public data files, reports, reference material, raw datasets).'
                     + (' A URL that is clearly part of, or relevant to, the agent\'s CURRENT assigned work is allowed, provided it does not fall into any blocked category.' if work_context else ''),
            'block': 'The URL, domain, or stated purpose suggests: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '. A blocked category ALWAYS blocks: relevance to the assigned work or a stated purpose never overrides it -- an agent can describe its work however it likes, and that must not launder an inappropriate download.',
        }
        instructions = f'An in-game agent wants to download a file from this URL: {url}\nStated reason: {purpose or "not given"}\n'
        if work_context:
            instructions += f'The agent is currently working on this assigned task: {work_context[:600]}\n'
        instructions += 'Decide allow or block based on the URL/domain, the stated purpose, and the work the download serves (the file has not been fetched yet). If the URL falls into any blocked category, you MUST block it regardless of work relevance or stated purpose.'
        decision, confidence, cost, trace_id = await _jev_quorum_decision(instructions, criteria)
        if not _jev_safety_gate(agent_id, 'download', 'This file', url, purpose, decision, confidence, cost, authorized, trace_id):
            return JSONResponse({'error': 'This file was not approved for a think tank agent to download.'}, status_code=403)
    safe_name = _sanitize_filename(filename or parsed.path.rsplit('/', 1)[-1])
    dest_dir = os.path.join(AGENTS_DIR, agent_id, 'downloads')
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, safe_name)
    try:
        result = await asyncio.to_thread(_download_stream_sync, url, headers, dest_path, max_bytes)
    except Exception as e:  # noqa: BLE001 -- a failed download must surface as a clean API error
        log_action(agent_id, 'download', {'url': url[:200], 'purpose': purpose,
                                          'decision': 'failed', 'reason': str(e)[:200]}, authorized=True)
        return JSONResponse({'error': f'download failed: {e}'}, status_code=400)
    try:
        with open(dest_path, 'rb') as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        sha256 = None
    rel = f"downloads/{safe_name}"
    log_action(agent_id, 'download', {'url': url[:200], 'purpose': purpose, 'bytes': result['bytes'],
                                      'path': rel, 'decision': 'allowed'}, authorized=True)
    _append_passport_decision('download', agent_id, {'url': url[:200], 'purpose': purpose,
                                                     'bytes': result['bytes'], 'path': rel})
    return JSONResponse({'ok': True, 'path': rel, 'bytes': result['bytes'],
                         'sha256': sha256, 'finalUrl': result['finalUrl'],
                         'status': result['status']})


@app.post('/api/sms/inbound')
async def sms_inbound(request: Request):
    """Receive-only SMS webhook. The ONLY way a text enters the inbox: a
    forwarder (Twilio/Android-forwarder/carrier) POSTs {from, body} with the
    shared secret. Nothing here can send a text. Fail-closed when no secret
    is configured or the header is wrong."""
    secret = _load_env().get('SMS_FORWARD_SECRET') or ''
    if not secret:
        return JSONResponse({'error': 'SMS inbound is not configured (set SMS_FORWARD_SECRET in .env)'}, status_code=501)
    presented = (request.headers.get('X-SMS-Forward-Secret') or '').strip()
    if not secrets.compare_digest(presented, secret):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    sender = (body.get('from') or '').strip()[:40]
    text = body.get('body')
    if not sender or not isinstance(text, str) or not text.strip():
        return JSONResponse({'error': 'from and body are required'}, status_code=400)
    mid, otp = _sms_ingest(sender, text.strip())
    print(f'[sms] inbound from {sender} ({len(text.strip())} chars, otp={"yes" if otp else "no"}) -> #{mid}', flush=True)
    return JSONResponse({'ok': True, 'messageId': mid})


@app.get('/api/sms/inbox')
async def sms_inbox(request: Request):
    """Operator view of the inbox (player or director/admin). OTP codes are
    masked by default; pass ?otp=1 to see them. Agents never reach this."""
    authorized, _actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'SMS inbox is player/director/admin-only'}, status_code=401)
    try:
        limit = max(1, min(int(request.query_params.get('limit') or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    show_otp = (request.query_params.get('otp') or '0') == '1'
    with _db() as conn:
        rows = conn.execute(
            'SELECT id, sender, body, otp_code, received_at, consumed_at '
            'FROM sms_messages ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    msgs = []
    for r in rows:
        m = _sms_message_row(r)
        if not show_otp and m['otpCode']:
            m['body'] = _sms_mask_otp(m['body'], m['otpCode'])
            m['otpCode'] = None
        msgs.append(m)
    return JSONResponse({'messages': msgs})


@app.post('/api/sms/grant')
async def sms_grant(request: Request):
    """Director/admin/player mints a single-use, short-lived read grant for ONE
    agent + ONE message. otpAllowed defaults to false: unless the operator
    explicitly checks it, the agent reads the text with any code masked.
    Every SMS read is one ask, nothing more."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a director, the admin, or the player may grant an SMS read'}, status_code=403)
    body = await request.json()
    agent_id = (body.get('agentId') or '').strip()
    try:
        message_id = int(body.get('messageId') or 0)
    except (TypeError, ValueError):
        message_id = 0
    otp_allowed = bool(body.get('otpAllowed'))
    if not agent_id or message_id <= 0:
        return JSONResponse({'error': 'agentId and messageId are required'}, status_code=400)
    try:
        ttl_s = max(SMS_TTL_MIN_S, min(int(body.get('ttlSec') or SMS_TTL_DEFAULT_S), SMS_TTL_MAX_S))
    except (TypeError, ValueError):
        ttl_s = 300
    with _db() as conn:
        exists = conn.execute('SELECT 1 FROM sms_messages WHERE id = ?', (message_id,)).fetchone()
    if not exists:
        return JSONResponse({'error': f'no SMS message #{message_id}'}, status_code=404)
    gid = _sms_mint_grant(agent_id, message_id, actor, otp_allowed, ttl_s)
    log_action(actor, 'sms_grant', {'agentId': agent_id, 'messageId': message_id,
                                    'otpAllowed': otp_allowed, 'ttlSec': ttl_s})
    _append_passport_decision('sms_grant', actor, {'agentId': agent_id, 'messageId': message_id,
                                                   'otpAllowed': otp_allowed})
    return JSONResponse({'ok': True, 'grant': gid, 'expiresInSec': ttl_s})


@app.post('/api/sms/read')
async def sms_read(request: Request):
    """The agent consumes its grant: ONE message, exactly once. The text is
    returned with any OTP code masked unless the grant explicitly allowed it."""
    body = await request.json()
    agent_id = (body.get('agentId') or '').strip()
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': 'sms-read'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    if not verify_agent_key(agent_id, request.headers.get('X-Agent-Key')):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    grant_id = (body.get('grant') or '').strip()
    if not grant_id:
        return JSONResponse({'error': 'grant is required'}, status_code=400)
    msg, otp_allowed = _sms_consume_grant(agent_id, grant_id)
    if msg is None:
        return JSONResponse({'error': 'invalid, expired, or already-used read grant'}, status_code=403)
    result = {'id': msg['id'], 'sender': msg['sender'], 'receivedAt': msg['receivedAt']}
    if msg['otpCode'] and not otp_allowed:
        result['body'] = _sms_mask_otp(msg['body'], msg['otpCode'])
        result['otpMasked'] = True
    else:
        result['body'] = msg['body']
        if msg['otpCode']:
            result['otpCode'] = msg['otpCode']
    log_action(agent_id, 'sms_read', {'messageId': msg['id'], 'otp': 'yes' if msg['otpCode'] else 'no',
                                      'otpMasked': bool(msg['otpCode'] and not otp_allowed)})
    return JSONResponse({'ok': True, 'message': result})


@app.post('/api/sms/revoke')
async def sms_revoke(request: Request):
    """Operator revokes an outstanding read grant before it is used."""
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a director, the admin, or the player may revoke an SMS read grant'}, status_code=403)
    body = await request.json()
    grant_id = (body.get('grant') or '').strip()
    if not grant_id:
        return JSONResponse({'error': 'grant is required'}, status_code=400)
    with _db() as conn:
        conn.execute('DELETE FROM sms_read_grants WHERE grant_id = ?', (grant_id,))
    log_action(actor, 'sms_revoke', {'grant': grant_id[:8]})
    return JSONResponse({'ok': True})


# --- Tank-owned account identities (TOTP) -----------------------------------
# The tank creates its own accounts, so it captures the TOTP secret at signup
# instead of importing a phone authenticator. The secret lives in the Fernet
# credential vault. Agents redeem a single-use owner grant for one code; the
# secret never leaves the server and the code is never logged.

@app.post('/api/tank-accounts')
async def tank_account_create(request: Request):
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a player, director, or admin may create a tank account'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be an object'}, status_code=400)
    try:
        rec = tank_accounts.create(
            service=body.get('service') or '',
            account=body.get('account') or '',
            username=body.get('username') or '',
            password=body.get('password') or '',
            totp_secret=body.get('totpSecret') or body.get('otpauthUri') or '',
            recovery_codes=body.get('recoveryCodes'),
            owner=actor,
            status=body.get('status') or 'active',
        )
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({'error': str(e)}, status_code=503)
    return JSONResponse(rec)


@app.get('/api/tank-accounts')
async def tank_account_list(request: Request):
    authorized, _actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a player, director, or admin may list tank accounts'}, status_code=403)
    return JSONResponse({'accounts': tank_accounts.list_accounts()})


@app.get('/api/tank-accounts/{account_id}')
async def tank_account_get(account_id: str, request: Request):
    authorized, _actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a player, director, or admin may read a tank account'}, status_code=403)
    rec = tank_accounts.get(account_id)
    if rec is None:
        return JSONResponse({'error': 'account not found'}, status_code=404)
    return JSONResponse(rec)


@app.post('/api/tank-accounts/{account_id}/code')
async def tank_account_code(account_id: str, request: Request):
    authorized, actor = _operator_gate(request)
    if not authorized or actor != 'player':
        return JSONResponse({'error': 'only the owner/player may request a code'}, status_code=403)
    try:
        result = tank_accounts.code(account_id, actor=actor)
    except KeyError:
        return JSONResponse({'error': 'account not found'}, status_code=404)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse(result)


@app.post('/api/tank-accounts/{account_id}/grant')
async def tank_account_grant(account_id: str, request: Request):
    authorized, actor = _operator_gate(request)
    if not authorized or actor != 'player':
        return JSONResponse({'error': 'only the owner/player may grant a code read'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be an object'}, status_code=400)
    try:
        result = tank_accounts.mint_grant(
            account_id,
            (body.get('agentId') or '').strip(),
            granted_by=actor,
            ttl_s=body.get('ttlSec') or tank_accounts.DEFAULT_GRANT_S,
        )
    except KeyError:
        return JSONResponse({'error': 'account not found'}, status_code=404)
    except (ValueError, TypeError) as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse(result)


@app.post('/api/tank-accounts/{account_id}/agent-code')
async def tank_account_agent_code(account_id: str, request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not agent_id:
        return JSONResponse({'error': 'a valid agent key is required'}, status_code=401)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be an object'}, status_code=400)
    grant = (body.get('grant') or '').strip()
    if not grant:
        return JSONResponse({'error': 'grant is required'}, status_code=400)
    if not tank_accounts.consume_grant(grant, agent_id, account_id):
        return JSONResponse({'error': 'invalid, expired, or already-used grant'}, status_code=403)
    try:
        result = tank_accounts.code(account_id, actor=agent_id, agent_id=agent_id)
    except KeyError:
        return JSONResponse({'error': 'account not found'}, status_code=404)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse(result)


@app.post('/api/tank-accounts/{account_id}/rotate')
async def tank_account_rotate(account_id: str, request: Request):
    authorized, actor = _operator_gate(request)
    if not authorized or actor != 'player':
        return JSONResponse({'error': 'only the owner/player may rotate a TOTP secret'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        result = tank_accounts.rotate(
            account_id, actor=actor, new_secret=body.get('totpSecret') or '')
    except KeyError:
        return JSONResponse({'error': 'account not found'}, status_code=404)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({'error': str(e)}, status_code=503)
    return JSONResponse(result)


@app.delete('/api/tank-accounts/{account_id}')
async def tank_account_delete(account_id: str, request: Request):
    authorized, actor = _operator_gate(request)
    if not authorized or actor != 'player':
        return JSONResponse({'error': 'only the owner/player may delete a tank account'}, status_code=403)
    try:
        result = tank_accounts.delete(account_id, actor=actor)
    except KeyError:
        return JSONResponse({'error': 'account not found'}, status_code=404)
    return JSONResponse(result)


# --- CAPTCHA relay and broker ------------------------------------------------
# A detected CAPTCHA creates a request. The owner answers on Telegram with
# "captcha <id> <answer>". The broker then fills that answer into the same
# browser page and submits it. Interactive challenges (reCAPTCHA/hCaptcha/
# Turnstile/Arkose) are never auto-clicked; the owner solves them.

@app.get('/api/captcha/policies')
async def captcha_policies(request: Request):
    authorized, _actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a player, director, or admin may read CAPTCHA policy'}, status_code=403)
    return JSONResponse({'policies': captcha.list_policies()})


@app.post('/api/captcha/policies')
async def captcha_policy_set(request: Request):
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a player, director, or admin may set CAPTCHA policy'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be an object'}, status_code=400)
    try:
        result = captcha.set_policy(
            body.get('host') or '', body.get('mode') or 'relay',
            notes=body.get('notes') or '', updated_by=actor)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse(result)


@app.post('/api/captcha/{request_id}/answer')
async def captcha_answer(request_id: str, request: Request):
    authorized, actor = _operator_gate(request)
    if not authorized:
        return JSONResponse({'error': 'only a player, director, or admin may answer a CAPTCHA'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be an object'}, status_code=400)
    try:
        rec = captcha.set_answer(request_id, body.get('answer') or '', answered_by=actor)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    if rec is None:
        return JSONResponse({'error': 'request is unknown, expired, or already solved'}, status_code=404)
    return JSONResponse(rec)
