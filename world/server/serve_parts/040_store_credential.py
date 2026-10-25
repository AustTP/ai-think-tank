

def _store_credential(name, service, value, status='active', proposed_by=None):
    """Store (or replace) a credential, encrypted at rest. `value` is either a
    raw token string (legacy callers) or a structured dict payload
    ({'kind': 'token', 'token': ...} / {'kind': 'login', 'username': ...,
    'password': ...}). The encrypted blob is the ONLY copy ever persisted; the
    plaintext exists only inside the process for the instant it is used."""
    if isinstance(value, dict):
        payload = dict(value)
        kind = payload.get('kind') if payload.get('kind') in ('token', 'login') else 'token'
    else:
        payload = {'kind': 'token', 'token': value}
        kind = 'token'
    now = time.time()
    with _db() as conn:
        conn.execute(
            'INSERT INTO external_credentials (name, service, kind, encrypted_value, status, proposed_by, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(name) DO UPDATE SET service=excluded.service, kind=excluded.kind, '
            'encrypted_value=excluded.encrypted_value, status=excluded.status, '
            'proposed_by=excluded.proposed_by, updated_at=excluded.updated_at',
            (name, service, kind, _seal_secret(json.dumps(payload)), status, proposed_by, now, now),
        )


def _list_credentials():
    """Credential names + services + kinds + statuses -- NEVER the values."""
    with _db() as conn:
        return [{'name': r[0], 'service': r[1], 'kind': r[2], 'status': r[3], 'proposedBy': r[4]}
                for r in conn.execute(
                    'SELECT name, service, kind, status, proposed_by FROM external_credentials ORDER BY name')]


def _credential_payload(name):
    """Decrypt one credential's stored value into its structured payload, or
    None when absent/unreadable. Legacy rows that stored a raw token string
    decrypt to {'kind': 'token', 'token': <raw>}. This is the single chokepoint
    that turns an encrypted blob back into something usable -- everything that
    USES a credential goes through here (or through resolve_capability_handle)."""
    raw = _credential_token(name)
    if raw is None:
        return None
    dec = _open_secret(raw)
    if dec is None:
        return None
    try:
        obj = json.loads(dec)
        if isinstance(obj, dict) and obj.get('kind') in ('token', 'login'):
            return obj
    except (ValueError, TypeError):
        pass
    return {'kind': 'token', 'token': dec}


def _delete_credential(name):
    # Deleting a credential also voids every handle that points at it -- a
    # cleared key can no longer authorize anything.
    with _db() as conn:
        conn.execute('DELETE FROM capability_handles WHERE credential_name = ?', (name,))
        conn.execute('DELETE FROM external_credentials WHERE name = ?', (name,))


def _validate_credential_body(body):
    """Validate a credential proposal. Returns (name, service, payload, error) --
    error is None when valid. 'token' kind stores one API key; 'login' kind
    stores a username + password for an interactive account. The secret is
    never echoed into error text."""
    body = body or {}
    name = (body.get('name') or '').strip()
    if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
        return None, None, None, 'name must be [a-zA-Z0-9_-] (a slug)'
    service = (body.get('service') or name).strip()
    if not service:
        return None, None, None, 'service is required'
    kind = (body.get('kind') or 'token').strip().lower()
    if kind == 'login':
        username = (body.get('username') or '').strip()
        password = body.get('password')
        if not username or not isinstance(password, str) or not password:
            return None, None, None, 'login credentials need a username and a password'
        return name, service[:120], {'kind': 'login', 'username': username[:200], 'password': password}, None
    if kind == 'token':
        token = body.get('value') or body.get('token')
        if not token or not isinstance(token, str) or not token.strip():
            return None, None, None, 'token credentials need a value (the token)'
        return name, service[:120], {'kind': 'token', 'token': token}, None
    return None, None, None, 'kind must be "token" or "login"'


def _mask_secret(value):
    """A short masked preview of a secret -- never enough to reconstruct it."""
    value = value or ''
    if len(value) <= 4:
        return '*' * len(value)
    return value[:2] + '*' * (len(value) - 4) + value[-2:]


def _mask_credential_preview(payload):
    """Masked preview of a stored payload for approvers/lists: username is shown
    (it is not a secret), the password/token is masked."""
    payload = payload or {}
    if payload.get('kind') == 'login':
        return {'kind': 'login', 'username': payload.get('username'),
                'password': _mask_secret(payload.get('password'))}
    return {'kind': 'token', 'token': _mask_secret(payload.get('token'))}


# Login adapters: the service-specific half of a username/password account.
# The vault + capability handles make the secret reachable and per-ask scoped,
# but actually PERFORMING an interactive login (form fields, CSRF, MFA,
# cookies) is code that only makes sense once a real account exists. Each
# adapter is a contract:
#   _LOGIN_ADAPTERS[service] = {
#       'login_url': 'https://host/...',  # SSRF-checked before dispatch
#       'fn': callable(grant, action) -> dict,
#   }
# `grant` is the resolved capability grant (username/password/purpose/service);
# `action` is {'method', 'url', 'body'} for the ONE scoped action. `fn` MUST
# perform the login + that single action, keep the session in-process, and
# never put the password in its result -- the skeleton scrubs secret-shaped
# keys as a defensive last line. The action host/method are already enforced
# by the handle scope before fn is called.
_LOGIN_ADAPTERS: dict = {}

