"""API service registry, channel routing, credential injection, daily budget.

Extracted from serve.py. This is the CONFIG half of api_call: what services
exist, which hostnames they own, how a credential is injected, and the daily
per-service call allowance. The gates around them -- SSRF, Jev classification,
rate limit, one-strike, page/spend accrual -- stay in serve.py and are never
delegated to a config row. Execution (_api_execute, workflows, the api_call
tool itself) also stays in serve.py because it is coupled to agent auth, spend
and trace propagation.

API_SERVICES_PATH stays in serve.py as the seed-file path; everything here
reaches serve's state through `_serve.X` at call time, so tests that patch
`serve._API_SERVICES` / `serve._API_CHANNELS` / a credential global keep
working and serve.py re-imports these names unchanged.
"""

import datetime
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

_API_SERVICES: dict = {}
_API_SERVICES_LOAD_ERROR = None
# Registry freshness: the api_services table is the operator-editable config,
# so _ensure_api_services_fresh() re-loads _API_SERVICES when the table's
# active-row fingerprint changes. The TTL bounds the cost to one aggregate
# query at most every _API_SERVICES_REFRESH_TTL_S per process; a config edit
# therefore goes live within that window, with no code change and no restart.
_API_SERVICES_REFRESH_TTL_S = 5.0
_API_SERVICES_FINGERPRINT = None
_API_SERVICES_FRESH_CHECK = 0.0

# Read-only CHANNELS: named capabilities (Reddit, Twitter/X, LinkedIn ...) that
# map hostnames to an ORDERED backend chain. Each backend is a registered
# service plus a url_template that rewrites the agent's requested URL ({url})
# into that backend's fetch URL (e.g. Jina Reader's https://r.jina.ai/{url}).
# The first backend that answers wins; the chain falls through on failure.
# Channels are pure config (JSON-only, not DB-backed): they grant no new
# credential on their own -- every backend must be a registered service, and
# the requested host is still SSRF-gated and Jev-judged like any other
# unregistered GET/HEAD.
_API_CHANNELS: dict = {}
_API_CHANNELS_LOAD_ERROR = None

# Registry column names that hold JSON (lists/dicts) and need load-time
# parsing; everything else is scalar.
_API_SERVICE_JSON_COLUMNS = ('default_headers', 'methods', 'path_rules', 'workflow')

_API_SERVICE_COLUMNS = (
    'id', 'name', 'base_url', 'credential_type', 'credential_key', 'credential_key2',
    'credential_name', 'auth_type', 'auth_value_format', 'auth_field', 'auth_token_url',
    'auth_header_name',
    'default_headers', 'methods', 'path_rules', 'spend_kind', 'spend_note', 'spend_cap',
    'workflow', 'status', 'proposed_by', 'created_at', 'updated_at',
)

_OAUTH2_TOKEN_CACHE: dict = {}
_OAUTH2_TOKEN_LOCK = threading.Lock()
OAUTH2_REFRESH_MARGIN_S = 30

_MULLVAD_TOKEN_CACHE: dict = {}
_MULLVAD_TOKEN_LOCK = threading.Lock()
MULLVAD_TOKEN_REFRESH_MARGIN_S = 60


def _api_service_spec_to_row(sid, spec, status='active', proposed_by=None):
    """Flatten a registry spec dict into a DB row dict. The spec uses the
    same shape as the original JSON seed (see world/api_services.json), which
    is exactly what the approval flow stores on an approved proposal."""
    cred = (spec or {}).get('credential') or {}
    auth = (spec or {}).get('auth') or {}
    spend = (spec or {}).get('spend') or {}
    now = time.time()
    return {
        'id': sid,
        'name': (spec or {}).get('name') or sid,
        'base_url': (spec or {}).get('base_url') or '',
        'credential_type': cred.get('type') or 'env',
        'credential_key': cred.get('key'),
        'credential_key2': cred.get('key2'),
        'credential_name': cred.get('name'),
        'auth_type': auth.get('type') or 'header_bearer',
        'auth_value_format': auth.get('value_format'),
        'auth_field': auth.get('field'),
        'auth_token_url': auth.get('token_url'),
        'auth_header_name': auth.get('header_name'),
        'default_headers': json.dumps((spec or {}).get('default_headers') or {}),
        'methods': json.dumps((spec or {}).get('methods') or []),
        'path_rules': json.dumps((spec or {}).get('path_rules') or []),
        'spend_kind': spend.get('kind') or 'none',
        'spend_note': spend.get('note'),
        'spend_cap': spend.get('cap'),
        'workflow': json.dumps((spec or {}).get('workflow') or None),
        'status': status,
        'proposed_by': proposed_by,
        'created_at': now,
        'updated_at': now,
    }


