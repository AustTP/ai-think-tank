"""X/Twitter research capability for the tank (2026-11-05).

DESIGN RULE (owner directive): the tank RUNS vetted upstream tools; it does
not re-implement their logic in-tree. The X reader here is a THIN adapter
around the `twscrape` CLI from the trusted repository vladkens/twscrape
(see GET /api/trusted-repos). This module contributes only:

  - credential custody: the operator's X session cookies (.env
    X_SESSION_AUTH_TOKEN / X_SESSION_CT0) are seeded ONCE into a 0700/0600
    twscrape pool database under THINK_TANK_DIR/x_scrape. Agents never see
    them, matching constitution rule 6.
  - a subprocess boundary: every call is `twscrape --db <pool> <verb>`,
    fixed argv, no shell, hard timeout. Upstream owns the GraphQL details;
    updating the tool never touches tank code.
  - the HTTP surface agents need: agent-key auth, result caps, audit rows,
    and a 429-with-retry-time when the shared account is rate-locked.

  GET /api/x/search?query=...&limit=20     search (Latest), cap 50
  GET /api/x/tweet/{tid}                   one tweet by id
  GET /api/x/user/{login}/tweets?limit=50  a user's recent posts (cap 100)

If the tool is not on the trusted repository list, or not installed, the
endpoints say so (503) instead of degrading into in-tree scraping code.
"""
import json
import os
import shutil
import subprocess
import time

_X_POOL_DIR = os.path.join(THINK_TANK_DIR, 'x_scrape')
_X_POOL_DB = os.path.join(_X_POOL_DIR, 'accounts.db')
_X_TOOL = 'twscrape'
_X_TRUSTED_REPO = 'vladkens/twscrape'
_X_CALL_WAIT_S = 90.0
_X_SEARCH_CAP = 50
_X_USER_CAP = 100
_X_PROC_TIMEOUT_S = 120.0
_X_SEARCH_MAX_ITEMS = 2000


def _trusted_repos():
    """The trusted-repository registry (187_trusted_repos). A tool runs only
    when its repo is on the list; an unreadable registry fails closed."""
    try:
        with open(os.path.join(THINK_TANK_DIR, 'trusted_repos.json')) as fh:
            data = json.load(fh)
        repos = {r.get('repo', '').lower() for r in data.get('repos', [])}
    except Exception:
        return set()
    return repos


def _x_tool_path():
    """The trusted tool's CLI binary, or None. Located through PATH like any
    other installed tool; the tank pins provenance through the trusted-repos
    registry, not through vendored code."""
    return shutil.which(_X_TOOL)


def _x_ready():
    """(ok, error_response_builder) -- shared preconditions: tool trusted,
    tool installed, pool seeded from .env."""
    if _X_TRUSTED_REPO not in _trusted_repos():
        return None, ('tool repo not trusted; ask an agent with repo access '
                      f'to add {_X_TRUSTED_REPO} to /api/trusted-repos')
    if _x_tool_path() is None:
        return None, (f'{_X_TOOL} CLI not installed on the server '
                      '(pip install twscrape)')
    marker = os.path.join(_X_POOL_DIR, 'seeded')
    if not os.path.exists(marker):
        auth = (_load_env() or {}).get('X_SESSION_AUTH_TOKEN', '')
        ct0 = (_load_env() or {}).get('X_SESSION_CT0', '')
        if not auth or not ct0:
            return None, 'X_SESSION_AUTH_TOKEN / X_SESSION_CT0 missing from .env'
        os.makedirs(_X_POOL_DIR, exist_ok=True)
        try:
            os.chmod(_X_POOL_DIR, 0o700)
        except OSError:
            pass
        cmd = [_x_tool_path(), '--db', _X_POOL_DB, 'add_cookie',
               'operator_session', f'auth_token={auth}; ct0={ct0}']
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
        except Exception as exc:
            return None, f'seed failed: {type(exc).__name__}'
        if proc.returncode != 0:
            return None, 'seed failed (tool refused the cookies)'
        with open(marker, 'w') as fh:
            fh.write(str(int(time.time())))
        for path in (_X_POOL_DB, marker):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    return True, None


