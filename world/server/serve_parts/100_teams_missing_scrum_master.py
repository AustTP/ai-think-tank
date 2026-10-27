

def _team_open_stuck_count(state, director_id):
    """Open cards on a director's direct reports that are visibly stuck.

    Counts failed/repairing/blocked cards and review cards that have already
    cycled at least twice. A single normal review is not a stuck card; three
    of these on a lean team mean the director's implicit facilitation is not
    holding and the team needs a real facilitator or an explicit acknowledgement.
    """
    members = {d.get('id') for d in (state.get('agentRoster') or [])
               if d.get('director') == director_id}
    if not members:
        return 0
    count = 0
    for task in (state.get('tasks') or {}).values():
        if task.get('assignedTo') not in members:
            continue
        status = task.get('status')
        if status in ('done', 'cancelled', 'quarantined'):
            continue
        gate = task.get('_peerGate') or {}
        if status in ('failed', 'repairing', 'blocked'):
            count += 1
        elif status == 'needs_review' and int(gate.get('cycleCount') or 0) >= 2:
            count += 1
        elif task.get('_stuck') or task.get('needsHelp'):
            count += 1
    return count


def _team_missed_ceremonies(state, team_id):
    """Closed sprints for this team whose retrospective never ran, recent first.

    A retrospective is recorded in state['retrospectives'][sprint_id] when the
    ceremony resolves. Two of a team's last five closed sprints missing one is
    the ceremony-side signal that the director's implicit facilitation has
    stopped holding.
    """
    closed = []
    for sid, sprint in (state.get('sprints') or {}).items():
        if sprint.get('status') != 'closed':
            continue
        if team_id not in (sprint.get('teamIds') or []):
            continue
        closed.append((sprint.get('closedAt') or 0, sid))
    closed.sort(reverse=True)
    retros = state.get('retrospectives') or {}
    return sum(1 for _ts, sid in closed[:5] if sid not in retros)


def _teams_missing_scrum_master(state, team_ids):
    """Which of the given team ids are missing a REQUIRED scrum master. Returns
    a list of {id, name} for enforcement at sprint creation, so a caller can
    tell the player exactly which teams to fix first.

    Scrum masters scale with team size: a team below
    SCRUM_MASTER_MIN_TEAM_SIZE workers usually doesn't need a dedicated
    facilitator -- its director stands in -- so it is not flagged. It is
    flagged once it has three or more open stuck cards, or two of its last five
    closed sprints missed a retrospective, and it has not explicitly
    acknowledged director facilitation. Teams at/above the threshold that still
    lack a designated scrum master always block a sprint."""
    import sim as _sim
    floor_default = getattr(_sim, 'SCRUM_MASTER_MIN_TEAM_SIZE', 4)
    stuck_trigger = getattr(_sim, 'SMALL_TEAM_STUCK_TRIGGER', 3)
    missing = []
    existing_ids = {(t.get('id')) for t in (state.get('teams') or [])}
    for tid in team_ids:
        if tid not in existing_ids:
            continue  # unknown teams aren't the scrum-master gate's concern
        t = next((x for x in (state.get('teams') or []) if x.get('id') == tid), None)
        if t.get('scrumMasterId'):
            continue
        director_id = t.get('directorId')
        floor = (_sim.team_policy(state, tid, 'scrumMasterMinTeamSize')
                 if hasattr(_sim, 'team_policy') else floor_default)
        if director_id and _sim._team_member_count(state, director_id) < floor:
            if t.get('directorFacilitation'):
                continue  # the director explicitly owns facilitation
            stuck = _team_open_stuck_count(state, director_id)
            missed = _team_missed_ceremonies(state, tid)
            if stuck < stuck_trigger and missed < 2:
                continue  # small team, still healthy -- director stands in
            missing.append({'id': tid, 'name': t.get('name') or tid})
            continue
        missing.append({'id': tid, 'name': t.get('name') or tid})
    return missing


app = FastAPI(lifespan=_lifespan)
from server.confirmations_api import router as _confirmations_router  # noqa: E402
from server.supervision_api import router as _supervision_router  # noqa: E402
from server.feedback_api import router as _feedback_router  # noqa: E402
from server.live_coverage_api import router as _live_coverage_router  # noqa: E402
app.include_router(_confirmations_router)
app.include_router(_supervision_router)
app.include_router(_feedback_router)
app.include_router(_live_coverage_router)


