

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
        except (urllib.error.URLError, TimeoutError) as _net_err:
            last_exc = None
            if attempt == max_attempts - 1:
                # A bare socket TimeoutError str()s to '' -- the live
                # 2026-11-02 outage surfaced as {"error": ""} from /api/chat,
                # which hid the actual failure from every consumer. Only an
                # EMPTY message gets the readable wrap; an error that already
                # says something ('net down') propagates unchanged.
                if str(_net_err).strip():
                    raise
                raise TimeoutError(
                    f'request timed out after {max_attempts} attempts '
                    f'(timeout={timeout}s): {getattr(req, "full_url", "?")}'
                ) from _net_err
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
# Free-pool endpoints need a longer open window: their failures are mostly
# provider-side quota/demand (429/503) that outlast a 5-minute cooldown, and a
# fast re-probe just burns the probe slot and re-opens the circuit
# (consultant finding 2026-10-27: 4 free-pool breakers tripped within 90s and
# 13 fallback hops in 24h). 15 minutes matches typical free-tier recovery.
FREE_POOL_CIRCUIT_COOLDOWN_S = 900
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
            cooldown = (FREE_POOL_CIRCUIT_COOLDOWN_S
                        if isinstance(model_slug, str)
                        and model_slug.startswith('@freepool:')
                        else CIRCUIT_BREAKER_COOLDOWN_S)
            state['open_until'] = time.time() + cooldown
            log_action(None, 'model_circuit_broken', {'model': model_slug, 'cooldown_s': cooldown})


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


def _provider_key_scope(provider):
    """The key-health scope for a provider: service name plus its key env,
    e.g. 'gemini:GEMINI_API_KEY_3'. Two endpoints may share one service name
    while carrying DIFFERENT keys (gemini-free vs gemini-free-key3, live
    2026-11-03); keying health by the bare service let one key's quota 403s
    block the other key's endpoint for the whole cooldown -- exactly the
    'one service blowing its limits stops the other free models' coupling.
    Providers without a key env fall back to the bare service name."""
    if not isinstance(provider, dict):
        return 'unknown'
    service = str(provider.get('service') or 'unknown')
    key_env = str(provider.get('api_key_env') or '').strip()
    return f'{service}:{key_env}' if key_env else service


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
# Hosts whose chat endpoint understands OpenRouter's provider preference
# object: OpenRouter enforces data_collection=deny router-side (documented
# per-request, plus an account-wide privacy toggle); Straitly accepts the
# same OpenRouter-shaped field. Gemini/Groq/Numes endpoints are NOT on this
# list -- Numes pins data_collection=deny in its endpoint spec's extra, and
# the other hosts must not receive an unknown top-level provider field.
_MODEL_DATA_DENY_HOSTS = ('openrouter.ai', 'api.straitly.ai')

# --- dialing-provider registry (provider-agnostic transports) ---------------
# Every vendor a model call can be sent to. The DEFAULT transport is the
# plain OpenRouter-shaped chat-completions POST with OPENROUTER_API_KEY; any
# other provider is an OpenAI-compatible endpoint dialed through its
# base_url + key env. Ripping a provider out (even the default) is: remove
# it from these two tuples + PROVIDER_CATALOGS -- nothing downstream knows
# the name. Per-provider spend caps are env constants
# (<NAME>_SPEND_CAP_USD) enforced at the transport; the tier pick layer
# reads the same registry to re-select from a provider with spend left.
DEFAULT_DIALING_PROVIDER = 'openrouter'
DIALING_PROVIDERS = ('openrouter', 'straitly')
# `settlement` marks how a vendor prices its calls (2026-11-13):
#   * absent          -- prepaid key (the normal shape; spend caps only)
#   * 'mpp'           -- per-call machine payments: a 402 with a
#                        Payment-Required challenge is settled through the
#                        MPP treasury (see 105_mpp_payments.py: enable
#                        switch, per-call cap, escalation floor, treasury
#                        and per-vendor caps, full ledger). No entry here
#                        uses it yet -- the rail is dormant until the
#                        operator registers an MPP-priced vendor and funds
#                        the treasury.
_PROVIDER_DIALING = {
    'straitly': {
        'base_url': 'https://api.straitly.ai/v1/chat/completions',
        'api_key_env': 'STRAITLY_API_KEY',
    },
}


