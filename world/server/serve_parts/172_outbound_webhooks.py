
# Outbound webhooks (operator direction, 2026-11-01): the tank RECEIVES
# webhooks (/api/webhook/{channel}) but could not NOTIFY external systems.
# A webhook is one subscribed URL with an optional shared secret; when an
# action lands in the activity log whose kind matches one of the
# subscription's event patterns, the tank POSTs the record to the URL.
#
# Delivery contract (GitHub-style): POST application/json, header
# X-Tank-Event carries the action kind, and when a secret is set the body
# is signed `X-Tank-Signature: sha256=<hex hmac-sha256(body, secret)>` so
# a receiver can authenticate the POST without trusting the transport.
#
# Storage is the settings table (`outbound_webhooks` key) -- operator
# state that survives restarts and never touches git. Secrets stay server
# side: the API returns hasSecret, never the secret itself.
#
# Safety: every URL must resolve to a public host (the SSRF resolution
# primitive from web_helpers -- a webhook pointed at the metadata service
# or a loopback admin surface would be a credential-theft vector driven
# through the player UI). Delivery is one bounded POST, no retries: a
# retry loop on the action-log hot path turns a slow receiver into an
# outage of the logger itself.

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from integrations.web_helpers import _is_safe_public_host

WEBHOOK_TIMEOUT_S = 5
WEBHOOK_BODY_MAX_BYTES = 64 * 1024
WEBHOOK_MAX_FANOUT = 8
WEBHOOK_MAX_PATTERNS = 20
WEBHOOK_SECRET_MAX_CHARS = 256
# Never re-emit our own delivery records: an endpoint that echoes the
# event kind back would otherwise recurse through log_action forever.
_WEBHOOK_SELF_KINDS = ('webhook_delivery', 'webhook_delivery_failed')
# The registry is read on the action-log hot path; a small TTL cache keeps
# the common no-webhooks case at a dict lookup instead of a DB read.
_WEBHOOK_CACHE = {'at': 0.0, 'items': []}


def _outbound_webhooks():
    """The webhook registry (settings key `outbound_webhooks`), with a 5s
    TTL cache: log_action fires per action, so the empty case must be cheap."""
    now = time.time()
    if now - _WEBHOOK_CACHE['at'] < 5:
        return _WEBHOOK_CACHE['items']
    try:
        raw = json.loads(_get_setting('outbound_webhooks', '[]') or '[]')
        items = raw if isinstance(raw, list) else []
    except Exception:
        items = []
    _WEBHOOK_CACHE['at'] = now
    _WEBHOOK_CACHE['items'] = items
    return items


def _save_outbound_webhooks(items):
    _set_setting('outbound_webhooks', json.dumps(items, ensure_ascii=False))
    _WEBHOOK_CACHE['at'] = time.time()
    _WEBHOOK_CACHE['items'] = list(items)


def _webhook_next_id(items):
    top = 0
    for item in items:
        m = (item.get('id') or '').split('-')
        if len(m) == 2 and m[0] == 'wh':
            try:
                top = max(top, int(m[1]))
            except ValueError:
                continue
    return f'wh-{top + 1}'


def _event_matches(patterns, kind):
    """A subscription fires when ANY of its event patterns matches the
    action kind. Patterns are case-insensitive substrings; a bare '*'
    matches everything. Empty pattern lists match nothing -- a webhook
    with no events is stored but silent, which is the safer default than
    firehosing every action to a new subscriber."""
    kind_l = (kind or '').lower()
    for pattern in patterns or []:
        p = (pattern or '').strip().lower()
        if p == '*' or (p and p in kind_l):
            return True
    return False


def _webhook_target_error(url):
    """(url, None) when the target is a public http(s) host; (None, reason)
    otherwise. The SSRF check runs at registration AND at every delivery:
    DNS is not stable, so a hostname that was public yesterday may not be."""
    url = (url or '').strip()
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None, 'url does not parse'
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None, 'webhook url must be a real http(s) url'
    host = (parsed.hostname or '').strip().lower()
    if not host or not _is_safe_public_host(host):
        return None, 'webhook url must resolve to a public host (no loopback, private, or link-local targets)'
    return url, None


def _webhook_body(agent_id, action, details):
    payload = {'kind': action, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'agent': agent_id, 'details': details if isinstance(details, (dict, list)) else None}
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if len(body) > WEBHOOK_BODY_MAX_BYTES:
        payload['details'] = {'truncated': True}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return body


def _deliver_webhook(record, agent_id, action, details):
    """One bounded POST. Returns (True, status) or (False, reason)."""
    url, err = _webhook_target_error(record.get('url'))
    if err:
        return False, err
    body = _webhook_body(agent_id, action, details)
    secret = (record.get('secret') or '')
    headers = {'Content-Type': 'application/json',
               'X-Tank-Event': action,
               'User-Agent': 'think-tank-webhook/1'}
    if secret:
        digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
        headers['X-Tank-Signature'] = 'sha256=' + digest
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_S) as resp:
            return True, resp.status
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}'
    except Exception as e:
        return False, (str(e) or e.__class__.__name__)[:200]