def _api_service_row_to_spec(row):
    """Inflate a DB row back into the registry spec dict shape the runtime
    helpers (auth injection, path rules, spend, workflow) consume."""
    def _jget(key, default):
        raw = row.get(key)
        if raw in (None, ''):
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default
    return {
        'id': row.get('id'),
        'name': row.get('name') or row.get('id'),
        'base_url': row.get('base_url') or '',
        'credential': {
            'type': row.get('credential_type') or 'env',
            'key': row.get('credential_key'),
            'key2': row.get('credential_key2'),
            'name': row.get('credential_name'),
        },
        'auth': {
            'type': row.get('auth_type') or 'header_bearer',
            'value_format': row.get('auth_value_format'),
            'field': row.get('auth_field'),
            'token_url': row.get('auth_token_url'),
            'header_name': row.get('auth_header_name'),
        },
        'default_headers': _jget('default_headers', {}),
        'methods': _jget('methods', []),
        'path_rules': _jget('path_rules', []),
        'spend': {'kind': row.get('spend_kind') or 'none', 'note': row.get('spend_note'),
                  'cap': row.get('spend_cap')},
        'workflow': _jget('workflow', None),
        'status': row.get('status') or 'active',
    }


def _seed_api_services_if_empty():
    """First-run seed: when the api_services table has no rows, load the
    canonical built-ins from world/api_services.json into it as 'active'."""
    import serve as _serve
    try:
        with _serve._db() as conn:
            count = conn.execute('SELECT COUNT(*) FROM api_services').fetchone()[0]
            if count:
                return
            if not os.path.exists(_serve.API_SERVICES_PATH):
                return
            with open(_serve.API_SERVICES_PATH, encoding='utf-8') as f:
                data = json.load(f)
            for sid, spec in (data.get('services') or {}).items():
                row = _serve._api_service_spec_to_row(sid, spec, status='active')
                conn.execute(
                    'INSERT OR IGNORE INTO api_services (' + ','.join(_API_SERVICE_COLUMNS) + ') '
                    'VALUES (' + ','.join('?' * len(_API_SERVICE_COLUMNS)) + ')',  # nosec B608 -- column set is a fixed constant tuple, never user input
                    [row.get(c) for c in _API_SERVICE_COLUMNS])
    except Exception as e:
        # A seed failure is harmless: _load_api_services() runs right after and
        # records its own error if the table can't be read. Fall through to the
        # JSON seed in that case.
        print(f'[api-services] seed failed: {e}', flush=True)


def _load_api_services():
    """Load the active api_services rows into _API_SERVICES, keyed by service
    id AND by the base URL's hostname (so a URL lookup by host works). Reads
    the DB table; falls back to the JSON seed file only if the DB is not yet
    available (module import happens before init_db). On any failure, leaves
    the registry empty and records the error -- fail closed: an empty
    registry means api_call only serves read-only unregistered GET/HEAD,
    never a credentialed write."""
    import serve as _serve
    _serve._API_SERVICES = {}
    _serve._API_SERVICES_LOAD_ERROR = None
    rows = None
    try:
        with _serve._db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT " + ','.join(_API_SERVICE_COLUMNS) + " FROM api_services WHERE status = 'active'"  # nosec B608 -- column set is a fixed constant tuple
            ).fetchall()
    except Exception as e:
        _serve._API_SERVICES_LOAD_ERROR = f'db unavailable at load: {e}'
        rows = None
    if rows is None:
        # DB not ready yet (import-time): fall back to the JSON seed so the
        # runtime has a working registry before init_db runs.
        try:
            with open(_serve.API_SERVICES_PATH, encoding='utf-8') as f:
                data = json.load(f)
            for sid, spec in (data.get('services') or {}).items():
                spec = dict(spec)
                spec['id'] = sid
                _serve._index_api_service(spec)
        except Exception as e:
            _serve._API_SERVICES_LOAD_ERROR = f'json seed unavailable: {e}'
        _serve._load_api_channels()
        return
    for row in rows:
        _serve._index_api_service(_serve._api_service_row_to_spec(dict(row)))
    _serve._load_api_channels()


