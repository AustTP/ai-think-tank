

@app.post('/api/photo-review/album')
async def photo_review_album(request: Request):
    """Review NEW photos from a macOS Photos album, on demand. The iPhone tags
    photos by adding them to an album; iCloud Photos syncs the album to this
    Mac; this reads the synced originals and reviews only assets not already
    recorded in library/photo-reviews/.reviewed.json."""
    authed, actor = _photo_review_auth(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    claimed = (body.get('agentId') or '').strip() or None
    if claimed and claimed != actor:
        return JSONResponse({'error': 'photo reviews require the operator itself'}, status_code=403)
    agent_id = claimed or actor
    album = (body.get('album') or '').strip()
    if not album:
        return JSONResponse({'error': 'album is required'}, status_code=400)
    mode = (body.get('mode') or 'general').strip().lower()
    if mode not in ('general', 'design'):
        return JSONResponse({'error': "mode must be 'general' or 'design'"}, status_code=400)
    try:
        limit = max(1, min(int(body.get('limit') or 5), 10))
    except (TypeError, ValueError):
        limit = 5
    try:
        assets = _apple_photos.album_assets(album)
    except FileNotFoundError as e:
        return JSONResponse({'error': str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({'error': f'could not read the Photos library: {e}'}, status_code=502)
    if not assets:
        return JSONResponse({'ok': True, 'album': album, 'total': 0,
                             'pending': 0, 'reviewed': [],
                             'message': f'album {album!r} has no photos (or has not synced yet)'})
    summary = await asyncio.to_thread(
        _review_album_batch_sync, album, body.get('project'), mode,
        body.get('context'), limit, agent_id, assets)
    if summary.get('busy'):
        return JSONResponse({'error': 'a photo review is already running'}, status_code=409)
    if not summary.get('ok'):
        return JSONResponse({'error': summary.get('error') or 'review failed'}, status_code=500)
    if not summary.get('reviewed'):
        return JSONResponse({'ok': True, 'album': album, 'total': summary['total'],
                             'pending': summary['pending'], 'reviewed': [],
                             'message': 'nothing new in this album'})
    return JSONResponse({'ok': True, 'album': album, 'total': summary['total'],
                         'pending': summary['pending'],
                         'reviewedCount': summary['reviewedCount'],
                         'reviewed': summary['reviewed']})


@app.get('/api/photo-review/album/watch')
async def photo_review_watch_status(request: Request):
    """Current watch config plus how many album assets still need review."""
    authed, _actor = _photo_review_auth(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    cfg = _photo_watch_config()
    out = dict(cfg)
    if cfg.get('enabled') and cfg.get('album'):
        try:
            assets = _apple_photos.album_assets(cfg['album'])
            manifest = _load_photo_manifest()
            out['pending'] = sum(
                1 for a in assets
                if _photo_needs_review(manifest, f'{cfg["album"]}::{a["uuid"]}'))
            out['total'] = len(assets)
        except Exception as e:
            out['pendingError'] = str(e)[:300]
    return JSONResponse(out)


@app.post('/api/photo-review/album/watch')
async def photo_review_watch(request: Request):
    """Start, stop, or re-baseline the watch on one dedicated album.

    action=start: enable the watcher. The first start marks every photo
    already in the album as baseline (seen, not reviewed), so only photos
    added afterwards are processed. action=baseline marks the current album
    contents seen again; action=stop disables the watcher."""
    authed, actor = _photo_review_auth(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    claimed = (body.get('agentId') or '').strip() or None
    if claimed and claimed != actor:
        return JSONResponse({'error': 'photo reviews require the operator itself'}, status_code=403)
    action = (body.get('action') or 'start').strip().lower()
    cfg = _photo_watch_config()
    if action == 'stop':
        cfg['enabled'] = False
        cfg['stoppedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _photo_save_watch_config(cfg)
        return JSONResponse({'ok': True, **cfg})
    if action not in ('start', 'baseline'):
        return JSONResponse({'error': "action must be 'start', 'stop' or 'baseline'"}, status_code=400)
    album = (body.get('album') or cfg.get('album') or '').strip()
    if not album:
        return JSONResponse({'error': 'album is required'}, status_code=400)
    try:
        albums = _apple_photos.list_albums()
    except Exception as e:
        return JSONResponse({'error': f'could not read the Photos library: {e}'}, status_code=502)
    if album not in albums:
        return JSONResponse({'error': f'album {album!r} does not exist in the Photos '
                                      'library yet (create it on the iPhone and let it sync)'},
                            status_code=400)
    baselined = 0
    if action == 'baseline' or not cfg.get('baselineDone'):
        try:
            baselined = _photo_baseline_album(album)
        except Exception as e:
            return JSONResponse({'error': f'could not baseline the album: {e}'}, status_code=502)
    if action == 'start':
        mode = (body.get('mode') or cfg.get('mode') or 'general').strip().lower()
        if mode not in ('general', 'design'):
            return JSONResponse({'error': "mode must be 'general' or 'design'"}, status_code=400)
        try:
            limit = max(1, min(int(body.get('limit') or cfg.get('limit') or PHOTO_WATCH_BATCH), 10))
        except (TypeError, ValueError):
            limit = PHOTO_WATCH_BATCH
        cfg.update({
            'enabled': True,
            'album': album,
            'project': (body.get('project') or cfg.get('project') or 'photos'),
            'mode': mode,
            'context': body.get('context', cfg.get('context') or ''),
            'limit': limit,
            'baselineDone': True,
            'startedAt': cfg.get('startedAt') or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'pending': 0,
            'lastCheckAt': None,
        })
    else:
        cfg['album'] = album
        cfg['baselineDone'] = True
        cfg.setdefault('enabled', False)
    cfg['baselinedCount'] = baselined
    cfg['baselinedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _photo_save_watch_config(cfg)
    log_action(actor, 'photo_watch', {'action': action, 'album': album,
                                      'baselined': baselined})
    return JSONResponse({'ok': True, **cfg})


@app.post('/api/photo-review/key')
async def photo_review_key_rotate(request: Request):
    """Rotate the iPhone Shortcut key. Player-only. The new key is returned
    once and written to .env; the old key stops working immediately."""
    authed, actor = _operator_gate(request)
    if not authed or actor != 'player':
        return JSONResponse({'error': 'photo review key rotation is player-only'},
                            status_code=403)
    key = secrets.token_urlsafe(32)
    try:
        _set_env_keys({'PHOTO_REVIEW_KEY': key})
    except Exception as e:
        return JSONResponse({'error': f'could not write .env: {e}'}, status_code=500)
    _reload_env_config()
    log_action(None, 'photo_review_key_rotated', {}, authorized=True)
    return JSONResponse({'ok': True, 'key': key,
                         'note': 'shown once; update the Shortcut now'})


@app.delete('/api/photo-review/key')
async def photo_review_key_revoke(request: Request):
    """Disable the iPhone Shortcut credential entirely."""
    authed, actor = _operator_gate(request)
    if not authed or actor != 'player':
        return JSONResponse({'error': 'photo review key revocation is player-only'},
                            status_code=403)
    try:
        _set_env_keys({'PHOTO_REVIEW_KEY': ''})
    except Exception as e:
        return JSONResponse({'error': f'could not write .env: {e}'}, status_code=500)
    _reload_env_config()
    log_action(None, 'photo_review_key_revoked', {}, authorized=True)
    return JSONResponse({'ok': True, 'revoked': True})


@app.post('/api/photo-review')
async def photo_review(request: Request):
    """On-demand review of one iPhone/Photos image. Nothing watches a folder:
    a photo is analyzed only when this endpoint is called (the /photos.html
    picker or an iOS Shortcut). Writes the image plus a text note under
    library/photo-reviews/, which .gitignore keeps out of the repo.
    Auth: player session, a director/admin agent key, or X-Photo-Key matching
    PHOTO_REVIEW_KEY in .env (purpose-scoped credential for a Shortcut)."""
    authed, actor = _photo_review_auth(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    claimed = (body.get('agentId') or '').strip() or None
    if claimed and claimed != actor:
        return JSONResponse({'error': 'photo reviews require the operator itself'}, status_code=403)
    agent_id = claimed or actor
    image_b64 = (body.get('imageBase64') or '').strip()
    if not image_b64:
        return JSONResponse({'error': 'imageBase64 is required'}, status_code=400)
    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception:
        return JSONResponse({'error': 'imageBase64 is not valid base64'}, status_code=400)
    if len(image_bytes) > 12 * 1024 * 1024:
        return JSONResponse({'error': 'image is too large (max 12 MB)'}, status_code=400)
    mode = (body.get('mode') or 'general').strip().lower()
    if mode not in ('general', 'design'):
        return JSONResponse({'error': "mode must be 'general' or 'design'"}, status_code=400)
    rel_dir = _photo_review_rel(body.get('project'))
    name = _safe_design_reference_name(body.get('name'))
    if not name:
        name = 'photo-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    ext = '.png'
    for magic, candidate in ((b'\x89PNG\r\n\x1a\n', '.png'),
                             (b'\xff\xd8\xff', '.jpg'),
                             (b'RIFF', '.webp')):
        if image_bytes[:len(magic)] == magic:
            ext = candidate
            break
    try:
        photo_dir = _safe_library_path(rel_dir)
        if not photo_dir:
            return JSONResponse({'error': 'invalid photo-reviews path'}, status_code=400)
        os.makedirs(photo_dir, exist_ok=True)
    except Exception:
        return JSONResponse({'error': 'could not create the photo-reviews directory'}, status_code=500)
    image_rel = os.path.join(rel_dir, f'{name}{ext}')
    image_path = _safe_library_path(image_rel)
    brief_rel = os.path.join(rel_dir, f'{name}.txt')
    brief_path = _safe_library_path(brief_rel)
    if not image_path or not brief_path:
        return JSONResponse({'error': 'invalid name'}, status_code=400)
    review = _photo_review_from_image(
        image_b64, f'player photo {name}', context=body.get('context'), mode=mode)
    if not review.get('ok'):
        return JSONResponse({'error': review.get('note', 'could not review the photo')}, status_code=502)
    header = (f'# Photo review: {name}\n\n'
              f'- Source: player upload ({ext.lstrip(".")})\n'
              f'- Directory: {rel_dir}\n'
              f'- Mode: {mode}\n'
              f'- Reviewed: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n'
              f'- Image file: {image_rel}\n\n')
    try:
        with open(image_path, 'wb') as f:
            f.write(image_bytes)
        _write_file(brief_path, header + review['brief'] + '\n')
    except Exception as e:
        return JSONResponse({'error': f'could not write the photo review: {e}'}, status_code=500)
    _prune_photo_reviews_sync()
    log_action(agent_id, 'photo_review',
               {'name': name, 'image': image_rel, 'brief': brief_rel, 'mode': mode})
    return JSONResponse({'ok': True, 'name': name, 'image': image_rel,
                         'briefPath': brief_rel, 'text': header + review['brief']})


@app.post('/api/design-reference-url')
async def design_reference_url(request: Request):
    """Designmind-style design brief from a live URL: fetch the page, take a
    real screenshot, run the vision tier, and write a design-references/<host>.txt
    brief any agent can read. Operator-gated (player session or a
    director/admin agent), and the URL goes through the same
    SSRF/public-host guard as /api/browse (never a private/internal address)."""
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
    url = (body.get('url') or '').strip()
    if not url:
        return JSONResponse({'error': 'url is required'}, status_code=400)
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        parsed = None
    if not parsed or parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return JSONResponse({'error': 'a valid http(s) URL is required'}, status_code=400)
    if not _is_safe_public_host(parsed.hostname):
        return JSONResponse({'error': 'that address resolves to a private or internal network location'}, status_code=400)
    screenshot_b64 = await asyncio.to_thread(_screenshot_url_sync, url)
    if not screenshot_b64:
        return JSONResponse({'error': 'could not screenshot that page (no headless browser, or the page did not render)'}, status_code=502)
    host = re.sub(r'[^A-Za-z0-9._\-]', '_', (parsed.hostname or 'page'))
    brief = _design_brief_from_image(screenshot_b64, f'URL {url}', context=f'Design review of the live page at {url}')
    if not brief.get('ok'):
        return JSONResponse({'error': brief.get('note', 'could not generate the design brief')}, status_code=502)
    project = _design_project_rel(body.get('project'), village=body.get('village') or 'main')
    try:
        design_dir = _safe_library_path(project)
        if not design_dir:
            return JSONResponse({'error': 'invalid design-references path'}, status_code=400)
        os.makedirs(design_dir, exist_ok=True)
    except Exception:
        return JSONResponse({'error': 'could not create the design-references directory'}, status_code=500)
    header = (f'# Design review: {url}\n\n'
              f'- Source: live page screenshot\n'
              f'- Project: {project}\n'
              f'- Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n')
    try:
        brief_rel = os.path.join(project, f'{host}.txt')
        brief_path = _safe_library_path(brief_rel)
        if brief_path:
            _write_file(brief_path, header + brief['brief'] + '\n')
    except Exception as e:
        return JSONResponse({'error': f'could not write the design review: {e}'}, status_code=500)
    log_action(agent_id, 'design_reference_url', {'url': url, 'brief': brief_rel, 'project': project})
    return JSONResponse({'ok': True, 'url': url, 'briefPath': brief_rel, 'text': header + brief['brief']})


def _fold_design_taste(briefs, project, owner='player', review_after_days=None,
                       village='main'):
    """Fold a project's raw design briefs into ONE distilled taste doc, the
    single source of truth agents load before design work. Uses the mid tier
    (a text-synthesis fold, not a vision read -- each brief is already a
    structured text report). Returns (taste_text, error).

    Memory-engineering: the taste doc is a SLOW-clock shared memory. Its header
    records owner + reviewAfterDays (the player who owns the taste, and when it
    must be re-verified) so the read path can skip an expired taste doc."""
    if not briefs:
        return None, 'no design briefs in this project yet'
    items = []
    for i, rel in enumerate(briefs, 1):
        target = _safe_library_path(rel)
        if not target or not os.path.isfile(target):
            continue
        try:
            with open(target, 'r', errors='replace') as f:
                items.append(f'--- Brief {i}: {rel} ---\n' + f.read()[:8000])
        except Exception:
            continue
    if not items:
        return None, 'no design briefs could be read'
    try:
        tier = _mid_tier_slug()
        if not tier:
            return None, 'no mid-tier model is configured to fold the taste doc'
        prompt = (
            'You are distilling a design TASTE DOC for a small team. Below are design briefs '
            'the player collected as inspiration for a project. Fold them into ONE concise '
            'taste reference with these exact sections:\n'
            'PALETTE: the hex colors to use (backgrounds, text, accents) -- merge the briefs, '
            'keep every distinct color that appears in more than one brief.\n'
            'TYPOGRAPHY: font families/styles, sizes, weights, heading-vs-body contrast.\n'
            'LAYOUT: grid, spacing rhythm, alignment, section structure, whitespace.\n'
            'STYLE: a one-line overall aesthetic.\n'
            'AVOID: colors/fonts/patterns only one brief mentions, and anything not grounded '
            'in at least two of the briefs. Be concrete -- real hex codes.\n\n' + '\n\n'.join(items)
        )
        result = _call_openrouter_sync(tier, [{'role': 'user', 'content': prompt}], 1200)
        reply = (result.get('choices') or [{}])[0].get('message', {}).get('content') or ''
    except Exception as e:
        return None, f'design taste fold failed: {e}'
    if not reply.strip():
        return None, 'design taste fold returned no reply'
    try:
        days = max(0, int(review_after_days)) if review_after_days is not None else _DESIGN_REVIEW_DAYS
    except (TypeError, ValueError):
        days = _DESIGN_REVIEW_DAYS
    review_after = (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    header = (f'# Design taste: {project}\n\n'
              f'- Folded from {len(items)} design briefs\n'
              f'- Owner: {owner}\n'
              f'- Village: {village}\n'
              f'- Review after: {review_after}\n'
              f'- Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n')
    return header + reply.strip()[:12000], None


@app.post('/api/design-reference/taste')
async def design_reference_taste(request: Request):
    """Fold a project's collected design briefs into one distilled taste doc
    (design-references/<project>/taste.md), the single source of truth agents
    are required to load before design work on that project. Operator-gated
    (player session or a director/admin agent).
    Overwrites taste.md in place -- re-run after adding briefs."""
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
    project = _design_project_rel(body.get('project'), village=body.get('village') or 'main')
    briefs = _list_design_briefs(body.get('project'), village=body.get('village') or 'main')
    if not briefs:
        return JSONResponse({'error': 'no design briefs in this project yet -- upload inspiration first'}, status_code=400)
    taste, error = _fold_design_taste(briefs, body.get('project'),
                                      owner=body.get('owner') or 'player',
                                      review_after_days=body.get('reviewAfterDays'),
                                      village=body.get('village') or 'main')
    if error:
        return JSONResponse({'error': error}, status_code=502)
    taste_rel = os.path.join(project, 'taste.md')
    taste_path = _safe_library_path(taste_rel)
    if not taste_path:
        return JSONResponse({'error': 'invalid project path'}, status_code=400)
    try:
        design_dir = _safe_library_path(project)
        os.makedirs(design_dir, exist_ok=True)
        _write_file(taste_path, taste + '\n')
    except Exception as e:
        return JSONResponse({'error': f'could not write the taste doc: {e}'}, status_code=500)
    log_action(agent_id, 'design_reference_taste', {'project': project, 'taste': taste_rel, 'briefs': len(briefs)})
    return JSONResponse({'ok': True, 'project': project, 'tastePath': taste_rel, 'briefCount': len(briefs),
                         'text': taste})


def _design_context_for_project(project, village='main'):
    """Read-before-act context block for design work on a project: the folded
    taste doc plus a pointer to the raw inspiration briefs, scoped to the
    reader's VILLAGE so one village's design vocabulary never leaks into
    another. Empty string when the project has no taste doc (the executor still
    runs, just without a design vocabulary -- the pre-existing behavior)."""
    rel_dir = _design_project_rel(project, village=village)
    taste_rel = os.path.join(rel_dir, 'taste.md')
    taste_path = _safe_library_path(taste_rel)
    lines = []
    if taste_path and os.path.isfile(taste_path):
        try:
            with open(taste_path, 'r', errors='replace') as f:
                taste = f.read(20000)
        except Exception:
            taste = ''
        if taste.strip() and not _taste_is_expired(taste):
            lines.append(f'The design taste for this project (READ THIS FIRST):\n{taste.strip()}')
    briefs = _list_design_briefs(project, village=village)
    if briefs:
        lines.append('Raw inspiration briefs in the project library (read the relevant ones): '
                     + ', '.join(f'{b}' for b in briefs))
    return '\n\n'.join(lines)


def _taste_is_expired(taste):
    """True when a taste doc's 'Review after: YYYY-MM-DD' header has passed --
    the taste is a SLOW-clock memory that must be re-verified, not trusted
    forever. Missing/unparseable review date is treated as not expired (never
    silently drop a valid doc on a format change)."""
    m = re.search(r'Review after:\s*(\d{4})-(\d{2})-(\d{2})', taste)
    if not m:
        return False
    try:
        review = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return review < datetime.date.today()


# Input pattern guard: fast, deterministic injection-vector check applied
# BEFORE any message list reaches a model (/api/chat and the sublow experiment
# proxy). Scans the message text for known prompt-injection patterns (ignore
# previous instructions, tone override, system-prompt extraction, role-play
# inversion). A match is rejected as 400 with an audit log entry so the player
# can see whether an agent or an external source triggered it -- not silently
# dropped. Shared rather than inline so both chokepoints enforce the identical
# rule (a one-pattern drift between them would be a silent hole).
_INPUT_GUARD_PATTERNS = (
    r'(?i)(ignore|forget|override|disregard|skip)\s+(all\s+)?(previous|prior|the\s+above)',
    r'(?i)(you\s+are\s+now|from\s+now\s+on|your\s+new\s+role|act\s+as)',
    r'(?i)system\s+(prompt|instruction|message|override)\s*(:|is|:)',
    r'(?i)(reveal|show|print|output|leak|display)\s+(your|the)\s+(prompt|system|instructions)',
)


def _input_guard_violation(messages):
    """None when the message list is clean; otherwise {'patterns': [the
    matched regexes], 'snippets': [the matching windows of text]} for the
    caller's audit log. Multimodal messages carry content as a list of parts
    ({type: text|image_url, ...}) -- scan the text parts only, since re.search
    on the raw list would TypeError (the vision review path hit this as a 500)."""
    for msg in (messages or []):
        raw = (msg.get('content') or '') if isinstance(msg, dict) else ''
        text = raw if isinstance(raw, str) else ' '.join(
            (p.get('text') or '') for p in raw
            if isinstance(p, dict) and isinstance(p.get('text'), str))
        matched = [p for p in _INPUT_GUARD_PATTERNS if re.search(p, text)]
        if matched:
            return {'patterns': matched,
                    'snippets': [text[max(0, m.start() - 60):m.end() + 60]
                                 for m in (re.search(p, text) for p in matched) if m]}
    return None


def _director_budget_gate(agent_id, model, service, authorized, lane):
    """The per-director monthly spend gate, shared by every model
    chokepoint (same shape and position as the per-agent gate): each
    director's team draws on its own admin-set monthly slice of the
    tank budget (POST /api/budget/director), so a director who burns
    their slice pauses their OWN team's calls -- other directors keep
    working -- until the chief of staff grants a raise (the 429 tells the
    caller to ask via POST /api/budget/request). Returns the 429
    response to return, or None when the director is not capped
    (feature off, no slice set, or the agent is unknown)."""
    if not agent_id:
        return None
    status = _director_spend_status(agent_id)
    if not status:
        return None
    log_action(agent_id, f'{lane}_director_budget_blocked',
               {'directorId': status['directorId'], 'model': model,
                'service': service}, authorized=authorized)
    return JSONResponse({
        'error': (f"Director {status['directorId']}'s team has used its "
                  f"monthly model-spend budget (${status['cap']:.2f}) and "
                  f"further calls are paused until the chief of staff grants more "
                  f"(ask via POST /api/budget/request)."),
        'directorBudgetExhausted': True,
        'directorId': status['directorId'],
        'directorBudgetUsd': status['cap'],
    }, status_code=429)


@app.post('/api/chat')
async def chat(request: Request):
    # The only place OPENROUTER_API_KEY is ever used -- it never leaves
    # this process. The client sends the model slug + full message list
    # (it owns persona/prompt construction, this is just a secure proxy)
    # and gets back the reply text, nothing else.
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)
    # /api/chat is in AUTH_PUBLIC_PATHS (it governs OpenRouter spend,
    # so it guards itself by EITHER a player session OR a valid agent key --
    # the same agent-key attribution model every other /api endpoint uses). The
    # server's own loopback (assign-big-task planning, via _http_json) has no
    # session cookie, only X-Agent-Key; requiring a session here silently broke
    # that path with a 401 that surfaced as "couldn't reach a model to plan."
    # A bad key must still fail closed so no anonymous clock gets billed.
    body = await request.json()
    session_ok = verify_session(request.cookies.get(SESSION_COOKIE_NAME))
    key_ok = verify_agent_key(body.get('agentId'), request.headers.get('X-Agent-Key')) is True
    if not session_ok and not key_ok:
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    if credits_pause_active():
        return JSONResponse({
            'error': 'OpenRouter credits are exhausted. Model work is paused and will resume automatically when credits are added.',
            'creditsPaused': True,
        }, status_code=503)
    model = body.get('model')
    messages = body.get('messages')
    agent_id = body.get('agentId')
    authorized = verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
    if not model or not messages:
        return JSONResponse({'error': 'model and messages are required'}, status_code=400)
    # Input pattern guard (shared _input_guard_violation): rejected as 400
    # with an audit log entry so the player can see whether an agent or an
    # external source triggered it -- not silently dropped.
    violation = _input_guard_violation(messages)
    if violation:
        log_action(agent_id, 'chat_input_guard_blocked',
                   {'model': model, 'patterns_matched': violation['patterns'],
                    'snippet': violation['snippets']},
                   authorized=authorized)
        return JSONResponse({'error': 'Blocked by input guard (potential prompt injection)'}, status_code=400)
    # 300 was sized for a short in-character 1:1 reply -- real gap:
    # assignBigTask()'s structured multi-subtask JSON breakdown got
    # silently truncated mid-response by this same cap, producing invalid
    # JSON. 600 covered that without meaningfully changing the cost
    # profile of routine short replies, which never asked for anywhere
    # near it. Raised again to 4000 for the same reason, a bigger version
    # of it: real code generation (runCodingTask(), tasks.js) needs room
    # for a genuine file, not a fragment -- still per-request opt-in
    # (callers explicitly pass a higher max_tokens; the 150 default for
    # routine chat/handoff/pair-programming replies is untouched).
    try:
        max_tokens = min(max(int(body.get('max_tokens', 150)), 1), CHAT_MAX_TOKENS_CAP)
    except (TypeError, ValueError):
        max_tokens = 150
    # The Bank: attribute this call's cost to a service. Content executors
    # pass the product/room they're charging (task.get('productId')); the ask
    # lane and the player label themselves; an unlabelled call defaults to a
    # general bucket so spend is never silently unaccounted. Accrual happens
    # HERE, at the one choke point every model call flows through.
    # Attribution label: content executors pass the product/room they charge;
    # the ask lane labels itself. Anything else is unlabelled spend and lands
    # in a single general bucket so no cost is ever silently unaccounted -- but
    # per-product grouping (the useful strategic view) comes only from an
    # explicit service label, never from the agent's own id.
    service = body.get('service') or '__general__'
    # Item 4 per-task spend gate: a call carrying a taskId is refused once that
    # task's own model-spend budget is exhausted (see _task_budget_exhausted --
    # the ceiling reads the DURABLE task's budgetUsd, the spent total reads the
    # task's own ledger bucket, so the gate and the sim can never disagree).
    # The sim fail-closes the card on 'budgetExhausted' in the content result;
    # this refusal is a hard 429 so NO further money is spent for the task.
    # Calls without a taskId (the ask lane, the player, serve's own loopback
    # planning calls) are never gated, and a state/ledger read failure never
    # blocks a call.
    task_id = (body.get('taskId') or '').strip()
    # Training capture (opt-in): attribute every provider call in
    # this request to its agent and task. All model calls flow
    # through _openrouter_chat_post, which reads this context; the
    # contextvar is request-scoped (each /api/chat runs in its own
    # task), so it never leaks to other requests.
    if training_capture.enabled():
        training_capture.CURRENT.set(training_capture.metadata(
            get_state_from_db(), agent_id, task_id, body.get('purpose')))
    if task_id and _task_budget_exhausted(task_id):
        spent, attempts = _task_budget_spent(task_id)
        log_action(agent_id, 'task_budget_blocked',
                   {'taskId': task_id, 'model': model, 'service': service,
                    'taskSpendUsd': round(spent, 6), 'taskSpendAttempts': attempts},
                   authorized=authorized)
        return JSONResponse({
            'error': (f'This task has used its full model-spend budget '
                      f'(${spent:.4f} spent) and further model calls are paused until a director raises it.'),
            'budgetExhausted': True,
            'taskSpendUsd': round(spent, 6),
            'taskSpendAttempts': attempts,
        }, status_code=429)
    # Per-agent spend gate: a single agent (or one prompt-injected agent) must
    # not be able to burn the whole shared monthly budget for everyone. The
    # ceiling reads THIS agent's own ledger bucket for the current month.
    if agent_id and _agent_spend_cap_exceeded(agent_id):
        log_action(agent_id, 'agent_budget_blocked',
                   {'model': model, 'service': service},
                   authorized=authorized)
        return JSONResponse({
            'error': (f'This agent has used its full monthly model-spend budget '
                      f'(${AGENT_SPEND_CAP_USD:.2f}) and further calls are '
                      f'paused until a director raises it.'),
            'agentBudgetExhausted': True,
            'agentSpendCapUsd': AGENT_SPEND_CAP_USD,
        }, status_code=429)
    # Per-director spend gate: each director's team draws on its own
    # monthly slice (admin-set); exhausting it pauses this director's
    # team only, not the whole tank.
    _dir_blocked = _director_budget_gate(agent_id, model, service,
                                         authorized, 'chat')
    if _dir_blocked:
        return _dir_blocked
    # Plain-writing directive (token saver): by default fold the anti-AI-slop
    # ban list into the first system message so prose replies come back
    # shorter -- less filler is fewer billed input AND output tokens, without
    # demanding clipped speech. Skipped for structured/JSON requests (the
    # "not a word longer" nudge can truncate a schema mid-object) and for any
    # caller that opts out with {"plain": false}.
    if body.get('plain', True):
        def _msg_text(m):
            raw = (m.get('content') or '') if isinstance(m, dict) else ''
            if isinstance(raw, str):
                return raw
            return ' '.join((p.get('text') or '') for p in raw
                            if isinstance(p, dict) and isinstance(p.get('text'), str))
        prompt_text = ' '.join(_msg_text(m) for m in (messages or []))
        if 'JSON' not in prompt_text:
            messages = _apply_plain_writing(messages)
    # Test-time compute: for the low/mid (cheap) tiers, sample the prompt
    # `best_of` times and fold the drafts into one answer -- JSON-majority
    # vote when the request is structured, a same-model self-verification
    # judge pass for open-ended prose -- so the reported reply is far more
    # consistent than any single cheap draft. Deliberation is ON by default
    # for those tiers and can be disabled/overridden per request
    # ({"deliberate": false, "best_of": 3}); expensive/reasoning/coding tiers
    # never deliberate. Each sample is a separate billed call and is accrued
    # against the spend ledger like any other.
    deliberate = _ttc_should_deliberate(model, body.get('deliberate'), body.get('best_of'))
    best_n = _ttc_best_of(body.get('best_of')) if deliberate else 1
    try:
        # urllib is blocking -- run it off the event loop rather than
        # stalling every other request for the duration of the API call.
        data = await asyncio.to_thread(_call_openrouter_sync, model, messages, max_tokens, best_of=best_n)
        if best_n > 1:
            # Sampling path: every sample is a real completion. Fold the set
            # into ONE answer, summing each sample's cost for the single
            # accrual below. Prose that can't be majority-voted reuses these
            # drafts in the judge pass rather than re-sampling.
            total_cost = 0.0
            drafts = []
            for s in data:
                sample_cost = (s.get('usage') or {}).get('cost', 0.0)
                if isinstance(sample_cost, (int, float)) and sample_cost:
                    total_cost += float(sample_cost)
                drafts.append((s.get('choices') or [{}])[0].get('message', {}).get('content') or '')
            winner = _ttc_majority_json(data)
            if winner is not None:
                try:
                    json.loads(winner)
                except Exception:
                    winner = None
            if winner is not None:
                reply = winner
            else:
                reply = _ttc_self_verify(model, messages, best_n, max_tokens,
                                         drafts=drafts, service=service,
                                         village_id=_sim_village_for_agent(agent_id))
            usage_cost = total_cost
        else:
            reply = data['choices'][0]['message']['content']
            if not reply:
                print(f'[chat-debug] empty reply for model={model} raw={json.dumps(data)[:2000]}', flush=True)
                # Reasoning-tier models sometimes burn the entire max_tokens on
                # hidden reasoning and return content:null (finish_reason
                # length). Retry once with reasoning off so the model must
                # produce a visible answer in the same budget.
                try:
                    data = await asyncio.to_thread(
                        _call_openrouter_sync_reasoning_off, model, messages, max_tokens)
                    reply = data['choices'][0]['message']['content']
                    if reply:
                        print(f'[chat-debug] empty-reply recovered on reasoning-off retry model={model}', flush=True)
                    else:
                        print(f'[chat-debug] empty reply after retry model={model}', flush=True)
                except Exception as retry_exc:
                    print(f'[chat-debug] empty-reply retry failed model={model} err={retry_exc}', flush=True)
            usage_cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(usage_cost, (int, float)) and usage_cost:
            village_id = _sim_village_for_agent(agent_id)
            _accrue_spend(service, usage_cost, village_id=village_id, agent_id=agent_id)
            # Item 4 per-task accrual: count this call against the task's own
            # ledger bucket so _task_budget_exhausted sees live spend. Only
            # calls that passed the gate (or carry no taskId) reach here.
            if task_id:
                _accrue_task_spend(task_id, usage_cost)
            # High-tier calls accrue against the dedicated high-tier monthly
            # budget too so the JEV gate can fail closed when the
            # month's high-tier allowance is spent. Single accrual point -- this
            # is the one place the expensive tier's cost is counted (was doubled
            # by a duplicate block halving the effective budget).
            if model == _high_tier_slug():
                _accrue_high_tier_spend(usage_cost)
        log_action(agent_id, 'chat', {'model': model, 'service': service, 'cost': float(usage_cost) if isinstance(usage_cost, (int, float)) else 0.0, 'best_of': best_n}, authorized=authorized)
        return JSONResponse({'reply': reply})
    except OpenRouterCreditsPaused as e:
        return JSONResponse({'error': str(e), 'creditsPaused': True}, status_code=503)
    except urllib.error.HTTPError as e:
        if e.code == 402:
            return JSONResponse({
                'error': 'OpenRouter credits are exhausted. Model work is paused and will resume automatically when credits are added.',
                'creditsPaused': True,
            }, status_code=503)
        return JSONResponse({'error': e.read().decode()}, status_code=e.code)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@app.get('/api/vpn-exits')
async def vpn_exits_view():
    try:
        state = get_state_from_db() or {}
    except Exception:  # noqa: BLE001 -- registry serves without cached locations
        state = {}
    registry = _vpn_exit_registry()
    snapshot = vpn_location.location_snapshot(state)
    exits = [vpn_location.describe_exit(name, registry, snapshot)
             for name in sorted(registry)]
    return JSONResponse({
        'exits': exits,
        'howToUse': 'Pass the exit name as browse_page\'s "exit" parameter (or POST /api/browse with viaVpnExit) to fetch one page through that container\'s own Mullvad tunnel. Omit it for an ordinary direct fetch. An exit not listed yet can be started by an operator: POST /api/vpn-exits/start {"relay": "se-got-001"}.',
        'configured': bool(exits),
        'locationTtlS': vpn_location.LOCATION_TTL_S,
    })


@app.post('/api/vpn-exits/refresh')
async def vpn_exits_refresh(request: Request):
    # Operator-gated live observation: fetch each exit's real
    # egress IP through its proxy and geolocate it, then cache
    # the snapshot in the sim state so /api/vpn-exits reports
    # where the exit actually appears to be (ZIP), not just the
    # static registry label.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- empty body means refresh every exit
        body = {}
    names = body.get('exits') if isinstance(body, dict) else None
    if names is not None and not isinstance(names, list):
        return JSONResponse(
            {'error': '"exits" must be a list of exit names'}, status_code=400)
    registry = _vpn_exit_registry()
    results = vpn_location.refresh_exits(registry, names)
    recorded = {name: r['record'] for name, r in results.items()
                if isinstance(r.get('record'), dict)}
    if recorded:
        state = get_state_from_db() or {}
        for name, record in recorded.items():
            vpn_location.record_location(state, name, record)
        save_state_to_db(state)
    return JSONResponse({
        'ok': True,
        'results': results,
        'exits': [vpn_location.describe_exit(name, registry,
                 vpn_location.location_snapshot(get_state_from_db() or {}))
                 for name in sorted(registry)],
        'actor': actor,
    })


@app.post('/api/vpn-exits/start')
async def vpn_exits_start(request: Request):
    # Operator-gated: spawning an exit container burns one
    # of the account's 5 Mullvad devices and runs docker
    # with --privileged on this host. Agents never start
    # exits -- an operator does, then agents USE the exit
    # through the ordinary viaVpnExit path.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'}, status_code=400)
    relay = (body.get('relay') or '').strip()
    name = (body.get('name') or '').strip()
    if not relay:
        return JSONResponse({'error': '"relay" is required (e.g. "se-got-001")'}, status_code=400)
    kwargs = {}
    if name:
        kwargs['name'] = name
    exit_record, error = VPN_EXIT_POOL.start(relay, **kwargs)
    if exit_record is None:
        # Pool refusals (cap reached, duplicate/bad name)
        # are caller-fixable; only a docker-run failure is
        # the host's fault.
        status = 502 if 'docker run failed' in (error or '') else 409
        return JSONResponse({'error': error}, status_code=status)
    try:
        state = get_state_from_db() or {}
    except Exception:  # noqa: BLE001 -- registry serves without cached locations
        state = {}
    described = vpn_location.describe_exit(
        exit_record.name, _vpn_exit_registry(),
        vpn_location.location_snapshot(state))
    return JSONResponse({'ok': True, 'exit': described, 'actor': actor})


@app.post('/api/vpn-exits/stop')
async def vpn_exits_stop(request: Request):
    # Operator-gated for the same reason as start: retiring
    # an exit removes a container this host is running.
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse({'error': 'body must be a JSON object'}, status_code=400)
    name = (body.get('name') or '').strip()
    if not name:
        return JSONResponse({'error': '"name" is required'}, status_code=400)
    if name not in VPN_EXIT_POOL.names():
        return JSONResponse({'error': f'no running exit named "{name}"'}, status_code=404)
    if not VPN_EXIT_POOL.stop(name):
        return JSONResponse({'error': f'failed to remove exit container "{name}"'}, status_code=502)
    return JSONResponse({'ok': True, 'stopped': name, 'actor': actor})


# ---------------------------------------------------------------------------
# Workroom org surface. Standing tasks and signal plans are durable
# declarations in the sim state (the scheduler and the signal waker
# both rebuild their objects from state on every pass, so a restart
# resumes exactly where things left off). The container broker is
# in-memory policy: it bounds what an agent may provision, it is not
# the runtime that runs the containers.
# ---------------------------------------------------------------------------


def _camel_case_name(name):
    """snake_case to the camelCase the API speaks."""
    head, *rest = str(name).split('_')
    return head + ''.join(part[:1].upper() + part[1:] for part in rest)


def _public_dict(value):
    """A JSON-safe, camelCase copy of a dataclass-shaped
    value: the pure modules name their fields snake_case,
    the API and the durable declarations speak camelCase,
    and sets, tuples, and datetimes do not serialize.
    Everything else passes through untouched."""
    if isinstance(value, dict):
        return {_camel_case_name(key): _public_dict(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_public_dict(item) for item in value]
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value