# Secret-shaped result keys that _login_scrub_result masks no matter what an
# adapter returns. Never logs or returns these un-masked.
_LOGIN_SECRET_KEYS = frozenset({
    'password', 'passwd', 'pwd', 'token', 'access_token', 'refresh_token',
    'secret', 'cookie', 'cookies', 'session', 'session_id', 'sessionid',
    'set_cookie', 'auth', 'authorization', 'api_key', 'apikey', 'key',
})


def _login_scrub_result(result, depth=0):
    """Mask any secret-shaped key anywhere in an adapter result. The adapter
    contract forbids returning secrets, but a scrub here is the last line of
    defense so a careless adapter can't leak a password to the agent."""
    if depth > 5 or result is None or isinstance(result, (str, int, float, bool)):
        return result
    if isinstance(result, dict):
        out = {}
        for k, v in result.items():
            if isinstance(k, str) and k.lower() in _LOGIN_SECRET_KEYS:
                out[k] = _mask_secret(str(v)) if isinstance(v, str) else '[redacted]'
            else:
                out[k] = _login_scrub_result(v, depth + 1)
        return out
    if isinstance(result, list):
        return [_login_scrub_result(v, depth + 1) for v in result]
    return result


def _login_safe_url(url):
    """Public-https + SSRF-safe check for the URLs a login flow touches (the
    action URL and the adapter's declared login URL). Fails closed."""
    if not isinstance(url, str) or not url.lower().startswith('https://'):
        return False
    host = urllib.parse.urlparse(url).hostname or ''
    return bool(host) and _is_safe_public_host(host)


# --- Scoped file download ----------------------------------------------------
# Agents can download files into their own filespace under a hard size cap.
# The URL must be public https (SSRF), the stream ABORTS the instant it
# crosses the cap (never buffered whole), the filename is sanitized so it
# cannot escape the agent's directory, and nothing downloaded is ever
# executed. An optional capability handle attaches a token credential and is
# scope-checked against the URL exactly like api_call.
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
_DOWNLOAD_CHUNK = 64 * 1024


def _sanitize_filename(name):
    base = os.path.basename((name or '').replace('\\', '/')).strip()
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)[:120]
    if not base or base in ('.', '..'):
        return 'download.bin'
    if base.startswith('.'):
        base = 'file_' + base.lstrip('.')
    return base


def _download_stream_sync(url, headers, dest_path, max_bytes):
    """Stream `url` to `dest_path` with a HARD size cap: the file is deleted
    and a ValueError raised the instant the cap is crossed. SSRF-safe via
    _safe_urlopen's per-IP pinning; the final host after redirects is
    re-checked as a public host."""
    req = urllib.request.Request(url, headers=headers or {}, method='GET')
    if 'User-Agent' not in {k.title() for k in (headers or {})}:
        req.add_header('User-Agent', 'AIThinkTankAgent/1.0')
    with _safe_urlopen(req, timeout=CURL_TIMEOUT_S) as resp:
        final_url = resp.geturl()
        if not _is_safe_public_host(urllib.parse.urlparse(final_url).hostname):
            raise ValueError('redirected to a disallowed host')
        status = resp.status
        total = 0
        try:
            with open(dest_path, 'wb') as f:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f'download exceeds the {max_bytes}-byte limit')
                    f.write(chunk)
        except Exception:
            try:
                os.remove(dest_path)
            except OSError:
                pass
            raise
    return {'status': status, 'bytes': total, 'finalUrl': final_url}


# --- Inbound SMS (receive-only) ---------------------------------------------
# See the DDL comment for sms_messages / sms_read_grants. OTP detection is
# deliberately simple: a standalone 4-8 digit token in the body. The code is
# stored so it can be MASKED by default and revealed only on an explicit
# operator grant (otp_allowed), one message, one agent, one read.
_SMS_OTP_RE = re.compile(r'(?<!\d)\d{4,8}(?!\d)')
_SMS_MAX_KEEP = 500


def _sms_detect_otp(body):
    if not body:
        return None
    m = _SMS_OTP_RE.search(body)
    return m.group(0) if m else None


def _sms_ingest(sender, body):
    otp = _sms_detect_otp(body)
    now = time.time()
    with _db() as conn:
        cur = conn.execute(
            'INSERT INTO sms_messages (sender, body, otp_code, received_at) VALUES (?, ?, ?, ?)',
            (sender, body, otp, now))
        mid = cur.lastrowid
        conn.execute('DELETE FROM sms_messages WHERE id NOT IN '
                     '(SELECT id FROM sms_messages ORDER BY id DESC LIMIT ?)', (_SMS_MAX_KEEP,))
    return mid, otp


def _sms_mask_otp(body, code):
    if not code:
        return body
    return body.replace(code, '*' * len(code))