def _ensure_api_services_fresh():
    """Reload the registry from the DB when the active-row fingerprint changes,
    so an operator's api_services row edit (add / revoke / tweak) goes live
    without a restart. Called at the api_call chokepoint and the registry list
    endpoint; TTL-guarded so the cost is one aggregate query per window, and
    _load_api_services() only runs when something actually changed. On any DB
    error, keeps the current registry -- fail closed, never a raised call."""
    import serve as _serve
    now = time.time()
    if now - _serve._API_SERVICES_FRESH_CHECK < _serve._API_SERVICES_REFRESH_TTL_S:
        return
    _serve._API_SERVICES_FRESH_CHECK = now
    try:
        with _serve._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM api_services WHERE status = 'active'"  # nosec B608 -- aggregate over a fixed table, never user input
            ).fetchone()
    except Exception:
        return  # DB not ready or busy: keep the registry as-is
    fingerprint = (row[0], row[1])
    if fingerprint != _serve._API_SERVICES_FINGERPRINT:
        _serve._API_SERVICES_FINGERPRINT = fingerprint
        _serve._load_api_services()


def _load_api_channels():
    """Load the CHANNELS (ordered read-only backend chains) from the JSON seed.
    Channels are config-only -- never DB rows, since they grant no credential on
    their own: every backend must be a registered service, and channel hosts are
    still SSRF-gated and Jev-judged as unregistered GET/HEAD. Keyed by channel
    hostname (exact + subdomain match). Fail closed: on any error, no channel
    routes (unregistered hosts just fall back to a plain GET/HEAD)."""
    import serve as _serve
    _serve._API_CHANNELS = {}
    _serve._API_CHANNELS_LOAD_ERROR = None
    try:
        if not os.path.exists(_serve.API_SERVICES_PATH):
            return
        with open(_serve.API_SERVICES_PATH, encoding='utf-8') as f:
            data = json.load(f)
        for chost, chan in (data.get('channels') or {}).items():
            if chost == 'comment' or not isinstance(chan, dict):
                continue
            hosts = chan.get('hosts') or []
            backends = chan.get('backends') or []
            if not hosts or not backends:
                _serve._API_CHANNELS_LOAD_ERROR = f'channel {chost} missing hosts or backends'
                return
            for host in hosts:
                host = (host or '').lower().lstrip('.')
                if not host or '.' not in host:
                    _serve._API_CHANNELS_LOAD_ERROR = f'channel {chost} has invalid host {host!r}'
                    return
                spec = dict(chan)
                spec['id'] = chost
                _serve._API_CHANNELS[host] = spec
    except Exception as e:
        _serve._API_CHANNELS_LOAD_ERROR = str(e)


def _api_channel_for_host(hostname):
    """Channel spec for a hostname (exact or subdomain of a channel host), or
    None if no channel owns that host. Mirrors _api_service_for_host."""
    import serve as _serve
    host = (hostname or '').lower()
    if not host:
        return None
    for chost, spec in _serve._API_CHANNELS.items():
        if host == chost or host.endswith('.' + chost):
            return spec
    return None


def _api_backend_url(backend, url):
    """Expand a channel backend's url_template against the requested URL. The
    template is trusted config; {url} is substituted with the original URL."""
    template = (backend or {}).get('url_template') or '{url}'
    return template.replace('{url}', url)


def _index_api_service(spec):
    """Register one spec (already inflated) into the in-memory _API_SERVICES
    map, by service id and by base hostname."""
    import serve as _serve
    base = (spec.get('base_url') or '').rstrip('/')
    if not base:
        return
    sid = spec.get('id')
    host = urllib.parse.urlparse(base).hostname
    if host:
        _serve._API_SERVICES[host.lower()] = spec
    if sid:
        _serve._API_SERVICES[sid] = spec


