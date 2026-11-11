# Habit Link (2026-11-11): the owner's own browsing, as a signal the tank
# may LEARN from without ever seeing page content. A paired browser
# extension (world/extensions/habit-link/) batches per-page metadata --
# host, path, title, dwell time, scroll depth, click count -- to
# POST /api/habit-link. Metadata only, and only what the extension itself
# chose to send; the server never fetches the page, reads history files, or
# touches cookies. Why it has value even though agents already browse: the
# tank's own browsing is agent-driven (Playwright in sandboxes) and the
# owner's real usage is a DIFFERENT, richer signal -- which sites the owner
# personally returns to, daily, is the strongest standing evidence that a
# host deserves agent reach.
#
# Three independent off switches, in order of ease:
#   1. Extension popup: Pause (also per-site list in options).
#   2. POST /api/habit-link/toggle {"enabled": false} -- live, persists in
#      the settings table across restarts.
#   3. HABIT_LINK_ENABLED=0 in .env -- hard off at the server.
# With any of them set, the endpoint refuses and the extension backs off.
#
# Auth: a shared token (HABIT_LINK_TOKEN in .env) the owner pastes into the
# extension's options page -- same fail-closed pattern as SMS_FORWARD_SECRET.
# Unset token means the whole feature is off at the server (501).

import datetime
import time
import urllib.parse

HABIT_LINK_MAX_EVENTS = 100
HABIT_LINK_MAX_HOSTS_SEEN = 2000
HABIT_LINK_TOKEN_HEADER = 'X-Habit-Link-Token'
# Allowlist-candidate thresholds: a host the owner personally returns to on
# 3+ distinct days with 10+ total page-hits is the same "repeatedly and
# confidently used" signal record_browse_success escalates on for agents --
# so it feeds the same human decision: approve it into the runtime
# allowlist, or leave agents to the per-request Jev gate.
HABIT_LINK_ALLOWLIST_MIN_HITS = 10
HABIT_LINK_ALLOWLIST_MIN_DAYS = 3

_HABIT_LINK_ENABLED_SETTING = 'habit_link_enabled'


def _habit_link_env_enabled():
    return (_load_env().get('HABIT_LINK_ENABLED') or '1').strip() != '0'


def _habit_link_live_enabled():
    return _get_setting(_HABIT_LINK_ENABLED_SETTING, '1') != '0'


def _habit_link_enabled():
    return _habit_link_env_enabled() and _habit_link_live_enabled()


def _habit_link_token():
    return (_load_env().get('HABIT_LINK_TOKEN') or '').strip()


def _habit_link_check(request):
    """(ok, http_response_or_none): configured + enabled + token presented."""
    if not _habit_link_token():
        return False, JSONResponse(
            {'error': 'Habit Link is not configured (set HABIT_LINK_TOKEN '
                      'in .env; the extension pairs via its options page)'},
            status_code=501)
    if not _habit_link_enabled():
        return False, JSONResponse(
            {'error': 'Habit Link is disabled (extension popup, '
                      'POST /api/habit-link/toggle, or HABIT_LINK_ENABLED=0)'},
            status_code=403)
    presented = (request.headers.get(HABIT_LINK_TOKEN_HEADER) or '').strip()
    if not presented or presented != _habit_link_token():
        return False, JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return True, None


def _habit_link_sanitize_event(raw):
    """One extension event -> storage row values, or None. Host is required
    and lower-cased; the query string is ALWAYS stripped (it can carry
    tokens/search terms); only http(s) is accepted; numbers are clamped."""
    if not isinstance(raw, dict):
        return None
    url = str(raw.get('url') or '').strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return None
    host = parsed.hostname.strip().lower()
    if len(host) > 253:
        return None
    path = parsed.path[:300]
    title = ' '.join(str(raw.get('title') or '').split())[:200]
    def _num(value, lo, hi, default=0):
        try:
            return max(lo, min(hi, float(value)))
        except (TypeError, ValueError):
            return default
    dwell_ms = int(_num(raw.get('dwellMs'), 0, 60 * 60 * 1000))
    scroll = _num(raw.get('scrollFrac'), 0.0, 1.0)
    clicks = int(_num(raw.get('clicks'), 0, 100))
    try:
        ts = max(0.0, min(float(raw.get('ts') or time.time()), time.time() + 60))
    except (TypeError, ValueError):
        ts = time.time()
    return {'host': host, 'path': path, 'title': title,
            'dwell_ms': dwell_ms, 'scroll': scroll, 'clicks': clicks, 'ts': ts}


@app.post('/api/habit-link')
async def habit_link_ingest(request: Request):
    """The extension's only write. Accepts a batch of metadata events,
    aggregates them per (host, day). No page content ever arrives here by
    design, and nothing in this endpoint can fetch or browse."""
    ok, err = _habit_link_check(request)
    if not ok:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        return JSONResponse({'error': 'body must be JSON'}, status_code=400)
    events = (body or {}).get('events')
    if not isinstance(events, list):
        return JSONResponse({'error': 'events must be an array'}, status_code=400)
    events = events[:HABIT_LINK_MAX_EVENTS]
    day = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    stored_hosts = set()
    with _db() as conn:
        for raw in events:
            ev = _habit_link_sanitize_event(raw)
            if ev is None:
                continue
            stored_hosts.add(ev['host'])
            conn.execute(
                'INSERT INTO habit_link_days (host, day, hits, dwell_ms, '
                'scroll_sum, scroll_n, clicks, last_ts) VALUES (?, ?, 1, ?, ?, 1, ?, ?) '
                'ON CONFLICT(host, day) DO UPDATE SET hits = hits + 1, '
                'dwell_ms = dwell_ms + excluded.dwell_ms, '
                'scroll_sum = scroll_sum + excluded.scroll_sum, '
                'scroll_n = scroll_n + 1, clicks = clicks + excluded.clicks, '
                'last_ts = MAX(last_ts, excluded.last_ts)',
                (ev['host'], day, ev['dwell_ms'], ev['scroll'],
                 ev['clicks'], ev['ts']))
        for host in stored_hosts:
            conn.execute(
                'INSERT OR IGNORE INTO habit_link_seen_hosts (host, first_seen_at) '
                'VALUES (?, ?)', (host, time.time()))
    for host in stored_hosts:
        _habit_link_maybe_allowlist_candidate(host)
    return JSONResponse({'ok': True, 'stored': len(stored_hosts)})


