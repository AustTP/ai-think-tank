"""Trusted codebase registry (2026-11-05, renamed from "trusted repos").

Owner directive: the tank RUNS vetted upstream codebases instead of
hard-coding their logic into the tree, and agents maintain the vetting
list themselves. "Codebase", not "repository": the source may surface
anywhere (a GitHub repo, a PyPI package, a vendor tarball, a git URL) --
an entry is a SOURCE THE TANK TRUSTS, whatever shape it arrives in.

  GET  /api/trusted-codebase        the list (player or agent key)
  POST /api/trusted-codebase        an agent ADDS a source (agent key)
  (+ /api/trusted-repos kept as a deprecated alias for both)

An entry records who vouched for the source and what the tank uses it
for; execution sites (e.g. the X adapter) consult this list before
running a tool and refuse anything unlisted. Storage is a single JSON
file (THINK_TANK_DIR/trusted_codebase.json, 0600) written atomically.

Agent adds are governed but not gated on approval: any authenticated
agent may add a source with a stated purpose; the entry records the
adder, and the operator can remove entries (there is deliberately no
agent-side delete -- disputes go to the chief of staff, who escalates).

Size policy (owner directive, 2026-11-05): downloading a trusted
codebase artifact below TRUSTED_CODEBASE_AUTO_APPROVE_MB (default 100
MB) needs no approval; anything larger requires the owner's approval
with a stated reason, and the requested download's size is surfaced to
both the agent and the owner. The policy itself lives in
188_codebase_fetch; this registry carries each entry's declared size
and the shared threshold helper.
"""
import json
import os
import re
import time

_TRUSTED_CODEBASE_PATH = os.path.join(THINK_TANK_DIR, 'trusted_codebase.json')
_TRUSTED_CODEBASE_MAX = 200
_TRUSTED_SLUG_RE = re.compile(r'^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$')
_TRUSTED_URL_RE = re.compile(r'^https://[A-Za-z0-9._~:/?#\[\]@!$&\'()*+,;=%-]+$')
_TRUSTED_KINDS = ('pip-cli', 'clone-run', 'container', 'archive')


def trusted_codebase_auto_mb():
    """The auto-approval threshold for codebase downloads (MB). Owner-set
    override through .env; the default is 100 MB."""
    try:
        raw = (_load_env() or {}).get('TRUSTED_CODEBASE_AUTO_APPROVE_MB', '')
        mb = float(raw)
        if 1 <= mb <= 10240:
            return mb
    except Exception:
        pass
    return 100.0


def _load_trusted_codebase():
    path = _TRUSTED_CODEBASE_PATH
    legacy = os.path.join(THINK_TANK_DIR, 'trusted_repos.json')
    for candidate in (path, legacy):
        try:
            with open(candidate) as fh:
                data = json.load(fh)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        items = data.get('sources')
        if not isinstance(items, list):
            items = data.get('repos')  # legacy key name
        if isinstance(items, list):
            # Normalize legacy entry shape: 'repo' -> 'source'.
            for entry in items:
                if isinstance(entry, dict) and 'source' not in entry \
                        and entry.get('repo'):
                    entry['source'] = entry.pop('repo')
            data['sources'] = items
            data.pop('repos', None)
            return data
    return {'sources': []}


def _save_trusted_codebase(data):
    os.makedirs(THINK_TANK_DIR, exist_ok=True)
    tmp = _TRUSTED_CODEBASE_PATH + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=1)
        fh.write('\n')
    os.replace(tmp, _TRUSTED_CODEBASE_PATH)
    try:
        os.chmod(_TRUSTED_CODEBASE_PATH, 0o600)
    except OSError:
        pass


def trusted_sources():
    """The trusted source list as the execution sites see it: a set of
    lowercase slugs and normalized source keys."""
    data = _load_trusted_codebase()
    keys = set()
    for entry in data['sources']:
        src = str(entry.get('source') or entry.get('repo') or '').strip().lower()
        if src:
            keys.add(src)
    return keys


def source_is_trusted(source):
    """True when a requested source matches a trusted entry exactly, or is
    a URL that resolves onto a trusted entry's host+path prefix."""
    src = str(source or '').strip().lower()
    if not src:
        return False
    keys = trusted_sources()
    if src in keys:
        return True
    if src.startswith('https://'):
        for key in keys:
            if key.startswith('https://') and src.startswith(key):
                return True
    return False