def _api_service_upsert(spec, status='active', proposed_by=None):
    """Persist a service spec as a row and refresh the in-memory registry.
    Used by the approval flow when a proposal is approved (status='active')
    and by the revoke flow (status='denied')."""
    import serve as _serve
    sid = spec.get('id')
    row = _serve._api_service_spec_to_row(sid, spec, status=status, proposed_by=proposed_by)
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO api_services (' + ','.join(_API_SERVICE_COLUMNS) + ') '  # nosec B608 -- column set is a fixed constant tuple, never user input
            'VALUES (' + ','.join('?' * len(_API_SERVICE_COLUMNS)) + ') '
            'ON CONFLICT(id) DO UPDATE SET '
            + ','.join(f'{c}=excluded.{c}' for c in _API_SERVICE_COLUMNS if c != 'id'),
            [row.get(c) for c in _API_SERVICE_COLUMNS])
    _serve._load_api_services()


def _validate_api_service_spec(spec):
    """Validate a proposed service spec (the shape in world/api_services.json).
    Returns (service_id, normalized_spec, error) -- error is None when valid.
    Security-sensitive constraints live here and are enforced again at approval
    time, so a forged/edited proposal can never grant more than this allows:
    public https base URL only, a known auth type, a known credential source,
    non-empty methods + path rules, and a known spend kind."""
    import serve as _serve
    spec = spec or {}
    sid = str(spec.get('id') or '').strip().lower()
    if not sid or not sid.replace('_', '').replace('-', '').isalnum():
        return None, None, 'service id must be a simple alphanumeric slug'
    base = str(spec.get('base_url') or '').strip().rstrip('/')
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != 'https' or not parsed.hostname or not _serve._is_safe_public_host(parsed.hostname):
        return None, None, 'base_url must be a public https URL'
    cred = spec.get('credential') or {}
    if cred.get('type') not in ('none', 'env', 'vault'):
        return None, None, 'credential.type must be "none", "env", or "vault"'
    auth = spec.get('auth') or {}
    auth_type = auth.get('type')
    if auth_type not in ('none', 'header_bearer', 'header_key', 'body_field', 'mullvad', 'oauth2_client_credentials'):
        return None, None, 'auth.type must be none, header_bearer, header_key, body_field, mullvad, or oauth2_client_credentials'
    if auth_type == 'mullvad':
        # Mullvad mints a bearer from the account number via its JSON token
        # endpoint -- requires a public https auth.token_url and a single-part
        # env credential (the account number; there is no client secret).
        token_url = str(auth.get('token_url') or '').strip().rstrip('/')
        parsed_tok = urllib.parse.urlparse(token_url)
        if parsed_tok.scheme != 'https' or not parsed_tok.hostname or not _serve._is_safe_public_host(parsed_tok.hostname):
            return None, None, 'mullvad requires a public https auth.token_url'
        if cred.get('type') != 'env' or not cred.get('key') or cred.get('key2'):
            return None, None, 'mullvad requires a single-part env credential (the account number)'
        auth = dict(auth)
        auth['token_url'] = token_url
    if auth_type == 'oauth2_client_credentials':
        # The token endpoint the OAuth2 client-credentials flow POSTs the
        # client id/secret pair to. It must be a public https URL (SSRF-safe,
        # same rule as base_url) and the credential must be the two-part env
        # pair -- a minted access token is never stored in the registry.
        token_url = str(auth.get('token_url') or '').strip().rstrip('/')
        parsed_tok = urllib.parse.urlparse(token_url)
        if parsed_tok.scheme != 'https' or not parsed_tok.hostname or not _serve._is_safe_public_host(parsed_tok.hostname):
            return None, None, 'oauth2_client_credentials requires a public https auth.token_url'
        if cred.get('type') != 'env' or not cred.get('key') or not cred.get('key2'):
            return None, None, 'oauth2_client_credentials requires a two-part env credential (key = client id, key2 = client secret)'
        auth = dict(auth)
        auth['token_url'] = token_url
    methods = spec.get('methods') or []
    if not methods or not all(m in _serve.CURL_METHODS for m in methods):
        return None, None, 'methods must be a non-empty subset of GET/POST/PUT/PATCH/DELETE/HEAD'
    path_rules = spec.get('path_rules') or []
    if not path_rules or not all(isinstance(r, dict) and r.get('prefix') for r in path_rules):
        return None, None, 'path_rules must be a non-empty list of {prefix, methods} rules'
    spend_kind = ((spec.get('spend') or {}).get('kind')) or 'none'
    if spend_kind not in ('none', 'page_request', 'apify', 'daily_calls'):
        return None, None, 'spend.kind must be none, page_request, apify, or daily_calls'
    if spend_kind == 'daily_calls':
        cap = (spec.get('spend') or {}).get('cap')
        if not isinstance(cap, (int, float)) or cap <= 0:
            return None, None, 'spend.kind daily_calls requires a positive numeric spend.cap'
    normalized = dict(spec)
    normalized['id'] = sid
    normalized['name'] = str(spec.get('name') or sid)
    normalized['base_url'] = base
    normalized['auth'] = auth
    return sid, normalized, None