def _sms_message_row(row):
    mid, sender, body, otp, received_at, consumed_at = row
    return {'id': mid, 'sender': sender, 'body': body, 'otpCode': otp,
            'receivedAt': received_at, 'consumed': consumed_at is not None}


def _sms_mint_grant(agent_id, message_id, granted_by, otp_allowed, ttl_s):
    gid = secrets.token_hex(16)
    now = time.time()
    with _db() as conn:
        conn.execute(
            'INSERT INTO sms_read_grants (grant_id, agent_id, message_id, granted_by, '
            'otp_allowed, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (gid, agent_id, message_id, granted_by, 1 if otp_allowed else 0, now, now + ttl_s))
    return gid


def _sms_consume_grant(agent_id, grant_id):
    """Resolve + consume a single-use SMS read grant. Returns
    (message_row, otp_allowed) or (None, None). Expired grants are swept and
    a consumed grant is gone -- every SMS read is exactly one ask."""
    with _db() as conn:
        row = conn.execute(
            'SELECT message_id, otp_allowed, expires_at FROM sms_read_grants '
            'WHERE grant_id = ? AND agent_id = ?', (grant_id, agent_id)).fetchone()
        if row is None:
            return None, None
        message_id, otp_allowed, expires_at = row
        if time.time() > expires_at:
            conn.execute('DELETE FROM sms_read_grants WHERE grant_id = ?', (grant_id,))
            return None, None
        conn.execute('DELETE FROM sms_read_grants WHERE grant_id = ?', (grant_id,))
        msg = conn.execute('SELECT id, sender, body, otp_code, received_at, consumed_at '
                           'FROM sms_messages WHERE id = ?', (message_id,)).fetchone()
    if msg is None:
        return None, None
    return _sms_message_row(msg), bool(otp_allowed)


_DIGITALOCEAN_BALANCE_CACHE = {'at': 0.0, 'data': None}
DIGITALOCEAN_BALANCE_CACHE_TTL_S = float(_load_env().get('BALANCE_CACHE_TTL_S', '300') or 300)


def _digitalocean_account_balance():
    """Live GET https://api.digitalocean.com/v2/customers/my/balance -- the
    account's own real, authoritative month-to-date usage. Returns a float
    (dollars) or None on any failure (no credential, network, bad response),
    so the circuit breaker below fails CLOSED (refuse) rather than silently
    treating an unreachable check as "must be fine." Cached briefly so a
    handle-mint request doesn't always cost a live network round-trip.
    While SANDBOX_EXECUTION=local the DO credential is unreachable by design
    (_digitalocean_enabled is False), so this returns None without touching
    the network -- the master switch cuts off even the read-only balance
    check, not just agent handles."""
    if not _digitalocean_enabled():
        return None
    token = _open_secret(_credential_token('digitalocean') or '')
    if not token:
        return None
    now = time.time()
    cached = _DIGITALOCEAN_BALANCE_CACHE
    if cached['data'] is not None and (now - cached['at']) < DIGITALOCEAN_BALANCE_CACHE_TTL_S:
        return cached['data']
    try:
        req = urllib.request.Request(
            'https://api.digitalocean.com/v2/customers/my/balance',
            headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 -- fixed DigitalOcean API host
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
        usage = float(data.get('month_to_date_usage') or 0)
        cached['at'] = now
        cached['data'] = usage
        return usage
    except Exception:
        return None


_TREG_BALANCE_CACHE = {'at': 0.0, 'data': None}
TREG_BALANCE_CACHE_TTL_S = float(_load_env().get('BALANCE_CACHE_TTL_S', '300') or 300)


def _treg_account_balance():
    """Live real usage for the Treg account -- a PREPAID balance (unlike
    DigitalOcean/PixelLab which report usage against a soft cap), so "usage"
    here is spent-so-far (balance_purchased - balance_remaining), comparable
    the same way as the others: refuse once spend reaches the configured cap.
    Looks up the org id from GET /orgs (Treg has no fixed org id to hardcode)
    then GET /orgs/{id}/balance. Returns None on any failure (fails closed)."""
    token = _open_secret(_credential_token('treg') or '')
    if not token:
        return None
    now = time.time()
    cached = _TREG_BALANCE_CACHE
    if cached['data'] is not None and (now - cached['at']) < TREG_BALANCE_CACHE_TTL_S:
        return cached['data']
    try:
        orgs_req = urllib.request.Request('https://treg.to/orgs', headers={'X-Treg-Token': token})
        with urllib.request.urlopen(orgs_req, timeout=10) as resp:  # nosec B310 -- fixed Treg API host
            orgs = json.loads(resp.read().decode('utf-8', errors='replace'))
        org_id = orgs[0]['org_id']
        bal_req = urllib.request.Request(
            f'https://treg.to/orgs/{org_id}/balance',
            headers={'X-Treg-Token': token, 'x-treg-org': str(org_id)})
        with urllib.request.urlopen(bal_req, timeout=10) as resp:  # nosec B310 -- fixed Treg API host
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
        purchased = sum(b.get('amount_micro', 0) for b in (data.get('blocks') or [])) / 1_000_000.0
        remaining = float(data.get('balance_usd') or 0)
        spent = max(0.0, purchased - remaining)
        cached['at'] = now
        cached['data'] = spent
        return spent
    except Exception:
        return None


# Real endpoint mechanics, confirmed with REAL live calls
# through the actual account, not guessed from docs alone -- Treg's catalog
# page doesn't show per-endpoint param schemas, and the first real attempt
# at harvestapi.linkedin.post.search failed closed (400, uncharged) because
# its declared params were never published anywhere findable; switched to
# scrapecreators.x.v1-linkedin-search-posts instead, whose real upstream API
# (api.scrapecreators.com) IS publicly documented and confirmed working on
# the first real correctly-shaped call. Per the Treg skill's own "Lessons
# learned": an earlier agent spike invented plausible-sounding per-post
# prices that were 30-100x off; this think tank does not repeat that mistake --
# the price below was independently re-derived from the actual billed
# balance delta across 2 real live calls ($0.00376 / 2 = $0.00188), matching
# the catalog's own stated price exactly. Prices can still drift over time
# without this constant being updated -- re-verify before relying on it long
# after.
# Metered endpoint prices live in world/service_costs.json (config.py).


def _treg_call(endpoint_id, body=None, method='POST', timeout=30, query=None):
    """Real call through Treg's proxy: https://treg.to/call/{endpoint_id}
    with X-Treg-Token, params matching the upstream provider's own API shape
    (Treg is a pass-through -- "you make the real upstream request", per its
    own docs). Same vault pattern as every other external credential (see
    library/skills/treg.md's stated policy): the caller (a spike tool
    executor) never holds the raw token, only this server-side chokepoint
    does. Returns (data, error) -- error is a human-readable string, data is
    the parsed JSON response (or a truncated raw-text fallback if the
    response isn't JSON). Cost is NOT parsed from the response (no reliable
    universal shape across providers) -- callers accrue the KNOWN catalog
    price from TREG_ENDPOINT_COSTS on a real success, matching the
    conservative "don't fabricate a number, use a verified one" rule this
    think tank already applies to the digitalocean/pixellab integrations.

    GET vs POST param placement confirmed LIVE, not guessed:
    Treg's own real error for a GET endpoint called with a JSON body was
    explicit -- "x.x.get-trends-by-woeid is GET -- add --method GET", then
    "needs --query woeid=<value> (a path parameter of /2/trends/by/woeid/
    {woeid})" -- i.e. a GET call's params belong in the URL query string,
    never a request body; a POST call's params are the JSON body, as
    originally assumed and confirmed working for the LinkedIn search call.

    `query` (also confirmed): some POST endpoints STILL
    need URL query params on top of a JSON body -- Apify-backed calls
    (e.g. apify.linkedin.search.jobs) rejected a plain JSON body with
    "Apify platform calls take maxTotalChargeUsd... /timeout...", and
    those only registered once passed as `?maxTotalChargeUsd=...&timeout=...`
    alongside the body, not inside it. `query` is independent of `body` so
    a GET call can keep using `body` as before; a POST call gets both."""
    token = _open_secret(_credential_token('treg') or '')
    if not token:
        return None, 'Treg is not configured (no credential in the vault)'
    url = f'https://treg.to/call/{endpoint_id}'
    headers = {'X-Treg-Token': token}
    data_bytes = None
    if method == 'GET':
        params = dict(query or {})
        if body:
            params.update(body)
        if params:
            url += '?' + urllib.parse.urlencode(params)
    else:
        if query:
            url += '?' + urllib.parse.urlencode(query)
        data_bytes = json.dumps(body or {}).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    try:
        req = urllib.request.Request(url, data=data_bytes, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 -- fixed Treg API host
            raw = resp.read().decode('utf-8', errors='replace')
        try:
            return json.loads(raw), None
        except ValueError:
            return {'_raw': raw[:5000]}, None
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:500]
        return None, f'Treg call failed ({e.code}): {detail}'
    except Exception as e:
        return None, f'Treg call failed: {e}'


# YouTube transcript support. The serving endpoint (_youtube_transcript_colab)
# always downloads the audio via the Apify actor and transcribes it on the
# Colab runtime -- every video treated the same. The local yt-dlp captions
# helper below (_youtube_transcript) is kept as a tested utility: it reads a
# video's OWN subtitles/auto-captions (never the audio/video) when present,
# free of any metered API, and is where a no-captions signal can be observed
# without spending Colab time.
_YOUTUBE_HOST_RE = re.compile(r'^(?:[a-z0-9-]+\.)*youtu(?:\.be|be\.com)$')
YOUTUBE_TRANSCRIPT_MAX_CHARS = 50000
YOUTUBE_TRANSCRIPT_MAX_SEGMENTS = 800
# Where extracted transcripts are filed so ANY agent can read them: the shared
# Library's media/ area (same tree as media/feeds.md and media/digests/ the
# Studio room reads and writes). One plain-text file per video, named by its
# video id -- searchable via /api/library/search and readable by any agent via
# /api/library/file. Written by the endpoint after a successful extraction.
YOUTUBE_TRANSCRIPTS_DIR = os.path.join(LIBRARY_DIR, 'media', 'transcripts')

# Designmind-style design references: a project-based directory the player
# seeds with real design briefs (an uploaded image, or a live-URL screenshot
# that gets a vision pass). Every file here is plain text in the same shape
# designmind.studioerlach.com produces -- palette, typography, layout, style --
# so ANY agent can read one via /api/library/file and find it via
# /api/library/search, and build against a concrete, shared visual vocabulary
# instead of guessing what "clean" or "modern" means.
DESIGN_REFERENCES_DIR = os.path.join(LIBRARY_DIR, 'design-references')


def _is_youtube_url(url):
    """True only for a real youtube.com / youtu.be URL (the fixed host set
    yt-dlp is allowed to touch). Never an arbitrary user-supplied host."""
    url = (url or '').strip()
    if not url.lower().startswith(('https://', 'http://')):
        return False
    try:
        host = urllib.parse.urlparse(url).hostname or ''
    except Exception:
        return False
    return bool(_YOUTUBE_HOST_RE.match(host))


def _youtube_video_id(url):
    """Extract the 11-char video id from a youtube.com / youtu.be URL, or
    None. Matches watch?v=, /shorts/, /live/, embed/ and youtu.be/ forms."""
    url = (url or '').strip()
    if not _is_youtube_url(url):
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    if parsed.netloc and parsed.netloc.startswith('youtu.be'):
        return (parsed.path or '').strip('/')[:11] or None
    qs = urllib.parse.parse_qs(parsed.query)
    v = (qs.get('v') or [None])[0]
    if v and len(v) == 11:
        return v
    for prefix in ('/shorts/', '/live/', '/embed/'):
        if (parsed.path or '').startswith(prefix):
            return (parsed.path or '').split(prefix, 1)[1].strip('/')[:11] or None
    return None


def _file_youtube_transcript(url, text, source):
    """Persist an extracted transcript to the shared Library's media/
    transcripts/ tree (YOUTUBE_TRANSCRIPTS_DIR) so ANY agent can read it via
    /api/library/file and find it via /api/library/search -- the Studio's
    media-building lane reads this same media/ area. Writes one plain-text
    file per video id (the id that a re-fetch updates in place), prefixed
    with a small header of when/what fetched it. Never raises; returns the
    Library-relative path on success or None on any failure -- the transcript
    is already returned to the caller regardless, so a write failure must not
    fail the request."""
    vid = _youtube_video_id(url)
    if not vid:
        return None
    try:
        os.makedirs(YOUTUBE_TRANSCRIPTS_DIR, exist_ok=True)
        rel = os.path.join('media', 'transcripts', f'{vid}.txt')
        target = _safe_library_path(rel)
        if not target:
            return None
        header = (f'# YouTube transcript\n\n'
                  f'- Video: {url}\n'
                  f'- Video id: {vid}\n'
                  f'- Fetched: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n'
                  f'- Source: {source}\n\n')
        _write_file(target, header + text)
        return rel
    except Exception:
        return None


def _cached_youtube_transcript(url):
    """Return (body, library-relative path) for an already-filed transcript, or
    None. The cache is the shared media/transcripts/{video_id}.txt file. This
    is checked before the free yt-dlp route and the metered Colab fallback so
    a retry, a second reviewer, or a later panel reads the existing transcript
    instead of paying to transcribe the same video twice."""
    vid = _youtube_video_id(url)
    if not vid:
        return None
    rel = os.path.join('media', 'transcripts', f'{vid}.txt')
    target = _safe_library_path(rel)
    if not target or not os.path.isfile(target):
        return None
    try:
        raw = open(target, encoding='utf-8', errors='replace').read()
    except OSError:
        return None
    if raw.startswith('# YouTube transcript\n\n'):
        parts = raw.split('\n\n', 2)
        raw = parts[2] if len(parts) == 3 else raw
    body = raw.strip()
    if not body:
        return None
    return body, rel


def _clean_subtitle_file(path):
    """Strip VTT/SRT timing + markup into deduped plain text. YouTube's
    aligned VTT repeats each caption frame across every cue line, so the
    dedup step (collapse consecutive identical lines) is what turns the
    noisy ~1MB file into readable transcript text."""
    try:
        raw = open(path, encoding='utf-8', errors='replace').read()
    except Exception:
        return None
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or '-->' in stripped or stripped.isdigit():
            continue
        text = re.sub(r'<[^>]+>', '', stripped)
        if not text.strip():
            continue
        if lines and text == lines[-1]:
            continue
        lines.append(text)
    # Second pass: collapse N>1 consecutive repeats that survived the first
    # pass (some captioners emit two identical lines that differ only in
    # trailing spaces/tags that stripping already normalized).
    clean = []
    for text in lines:
        if clean and text == clean[-1]:
            continue  # pragma: no cover -- first pass already collapses consecutive duplicates
        clean.append(text)
    return '\n'.join(clean)


def _youtube_transcript(url, lang='en', timeout=90):
    """Fetch the transcript of a YouTube video via local yt-dlp. Returns
    (text, None) on success or (None, error-string) on failure. Free -- no
    metered API involved, so nothing is accrued against any balance."""
    url = (url or '').strip()
    if not _is_youtube_url(url):
        return None, f'Not a YouTube URL (only youtube.com / youtu.be are allowed): {url}'
    try:
        with tempfile.TemporaryDirectory(prefix='yt_transcript_') as tmp:
            tmpl = os.path.join(tmp, 'yt_sub')
            cmd = [
                'yt-dlp', '--skip-download', '--no-playlist',
                '--write-auto-subs', '--write-subs',
                '--sub-langs', f'{lang}.*',
                '--sub-format', 'vtt/srt/best',
                '-o', tmpl, url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                # yt-dlp exits non-zero for "no subtitles" too -- surface its
                # own last line (the actual reason) rather than guessing.
                tail = (result.stderr or result.stdout or '').strip().splitlines()
                reason = (tail[-1] if tail else 'yt-dlp failed').strip()
                return None, f'yt-dlp could not fetch subtitles ({result.returncode}): {reason[:300]}'
            subs = sorted(f for f in os.listdir(tmp)
                          if f.startswith('yt_sub') and f.endswith(('.vtt', '.srt')))
            if not subs:
                return None, 'yt-dlp succeeded but produced no subtitle files (no captions for this video).'
            text = _clean_subtitle_file(os.path.join(tmp, subs[0]))
            if not text:
                return None, 'Subtitle file downloaded but produced no readable text.'
            segments = text.splitlines()
            if len(segments) > YOUTUBE_TRANSCRIPT_MAX_SEGMENTS:
                text = '\n'.join(segments[:YOUTUBE_TRANSCRIPT_MAX_SEGMENTS])
            if len(text) > YOUTUBE_TRANSCRIPT_MAX_CHARS:
                text = text[:YOUTUBE_TRANSCRIPT_MAX_CHARS]
            return text, None
    except subprocess.TimeoutExpired:
        return None, f'yt-dlp timed out after {timeout}s (video too long or network stalled).'
    except FileNotFoundError:
        return None, 'yt-dlp is not installed on the host (need `pip install yt-dlp`).'
    except Exception as e:
        return None, f'YouTube transcript fetch failed: {e}'


# The SPEND fallback for /api/youtube-transcript: the endpoint tries the free
# local yt-dlp captions path (_youtube_transcript) first and only reaches this
# when that finds no captions, so the paid download-and-transcribe route is
# spent solely on videos captions can't serve. (This reverses the earlier
# "force it to always download ... treat them all the same" decision.)
# The download runs through the Apify youtube-link actor (IHDsaLO64Wge9wSWx),
# and the WHOLE orchestration -- start the actor run, poll it to SUCCEEDED,
# fetch the dataset item, download the audio, run faster-whisper -- executes
# INSIDE the Colab runtime, never on the host: no video audio ever touches
# this machine, and the host only sends code + the APIFY_API_KEY env var and
# reads stdout. Budget-gated by the same _colab_budget_exceeded guard the
# run_on_colab tool uses; runs as the player's own Colab account on the free
# tier (unlimited usage, not runtime-guaranteed -- a refused/cooldown slot
# returns an honest error, never a fabricated transcript). The api.apify.com
# host is allowlisted (see BROWSE_ALLOWLIST_DOMAINS) so the Colab URL gate
# passes without a Jev round trip, exactly like dreyx.com before it. Returns
# (text, None) or (None, error-string); fails closed on any failure.
def _youtube_transcript_colab(url, lang='en', model_size='small'):
    """Whisper-transcribe a YouTube video on a Colab runtime: the Apify actor
    downloads the audio, faster-whisper transcribes it -- all on the runtime."""
    url = (url or '').strip()
    if not _is_youtube_url(url):
        return None, f'Not a YouTube URL (only youtube.com / youtu.be are allowed): {url}'
    if not COLAB_ENABLED:
        return None, 'Colab is disabled (COLAB_ENABLED=0) -- cannot download and transcribe the audio.'
    if not COLAB_CLI_AVAILABLE:
        return None, 'Colab CLI is not installed -- cannot download and transcribe the audio.'
    if _colab_budget_exceeded():
        return None, 'Colab usage cap reached for this period -- cannot download and transcribe the audio.'
    if not APIFY_API_KEY:
        return None, ('APIFY_API_KEY is not configured -- it is required to download the audio '
                      'on the Colab runtime (the Apify actor does the YouTube download).')
    # Everything runs INSIDE the Colab runtime, never on the host: the runtime
    # starts the Apify actor run with APIFY_API_KEY (passed via env), polls it
    # to SUCCEEDED, fetches the dataset item's downloadUrl, downloads the audio
    # (retrying with a token-suffixed URL for private KV stores), and runs
    # faster-whisper on it. Fail-closed: any failure prints __ERROR__ and the
    # run is only accepted as a transcript when it ends with the
    # __TRANSCRIPT_END__ marker -- a mid-run crash can never masquerade as text.
    code = (
        "import os, sys, json, time, urllib.request, urllib.error\n"
        "from faster_whisper import WhisperModel\n"
        "api_key = os.environ.get('APIFY_API_KEY', '')\n"
        "url = %r\n"
        "lang = %r\n"
        "out_path = '/content/yt_audio.webm'\n"
        "def api(path, method='GET', body=None):\n"
        "    req = urllib.request.Request('https://api.apify.com/v2' + path,\n"
        "        data=json.dumps(body).encode('utf-8') if body is not None else None,\n"
        "        method=method,\n"
        "        headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'})\n"
        "    with urllib.request.urlopen(req, timeout=120) as resp:\n"
        "        return json.loads(resp.read().decode('utf-8', errors='replace'))\n"
        "if not api_key:\n"
        "    print('__ERROR__: APIFY_API_KEY not set on the runtime')\n"
        "    sys.exit(1)\n"
        "try:\n"
        "    run = api('/acts/IHDsaLO64Wge9wSWx/runs', 'POST',\n"
        "              {'videos': [{'url': url}], 'audioQuality': 'best'})\n"
        "except Exception as e:\n"
        "    print('__ERROR__: actor start failed: ' + repr(e)[:300])\n"
        "    sys.exit(1)\n"
        "run = run.get('data') if isinstance(run, dict) else run\n"
        "run_id = run.get('id') if isinstance(run, dict) else None\n"
        "dataset_id = run.get('defaultDatasetId') if isinstance(run, dict) else None\n"
        "if not run_id or not dataset_id:\n"
        "    print('__ERROR__: actor run response missing id/defaultDatasetId: ' + repr(run)[:300])\n"
        "    sys.exit(1)\n"
        "status = ''\n"
        "deadline = time.time() + 300\n"
        "while time.time() < deadline:\n"
        "    try:\n"
        "        poll = api('/actor-runs/' + str(run_id))\n"
        "        status = (poll.get('data') if isinstance(poll, dict) else {}).get('status', '')\n"
        "    except Exception:\n"
        "        status = ''\n"
        "    if status == 'SUCCEEDED':\n"
        "        break\n"
        "    if status in ('FAILED', 'ABORTED', 'TIMED-OUT'):\n"
        "        print('__ERROR__: Apify actor run ended ' + status)\n"
        "        sys.exit(1)\n"
        "    time.sleep(4)\n"
        "else:\n"
        "    print('__ERROR__: Apify actor run did not finish in time')\n"
        "    sys.exit(1)\n"
        "try:\n"
        "    items = api('/datasets/' + str(dataset_id) + '/items?limit=1')\n"
        "except Exception as e:\n"
        "    print('__ERROR__: could not fetch dataset items: ' + repr(e)[:300])\n"
        "    sys.exit(1)\n"
        "if not isinstance(items, list):\n"
        "    items = (items.get('data') if isinstance(items, dict) else None) or []\n"
        "download_url = None\n"
        "if isinstance(items, list):\n"
        "    for item in items:\n"
        "        if isinstance(item, dict):\n"
        "            download_url = item.get('downloadUrl') or item.get('download_url')\n"
        "            if download_url:\n"
        "                break\n"
        "if not download_url:\n"
        "    print('__ERROR__: no downloadUrl in the actor dataset output')\n"
        "    sys.exit(1)\n"
        "try:\n"
        "    with urllib.request.urlopen(download_url, timeout=300) as resp:\n"
        "        data = resp.read()\n"
        "except urllib.error.HTTPError:\n"
        "    try:\n"
        "        with urllib.request.urlopen(download_url + '?token=' + api_key, timeout=300) as resp:\n"
        "            data = resp.read()\n"
        "    except Exception as e:\n"
        "        print('__ERROR__: could not download the audio: ' + repr(e)[:300])\n"
        "        sys.exit(1)\n"
        "except Exception as e:\n"
        "    print('__ERROR__: could not download the audio: ' + repr(e)[:300])\n"
        "    sys.exit(1)\n"
        "with open(out_path, 'wb') as f:\n"
        "    f.write(data)\n"
        "if not data or os.path.getsize(out_path) < 1000:\n"
        "    print('__ERROR__: downloaded audio is empty or too small')\n"
        "    sys.exit(1)\n"
        "import traceback\n"
        "try:\n"
        "    import glob\n"
        "    if os.path.exists('/usr/local/cuda'):\n"
        "        for base in ('libcublas', 'libcublasLt', 'libcudart'):\n"
        "            link = '/usr/lib64-nvidia/' + base + '.so.12'\n"
        "            if os.path.lexists(link):\n"
        "                continue\n"
        "            matches = sorted(glob.glob('/usr/local/cuda*/lib64/' + base + '.so'))\n"
        "            if matches and os.path.isdir('/usr/lib64-nvidia'):\n"
        "                try:\n"
        "                    os.symlink(matches[-1], link)\n"
        "                except Exception:\n"
        "                    pass\n"
        "    model = WhisperModel(%r, device='cuda' if os.path.exists('/usr/local/cuda') else 'cpu',\n"
        "                         compute_type='float16' if os.path.exists('/usr/local/cuda') else 'int8')\n"
        "    segments, _info = model.transcribe(out_path, language=(lang if lang != 'en' else None),\n"
        "                                        beam_size=3)\n"
        "    print('__TRANSCRIPT_BEGIN__')\n"
        "    for seg in segments:\n"
        "        print(seg.text.strip())\n"
        "    print('__TRANSCRIPT_END__')\n"
        "except Exception:\n"
        "    print('__ERROR__: whisper failed:')\n"
        "    traceback.print_exc()\n"
        "    sys.exit(1)\n"
    ) % (url, lang, model_size)
    purpose = 'transcribe a YouTube video by downloading its audio'
    try:
        result = _colab_compute_run('player', code, purpose,
                                    packages=['faster-whisper', 'av==13.1.0'],
                                    timeout_seconds=min(600, COLAB_TIMEOUT_MAX_S),
                                    runtimes=1, kind='gpu',
                                    env={'APIFY_API_KEY': APIFY_API_KEY or ''})
    except Exception as e:
        return None, f'Colab transcription run crashed: {e}'
    if not isinstance(result, dict):
        return None, 'Unexpected Colab transcription response.'
    if result.get('error'):
        return None, f'Colab transcription failed: {result["error"]}'
    stdout = (result.get('stdout') or '').strip()
    if '__ERROR__' in stdout:
        detail = next((line.split('__ERROR__:', 1)[1].strip() for line in stdout.splitlines()
                       if '__ERROR__' in line), '')
        return None, ('Colab could not download or transcribe the audio on the runtime'
                      + (f': {detail[:200]}' if detail else '.') + '.')
    if '__TRANSCRIPT_END__' not in stdout:
        return None, 'Colab transcription did not complete (no end marker) -- the run died partway.'
    if '__TRANSCRIPT_BEGIN__' in stdout:
        body = stdout.split('__TRANSCRIPT_BEGIN__', 1)[1].split('__TRANSCRIPT_END__', 1)[0]
    else:
        body = stdout.rsplit('__TRANSCRIPT_END__', 1)[0]
    lines = [l.strip() for l in body.splitlines()
             if l.strip() and l.strip() not in ('__TRANSCRIPT_BEGIN__', '__TRANSCRIPT_END__')]
    if not lines:
        return None, 'Colab transcription returned no text.'
    text = '\n'.join(lines)
    if len(text) > YOUTUBE_TRANSCRIPT_MAX_CHARS:
        text = text[:YOUTUBE_TRANSCRIPT_MAX_CHARS]
    return text, None


def _pixellab_token():
    """PixelLab bearer token: the vault credential first (the historical
    path), then the operator's PIXELLAB_API_KEY in .env. Env is the shipped
    path for fresh installs and the fallback for an existing vault row with no
    stored secret."""
    token = _open_secret(_credential_token('pixellab') or '')
    if token:
        return token
    return (_load_env().get('PIXELLAB_API_KEY') or '').strip() or None


_PIXELLAB_BALANCE_CACHE = {'at': 0.0, 'data': None}
PIXELLAB_BALANCE_CACHE_TTL_S = float(_load_env().get('BALANCE_CACHE_TTL_S', '300') or 300)


def _pixellab_account_balance(force=False):
    """Live real spend for the PixelLab account. Its GET /v2/balance reports
    remaining USD credits (verified in
    library/skills/pixellab.md), not spend-to-date, and PixelLab has no
    separate 'starting balance' endpoint -- so this tracks DEPLETION since the
    first observed balance this process has seen, seeded via the config-time
    `PIXELLAB_STARTING_CREDITS_USD` if set. Falls back to reporting $0 spent
    on the very first call of a process (nothing to compare against yet) --
    a real gap noted below, not a silent lie: the very first mint after a
    restart cannot detect PRIOR depletion, only depletion observed from here
    on. Returns None on any failure (fails closed).

    `force=True` bypasses the cached value (still refreshes the cache after)
    -- needed by a real generation call that wants a before/after balance
    delta as its real cost: two calls inside the same TTL window would
    otherwise both return the same stale cached number and show zero cost
    even after a real, billed generation."""
    token = _pixellab_token()
    if not token:
        return None
    now = time.time()
    cached = _PIXELLAB_BALANCE_CACHE
    if not force and cached.get('data') is not None and (now - cached['at']) < PIXELLAB_BALANCE_CACHE_TTL_S:
        return cached['data']
    try:
        req = urllib.request.Request('https://api.pixellab.ai/v2/balance',
                                     headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 -- fixed PixelLab API host
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
        remaining = float((data.get('credits') or {}).get('usd') or 0)
        if 'baseline' not in cached:
            cached['baseline'] = remaining  # first observation this process has made
        spent = max(0.0, cached['baseline'] - remaining)
        cached['at'] = now
        cached['data'] = spent
        return spent
    except Exception:
        return None