def _serving_provider(provider):
    """Which registry provider a resolved spec dials; the default transport
    when there is no spec."""
    if provider and provider.get('is_custom') and provider.get('provider'):
        return provider['provider']
    return DEFAULT_DIALING_PROVIDER


def _provider_dialing_spec(model_id, provider_name):
    """A transport spec for `model_id` explicitly dialed through a
    non-default provider (the tier-refresh verify path: the candidate is not
    in model_tiers yet, so slug-based resolution cannot see it). Price is
    the provider's own listing price, falling back to the canonical row."""
    entry = _PROVIDER_DIALING.get(provider_name)
    if not entry:
        return None
    price = 0.0
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT price_per_m FROM model_provider_catalog WHERE provider = ? AND model_id = ?',
                (provider_name, model_id)).fetchone()
            if not row:
                row = conn.execute(
                    'SELECT price_per_m FROM model_catalog WHERE model_id = ?',
                    (model_id,)).fetchone()
            price = float(row[0]) if row else 0.0
    except Exception:
        price = 0.0
    return {'provider': provider_name, 'band': None, 'model': model_id,
            'base_url': entry['base_url'], 'api_key_env': entry['api_key_env'],
            'price_per_m': price, 'send_reasoning': False, 'is_custom': True}


def _tier_provider_spec(model):
    """The dialing spec for a PLAIN slug from the tier tables: its band pick
    (model_tiers) or a fallback row (model_tier_fallbacks). Only a
    non-default provider produces a spec -- an 'openrouter'/NULL provider
    returns None and rides the default transport exactly as before."""
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT provider, price_per_m FROM model_tiers WHERE slug = ? AND provider IS NOT NULL',
                (model,)).fetchone()
            if not row:
                row = conn.execute(
                    'SELECT provider, price_per_m FROM model_tier_fallbacks '
                    'WHERE slug = ? AND provider IS NOT NULL', (model,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    name = row[0]
    entry = _PROVIDER_DIALING.get(name)
    if not entry:
        return None
    return {'provider': name, 'band': None, 'model': model,
            'base_url': entry['base_url'], 'api_key_env': entry['api_key_env'],
            'price_per_m': float(row[1] or 0), 'send_reasoning': False,
            'is_custom': True}


def _model_provider_for(model):
    """Resolve a synthetic @provider:<band> slug to its operator override, a
    synthetic @freepool:<band>:<endpoint> slug to its free-pool endpoint
    spec, or a plain slug whose tier row names a non-default DIALING
    provider to that provider's transport spec. Returns None for ordinary
    OpenRouter slugs. `is_custom` is true only when a base_url is in play,
    so a model-only override still rides the default auth and failover."""
    cfg = MODEL_PROVIDER_OVERRIDES or {}
    if isinstance(model, str) and model.startswith('@freepool:'):
        return _freepool_provider_for(model)
    if isinstance(model, str) and not model.startswith('@provider:'):
        return _tier_provider_spec(model)
    if not isinstance(model, str):
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


# --- Outbound secret masking (2026-11-13, operator direction) ------------
# Free-tier providers may log or train on request bodies (Numes declares a
# per-endpoint data policy; Google's free Gemini tier trains on content).
# Capability handles keep raw keys out of agent context, but one pasted
# value in one prompt would still reach the wire and sit in a provider's
# logs forever. Before any chat payload leaves the process, every long,
# secret-named env VALUE is replaced with a stable placeholder. Header auth
# is untouched -- masking covers the BODY only.
_MODEL_MASK_ENV_BITS = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'COOKIE',
                        'ACCOUNT', 'REFRESH', 'HASH', 'SALT', 'NUMBER')
_MODEL_MASK_MIN_LEN = 16
_MODEL_MASK_TTL_S = 60.0
_model_mask_cache = {'at': 0.0, 'pairs': ()}


def _model_secret_pairs():
    """(value, placeholder) pairs for every env var whose NAME looks
    secret-shaped and whose value is long enough to be a real credential.
    Longest first so a long value cannot be partially unmasked by a shorter
    prefix of itself. Numeric-looking values are skipped: they are config,
    not credentials."""
    now = time.time()
    if now - _model_mask_cache['at'] < _MODEL_MASK_TTL_S:
        return _model_mask_cache['pairs']
    pairs = []
    for name, value in (_load_env() or {}).items():
        if not any(bit in str(name).upper() for bit in _MODEL_MASK_ENV_BITS):
            continue
        value = str(value or '').strip()
        if len(value) < _MODEL_MASK_MIN_LEN:
            continue
        try:
            float(value)
            continue
        except ValueError:
            pass
        pairs.append((value, f'[REDACTED:{name}]'))
    pairs.sort(key=lambda pair: -len(pair[0]))
    _model_mask_cache.update(at=now, pairs=tuple(pairs))
    return _model_mask_cache['pairs']


def _mask_model_secrets(obj):
    """Recursively replace secret env values inside strings of an outbound
    JSON payload. Returns the same structure; non-string leaves untouched."""
    pairs = _model_secret_pairs()
    if not pairs:
        return obj
    if isinstance(obj, str):
        for value, placeholder in pairs:
            if value in obj:
                obj = obj.replace(value, placeholder)
        return obj
    if isinstance(obj, list):
        return [_mask_model_secrets(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _mask_model_secrets(val) for key, val in obj.items()}
    return obj


def _openrouter_chat_post(body, provider=None):
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
    document; re-raises every other failure unchanged.

    `provider` (optional) is a pre-resolved transport spec -- the verify
    path passes one because its candidate has no tier row yet; None
    resolves from the body's model exactly as before. The per-provider
    spend cap is enforced HERE, at the single POST every layer shares: a
    billable dial answers to its vendor's own monthly allowance, and a
    tripped cap raises so the caller's free-pool/tier fallbacks take over."""
    provider = provider if provider is not None else _model_provider_for((body or {}).get('model'))
    if (not provider or not provider.get('free')) \
            and (not provider or not provider.get('is_custom')
                 or _provider_is_billable(provider)):
        # The SHARED model-router pool is the binding constraint (operator,
        # 2026-11-13): OpenRouter and Straitly draw from ONE monthly budget,
        # no hard half per router. The per-provider cap is the disaster
        # brake behind it (one router alone cannot exceed the pool).
        if _model_pool_cap_exceeded():
            raise RuntimeError(
                f'model router pool (${MODEL_ROUTER_POOL_USD:.2f}/month) '
                'exhausted -- paid calls fall back to the free pool')
        _name = _serving_provider(provider)
        if _provider_cap_exceeded(_name):
            raise RuntimeError(
                f'{_name} spend cap (${_provider_spend_cap(_name):.2f}/month) '
                'reached -- tier picks move to a provider with spend left')
    is_custom = bool(provider and provider.get('is_custom'))
    if provider:
        body = dict(body)
        body['model'] = provider['model']
        if not provider.get('send_reasoning'):
            body.pop('reasoning', None)
        if provider.get('extra'):
            body.update(provider['extra'])
    body = constitution.with_rules(body)
    source = provider.get('provider') if provider and is_custom else 'openrouter'
    base_url = provider.get('base_url') if is_custom else OPENROUTER_CHAT_COMPLETIONS_URL
    if (urllib.parse.urlparse(base_url).hostname or '') in _MODEL_DATA_DENY_HOSTS:
        # Paid lanes carry the same data policy the free-pool endpoint
        # specs pin (operator, 2026-11-13): OpenRouter's documented
        # provider preference data_collection=deny refuses every
        # endpoint whose policy trains on request data (verified live
        # 2026-11-13 against z-ai/glm-5.3-flash), and Straitly accepts
        # the same OpenRouter-shaped field without error -- enforcement
        # on Straitly's side is unverified, so it is a best-effort hint
        # there. Merge, never clobber: an endpoint spec or caller that
        # already set provider prefs wins.
        prefs = body.get('provider')
        prefs = dict(prefs) if isinstance(prefs, dict) else {}
        prefs.setdefault('data_collection', 'deny')
        body['provider'] = prefs
    api_key = None
    if is_custom:
        key_env = provider.get('api_key_env') or ''
        api_key = (_load_env().get(key_env) if key_env else None)
    else:
        api_key = OPENROUTER_API_KEY

    def _post_once(payload):
        # A real User-Agent on every model POST: Groq's edge blocks the bare
        # Python-urllib signature with a Cloudflare 403 (error 1010) before
        # auth is even considered -- confirmed live against api.groq.com.
        payload = _mask_model_secrets(payload)
        headers = {'Content-Type': 'application/json', 'User-Agent': 'AIThinkTankAgent/1.0'}
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
        if provider and provider.get('is_custom') and not provider.get('free') \
                and provider.get('provider'):
            # Per-provider rollup (the dimension the provider caps read):
            # the same dollar also lands in the caller's service lane, this
            # names WHO served it. Fail-open like every accrual.
            _accrue_provider_spend(provider['provider'],
                                   (result.get('usage') or {}).get('cost') or 0.0)
        if provider and provider.get('free'):
            # Quota accounting for the free pool: RPM/TPM/RPD windows are fed
            # from the real POST, not the pick, so the picker's view stays
            # honest. Fail-open inside the helper.
            _note_free_pool_call(provider, result)
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
        if e.code == 429 and provider and provider.get('free'):
            # A quota rejection from a free endpoint: cool it down so the
            # picker routes the next calls around it (the in-flight call
            # still falls through to the paid pick in the call layers).
            _note_free_pool_rate_limited(provider, detail,
                                         (e.headers or {}).get('Retry-After'))
        if (provider and provider.get('openrouter_free')
                and e.code in (403, 404)):
            # OpenRouter refused this :free model outright (harness-gated or
            # gone): ban it and let the pick rotate to the next candidate.
            _freepool_note_rejected(provider)
        if (e.code == 402 and is_custom
                and (provider or {}).get('settlement') == 'mpp'
                and _mpp_settle_and_retry(e, provider, body.get('model'))):
            # The vendor prices this call through a machine-payment challenge
            # (MPP, operator decision 2026-11-13) and the payment just
            # SETTLED through the treasury: exactly one retry of the same
            # body. Every fence (enable switch, per-call cap, escalation
            # floor, treasury + vendor caps) is enforced inside
            # _mpp_settle_and_retry -- a refusal raises and the call fails
            # like any other error, feeding the normal fallbacks.
            return _post_once_tracked(body)
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
            if e2.code == 429 and provider and provider.get('free'):
                _note_free_pool_rate_limited(provider, detail2,
                                             (e2.headers or {}).get('Retry-After'))
            if (provider and provider.get('openrouter_free')
                    and e2.code in (403, 404)):
                _freepool_note_rejected(provider)
            if e2.code == 402 and not is_custom:
                _set_credits_pause(True, source='openrouter_402')
            if not is_custom:
                _queue_tier_recheck_if_availability(body.get('model'), e2.code, detail2)
            if any(marker in detail2 for marker in _AVAILABILITY_ERROR_MARKERS):
                e2.availability_failure = True
            raise


def _is_low_paid_leg(model):
    """Whether `model` IS the low band's paid leg: the DB tier pick for
    'low', or the operator's @provider:low pin of it. Freepool slugs and
    every other band are not. Only consulted while the paid switch is on, so
    the DB read costs nothing in normal operation."""
    if not isinstance(model, str) or model.startswith('@freepool:'):
        return False
    if model == '@provider:low':
        return True
    try:
        return model == _raw_model_tier_slug('low')
    except Exception:
        return False


def _free_pool_fallback_slugs(model, _fallback_depth, _tried=None):
    """Where a failed call should be retried when the free pool is in play:
    a freepool slug falls through to the band's OTHER healthy free endpoints
    first (a 503 on one provider says nothing about the next one, and every
    failed hop is marked so the cascade cannot loop), and only then to the
    band's PAID pick -- free quota is best-effort; the agent must still get
    an answer. A failed low-band paid pick falls through to a healthy
    freepool route (the paid-first assist -- and in free-first mode this is
    how a paid call that also failed gets one more shot at a different free
    endpoint). Ordered, deduped, depth-bounded like the tier fallbacks.
    Empty outside those two cases."""
    out = []
    if _fallback_depth >= TIER_FALLBACK_MAX_DEPTH:
        return out
    tried = set(_tried or ())
    tried.add(model)
    if isinstance(model, str) and model.startswith('@freepool:'):
        parts = model.split(':', 2)
        failed_name = parts[2] if len(parts) == 3 else None
        band = parts[1] if len(parts) == 3 else None
        cfg = _free_pool_cfg() or {}
        for ep in cfg.get('endpoints') or []:
            if ep['name'] == failed_name or band not in (cfg.get('bands') or []):
                continue
            slug = f'@freepool:{band}:{ep["name"]}'
            if slug in tried:
                continue
            if _freepool_ready(ep, band) and slug not in out:
                out.append(slug)
        paid = _freepool_paid_fallback(model)
        if paid and paid not in tried and not _low_paid_blocked():
            out.append(paid)
        return out
    try:
        paid_low = _raw_model_tier_slug('low')
    except Exception:
        paid_low = None
    if model and paid_low and model == paid_low:
        free = _freepool_route_for_band('low', force=True)
        if free and free not in tried:
            out.append(free)
    return out


def _degrade_fit_free_pool(messages):
    """Shrink a conversation so a free-pool endpoint's request-size limit can
    take it. The paid tiers accepted bodies the free proxies reject with
    HTTP 413 (live 2026-11-05: a spike's research context degraded to the
    pool and died instantly). Keep the system message and the LAST few turns
    (where the actual question lives), and head+tail-truncate any single
    message larger than _FREE_MSG_MAX_CHARS."""
    _FREE_MSG_MAX_CHARS = 12000
    try:
        msgs = [m for m in (messages or []) if isinstance(m, dict)]
    except Exception:
        return messages
    if not msgs:
        return messages
    body_chars = sum(len(str(m.get('content') or '')) for m in msgs)
    if body_chars <= 80000:
        return messages

    def _trim(m):
        c = m.get('content')
        if not isinstance(c, str) or len(c) <= _FREE_MSG_MAX_CHARS:
            return m
        head = c[:_FREE_MSG_MAX_CHARS // 2]
        tail = c[-(_FREE_MSG_MAX_CHARS // 2):]
        out = dict(m)
        out['content'] = head + '\n...[truncated to fit the free tier]\n...' + tail
        return out

    keep_head = msgs[:1] if str(msgs[0].get('role')) == 'system' else []
    rest = msgs[len(keep_head):]
    kept = rest[-4:]
    kept = [_trim(m) for m in kept]
    return keep_head + kept


def _call_openrouter_once_sync(model, messages, max_tokens, _fallback_depth=0,
                               reasoning_override=None, _tried=None,
                               provider_name=None):
    # A BARE tier string ('@freepool:low', two segments) is a band name, not
    # a postable model id (2026-11-02 live bug: POSTing it verbatim earns
    # OpenRouter's 400 'not a valid model', the fallback generator sees
    # len(parts) != 3 so band=None skips every candidate, and the consumed
    # HTTPError body surfaced as {"error": ""} with status 400). Expand to
    # the band's current slug exactly as _resolve_model_tier would, so every
    # caller that names a band -- /api/chat bodies, the hybrid pre-pass,
    # anything else -- gets the same pool-leg selection executors get.
    if isinstance(model, str) and model.startswith('@freepool:') \
            and model.count(':') == 1:
        band = model.split(':', 1)[1]
        resolved = _slug_for_band(band)
        if resolved and resolved != model:
            model = resolved
    provider = _model_provider_for(model)
    if provider is None and provider_name \
            and provider_name != DEFAULT_DIALING_PROVIDER:
        # Explicit non-default dialing (the tier-refresh verify path: the
        # candidate has no tier row yet, so slug-based resolution cannot
        # see its provider).
        provider = _provider_dialing_spec(model, provider_name)
    is_custom = bool(provider and provider.get('is_custom'))
    free_route = bool(provider and provider.get('free'))
    # Free routes are exempt from the credits pause and the monthly spend cap
    # by construction: price_per_m 0 spends nothing, and serving low-band work
    # for free while the account is out of credits is exactly the fallback the
    # free pool exists to provide. BELOW the free floor ($0.05, operator
    # directive 2026-11-04) even free routes stop: the decisions model serves
    # from the laya/colab standby and nothing else runs on OpenRouter.
    if not is_custom and not free_route and credits_pause_active():
        # Operator directive (2026-11-05): the tank runs on the free tier when
        # the paid account is dry. Degrade the call to a healthy free-pool
        # endpoint instead of refusing it -- the pool's models are weaker than
        # the paid tiers, but a weaker answer beats a parked card. Raise only
        # when even the free pool cannot serve (all endpoints down/breaked).
        degraded = _freepool_route_for_band('low', force=True)
        if degraded:
            model = degraded
            provider = _model_provider_for(model)
            free_route = True
            messages = _degrade_fit_free_pool(messages)
        else:
            raise OpenRouterCreditsPaused('OpenRouter credits are exhausted; work is paused until the balance is restored')
    if not is_custom and free_route and _credits_free_floor_reached():
        raise OpenRouterCreditsPaused(
            'OpenRouter balance is at the free floor ($0.05); free routes are '
            'stopped -- the laya/colab standby serves the decisions model')
    if (not is_custom or _provider_is_billable(provider)) and not free_route and _think_tank_spend_cap_exceeded():
        raise RuntimeError(f'Think Tank spend cap (${SPEND_CAP_USD}) reached -- no further model calls until it is raised in .env')
    if not is_custom and _api_key_is_broken('openrouter'):
        raise RuntimeError('OpenRouter API key is blocked after repeated auth failures -- swap the key in .env and reload env')
    paid_block = None if free_route else _low_paid_blocked()
    if paid_block and _is_low_paid_leg(model):
        # The paid switch is a test/emergency lever scoped to the LOW band's
        # paid leg only: with it on, a low call either succeeds through the
        # free pool or fails visibly here -- it never silently spends.
        raise RuntimeError(f'The paid low tier is switched off by the '
                           f'{paid_block} policy switch -- free pool could '
                           f'not serve this call')
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
        result = _openrouter_chat_post(body, provider=provider)
        if _reasoning_starved(result):
            if free_route:
                # A free 200 with null content is a FAILED free attempt, not a
                # success to hand back: reasoning-heavy free models burn the
                # whole budget thinking (confirmed live on :free catalog
                # models at the tank's max_tokens budgets). Treat it like any
                # other free failure -- rotate an OpenRouter-free pick that is
                # systematically starved, and let the fallback land on the
                # band's paid pick.
                if provider.get('openrouter_free'):
                    _freepool_note_rejected(provider)
                raise RuntimeError(f'{model} returned no content (reasoning starved)')
            recovered = _retry_reasoning_starved(body, result, provider=provider)
            if recovered is not None:
                result = recovered
    except urllib.error.HTTPError as e:
        record_model_result(model, success=False)
        if e.code in (401, 403):
            if free_route and is_custom:
                _note_api_key_failure(_provider_key_scope(provider), e.code)
            elif not is_custom and not free_route:
                _note_api_key_failure('openrouter', e.code)
        for slug in _free_pool_fallback_slugs(model, _fallback_depth, _tried):
            if is_model_circuit_broken(slug):
                continue
            try:
                result = _call_openrouter_once_sync(
                    slug, messages, max_tokens,
                    _fallback_depth=_fallback_depth + 1,
                    _tried=(_tried or ()) + (model,))
            except Exception:
                continue
            print(f'[free-pool] {model} unavailable; served the call with '
                  f'{slug}', flush=True)
            log_action(None, 'free_pool_fallback',
                       {'from': model, 'to': slug, 'depth': _fallback_depth},
                       authorized=False)
            return result
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
        for slug in _free_pool_fallback_slugs(model, _fallback_depth, _tried):
            if is_model_circuit_broken(slug):
                continue
            try:
                result = _call_openrouter_once_sync(
                    slug, messages, max_tokens,
                    _fallback_depth=_fallback_depth + 1,
                    _tried=(_tried or ()) + (model,))
            except Exception:
                continue
            print(f'[free-pool] {model} unavailable; served the call with '
                  f'{slug}', flush=True)
            log_action(None, 'free_pool_fallback',
                       {'from': model, 'to': slug, 'depth': _fallback_depth},
                       authorized=False)
            return result
        # Transport-class failures (remote end closed connection, connection
        # reset, socket timeout) land in this generic branch, not the
        # HTTPError one, so they never reached the band's tier fallbacks --
        # the coding band's provider flaked chronically on 2026-11-03 and
        # every story burned its retry budget on raw re-attempts of the same
        # dead connection. Try the band's configured alternates here too.
        if _fallback_depth < TIER_FALLBACK_MAX_DEPTH:
            fallback = _try_tier_fallbacks(
                model, _fallback_depth,
                lambda slug: _call_openrouter_once_sync(
                    slug, messages, max_tokens,
                    _fallback_depth=_fallback_depth + 1))
            if fallback is not None:
                return fallback
        raise
    record_model_result(model, success=True)
    if not is_custom:
        _note_api_key_success('openrouter')
    elif free_route:
        _note_api_key_success(_provider_key_scope(provider))
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


def _retry_reasoning_starved(body, first, provider=None):
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
            retry = _openrouter_chat_post(dict(body, reasoning=payload),
                                          provider=provider)
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

def _call_openrouter_sync(model, messages, max_tokens, best_of=1,
                          provider_name=None):
    """The plain chat-completions call (default transport, or a non-default
    provider when `provider_name` names one -- the tier-refresh verify
    path). With `best_of=1` (the default) it returns a single parsed result
    dict exactly as before.

    `best_of>1` is the test-time-compute sampling primitive: it returns a
    LIST of `best_of` independent completions (self-consistency samples) that
    the caller -- /api/chat's deliberation path -- folds into a final answer
    via JSON majority-voting or a self-verification pass. Each sample runs
    through the same circuit breaker, retry, and model-result bookkeeping as
    a lone call."""
    if best_of is None or best_of <= 1:
        return _call_openrouter_once_sync(model, messages, max_tokens,
                                          provider_name=provider_name)
    samples = []
    for _ in range(int(best_of)):
        samples.append(_call_openrouter_once_sync(model, messages, max_tokens,
                                                  provider_name=provider_name))
    return samples


def _post_openrouter_raw(model, messages, tools=None, max_tokens=None, tool_choice=None,
                         _fallback_depth=0, _tried=None):
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
    free_route = bool(provider and provider.get('free'))
    if not is_custom and not free_route and credits_pause_active():
        # Same free-tier degrade as the plain call: the tank runs on the free
        # tier while the paid account is dry (operator directive 2026-11-05).
        degraded = _freepool_route_for_band('low', force=True)
        if degraded:
            model = degraded
            provider = _model_provider_for(model)
            free_route = True
            messages = _degrade_fit_free_pool(messages)
        else:
            raise OpenRouterCreditsPaused('OpenRouter credits are exhausted; work is paused until the balance is restored')
    if (not is_custom or _provider_is_billable(provider)) and not free_route and _think_tank_spend_cap_exceeded():
        raise RuntimeError(f'Think Tank spend cap (${SPEND_CAP_USD}) reached -- no further model calls until it is raised in .env')
    if not is_custom and _api_key_is_broken('openrouter'):
        raise RuntimeError('OpenRouter API key is blocked after repeated auth failures -- swap the key in .env and reload env')
    paid_block = None if free_route else _low_paid_blocked()
    if paid_block and _is_low_paid_leg(model):
        # The paid switch is a test/emergency lever scoped to the LOW band's
        # paid leg only: with it on, a low call either succeeds through the
        # free pool or fails visibly here -- it never silently spends.
        raise RuntimeError(f'The paid low tier is switched off by the '
                           f'{paid_block} policy switch -- free pool could '
                           f'not serve this call')
    if is_model_circuit_broken(model):
        if free_route and _fallback_depth < TIER_FALLBACK_MAX_DEPTH:
            # 2026-10-30 Tier-1: a breaker-open PRIMARY used to refuse the
            # whole call even though the free pool has healthy siblings --
            # the ask lane then failed outright ("temporarily circuit-
            # broken") while another endpoint could have served. Rotate the
            # pool exactly like the HTTPError fallback below; the breaker
            # still protects each individual endpoint.
            for slug in _free_pool_fallback_slugs(model, _fallback_depth, _tried):
                if is_model_circuit_broken(slug):
                    continue
                try:
                    result = _post_openrouter_raw(
                        slug, messages, tools, max_tokens, tool_choice,
                        _fallback_depth=_fallback_depth + 1,
                        _tried=(_tried or ()) + (model,))
                except Exception:
                    continue
                print(f'[free-pool] {model} breaker-open; served the call with '
                      f'{slug}', flush=True)
                log_action(None, 'free_pool_fallback',
                           {'from': model, 'to': slug, 'reason': 'circuit_broken',
                            'depth': _fallback_depth}, authorized=False)
                return result
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
        result = _openrouter_chat_post(body, provider=provider)
        if _reasoning_starved(result):
            if free_route:
                # Same rule as the plain call layer: a free 200 with null
                # content is a failed free attempt -- rotate a systematically
                # starved OpenRouter-free pick and land on the paid pick.
                if provider.get('openrouter_free'):
                    _freepool_note_rejected(provider)
                raise RuntimeError(f'{model} returned no content (reasoning starved)')
            recovered = _retry_reasoning_starved(body, result, provider=provider)
            if recovered is not None:
                result = recovered
    except urllib.error.HTTPError as e:
        record_model_result(model, success=False)
        if e.code in (401, 403):
            if free_route and is_custom:
                _note_api_key_failure(_provider_key_scope(provider), e.code)
            elif not is_custom and not free_route:
                _note_api_key_failure('openrouter', e.code)
        for slug in _free_pool_fallback_slugs(model, _fallback_depth, _tried):
            if is_model_circuit_broken(slug):
                continue
            try:
                result = _post_openrouter_raw(
                    slug, messages, tools, max_tokens, tool_choice,
                    _fallback_depth=_fallback_depth + 1,
                    _tried=(_tried or ()) + (model,))
            except Exception:
                continue
            print(f'[free-pool] {model} unavailable; served the call with '
                  f'{slug}', flush=True)
            log_action(None, 'free_pool_fallback',
                       {'from': model, 'to': slug, 'depth': _fallback_depth},
                       authorized=False)
            return result
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
        for slug in _free_pool_fallback_slugs(model, _fallback_depth, _tried):
            if is_model_circuit_broken(slug):
                continue
            try:
                result = _post_openrouter_raw(
                    slug, messages, tools, max_tokens, tool_choice,
                    _fallback_depth=_fallback_depth + 1,
                    _tried=(_tried or ()) + (model,))
            except Exception:
                continue
            print(f'[free-pool] {model} unavailable; served the call with '
                  f'{slug}', flush=True)
            log_action(None, 'free_pool_fallback',
                       {'from': model, 'to': slug, 'depth': _fallback_depth},
                       authorized=False)
            return result
        raise
    record_model_result(model, success=True)
    if not is_custom:
        _note_api_key_success('openrouter')
    elif free_route:
        _note_api_key_success(_provider_key_scope(provider))
    return result