def _habit_link_host_stats(host):
    """Aggregate day stats for one host: (hits, days_seen, avg_scroll)."""
    with _db() as conn:
        row = conn.execute(
            'SELECT SUM(hits), COUNT(*), AVG(CASE WHEN scroll_n > 0 '
            'THEN scroll_sum / scroll_n ELSE 0 END) FROM habit_link_days '
            'WHERE host = ?', (host,)).fetchone()
    return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0.0)


def _habit_link_maybe_allowlist_candidate(host):
    """The owner's own repeated use is standing evidence a host deserves the
    same allowlist-candidate decision agent browse-trails earn. Fires the
    EXISTING 'allowlist candidate' escalation (same approve flow, same
    runtime-allowlist grant, same director revocability) exactly once per
    host. Skips hosts the tank can already reach."""
    try:
        if host in _effective_allowlist_domains():
            return
        hits, days, _avg = _habit_link_host_stats(host)
        if hits < HABIT_LINK_ALLOWLIST_MIN_HITS or days < HABIT_LINK_ALLOWLIST_MIN_DAYS:
            return
        with _db() as conn:
            row = conn.execute(
                'SELECT candidate_notified_at FROM habit_link_seen_hosts '
                'WHERE host = ?', (host,)).fetchone()
            if row and row[0]:
                return
            conn.execute(
                'UPDATE habit_link_seen_hosts SET candidate_notified_at = ? '
                'WHERE host = ?', (time.time(), host))
        create_escalation(
            'allowlist candidate',
            f'You have personally used {host} on {days} separate days '
            f'({hits} pages, per your Habit Link extension). Approving adds it '
            'to the RUNTIME allowlist (full read+write reachability for '
            'sandboxed scripts, same semantics as every allowlist entry) so '
            'agents can work with the sites you actually use without a '
            'per-request Jev round trip; a director can revoke it later if '
            'its standing changes.',
            on_approve_note=host)
        log_action(None, 'habit_link_allowlist_candidate',
                   {'host': host, 'hits': hits, 'days': days}, authorized=False)
    except Exception:  # noqa: BLE001 -- candidate escalation is best-effort
        pass


@app.get('/api/habit-link/summary')
async def habit_link_summary(request: Request):
    """Operator read: top hosts by usage over the window, plus the current
    toggle state. Player or director/admin only."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        days = max(1, min(int(request.query_params.get('days') or 14), 90))
    except (TypeError, ValueError):
        days = 14
    with _db() as conn:
        rows = conn.execute(
            'SELECT host, SUM(hits), SUM(dwell_ms), SUM(clicks), COUNT(*), '
            'MAX(last_ts) FROM habit_link_days WHERE day >= date(\'now\', ?) '
            'GROUP BY host ORDER BY SUM(hits) DESC LIMIT 50',
            (f'-{days} days',)).fetchall()
        seen = conn.execute(
            'SELECT COUNT(*) FROM habit_link_seen_hosts').fetchone()[0]
    return JSONResponse({
        'enabled': _habit_link_enabled(),
        'envEnabled': _habit_link_env_enabled(),
        'liveEnabled': _habit_link_live_enabled(),
        'windowDays': days,
        'trackedHosts': int(seen or 0),
        'hosts': [
            {'host': r[0], 'hits': int(r[1] or 0), 'dwellMs': int(r[2] or 0),
             'clicks': int(r[3] or 0), 'activeDays': int(r[4] or 0),
             'lastSeen': r[5]}
            for r in rows
        ]})


@app.get('/api/habit-link/status')
async def habit_link_status(request: Request):
    """Extension-facing lightweight status check (token-gated, like ingest,
    NOT the operator gate): tells the service worker whether collection is
    still wanted, so a paused/disabled feature stops the extension's own
    buffering within one poll cycle."""
    ok, err = _habit_link_check(request)
    if not ok:
        return err
    return JSONResponse({'enabled': True})


@app.post('/api/habit-link/toggle')
async def habit_link_toggle(request: Request):
    """The easy master switch the owner asked for: one call (or one button
    in the operator UI) that stops collection at the server regardless of
    what the extension does. Persists in the settings table."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        return JSONResponse({'error': 'body must be JSON'}, status_code=400)
    enabled = (body or {}).get('enabled')
    if not isinstance(enabled, bool):
        return JSONResponse({'error': 'enabled must be true or false'},
                            status_code=400)
    _set_setting(_HABIT_LINK_ENABLED_SETTING, '1' if enabled else '0')
    log_action(actor, 'habit_link_toggled', {'enabled': enabled}, authorized=True)
    return JSONResponse({'ok': True, 'enabled': enabled,
                         'envEnabled': _habit_link_env_enabled()})