def _approve_api_service_proposal(esc):
    """Approval side-effect for an 'api service proposal' escalation. Parses
    the validated spec out of the escalation note, re-validates it (a forged
    or edited note cannot grant more than the validator allows), and writes it
    to the api_services registry as active -- the only path that registers a
    service. Returns (ok, message)."""
    import serve as _serve
    try:
        spec = json.loads(esc.get('note') or '{}')
    except ValueError:
        return False, 'the proposal note could not be read; no service was registered.'
    sid, norm, error = _serve._validate_api_service_spec(spec)
    if error:
        return False, f'the proposal is no longer valid: {error}. No service registered.'
    _serve._api_service_upsert(norm, status='active', proposed_by=esc.get('resolvedBy') or 'player')
    return True, f'Service {sid} is now registered and reachable by agents.'


def _api_base_url(host):
    """Registry base_url for a host, or a plain https://host fallback (used by
    the curated tool executors so the base URL comes from the registry, not a
    hardcoded literal)."""
    import serve as _serve
    spec = _serve._api_service_for_host(host)
    return (spec or {}).get('base_url') or ('https://' + host)


def _api_service_for_host(hostname):
    """Registry spec for a hostname (exact or subdomain of a registered base
    host), or None if unregistered."""
    import serve as _serve
    host = (hostname or '').lower()
    if not host:
        return None
    for registered_host, spec in _serve._API_SERVICES.items():
        if '.' not in registered_host:  # a service-id key, not a host
            continue
        if host == registered_host or host.endswith('.' + registered_host):
            return spec
    return None


def _api_service_path_allowed(spec, method, path):
    """True when (method, path) is inside a registered service's path_rules.
    path_rules prefixes are matched against the URL path with the base_url's
    own path stripped first -- so an Apify base_url of https://api.apify.com/v2
    and a rule prefix of '/actors/' both accept '/v2/actors/...' and reject
    '/v2/users/me' unless a '/users/me' rule exists."""
    base_path = urllib.parse.urlparse((spec or {}).get('base_url') or '').path
    rel_path = path or '/'
    if base_path and rel_path.startswith(base_path):
        rel_path = rel_path[len(base_path):] or '/'
    for rule in (spec or {}).get('path_rules') or []:
        prefix = rule.get('prefix', '/')
        if rel_path.startswith(prefix) and method in rule.get('methods', []):
            return True
    return False


