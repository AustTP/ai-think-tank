

def _run_browser_act_sync(sandbox_dir, action, agent_id=None):
    """Run ONE already-classified browser_act op in the sandbox and return the
    parsed result dict from the driver's result file (falling back to parsing
    its stdout JSON). Returns None on harness-level failure (docker/IO), never
    a browser-level 'error' -- those come back inside the parsed result.

    The action file can carry a DECRYPTED credential (the vault injection
    path), so it never lands in the shared /workspace mount: any concurrent
    /api/execute command runs in that same mount and could read it, and the
    next _snapshot_sandbox would git-commit it. It is written to a host-side
    temp dir instead, bind-mounted read-only into ONLY this browser container
    (never into the agent exec sandbox), and deleted in the finally below.
    Result/screenshot stay in the mount -- they carry page state, never input
    values (the driver strips those -- see _page_state / the fill pop)."""
    import secrets as _secrets
    work_dir = os.path.join(sandbox_dir, '.browser-act')
    os.makedirs(work_dir, exist_ok=True)
    run_id = _secrets.token_hex(4)
    result_path = os.path.join(work_dir, f'result-{run_id}.json')
    shot_path = os.path.join(work_dir, f'shot-{run_id}.png')
    action['workDir'] = '/workspace/.browser-act'
    action['profileDir'] = '/browser-profile'
    action['resultPath'] = f'/workspace/.browser-act/result-{run_id}.json'
    # Refresh the per-agent profile's Safari cookie seed before the container
    # starts. The driver re-applies it to the persistent context on every run.
    # A malformed URL is the driver's error to report, not a crash here.
    try:
        _act_host = urllib.parse.urlparse(
            action.get('url') or action.get('pageUrl') or '').hostname or ''
    except ValueError:
        _act_host = ''
    _sync_safari_cookies(agent_id, _act_host)
    action['screenshotPath'] = f'/workspace/.browser-act/shot-{run_id}.png'
    # Let the driver know whether it may attempt a Scrapling-style Cloudflare
    # solve. The driver runs in the sandbox, which does not inherit the host
    # .env, so the mode travels in the action document. An explicit value (used
    # by the agentic nested call to disable re-solving) wins.
    action.setdefault('autoSolve',
                      (_load_env().get('CAPTCHA_AUTO_SOLVE') or 'off').strip().lower())
    # A live session for this agent owns the browser: run the op against it so
    # the player can watch and step in. Only this falls through to launching a
    # new one-shot Chromium.
    try:
        session = browser_session.get(agent_id) if agent_id else None
    except browser_session.BrowserSessionError:
        session = None
    if session and session.get('status') == 'stopped':
        session = None
    if session:
        # The session container mounts its OWN sandbox dir at /workspace, so
        # the result and shot must be read from there, not from this call's
        # dir. Paths inside the container stay identical.
        session_dir = browser_session.session_sandbox_dir(agent_id) or sandbox_dir
        session_work = os.path.join(session_dir, '.browser-act')
        os.makedirs(session_work, exist_ok=True)
        result_path = os.path.join(session_work, f'result-{run_id}.json')
        shot_path = os.path.join(session_work, f'shot-{run_id}.png')
        try:
            grant_host = urllib.parse.urlparse(
                action.get('url') or action.get('pageUrl') or '').hostname or ''
        except ValueError:
            grant_host = ''
        if grant_host and not _is_allowlisted_host(grant_host):
            _grant_egress_host(grant_host)
        try:
            result = browser_session.exec_driver(
                agent_id, action, timeout_s=BROWSER_ACT_TIMEOUT_S)
        except browser_session.BrowserSessionError as e:
            return None, str(e)
        except subprocess.TimeoutExpired:
            return None, 'browser action timed out in session'
        finally:
            if grant_host and not _is_allowlisted_host(grant_host):
                _revoke_egress_host(grant_host)
        return _read_browser_act_result(result, result_path, shot_path)
    # Host-side, outside the mount. Deleted in finally -- it is never
    # world-readable from any other container and never survives the run.
    action_dir = tempfile.mkdtemp(prefix='browser_act_')
    action_path = os.path.join(action_dir, f'action-{run_id}.json')
    try:
        with open(action_path, 'w') as f:
            json.dump(action, f)
    except OSError as e:
        shutil.rmtree(action_dir, ignore_errors=True)
        return None, f'could not write browser action to sandbox: {e}'
    # JEV-approved egress: grant the target host in the shared grants file the
    # proxy re-reads, so a Jev-approved non-allowlisted host is genuinely
    # reachable by the browser. Revoked in a finally below -- the grant never
    # outlives this one action run.
    try:
        grant_host = urllib.parse.urlparse(
            action.get('url') or action.get('pageUrl') or '').hostname or ''
    except Exception:
        grant_host = ''
    if grant_host and not _is_allowlisted_host(grant_host):
        _grant_egress_host(grant_host)
    proxy_url = f'http://{PROXY_CONTAINER}:{PROXY_PORT}'
    docker_cmd = [
        'docker', 'run', '--rm',
        '--network', SANDBOX_NETWORK,
        '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges',
        '-e', f'http_proxy={proxy_url}', '-e', f'https_proxy={proxy_url}',
        '-e', f'HTTP_PROXY={proxy_url}', '-e', f'HTTPS_PROXY={proxy_url}',
        '-e', f'BROWSER_PROXY_URL={proxy_url}',
        '--memory', '512m',
        '--cpus', '1',
        '--pids-limit', '128',
        '--shm-size', '256m',
        '-v', f'{sandbox_dir}:/workspace',
        # Read-only, and mounted into THIS browser container only: the agent
        # exec sandbox never sees /browser-act, so a concurrent shell cannot
        # read the (decrypted) action file, and nothing in the mount can be
        # swapped between the Jev classification and the driver reading it.
        '-v', f'{action_dir}:/browser-act:ro',
        '-v', f'{_browser_profile_dir(agent_id)}:/browser-profile',
        '-w', '/workspace',
        SANDBOX_IMAGE,
        # Headful Chromium under a private Xvfb. Do NOT use xvfb-run here:
        # in this container it hangs before running the child command. Start
        # Xvfb directly, wait for its socket, then run the driver.
        'sh', '-c',
        ('Xvfb :99 -screen 0 1280x900x24 >/tmp/xvfb.log 2>&1 & '
         'export DISPLAY=:99; '
         'for i in $(seq 1 25); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.2; done; '
         'python3 /opt/browser_driver.py "$1"; rc=$?; '
         'kill %1 2>/dev/null; exit $rc'),
        'sh', f'/browser-act/action-{run_id}.json',
    ]
    try:
        try:
            # One Chromium profile at a time: a persistent user-data dir cannot
            # be opened by two browsers concurrently.
            profile_dir = _browser_profile_dir(agent_id)
            os.makedirs(profile_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(profile_dir, 0o700)
            except OSError:
                pass
            with _BROWSER_PROFILE_LOCK:
                result = _run_docker_sandbox(docker_cmd, BROWSER_ACT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return None, 'browser action timed out in sandbox'
        return _read_browser_act_result(result, result_path, shot_path)
    finally:
        if grant_host and not _is_allowlisted_host(grant_host):
            _revoke_egress_host(grant_host)
        shutil.rmtree(action_dir, ignore_errors=True)


# How long real JS/AJAX content gets to finish loading before the text is
# read back out -- a FIXED settle window, not "wait for network idle"
# (page.goto's own wait_until option): a feed site's background polling or
# websockets can mean the network never truly goes idle, which would just
# burn the whole request timeout waiting for something that was never
# coming. Matches the ~4s window _screenshot_url_sync already uses for the
# same real-external-page-rendering job.
RENDER_SETTLE_MS = 3000
RENDER_TIMEOUT_S = 20


def _fetch_rendered_page_sync(url, proxy=None):
    # Real fix for a real, confirmed gap: _fetch_page_sync (plain urllib)
    # only ever sees a page's INITIAL HTML. For a site that renders its
    # actual content client-side via JS/AJAX -- Reddit, Twitter/X, and any
    # modern single-page app -- that's a near-empty shell, not the real
    # content. Reuses the exact Playwright/Chromium already installed for
    # page-probe and screenshots. Real network access, like
    # _screenshot_url_sync and unlike page-probe's blackholed sandbox use
    # -- this renders a real external page, which needs the real internet
    # to look like anything. `proxy` (a sandbox VPN exit's proxy URL)
    # routes the whole browser through that exit -- Playwright natively
    # supports an HTTP proxy at launch, so every request the page makes
    # (including its AJAX) egresses via the named container.
    with sync_playwright() as p:
        launch = {'headless': True}
        if proxy:
            launch['proxy'] = {'server': proxy}
        browser = p.chromium.launch(**launch)
        try:
            page = browser.new_page()
            page.goto(url, timeout=RENDER_TIMEOUT_S * 1000, wait_until='domcontentloaded')
            page.wait_for_timeout(RENDER_SETTLE_MS)
            final_url = page.url
            # Re-check the FINAL host, same reasoning as _fetch_page_sync's
            # own re-check: a redirect (including a client-side JS one,
            # which only a real browser would ever follow) is exactly how
            # an allowed-looking URL could still end up somewhere unsafe.
            final_host = urllib.parse.urlparse(final_url).hostname
            if not _is_safe_public_host(final_host):
                raise ValueError('redirected to a disallowed host')
            text = page.evaluate('document.body ? document.body.innerText : ""') or ''
            links_raw = page.evaluate(
                "Array.from(document.querySelectorAll('a[href]')).slice(0, 200)"
                ".map(a => ({url: a.href, text: (a.textContent || '').trim().slice(0, 100)}))"
            ) or []
            links = [l for l in links_raw if l.get('url', '').startswith(('http://', 'https://'))]
            return final_url, text, links
        finally:
            browser.close()


HTTP_CACHE_DIR = os.path.join(THINK_TANK_DIR, '.http_cache')
HTTP_CACHE_TTL_S = 6 * 3600


def _http_cache_paths(url, proxy=None, namespace=''):
    key = hashlib.sha256((str(namespace) + '\n' + str(proxy or '') + '\n' + url).encode()).hexdigest()
    return (os.path.join(HTTP_CACHE_DIR, key + '.json'),
            os.path.join(HTTP_CACHE_DIR, key + '.body'))


def _read_http_cache(url, proxy=None, namespace=''):
    meta_path, body_path = _http_cache_paths(url, proxy, namespace)
    try:
        with open(meta_path, encoding='utf-8') as fh:
            meta = json.load(fh)
        with open(body_path, 'rb') as fh:
            body = fh.read().decode('utf-8', errors='replace')
    except (OSError, ValueError):
        return None
    meta['body'] = body
    return meta


def _write_http_cache(url, proxy, final_url, content_type, body, truncated,
                      etag, last_modified, ttl_s, last_modified_header='',
                      namespace=''):
    os.makedirs(HTTP_CACHE_DIR, exist_ok=True)
    meta_path, body_path = _http_cache_paths(url, proxy)
    meta = {
        'url': url, 'final_url': final_url, 'content_type': content_type,
        'truncated': bool(truncated), 'etag': etag or '',
        'last_modified': last_modified,
        'last_modified_header': last_modified_header or '',
        'expires': time.time() + max(0, ttl_s),
    }
    tmp_meta, tmp_body = meta_path + '.tmp', body_path + '.tmp'
    with open(tmp_body, 'w', encoding='utf-8') as fh:
        fh.write(body)
    with open(tmp_meta, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh)
    os.replace(tmp_body, body_path)
    os.replace(tmp_meta, meta_path)


def _read_http_cache_bytes(url, proxy=None, namespace=''):
    meta_path, body_path = _http_cache_paths(url, proxy, namespace)
    try:
        with open(meta_path, encoding='utf-8') as fh:
            meta = json.load(fh)
        with open(body_path, 'rb') as fh:
            meta['raw'] = fh.read()
    except (OSError, ValueError):
        return None
    return meta


def _write_http_cache_bytes(url, proxy, final_url, content_type, raw, truncated,
                            etag, last_modified, ttl_s, last_modified_header='',
                            namespace=''):
    os.makedirs(HTTP_CACHE_DIR, exist_ok=True)
    meta_path, body_path = _http_cache_paths(url, proxy, namespace)
    meta = {
        'url': url, 'final_url': final_url, 'content_type': content_type,
        'truncated': bool(truncated), 'etag': etag or '',
        'last_modified': last_modified,
        'last_modified_header': last_modified_header or '',
        'expires': time.time() + max(0, ttl_s),
    }
    tmp_meta, tmp_body = meta_path + '.tmp', body_path + '.tmp'
    with open(tmp_body, 'wb') as fh:
        fh.write(raw)
    with open(tmp_meta, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh)
    os.replace(tmp_body, body_path)
    os.replace(tmp_meta, meta_path)


def _cache_ttl_from_headers(headers):
    cc = (headers.get('Cache-Control') or '').lower()
    if 'no-store' in cc:
        return None
    if 'no-cache' in cc:
        return 0
    m = re.search(r'max-age=(\d+)', cc)
    if m:
        return max(1, min(int(m.group(1)), 24 * 3600))
    return HTTP_CACHE_TTL_S


def _fetch_page_sync(url, proxy=None):
    cached = _read_http_cache(url, proxy)
    now = time.time()
    if cached and cached.get('expires', 0) > now:
        return (cached.get('final_url') or url, cached.get('content_type') or '',
                cached.get('body') or '', bool(cached.get('truncated')),
                cached.get('last_modified'))
    headers = {'User-Agent': 'AIThinkTankAgent/1.0'}
    if cached and cached.get('etag'):
        headers['If-None-Match'] = cached['etag']
    elif cached and cached.get('last_modified_header'):
        headers['If-Modified-Since'] = cached['last_modified_header']
    req = urllib.request.Request(url, headers=headers, method='GET')
    # proxy routes this one fetch through a sandbox VPN exit's proxy;
    # _safe_urlopen pins the proxy's own address and carries the origin
    # over urllib's standard ProxyHandler semantics (CONNECT for https),
    # so the SSRF pinning discipline is preserved on both hops.
    try:
        with _safe_urlopen(req, timeout=BROWSE_TIMEOUT_S, proxy=proxy) as resp:  # user URLs pre-cleared by _jev_safety_gate's SSRF hostname guard (:2157); _safe_urlopen pins each resolved IP so DNS cannot rebind to an internal address
            final_url = resp.geturl()
            # Re-check the FINAL host after redirects -- a redirect chain is
            # exactly how an allowed-looking URL could still end up pointed at
            # an internal address.
            final_host = urllib.parse.urlparse(final_url).hostname
            if not _is_safe_public_host(final_host):
                raise ValueError('redirected to a disallowed host')
            content_type = resp.headers.get('Content-Type', '')
            last_modified = _parse_http_date_ms(resp.headers.get('Last-Modified'))
            raw = resp.read(BROWSE_MAX_BYTES + 1)
            truncated = len(raw) > BROWSE_MAX_BYTES
            raw = raw[:BROWSE_MAX_BYTES]
            body = raw.decode(errors='replace')
            ttl = _cache_ttl_from_headers(resp.headers)
            if ttl is not None:
                _write_http_cache(url, proxy, final_url, content_type, body,
                                  truncated, resp.headers.get('ETag'),
                                  last_modified, ttl,
                                  resp.headers.get('Last-Modified') or '')
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            # Revalidated: refresh the expiry and reuse the stored body.
            meta_path, _ = _http_cache_paths(url, proxy)
            try:
                with open(meta_path, encoding='utf-8') as fh:
                    meta = json.load(fh)
                meta['expires'] = now + HTTP_CACHE_TTL_S
                with open(meta_path, 'w', encoding='utf-8') as fh:
                    json.dump(meta, fh)
            except OSError:
                pass
            return (cached.get('final_url') or url, cached.get('content_type') or '',
                    cached.get('body') or '', bool(cached.get('truncated')),
                    cached.get('last_modified'))
        raise
    return final_url, content_type, body, truncated, last_modified


# llm_resilience research: retries only genuinely
# TRANSIENT failures (timeouts, rate limits, upstream 5xx) with backoff --
# deliberately does NOT retry 4xx client errors (a bad model slug, a
# malformed request), since retrying those just wastes calls on something
# that will fail identically every time. This is a different layer from
# the circuit breaker below: this handles "briefly flaky," the breaker
# handles "persistently broken."
_RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}


def _urlopen_with_resilience(req, timeout, max_attempts=3, base_delay=0.5):
    last_exc = None
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 -- _urlopen_with_resilience callers use fixed SELF_BASE_URL/OpenRouter or SSRF-gated URLs
                return resp.read()
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code not in _RETRYABLE_HTTP_CODES or attempt == max_attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            last_exc = None
            if attempt == max_attempts - 1:
                raise
        time.sleep(base_delay * (2 ** attempt))
    raise last_exc


# Circuit-breaker research: a "one-strike"
# style breaker, but for repeated PERSISTENT failures (not transient ones
# already handled by retry above) -- if a specific model slug fails
# `CIRCUIT_BREAKER_THRESHOLD` times in a row, stop spending calls on it
# for a cooldown window instead of hammering something known-broken on
# every subsequent request. This is exactly what would have limited the
# damage from the three real dead/broken-model bugs if they'd
# occurred during live play instead of during the pre-flight verification
# that was built specifically to catch them before caching a pick.
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN_S = 300
# Half-open probe. The old breaker was OPEN-or-CLOSED: after the cooldown
# expired, the very next call went straight through, and if it happened to
# fail (a transient blip right at recovery) it re-tripped immediately. That
# made recovery brittle. Now a single PROBE is allowed through once the
# cooldown elapses; only if it SUCCEEDS does the circuit close. The probe's
# state is per-model, and while it is in flight no second caller rides through.
_model_circuit_state: dict[str, dict[str, object]] = {}  # model_slug -> {'consecutive_failures': int, 'open_until': float, 'probing': bool}
# The probe slot check-then-set above is a race without a lock, and Jev
# decisions fire from several threads at once (content executors, governance
# loops) -- a raw race here would let two callers ride a HALF_OPEN probe at the
# same moment. Serialize the breaker's state machine (cheap; only touches this
# dict, never the network).
_model_circuit_lock = threading.Lock()


def is_model_circuit_broken(model_slug):
    """True if the model must be skipped (call should raise). Once the cooldown
    elapses the circuit moves to OPEN -> HALF_OPEN and EXACTLY ONE call is
    allowed through as a recovery probe; a second call while that probe is still
    unresolved is still rejected."""
    with _model_circuit_lock:
        state = _model_circuit_state.get(model_slug)
        if not state or state.get('open_until', 0) == 0:
            return False  # CLOSED
        if time.time() >= state['open_until']:
            # Cooldown elapsed -> HALF_OPEN. If we are not already probing, open the
            # single probe slot; whether the probe is allowed depends on it being free.
            if not state.get('probing'):
                state['probing'] = True
                return False  # allow the probe through
            return True  # probe already in flight -- no second ride-along
        return True  # still OPEN, cooldown not spent


def record_model_result(model_slug, success):
    """Feed a call outcome back. In HALF_OPEN a probe SUCCESS closes the circuit
    (memory refreshed), a probe FAILURE re-opens it with a fresh cooldown."""
    with _model_circuit_lock:
        state = _model_circuit_state.setdefault(model_slug, {'consecutive_failures': 0, 'open_until': 0, 'probing': False})
        if success:
            if state.get('probing'):
                # Probe succeeded -> CLOSED, exactly once.
                state['consecutive_failures'] = 0
                state['open_until'] = 0
                state['probing'] = False
                log_action(None, 'model_circuit_recovered', {'model': model_slug})
            else:
                # CLOSED path: a success clears the failure streak.
                state['consecutive_failures'] = 0
                state['probing'] = False
            return
        state['consecutive_failures'] += 1
        state['probing'] = False
        if state['consecutive_failures'] >= CIRCUIT_BREAKER_THRESHOLD:
            # Re-open with a fresh cooldown; pin the streak at the threshold so it
            # can't grow unboundedly across repeated probe failures (the circuit is
            # already OPEN; only "< threshold" vs ">= threshold" matters).
            state['consecutive_failures'] = CIRCUIT_BREAKER_THRESHOLD
            state['open_until'] = time.time() + CIRCUIT_BREAKER_COOLDOWN_S
            log_action(None, 'model_circuit_broken', {'model': model_slug, 'cooldown_s': CIRCUIT_BREAKER_COOLDOWN_S})


# API-key auth-health tracker: per-service consecutive auth failures, with
# a hard-stop circuit breaker. A 401/403 from an external API usually means
# the key is dead (revoked/expired/wrong); some providers also 403 on
# temporary quota, so a single blip doesn't trip anything. Once the streak
# crosses API_KEY_AUTH_FAILURE_THRESHOLD the service is BLOCKED: callers
# fail fast instead of burning calls (and budget) on a key known-bad. A
# half-open probe after API_KEY_AUTH_COOLDOWN_S lets exactly one call
# through -- if it succeeds the service unblocks (transient blip); if it
# 401s again the block re-arms. The operator swapping the key and hitting
# /api/env/reload wipes the dict outright, unblocking everything.
API_KEY_AUTH_FAILURE_THRESHOLD = 2
API_KEY_AUTH_COOLDOWN_S = 600  # 10 min before a half-open probe is allowed
_api_key_health: dict[str, dict[str, object]] = {}  # service -> {'consecutive_failures', 'blocked_until', 'probing', 'last_failure_ts', 'last_failure_code', 'last_success_ts'}
_api_key_health_lock = threading.Lock()


def _api_key_is_broken(service):
    """True if the service must be skipped (call should fail fast). Mirrors
    is_model_circuit_broken: CLOSED normally; once the auth-failure streak
    crosses the threshold, OPEN until the cooldown elapses -- then HALF_OPEN
    and EXACTLY ONE probe is allowed through; only a success closes it."""
    with _api_key_health_lock:
        entry = _api_key_health.get(service)
        if not entry:
            return False
        blocked_until = entry.get('blocked_until') or 0
        if blocked_until == 0:
            return False  # CLOSED
        if time.time() >= blocked_until:
            if not entry.get('probing'):
                entry['probing'] = True
                return False  # allow the single probe through
            return True  # probe already in flight
        return True  # still OPEN


def _note_api_key_failure(service, status_code):
    """Record an auth failure (401/403) for an external API service. On the
    first failure past the threshold the service is blocked; a probe that
    fails re-arms the block with a fresh cooldown."""
    with _api_key_health_lock:
        entry = _api_key_health.setdefault(service, {})
        was_probing = bool(entry.get('probing'))
        entry['consecutive_failures'] = int(entry.get('consecutive_failures') or 0) + 1
        entry['last_failure_ts'] = time.time()
        entry['last_failure_code'] = int(status_code)
        entry['probing'] = False
        if entry['consecutive_failures'] >= API_KEY_AUTH_FAILURE_THRESHOLD:
            entry['blocked_until'] = time.time() + API_KEY_AUTH_COOLDOWN_S
    try:
        log_action(None, 'api_key_auth_failure',
                   {'service': service, 'status': int(status_code), 'probe_failed': was_probing})
    except Exception:
        pass


def _note_api_key_success(service):
    """Record a successful call for an external API service -- closes the
    circuit (unblocks) the moment the key works again."""
    with _api_key_health_lock:
        entry = _api_key_health.setdefault(service, {})
        entry['consecutive_failures'] = 0
        entry['blocked_until'] = 0
        entry['probing'] = False
        entry['last_success_ts'] = time.time()


def _api_key_auth_failures_snapshot():
    """Point-in-time view for the health check: services whose consecutive
    auth-failure streak has crossed the alert threshold (blocked or
    recently-blocked), with streak size and last status code."""
    now = time.time()
    with _api_key_health_lock:
        out = {}
        for service, entry in _api_key_health.items():
            streak = int(entry.get('consecutive_failures') or 0)
            if streak >= API_KEY_AUTH_FAILURE_THRESHOLD:
                blocked_until = entry.get('blocked_until') or 0
                out[service] = {
                    'status_code': entry.get('last_failure_code'),
                    'consecutive_failures': streak,
                    'seconds_ago': now - (entry.get('last_failure_ts') or 0),
                    'blocked': blocked_until > now,
                    'blocked_for_s': max(0, blocked_until - now),
                }
    return out


# PLAIN_WRITING_BAN_LIST is loaded from world/plain_writing.json by
# world/config.py (operator-edited; add a new AI-slop word there, not in code).
# Directs the village's models to write plain, short, no-AI-flavored prose --
# the SAME anti-slop signal the coupon scanner uses (word ban list), applied at
# the model-call boundary so every prose-producing path inherits it. This is a
# TOKEN-saving directive, not a style law: it cuts filler that costs input and
# output tokens, without demanding clipped caveman-speak.
PLAIN_WRITING_DIRECTIVE = (
    "Write plainly, the way a competent person talks to a colleague. "
    "Lead with the point. Use active voice and concrete facts. Make every "
    "sentence do work. Cut filler, hedging, throat-clearing, preamble, "
    "binary contrasts ('not X, it is Y'), faux-insight setups, colon reveals, "
    "importance puffery, weasel attribution, fake-profound endings, and any "
    "closing summary or recap. Prefer short sentences. Do not use the words "
    "or phrases: {banned}. Never use em dashes or en dashes; use a period, "
    "comma, or hyphen. Keep the reply exactly as long as it needs to be and "
    "not a word longer."
)


def _apply_plain_writing(messages):
    """Return `messages` with the plain-writing directive folded into the first
    system message (appended, so the caller's persona/instructions keep
    precedence). Returns the same list when there is no system message to
    attach to. Callers that need strictly structured output (JSON extraction)
    should skip this -- the directive's "not a word longer" nudge can truncate
    a schema mid-object."""
    messages = list(messages or [])
    directive = PLAIN_WRITING_DIRECTIVE.format(banned=', '.join(PLAIN_WRITING_BAN_LIST))
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get('role') == 'system':
            content = msg.get('content') or ''
            if 'Write plainly' in content:
                return messages
            messages[i] = dict(msg)
            messages[i]['content'] = f"{content}\n\n{directive}".strip()
            return messages
    return messages


OPENROUTER_CHAT_COMPLETIONS_URL = 'https://openrouter.ai/api/v1/chat/completions'


def _model_provider_for(model):
    """Resolve a synthetic @provider:<band> slug to its operator override.
    Returns None for ordinary OpenRouter slugs. `is_custom` is true only when
    a base_url is configured, so a model-only override still rides OpenRouter
    auth, credits, and failover."""
    cfg = MODEL_PROVIDER_OVERRIDES or {}
    if not isinstance(model, str) or not model.startswith('@provider:'):
        return None
    band = model.split(':', 1)[1]
    spec = (cfg.get('tiers') or {}).get(band) or cfg.get('default')
    if not spec:
        return None
    actual = spec.get('model') or _raw_model_tier_slug(band)
    if not actual:
        return None
    return {**spec, 'band': band, 'model': actual,
            'is_custom': bool(spec.get('base_url'))}


def _provider_is_billable(provider):
    """True when a custom endpoint declares a nonzero price. A zero price is
    treated as local/free and does not trip the global spend cap; a paid
    endpoint still does, even though OpenRouter credit gates are skipped."""
    if not provider or not provider.get('is_custom'):
        return False
    try:
        return float(provider.get('price_per_m') or 0) > 0
    except (TypeError, ValueError):
        return False


def _apply_provider_cost(result, provider):
    if not isinstance(result, dict) or not provider or not provider.get('is_custom'):
        return result
    usage = result.get('usage')
    if not isinstance(usage, dict):
        usage = {}
        result['usage'] = usage
    if usage.get('cost') is not None:
        return result
    tokens = usage.get('total_tokens')
    if tokens is None:
        tokens = (usage.get('prompt_tokens') or 0) + (usage.get('completion_tokens') or 0)
    try:
        tokens = float(tokens or 0)
        price = float(provider.get('price_per_m') or 0)
    except (TypeError, ValueError):
        return result
    if tokens and price:
        usage['cost'] = tokens * price / 1_000_000
    else:
        usage.setdefault('cost', 0.0)
    return result


def _openrouter_chat_post(body):
    """Single POST of a chat-completions body, shared by the two call layers
    (_call_openrouter_once_sync and _post_openrouter_raw), with one recovery
    retry for MANDATORY-reasoning models: OpenRouter rejects an explicit
    reasoning={'enabled': False} on them with a 400 ("Reasoning is mandatory
    for this endpoint and cannot be disabled" -- confirmed against
    z-ai/glm-5.3-flash), and with no reasoning field at all they spend the
    entire token budget on hidden reasoning and return content: null. A
    CAPPED reasoning budget answers fine at any size (same model, verified
    at 64 and 16000 tokens), so on that exact error retry once with
    reasoning capped to half the request budget. Returns the parsed JSON
    document; re-raises every other failure unchanged."""
    provider = _model_provider_for((body or {}).get('model'))
    is_custom = bool(provider and provider.get('is_custom'))
    if provider:
        body = dict(body)
        body['model'] = provider['model']
        if not provider.get('send_reasoning'):
            body.pop('reasoning', None)
    body = constitution.with_rules(body)
    source = provider.get('provider') if provider and is_custom else 'openrouter'
    base_url = provider.get('base_url') if is_custom else OPENROUTER_CHAT_COMPLETIONS_URL
    api_key = None
    if is_custom:
        key_env = provider.get('api_key_env') or ''
        api_key = (_load_env().get(key_env) if key_env else None)
    else:
        api_key = OPENROUTER_API_KEY

    def _post_once(payload):
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        req = urllib.request.Request(
            base_url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method='POST',
        )
        return json.loads(_urlopen_with_resilience(req, timeout=30))

    def _post_once_tracked(payload):
        # Training capture (opt-in): one row per POST, so the
        # recovery retry below gets its own record too. Recording
        # is fail-open -- record() swallows its own errors.
        start = time.monotonic()
        model_slug = payload.get('model')
        try:
            result = _post_once(payload)
        except Exception as error:
            training_capture.record(source, payload, error=error,
                                    duration_ms=int((time.monotonic() - start) * 1000))
            raise
        duration_ms = int((time.monotonic() - start) * 1000)
        _apply_provider_cost(result, provider)
        training_capture.record(source, payload, result,
                                duration_ms=duration_ms)
        # BP-63: the same wall-clock measurement feeds the tier pick's
        # speed tie-break. Only successful POSTs count -- a failed call's
        # duration is queue noise, not model speed.
        if isinstance(result, dict):
            _record_model_latency(model_slug, duration_ms)
        return result

    try:
        return _post_once_tracked(body)
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'ignore').lower()
        except Exception:
            pass
        if e.code == 402 and not is_custom:
            _set_credits_pause(True, source='openrouter_402')
        if e.code != 400 or 'reasoning is mandatory' not in detail:
            # Availability-class failure of a SELECTED tier model queues a
            # re-pick (see _queue_tier_recheck_if_availability); transient
            # errors are ignored there and owned by retry/circuit-breaker.
            if not is_custom:
                _queue_tier_recheck_if_availability(body.get('model'), e.code, detail)
            if any(marker in detail for marker in _AVAILABILITY_ERROR_MARKERS):
                e.availability_failure = True
            raise
        body = dict(body)
        body['reasoning'] = {'max_tokens': max(1, (body.get('max_tokens') or 2048) // 2)}
        try:
            return _post_once_tracked(body)
        except urllib.error.HTTPError as e2:
            detail2 = ''
            try:
                detail2 = e2.read().decode('utf-8', 'ignore').lower()
            except Exception:
                pass
            if e2.code == 402 and not is_custom:
                _set_credits_pause(True, source='openrouter_402')
            if not is_custom:
                _queue_tier_recheck_if_availability(body.get('model'), e2.code, detail2)
            if any(marker in detail2 for marker in _AVAILABILITY_ERROR_MARKERS):
                e2.availability_failure = True
            raise


def _call_openrouter_once_sync(model, messages, max_tokens, _fallback_depth=0,
                               reasoning_override=None):
    provider = _model_provider_for(model)
    is_custom = bool(provider and provider.get('is_custom'))
    if not is_custom and credits_pause_active():
        raise OpenRouterCreditsPaused('OpenRouter credits are exhausted; work is paused until the balance is restored')
    if (not is_custom or _provider_is_billable(provider)) and _think_tank_spend_cap_exceeded():
        raise RuntimeError(f'Think Tank spend cap (${SPEND_CAP_USD}) reached -- no further model calls until it is raised in .env')
    if not is_custom and _api_key_is_broken('openrouter'):
        raise RuntimeError('OpenRouter API key is blocked after repeated auth failures -- swap the key in .env and reload env')
    if is_model_circuit_broken(model):
        raise RuntimeError(f'{model} is temporarily circuit-broken after repeated failures')
    # Keep reasoning for the tiers that benefit from it (coding, high, and mid --
    # the JEV gate now escalates mid for judgment calls, so it earns the same
    # reasoning), but CAP it to half the request budget so the visible answer
    # still has room. The system's max_tokens is deliberately tight, and
    # hidden reasoning can otherwise consume the whole budget and come back
    # empty (the bug traced in refresh_model_tiers). Every other tier never
    # reads reasoning output, so turning it off there is free -- and a
    # mandatory-reasoning model that rejects that override is recovered by
    # _openrouter_chat_post's capped retry.
    if reasoning_override is not None:
        reasoning = reasoning_override
    elif model in (_coding_tier_slug(), _high_tier_slug(), _mid_tier_slug(),
                   _reasoning_tier_slug(), _3d_tier_slug()):
        reasoning = {'max_tokens': max_tokens // 2}
    else:
        reasoning = {'enabled': False}
    body = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'reasoning': reasoning}
    try:
        result = _openrouter_chat_post(body)
        if _reasoning_starved(result):
            recovered = _retry_reasoning_starved(body, result)
            if recovered is not None:
                result = recovered
    except urllib.error.HTTPError as e:
        record_model_result(model, success=False)
        if e.code in (401, 403) and not is_custom:
            _note_api_key_failure('openrouter', e.code)
        if (getattr(e, 'availability_failure', False)
                and _fallback_depth < TIER_FALLBACK_MAX_DEPTH):
            fallback = _try_tier_fallbacks(
                model, _fallback_depth,
                lambda slug: _call_openrouter_once_sync(
                    slug, messages, max_tokens,
                    _fallback_depth=_fallback_depth + 1))
            if fallback is not None:
                return fallback
        raise
    except Exception:
        record_model_result(model, success=False)
        raise
    record_model_result(model, success=True)
    if not is_custom:
        _note_api_key_success('openrouter')
    return result


def _reasoning_starved(data):
    """True when a completion spent its ENTIRE max_tokens on hidden reasoning
    and came back with content:null/empty + finish_reason=length -- the shape
    that surfaced live as the ask lane's "couldn't settle" replies (Ada's and
    Ben's truncated feedback, clara/theo's null content on
    anthropic/claude-haiku-5.5 at max_tokens=300 with reasoning capped to 150).
    A starved reply is useless to EVERY caller, so both call layers retry it
    once with reasoning disabled."""
    try:
        choice = (data.get('choices') or [{}])[0]
        msg = choice.get('message') or {}
        return (not (msg.get('content') or '').strip()
                and not (msg.get('tool_calls') or [])
                and (choice.get('finish_reason') or '') == 'length')
    except Exception:
        return False


def _retry_reasoning_starved(body, first):
    """One recovery attempt for a reasoning-starved response: re-POST the same
    body with reasoning disabled so the full max_tokens budget goes to the
    visible answer. A mandatory-reasoning model rejects `enabled: False` with
    a 400 that _openrouter_chat_post itself recovers into the SAME capped
    config that just starved -- so if that retry comes back starved too, one
    last attempt uses a verified-working tiny reasoning cap (64 tokens; the
    cap size documented in _openrouter_chat_post's docstring). Returns the
    improved document, or None to keep the original. On success, every cost
    already burned (the starved original + any failed retry) is merged into
    the winner's usage.cost so callers' spend accrual stays exact."""
    wasted = ((first.get('usage') or {}).get('cost') or 0.0)
    for payload in ({'enabled': False}, {'max_tokens': 64}):
        try:
            retry = _openrouter_chat_post(dict(body, reasoning=payload))
        except Exception:
            continue
        retry_cost = ((retry.get('usage') or {}).get('cost') or 0.0)
        msg = ((retry.get('choices') or [{}])[0].get('message') or {})
        if (msg.get('content') or '').strip():
            usage = retry.setdefault('usage', {})
            usage['cost'] = (usage.get('cost') or 0.0) + wasted
            return retry
        wasted += retry_cost
    return None


def _call_openrouter_sync_reasoning_off(model, messages, max_tokens):
    """One-shot recovery used when a reasoning-tier model burns the whole
    token budget on hidden thinking and returns content:null."""
    return _call_openrouter_once_sync(
        model, messages, max_tokens, reasoning_override={'enabled': False})

def _call_openrouter_sync(model, messages, max_tokens, best_of=1):
    """The plain OpenRouter chat-completions call. With `best_of=1` (the
    default) it returns a single parsed result dict exactly as before.

    `best_of>1` is the test-time-compute sampling primitive: it returns a
    LIST of `best_of` independent completions (self-consistency samples) that
    the caller -- /api/chat's deliberation path -- folds into a final answer
    via JSON majority-voting or a self-verification pass. Each sample runs
    through the same circuit breaker, retry, and model-result bookkeeping as
    a lone call."""
    if best_of is None or best_of <= 1:
        return _call_openrouter_once_sync(model, messages, max_tokens)
    samples = []
    for _ in range(int(best_of)):
        samples.append(_call_openrouter_once_sync(model, messages, max_tokens))
    return samples


def _post_openrouter_raw(model, messages, tools=None, max_tokens=None, tool_choice=None,
                         _fallback_depth=0):
    """Lowest-level OpenRouter chat-completions POST, shared by the plain
    `_call_openrouter_sync` and the /api/intent/ask tool loop. `tools`,
    `max_tokens`, and `tool_choice` are optional; bodies only include what
    the caller needs. `tool_choice='required'` forces the model to call a
    tool rather than answer directly -- used for exactly one turn by
    _call_agent_tool_loop's force_first_tool. Same circuit breaker +
    resilience + model-result bookkeeping as the plain call -- the tool loop
    is a real model consumer, so it earns the same protections. Returns the
    parsed OpenRouter JSON document."""
    provider = _model_provider_for(model)
    is_custom = bool(provider and provider.get('is_custom'))
    if not is_custom and credits_pause_active():
        raise OpenRouterCreditsPaused('OpenRouter credits are exhausted; work is paused until the balance is restored')
    if (not is_custom or _provider_is_billable(provider)) and _think_tank_spend_cap_exceeded():
        raise RuntimeError(f'Think Tank spend cap (${SPEND_CAP_USD}) reached -- no further model calls until it is raised in .env')
    if not is_custom and _api_key_is_broken('openrouter'):
        raise RuntimeError('OpenRouter API key is blocked after repeated auth failures -- swap the key in .env and reload env')
    if is_model_circuit_broken(model):
        raise RuntimeError(f'{model} is temporarily circuit-broken after repeated failures')
    if not _messages_pass_boundary_check(messages):
        raise RuntimeError('external data boundary failed verification -- refusing model call')
    body = {'model': model, 'messages': messages}
    if tools is not None:
        body['tools'] = tools
    if max_tokens is not None:
        body['max_tokens'] = max_tokens
    if tool_choice is not None:
        body['tool_choice'] = tool_choice
    # Same reasoning override as _call_openrouter_once_sync: the tool loop
    # never reads reasoning output, so it is disabled outright -- except for
    # the tiers that genuinely benefit from it, where it is capped to half
    # the (known) request budget so hidden reasoning can't swallow the whole
    # answer. Without this, a default_enabled-reasoning model called here
    # would spend the entire tight token budget thinking and return nothing
    # visible (the incident _call_openrouter_once_sync's override exists for).
    # A mandatory-reasoning model that rejects the disabled override is
    # recovered by _openrouter_chat_post's capped retry.
    reasoning = {'enabled': False}
    if model in (_coding_tier_slug(), _high_tier_slug(), _mid_tier_slug(),
                 _reasoning_tier_slug(), _3d_tier_slug()):
        if max_tokens is not None:
            reasoning = {'max_tokens': max_tokens // 2}
    body['reasoning'] = reasoning
    try:
        result = _openrouter_chat_post(body)
        if _reasoning_starved(result):
            recovered = _retry_reasoning_starved(body, result)
            if recovered is not None:
                result = recovered
    except urllib.error.HTTPError as e:
        record_model_result(model, success=False)
        if e.code in (401, 403) and not is_custom:
            _note_api_key_failure('openrouter', e.code)
        if (getattr(e, 'availability_failure', False)
                and _fallback_depth < TIER_FALLBACK_MAX_DEPTH):
            fallback = _try_tier_fallbacks(
                model, _fallback_depth,
                lambda slug: _post_openrouter_raw(
                    slug, messages, tools, max_tokens, tool_choice,
                    _fallback_depth=_fallback_depth + 1))
            if fallback is not None:
                return fallback
        raise
    except Exception:
        record_model_result(model, success=False)
        raise
    record_model_result(model, success=True)
    if not is_custom:
        _note_api_key_success('openrouter')
    return result