OPENROUTER_API_KEY = _load_env().get('OPENROUTER_API_KEY')
# Tavily search: real web search, not guessed URLs.
# CAPTCHA wall on every major engine (confirmed against both Google and
# DuckDuckGo). Tavily is a real API built for exactly this (AI-agent search),
# returns clean {title, url, content} results, no scraping/CAPTCHA involved.
# search_web is simply absent from AGENT_ASK_TOOLS when this is unset.
TAVILY_API_KEY = _load_env().get('TAVILY_API_KEY')
# GitHub PAT: read-only access to real public repos/issues/PRs
# so engineering work grounds itself in real code. Loaded as a module constant
# like TAVILY/OPENROUTER keys; _github_call reads it for every request. Absent
# (blank) means the GitHub tools are simply NOT offered to agents -- same
# pattern as search_web, so the surface never advertises an unusable tool.
GITHUB_TOKEN = _load_env().get('GITHUB_TOKEN')
# Apify API key: real account access for web scraping/automation
# (running actors, fetching datasets). The account is on the FREE plan with a
# ~$5/mo usage cap by the player's choice -- see the Apify budget block below
# for the monthly cap surfaced in the Bank. Absent (blank) means the reconcile
# line is omitted (fail closed), never fabricated.
APIFY_API_KEY = _load_env().get('APIFY_API_KEY')
# Higgsfield AI API key: a two-part credential (key ID + shared
# secret) for image/video generation + editing, stored in .env like the other
# service keys. Loaded as module constants; _higgsfield_configured() is the
# single availability gate (both halves present) that gates the
# generate_image / generate_video agent tools and the Higgsfield helpers below
# -- the same "absent means the surface never advertises it" pattern as
# TAVILY_API_KEY/GITHUB_TOKEN. The tools simply don't exist in the tool list
# until both halves are configured.
HIGGSFIELD_API_KEY_ID = _load_env().get('HIGGSFIELD_API_KEY_ID')
HIGGSFIELD_API_KEY_SECRET = _load_env().get('HIGGSFIELD_API_KEY_SECRET')
# OpenSky Network (flights): OAuth2 client-credentials pair (client id +
# secret), created at opensky-network.org/my-opensky/account. Loaded as module
# constants so the registry's env credential resolves them (same pattern as
# the other service keys). The server mints a short-lived access token from
# the pair and injects it as a Bearer header; agents never see the pair.
# Absent (blank) means the registered opensky service reports a missing
# credential -- the tools stay present, the calls fail clean.
OPEN_SKY_CLIENT_ID = _load_env().get('OPEN_SKY_CLIENT_ID')
OPEN_SKY_CLIENT_SECRET = _load_env().get('OPEN_SKY_CLIENT_SECRET')
# AISStream (ships): API key for the real-time AIS WebSocket stream at
# wss://stream.aisstream.io/v0/stream. Loaded as a module constant so the
# registry's env credential can resolve it (same pattern as the other service
# keys). The live feed is WebSocket-only, so the HTTP api_call path cannot
# stream it today -- the registry entry documents the key for a future
# stream collector. Absent (blank) means the registered aisstream service
# reports a missing credential.
AISSTREAM_API_KEY = _load_env().get('AISSTREAM_API_KEY')
# HuggingFace token: Hub API (model discovery/metadata) + Inference Providers
# router (OpenAI-compatible chat at router.huggingface.co/v1). Loaded as a
# module constant so the registry's env credential resolves it (same pattern
# as the other service keys). Gated model repos still need a one-time license
# accept on huggingface.co under this account; ungated models work immediately.
HF_TOKEN = _load_env().get('HF_TOKEN')


def _higgsfield_configured():
    return bool(HIGGSFIELD_API_KEY_ID and HIGGSFIELD_API_KEY_SECRET)


# ---- Higgsfield image/video generation (wired 2026-10-06) ------------------
# Real generative media through the think tank's Higgsfield account. One
# authenticated, asynchronous request lifecycle for every model: POST to the
# model endpoint with the two-part `Key ID:SECRET` credential, get a
# request_id, poll the status endpoint to a terminal state. Call shape
# verified against the live docs (docs.higgsfield.ai) -- SOUL V2
# (higgsfield-ai/soul/v2/standard) for images, Kling 3.0 Standard
# (kling-video/v3.0/std/text-to-video) for video -- NOT guessed from the
# OpenAPI spec, per the "verify, don't assume" discipline applied to every
# other real integration. Cost is the REAL up-front /estimate number for the
# exact body being submitted, accrued on SUCCESS only (failed/nsfw/canceled
# requests are not charged by Higgsfield). The caller never holds the raw
# credential halves; the vault pattern is the same as every other service.
_HIGGSFIELD_BASE_URL = 'https://api.higgsfield.ai'
_HIGGSFIELD_IMAGE_ENDPOINT = 'higgsfield-ai/soul/v2/standard'
_HIGGSFIELD_VIDEO_ENDPOINT = 'kling-video/v3.0/std/text-to-video'
_HIGGSFIELD_POLL_IMAGE_TIMEOUT_S = float(_load_env().get('HIGGSFIELD_POLL_IMAGE_TIMEOUT_S', '90') or 90)
_HIGGSFIELD_POLL_VIDEO_TIMEOUT_S = float(_load_env().get('HIGGSFIELD_POLL_VIDEO_TIMEOUT_S', '300') or 300)


def _higgsfield_call(method, path, body=None, timeout=120):
    """Real call to the Higgsfield API (https://api.higgsfield.ai{path}) with
    the two-part Key credential, mirroring _pixellab_call's shape. `path`
    includes the leading slash and any endpoint id, e.g.
    '/higgsfield-ai/soul/v2/standard' or '/requests/<id>/status'. Returns
    (data, error) like every other external integration."""
    if not (HIGGSFIELD_API_KEY_ID and HIGGSFIELD_API_KEY_SECRET):
        return None, 'Higgsfield is not configured (no API key pair in the vault)'
    try:
        data_bytes = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(
            f'{_HIGGSFIELD_BASE_URL}{path}', data=data_bytes, method=method,
            headers={
                'Authorization': f'Key {HIGGSFIELD_API_KEY_ID}:{HIGGSFIELD_API_KEY_SECRET}',
                'Content-Type': 'application/json',
            })
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 -- fixed Higgsfield API host
            return json.loads(resp.read().decode('utf-8', errors='replace')), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:500]
        return None, f'Higgsfield call failed ({e.code}): {detail}'
    except Exception as e:
        return None, f'Higgsfield call failed: {e}'


def _higgsfield_estimate_usd(endpoint_id, body):
    """Real up-front cost estimate from Higgsfield's /estimate endpoint for the
    EXACT body being submitted -- a verified number, never a guess (the
    "don't fabricate a number" rule applied to every other real integration).
    Returns float USD or None (estimate unavailable)."""
    data, error = _higgsfield_call('POST', f'/estimate/{endpoint_id}', body, timeout=30)
    if error or not isinstance(data, dict):
        return None
    try:
        usd = float(data.get('usd') or 0)
        return usd if usd > 0 else None
    except (TypeError, ValueError):
        return None


def _higgsfield_poll(request_id, timeout=300, interval=3):
    """Poll a Higgsfield request until a terminal status (completed/failed/
    nsfw/canceled), the timeout, or an error -- same shape as
    _pixellab_poll_job. Returns the final (data, error) tuple."""
    start = time.time()
    while time.time() - start < timeout:
        data, error = _higgsfield_call('GET', f'/requests/{request_id}/status', timeout=30)
        if error:
            return None, error
        status = (data or {}).get('status')
        if status in ('completed', 'failed', 'nsfw', 'canceled'):
            return data, None
        time.sleep(interval)
    return None, 'Higgsfield request did not finish in time (timed out while polling)'