def _x_run_cli(verb_args, wait_s):
    """Run the tool CLI with fixed argv on the server's pool and parse its
    JSONL stdout. Timeouts surface as TimeoutError -> 429 upstream."""
    deadline = time.time() + wait_s
    out_lines = []
    proc = subprocess.Popen(
        [_x_tool_path(), '--db', _X_POOL_DB] + verb_args,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            out_lines.append(line)
            if len(out_lines) >= _X_SEARCH_MAX_ITEMS:
                proc.kill()
                break
            if time.time() > deadline:
                proc.kill()
                raise TimeoutError('x rate window: account rate-locked')
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise TimeoutError('x call timed out')
    rows = []
    for line in out_lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _x_strip_tweet(d):
    """Compact, bounded projection for agent consumption."""
    user = d.get('user') or {}
    return {
        'id': d.get('id_str') or (str(d.get('id')) if d.get('id') else None),
        'date': d.get('date'),
        'text': (d.get('rawContent') or '')[:2000],
        'username': user.get('username'),
        'likes': d.get('likeCount'), 'views': d.get('viewCount'),
        'replies': d.get('replyCount'), 'rts': d.get('retweetCount'),
        'inReplyTo': d.get('inReplyToTweetIdStr') or (
            str(d.get('inReplyToTweetId')) if d.get('inReplyToTweetId') else None),
        'conversationId': d.get('conversationIdStr') or (
            str(d.get('conversationId')) if d.get('conversationId') else None),
        'links': [l.get('url') for l in (d.get('links') or []) if l.get('url')],
        'url': d.get('url'),
    }


def _x_gate(request, agent_id, kind, detail):
    """Shared auth + rate-limit + audit preamble. Returns a JSONResponse on
    rejection, else None."""
    if not check_rate_limit(agent_id):
        log_action(agent_id, 'rate_limited', {'endpoint': kind})
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    authorized = (verify_agent_key(agent_id, request.headers.get('X-Agent-Key'))
                  is True) or bool(verify_session(
                      request.cookies.get(SESSION_COOKIE_NAME)))
    if not authorized:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    log_action(agent_id, kind, detail, authorized=authorized)
    return None


def _x_search(query, limit):
    rows = _x_run_cli(['search', query, '--limit', str(limit)],
                      _X_CALL_WAIT_S + 30)
    return [_x_strip_tweet(r) for r in rows if r.get('id_str')][:limit]


def _x_tweet(tid):
    rows = _x_run_cli(['tweet_details', str(tid)], _X_CALL_WAIT_S)
    d = rows[0] if rows else None
    return _x_strip_tweet(d) if d else None


def _x_user_tweets(login, limit):
    # The CLI's user_tweets takes a user id: resolve the handle first.
    who = _x_run_cli(['user_by_login', login], 60)
    uid = (who[0].get('id_str') or str(who[0].get('id'))) if who else None
    if not uid:
        return None
    rows = _x_run_cli(['user_tweets', uid, '--limit', str(limit)],
                      _X_CALL_WAIT_S + 30)
    return [_x_strip_tweet(r) for r in rows if r.get('id_str')][:limit]


@app.get('/api/x/search')
async def api_x_search(request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key')) or 'player'
    q = (request.query_params.get('query') or '').strip()
    try:
        limit = min(max(int(request.query_params.get('limit') or 20), 1),
                    _X_SEARCH_CAP)
    except ValueError:
        return JSONResponse({'error': 'malformed limit'}, status_code=400)
    if not q or len(q) > 500:
        return JSONResponse({'error': 'query required (1-500 chars)'},
                            status_code=400)
    blocked = _x_gate(request, agent_id, 'x_search',
                      {'query': q, 'limit': limit})
    if blocked:
        return blocked
    ok, err = _x_ready()
    if not ok:
        return JSONResponse({'error': err}, status_code=503)
    try:
        rows = _x_search(q, limit)
    except TimeoutError as exc:
        return JSONResponse({'error': str(exc)}, status_code=429)
    except Exception as exc:
        return JSONResponse(
            {'error': f'x search failed: {type(exc).__name__}'},
            status_code=502)
    return JSONResponse({'tweets': rows, 'count': len(rows)})


@app.get('/api/x/tweet/{tid}')
async def api_x_tweet(request: Request, tid: str):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key')) or 'player'
    if not tid.isdigit() or not (10 <= len(tid) <= 25):
        return JSONResponse({'error': 'malformed tweet id'}, status_code=400)
    blocked = _x_gate(request, agent_id, 'x_tweet', {'tweetId': tid})
    if blocked:
        return blocked
    ok, err = _x_ready()
    if not ok:
        return JSONResponse({'error': err}, status_code=503)
    try:
        tw = _x_tweet(tid)
    except TimeoutError as exc:
        return JSONResponse({'error': str(exc)}, status_code=429)
    except Exception as exc:
        return JSONResponse(
            {'error': f'x fetch failed: {type(exc).__name__}'},
            status_code=502)
    if tw is None:
        return JSONResponse({'error': 'no such tweet'}, status_code=404)
    return JSONResponse({'tweet': tw})


@app.get('/api/x/user/{login}/tweets')
async def api_x_user_tweets(request: Request, login: str):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key')) or 'player'
    if not login or len(login) > 40 or not login.replace('_', '').isalnum():
        return JSONResponse({'error': 'malformed handle'}, status_code=400)
    try:
        limit = min(max(int(request.query_params.get('limit') or 50), 1),
                    _X_USER_CAP)
    except ValueError:
        return JSONResponse({'error': 'malformed limit'}, status_code=400)
    blocked = _x_gate(request, agent_id, 'x_user_tweets',
                      {'login': login, 'limit': limit})
    if blocked:
        return blocked
    ok, err = _x_ready()
    if not ok:
        return JSONResponse({'error': err}, status_code=503)
    try:
        rows = _x_user_tweets(login, limit)
    except TimeoutError as exc:
        return JSONResponse({'error': str(exc)}, status_code=429)
    except Exception as exc:
        return JSONResponse(
            {'error': f'x fetch failed: {type(exc).__name__}'},
            status_code=502)
    if rows is None:
        return JSONResponse({'error': 'no such user'}, status_code=404)
    return JSONResponse({'tweets': rows, 'count': len(rows)})
