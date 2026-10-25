

def _colab_standby_reachable(port=None):
    """True when laya answers /health through the loopback forward -- the
    loop trusts this (not the boot exec) before declaring the standby up."""
    port = port or COLAB_STANDBY_PORT
    try:
        with urllib.request.urlopen(  # nosec B310 -- loopback-only by construction
                f'http://127.0.0.1:{port}/health', timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


async def _colab_failover_loop():
    """15-min poll. Jev degraded -> stand Laya up on the dedicated CPU standby
    session (provision via CLI if missing, boot laya-serve, keep the loopback
    ssh forward alive); the chain then reaches it at 127.0.0.1:8939 with no
    tunnel, no pairing, nothing public. Jev healthy -> kill the remote laya
    process once two clean cycles confirm recovery. All CLI calls run on
    threads so a slow boot (first-use pip install / model preload) never
    stalls the event loop that serves the think tank. Nothing happens at all
    unless COLAB_ENABLED and the colab CLI is present."""
    healthy_cycles = 0
    while True:
        try:
            session = COLAB_STANDBY_SESSION
            if not _colab_standby_enabled():
                await asyncio.sleep(COLAB_FAILOVER_INTERVAL_S)
                continue
            if _jev_is_degraded():
                healthy_cycles = 0
                ok, msg = await asyncio.to_thread(_colab_standby_ensure_session, session)
                if not ok:
                    print(f'[colab-standby] cannot provision {session}: {msg}', flush=True)
                else:
                    await asyncio.to_thread(_colab_standby_ensure_forward, session)
                    if await asyncio.to_thread(_colab_standby_reachable):
                        print('[colab-standby] Jev degraded; Laya up behind the loopback forward', flush=True)
                    else:
                        boot = await asyncio.to_thread(_colab_standby_ensure_service, session)
                        if not await asyncio.to_thread(_colab_standby_reachable):
                            print(f'[colab-standby] standing Laya up (not reachable yet): {boot}', flush=True)
            else:
                if await asyncio.to_thread(_colab_standby_reachable):
                    await asyncio.to_thread(_colab_standby_teardown_service, session)
                healthy_cycles += 1
                if healthy_cycles >= COLAB_STANDBY_HEALTHY_CYCLES:
                    print('[colab-standby] Jev healthy; Colab standby stood down', flush=True)
        except Exception as e:
            print(f'[colab-standby] loop error: {e}', flush=True)
        await asyncio.sleep(COLAB_FAILOVER_INTERVAL_S)


def _jev_health_window():
    """(attempts, failures) over the same rolling window the health alert
    uses, so the standby loop and the health page never disagree about
    whether Jev is degraded."""
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            'SELECT COUNT(*), SUM(ok = 0) FROM decision_tape WHERE ts > ?',
            (now - JEV_HEALTH_WINDOW_S,),
        ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def _jev_is_degraded():
    """True when the decisions model looks unavailable -- exactly the
    predicate underlying the 'Jev decision call(s) failed' health alert."""
    attempts, failures = _jev_health_window()
    return (attempts >= JEV_HEALTH_MIN_ATTEMPTS
            and failures >= attempts * JEV_HEALTH_FAILURE_RATE)


# ---- Colab agent compute -- run_on_colab tool ----------------
# The think tank can borrow a real Google Colab runtime for agent work this Mac
# cannot do (CUDA/torch GPU jobs, fine-tuning experiments, heavy numeric
# work). Same lever as the Laya standby -- the colab CLI running on this
# machine's account -- for a dedicated 'think-tank-gpu' session, provisioned on
# demand by a spike agent's run_on_colab call and stood down after a short
# idle grace.
#
# Budgeting is deliberately HONEST about what Colab actually enforces: Google
# publishes no fixed, guaranteed free quota -- limits are dynamic (sessions
# cap at ~12h, idle auto-disconnect lands around ~90 min, GPU availability and
# cooldowns shift with demand). The account does carry a REAL, live compute-
# unit balance, read by `colab usage` and reconciled here (_colab_account_usage)
# -- that is the genuine hard gate: a GPU job is refused when the account
# balance is exhausted. COLAB_MONTHLY_UNITS is ONLY an optional operator-set
# think tank convention cap on top (default 0 = off, mirroring SPEND_CAP_USD), not
# a number Google publishes; when set it also surfaces in the Bank. The idle
# teardown (15 min) keeps a parked GPU below Colab's ~90-min idle disconnect so
# the session never lingers to a limit Google would hit first. The tool only
# appears in the spike toolchain when the CLI is actually installed here -- the
# same "absent = the surface never advertises it" rule as search_web/GitHub.
COLAB_CLI_PATH = shutil.which('colab') or os.path.expanduser('~/.local/bin/colab')
COLAB_CLI_AVAILABLE = bool(COLAB_CLI_PATH) and os.path.exists(COLAB_CLI_PATH)
COLAB_GPU_SESSION = 'think-tank-gpu'
COLAB_GPU_ACCEL = 'T4'
# CPU is a first-class runtime option too, not a fallback: a large task that
# is purely CPU-bound (no CUDA/torch-GPU call) should not have to rent a T4
# at all -- free-tier CPU runtimes are more likely to be granted than a GPU
# slot, so sharding a big numeric/data job across CPU runtimes is often the
# more reliable path. Own session namespace so the two never collide.
COLAB_CPU_SESSION = 'think-tank-cpu'
COLAB_MONTHLY_UNITS = float(_load_env().get('COLAB_MONTHLY_UNITS', '0') or 0)
COLAB_FREE_TIER = str(_load_env().get('COLAB_FREE_TIER', '') or '').lower() in ('1', 'true', 'yes')
COLAB_ENABLED = str(_load_env().get('COLAB_ENABLED', '1') or '1').lower() not in ('0', 'false', 'no')
COLAB_LEDGER_KEY = '__colab_compute__'
_COLAB_USAGE_CACHE = {'at': 0.0, 'data': None}
_COLAB_USAGE_CACHE_TTL_S = 120
COLAB_MIN_UNITS_PER_RUN = 1.0       # every run pays a floor, even a 5s one
COLAB_UNITS_PER_MIN_GPU = 1.0       # T4 burn estimate per elapsed minute
COLAB_UNITS_PER_MIN_CPU = 0.05      # CPU runtime burn estimate per elapsed minute
COLAB_CODE_MAX_CHARS = 30000
COLAB_TIMEOUT_MAX_S = 900
COLAB_IDLE_GRACE_S = 15 * 60        # a parked GPU tears down after this idle
# A single run_on_colab call may shard one computation across up to this many
# named T4 runtimes (each gets its own provisioned session + shard env vars).
# Free tier grants whatever it grants -- fewer runtimes than requested is
# handled honestly (degrade to what provisioned, say so in the result), never
# by lying about an account wallet being empty.
COLAB_RUNTIMES_MAX = 5
# Sharding is Jev-gated like the model-tier gate (TIER_GATE_MIN_CONFIDENCE):
# the agent proposes a runtime count, but Jev picks the highest band the task's
# stated purpose earns, and an uncertain or unreachable classifier degrades to
# a SINGLE runtime -- one agent call must not 5x the spend on a computation
# that doesn't need it. Bands cap approved counts; 'shard' tops out at
# COLAB_RUNTIMES_MAX.
_COLAB_SHARD_BANDS = {
    'single': {'max': 1, 'description': "One runtime -- the whole computation on a single session. The cheap default; right for anything that does not genuinely need to split work."},
    'double': {'max': 2, 'description': "Two runtimes -- the computation is split across two sessions to roughly halve wall time."},
    'shard': {'max': COLAB_RUNTIMES_MAX, 'description': "Three to five runtimes -- a large, genuinely map-reduce-style computation split across many sessions."},
}
# A failed 'colab new' is not always permanent: free-tier availability, GPU
# cooldowns, and transient CLI/API errors recover quickly. Retry provisioning a
# BOUNDED number of times (these are the waits, in seconds, between each
# attempt -- 3 attempts total) so a transient refusal doesn't punt the agent
# into a whole expensive re-investigation. A genuine quota/capacity refusal
# still fails, honestly, after the retries are spent.
COLAB_PROVISION_RETRY_DELAYS_S = (5, 15)
_COLAB_COMPUTE_LAST_USED = 0.0
_COLAB_COMPUTE_LOCK = threading.Lock()




def _colab_cli(*args, timeout=120, input=None):
    """Run a colab CLI subcommand. Returns (exit_code, output_text). Never
    raises -- every caller surfaces the text like any other tool result.
    Inherits this process's environment (PATH for the SSH bridge etc.)."""
    cmd = [COLAB_CLI_PATH] + [str(a) for a in args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, input=input)
        return proc.returncode, ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
    except subprocess.TimeoutExpired:
        return -1, f'timeout after {timeout}s'
    except Exception as e:
        return -1, str(e)


def _colab_session_exists(session):
    """True when a colab CLI session with this name is currently provisioned.
    The CLI exits 0 even for a missing session -- it prints "Session 'X' not
    found." to stderr with a 0 exit code -- so the exit code alone is not
    enough: a stale/phantom registration would make provision skip `colab new`
    and every later exec fail against a session that does not exist. "not
    found" / "no active sessions" in the output therefore means not-existing."""
    rc, out = _colab_cli('status', '-s', session, timeout=90)
    if rc != 0:
        return False
    low = (out or '').lower()
    return 'not found' not in low and 'no active sessions' not in low


def _colab_account_usage():
    """Live reconcile against the REAL Colab account via `colab usage`:
    current compute-unit balance, burn rate, active assignments. Colab
    publishes no fixed quota (limits are dynamic -- session caps, idle auto-
    disconnect, GPU availability, cooldowns), but `colab usage` reports the
    real current balance the account is enforcing, so this is the genuine
    gate. Returns {'balance', 'rate', 'assignments'} or None on any failure
    (CLI missing, timeout, unparseable) so callers fail closed and fall back
    to the operator cap alone. Cached briefly like the Apify/OpenRouter
    reconciles: a bank readout or availability check shouldn't always shell
    out to the CLI."""
    if not COLAB_CLI_AVAILABLE:
        return None
    now = time.time()
    cached = _COLAB_USAGE_CACHE
    if cached['data'] is not None \
            and (now - cached['at']) < _COLAB_USAGE_CACHE_TTL_S:
        return cached['data']
    rc, out = _colab_cli('usage', timeout=30)
    if rc != 0:
        return None
    data = {'balance': None, 'rate': None, 'assignments': None}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('Current balance:'):
            m = re.search(r'([\d.]+)', line)
            if m:
                data['balance'] = float(m.group(1))
        elif line.startswith('Usage rate:'):
            m = re.search(r'([\d.]+)', line)
            if m:
                data['rate'] = float(m.group(1))
        elif line.startswith('Active assignments:'):
            m = re.search(r'(\d+)', line)
            if m:
                data['assignments'] = int(m.group(1))
    if data['balance'] is None:
        return None  # unparseable output -- fail closed
    cached['at'] = now
    cached['data'] = data
    return data


def _colab_compute_provision(session=None, kind='gpu'):
    """Idempotent: ensure a Colab runtime with the given name exists
    (defaults to the dedicated think-tank-gpu session; kind 'gpu' rents a T4,
    'cpu' rents a CPU runtime -- CPU slots are more likely granted on the free
    tier than a GPU, so CPU-bound work should not need to rent a T4). Returns
    (ok, msg). A failed 'colab new' (quota/capacity/cooldown) is retried a
    bounded number of times with backoff (COLAB_PROVISION_RETRY_DELAYS_S)
    before the honest per-run error is returned -- a transient refusal should
    not send the agent off on a whole re-investigation, and the retry count is
    bounded so a genuinely unavailable slot still gives up and says so."""
    session = session or COLAB_GPU_SESSION
    if not COLAB_CLI_AVAILABLE:
        return False, 'the colab CLI is not installed on this machine'
    if _colab_session_exists(session):
        return True, 'ok'
    out = ''
    for attempt, delay in enumerate((0,) + COLAB_PROVISION_RETRY_DELAYS_S):
        if attempt:
            time.sleep(delay)
        cmd = ['new', '-s', session]
        if kind == 'gpu':
            cmd += ['--gpu', COLAB_GPU_ACCEL]
        rc, out = _colab_cli(*cmd, timeout=300)
        if rc == 0:
            return True, 'ok'
    return False, f'provision failed: {out[-500:]}'


_COLAB_DENIED_EXPRESSIONS = {
    'drive access': re.compile(
        r'google\.colab\.drive|drive\.mount|from google\.colab import drive|'
        r'\bpydrive\b|\bdrive[a-z_]*\b.?mount|\bdrivemount\b|'
        r'\bdrive_root\b|mounted_drive|/content/drive|\bgdown\b|'
        r'\bfile\.id=|\bapplication/vnd\.google-apps|'
        r'colab drivemount', re.I),
    'google cloud / gcp credentials': re.compile(
        r'\bgsutil\b|\bgcloud\b|storage\.client|storage\.bucket|blob\.upload|'
        r'bigu?query|\bcolab auth\b|google\.oauth2|oauth2client|'
        r'google\.auth|google_auth_httplib2', re.I),
    'crypto mining / account-banned load': re.compile(
        r'\bnicehash\b|\bxmrig\b|\bstratum\b|\bcryptonight\b|'
        r'iminer|cryptocurrency|monero mining|ethash', re.I),
    'bulk media / torrents': re.compile(
        r'\bdeezloader\b|\bpeerflix\b|'
        r'\btorrent\b|\btransmission\b|rippedstreams', re.I),
    # yt-dlp / youtube_dl in their own class, separate from torrents: the
    # YouTube transcription path (_youtube_transcript_colab) no longer touches
    # yt-dlp at all -- the Apify actor downloads the audio and it is refused
    # here for agent-facing runs just like any other bulk-download tooling;
    # torrent/mass-download tooling stays refused unconditionally.
    'bulk media download': re.compile(
        r'\b(yt.?dlp|youtube_dl)\b', re.I),
    'data exfiltration hosts': re.compile(
        r'webhook\.site|requestbin|pastebin\.com|transfer\.sh|file\.io|'
        r'0x0\.st|catbox\.moe|discord(app)?\.com|api\.telegram\.org', re.I),
    # Offensive-security / red-team work is refused on the player's Colab
    # account ON PURPOSE: a scan or exploit attempt launched
    # from a Colab runtime looks like it originates from the player's own Google
    # infrastructure, and the think tank has its OWN local sandbox (the Work Room)
    # for that kind of testing. This class catches the recognizable tooling +
    # a scan-verb pattern; it is not a substitute for the local sandbox's own
    # policy, just the boundary for what runs under the player's account.
    'offensive security / red team': re.compile(
        r'\b(nmap|masscan|zenmap)\b|\b(metasploit|msfconsole|msfvenom)\b|'
        r'\bsqlmap\b|\bnuclei\b|\bnikto\b|\bgobuster\b|\b(wpscan|joomscan)\b|'
        r'\bhydra\b|\baircrack(-ng)?\b|\bhashcat\b|\bjohn\.?the\s?ripper\b|'
        r'\b(impacket|evil-winrm|bloodhound)\b|\bcrackmapexec\b|\b(responder|smbclient)\b|'
        r'\b(c2 |cobaltstrike|sliver|mythic)\b|'
        r'exploit|shellcode|reverse\s?shell|remote\s?code\s?execution|privilege\s?escalation|'
        r'directory\s?brute|subdomain\s?enum|port\s?scan|service\s?enumerat', re.I),
}


def _colab_denied(text, skip_labels=()):
    """Hard guard on what agents may send to the player's real Google account.
    Colab CLI runs in the player's identity, and the stored OAuth token carries
    drive.file + cloud-platform scopes -- so code running in a session COULD, in
    principle, touch Drive, GCS, or other account surfaces. The think tank has no
    legitimate reason to: this refuses any run touching those, plus the classic
    account-killers (mining, bulk media, torrents, exfil hosts). Returns a
    short label of the first matched class or None when clean.

    `skip_labels` exempts specific classes. Historically used ONLY by the
    sanctioned single-video YouTube transcription path (which previously
    touched yt-dlp). That path now downloads via the Apify actor on the
    runtime, so it needs no exemption -- agent-facing Colab runs
    (_COLAB_RUN_TOOL) never pass a skip label, and the parameter remains for
    any future sanctioned internal path that must clear a specific class."""
    if not text:
        return None
    for label, rx in _COLAB_DENIED_EXPRESSIONS.items():
        if label in skip_labels:
            continue
        if rx.search(text):
            return label
    return None


_COLAB_TARGET_RE = re.compile(r'https?://[^\s\'"<>)\]]+', re.I)


def _colab_target_hosts(code, purpose):
    """Best-effort literal-URL extraction from a Colab job's code + purpose,
    returning the set of distinct hostnames it statically references. This is
    deliberately the same class of guard as _colab_denied (regex on submitted
    text): it catches what the agent literally asks to touch, which is exactly
    the shape of a tool call -- an agent cannot seriously ask to "scan
    example.com" without writing the hostname. A dynamically-constructed host
    is opaque here just as it is to the deny regex, and the local
    sandbox/proxy boundary still owns the enforcement post-fetch."""
    hosts = set()
    for text in (code, purpose):
        if not text:
            continue
        for m in _COLAB_TARGET_RE.findall(text):
            try:
                host = urllib.parse.urlparse(m).hostname
            except ValueError:
                continue
            if host:
                hosts.add(host.lower())
    return hosts


def _colab_gate_urls(agent_id, code, purpose, authorized=None):
    """Apply the SAME internet-location policy a local /api/browse would, to
    every literal target URL in a Colab job -- this is what keeps a remote
    Colab run from being a second, unvetted path to the internet:
    allowlisted hosts pass (same skip-the-Jev round trip as browse), a
    private/internal/SSRF target is refused, and everything else goes through
    the same JEV allow/block classify + safety gate (low confidence escalates
    to a human). Returns None when every target is cleared, else a short
    refusal reason describing the first blocked target."""
    for host in sorted(_colab_target_hosts(code, purpose)):
        # Player-vetted domain -- skip the Jev round trip, exactly like browse.
        if _is_allowlisted_host(host):
            log_action(agent_id, 'colab_target', {'host': host, 'decision': 'allowed_by_allowlist'}, authorized=authorized)
            continue
        # Same SSRF/private-host boundary as /api/browse, never skipped.
        if not _is_safe_public_host(host):
            log_action(agent_id, 'colab_target', {'host': host, 'decision': 'blocked', 'reason': 'private/internal host'}, authorized=authorized)
            return f'{host} resolves to a private or internal network location and cannot be reached from a Colab run.'
        criteria = {
            'allow': 'The URL/domain and stated purpose look like ordinary, legal network use (reference material, news, public APIs, general research).',
            'block': 'The URL, domain, or stated purpose suggests a prohibited category: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '.',
        }
        instructions = f'A Colab compute job (running as-the-player) wants to contact this host: {host}\nStated purpose: {purpose or "not given"}\nDecide allow or block based on the domain and stated purpose.'
        decision, confidence, cost = _jev_quorum_choice_sync(instructions, criteria)
        if not _jev_safety_gate(agent_id, 'colab_target', 'This host', host, purpose,
                                decision, confidence, cost, bool(authorized)):
            return f'{host} was not approved for a Colab run to contact.'
        record_browse_success(host)
    return None


def _colab_shard_band(code, purpose, requested):
    """Jev decides the highest runtime-count band a sharded Colab computation
    earns -- mirrors _tier_gate_decider_default (same quorum choice, same
    fail-closed shape). Returns (band_id, confidence); (None, 0.0) on an
    unreachable or non-binary classifier so the caller fails closed to a
    single runtime. `requested` only grounds the prompt; the caller applies
    the band to the final count."""
    try:
        decision, confidence, _cost = _jev_quorum_choice_sync(
            'One computation wants to run on the player\'s FREE-tier Google Colab account. '
            f'The agent requested {requested} runtime(s) (max {COLAB_RUNTIMES_MAX}). '
            'Every granted runtime runs the SAME code and splits work via '
            'COLAB_SHARD_INDEX/COLAB_SHARD_COUNT; each one is a metered extra cost '
            '(compute units) and an extra fan-out of the think tank\'s decision gates.\n'
            f'Stated purpose: {purpose or "not given"}\n'
            f'Compute summary (first 500 chars): {(code or "")[:500]}\n'
            'Pick the SMALLEST band the computation genuinely earns -- single unless the '
            'work really needs parallel runtimes at that size.',
            {bid: b['description'] for bid, b in _COLAB_SHARD_BANDS.items()},
        )
        return decision, confidence
    except Exception:
        return None, 0.0


def _colab_compute_run(agent_id, code, purpose, packages, timeout_seconds, runtimes=1, kind='gpu', skip_deny_labels=(), env=None):
    """Run `code` (Python) on Colab runtime(s) and capture the output.
    Blocking -- the spike tool executor calls this on a worker thread like
    every other agent tool (see the ask lane's nested-call deadlock note).

    `kind` picks the runtime: 'gpu' rents T4 runtimes (for CUDA/torch/GPU
    work), 'cpu' rents CPU runtimes -- a large task that is purely CPU-bound
    should not have to rent a GPU, and CPU slots are more likely to be granted
    on the free tier, so CPU is the better default for big shardable numeric/
    data jobs. `runtimes` (1..COLAB_RUNTIMES_MAX) shards ONE computation
    across that many runtimes: each named session runs the SAME `code`, with
    COLAB_SHARD_INDEX (0-based) and COLAB_SHARD_COUNT env vars injected so the
    code can partition its own work (map-reduce style) and print its slice.
    This is the achievable form of "chain runtimes together" on the free tier:
    free tier does NOT meter usage (no prepaid wallet, unlimited), it just
    does not GUARANTEE a runtime -- so this provisions as many of the requested
    runtimes as the account actually grants, degrades to fewer (round-robin
    shards over whatever granted), and reports the honest grant count back.
    Extra runtimes are torn down after the run; only the primary stays parked
    for the idle loop to reap.

    Returns a dict {stdout, units, elapsed_s, session} on success (single
    runtime) or {stdout, units, elapsed_s, session, shards, runtimes} when
    sharded, or {error, ...} on any failure. Budget-gated: the only hard gate
    on the free tier is the operator-set COLAB_MONTHLY_UNITS convention cap --
    the account balance is NOT a gate there (see _colab_budget_exceeded)."""
    global _COLAB_COMPUTE_LAST_USED
    kind = 'gpu' if kind != 'cpu' else 'cpu'
    primary_session = COLAB_GPU_SESSION if kind == 'gpu' else COLAB_CPU_SESSION
    units_per_min = COLAB_UNITS_PER_MIN_GPU if kind == 'gpu' else COLAB_UNITS_PER_MIN_CPU
    if not code or not code.strip():
        return {'error': 'code is required'}
    if len(code) > COLAB_CODE_MAX_CHARS:
        return {'error': f'code must be under {COLAB_CODE_MAX_CHARS} characters'}
    if not COLAB_ENABLED:
        return {'error': 'Colab compute is disabled by the operator (COLAB_ENABLED=0); '
                         'do not try to run remote GPU jobs'}
    try:
        runtimes = max(1, min(int(runtimes or 1), COLAB_RUNTIMES_MAX))
    except (TypeError, ValueError):
        runtimes = 1
    requested_runtimes = runtimes
    # Sharding is deliberately gated on the decisions model being healthy.
    # Each shard's job still gets URL-gated through the Jev decision chain
    # (_colab_gate_urls -> _jev_quorum_choice_sync), so a sharded run fans
    # several concurrent decisions through the SAME decision path that is
    # already failing -- exactly the wrong time to multiply work that depends
    # on it. While Jev is degraded (fallback/standby active), sharding is
    # refused with an honest, retry-able reason; a single runtime stays open.
    if runtimes > 1 and _jev_is_degraded():
        return {'error': 'Jev\'s decisions model is currently degraded (fallback active) -- '
                         'sharding is disabled while it is down, because every shard\'s URL gate '
                         'depends on the same decision path that is already failing. '
                         'Re-run with runtimes=1, or retry sharding once Jev recovers.'}
    # Jev gates how many runtimes a shard request may actually use (band
    # decision, fail closed to 1). The agent still proposes the count; the
    # gate caps what it earns, so one agent call can't 5x the spend on a
    # computation that doesn't need it -- the same fail-to-cheap shape as
    # _resolve_model_tier. runtimes==1 skips the gate entirely.
    if runtimes > 1:
        band, confidence = _colab_shard_band(code, purpose, runtimes)
        band_max = _COLAB_SHARD_BANDS.get(band, {}).get('max', 1)
        if band_max > 1 and confidence < TIER_GATE_MIN_CONFIDENCE:
            band_max = 1  # don't spend up on weak signal -- fail closed to single
        runtimes = min(runtimes, band_max)
        log_action(agent_id, 'colab_shard_gate',
                   {'requested': requested_runtimes, 'band': band,
                    'confidence': round(confidence, 3), 'approved': runtimes},
                   authorized=False)
    blocked = _colab_denied(code, skip_deny_labels) or _colab_denied(purpose or '', skip_deny_labels)
    if blocked is None:
        for pkg in (packages or []):
            if isinstance(pkg, str) and _colab_denied(pkg, skip_deny_labels):
                blocked = f"package '{pkg}'"
                break
    if blocked:
        return {'error': 'refusing to run: it uses ' + blocked
                         + ', which is off-limits on the player\'s real Google '
                           'account (Drive/GCS/cloud creds, mining, bulk media, '
                           'torrents, exfil hosts, or offensive-security tooling). '
                           'Red-team and scan work runs in the local Work Room '
                           'sandbox instead, never on Colab.'}
    # Same internet-location policy as local browse: every literal URL the
    # job references must clear the player-vetted allowlist or a Jev
    # allow/block gate (low confidence escalates to a human). A Colab run
    # is NOT a second, unvetted path to the internet.
    target_reason = _colab_gate_urls(agent_id, code, purpose)
    if target_reason:
        return {'error': 'refusing to run: ' + target_reason}
    if _colab_budget_exceeded():
        if COLAB_MONTHLY_UNITS and _colab_spend_this_month() >= COLAB_MONTHLY_UNITS:
            # On the free tier this is the ONLY hard gate, and it is a think
            # tank convention cap (operator-set), NOT a Google limit -- say so
            # plainly so the agent never mistakes it for "the account is out
            # of money this month". T4 itself is unlimited; the operator can
            # just raise the cap.
            return {'error': 'the think tank has used its own monthly Colab unit cap '
                             '(COLAB_MONTHLY_UNITS, an operator-set convention -- NOT a '
                             'Google quota: free-tier T4 usage is unlimited, runtime is just '
                             'not guaranteed). The operator can raise the cap; until then '
                             'GPU jobs are paused.'}
        return {'error': 'the Colab account has no prepaid compute units left right now -- '
                         'GPU jobs cannot run until the account balance recovers '
                         '(this only applies on a paid account; free tier never gates on this)'}
    try:
        timeout = max(1, min(int(timeout_seconds or 300), COLAB_TIMEOUT_MAX_S))
    except (TypeError, ValueError):
        timeout = 300
    with _COLAB_COMPUTE_LOCK:
        start = time.time()
        # Provision the primary (must succeed) plus best-effort extras. The
        # account grants what it grants; anything less than requested is a
        # graceful degradation, reported back, not an error.
        sessions = []
        ok, msg = _colab_compute_provision(primary_session, kind=kind)
        if not ok:
            return {'error': f'could not provision a Colab {kind} session: {msg}'}
        sessions.append(primary_session)
        for i in range(1, runtimes):
            extra = f'{primary_session}-{i}'
            ok, _msg = _colab_compute_provision(extra, kind=kind)
            if ok:
                sessions.append(extra)
            else:
                break  # account isn't granting more right now -- degrade
        if packages:
            clean = [re.sub(r'[^A-Za-z0-9._=-]', '', str(p))
                     for p in packages if isinstance(p, str)]
            pkg_line = ' '.join(p for p in clean if p)
            if pkg_line:
                for session in sessions:
                    rc, out = _colab_cli(
                        'exec', '-s', session, '--timeout', '600',
                        '--env', f'COLAB_PKGS={pkg_line}',
                        timeout=660,
                        input=('import os, subprocess\n'
                               'subprocess.run("pip install -q " + os.environ["COLAB_PKGS"], '
                               'shell=True, timeout=540)\nprint("__COLAB_PKGS_INSTALLED__")\n'))
                    if rc != 0 or '__COLAB_PKGS_INSTALLED__' not in out:
                        return {'error': f'package install failed on Colab ({session}): {out[-800:]}'}
        shards = []
        total_units = 0.0
        # Optional caller-supplied env vars (e.g. APIFY_API_KEY for the
        # YouTube transcription run), emitted as --env KEY=VALUE flags exactly
        # like the injected shard vars below. Values are secrets only the
        # runtime needs; they never appear in the logged code/action rows.
        env_flags = []
        for env_key, env_val in (env or {}).items():
            env_flags += ['--env', f'{env_key}={env_val}']
        for shard_index in range(runtimes):
            session = sessions[shard_index % len(sessions)]
            shard_start = time.time()
            rc, out = _colab_cli(
                'exec', '-s', session, '--timeout', str(timeout + 30),
                '--env', f'COLAB_SHARD_INDEX={shard_index}',
                '--env', f'COLAB_SHARD_COUNT={runtimes}',
                *env_flags,
                # Host-side subprocess timeout must exceed the CLI's own
                # --timeout, or the host kills the exec (and the transcript
                # transcription with it) before the CLI can finish or report.
                timeout=timeout + 90,
                input=code + '\nprint("__COLAB_DONE__")\n')
            elapsed = max(1, int(time.time() - shard_start))
            _COLAB_COMPUTE_LAST_USED = time.time()
            units = max(COLAB_MIN_UNITS_PER_RUN,
                        round(elapsed / 60.0 * units_per_min, 3))
            total_units += units
            _accrue_colab_units(units)
            out = '\n'.join(l for l in out.splitlines() if '__COLAB_DONE__' not in l)
            shards.append({'index': shard_index, 'session': session, 'rc': rc,
                           'stdout': out[-6000:], 'units': units, 'elapsed_s': elapsed})
        log_action('agent', 'colab_compute_run', {
            'purpose': (purpose or '')[:120],
            'sessions': sessions,
            'requested_runtimes': requested_runtimes,
            'approved_runtimes': runtimes,
            'granted_runtimes': len(sessions),
            'kind': kind,
            'timeout': timeout,
            'units': round(total_units, 3),
            'shards': [{'index': s['index'], 'session': s['session'], 'rc': s['rc']} for s in shards],
            'code': code[:2000],
        }, authorized=True)
        # Reap the extra runtimes -- a parked T4 keeps burning units between
        # agent jobs, and only the primary has an idle guard. Free tier burns
        # unlimited but NOT nothing: every minute of GPU is still real usage.
        for extra in sessions[1:]:
            try:
                _colab_cli('stop', '-s', extra, timeout=120)
            except Exception:
                pass
        total_elapsed = max(1, int(time.time() - start))
        if runtimes == 1:
            shard = shards[0]
            if shard['rc'] != 0:
                return {'error': f"Colab run failed (exit {shard['rc']}): {shard['stdout'][-1500:]}",
                        'units': shard['units']}
            return {'stdout': shard['stdout'], 'units': shard['units'],
                    'elapsed_s': shard['elapsed_s'], 'session': shard['session']}
        # Sharded: combine per-runtime output, labeled, so the model can see
        # which runtime produced what and honest grant count vs request.
        parts = []
        for s in shards:
            tag = f'--- runtime {s["index"]} ({s["session"]}) ---'
            if s['rc'] != 0:
                parts.append(f'{tag}\nRUN FAILED (exit {s["rc"]})')
            else:
                parts.append(f'{tag}\n{s["stdout"]}')
        combined = '\n'.join(parts)
        return {'stdout': combined[-6000:], 'units': round(total_units, 3),
                'elapsed_s': total_elapsed, 'session': sessions[0],
                'shards': [{'index': s['index'], 'session': s['session'], 'rc': s['rc']}
                           for s in shards],
                'runtimes': len(sessions)}


async def _colab_compute_idle_loop():
    """Parked-runtime guard: every 5 min, stop the dedicated GPU and CPU
    sessions once they have sat idle past COLAB_IDLE_GRACE_S, so a standing
    runtime stops burning the account's compute units between agent jobs.
    Same lifecycle ownership as the Jev standby, but for the agent-compute
    sessions and driven by an idle timer rather than a health signal."""
    while True:
        try:
            if _COLAB_COMPUTE_LAST_USED > 0 \
                    and (time.time() - _COLAB_COMPUTE_LAST_USED) > COLAB_IDLE_GRACE_S:
                for session, label in ((COLAB_GPU_SESSION, 'GPU'), (COLAB_CPU_SESSION, 'CPU')):
                    if _colab_session_exists(session):
                        print(f'[colab-compute] {label} session idle; tearing down', flush=True)
                        _colab_cli('stop', '-s', session, timeout=120)
        except Exception as e:
            print(f'[colab-compute] idle teardown error: {e}', flush=True)
        await asyncio.sleep(5 * 60)


# JEV_SAFETY_CONFIDENCE and the ant-colony quorum samplers
# (_jev_quorum_decision / _jev_quorum_choice_sync) moved to jev.py (Phase 0c
# self-modification split) and are re-exported below -- every caller and
# every test patch on serve.X keeps its exact surface.


# Composite multi-signal trust gate for the DIRECTOR auto-approval path.
# The old check was single-signal: a lone Jev `confidence` above the floor let the
# delegated director auto-approve/deny ANY pending escalation. That gave a
# high-confidence Jev answer the same authority regardless of (a) how risky the
# escalation's *kind* was, or (b) how flaky the Jev decision model had been.
#
# We replace it with a composite score:
#   effective = confidence - error_penalty
# gated per-escalation-KIND by a risk floor. The two signals actually present at
# this chokepoint are used; signals the path has no source for (source-doc count,
# tool-consistency) are NOT invented. The category override is the sharp part: a
# safety-critical kind's floor can be RAISED above the global default (or set to
# 1.0 = never auto-approve), so a confident-but-wrong Jev answer still lands in
# the human's inbox for that class of escalation. Fail-closed invariant preserved:
# a classifier failure (exception / non-approve-deny) still leaves the escalation
# pending and bumps the error counter -- never auto-decides.
# The floor table and its three scalars moved to jev.py (Phase 0c) and are
# re-exported below -- the composite gate itself stays protected in serve,
# but the DATA it consumes is jev-owned and self-editable.


# Heterogeneous-judge cross-check (external codebase eval: self-evolve
# tools/sie/judges.py + selfdeception.py). The composite gate above still
# trusts ONE decisions model to stand in for the admin. When the think tank
# runs a SECOND, DIFFERENT decisions model, the director's auto-approval also
# asks that model the same escalation question, and the primary only wins if
# the judge independently agrees (or the judge is unavailable to ask).
# JEV_JUDGE_MODEL here is only the env fallback; the live value resolves
# through _jev_judge_model() (settings `jev_judge_model` wins), and it must
# differ from the primary or the gate is disabled -- a second judge that is
# literally the same model is a retry, not independent evidence.
ESCALATION_JUDGE_ALPHA_HIGH = 0.9   # both models >= this confidence while drifting = suspected collusion
ESCALATION_JUDGE_DRIFT_CIRCUIT = 4  # drift hits this -> kind becomes human-only
JEV_JUDGE_MODEL = (_load_env().get('JEV_JUDGE_MODEL') or '').strip() or None


class _thread_safe_counter:
    """Minimal thread-safe dict-of-counters for consecutive Jev decision errors
    per escalation kind (the error_history signal). A fresh process starts at
    zero; the penalty is cosmetic only until the counter is bumped by a real
    failure in this loop."""
    def __init__(self):
        self._vals = {}
        self._lock = threading.Lock()

    def bump(self, key):
        with self._lock:
            self._vals[key] = self._vals.get(key, 0) + 1

    def reset(self, key):
        with self._lock:
            self._vals.pop(key, None)

    def get(self, key):
        with self._lock:
            return self._vals.get(key, 0)


_escalation_jev_errors = _thread_safe_counter()


def _escalation_floor(kind):
    """Per-kind composite floor (1.0 = never auto-approve). A known kind's fixed
    risk floor wins (e.g. never auto-approve an explicit 'blocked' verdict); the
    routine kinds (the base default and 'unresolved review requirement') resolve
    through the LIVE calibration-adjusted threshold (_effective_safety_confidence)
    so the feedback loop can raise/lower the whole routine bar without a restart
    (see _calibration_adjust_pass)."""
    entry = ESCALATION_KIND_RISK.get(kind)
    if entry is None or entry.get('floor', JEV_SAFETY_CONFIDENCE) == JEV_SAFETY_CONFIDENCE:
        return _effective_safety_confidence()
    return entry['floor']


def _jev_directory_score(kind, confidence):
    """Composite trust score for the director auto-approval decision. `confidence`
    is Jev's calibrated LLM confidence (0-1); we subtract a penalty weighted by the
    escalation kind's recent Jev-error history so a model that has been failing on
    this kind needs to be more confident to win the human's delegation."""
    penalty = min(ESCALATION_ERROR_PENALTY_CAP,
                  ESCALATION_ERROR_PENALTY_STEP * _escalation_jev_errors.get(kind))
    return max(0.0, min(1.0, float(confidence) - penalty))


def _jev_judge_model():
    """The heterogeneous second-decisions model for the director escalation
    cross-check. Resolution: settings `jev_judge_model` (operator switch, same
    live-editable pattern as `jev_model`) > env JEV_JUDGE_MODEL > None. Returns
    None when none is configured or when it is not actually different from the
    primary -- a judge that is the primary model adds no independent evidence,
    so the gate is disabled rather than pretending to double-check."""
    saved = (_get_setting('jev_judge_model') or '').strip()
    judge = saved or JEV_JUDGE_MODEL
    if not judge:
        return None
    primary = _jev_model()
    if not primary or judge == primary:
        return None
    return judge


def _escalation_judge_crosscheck(instr, criteria):
    """Ask the heterogeneous judge the SAME escalation question the primary just
    answered -- a fresh, independent judgment that sees ONLY the original
    question and criteria, never the primary's decision or confidence (the
    self-deception rule: an agreeing answer is two genuinely independent reads,
    not the second model echoing the first). Returns (decision, confidence,
    cost) on a clean approve/deny, or (None, None, cost) when the gate is
    disabled, the judge call failed, or the judge's answer was non-binary.
    Never raises; cost is already accrued by _call_openrouter_decision_sync."""
    judge = _jev_judge_model()
    if not judge:
        return None, None, 0.0
    try:
        data = _call_openrouter_decision_sync(
            judge, {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instr, 'criteria': criteria}},
        )
    except Exception:
        return None, None, 0.0
    decision, confidence, cost = _jev_choice(data)
    if decision not in ('approve', 'deny'):
        return None, None, float(cost)
    return decision, float(confidence), float(cost)