def _api_credential_value(spec):
    """Resolve a registry entry's credential to (token, token2, error). token2
    is the second half of a two-part key credential (Higgsfield). Env keys
    resolve through SERVE's module globals first (tests patch them, and hot
    env reload rebinds them) with a live .env fallback, so a brand-new service
    needs ZERO code changes -- just a row + a .env key. Vault-held credentials
    resolve through the vault. This function is the single credential
    chokepoint, same as _treg_call/_pixellab_call."""
    import serve as _serve
    cred = (spec or {}).get('credential') or {}
    ctype = cred.get('type')
    if ctype == 'none':
        # A keyless/free public API (Open-Meteo, ...) -- no credential to
        # resolve, no auth to inject, and never an "unconfigured credential"
        # block. This is the config-not-code path for free integrations.
        return None, None, None
    if ctype == 'env':
        key = cred.get('key') or ''
        token = _serve.__dict__.get(key)
        if not token:
            token = _serve._load_env().get(key) or ''
        key2 = cred.get('key2') or ''
        token2 = _serve.__dict__.get(key2) if key2 else None
        if key2 and not token2:
            token2 = _serve._load_env().get(key2) or ''
        if not token:
            return None, None, f'env key {key} is not set'
        return token, token2, None
    if ctype == 'vault':
        name = cred.get('name') or ''
        token = _serve._open_secret(_serve._credential_token(name) or '')
        if not token:
            return None, None, f'credential {name} is not in the vault'
        return token, None, None
    return None, None, 'registry entry has no credential configured'


def _api_apply_auth(spec, token, token2, method, url, headers, body):
    """Inject a registry credential into a request. Auth comes from the
    registry spec, never from anything the agent supplied. Returns the
    (url, headers, body) triple with the credential applied."""
    import serve as _serve
    auth = (spec or {}).get('auth') or {}
    atype = auth.get('type')
    if atype == 'header_bearer':
        headers['Authorization'] = f'Bearer {token}'
    elif atype == 'header_key':
        fmt = auth.get('value_format') or 'Key {key}'
        headers[auth.get('header_name') or 'Authorization'] = fmt.format(key=token, key2=token2)
    elif atype == 'body_field':
        field = auth.get('field') or 'api_key'
        body = dict(body or {})
        body[field] = token
    elif atype == 'mullvad':
        # Mullvad account API: mint a short-lived bearer from the account
        # number via the JSON token endpoint, inject as Bearer. The minted
        # bearer is the ONLY credential that ever rides on the actual request.
        token_url = (auth.get('token_url') or '').strip()
        if not token_url:
            raise ValueError('mullvad auth requires auth.token_url')
        bearer = _serve._mullvad_access_token(token_url, token)
        headers['Authorization'] = f'Bearer {bearer}'
    elif atype == 'oauth2_client_credentials':
        # OAuth2 client-credentials: the registry stores the CLIENT ID/SECRET
        # pair (token = client_id, token2 = client_secret) plus the token
        # endpoint. A short-lived access token is minted and cached in-process
        # (never persisted, never returned to the agent), then injected as a
        # Bearer header. The minted bearer is the ONLY credential that ever
        # rides on the actual API request.
        token_url = (auth.get('token_url') or '').strip()
        if not token_url:
            raise ValueError('oauth2_client_credentials requires auth.token_url')
        bearer = _serve._oauth2_access_token(token_url, token, token2)
        headers['Authorization'] = f'Bearer {bearer}'
    return url, headers, body


# ---------------------------------------------------------------------------
# OAuth2 client-credentials mint + cache. OpenSky (and other OAuth2-protected
# APIs) take a client id/secret pair and exchange it for a SHORT-LIVED access
# token (OpenSky: 30 minutes) -- the registry can't inject a static bearer, it
# has to mint one. The minted token lives in a module-level cache keyed by
# (token_url, client_id) so a long-lived think tank polls the same service all
# day without one mint per request; it is refreshed proactively before expiry
# and never logged, persisted, or returned to an agent. The token endpoint is
# SSRF-gated the same way the registry base_urls are: public https only.
# ---------------------------------------------------------------------------
def _oauth2_token_request_sync(token_url, client_id, client_secret):
    """POST the client-credentials grant to the token endpoint and return the
    raw response body. Form-encoded body (OAuth2's content type), SSRF-pinned
    like every other outbound call, redirect-final-host re-checked. Raises on
    a non-2xx response or a redirect to a disallowed host."""
    import serve as _serve
    if not client_id or not client_secret:
        raise ValueError('oauth2 client id/secret are not set')
    body = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
    }).encode()
    headers = {'Content-Type': 'application/x-www-form-urlencoded',
               'Accept': 'application/json', 'User-Agent': 'AIThinkTankAgent/1.0'}
    req = urllib.request.Request(token_url, data=body, headers=headers, method='POST')
    with _serve._safe_urlopen(req, timeout=_serve.API_CALL_TIMEOUT_S) as resp:  # token URL is public-https validated at registration; _safe_urlopen pins each resolved IP so DNS cannot rebind to an internal address
        final_url = resp.geturl()
        final_host = urllib.parse.urlparse(final_url).hostname
        if not _serve._is_safe_public_host(final_host):
            raise ValueError('oauth2 token endpoint redirected to a disallowed host')
        raw = resp.read(_serve.API_CALL_MAX_BODY_BYTES + 1)
    if resp.status >= 400:
        raise ValueError(f'oauth2 token endpoint returned HTTP {resp.status}')
    return raw.decode(errors='replace')[:_serve.API_CALL_MAX_BODY_BYTES]