def _higgsfield_log_manifest(media_kind, prompt, endpoint_id, urls, agent_id, agent_key):
    """Write a durable manifest of a completed generation into the Library
    (media/generated/), so the player has a record and the links while
    Higgsfield retains the output. Best-effort: a manifest failure must never
    fail the generation that already succeeded."""
    try:
        slug = ''.join(c if c.isalnum() else '-' for c in prompt.lower())[:40].strip('-') or 'media'
        path = f'media/generated/{int(time.time() * 1000)}-{media_kind}-{slug}.md'
        content = (
            f"# Generated {media_kind} -- {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n"
            f"Model: {endpoint_id}\nPrompt: {prompt}\n\nOutput URL(s):\n"
            + '\n'.join(f'- {u}' for u in urls)
            + '\n\nHiggsfield retains generated output for at least 7 days. Download it to your '
              'own storage for long-term retention.\n'
        )
        _http_json('POST', SELF_BASE_URL, '/api/library/file',
                   {'agentId': agent_id, 'path': path, 'content': content, 'source': 'firsthand'},
                   agent_key)
    except Exception:
        pass


def _higgsfield_generate(media_kind, args, agent_id=None, agent_key=None):
    """Run one real Higgsfield generation: estimate the cost up-front, submit,
    poll to completion, accrue the real estimated cost on SUCCESS only, write a
    durable manifest into the Library, and return a model-facing result string.
    The single choke point for every Higgsfield generation, so spend is accrued
    exactly once per real, billed generation. `media_kind` is 'image' or
    'video'."""
    args = args or {}
    prompt = (args.get('prompt') or '').strip()
    if not prompt:
        return 'prompt is required'
    if media_kind == 'image':
        endpoint_id = _HIGGSFIELD_IMAGE_ENDPOINT
        body = {'prompt': prompt}
    else:
        endpoint_id = _HIGGSFIELD_VIDEO_ENDPOINT
        body = {'prompt': prompt}
        duration = args.get('duration')
        if duration is not None:
            try:
                body['duration'] = max(3, min(15, int(duration)))
            except (TypeError, ValueError):
                pass
        ar = (args.get('aspect_ratio') or '').strip()
        if ar in ('16:9', '9:16', '1:1'):
            body['aspect_ratio'] = ar
        sound = (args.get('sound') or '').strip().lower()
        if sound in ('on', 'off'):
            body['sound'] = sound
    usd_estimate = None
    base = _api_base_url('api.higgsfield.ai')
    # Runs through the generic workflow chokepoint: estimate up front, submit,
    # poll to completion -- all driven by the registry's higgsfield workflow
    # spec (no bespoke _higgsfield_call/estimate/poll here). Spend is accrued
    # inside _api_execute ONLY on a successful 'completed' terminal.
    out = _api_execute(agent_id, agent_key, base, 'POST', body, {},
                       f'generate {media_kind}', workflow=True,
                       workflow_params={'endpoint': endpoint_id})
    if out.get('ok') is not True:
        return f'Could not generate: {out.get("reason") or out.get("error") or "failed"}'
    result = out.get('data') or {}
    terminal = result.get('status') or 'unknown'
    if terminal != 'completed':
        return f'Generation did not complete (status: {terminal}). It was not charged.'
    urls = []
    for item in (result.get('images') or []):
        if isinstance(item, dict) and item.get('url'):
            urls.append(item['url'])
    v = result.get('video')
    if isinstance(v, dict) and v.get('url'):
        urls.append(v['url'])
    for item in (result.get('videos') or []):
        if isinstance(item, dict) and item.get('url'):
            urls.append(item['url'])
    if not urls:
        return f'Generation completed but no output URL was returned: {json.dumps(result)[:500]}'
    usd_estimate = out.get('usd')
    charged = isinstance(usd_estimate, (int, float)) and usd_estimate > 0
    _higgsfield_log_manifest(media_kind, prompt, endpoint_id, urls, agent_id, agent_key)
    kind_word = 'image' if media_kind == 'image' else 'video'
    cost_line = f' (~${usd_estimate:.3f} billed to the think tank)' if charged else ''
    return (f'Generated a real {kind_word}. Output URL(s): ' + ', '.join(urls) + cost_line
            + '. A manifest with these links is under media/generated/ in the Library.')




SERVER_ACCESS_KEY = _get_or_create_server_secret()

# Real authentication -- once you're considering a public
# deployment, the old model (anyone who loads the page learns the same
# bearer key everyone else does, straight out of the page's own source)
# stops being acceptable. This is a real login: a single admin account
# (single-tenant by design -- a second
# real user account is only worth adding if a real need appears,
# not as a default to build speculatively now), a salted PBKDF2
# password hash (stdlib only, no new dependency), and a server-side
# session whose id is the only thing the browser ever holds, as an
# HttpOnly cookie -- unlike the old key, page JS (and so an XSS bug)
# can't read it at all.
#
# Credential creation, hashing, the session cookie constants, and login
# rate limiting live in auth.py (extracted 2026-10-05) and are reached
# through `serve.` at call time. The three assignments below are the only
# auth code that stays here: they RUN at import time, storing each secret
# in ~/ai-think-tank/.env on first boot and exposing the loaded values.
ADMIN_USERNAME, ADMIN_PASSWORD_SALT, ADMIN_PASSWORD_HASH, _GENERATED_PASSWORD = _get_or_create_admin_credentials()






DEVICE_API_KEY, _GENERATED_DEVICE_KEY = _get_or_create_device_key()

