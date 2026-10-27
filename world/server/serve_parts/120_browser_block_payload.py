import core.resources as resources



def _browser_block_payload(parsed_result, agent_id, op, url, purpose):
    """Turn a driver-reported challenge into a stop-and-report response."""
    challenge = parsed_result.get('challenge') or {}
    try:
        host = urllib.parse.urlparse(
            parsed_result.get('url') or url).hostname or ''
    except ValueError:
        host = ''
    _record_browser_block(agent_id, host, op, parsed_result.get('url') or url,
                          challenge, parsed_result.get('status'), purpose)
    vendor = challenge.get('vendor') or 'unknown'
    parsed_result['ok'] = False
    parsed_result['error'] = (
        f'blocked by {vendor} challenge on {host or "the target site"}; '
        'stopped instead of retrying')
    parsed_result['blocked'] = {
        'vendor': vendor,
        'marker': challenge.get('marker') or '',
        'host': host,
        'status': parsed_result.get('status'),
        'kind': 'challenge',
        'logPath': BROWSE_BLOCK_LOG,
    }
    parsed_result['hint'] = (
        'This is a site-side block, not a bad selector. Do not retry the same '
        'URL. Ask the operator to take over the browser, or request API or '
        'allowlist access.')
    return parsed_result

# The quality pipeline's flake8 step legitimately takes ~32s on this repo
# (11MB, 1 CPU in the sandbox); 30s made every run a false failure. 120s
# leaves headroom for flake8/mypy/pytest without opening the door to a
# runaway command. The pipeline-level cap (_QUALITY_PIPELINE_TIMEOUT_S) is
# still 300s, so this per-step ceiling only changes which step is blamed.
SANDBOX_TIMEOUT_S = 120
SANDBOX_MAX_OUTPUT = 20_000
COMMAND_CONTEXT_MAX = 12_000


_OUTPUT_SIGNAL = re.compile(
    r'(?i)(error|exception|traceback|failed|failure|fatal|warning|warn|assert|'
    r'denied|timeout|no such file|not found|diff --git|^@@|^\+\+\+|^---|^\+|^-)')