def _outbound_webhook_emit(agent_id, action, details):
    """Fan one action-log record out to every enabled webhook subscribed to
    its kind. Called from log_action behind a try/except -- a webhook can
    never break logging. Delivery results are logged back through
    log_action under self-kinds the emit skips, so a receiver cannot loop
    the tank into itself. Bounded fan-out and a short timeout."""
    if action in _WEBHOOK_SELF_KINDS:
        return
    items = [w for w in _outbound_webhooks() if w.get('enabled')]
    if not items:
        return
    matched = [w for w in items if _event_matches(w.get('events'), action)]
    for record in matched[:WEBHOOK_MAX_FANOUT]:
        ok, note = _deliver_webhook(record, agent_id, action, details)
        log_action('server', 'webhook_delivery' if ok else 'webhook_delivery_failed',
                   {'id': record.get('id'), 'url': record.get('url'), 'note': note},
                   authorized=True)


# --- player/admin HTTP surface ----------------------------------------------

@app.get('/api/webhooks')
async def list_webhooks_endpoint(request: Request):
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    items = [{'id': w.get('id'), 'url': w.get('url'), 'events': w.get('events') or [],
              'enabled': bool(w.get('enabled')), 'hasSecret': bool(w.get('secret')),
              'createdAt': w.get('createdAt')} for w in _outbound_webhooks()]
    return JSONResponse({'webhooks': items})


@app.post('/api/webhooks')
@_state_writer
async def register_webhook_endpoint(request: Request):
    """Register an outbound webhook (player or admin agent).
    Body: {url, secret?, events: ['card_report', ...], enabled?}."""
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
    if actor != 'player' and not _agent_is_admin(actor, state):
        return JSONResponse({'error': 'only the player or an admin agent can register webhooks'}, status_code=403)
    url, err = _webhook_target_error(body.get('url'))
    if err:
        return JSONResponse({'error': err}, status_code=400)
    events = body.get('events') or []
    if not isinstance(events, list) or len(events) > WEBHOOK_MAX_PATTERNS:
        return JSONResponse({'error': f'events must be a list of at most {WEBHOOK_MAX_PATTERNS} event patterns'}, status_code=400)
    events = [str(e).strip()[:120] for e in events if str(e).strip()]
    secret = (body.get('secret') or '')
    if len(secret) > WEBHOOK_SECRET_MAX_CHARS:
        return JSONResponse({'error': 'secret too long'}, status_code=400)
    items = _outbound_webhooks()
    record = {'id': _webhook_next_id(items), 'url': url,
              'secret': secret or None, 'events': events, 'enabled': body.get('enabled', True),
              'createdAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'createdBy': actor or 'player'}
    items.append(record)
    _save_outbound_webhooks(items)
    save_state_to_db(state)
    log_action(actor or 'player', 'webhook_registered',
               {'id': record['id'], 'events': events}, authorized=True)
    return JSONResponse({'ok': True, 'webhook': {'id': record['id'], 'url': url,
                                                 'events': events, 'hasSecret': bool(secret)}})


@app.post('/api/webhooks/{webhook_id}/test')
@_state_writer
async def test_webhook_endpoint(webhook_id: str, request: Request):
    """Deliver a sample event so the operator can verify a receiver end to
    end. The sample carries the real signature path; a 2xx means the
    receiver accepted the POST."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if actor != 'player' and not _agent_is_admin(actor, state):
        return JSONResponse({'error': 'only the player or an admin agent can test webhooks'}, status_code=403)
    record = next((w for w in _outbound_webhooks() if w.get('id') == webhook_id), None)
    if not record:
        return JSONResponse({'error': 'unknown webhook'}, status_code=404)
    ok, note = _deliver_webhook(record, actor or 'player', 'webhook_test',
                                {'sample': True, 'webhookId': webhook_id})
    save_state_to_db(state)
    log_action(actor or 'player', 'webhook_delivery' if ok else 'webhook_delivery_failed',
               {'id': webhook_id, 'url': record.get('url'), 'note': note}, authorized=True)
    return JSONResponse({'ok': ok, 'note': note}, status_code=200 if ok else 502)


@app.delete('/api/webhooks/{webhook_id}')
@_state_writer
async def delete_webhook_endpoint(webhook_id: str, request: Request):
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    if actor != 'player' and not _agent_is_admin(actor, state):
        return JSONResponse({'error': 'only the player or an admin agent can delete webhooks'}, status_code=403)
    items = _outbound_webhooks()
    kept = [w for w in items if w.get('id') != webhook_id]
    if len(kept) == len(items):
        return JSONResponse({'error': 'unknown webhook'}, status_code=404)
    _save_outbound_webhooks(kept)
    save_state_to_db(state)
    log_action(actor or 'player', 'webhook_deleted', {'id': webhook_id}, authorized=True)
    return JSONResponse({'ok': True})