# The inbound webhook key (see /api/webhook/{channel} below): external
# watchers -- the Google Apps Script that watches the Gmail inbox for X
# notification emails being the first -- present this bearer key on every
# POST, the same shape as X-Device-Key above. Same "auto-generate, persist
# to .env, surface once" convention -- the only time the plaintext is
# printed is the first boot that generates it. One key for every channel:
# the channel name in the URL scopes the data, not the credential.
def _get_or_create_webhook_key():
    env = _load_env()
    key = env.get('WEBHOOK_API_KEY')
    if key:
        return key, None
    key = secrets.token_urlsafe(24)
    with open(os.path.join(THINK_TANK_DIR, '.env'), 'a', encoding='utf-8') as f:
        f.write(f'\nWEBHOOK_API_KEY={key}\n')
    return key, key  # second value set only when freshly generated

WEBHOOK_API_KEY, _GENERATED_WEBHOOK_KEY = _get_or_create_webhook_key()

# Our own X handle (no @, from .env): the x-mention webhook consumer (loop
# below /api/webhook/{channel}) only files a large request when the relayed
# notification is someone ELSE mentioning us -- the Gmail watcher relays
# every X notification email, including our own activity, which must never
# wake the tank. Unset = consumer disabled (loop never started), the same
# all-or-nothing gate as the Telegram bridge.
X_HANDLE = (_load_env().get('X_HANDLE') or '').strip().lstrip('@')



# Kill switch for agent internet access -- this needs to be
# something you can flip off in one place without touching code, same
# spirit as the API key itself living in .env rather than in world/.
# Defaults to enabled; set AGENT_BROWSING_ENABLED=false in ~/ai-think-tank/.env
# to shut it off entirely.
BROWSING_ENABLED = _load_env().get('AGENT_BROWSING_ENABLED', 'true').strip().lower() != 'false'

# Same kill-switch pattern: Telegram only,
# no more email. Keeps the credential/outbox machinery intact (so flipping
# it back on needs no re-provisioning) -- this just gates the actual send.
PLAYER_EMAIL_ENABLED = _load_env().get('PLAYER_EMAIL_ENABLED', 'true').strip().lower() != 'false'

# Hard absolute spend cap, added after a real
# incident: a _peer_gated_lane bug let a scheduled research task loop
# review/fix forever, burning ~$9 across both think tanks in one evening before
# anyone noticed. That bug is fixed, but this is deliberately independent
# protection against ANY future bug (known or not) doing the same thing --
# a manual, absolute ceiling, not a per-bug patch. Explicit 0 disables it;
# unset defaults to a conservative $50/MONTH so a fresh/researcher clone is
# bounded until the player chooses a ceiling (was '0' -- a new think tank
# ran UNbounded until the .env was hand-edited, which is the exact failure
# mode this protection exists for; was '5' as a cumulative ceiling before the
# cap became a per-month budget that resets at each UTC month boundary).
# Baseline (spend at the moment this protection was installed) is stored
# once in the ledger itself, per month, so pre-existing historical spend never
# counts against it -- only what accrues within the current month does, and a
# new month starts with a fresh allowance (see _think_tank_spend_cap_exceeded).
# To raise the ceiling, raise
# SPEND_CAP_USD in .env and restart (deliberately manual, no live reset
# endpoint -- a cap you can silently raise from inside the think tank isn't a
# real ceiling).
SPEND_CAP_USD = float(_load_env().get('SPEND_CAP_USD', '50') or 0)

# Per-agent monthly spend ceiling, checked at the model chokepoints that know
# WHO is spending (/api/chat, _api_execute): a prompt-injected or runaway
# agent can otherwise burn the WHOLE shared monthly budget (and every shared
# service quota) for every other agent in one session. Default 0 = disabled
# (keep the existing single shared ceiling unless a per-agent cap is wanted).
AGENT_SPEND_CAP_USD = float(_load_env().get('AGENT_SPEND_CAP_USD', '0') or 0)

# OpenRouter funding model, split in two so the numbers mean what the
# operator actually said:
#
#   * LIVE ACCOUNT FUNDS -- the real dollars sitting in the OpenRouter
#     account right now, read from the /api/v1/credits probe (account-wide
#     total_credits purchased minus total_usage spent). The percentage
#     ALERTS fire on THIS: warn when >= 80% of the deposited funds have been
#     spent, critical at >= 90%. E.g. deposit $10, spend $8 -> warn (80%
#     used); spend $9 -> critical (90% used). No deposit -> no divide, no
#     alert from this path.
#
#   * MONTHLY PLAN -- the operator's expected total funding for the month
#     (may be topped up gradually, not one lump sum). Purely planning
#     CONTEXT for the /api/budget gauge and the chief of staff health view: "only
#     $10 is in the account right now, but I expect to add $50 this month."
#     It is NOT the alert denominator and NOT an enforced cap. 0/unset just
#     means the gauge skips the plan line.
OPENROUTER_MONTHLY_BUDGET_USD = float(_load_env().get('OPENROUTER_MONTHLY_BUDGET_USD', '0') or 0)
# Fixed alert thresholds as fractions of the LIVE account funds (remaining /
# totalCredits). Named constants (not env knobs) so the 20%/10% shape the
# operator asked for is one obvious place and tests can pin the exact
# thresholds.
OPENROUTER_CREDITS_WARN_PCT = 0.20   # remaining <= 20% of live funds -> warning push
OPENROUTER_CREDITS_CRIT_PCT = 0.10   # remaining <= 10% of live funds -> critical push
# Credit watchdog: when the live OpenRouter balance is at or below this floor,
# pause new model-dependent work, send one notice, and keep probing. When the
# balance is restored, resume the queued work automatically. No hourly
# reminders. The watchdog never buys or tops up credits.
OPENROUTER_CREDITS_WATCHDOG_FLOOR_USD = float(
    _load_env().get('OPENROUTER_CREDITS_WATCHDOG_FLOOR_USD', '0.50') or 0.50)
_CREDITS_WATCHDOG_STATE = {'low': False}
_CREDITS_PAUSE = {'active': False, 'since': 0.0}


class OpenRouterCreditsPaused(RuntimeError):
    """Raised when a model call is refused because the account has no credits."""
    credits_paused = True


def credits_pause_active():
    return bool(_CREDITS_PAUSE.get('active'))