def _escalation_judge_drift(kind):
    """Per-kind drift counter for the judge cross-check. Persisted in the
    settings JSON row `escalation_judge_drift` so a tripped circuit survives a
    process restart (the in-memory _escalation_jev_errors streak does not)."""
    try:
        raw = _get_setting('escalation_judge_drift') or '{}'
        return int((json.loads(raw) or {}).get(kind, 0))
    except Exception:
        return 0


def _bump_escalation_judge_drift(kind):
    """Record one judge-gate failure-closed event for this escalation kind."""
    try:
        raw = _get_setting('escalation_judge_drift') or '{}'
        try:
            data = json.loads(raw) or {}
        except Exception:
            data = {}
        data[kind] = int(data.get(kind, 0)) + 1
        _set_setting('escalation_judge_drift', json.dumps(data))
    except Exception:
        pass


def _reset_escalation_judge_drift(kind):
    """A human resolving an escalation of this kind clears its drift circuit --
    the director's delegation for that kind is actionable again."""
    try:
        raw = _get_setting('escalation_judge_drift') or '{}'
        try:
            data = json.loads(raw) or {}
        except Exception:
            data = {}
        if data.pop(kind, None) is not None:
            _set_setting('escalation_judge_drift', json.dumps(data))
    except Exception:
        pass


def _escalation_judge_drift_summary():
    """The full per-kind drift map, for the /api/jev/model read surface."""
    try:
        raw = _get_setting('escalation_judge_drift') or '{}'
        data = json.loads(raw) or {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}
    except Exception:
        return {}
