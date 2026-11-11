# Credential health (2026-11-11): an expired, revoked, or otherwise unusable
# API key used to die silently -- every task touching that service failed,
# the failure was buried in action_log, and nobody was told until an agent
# complained or a delivery slipped. This part closes that gap at the two
# places credentials actually meet the network:
#
#   - /api/curl with a capability handle (server/serve_parts/360_curl.py),
#   - /api/api-call + curated executors (server/serve_parts/350_api_channel_execute.py).
#
# The rule: a 401/403 from the EXTERNAL service while carrying a credential
# is an auth failure for that credential; any other response (including a
# 500, which means the request got THROUGH auth) is a success. Consecutive
# failures accumulate in credential_health; at
# CREDENTIAL_HEALTH_NOTIFY_AFTER the player is alerted once on the standing
# health-alert channels (email + Telegram via _persist/_push_new_health_alerts,
# which dedup on (category, severity)), and re-alerted every
# CREDENTIAL_HEALTH_RENOTIFY_S only while the streak continues. A success or
# a manual clear resets the streak.

import time

CREDENTIAL_HEALTH_NOTIFY_AFTER = 2
CREDENTIAL_HEALTH_RENOTIFY_S = 6 * 3600

_CREDENTIAL_AUTH_FAILURE_STATUSES = (401, 403)


def _credential_health_label(kind, name):
    """Stable per-credential label: 'vault:<row name>' or 'env:<KEY name>'."""
    return f'{kind}:{name}'


def _credential_note_auth_result(kind, name, status, detail=''):
    """Record one credentialed external-call outcome. A 401/403 from the
    service means the credential itself is failing auth; anything else means
    the request got through (even a 5xx is 'the key works, the service is
    sick'). Never raises -- notification is best-effort, the original caller
    already has its own error path."""
    try:
        label = _credential_health_label(kind, name)
        now = time.time()
        ok = status not in _CREDENTIAL_AUTH_FAILURE_STATUSES
        with _db() as conn:
            row = conn.execute(
                'SELECT consecutive_failures, notified_at FROM credential_health '
                'WHERE label = ?', (label,)).fetchone()
            streak = int(row[0]) if row else 0
            notified_at = row[1] if row else None
            if ok:
                if not row:
                    return
                if streak == 0:
                    return
                conn.execute(
                    'UPDATE credential_health SET consecutive_failures = 0, '
                    'first_failed_at = NULL, last_detail = \'\', notified_at = NULL '
                    'WHERE label = ?', (label,))
                return
            streak += 1
            should_notify = (
                streak >= CREDENTIAL_HEALTH_NOTIFY_AFTER
                and (notified_at is None
                     or now - notified_at >= CREDENTIAL_HEALTH_RENOTIFY_S))
            if not row:
                conn.execute(
                    'INSERT INTO credential_health (label, kind, service, '
                    'consecutive_failures, first_failed_at, last_failed_at, '
                    'last_detail, notified_at, total_failures) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)',
                    (label, kind, '', streak, now, now, str(detail)[:300],
                     now if should_notify else None))
            else:
                conn.execute(
                    'UPDATE credential_health SET consecutive_failures = ?, '
                    'last_failed_at = ?, last_detail = ?, notified_at = CASE '
                    'WHEN ? THEN ? ELSE notified_at END, '
                    'total_failures = total_failures + 1 WHERE label = ?',
                    (streak, now, str(detail)[:300], should_notify, now, label))
        if should_notify:
            _credential_health_alert(kind, name, streak, detail)
    except Exception as exc:  # noqa: BLE001 -- health tracking must never break the caller
        try:
            print(f'[credential-health] tracking error: {exc}', flush=True)
        except Exception:
            pass


def _credential_note_auth_success(kind, name):
    """Convenience wrapper: any non-auth-failure outcome."""
    _credential_note_auth_result(kind, name, 200)


def _credential_health_alert(kind, name, streak, detail=''):
    """Push one standing health alert on BOTH channels (email + Telegram),
    through the exact dedup machinery the 5-minute health loop uses, so a
    dead key can never become a spam firehose. Severity escalates with the
    streak: warning at 2-4 consecutive failures, critical at 5+."""
    severity = 'critical' if streak >= 5 else 'warning'
    message = (
        f'Credential {name} ({kind}) has failed external authentication '
        f'{streak} consecutive times. Every task that touches this service '
        f'is failing right now.\n\n'
        f'Last response: {str(detail)[:200] or "(no detail)"}\n\n'
        f'Fix: store a fresh key (vault row via /api/credentials, or the '
        f'matching .env key), then POST /api/credentials/health/clear with '
        f'{{"label": "{kind}:{name}"}} -- or just let the next successful '
        f'call clear the streak automatically.')
    alerts = [{'category': 'credential', 'severity': severity, 'message': message}]
    try:
        persisted = _persist_new_health_alerts(alerts)
    except Exception:
        persisted = alerts
    try:
        _push_new_health_alerts(persisted or alerts)
    except Exception:
        pass
    try:
        log_action(None, 'credential_health_alert',
                   {'credential': name, 'kind': kind, 'streak': streak,
                    'severity': severity}, authorized=False)
    except Exception:
        pass


def _credential_health_rows():
    """All tracked credentials with a live (non-zero) failure streak,
    newest failure first. Never returns credential values."""
    with _db() as conn:
        rows = conn.execute(
            'SELECT label, kind, service, consecutive_failures, '
            'first_failed_at, last_failed_at, last_detail, notified_at, '
            'total_failures FROM credential_health '
            'WHERE consecutive_failures > 0 ORDER BY last_failed_at DESC').fetchall()
    return [
        {'label': r[0], 'kind': r[1], 'service': r[2],
         'consecutiveFailures': r[3], 'firstFailedAt': r[4],
         'lastFailedAt': r[5], 'lastDetail': r[6], 'notifiedAt': r[7],
         'totalFailures': r[8]}
        for r in rows
    ]


@app.get('/api/credentials/health')
async def credentials_health(request: Request):
    """Operator view of credential health: which keys are failing external
    auth right now, how hard, and when the player was last pinged. Player or
    director/admin only -- streak details are ops surface, not agent toy."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return JSONResponse({'credentials': _credential_health_rows(),
                         'notifyAfter': CREDENTIAL_HEALTH_NOTIFY_AFTER,
                         'renotifySeconds': CREDENTIAL_HEALTH_RENOTIFY_S})


@app.post('/api/credentials/health/clear')
async def credentials_health_clear(request: Request):
    """Manual clear after the human replaces a key. Automatic reset still
    happens on the next successful call; this just silences a streak the
    operator already dealt with."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    label = str((body or {}).get('label') or '').strip()
    if not label:
        return JSONResponse({'error': 'label is required'}, status_code=400)
    with _db() as conn:
        conn.execute(
            'UPDATE credential_health SET consecutive_failures = 0, '
            'first_failed_at = NULL, last_detail = \'\', notified_at = NULL '
            'WHERE label = ?', (label,))
    log_action(actor, 'credential_health_cleared', {'label': label}, authorized=True)
    return JSONResponse({'ok': True})