def _oauth2_access_token(token_url, client_id, client_secret):
    """Return a valid access token for (token_url, client_id), minting and
    caching one when the cache is empty or near expiry. Raises when the mint
    fails so the caller (the auth-injection path) fails the request cleanly."""
    import serve as _serve
    parsed = urllib.parse.urlparse(token_url)
    if parsed.scheme != 'https' or not parsed.hostname or not _serve._is_safe_public_host(parsed.hostname):
        raise ValueError('oauth2 token_url must be a public https URL')
    cache_key = (token_url, client_id)
    with _serve._OAUTH2_TOKEN_LOCK:
        cached = _serve._OAUTH2_TOKEN_CACHE.get(cache_key)
        if cached and time.time() + _serve.OAUTH2_REFRESH_MARGIN_S < cached['expires_at']:
            return cached['token']
        raw = _serve._oauth2_token_request_sync(token_url, client_id, client_secret)
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError('oauth2 token endpoint returned non-JSON')
        token = (data or {}).get('access_token')
        if not token:
            raise ValueError('oauth2 token endpoint returned no access_token')
        try:
            expires_in = float(data.get('expires_in') or 3600)
        except (ValueError, TypeError):
            expires_in = 3600.0
        _serve._OAUTH2_TOKEN_CACHE[cache_key] = {'token': token, 'expires_at': time.time() + expires_in}
        return token


# ---------------------------------------------------------------------------
# Mullvad account-token mint + cache. Mullvad's account API is OAuth2-ish but
# NOT the standard client-credentials form grant: the token endpoint is POST
# api.mullvad.net/auth/v1/token with a JSON body {"account_number": "..."}
# (no client secret -- the account number IS the credential), and the response
# carries an ISO-8601 `expiry` instead of `expires_in` (confirmed live: the
# stock form-encoded grant is 415'd, and a JSON body with "client_id" is 400'd
# with 'Either account_number or authorization_code is required'). The minted
# bearer is cached keyed by (token_url, account_number), refreshed proactively
# before expiry, and never logged or returned to an agent.
# ---------------------------------------------------------------------------
def _mullvad_token_request_sync(token_url, account_number):
    """POST the JSON {account_number} grant to Mullvad's token endpoint and
    return the raw response body. SSRF-pinned like every other outbound call,
    redirect-final-host re-checked, raises on a non-2xx response."""
    import serve as _serve
    if not account_number:
        raise ValueError('mullvad account number is not set')
    body = json.dumps({'account_number': account_number}).encode()
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
               'User-Agent': 'AIThinkTankAgent/1.0'}
    req = urllib.request.Request(token_url, data=body, headers=headers, method='POST')
    with _serve._safe_urlopen(req, timeout=_serve.API_CALL_TIMEOUT_S) as resp:
        final_url = resp.geturl()
        final_host = urllib.parse.urlparse(final_url).hostname
        if not _serve._is_safe_public_host(final_host):
            raise ValueError('mullvad token endpoint redirected to a disallowed host')
        raw = resp.read(_serve.API_CALL_MAX_BODY_BYTES + 1)
    if resp.status >= 400:
        raise ValueError(f'mullvad token endpoint returned HTTP {resp.status}')
    return raw.decode(errors='replace')[:_serve.API_CALL_MAX_BODY_BYTES]