def _reset_model_circuits():
    """Credits failures are not model failures. On restore, close every model
    circuit so queued work resumes immediately instead of waiting out a
    5-minute breaker cooldown."""
    with _model_circuit_lock:
        for state in _model_circuit_state.values():
            state['consecutive_failures'] = 0
            state['open_until'] = 0
            state['probing'] = False


def _set_credits_pause(active, credits=None, source='probe'):
    """Toggle the global credit pause and send one message per transition."""
    active = bool(active)
    if active == _CREDITS_PAUSE.get('active'):
        return None
    _CREDITS_PAUSE['active'] = active
    _CREDITS_PAUSE['since'] = time.time() if active else 0.0
    _CREDITS_WATCHDOG_STATE['low'] = active
    remaining = None
    if isinstance(credits, dict):
        try:
            remaining = float(credits.get('remaining') or 0)
        except (TypeError, ValueError):
            remaining = None
    if active:
        subject = '[AI Think Tank] OpenRouter credits exhausted'
        body = (f'Remaining ${remaining:.2f}.' if remaining is not None else 'The account is out of credits.') + \
               ' New model work is paused. Queued work will resume automatically when credits are added.'
    else:
        _reset_model_circuits()
        subject = '[AI Think Tank] OpenRouter credits restored'
        body = (f'Remaining ${remaining:.2f}. ' if remaining is not None else '') + \
               'Work resumed automatically where it stopped.'
    try:
        send_player_telegram_sync(subject, body)
    except Exception:
        pass
    try:
        log_action('system', 'credits_pause',
                   {'active': active, 'source': source, 'remaining': remaining},
                   authorized=True)
    except Exception:
        pass
    return 'paused' if active else 'resumed'

# Page-request budget: the think tank has a MONTHLY allowance of
# EXTERNAL page requests -- each browse_page fetch (/api/browse) and each
# search_web call (Tavily) counts as ONE request. A count-based quota, not a
# dollar cap: 1000 free page requests/month (set PAGE_REQUEST_MONTHLY_BUDGET in
# .env; 0/unset disables). Enforced at the two chokepoints so a runaway
# research request (spikes now allowed up to 50 page fetches) can't blow the
# month. Rollover: a fresh calendar month resets the counter.
PAGE_REQUEST_MONTHLY_BUDGET = int(_load_env().get('PAGE_REQUEST_MONTHLY_BUDGET', '1000') or 1000)
PAGE_REQUEST_LEDGER_KEY = '__page_budget__'
PAGE_REQUEST_BUDGET_START_KEY = '__page_budget_start__'



# HIGH-tier spend cap: the high tier is the expensive one (a
# stronger model for high-stakes planning), and the player wants its USE kept
# very limited -- a dollar budget, not a confidence one. This is a MONTHLY cap
# on total high-tier model spend: once the month's allowance is consumed, the
# JEV tier gate fails CLOSED on high (routing to mid instead), so the expensive
# tier can never overrun. Set HIGH_TIER_MONTHLY_BUDGET_USD in .env to change it
# (e.g. 1.00 = at most $1 of high-tier spend per calendar month); 0
# explicitly disables the cap. The default is $5, a real cap for a band that
# is meant to be rare. Accrual happens at the /api/chat choke point (see
# _accrue_high_tier_spend), the single place every model call's real cost is
# known.
HIGH_TIER_MONTHLY_BUDGET_USD = float(_load_env().get('HIGH_TIER_MONTHLY_BUDGET_USD', '5') or 0)
# Highest per-million-token price the HIGH tier will even CONSIDER:
# the refresh only offers Jev/score-pick candidates priced at or below
# this, so a $210/M model can never win the high tier no matter its score.
# This is a per-call price ceiling (models under it), distinct from the monthly
# spend cap above (how much aggregate high-tier spend is allowed). Set
# HIGH_TIER_MAX_PRICE_USD in .env (e.g. 6.00 = never pick a model over $6/M);
# 0/unset disables the ceiling.
HIGH_TIER_MAX_PRICE_USD = float(_load_env().get('HIGH_TIER_MAX_PRICE_USD', '0') or 0)
# Same per-model price ceiling for the CODING tier: the refresh only offers
# Jev/score-pick coding candidates at or below this, so a $12/M coding
# front-runner can't win on score alone when the player wants coding spend
# bounded too. Set CODING_TIER_MAX_PRICE_USD in .env; 0/unset disables.
CODING_TIER_MAX_PRICE_USD = float(_load_env().get('CODING_TIER_MAX_PRICE_USD', '0') or 0)
# Same per-model price ceiling for the VISION tier: the refresh only offers
# MMMU-scored candidates at or below this, so a frontier vision model can't
# win on image-comprehension score alone past the bound. Set
# VISION_TIER_MAX_PRICE_USD in .env; 0/unset disables.
VISION_TIER_MAX_PRICE_USD = float(_load_env().get('VISION_TIER_MAX_PRICE_USD', '0') or 0)
# The SUBLOW tier's per-model price ceiling. Unlike the ceilings above, this
# one is the band's very definition, not an optional bound on an open-ended
# search: sublow exists to give short, bounded test scenarios run at scale
# (hundreds of reenactment/testing agents) the cheapest model that is still
# coherent, hard-capped at $0.09/M blended by the player's explicit call.
# So it defaults ON at 0.09 rather than 0/off -- 0 here would let sublow
# compete across the whole catalog and duplicate the low band's pick.
SUBLOW_TIER_MAX_PRICE_USD = float(_load_env().get('SUBLOW_TIER_MAX_PRICE_USD', '0.09') or 0)
# Reserved spend-ledger bucket name for high-tier accrual (kept apart from
# per-service buckets so the bank can show "how much went to the expensive
# tier" at a glance, and so the cap reads it cleanly).
HIGH_TIER_LEDGER_KEY = '__high_tier__'