def seed_trusted_codebase():
    """First-boot seed: the sources the operator already vetted by using
    them. Idempotent -- never clobbers agent additions."""
    data = _load_trusted_codebase()
    have = {str(e.get('source') or e.get('repo') or '').lower()
            for e in data['sources']}
    for entry in (
        {'source': 'vladkens/twscrape', 'kind': 'pip-cli', 'tool': 'twscrape',
         'purpose': 'X/Twitter GraphQL reader (search, tweets, timelines); '
                    'runs against the tank session pool at '
                    'x_scrape/accounts.db. Replaces the Apify actor lane '
                    'per the owner directive of 2026-11-05.',
         'added_by': 'operator', 'added_at': '2026-11-05'},
    ):
        if entry['source'].lower() not in have:
            data['sources'].append(entry)
    if data['sources']:
        _save_trusted_codebase(data)


def _trusted_auth(request):
    """Returns the authenticated principal or None."""
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    if agent_id and verify_agent_key(agent_id, key) is True:
        return agent_id
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return 'player'
    return None


def _trusted_entry_payload(data):
    return {'sources': data['sources'], 'autoApproveMb':
            trusted_codebase_auto_mb()}


@app.get('/api/trusted-codebase')
async def api_trusted_codebase_get(request: Request):
    principal = _trusted_auth(request)
    if not principal:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(principal):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    data = _load_trusted_codebase()
    log_action(principal, 'trusted_codebase_read',
               {'count': len(data['sources'])}, authorized=True)
    return JSONResponse(_trusted_entry_payload(data))


# Deprecated alias kept for the first cascade wave; /api/trusted-repos and
# /api/trusted-codebase are the same registry.
@app.get('/api/trusted-repos')
async def api_trusted_repos_get(request: Request):
    return await api_trusted_codebase_get(request)


@app.post('/api/trusted-codebase')
async def api_trusted_codebase_add(request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not agent_id or verify_agent_key(
            agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    source = str(body.get('source') or body.get('repo') or '').strip()
    purpose = str(body.get('purpose') or '').strip()
    kind = str(body.get('kind') or 'pip-cli').strip()
    tool = str(body.get('tool') or '').strip()
    declared_mb = body.get('declaredSizeMb')
    if _TRUSTED_SLUG_RE.match(source):
        source = source  # owner/name slug, canonical form
    elif _TRUSTED_URL_RE.match(source) and source.lower().startswith('https://'):
        source = source.rstrip('/')
    else:
        return JSONResponse(
            {'error': 'source must be an owner/name slug or an https URL'},
            status_code=400)
    if kind not in _TRUSTED_KINDS:
        return JSONResponse(
            {'error': f'kind must be one of {sorted(_TRUSTED_KINDS)}'},
            status_code=400)
    if not purpose or len(purpose) > 500:
        return JSONResponse(
            {'error': 'purpose required (1-500 chars) -- say what the tank '
                      'uses the codebase for'}, status_code=400)
    if len(tool) > 60:
        return JSONResponse({'error': 'tool name too long'}, status_code=400)
    try:
        declared_mb = round(float(declared_mb), 1) if declared_mb else None
        if declared_mb is not None and not (0.01 <= declared_mb <= 10240):
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({'error': 'declaredSizeMb must be a number '
                            'between 0.01 and 10240'}, status_code=400)
    data = _load_trusted_codebase()
    if len(data['sources']) >= _TRUSTED_CODEBASE_MAX:
        return JSONResponse({'error': 'registry full; ask the operator to '
                            'prune'}, status_code=409)
    existing = next((e for e in data['sources']
                     if str(e.get('source') or e.get('repo') or '').lower()
                     == source.lower()), None)
    if existing:
        # Re-vouch: append the new voucher instead of duplicating the entry.
        vouchers = existing.setdefault('vouched_by', [existing.get('added_by')])
        if agent_id not in vouchers:
            vouchers.append(agent_id)
        existing['last_vouched_at'] = time.strftime('%Y-%m-%d')
        if declared_mb and not existing.get('declaredSizeMb'):
            existing['declaredSizeMb'] = declared_mb
        _save_trusted_codebase(data)
        log_action(agent_id, 'trusted_codebase_revouch',
                   {'source': source}, authorized=True)
        return JSONResponse({'ok': True, 'revouched': source,
                             'vouched_by': vouchers})
    entry = {'source': source, 'kind': kind, 'purpose': purpose,
             'added_by': agent_id, 'added_at': time.strftime('%Y-%m-%d')}
    if tool:
        entry['tool'] = tool
    if declared_mb:
        entry['declaredSizeMb'] = declared_mb
    data['sources'].append(entry)
    _save_trusted_codebase(data)
    log_action(agent_id, 'trusted_codebase_add',
               {'source': source, 'kind': kind}, authorized=True)
    return JSONResponse({'ok': True, 'added': entry})


# Deprecated alias.
@app.post('/api/trusted-repos')
async def api_trusted_repos_add(request: Request):
    return await api_trusted_codebase_add(request)


seed_trusted_codebase()
