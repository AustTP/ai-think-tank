"""Operator-grade failure surface for the tank (2026-11-05).

The escalation flood of 2026-11-04 burned the directors for half a day while
real, fixable failure patterns (classifier unavailability blocking lint, a
research card stalling on free-pool timeouts) went unreported to anyone who
could route a fix. Root cause: directors only ever SAW escalations -- the
aggregate failure surface existed in action_log and health_alerts but no
surface aggregated it for them.

GET /api/ops/failures?hours=12 returns, for the window:

  - execute_classify blocks grouped by reason prefix (the flood signature,
    classifier unavailability, per-command head);
  - classifier-unavailable event count (the free-pool reliability signal);
  - tool/API failures by endpoint (5xx, timeouts);
  - open health alerts, newest first (capped).

Read-only: one SELECT family over action_log + health_alerts. Auth is
NARROW on purpose (the regional VP's village scan, the player, and admin):
the data itself is aggregate counts and sanitized reason strings, not
payloads, but the failure surface is a leadership instrument, not an
agent toy.
"""

import time
from collections import Counter

_OPS_DEFAULT_HOURS = 12
_OPS_MAX_HOURS = 168
_OPS_ROW_CAP = 5000


def _ops_failures(hours):
    """Aggregate the failure surface for the last `hours` hours. Pure read;
    returns a JSON-able dict. Unknown shapes fail soft to empty groups."""
    cutoff = time.time() - hours * 3600
    out = {'windowHours': hours, 'executeBlocks': [],
           'classifierUnavailable': 0, 'failedActions': 0, 'healthAlerts': []}
    with _db() as conn:
        rows = conn.execute(
            "SELECT details FROM action_log WHERE action='execute_classify' "
            "AND ts >= ? LIMIT ?", (cutoff, _OPS_ROW_CAP)).fetchall()
        reasons = Counter()
        for (details,) in rows:
            try:
                d = json.loads(details)
            except Exception:
                continue
            if d.get('decision') != 'blocked':
                continue
            why = str(d.get('reason') or '')
            if 'classifier unavailable' in why:
                out['classifierUnavailable'] += 1
                continue
            head = ' '.join(str(d.get('target') or '').split()[:3])
            reasons[f'{head} :: {why[:80]}'] += 1
        out['executeBlocks'] = sorted(
            ({'pattern': k, 'count': v} for k, v in reasons.items()),
            key=lambda x: -x['count'])[:20]
        n = conn.execute(
            "SELECT count(*) FROM action_log WHERE ts >= ? AND "
            "(action LIKE '%_failed' OR action LIKE '%error%' OR "
            "action LIKE '%timeout%')",
            (cutoff,)).fetchone()[0]
        out['failedActions'] = n
        alerts = conn.execute(
            "SELECT id, category, severity, message, ts FROM health_alerts "
            "WHERE ts >= ? ORDER BY ts DESC LIMIT 20", (cutoff,)).fetchall()
        for aid, cat, sev, msg, ts in alerts:
            out['healthAlerts'].append(
                {'id': aid, 'category': cat, 'severity': sev,
                 'message': str(msg or '')[:200],
                 'ageHours': round((time.time() - (ts or 0)) / 3600, 1)})
    return out


@app.get('/api/ops/failures')
async def api_ops_failures(request: Request):
    hours = int(request.query_params.get('hours') or _OPS_DEFAULT_HOURS)
    hours = max(1, min(hours, _OPS_MAX_HOURS))
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    is_agent = agent_id and verify_agent_key(agent_id, key) is True
    is_player = bool(verify_session(request.cookies.get(SESSION_COOKIE_NAME)))
    allowed = is_player
    if is_agent:
        try:
            with _db() as conn:
                row = conn.execute(
                    'SELECT blob FROM roster_agent WHERE id = ?',
                    (agent_id,)).fetchone()
            blob = json.loads(row[0]) if row else {}
        except Exception:
            blob = {}
        allowed = allowed or blob.get('role') in ('regional_vp',
                                                  'Chief of Staff') \
            or blob.get('isAdmin')
    if not allowed:
        return JSONResponse(
            {'error': 'the village scan is the regional VP instrument; '
                      'directors use /api/ops/cards for their span'},
            status_code=403)
    authorized = is_player or is_agent
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        surface = await asyncio.to_thread(_ops_failures, hours)
    except Exception as exc:
        log_action(agent_id, 'ops_failures_error',
                   {'error': type(exc).__name__}, authorized=authorized)
        return JSONResponse({'error': 'failure-surface query failed'},
                            status_code=502)
    log_action(agent_id, 'ops_failures_read',
               {'hours': hours, 'blockedPatterns':
                    len(surface.get('executeBlocks') or [])},
               authorized=authorized)
    return JSONResponse(surface)