def _mullvad_access_token(token_url, account_number):
    """Return a valid Mullvad access token for the account number, minting and
    caching one when the cache is empty or near expiry. Raises when the mint
    fails so the auth-injection path fails the request cleanly."""
    import serve as _serve
    parsed = urllib.parse.urlparse(token_url)
    if parsed.scheme != 'https' or not parsed.hostname or not _serve._is_safe_public_host(parsed.hostname):
        raise ValueError('mullvad token_url must be a public https URL')
    cache_key = (token_url, account_number)
    with _serve._MULLVAD_TOKEN_LOCK:
        cached = _serve._MULLVAD_TOKEN_CACHE.get(cache_key)
        if cached and time.time() + _serve.MULLVAD_TOKEN_REFRESH_MARGIN_S < cached['expires_at']:
            return cached['token']
        raw = _serve._mullvad_token_request_sync(token_url, account_number)
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError('mullvad token endpoint returned non-JSON')
        token = (data or {}).get('access_token')
        if not token:
            raise ValueError('mullvad token endpoint returned no access_token')
        expires_at = time.time() + 3600.0
        expiry = (data or {}).get('expiry')
        if expiry:
            try:
                expires_at = datetime.datetime.fromisoformat(str(expiry).replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
        _serve._MULLVAD_TOKEN_CACHE[cache_key] = {'token': token, 'expires_at': expires_at}
        return token


# ---------------------------------------------------------------------------
# Daily per-service call budget (spend.kind = 'daily_calls'). Some registered
# services run on a DAILY quota rather than dollars or a monthly count --
# OpenSky's per-account credit buckets refill every day (4,000/day per endpoint
# family for a standard account), so the correct rollover key is a UTC date,
# not a month. A registry entry declares a conservative spend.cap (a call count
# well under the real credit allowance) and the chokepoint blocks before the
# quota could be exhausted, mirroring the Apify monthly gate. Counts live in
# their own kv_apicalls row so the accounting never touches the sim-state blob.
# ---------------------------------------------------------------------------
def _api_daily_calls_ledger_read():
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS kv_apicalls (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute('SELECT blob FROM kv_apicalls WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _api_daily_calls_ledger_write(ledger):
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute(
                'INSERT INTO kv_apicalls (id, blob, updated_at) VALUES (1, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
                (json.dumps(ledger), time.time()),
            )
    except Exception:
        pass


def _api_daily_calls_day(now=None):
    """The current UTC date (2026-10-08) -- the daily rollover key: a fresh
    date starts a new allowance for every service."""
    return datetime.datetime.fromtimestamp(now or time.time(), datetime.timezone.utc).strftime('%Y-%m-%d')


def _api_daily_calls_used(service):
    """How many api_call requests this service has consumed today."""
    sid = (service or {}).get('id')
    if not sid:
        return 0
    import serve as _serve
    ledger = _serve._api_daily_calls_ledger_read()
    bucket = ledger.get(_serve._api_daily_calls_day()) or {}
    return int(bucket.get(sid, 0) or 0)


def _api_daily_calls_exceeded(service):
    """True when a registered service with spend.kind=daily_calls has consumed
    its configured daily cap. Never true when no cap is configured."""
    spend = ((service or {}).get('spend') or {})
    if spend.get('kind') != 'daily_calls':
        return False
    cap = spend.get('cap')
    if not isinstance(cap, (int, float)) or cap <= 0:
        return False
    import serve as _serve
    return _serve._api_daily_calls_used(service) >= cap


def _api_accrue_daily_call(service):
    """Record ONE api_call against the service's daily budget. Best-effort like
    the other ledgers: an accounting failure must never break the real request."""
    sid = (service or {}).get('id')
    if not sid:
        return
    try:
        import serve as _serve
        ledger = _serve._api_daily_calls_ledger_read()
        day = _serve._api_daily_calls_day()
        bucket = ledger.setdefault(day, {})
        bucket[sid] = int(bucket.get(sid, 0) or 0) + 1
        _serve._api_daily_calls_ledger_write(ledger)
    except Exception:
        pass