# --- Test-time compute (deliberation) on the low and mid tiers ----------------
# The low and mid tiers are the cheap, quality-limited models the sim leans on
# most (chat, daily logs, gathering). Because they can't just "think harder"
# on their own like the expensive tier, /api/chat runs a lightweight
# deliberation loop for them: sample the prompt `TTC_BEST_OF` times and fold
# the drafts into one answer by JSON-majority vote (structured responses) or a
# same-model self-verification pass (open-ended prose), so the *reported*
# output is far more consistent than any single cheap draft. Each sample is a
# real billed call, so the whole thing is gated on a flag and capped at
# TTC_MAX_BEST_OF -- high-tier and reasoning-tier calls never deliberate
# (they're already expensive), and a caller can opt out per-request with
# {"deliberate": false}.
TTC_ENABLED = os.environ.get('TTC_ENABLED', '1').lower() not in ('0', 'false', 'no', 'off')
TTC_BEST_OF = max(2, min(int(_load_env().get('TTC_BEST_OF', '2') or 2), int(_load_env().get('TTC_MAX_BEST_OF', '3') or 3)))
TTC_MAX_BEST_OF = max(TTC_BEST_OF, int(_load_env().get('TTC_MAX_BEST_OF', '3') or 3))


def _ttc_should_deliberate(model, deliberate=None, best_of=None):
    """Whether a /api/chat request should get test-time compute at all: the
    feature is enabled, the resolved model is a low/mid (cheap) tier (the
    expensive/reasoning/coding tiers already think hard enough per call), and
    the caller hasn't explicitly opted out. An explicit request for
    best_of>1 is honored even when the caller omitted `deliberate`."""
    if not TTC_ENABLED:
        return False
    if deliberate is not None and not deliberate:
        return False
    if best_of is not None and int(best_of) > 1:
        return True
    if model is None:
        return False
    if model in (_high_tier_slug(), _coding_tier_slug(), _reasoning_tier_slug()):
        return False
    # Low and mid tiers (None is the fail-closed slug from _resolve_model_tier
    # when the model_tiers table is empty -- no configured tiers, no guessing).
    return model in (_low_tier_slug(), _mid_tier_slug())


def _ttc_best_of(best_of=None):
    """Clamp a caller's best_of (or fall back to the configured default) into
    the [2, TTC_MAX_BEST_OF] range. Returns 1 (no sampling) when deliberation
    is disabled entirely."""
    if not TTC_ENABLED:
        return 1
    if best_of is not None:
        try:
            return max(2, min(int(best_of), TTC_MAX_BEST_OF))
        except (TypeError, ValueError):
            return TTC_BEST_OF
    return TTC_BEST_OF