def _compact_command_output(text, limit=COMMAND_CONTEXT_MAX):
    """Keep head, tail, error lines, and diff-like lines from a command output.

    Execute results were capped at 20k characters, which is still a large
    prompt block for a log that mostly repeats. A plain head/tail slice can
    drop the one traceback or diff hunk in the middle, so signal lines are
    reserved first, then the remaining budget fills head and tail.
    """
    text = text or ''
    if len(text) <= limit:
        return text
    if '\n' not in text:
        head = max(0, (limit * 2) // 3)
        tail = max(0, limit - head - 40)
        return (text[:head] + f'\n... [{len(text) - head - tail} chars omitted] ...\n'
                + text[-tail:] if tail else text[:head])
    lines = text.splitlines()
    signal = [(i, line) for i, line in enumerate(lines)
              if _OUTPUT_SIGNAL.search(line)]
    if len(signal) > 80:
        signal = signal[:40] + signal[-39:]
    selected = {i: line for i, line in signal}
    used = sum(len(line) + 1 for line in selected.values())
    head_n = max(1, len(lines) // 5)
    tail_n = max(1, len(lines) // 5)
    for i in list(range(head_n)) + list(range(max(0, len(lines) - tail_n), len(lines))):
        if i in selected:
            continue
        line = lines[i]
        if used + len(line) + 1 > limit - 60:
            continue
        selected[i] = line
        used += len(line) + 1
    out = '\n'.join(selected[i] for i in sorted(selected))
    if len(out) > limit:
        out = out[:limit - 40] + '\n... [signal output truncated] ...'
    omitted = max(0, len(lines) - len(selected))
    return out + (f'\n... [{omitted} lines omitted] ...' if omitted else '')

# Where agent commands actually run: `local` = the local Docker
# sandbox (the default and only tested backend); `digitalocean` = a provisioned
# DigitalOcean Droplet. This ALSO serves as the master gate for the DO
# credential -- see _digitalocean_enabled / the mint+resolve refusals below:
# while local, agents can never obtain or use a DO capability handle, so the
# vault-held DO token is unreachable until this is flipped to 'digitalocean'.
SANDBOX_EXECUTION = _load_env().get('SANDBOX_EXECUTION', 'local').strip().lower()


def _digitalocean_enabled():
    """True only when SANDBOX_EXECUTION=digitalocean -- the single switch that
    makes the DigitalOcean credential usable by the think tank. Every agent-facing
    entry point (handle mint, handle resolve) hard-refuses while this is False,
    so flipping the variable is the SOLE way DO becomes reachable."""
    return SANDBOX_EXECUTION == 'digitalocean'

# Categories a proposed command is checked against BEFORE it ever runs --
# same "classify the request, not the result" shape as browsing, and for
# the same reason: the sandbox (network-isolated, no host filesystem,
# resource-capped -- see _run_in_sandbox_sync) contains the blast radius
# of anything that slips through, but containment isn't the same thing as
# never running it in the first place.
EXECUTE_BLOCK_CATEGORIES = (
    'attempts to access, read, exfiltrate, or transmit credentials, API keys, tokens, or secrets',
    'attempts to escape the sandbox, access the host filesystem, or affect anything outside /workspace',
    'deliberately destructive, resource-exhausting, or denial-of-service behavior (fork bombs, infinite loops with no purpose, filling disk space)',
    # The sandbox's own network egress is allowlisted to package
    # registries only (sandbox_proxy.py) regardless of what Jev decides
    # here -- this is a second, independent reason to still block on
    # intent, not the only thing standing between a bad command and the
    # real internet.
    'deliberate attempts to reach destinations other than well-known package registries (pip, npm, apt, git), such as arbitrary URLs, IP addresses, or exfiltration endpoints',
)

# Fired whenever Jev blocks a command, because admins (and this
# feature) should be able to escalate something to you rather than just
# silently refusing forever -- see _send_escalation_email_sync /
# /api/escalation/resolve. Not a general "ask about everything" channel:
# only for a blocked-but-maybe-legitimate command, or a firing decision
# that resolved to "fire" (the most consequential, hardest-to-reverse
# admin call in the think tank) -- chosen because both are exactly the kind
# of thing that's rare, genuinely blocking, and where a wrong autonomous
# call is expensive, rather than routine day-to-day work admins should
# just handle themselves.
ESCALATION_EMAIL_TO = _load_env().get('ESCALATION_EMAIL_TO')
SMTP_HOST = _load_env().get('SMTP_HOST')
SMTP_PORT = int(_load_env().get('SMTP_PORT', '587'))
SMTP_USER = _load_env().get('SMTP_USER')
SMTP_PASSWORD = _load_env().get('SMTP_PASSWORD')
# Resolve links in escalation messages must hit THIS process. Prefer an
# explicit ESCALATION_BASE_URL, else follow the .env SELF_BASE_URL (the
# operator's boot-time guess at our own address); the old hardcoded
# localhost:8010 default silently produced dead Approve/Deny URLs whenever
# the real server ran on another port (e.g. :8937).
ESCALATION_BASE_URL = _load_env().get('ESCALATION_BASE_URL') or _load_env().get('SELF_BASE_URL', 'http://localhost:8010')

# Telegram bridge: lets the player talk to the think tank's admin
# from a phone, like texting, without exposing anything to the public
# internet -- this process makes an OUTBOUND long-poll to Telegram's API, so
# no inbound webhook/public port is needed. A server-side integration
# credential (like OPENROUTER_API_KEY above), not a vault entry -- no agent
# ever gets scoped access to it, only this loop uses it. Disabled (no loop
# started) unless both are set. TELEGRAM_ALLOWED_CHAT_IDS is a comma-
# separated allowlist of numeric Telegram chat ids -- without it the bot
# would answer ANYONE who finds its username, not just the player.
TELEGRAM_BOT_TOKEN = _load_env().get('TELEGRAM_BOT_TOKEN')
TELEGRAM_ALLOWED_CHAT_IDS = {c.strip() for c in (_load_env().get('TELEGRAM_ALLOWED_CHAT_IDS') or '').split(',') if c.strip()}
TELEGRAM_NOTIFICATIONS_ENABLED = _env_flag(
    _load_env().get('TELEGRAM_NOTIFICATIONS_ENABLED', 'true'))
TELEGRAM_SUPPRESS_DURING_TESTS = _env_flag(
    _load_env().get('TELEGRAM_SUPPRESS_DURING_TESTS', 'true'))
TELEGRAM_POLL_TIMEOUT_S = 25


def _in_test_process():
    # True when this interpreter is running a test file, not the server.
    import os as _os
    import sys as _sys
    if _os.environ.get('PYTEST_CURRENT_TEST'):
        return True
    argv0 = (_sys.argv[0] or '').replace('\\', '/')
    base = _os.path.basename(argv0)
    return '/tests/' in argv0 or base.startswith('test_')


def telegram_push_allowed():
    if not TELEGRAM_NOTIFICATIONS_ENABLED:
        return False
    if TELEGRAM_SUPPRESS_DURING_TESTS and _in_test_process():
        return False
    return True
# Conversation memory for the Telegram bridge (world/memory.py). STM stores
# each ask turn per chat session and injects the recent turns + a rolling
# summary into the next ask, so a follow-up question is answered in context;
# LTM distills durable facts/preferences from the conversation and recalls the
# relevant ones on each ask. All knobs are live-rebindable via
# _ENV_RELOAD_FIELDS (memory.py reads these serve globals at call time).
MEMORY_ENABLED = _env_flag(_load_env().get('MEMORY_ENABLED', 'true'))
STM_MAX_TURNS = int(_load_env().get('STM_MAX_TURNS', '40') or 40)
STM_MAX_TOKENS = int(_load_env().get('STM_MAX_TOKENS', '8000') or 8000)
STM_TTL_HOURS = int(_load_env().get('STM_TTL_HOURS', '168') or 168)
LTM_MAX_RECALL = int(_load_env().get('LTM_MAX_RECALL', '5') or 5)
LTM_MAX_PER_SESSION = int(_load_env().get('LTM_MAX_PER_SESSION', '200') or 200)
LTM_EXTRACT_EVERY_TURNS = int(_load_env().get('LTM_EXTRACT_EVERY_TURNS', '6') or 6)
# LTM trust filter: a memory below LTM_MIN_CONFIDENCE is never recalled, and a
# memory older than LTM_MAX_AGE_DAYS is not either (0 = no age cap). Prevents
# an offhand false fact from outranking a stable, repeated one.
LTM_MIN_CONFIDENCE = float(_load_env().get('LTM_MIN_CONFIDENCE', '0.3') or 0.3)
LTM_MAX_AGE_DAYS = int(_load_env().get('LTM_MAX_AGE_DAYS', '180') or 180)
# LTM consolidation: periodically merge near-duplicate memories into richer
# single records (LLM merge on a bounded cadence). LTM_CONSOLIDATE_EVERY_TURNS
# is the cadence in new turns; LTM_CONSOLIDATE_MAX_CLUSTERS bounds LLM spend
# per run.
LTM_CONSOLIDATE_ENABLED = _load_env().get('LTM_CONSOLIDATE_ENABLED', 'true') not in ('0', 'false', 'False')
LTM_CONSOLIDATE_EVERY_TURNS = int(_load_env().get('LTM_CONSOLIDATE_EVERY_TURNS', '12') or 12)
LTM_CONSOLIDATE_MAX_CLUSTERS = int(_load_env().get('LTM_CONSOLIDATE_MAX_CLUSTERS', '4') or 4)
# Scoped file + Notes access for agents (world/fs.py). When enabled, the
# `local_file` tool is offered to the ask lane; every call is still gated by a
# player-issued grant (scope + caps) in state, with containment + trash +
# Notes-ownership enforced regardless.
FILE_ACCESS_ENABLED = _load_env().get('FILE_ACCESS_ENABLED', 'true') not in ('0', 'false', 'False')

# App control via AppleScript (world/fs.py). When enabled, the `app_script`
# tool is offered once the player has granted at least one app (bundleId) in
# state; every call is gated by that grant, and the script body is validated
# so it can only talk to the granted app -- never a shell, URL, escalation, or
# a different app (those would bypass the file-grant containment and the Jev
# egress gates).
APP_ACCESS_ENABLED = _load_env().get('APP_ACCESS_ENABLED', 'true') not in ('0', 'false', 'False')


def _load_failures():
    if os.path.exists(FAILURES_PATH):
        with open(FAILURES_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    return []


def _load_successes():
    if os.path.exists(SUCCESSES_PATH):
        with open(SUCCESSES_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    return []


def _save_successes(data):
    with open(SUCCESSES_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def _save_failures(data):
    with open(FAILURES_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def _load_rule_proposals():
    if os.path.exists(RULE_PROPOSALS_PATH):
        with open(RULE_PROPOSALS_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    return []


def _load_success_proposals():
    if os.path.exists(SUCCESS_PROPOSALS_PATH):
        with open(SUCCESS_PROPOSALS_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    return []


def _save_success_proposals(data):
    with open(SUCCESS_PROPOSALS_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def _save_rule_proposals(data):
    with open(RULE_PROPOSALS_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def _record_success(room, title, grade, agent_id='', section=''):
    """Append one high-graded deliverable to the success ledger -- the durable
    output of the 'what worked' step, and the raw material the weekly
    success-mining pass aggregates. Bounded to the tail (SUCCESS_MAX_RECORDS)
    so an aging think tank cannot grow the file without bound. Returns the new
    record's id. Best-effort by design: callers swallow a ledger failure."""
    successes = _load_successes()
    record = {
        'id': 'succ-' + secrets.token_hex(4),
        'ts': time.time(),
        'room': (room or '')[:80],
        'title': (title or '')[:200],
        'grade': grade,
        'section': (section or '')[:120],
        'agentId': agent_id,
    }
    successes.append(record)
    del successes[:-SUCCESS_MAX_RECORDS]
    _save_successes(successes)
    return record['id']


def _success_text_for(room, count, avg_grade):
    """A proposed success-lesson statement for a room that keeps shipping
    high-graded work, phrased as an instruction the operator could encode as a
    standard (the positive mirror of _rule_text_for)."""
    return (f'Keep doing what makes the {room} room ship well: {count} recent '
            f'deliverables graded {avg_grade:.1f}/10 or better. Apply that standard '
            f'and its practices to future work here.')


def _mine_success_proposals(now_ts=None, recurrence=SUCCESS_MIN_RECURRENCE):
    """The positive half of rule mining: group the success ledger by room and
    any room that shipped at least `recurrence` high-graded deliverables becomes
    a PROPOSED success lesson (lesson text + examples + a fixture). Nothing is
    auto-applied -- the operator approves by encoding the standard, exactly like
    the failure rules. Returns the proposals created this pass."""
    now_ts = time.time() if now_ts is None else now_ts
    successes = _load_successes()
    groups = {}
    for s in successes:
        room = (s.get('room') or '').strip()
        if not room:
            continue
        groups.setdefault(room, []).append(s)
    proposals = _load_success_proposals()
    seen = {p.get('dedupeKey') for p in proposals}
    created = []
    for room, rows in sorted(groups.items()):
        if len(rows) < recurrence:
            continue
        dedupe = f'success:{room}'
        if dedupe in seen:
            continue
        avg = sum(float(r.get('grade') or 0.0) for r in rows) / len(rows)
        proposals.append({
            'id': 'succ-rule-' + secrets.token_hex(4),
            'ts': now_ts,
            'room': room,
            'count': len(rows),
            'avgGrade': round(avg, 1),
            'lesson': _success_text_for(room, len(rows), avg),
            'examples': [{'title': r.get('title') or '', 'section': r.get('section') or ''}
                         for r in rows[:3]],
            'fixture': {'title': rows[0].get('title') or '', 'room': rows[0].get('room') or ''},
            'status': 'pending',
            'dedupeKey': dedupe,
        })
        created.append(proposals[-1])
    del proposals[:-SUCCESS_PROPOSALS_MAX]
    if created:
        _save_success_proposals(proposals)
    return created


def _success_proposals():
    """Read-only surface for the operator (the chief of staff endpoint backs onto it)."""
    return _load_success_proposals()


# Deterministic pre-classifier signals: money and any number-without-source is
# ALWAYS a factual_error (never auto-OK'd, the "unsure about anything involving
# money" rule); style-keyworded text maps to style. Everything else needs the
# Jev classifier. Kept separate so the obvious buckets never spend a Jev call.
_FAILURE_MONEY_HINTS = ('$', 'dollar', 'cost', 'price', 'budget')
_FAILURE_STYLE_HINTS = ('rephrase', 'wording', 'tone', 'style', 'awkward', 'fluff', 'jargon', 'verbose')


def _classify_failure_rule(text):
    """Deterministic first cut of the sort step. Returns a FAILURE_TYPES value,
    or None when the text needs the Jev classifier."""
    low = (text or '').lower()
    if any(w in text for w in _FAILURE_MONEY_HINTS) or any(w in low for w in ('cost', 'price', 'budget', 'dollar')):
        return 'factual_error'
    if any(w in low for w in _FAILURE_STYLE_HINTS):
        return 'style'
    return None


def _failure_taxonomy_decider_default(text):
    """The Jev half of the sort step: classify WHY a deliverable was sent back.
    Returns (failure_type, confidence). Fails closed to ('style', 0.0) on a
    failed call or a low-confidence answer -- a mislabeled fact landing in
    'style' only produces a softer proposed rule, never a wrong safety gate
    (the deterministic money rule above already pins the safety-critical
    bucket without any model call)."""
    try:
        decision = _call_openrouter_decision_sync(
            _jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice',
                        'instructions': f'Classify why this draft was sent back for revision: "{text}".',
                        'criteria': {'factual_error': FAILURE_TYPE_LABELS['factual_error'],
                                     'client_preference': FAILURE_TYPE_LABELS['client_preference'],
                                     'missing_information': FAILURE_TYPE_LABELS['missing_information'],
                                     'style': FAILURE_TYPE_LABELS['style']}}})
        choice, confidence, _cost = _jev_choice(decision)
    except Exception:
        return 'style', 0.0
    if choice not in FAILURE_TYPES:
        return 'style', 0.0
    if confidence < _effective_review_grade_confidence():
        return 'style', confidence
    return choice, confidence


_failure_taxonomy_decider = _failure_taxonomy_decider_default


def _classify_failure(text):
    """The sort step: a draft failure -> one of the four buckets. Deterministic
    rules first (zero spend for the obvious cases), Jev classifier as the
    fallback."""
    rule = _classify_failure_rule(text)
    if rule:
        return rule
    return _failure_taxonomy_decider(text)[0]


def _record_failure(failure_type, summary, rule_hint='', section='', input_text='', accepted_text='', agent_id=''):
    """Append one classified failure to the ledger -- the durable output of the
    sort step, and the raw material the weekly rule-mining pass aggregates.
    Bounded to the tail (FAILURE_MAX_RECORDS) so an aging think tank cannot
    grow the file without bound. Returns the new record's id."""
    failures = _load_failures()
    record = {
        'id': 'fail-' + secrets.token_hex(4),
        'ts': time.time(),
        'type': failure_type if failure_type in FAILURE_TYPES else 'style',
        'summary': (summary or '')[:500],
        'ruleHint': (rule_hint or '')[:200],
        'section': (section or '')[:120],
        'input': (input_text or '')[:2000],
        'accepted': (accepted_text or '')[:2000],
        'agentId': agent_id,
    }
    failures.append(record)
    del failures[:-FAILURE_MAX_RECORDS]
    _save_failures(failures)
    return record['id']


def _rule_text_for(failure_type, hint):
    """A proposed rule statement for a recurring failure pattern, phrased as
    an instruction the operator could encode in the ban list or Jev criteria."""
    label = FAILURE_TYPE_LABELS.get(failure_type, 'a recurring issue')
    return f'Never ship work where {label}: "{hint}".'


def _mine_rule_proposals(now_ts=None, recurrence=RULE_MIN_RECURRENCE):
    """The 'write a rule' + 'add a test' steps, as PROPOSALS: group the failure
    ledger by (type, ruleHint) and any pattern that recurred at least
    `recurrence` times becomes a pending proposal carrying the rule text and a
    test fixture (the offending input + the requirement it missed). Nothing is
    auto-applied -- the operator approves by encoding the rule (ban list, Jev
    criteria) and pasting the fixture into a conformance test. Returns the
    proposals created this pass."""
    now_ts = time.time() if now_ts is None else now_ts
    failures = _load_failures()
    groups = {}
    for f in failures:
        ftype = f.get('type') or 'style'
        hint = (f.get('ruleHint') or '').strip()
        if not hint:
            continue
        groups.setdefault((ftype, hint), []).append(f)
    proposals = _load_rule_proposals()
    seen = {p.get('dedupeKey') for p in proposals}
    created = []
    for (ftype, hint), rows in sorted(groups.items()):
        if len(rows) < recurrence:
            continue
        dedupe = f'{ftype}:{hint}'
        if dedupe in seen:
            continue
        proposals.append({
            'id': 'rule-' + secrets.token_hex(4),
            'ts': now_ts,
            'type': ftype,
            'ruleHint': hint,
            'count': len(rows),
            'rule': _rule_text_for(ftype, hint),
            'examples': [{'input': r.get('input') or '', 'issue': r.get('summary') or ''}
                         for r in rows[:3]],
            'fixture': {'input': rows[0].get('input') or '', 'issue': rows[0].get('summary') or ''},
            'status': 'pending',
            'dedupeKey': dedupe,
        })
        created.append(proposals[-1])
    del proposals[:-RULE_PROPOSALS_MAX]
    if created:
        _save_rule_proposals(proposals)
    return created


def _rule_proposals():
    """Read-only surface for the operator (the chief of staff endpoint backs onto it)."""
    return _load_rule_proposals()


SANDBOX_NETWORK = 'ai-think-tank-sandbox-net'  # internal -- no route to the internet at all
EGRESS_NETWORK = 'ai-think-tank-egress-net'    # normal -- real internet access, only the proxy container touches it
PROXY_CONTAINER = 'ai-think-tank-egress-proxy'
# Shared JEV-approved-egress grants file (2026-10-08): a short-lived, scoped
# {host: expiry} map written by serve.py when Jev approves a browser action
# to a non-allowlisted host, and mounted into the egress proxy container so
# the browser can actually reach it. Removed when the run finishes; the proxy
# re-reads it on every connection and ignores expired grants.
EGRESS_GRANTS_PATH = os.path.join(ROOT, 'sandbox_egress_grants.json')
PROXY_PORT = 8899


def _sandbox_proxy_source():
    # The proxy script moved to tools/ in the package split; keep the old
    # top-level path as a fallback for checkouts that have not migrated yet.
    moved = os.path.join(ROOT, 'tools', 'sandbox_proxy.py')
    if os.path.isfile(moved):
        return moved
    return os.path.join(ROOT, 'sandbox_proxy.py')


def _docker_network_exists(name):
    result = subprocess.run(['docker', 'network', 'inspect', name], capture_output=True, timeout=8)
    return result.returncode == 0


def _docker_container_running(name):
    result = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', name], capture_output=True, text=True, timeout=8)
    return result.returncode == 0 and result.stdout.strip() == 'true'


def _docker_container_env_value(name, key):
    # Used to detect a stale proxy container (BROWSE_ALLOWLIST_DOMAINS
    # changed since it was last created) without tearing down a running one
    # on every single restart -- only when its configured allowlist has
    # actually drifted from the current one.
    result = subprocess.run(['docker', 'inspect', '-f', '{{range .Config.Env}}{{println .}}{{end}}', name],
                            capture_output=True, text=True, timeout=8)
    if result.returncode != 0:
        return None
    prefix = key + '='
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def ensure_sandbox_networking():
    # Idempotent, safe to call on every serve.py start -- Docker state
    # (networks, the proxy container) outlives this process, so a restart
    # shouldn't error out on "already exists" or spin up a second proxy.
    #
    # Real fix for "the sandbox needs to install packages," not just
    # flipping the network back on: the sandbox network is INTERNAL (no
    # route out at all -- confirmed, an `apk add` inside a container
    # on this network alone fails outright). The only way out is through
    # `PROXY_CONTAINER`, which is dual-homed on both this network and
    # `EGRESS_NETWORK` (which has real internet access) and only forwards
    # to a fixed allowlist of package-registry hosts (sandbox_proxy.py).
    # A command that ignores the proxy env vars below and tries to
    # connect directly still has nowhere to go -- the isolation is
    # enforced by the network topology, not by tool cooperation.
    if not _docker_network_exists(SANDBOX_NETWORK):
        subprocess.run(['docker', 'network', 'create', '--internal', SANDBOX_NETWORK], capture_output=True, timeout=8)
    if not _docker_network_exists(EGRESS_NETWORK):
        subprocess.run(['docker', 'network', 'create', EGRESS_NETWORK], capture_output=True, timeout=8)
    # Player-vetted data sites: the SAME BROWSE_ALLOWLIST_DOMAINS
    # /api/browse skips Jev for, passed into the proxy so agent-run SCRIPTS
    # can do real systematic crawling against them too, not just one-URL-at-
    # a-time browse_page calls -- see sandbox_proxy.py's own module docstring
    # for why this stays narrow (only the player-vetted list, never general
    # internet access). If the proxy is already running with a stale/
    # different allowlist (BROWSE_ALLOWLIST_DOMAINS changed since it was last
    # created), recreate it so the running proxy never silently drifts from
    # the current config -- but only then, not on every ordinary restart.
    desired_extra_hosts = ','.join(sorted(_effective_allowlist_domains()))
    if _docker_container_running(PROXY_CONTAINER):
        current_extra_hosts = _docker_container_env_value(PROXY_CONTAINER, 'SANDBOX_EGRESS_EXTRA_HOSTS')
        current_grants_path = _docker_container_env_value(PROXY_CONTAINER, 'SANDBOX_EGRESS_GRANTS_PATH')
        # Recreate when the allowlist env drifted OR the Jev-approved-egress
        # grants file isn't mounted yet (a proxy created before 2026-10-08
        # won't have it -- without it, Jev-approved browser hosts stay
        # unreachable at the network layer).
        if current_extra_hosts != desired_extra_hosts or current_grants_path != EGRESS_GRANTS_PATH:
            subprocess.run(['docker', 'rm', '-f', PROXY_CONTAINER], capture_output=True, timeout=8)
    if not _docker_container_running(PROXY_CONTAINER):
        subprocess.run(['docker', 'rm', '-f', PROXY_CONTAINER], capture_output=True, timeout=8)  # clear a stale/stopped one, if any
        subprocess.run([
            'docker', 'run', '-d', '--name', PROXY_CONTAINER,
            '--network', SANDBOX_NETWORK,
            '-e', f'SANDBOX_EGRESS_EXTRA_HOSTS={desired_extra_hosts}',
            '-e', f'SANDBOX_EGRESS_GRANTS_PATH={EGRESS_GRANTS_PATH}',
            '-v', f'{_sandbox_proxy_source()}:/proxy.py:ro',
            '-v', f'{EGRESS_GRANTS_PATH}:{EGRESS_GRANTS_PATH}:ro',
            SANDBOX_IMAGE, 'python3', '/proxy.py',
        ], capture_output=True, timeout=8)
        subprocess.run(['docker', 'network', 'connect', EGRESS_NETWORK, PROXY_CONTAINER], capture_output=True, timeout=8)


# JEV-approved egress grants (2026-10-08): a {host: expiry-epoch} map in the
# shared file the egress proxy re-reads on every connection. Write a grant
# right before a Jev-approved browser run to a non-allowlisted host, revoke it
# when the run finishes. Atomic replace so the proxy never reads a
# half-written file; short TTL so an orphaned grant expires on its own.
_EGRESS_GRANT_TTL_S = 120
_egress_grants_lock = threading.Lock()


def _grant_egress_host(host):
    if not host:
        return
    host = (host or '').lower().strip()
    with _egress_grants_lock:
        grants = {}
        try:
            with open(EGRESS_GRANTS_PATH, 'r') as f:
                grants = json.load(f) or {}
        except (OSError, ValueError):
            grants = {}
        grants[host] = time.time() + _EGRESS_GRANT_TTL_S
        _write_egress_grants(grants)


def _revoke_egress_host(host):
    if not host:
        return
    host = (host or '').lower().strip()
    with _egress_grants_lock:
        try:
            with open(EGRESS_GRANTS_PATH, 'r') as f:
                grants = json.load(f) or {}
        except (OSError, ValueError):
            grants = {}
        grants.pop(host, None)
        _write_egress_grants(grants)


def _write_egress_grants(grants):
    # Written IN PLACE, not via tmp+os.replace: the egress proxy bind-mounts
    # this exact file (now :ro), and a bind mount pins the original inode --
    # an os.replace would hand the proxy a NEW inode it never sees, silently
    # freezing the grants it applies. In-place truncate+write updates the same
    # inode the proxy watches by mtime. Safe on a torn write: the proxy's read
    # fails closed (empty grants) until the file is next written fully.
    with open(EGRESS_GRANTS_PATH, 'w') as f:
        json.dump(grants, f)


def _run_docker_sandbox(docker_cmd, timeout_s):
    """Run a sandbox container and REAP it if the CLI times out. A killed
    `docker run` does not stop the container -- a command like `sleep 1000`
    would otherwise keep eating its 256m/1cpu allocation per timed-out call
    until the command exits on its own. A --cidfile records the container id,
    and a timed-out run is `docker rm -f`'d explicitly before the timeout is
    re-raised. Returns the subprocess.CompletedProcess, or re-raises the
    TimeoutExpired after reaping (callers keep their current timedOut=True
    path)."""
    import tempfile as _tmp
    cid_file = _tmp.NamedTemporaryFile(delete=False, suffix='.cid')
    cid_path = cid_file.name
    cid_file.close()
    try:
        # Docker 29+ refuses --cidfile when the file already exists ("container
        # ID file found..."). The placeholder above is only a unique-name
        # reservation -- remove it so docker creates the cidfile itself.
        try:
            os.remove(cid_path)
        except OSError:
            pass
        # --cidfile is a `docker run` option and must precede the image name.
        idx = docker_cmd.index(SANDBOX_IMAGE)
        cmd = docker_cmd[:idx] + ['--cidfile', cid_path] + docker_cmd[idx:]
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                with open(cid_path) as f:
                    cid = f.read().strip()
                if cid:
                    subprocess.run(['docker', 'rm', '-f', cid],
                                   capture_output=True, timeout=15)
            except Exception:
                pass
            raise
    finally:
        try:
            os.remove(cid_path)
        except OSError:
            pass


def _run_in_sandbox_sync(sandbox_dir, command, limits=None):
    # Backend dispatch: SANDBOX_EXECUTION chooses WHERE agent
    # commands run. `local` (default) -> the local Docker sandbox below;
    # `digitalocean` -> a provisioned Droplet. The digitalocean branch FAILS
    # CLOSED with an explicit error until a remote executor is actually
    # provisioned -- it must never silently fall through to the local
    # backend, because that would run a command the agent believed was
    # executing on DigitalOcean somewhere entirely different (a security lie,
    # not just a correctness bug). Only reachable when the player has flipped
    # the switch, per _digitalocean_enabled.
    if SANDBOX_EXECUTION == 'digitalocean':
        return {
            'exitCode': None,
            'stdout': '',
            'stderr': ('SANDBOX_EXECUTION=digitalocean is set but a DigitalOcean execution '
                       'backend has not been provisioned yet. No command was run anywhere -- '
                       'flip SANDBOX_EXECUTION back to local, or provision the DO executor.'),
            'timedOut': False,
        }
    # Only reachable network is SANDBOX_NETWORK (internal -- see
    # ensure_sandbox_networking() above); the proxy env vars are what let
    # a well-behaved package manager actually install anything, not a
    # relaxation of the isolation itself. --memory/--cpus/--pids-limit cap
    # resource usage; --rm means nothing lingers after; only `sandbox_dir`
    # is mounted, so nothing outside /workspace is reachable even from
    # inside the container -- confirmed (an `ls /Users` from inside
    # an identical container found nothing there at all).
    proxy_url = f'http://{PROXY_CONTAINER}:{PROXY_PORT}'
    limits = limits or resources.limits_for('light')
    docker_cmd = [
        'docker', 'run', '--rm',
        '--network', SANDBOX_NETWORK,
        '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges',
        '-e', f'http_proxy={proxy_url}', '-e', f'https_proxy={proxy_url}',
        '-e', f'HTTP_PROXY={proxy_url}', '-e', f'HTTPS_PROXY={proxy_url}',
        '--memory', f"{int(limits.get('memoryMb') or 512)}m",
        '--cpus', str(limits.get('cpus') or 1),
        '--pids-limit', str(int(limits.get('pidsLimit') or 128)),
        '-v', f'{sandbox_dir}:/workspace',
        '-w', '/workspace',
        SANDBOX_IMAGE,
        # bash, not dash (sh): agents author bash (arrays, [[ ]], process
        # substitution). dash dies with `Syntax error: "(" unexpected` on those
        # and the command never ran -- observed killing tasks 445/447/448/449.
        # bash is already in the image (Debian slim ships it); POSIX-only
        # scripts keep working under it.
        'bash', '-c', command,
    ]
    try:
        result = _run_docker_sandbox(
            docker_cmd, int(limits.get('timeoutS') or SANDBOX_TIMEOUT_S))
        return {
            'exitCode': result.returncode,
            'stdout': _compact_command_output(result.stdout),
            'stderr': _compact_command_output(result.stderr),
            'timedOut': False,
        }
    except subprocess.TimeoutExpired as e:
        return {'exitCode': None, 'stdout': _compact_command_output(e.stdout or ''), 'stderr': _compact_command_output(e.stderr or ''), 'timedOut': True}


def run_cron_script_sandboxed(script_relpath, timeout_s, env):
    """Run one allowlisted cron script inside the same internal-only sandbox
    and egress proxy as execute_script. The repo is mounted read-only; only
    library/ is writable. No host environment is inherited."""
    if SANDBOX_EXECUTION != 'local':
        return {'exitCode': None, 'stdout': '',
                'stderr': ('cron scripts require SANDBOX_EXECUTION=local; '
                           'no command was run'),
                'timedOut': False}
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    library = os.path.join(repo_root, 'library')
    os.makedirs(library, exist_ok=True)
    proxy_url = f'http://{PROXY_CONTAINER}:{PROXY_PORT}'
    docker_cmd = [
        'docker', 'run', '--rm',
        '--network', SANDBOX_NETWORK,
        '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges',
        '-e', f'http_proxy={proxy_url}', '-e', f'https_proxy={proxy_url}',
        '-e', f'HTTP_PROXY={proxy_url}', '-e', f'HTTPS_PROXY={proxy_url}',
        '--memory', '512m',
        '--cpus', '1',
        '--pids-limit', '128',
        # Run as the host user so files created under the writable library
        # mount stay owned by the operator, not root.
        '--user', f'{os.getuid()}:{os.getgid()}',
        '-v', f'{repo_root}:/workspace:ro',
        '-v', f'{library}:/workspace/library:rw',
        '-w', '/workspace',
    ]
    for key, value in (env or {}).items():
        if key and value is not None:
            docker_cmd += ['-e', f'{key}={value}']
    docker_cmd += [SANDBOX_IMAGE, 'python3', script_relpath]
    try:
        result = _run_docker_sandbox(docker_cmd, timeout_s)
        return {
            'exitCode': result.returncode,
            'stdout': _compact_command_output(result.stdout),
            'stderr': _compact_command_output(result.stderr),
            'timedOut': False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            'exitCode': None,
            'stdout': _compact_command_output(e.stdout or ''),
            'stderr': _compact_command_output(e.stderr or ''),
            'timedOut': True,
        }


# browser_act: run the fixed browser driver inside the SAME sandbox container
# as every other agent command (see browser_driver.py / the sandbox Dockerfile
# COPY). One op per invocation, driver is a fixed /opt/browser_driver.py, never
# a free-form script. Network rides the same internal SANDBOX_NETWORK +
# allowlist egress proxy, so a page an agent script couldn't reach via the
# proxy is a page the browser can't load either. The action is written to the
# sandbox's own scratch dir (mounted at /workspace) so the driver's persistent
# profile + result/screenshot files survive for the caller to read back.
BROWSER_ACT_TIMEOUT_S = 180
BROWSER_ACT_MAX_OUTPUT = 60_000


def _read_browser_act_result(result, result_path, shot_path):
    """Prefer the driver's own result file (not truncated by capture caps),
    then fall back to stdout JSON. Shared by the one-shot and session paths."""
    if os.path.isfile(result_path):
        try:
            with open(result_path, 'r') as f:
                parsed = json.load(f)
            if os.path.isfile(shot_path):
                with open(shot_path, 'rb') as f:
                    parsed['_shotB64'] = base64.b64encode(f.read()).decode()
            return parsed, None
        except (OSError, ValueError):
            pass
    out = (result.stdout or '')[:BROWSER_ACT_MAX_OUTPUT]
    try:
        parsed = json.loads(out)
        if os.path.isfile(shot_path):
            with open(shot_path, 'rb') as f:
                parsed['_shotB64'] = base64.b64encode(f.read()).decode()
        return parsed, None
    except ValueError:
        return None, (f'browser driver produced no parseable result '
                      f'(exit {result.returncode}): '
                      f'{out[-500:] or (result.stderr or "")[-500:]}')
