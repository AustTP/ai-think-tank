"""X/Twitter research capability for the tank (2026-11-05).

Replaces the Apify actor lane for X data. The tank now reads X through its
own authenticated session: the operator's X cookies (auth_token + ct0 in
.env as X_SESSION_AUTH_TOKEN / X_SESSION_CT0) are seeded once into a local
twscrape pool database, and agents get three READ-ONLY endpoints. They
never see the session material; the pool does account state and per-endpoint
rate-limit accounting the way the commercial scraper actors do internally.

  GET /api/x/search?query=...&limit=20     search (Latest), limit capped at 50
  GET /api/x/tweet/{tid}                   one tweet by id
  GET /api/x/user/{login}/tweets?limit=50  a user's recent posts (cap 100)

Every call is agent-key (or player-session) authenticated, rate-limited,
and audited through log_action. Because the tank shares ONE account, a
request whose underlying call would wait longer than _X_CALL_WAIT_S for a
rate window returns 429 with the pool's next-available time instead of
holding the tank's worker. Cookies never appear in responses, logs, or
errors; the pool lives in a 0700 directory under THINK_TANK_DIR.

twscrape is optional at import: the module degrades to a 503 with a clear
message when the library is absent, so the server keeps booting.
"""
import asyncio
import json
import os
import threading
import time

_X_POOL_DIR = os.path.join(THINK_TANK_DIR, 'x_scrape')
_X_POOL_DB = os.path.join(_X_POOL_DIR, 'accounts.db')
_X_CALL_WAIT_S = 90.0
_X_SEARCH_CAP = 50
_X_USER_CAP = 100


def _x_scraper():
    try:
        from twscrape import API, AccountsPool
    except Exception:
        return None
    return AccountsPool, API


def _x_seed_pool():
    """Seed the pool from .env exactly once (marker file gates it). Never
    logs or returns the cookie material itself."""
    auth = (_load_env() or {}).get('X_SESSION_AUTH_TOKEN', '')
    ct0 = (_load_env() or {}).get('X_SESSION_CT0', '')
    if not auth or not ct0:
        return False, 'X_SESSION_AUTH_TOKEN / X_SESSION_CT0 missing from .env'
    os.makedirs(_X_POOL_DIR, exist_ok=True)
    try:
        os.chmod(_X_POOL_DIR, 0o700)
    except OSError:
        pass
    marker = os.path.join(_X_POOL_DIR, 'seeded')
    if os.path.exists(marker):
        return True, ''
    AccountsPool, API = _x_scraper()
    if AccountsPool is None:
        return False, 'twscrape not installed'

    async def _seed():
        pool = AccountsPool(_X_POOL_DB)
        await pool.add_account_cookies(
            'operator_session', f'auth_token={auth}; ct0={ct0}')
        with open(marker, 'w') as fh:
            fh.write(str(int(time.time())))
        for path in (_X_POOL_DB, marker):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    try:
        # The endpoints run on the server's event loop, so the seed coroutine
        # goes to the shared background loop; _x_call applies the wait cap.
        _x_call(_seed(), 30)
    except Exception as exc:
        return False, f'seed failed: {type(exc).__name__}'
    with open(marker, 'w') as fh:
        fh.write(str(int(time.time())))
    for path in (_X_POOL_DB, marker):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return True, ''


_x_loop = None
_x_lock = threading.Lock()


def _x_get_loop():
    global _x_loop
    with _x_lock:
        if _x_loop is None or _x_loop.is_closed():
            _x_loop = asyncio.new_event_loop()
            t = threading.Thread(target=_x_loop.run_forever, daemon=True)
            t.start()
    return _x_loop


def _x_call(coro, timeout):
    """Run one twscrape call on the shared loop with a hard wait cap so a
    rate window never holds a tank worker longer than _X_CALL_WAIT_S."""
    fut = asyncio.run_coroutine_threadsafe(
        asyncio.wait_for(coro, timeout=timeout), _x_get_loop())
    try:
        return fut.result(timeout + 5)
    except asyncio.TimeoutError:
        raise TimeoutError('x rate window: pool account still locked')


def _x_strip_tweet(tw):
    """Compact, bounded projection for agent consumption."""
    try:
        d = json.loads(tw.json()) if hasattr(tw, 'json') else dict(tw)
    except Exception:
        d = {}
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


async def _x_search(query, limit):
    AccountsPool, API = _x_scraper()
    api = API(AccountsPool(_X_POOL_DB))
    out = []
    async for tw in api.search(query, limit=limit):
        out.append(_x_strip_tweet(tw))
        if len(out) >= limit:
            break
    return out


async def _x_tweet(tid):
    AccountsPool, API = _x_scraper()
    api = API(AccountsPool(_X_POOL_DB))
    tw = await api.tweet_details(int(tid))
    return _x_strip_tweet(tw) if tw is not None else None


async def _x_user_tweets(login, limit):
    AccountsPool, API = _x_scraper()
    api = API(AccountsPool(_X_POOL_DB))
    user = await api.user_by_login(login)
    if user is None:
        return None
    out = []
    async for tw in api.user_tweets(user.id, limit=limit):
        out.append(_x_strip_tweet(tw))
        if len(out) >= limit:
            break
    return out


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


def _x_ready():
    if _x_scraper() is None:
        return JSONResponse({'error': 'twscrape not installed on the server'},
                            status_code=503)
    ok, why = _x_seed_pool()
    if not ok:
        return JSONResponse({'error': why}, status_code=503)
    return None


@app.get('/api/x/search')
async def api_x_search(request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key')) or 'player'
    q = (request.query_params.get('query') or '').strip()
    limit = min(max(int(request.query_params.get('limit') or 20), 1),
                _X_SEARCH_CAP)
    if not q or len(q) > 500:
        return JSONResponse({'error': 'query required (1-500 chars)'},
                            status_code=400)
    blocked = _x_gate(request, agent_id, 'x_search',
                      {'query': q, 'limit': limit})
    if blocked:
        return blocked
    ready = _x_ready()
    if ready:
        return ready
    try:
        rows = _x_call(_x_search(q, limit), _X_CALL_WAIT_S + 30)
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
    ready = _x_ready()
    if ready:
        return ready
    try:
        tw = _x_call(_x_tweet(tid), _X_CALL_WAIT_S)
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
    limit = min(max(int(request.query_params.get('limit') or 50), 1),
                _X_USER_CAP)
    blocked = _x_gate(request, agent_id, 'x_user_tweets',
                      {'login': login, 'limit': limit})
    if blocked:
        return blocked
    ready = _x_ready()
    if ready:
        return ready
    try:
        rows = _x_call(_x_user_tweets(login, limit), _X_CALL_WAIT_S + 30)
    except TimeoutError as exc:
        return JSONResponse({'error': str(exc)}, status_code=429)
    except Exception as exc:
        return JSONResponse(
            {'error': f'x fetch failed: {type(exc).__name__}'},
            status_code=502)
    if rows is None:
        return JSONResponse({'error': 'no such user'}, status_code=404)
    return JSONResponse({'tweets': rows, 'count': len(rows)})