def _ttc_majority_json(samples):
    """Fold best-of-N drafts of a JSON-structured request into ONE answer by
    exact-match majority vote. Each sample is the OpenRouter result dict; the
    candidate is its reply content (normalized: fences and surrounding text
    stripped). The most common candidate wins, ties broken by first-appearance
    order. Returns the winning candidate string, or None when no candidate is
    parseable."""
    from collections import Counter
    counts = Counter()
    order = []
    seen = set()
    for s in samples:
        content = (s.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        cleaned = re.sub(r'^```json\s*|^```\s*|```\s*$', '', content.strip()).strip()
        if not cleaned:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            order.append(cleaned)
        counts[cleaned] += 1
    if not order:
        return None
    best = max(order, key=lambda c: counts[c])
    return best if counts[best] >= 2 else None


def _ttc_self_verify(model, messages, best_of, max_tokens, drafts=None, service=None,
                     village_id=None):
    """Self-verification pass for open-ended (non-JSON) prompts: take best-of-N
    drafts of the SAME request and ask the SAME cheap model to pick the best
    one and return it VERBATIM (best-of-N with a judge -- often measurably
    better than voting for prose). Returns the chosen content string, or the
    first draft when verification fails.

    Cost accounting stays accurate: when `drafts` is None the helper samples
    them itself and accrues each sample's cost to `service`; otherwise the
    caller already accrued the drafts and the helper only pays for the single
    judge call."""
    if drafts is None:
        drafts = []
        for _ in range(int(best_of)):
            r = _call_openrouter_sync(model, messages=messages, max_tokens=max_tokens)
            drafts.append((r.get('choices') or [{}])[0].get('message', {}).get('content') or '')
            if service:
                cost = (r.get('usage') or {}).get('cost', 0.0)
                if isinstance(cost, (int, float)) and cost:
                    _accrue_spend(service, cost, village_id=village_id)
    drafts = [d for d in drafts if d]
    if not drafts:
        return ''
    if len(drafts) < 2:
        return drafts[0]
    judge = (
        'Here are several candidate answers to the same request. Choose the single BEST one -- '
        'the most complete, accurate, and clearly written -- and return it VERBATIM, byte for byte, '
        'with no commentary, no quotes, no prefixes, no markdown.'
    )
    judge_messages = [{'role': 'system', 'content': judge}]
    for i, d in enumerate(drafts):
        judge_messages.append({'role': 'user', 'content': f'Candidate {i + 1}:\n{d}'})
    judge_messages.append({'role': 'user', 'content': 'Return the best candidate verbatim.'})
    r = _call_openrouter_sync(model, messages=judge_messages, max_tokens=max_tokens)
    if service:
        cost = (r.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            _accrue_spend(service, cost, village_id=village_id)
    chosen = (r.get('choices') or [{}])[0].get('message', {}).get('content') or ''
    return chosen if chosen else drafts[0]




# Apify FREE-plan monthly budget: the player's Apify account is on
# the FREE plan with a ~$5/mo usage cap by choice -- no subscription, no
# pay-as-you-go. This is a MONTHLY budget, mirroring the high-tier cap above:
# the Bank shows used/cap/left against it, and _accrue_apify_spend records
# real actor-run spend so the think tank never quietly exceeds what the plan
# allows. Set APIFY_MONTHLY_BUDGET_USD in .env (default 5.00 = the FREE plan's
# real cap); 0/unset disables the budget row entirely.
APIFY_MONTHLY_BUDGET_USD = float(
    _load_env().get('APIFY_MONTHLY_BUDGET_USD', '5') or 0)
# Reserved spend-ledger bucket name for Apify accrual -- kept apart from
# per-service buckets like the high-tier one, and seeded into the Bank view as
# its own row so the $5/month cap is visible from day one, not only after the
# first actor run lands in the ledger.
APIFY_LEDGER_KEY = '__apify__'
# Brief cache for the live account-usage reconcile (mirrors the OpenRouter
# credits cache: a bank readout shouldn't always cost a network round-trip).
_APIFY_USAGE_CACHE = {'at': 0.0, 'data': None}
_APIFY_USAGE_CACHE_TTL_S = 60




def _apify_account_usage():
    """Live reconcile against the REAL Apify account -- GET /v2/users/me for
    the plan's monthly cap, then /v2/users/me/usage/monthly for what this
    cycle has actually spent. Account-wide (not the think tank's own ledger), so
    a bank readout can show the real number the plan is enforcing. Returns a
    dict {capUsd, usedUsd, remainingUsd, cycleStart, cycleEnd} or None on any
    failure (no key, network, bad response) so the Bank teller fails closed
    and omits the line rather than ever showing stale or fabricated numbers.
    Cached briefly like the OpenRouter credits reconcile."""
    if not APIFY_API_KEY:
        return None
    now = time.time()
    cached = _APIFY_USAGE_CACHE
    if cached['data'] is not None \
            and (now - cached['at']) < _APIFY_USAGE_CACHE_TTL_S:
        return cached['data']
    try:
        def _get(path):
            req = urllib.request.Request(
                f'https://api.apify.com/v2{path}',
                headers={'Authorization': f'Bearer {APIFY_API_KEY}'})
            with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
                raw = resp.read().decode('utf-8', errors='replace')
                return json.loads(raw)
        me = _get('/users/me')
        plan = (me.get('data') or {}).get('plan') or {}
        cap = float(plan.get('maxMonthlyUsageUsd') or 0) \
            or APIFY_MONTHLY_BUDGET_USD
        usage = _get('/users/me/usage/monthly')
        u = (usage or {}).get('data') or {}
        used = 0.0
        for svc, v in (u.get('monthlyServiceUsage') or {}).items():
            used += float((v or {}).get('amountAfterVolumeDiscountUsd') or 0)
        cycle = u.get('usageCycle') or {}
        result = {
            'capUsd': cap,
            'usedUsd': round(used, 6),
            'remainingUsd': round(max(0.0, cap - used), 6),
            'cycleStart': cycle.get('startAt'),
            'cycleEnd': cycle.get('endAt'),
        }
        cached['at'] = now
        cached['data'] = result
        return result
    except Exception:
        return None


def _apify_call(path, method='GET', body=None, query=None, timeout=30):
    """Real call against the Apify platform API: https://api.apify.com/v2{path}
    with the player's APIFY_API_KEY as a Bearer token. Returns (data, error) --
    error is a human-readable string, data is the parsed JSON response (or a
    truncated raw-text fallback if the response isn't JSON). Same vault pattern
    as _treg_call/_pixellab_call/_google_call: the caller (a spike tool
    executor) never holds the raw key, only this server-side chokepoint does.
    Never raises -- any failure (no key, network, bad response, non-2xx) is
    returned as (None, error) so callers fail closed.

    GET vs POST placement mirrors _treg_call's live-confirmed rule: a POST
    call's params that Apify wants on the query string (timeout,
    maxTotalChargeUsd) go in `query`, while the actor's input JSON goes in the
    body. Cost is accrued by the caller from the run object's usageTotalUsd --
    a real, Apify-reported number, matching the think tank's "don't fabricate a
    number, use a verified one" rule."""
    if not APIFY_API_KEY:
        return None, 'Apify is not configured (no APIFY_API_KEY in the vault)'
    url = f'https://api.apify.com/v2{path}'
    headers = {'Authorization': f'Bearer {APIFY_API_KEY}'}
    data_bytes = None
    if method in ('POST', 'PUT', 'PATCH'):
        headers['Content-Type'] = 'application/json'
        data_bytes = json.dumps(body or {}).encode('utf-8')
    if query:
        url += '?' + urllib.parse.urlencode(query)
    try:
        req = urllib.request.Request(url, data=data_bytes, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 -- fixed Apify API host
            raw = resp.read().decode('utf-8', errors='replace')
        try:
            return json.loads(raw), None
        except ValueError:
            return {'_raw': raw[:5000]}, None
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:500]
        return None, f'Apify call failed ({e.code}): {detail}'
    except Exception as e:
        return None, f'Apify call failed: {e}'


BROWSE_MAX_BYTES = 200_000
BROWSE_TIMEOUT_S = 10

# Categories an agent's browsing request is checked against BEFORE any
# fetch happens -- deliberately classifying the destination + stated
# purpose, not fetched content, so a rejected request never actually pulls
# anything onto this machine in the first place. This is the real gate;
# everything else here (SSRF checks, GET-only, size/time caps) is
# necessary hygiene around it, not a substitute for it.
# BROWSE_BLOCK_CATEGORIES is loaded from world/browse_policy.json by
# world/config.py (operator-edited; a blocked category ALWAYS blocks regardless
# of the agent's stated purpose -- see the module docstring there).

# Explicit allowlist -- player's own call: a small, deliberately
# curated set of domains the PLAYER has already vetted, so an agent never has
# to re-litigate them through Jev every single visit. Everything else still
# goes through the full Jev classify + confidence-gated escalation above --
# this is an addition for specific, named, low-risk sites, never a general
# bypass. Still passes through _is_safe_public_host (SSRF/private-network
# protection) unconditionally -- the allowlist skips the CONTENT/purpose
# judgment call, never the network-safety one.
#
# Also used by ensure_sandbox_networking() to let agent-run SCRIPTS (not
# just the browse_page tool) reach these same domains through the sandbox's
# egress proxy. That proxy deliberately never terminates TLS (see
# sandbox_proxy.py), so it cannot restrict HTTP methods inside HTTPS --
# treat every entry here as full read+write reachability for a script, not
# just read access, when deciding whether a domain belongs on this list.
BROWSE_ALLOWLIST_DOMAINS = {d.strip().lower() for d in
                            _load_env().get('BROWSE_ALLOWLIST_DOMAINS', '').split(',') if d.strip()}
# Human-approved runtime grants: domains the player approved via
# the '/api/allowlist/request' flow's email link, persisted in the DB settings
# table so a restart keeps them (the .env list above is the code-reviewed
# baseline; these are the approval-anchored additions on top of it). Merged
# into _effective_allowlist_domains() below, which every gate reads -- so a
# single runtime grant reaches /api/browse, the sandbox egress proxy, AND the
# Colab URL gate alike. Same reachability semantics as the env list: full
# read+write for a script, never terminated TLS, so approve wisely.
ALLOWLIST_GRANTS_SETTING = 'allowlist_grants'


def _runtime_allowlist_grants():
    raw = _get_setting(ALLOWLIST_GRANTS_SETTING, '') or ''
    return {d.strip().lower() for d in raw.split(',') if d.strip()}


def _effective_allowlist_domains():
    return BROWSE_ALLOWLIST_DOMAINS | _runtime_allowlist_grants()


# Standing safe domains (operator decision, 2026-10-13): a curated READ-ONLY
# tier for public reference sources the agents asked for by name (their #1
# complaint was waiting on email for routine sources). These skip the Jev
# classify round trip on read paths -- browse, GET/HEAD api-call, and the
# redirect re-gate -- exactly like an allowlist entry, but they are DELIBERATELY
# NOT part of _effective_allowlist_domains(): the sandbox egress proxy, Colab
# URL gate, and _is_allowlisted_host's full read+write semantics stay untouched.
# A standing domain is reachable to READ, never as standing write surface.
# The list itself is operator data in world/browse_policy.json
# (`standing_safe_domains`), loaded live by config.py.


def _is_standing_safe_host(hostname):
    """True for a curated public-reference host: exact or subdomain match
    against STANDING_SAFE_DOMAINS (same suffix rule as
    _is_allowlisted_host). Read-only tier -- see the comment above."""
    host = (hostname or '').lower()
    return any(host == d or host.endswith('.' + d) for d in STANDING_SAFE_DOMAINS)


def _grant_allowlist(host, actor='admin', via='escalation approval'):
    """Persist a human-approved allowlist grant and refresh the egress proxy
    so running sandbox scripts can actually reach it. Returns the normalized
    host, or None when empty. `actor`/`via` are attributed in the action log
    so a director's direct grant is distinguishable from the player's email
    link."""
    host = (host or '').strip().lower()
    if not host:
        return None
    grants = _runtime_allowlist_grants()
    grants.add(host)
    _set_setting(ALLOWLIST_GRANTS_SETTING, ','.join(sorted(grants)))
    log_action(actor, 'allowlist_grant', {'host': host, 'via': via}, authorized=True)
    try:
        ensure_sandbox_networking()
    except Exception as e:
        # A grant still persisted; a proxy refresh failure shouldn't VETO a
        # human's explicit approval, it just means scripts wait until restart.
        print(f'[allowlist] egress proxy refresh failed after grant: {e}', flush=True)
    return host


def _revoke_allowlist(host, actor='admin', via='operator revoke'):
    """Remove a runtime allowlist grant and refresh the egress proxy so
    already-running sandbox scripts lose the host too. Returns the normalized
    host on a real removal, 'not_granted' when the host was only in the env
    baseline (that list is a .env restart change, not a runtime grant), or
    None when the host wasn't on the list at all / was empty.

    Only RUNTIME grants (the DB-persisted ones layered on top of the env
    baseline) are removable here. The env baseline stays operator-owned: it
    is the code-reviewed list a fresh start boots with, so removing one of
    those is a .env edit + restart, not a live toggle -- this keeps the
    effective list always a superset of the reviewed baseline."""
    host = (host or '').strip().lower()
    if not host:
        return None
    grants = _runtime_allowlist_grants()
    if host not in grants:
        return 'not_granted' if host in BROWSE_ALLOWLIST_DOMAINS else None
    grants.discard(host)
    _set_setting(ALLOWLIST_GRANTS_SETTING, ','.join(sorted(grants)))
    log_action(actor, 'allowlist_revoke', {'host': host, 'via': via}, authorized=True)
    try:
        ensure_sandbox_networking()
    except Exception as e:
        # A revoke still persisted; a proxy refresh failure just means the
        # egress proxy keeps the host until restart -- the Jev/browse gates
        # already re-read the effective list per call, so they drop it now.
        print(f'[allowlist] egress proxy refresh failed after revoke: {e}', flush=True)
    return host


def _is_allowlisted_host(hostname):
    host = (hostname or '').lower()
    return any(host == d or host.endswith('.' + d) for d in _effective_allowlist_domains())


def _allowlist_wait_samples(window_days=90, cap=50):
    """Historical seconds between each allowlist REQUEST and the GRANT that
    resolved the same host (first request -> first later grant, per host).
    Used only for the human-readable wait estimate agents see on blocked
    fetches and request confirmations -- never for a decision. Returns [] on
    any DB problem (estimate degrades to 'no history yet')."""
    try:
        cutoff = time.time() - window_days * 86400
        with _db() as conn:
            rows = conn.execute(
                "SELECT action, ts, details FROM action_log "
                "WHERE action IN ('allowlist_request', 'allowlist_grant') "
                "AND ts > ? ORDER BY ts", (cutoff,)).fetchall()
    except Exception:
        return []
    first_request, samples = {}, []
    for action, ts, details in rows:
        try:
            host = (json.loads(details) if details else {}).get('host') or ''
        except Exception:
            continue
        host = host.strip().lower()
        if not host:
            continue
        if action == 'allowlist_request':
            first_request.setdefault(host, ts)
        elif host in first_request and ts >= first_request[host]:
            delta = ts - first_request[host]
            if 0 <= delta <= window_days * 86400:
                samples.append(delta)
            first_request.pop(host, None)
    samples.sort()
    return samples[-cap:]
