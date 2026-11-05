"""Trusted GitHub repository registry (2026-11-05).

Owner directive: the tank RUNS vetted upstream repositories instead of
hard-coding their logic into the tree, and agents maintain the vetting
list themselves. This module is that registry:

  GET  /api/trusted-repos                 the list (player or agent key)
  POST /api/trusted-repos                 an agent ADDS a repo (agent key)

A registry entry records who vouched for the repo and what the tank uses
it for; execution sites (e.g. the X adapter) consult this list before
running a tool and refuse anything unlisted. Storage is a single JSON
file (THINK_TANK_DIR/trusted_repos.json, 0600) written atomically, the
same convention as escalations.json.

Agent adds are governed but not gated on approval: any authenticated
agent may add a repo with a stated purpose; the entry records the
adder, and the operator can remove entries (there is deliberately no
agent-side delete -- disputes go to the chief of staff, who escalates).
Validation: GitHub slug shape, dedupe by slug (first entry wins, later
adds append a re-vouch note), bounded sizes.
"""
import json
import os
import re
import time

_TRUSTED_REPOS_PATH = os.path.join(THINK_TANK_DIR, 'trusted_repos.json')
_TRUSTED_REPOS_MAX = 200
_TRUSTED_SLUG_RE = re.compile(r'^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$')
_TRUSTED_KINDS = ('pip-cli', 'clone-run', 'container')


def _load_trusted_repos():
    try:
        with open(_TRUSTED_REPOS_PATH) as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get('repos'), list):
            return data
    except Exception:
        pass
    return {'repos': []}


def _save_trusted_repos(data):
    os.makedirs(THINK_TANK_DIR, exist_ok=True)
    tmp = _TRUSTED_REPOS_PATH + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=1)
        fh.write('\n')
    os.replace(tmp, _TRUSTED_REPOS_PATH)
    try:
        os.chmod(_TRUSTED_REPOS_PATH, 0o600)
    except OSError:
        pass


def seed_trusted_repos():
    """First-boot seed: the repositories the operator already vetted by
    using them. Kept idempotent -- never clobbers agent additions."""
    data = _load_trusted_repos()
    have = {r.get('repo', '').lower() for r in data['repos']}
    for entry in (
        {'repo': 'vladkens/twscrape', 'kind': 'pip-cli', 'tool': 'twscrape',
         'purpose': 'X/Twitter GraphQL reader (search, tweets, timelines); '
                    'runs against the tank session pool at '
                    'x_scrape/accounts.db. Replaces the Apify actor lane '
                    'per the owner directive of 2026-11-05.',
         'added_by': 'operator', 'added_at': '2026-11-05'},
    ):
        if entry['repo'].lower() not in have:
            data['repos'].append(entry)
    if data['repos']:
        _save_trusted_repos(data)


def _trusted_auth(request):
    """Returns the authenticated principal or None."""
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    if agent_id and verify_agent_key(agent_id, key) is True:
        return agent_id
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return 'player'
    return None


@app.get('/api/trusted-repos')
async def api_trusted_repos_get(request: Request):
    principal = _trusted_auth(request)
    if not principal:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(principal):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    data = _load_trusted_repos()
    log_action(principal, 'trusted_repos_read',
               {'count': len(data['repos'])}, authorized=True)
    return JSONResponse({'repos': data['repos']})


@app.post('/api/trusted-repos')
async def api_trusted_repos_add(request: Request):
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
    repo = str(body.get('repo') or '').strip()
    purpose = str(body.get('purpose') or '').strip()
    kind = str(body.get('kind') or 'pip-cli').strip()
    tool = str(body.get('tool') or '').strip()
    if not _TRUSTED_SLUG_RE.match(repo):
        return JSONResponse(
            {'error': 'repo must be an owner/name GitHub slug'},
            status_code=400)
    if kind not in _TRUSTED_KINDS:
        return JSONResponse({'error': f'kind must be one of {sorted(_TRUSTED_KINDS)}'},
                            status_code=400)
    if not purpose or len(purpose) > 500:
        return JSONResponse(
            {'error': 'purpose required (1-500 chars) -- say what the tank '
                      'uses the repo for'}, status_code=400)
    if len(tool) > 60:
        return JSONResponse({'error': 'tool name too long'}, status_code=400)
    data = _load_trusted_repos()
    if len(data['repos']) >= _TRUSTED_REPOS_MAX:
        return JSONResponse({'error': 'registry full; ask the operator to '
                            'prune'}, status_code=409)
    existing = next((r for r in data['repos']
                     if r.get('repo', '').lower() == repo.lower()), None)
    if existing:
        # Re-vouch: append the new voucher instead of duplicating the entry.
        vouchers = existing.setdefault('vouched_by', [existing.get('added_by')])
        if agent_id not in vouchers:
            vouchers.append(agent_id)
        existing['last_vouched_at'] = time.strftime('%Y-%m-%d')
        _save_trusted_repos(data)
        log_action(agent_id, 'trusted_repo_revouch',
                   {'repo': repo}, authorized=True)
        return JSONResponse({'ok': True, 'revouched': repo,
                             'vouched_by': vouchers})
    entry = {'repo': repo, 'kind': kind, 'purpose': purpose,
             'added_by': agent_id, 'added_at': time.strftime('%Y-%m-%d')}
    if tool:
        entry['tool'] = tool
    data['repos'].append(entry)
    _save_trusted_repos(data)
    log_action(agent_id, 'trusted_repo_add', {'repo': repo, 'kind': kind},
               authorized=True)
    return JSONResponse({'ok': True, 'added': entry})


seed_trusted_repos()
