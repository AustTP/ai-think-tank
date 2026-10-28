"""Free-provider pool: a quota-aware free-first layer in front of the paid
tier picks of the bands listed in model_providers.json's free_pool section.

Three free sources, each with its own quota reality (operator-researched):
  * Google AI Studio (Gemini free tier) -- quotas are PER MODEL: exhausting
    one model's limit never touches another's. Flash-class models are the
    usable workhorses (Pro-class is ~2 RPM / ~50 RPD and useless for volume).
  * Groq Console (free tier) -- quotas are ORG-WIDE SHARED: every Groq model
    draws from one collective RPM bucket (and per-model TPM that is tight,
    6k-30k/min, so TPM exhausts long before the daily request ceiling).
  * OpenRouter ":free" models -- one GLOBAL account pool: 20 RPM across all
    free models, 50 RPD for accounts with lifetime spend under $10 and 1000
    RPD at $10 or more (verified against /api/v1/credits: this account is
    over the threshold, so 1000).

How it works: `_low_tier_slug` (060) consults `_freepool_route_for_band`
BEFORE the DB tier pick. A healthy free endpoint resolves to the synthetic
slug '@freepool:<band>:<endpoint>'; `_model_provider_for` (130) translates
that slug to a concrete OpenAI-compatible endpoint spec, so the whole existing
transport (resilience, circuit breaker, key-health, training capture) applies
unchanged. When no free endpoint has quota room, the band's normal PAID pick
serves -- the paid model and the free pool are used in conjunction, not
either/or. When a free call fails (429 quota, 5xx, dead key, or a 200 with
null content from a reasoning-starved free model), the call layers fall
through to the band's paid slug so the agent still gets an answer.

Free routes are exempt from the credits pause and the monthly spend cap by
construction (price_per_m 0): a spent budget pauses PAID work exactly as
before, while free models keep serving -- the budget-exhausted fallback the
pool exists to provide.

Quota windows are in-memory (fixed 60s buckets for RPM/TPM, UTC-day buckets
for RPD) and conservative: a server restart resets them, so the first minutes
after a restart may burn a few calls into 429s; every 429 puts the endpoint on
a cooldown (Retry-After when the provider sends one, else 60s; a daily-limit
response cools down until the next UTC midnight) and the paid pick absorbs
the traffic until then. The circuit breaker adds the persistent-failure
backstop on top.
"""

import contextvars
import json
import os
import threading
import time

# Resolved once at import from ROOT (world/) -- the same file
# _load_model_providers reads. Kept as this part's own constant so tests can
# repoint it without patching the shared config namespace.
_FREE_POOL_PROVIDERS_PATH = os.path.join(ROOT, 'model_providers.json')
# Hermeticity: the free pool can hand a model call a LIVE network path, so it
# must never do that inside a test suite. Every suite repoints THINK_TANK_DIR
# at a temp dir (the shared setUpModule discipline), so the pool only
# activates when the state dir is the real project root it booted with. A
# suite that WANTS to exercise the pool patches this constant alongside its
# own fixture paths.
_FREE_POOL_PROJECT_ROOT = os.path.dirname(ROOT)

# Re-read model_providers.json at most once per second (stat-guarded) so an
# operator edit lands without a restart, like MODEL_PROVIDER_OVERRIDES does on
# its reload path.
_FREE_POOL_STAT_INTERVAL_S = 1.0
_free_pool_cache = {'at': 0.0, 'mtime': None, 'cfg': None}
_free_pool_lock = threading.Lock()
# All mutable quota state lives behind _free_pool_lock. RPM/TPM are keyed by
# pool_key (so Groq's org-wide shared buckets are counted once across its
# models); RPD is keyed per endpoint because providers meter daily requests
# per model even when the minute buckets are shared.
_free_pool_state = {
    'rpm': {},            # pool_key -> {'minute': int, 'stamps': [ts, ...]}
    'rpd': {},            # endpoint name -> {'date': 'YYYY-MM-DD', 'count': int}
    'tpm': {},            # pool_key -> {'minute': int, 'tokens': int}
    'cooldown': {},       # endpoint name -> ts until which the endpoint is skipped
    'or_free_banned': {},  # catalog model_id -> ts until which the pick skips it
    'or_free_chosen': None,
}

_FREE_POOL_NAME_RE_STRICT = 'abcdefghijklmnopqrstuvwxyz0123456789._-'


def _freepool_valid_name(name):
    return bool(name) and all(c in _FREE_POOL_NAME_RE_STRICT for c in name)


def _load_free_pool_config():
    """Parse and validate the free_pool section of model_providers.json.
    Returns None when the file is missing, the section is absent/disabled, or
    anything is malformed -- a typo must disable the pool, never redirect
    traffic (same discipline as _load_model_providers)."""
    try:
        with open(_FREE_POOL_PROVIDERS_PATH, encoding='utf-8') as fh:
            raw = json.load(fh)
    except Exception:
        return None
    pool = raw.get('free_pool') if isinstance(raw, dict) else None
    if not isinstance(pool, dict) or not pool.get('enabled'):
        return None
    bands = [b for b in (pool.get('bands') or []) if isinstance(b, str) and b.strip()]
    mode = pool.get('mode') or 'free-first'
    if mode not in ('free-first', 'budget-only', 'paid-first'):
        return None
    endpoints = []
    for ep in (pool.get('endpoints') or []):
        if not isinstance(ep, dict):
            continue
        name = str(ep.get('name') or '').strip()
        if not _freepool_valid_name(name):
            continue
        base_url = str(ep.get('base_url') or '').strip()
        if base_url and not base_url.startswith(('http://', 'https://')):
            continue
        model = str(ep.get('model') or '').strip()
        is_or_free = bool(ep.get('openrouter_free'))
        if not is_or_free and not (base_url and model):
            continue
        service = str(ep.get('service') or '').strip() or ('openrouter' if is_or_free else name)
        api_key_env = str(ep.get('api_key_env') or '').strip()
        extra = ep.get('extra') if isinstance(ep.get('extra'), dict) else None

        def _limit(key):
            try:
                value = int(ep.get(key) or 0)
            except (TypeError, ValueError):
                return 0
            return max(0, value)

        endpoints.append({
            'name': name, 'service': service, 'base_url': base_url,
            'model': model, 'api_key_env': api_key_env,
            'openrouter_free': is_or_free, 'extra': extra,
            'price_per_m': 0.0,
            'pool_key': str(ep.get('pool_key') or '').strip() or name,
            'rpm': _limit('rpm'), 'rpd': _limit('rpd'), 'tpm': _limit('tpm'),
        })
    if not endpoints:
        return None
    try:
        min_score = pool.get('or_free_min_score')
        min_score = None if min_score is None else float(min_score)
    except (TypeError, ValueError):
        min_score = None
    return {'enabled': True, 'bands': [b.strip() for b in bands],
            'mode': mode, 'endpoints': endpoints,
            'or_free_min_score': min_score}


def _free_pool_cfg():
    # Test-suite gate first: a repointed THINK_TANK_DIR means hermetic tests
    # are running -- the pool stays inert and unmocked model calls behave
    # exactly as they did before the pool existed.
    try:
        if os.path.realpath(THINK_TANK_DIR) != os.path.realpath(_FREE_POOL_PROJECT_ROOT):
            return None
    except Exception:
        return None
    now = time.time()
    if now - _free_pool_cache['at'] < _FREE_POOL_STAT_INTERVAL_S and _free_pool_cache['cfg'] is not None:
        return _free_pool_cache['cfg']
    try:
        mtime = os.stat(_FREE_POOL_PROVIDERS_PATH).st_mtime
    except OSError:
        return None
    if _free_pool_cache['mtime'] == mtime and _free_pool_cache['cfg'] is not None:
        _free_pool_cache['at'] = now
        return _free_pool_cache['cfg']
    cfg = _load_free_pool_config()
    _free_pool_cache['at'] = now
    _free_pool_cache['mtime'] = mtime
    _free_pool_cache['cfg'] = cfg
    return cfg


def _reload_free_pool_config():
    """Drop the cache so the next read re-parses model_providers.json (wired
    onto the same reload path that refreshes MODEL_PROVIDER_OVERRIDES)."""
    _free_pool_cache['at'] = 0.0
    _free_pool_cache['mtime'] = None
    _free_pool_cache['cfg'] = None


# --- Governance: who can switch the pool off, and how the switches bite ---
#
# Two authorities, per the operator's design:
#   * GLOBAL -- the chief of staff (the admin agent) or the player turns a
#     service off for the whole tank.
#   * TEAM -- each director turns a service off for their own team's work
#     (a team IS a director: team id == director id).
# The global switch OVERRIDES the team switch: a service blocked globally is
# blocked for everyone, and no team setting can re-enable it. A team switch
# only ever narrows. Both live on the state document (state['freePoolPolicy'])
# so they survive restarts and are visible to the same state surfaces every
# other tank setting uses; the policy API (server/free_pool_policy_api.py)
# writes them with the same caller-auth discipline as /api/supervision.
#
# Enforcement needs to know WHOSE work a pick is for. Model calls are made
# from plain worker threads with no ambient agent identity, so the work
# dispatch stamps the acting agent into a ContextVar (which raw threads do
# NOT inherit -- the dispatch sets it inside its own worker thread, where
# asyncio.to_thread propagation then carries it through the sync call chain).
# Calls with no stamped actor (player /api/chat, JEV quorum, health loops)
# are governed by the global switches only -- which is the correct reading:
# those calls are not any team's work.

FREE_POOL_SERVICES = ('gemini', 'groq', 'openrouter')
# The paid switch rides the same policy document and the same team/global
# scopes, but it is not a pool endpoint: it blocks the LOW band's paid leg
# (the DB pick or a @provider:low pin) so the free pool can be tested with
# no paid safety net underneath it. Mid/high/coding calls are untouched.
POLICY_SERVICES = FREE_POOL_SERVICES + ('paid',)

_FREE_POOL_ACTOR_TEAM: contextvars.ContextVar = contextvars.ContextVar(
    'free_pool_actor_team', default=None)
_FREE_POOL_TEAM_MAP = {'at': 0.0, 'map': {}}
_FREE_POOL_TEAM_MAP_TTL_S = 60.0
_FREE_POOL_POLICY_CACHE = {'at': 0.0, 'policy': None}
_FREE_POOL_POLICY_TTL_S = 5.0


def _free_pool_team_of(agent_id):
    """The team id whose work `agent_id` is doing: the agent's roster
    `director` (a team IS a director -- team id == director id). Resolved
    from one cached roster read per minute; None for unknown actors."""
    if not agent_id:
        return None
    now = time.time()
    with _free_pool_lock:
        stale = now - _FREE_POOL_TEAM_MAP['at'] > _FREE_POOL_TEAM_MAP_TTL_S
    if stale:
        try:
            state = get_state_from_db() or {}
            roster = state.get('agentRoster') or []
            mapping = {d.get('id'): (d.get('director') or None)
                       for d in roster if isinstance(d, dict) and d.get('id')}
        except Exception:
            mapping = {}
        with _free_pool_lock:
            _FREE_POOL_TEAM_MAP['map'] = mapping
            _FREE_POOL_TEAM_MAP['at'] = now
    with _free_pool_lock:
        return _FREE_POOL_TEAM_MAP['map'].get(agent_id)


def _set_free_pool_actor(agent_id):
    """Stamp the acting agent's team into the ambient slot. Called at the top
    of a work-dispatch worker thread (sim 030) so every free-pool pick made
    while that agent's work runs is attributed to the agent's team."""
    try:
        _FREE_POOL_ACTOR_TEAM.set(_free_pool_team_of(agent_id))
    except Exception:
        pass


def _free_pool_policy():
    """The disabled-service sets, read from the state doc behind a short
    cache (the policy API invalidates it on every write, so a director's
    switch lands on the next pick immediately). Fail-open: an unreadable
    policy disables nothing."""
    now = time.time()
    with _free_pool_lock:
        if (_FREE_POOL_POLICY_CACHE['policy'] is not None
                and now - _FREE_POOL_POLICY_CACHE['at'] <= _FREE_POOL_POLICY_TTL_S):
            return _FREE_POOL_POLICY_CACHE['policy']
    try:
        raw = (get_state_from_db() or {}).get('freePoolPolicy') or {}
    except Exception:
        raw = {}
    policy = {
        'globalDisabled': {s: bool((raw.get('globalDisabled') or {}).get(s))
                           for s in POLICY_SERVICES},
        'teams': {team: {s: bool((entry or {}).get(s)) for s in POLICY_SERVICES}
                  for team, entry in (raw.get('teams') or {}).items()
                  if isinstance(entry, dict)},
    }
    with _free_pool_lock:
        _FREE_POOL_POLICY_CACHE['at'] = now
        _FREE_POOL_POLICY_CACHE['policy'] = policy
    return policy


def _invalidate_free_pool_policy_cache():
    with _free_pool_lock:
        _FREE_POOL_POLICY_CACHE['at'] = 0.0
        _FREE_POOL_POLICY_CACHE['policy'] = None


def _freepool_service_blocked(ep):
    """'global' / 'team' / None: why (if at all) this endpoint's service is
    switched off for the current pick. Global overrides team."""
    try:
        policy = _free_pool_policy()
        if policy['globalDisabled'].get(ep['service']):
            return 'global'
        team = _FREE_POOL_ACTOR_TEAM.get()
        if team and (policy['teams'].get(team) or {}).get(ep['service']):
            return 'team'
    except Exception:
        return None
    return None


def _low_paid_blocked():
    """'global' / 'team' / None: whether the LOW band's paid leg is switched
    off for the current pick, by the same two-authority policy as the free
    switches. Global overrides team; a team switch only bites calls stamped
    with that team's actor."""
    try:
        policy = _free_pool_policy()
        if policy['globalDisabled'].get('paid'):
            return 'global'
        team = _FREE_POOL_ACTOR_TEAM.get()
        if team and (policy['teams'].get(team) or {}).get('paid'):
            return 'team'
    except Exception:
        return None
    return None


def _utc_today():
    return time.strftime('%Y-%m-%d', time.gmtime())


def _next_utc_midnight(now=None):
    # UTC days are exact 86400s epoch boundaries, so the next UTC midnight is
    # floor(epoch / 86400) * 86400 + 86400 -- no local-time structs involved
    # (mixing time.mktime with a gmtime struct would apply the local offset
    # twice).
    now = now or time.time()
    return (now // 86400) * 86400 + 86400


def _freepool_minute_bucket(now=None):
    return int((now or time.time()) // 60)


def _or_free_min_score():
    """Score floor for the auto-picked OpenRouter-free model, on the band's
    own benchmark. Data check (2026-10, artificial-analysis indexes): the
    paid low pick (ling-3.1-flash) scores 41.1 on intelligence_index; the
    best free candidates score 25.0-26.5 and the zero-price catalog tail
    falls to 8.4. A floor of 20 keeps the coherent tier (aligned with the
    weakest pool endpoint, gemini-3.5-flash-lite at 22.2, and the sublow
    pick at 20.1) and drops models too weak for the band's 'follows basic
    instructions' bar (gemma-4 16.7, nemotron-lightning 12.9, gpt-oss-20b
    10.0, lfm-2.5 8.4). Overridable in model_providers.json ->
    free_pool.or_free_min_score."""
    try:
        value = (_free_pool_cfg() or {}).get('or_free_min_score')
        return 20.0 if value is None else float(value)
    except (TypeError, ValueError):
        return 20.0


def _freepool_openrouter_free_model(band):
    """Best-scored zero-price catalog model for the band's benchmark, with a
    ban list for models OpenRouter refuses to serve outside agentic harnesses
    (confirmed live: some :free models 403 with 'only available on agentic
    harnesses') and a score floor (_or_free_min_score) so the auto-pick can
    never serve a model weaker than the pool's weakest pinned endpoint.
    Reasoning-flavored ids are deprioritized: the tank's tight
    max_tokens budgets starve hidden thinkers into null content, and a null
    answer routes to the paid pick anyway."""
    with _free_pool_lock:
        chosen = _free_pool_state['or_free_chosen']
    if chosen:
        return chosen
    benchmark = ((BAND_BENCHMARK or {}).get(band) or ['intelligence_index'])[0]
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT c.model_id, b.score FROM model_catalog c '
                'JOIN model_benchmark_scores b ON b.model_id = c.model_id AND b.benchmark = ? '
                'WHERE CAST(c.price_per_m AS REAL) = 0 '
                'ORDER BY b.score DESC LIMIT 40', (benchmark,)).fetchall()
    except Exception:
        return None
    now = time.time()
    ranked = []
    floor = _or_free_min_score()
    for model_id, score in rows:
        if not model_id or ':batch' in model_id:
            continue
        if score is None or score < floor:
            continue
        low = model_id.lower()
        penalty = 1000.0 if any(w in low for w in ('reason', 'think', '-r1')) else 0.0
        ranked.append((score + penalty, model_id))
    ranked.sort(reverse=True)
    for _score, model_id in ranked:
        with _free_pool_lock:
            if now < (_free_pool_state['or_free_banned'].get(model_id) or 0.0):
                continue
        if is_model_circuit_broken(model_id):
            continue
        with _free_pool_lock:
            _free_pool_state['or_free_chosen'] = model_id
        return model_id
    return None


def _freepool_ready(ep, band):
    """True when this endpoint may take a call right now: not switched off by
    the global or team policy, key present, key healthy, circuit closed,
    cooldown spent, and every configured quota window (RPM/TPM by pool_key,
    RPD per endpoint) has room."""
    if _freepool_service_blocked(ep):
        return False
    if ep.get('openrouter_free'):
        if not _freepool_openrouter_free_model(band):
            return False
    else:
        if ep.get('api_key_env') and not (_load_env().get(ep['api_key_env']) or '').strip():
            return False
        if _api_key_is_broken(ep['service']):
            return False
    if is_model_circuit_broken(f'@freepool:{band}:{ep["name"]}'):
        return False
    now = time.time()
    with _free_pool_lock:
        if now < (_free_pool_state['cooldown'].get(ep['name']) or 0.0):
            return False
        minute = _freepool_minute_bucket(now)
        if ep['rpm']:
            rpm = _free_pool_state['rpm'].get(ep['pool_key']) or {}
            stamps = [t for t in (rpm.get('stamps') or []) if t > now - 60]
            if len(stamps) >= ep['rpm']:
                return False
        if ep['tpm']:
            tpm = _free_pool_state['tpm'].get(ep['pool_key']) or {}
            if tpm.get('minute') == minute and tpm.get('tokens', 0) >= ep['tpm']:
                return False
        rpd = _free_pool_state['rpd'].get(ep['name']) or {}
        if ep['rpd'] and rpd.get('date') == _utc_today() and rpd.get('count', 0) >= ep['rpd']:
            return False
    return True


def _freepool_route_for_band(band, force=False):
    """The band's current free route as a synthetic slug, or None to use the
    normal paid pick. `force` ignores the mode gate (used by the paid-failure
    fallback, which tries a free endpoint whenever one is healthy regardless
    of mode)."""
    try:
        cfg = _free_pool_cfg()
        if not cfg or band not in cfg['bands']:
            return None
        paid_blocked = _low_paid_blocked()
        if not force:
            if cfg['mode'] == 'paid-first' and not paid_blocked:
                # The operator wants paid first: the picker never routes to
                # the pool; the call layers' failure fallback does, via
                # force=True. With the low paid leg switched off there is no
                # paid alternative, so the pool serves.
                return None
            if cfg['mode'] == 'budget-only':
                if not (credits_pause_active() or _think_tank_spend_cap_exceeded()
                        or paid_blocked):
                    return None
        for ep in cfg['endpoints']:
            if _freepool_ready(ep, band):
                return f'@freepool:{band}:{ep["name"]}'
    except Exception:
        return None
    return None


def _freepool_provider_for(model):
    """Translate '@freepool:<band>:<endpoint>' into a concrete provider spec
    for the transport layer. Resolution is a pure name lookup -- quota health
    is the PICKER's job -- so every re-resolution within one call sees the
    same endpoint and the same concrete model. Unknown slugs return None."""
    try:
        if not isinstance(model, str) or not model.startswith('@freepool:'):
            return None
        parts = model.split(':', 2)
        if len(parts) != 3:
            return None
        band, name = parts[1], parts[2]
        cfg = _free_pool_cfg()
        ep = next((e for e in (cfg or {}).get('endpoints') or [] if e['name'] == name), None)
        if ep is None:
            return None
        model_slug = (_freepool_openrouter_free_model(band)
                      if ep.get('openrouter_free') else ep['model'])
        if not model_slug:
            return None
        return {
            'provider': 'freepool-' + ep['service'],
            'base_url': ep['base_url'],
            'model': model_slug,
            'api_key_env': ep['api_key_env'],
            'price_per_m': 0.0,
            'send_reasoning': False,
            'extra': ep.get('extra'),
            'is_custom': bool(ep['base_url']),
            'free': True,
            'openrouter_free': bool(ep.get('openrouter_free')),
            'service': ep['service'],
            'name': ep['name'],
            'band': band,
            'pool_key': ep['pool_key'],
        }
    except Exception:
        return None


def _freepool_slug_band(model):
    """The band encoded in a '@freepool:<band>:<endpoint>' slug, else None --
    lets tier-membership checks (e.g. the TTC sampling gate) recognize a
    freepool route as the band it is standing in for."""
    try:
        if isinstance(model, str) and model.startswith('@freepool:'):
            parts = model.split(':', 2)
            if len(parts) == 3:
                return parts[1]
    except Exception:
        pass
    return None


def _freepool_paid_fallback(model):
    """The band's real (paid) tier pick for a freepool slug -- the call
    layers' landing spot when a free attempt fails. None when the band has no
    configured pick."""
    band = _freepool_slug_band(model)
    if not band:
        return None
    try:
        return _model_tier_slug(band)
    except Exception:
        return None


def _note_free_pool_call(provider, result):
    """Record one successful free POST against the endpoint's quota windows.
    Fail-open: accounting must never break the call it is observing."""
    if not provider or not provider.get('free'):
        return
    try:
        usage = (result or {}).get('usage') if isinstance(result, dict) else None
        tokens = 0
        if isinstance(usage, dict):
            tokens = int(usage.get('total_tokens') or 0)
            if not tokens:
                tokens = int(usage.get('prompt_tokens') or 0) + int(usage.get('completion_tokens') or 0)
        now = time.time()
        minute = _freepool_minute_bucket(now)
        with _free_pool_lock:
            rpm = _free_pool_state['rpm'].setdefault(provider['pool_key'], {'minute': minute, 'stamps': []})
            if rpm.get('minute') != minute:
                rpm['minute'] = minute
                rpm['stamps'] = []
            rpm['stamps'].append(now)
            if tokens:
                tpm = _free_pool_state['tpm'].setdefault(provider['pool_key'], {'minute': minute, 'tokens': 0})
                if tpm.get('minute') != minute:
                    tpm['minute'] = minute
                    tpm['tokens'] = 0
                tpm['tokens'] += tokens
            today = _utc_today()
            rpd = _free_pool_state['rpd'].setdefault(provider['name'], {'date': today, 'count': 0})
            if rpd.get('date') != today:
                rpd['date'] = today
                rpd['count'] = 0
            rpd['count'] += 1
    except Exception:
        pass


def _note_free_pool_rate_limited(provider, detail='', retry_after=None):
    """A 429 from a free endpoint: cool it down so the picker routes around
    it. Retry-After is honored when the provider sends one; otherwise a
    default 60s (a per-minute bucket refill). A daily-limit response cools
    down until the next UTC midnight -- retrying a day-capped endpoint every
    minute would only burn the paid fallback's patience."""
    if not provider or not provider.get('free'):
        return
    try:
        detail = (detail or '').lower()
        now = time.time()
        cooldown = 60.0
        try:
            retry_after = float(retry_after) if retry_after else 0.0
        except (TypeError, ValueError):
            retry_after = 0.0
        if retry_after > 0:
            cooldown = min(retry_after, 3600.0)
        elif any(w in detail for w in ('per day', 'per-day', 'daily', 'day limit', 'requests per day', 'rpd')):
            cooldown = max(60.0, _next_utc_midnight(now) - now)
        with _free_pool_lock:
            _free_pool_state['cooldown'][provider['name']] = now + cooldown
    except Exception:
        pass


def _freepool_note_rejected(provider):
    """OpenRouter refused this :free model outright (403 'only available on
    agentic harnesses', 404 model gone): ban it for a day and clear the
    choice so the next pick takes the next-ranked candidate instead of
    retrying a model that can never work through plain chat completions."""
    if not provider or not provider.get('free') or not provider.get('openrouter_free'):
        return
    try:
        now = time.time()
        with _free_pool_lock:
            _free_pool_state['or_free_banned'][provider['model']] = now + 86400
            if _free_pool_state['or_free_chosen'] == provider['model']:
                _free_pool_state['or_free_chosen'] = None
    except Exception:
        pass


def _freepool_status():
    """Point-in-time quota view for the health surface: per endpoint, what the
    picker would decide right now and how full its windows are. Never
    raises."""
    out = {'enabled': False, 'mode': None, 'bands': [], 'endpoints': []}
    try:
        cfg = _free_pool_cfg()
        if not cfg:
            return out
        out.update({'enabled': True, 'mode': cfg['mode'], 'bands': list(cfg['bands']),
                    'paidBlocked': _low_paid_blocked()})
        now = time.time()
        for ep in cfg['endpoints']:
            with _free_pool_lock:
                rpm = _free_pool_state['rpm'].get(ep['pool_key']) or {}
                stamps = [t for t in (rpm.get('stamps') or []) if t > now - 60]
                tpm = _free_pool_state['tpm'].get(ep['pool_key']) or {}
                rpd = _free_pool_state['rpd'].get(ep['name']) or {}
                cooldown = _free_pool_state['cooldown'].get(ep['name']) or 0.0
            entry = {
                'name': ep['name'], 'service': ep['service'],
                'blocked': _freepool_service_blocked(ep),
                'rpm_used': len(stamps), 'rpm_limit': ep['rpm'],
                'tpm_used': tpm.get('tokens', 0) if tpm.get('minute') == _freepool_minute_bucket(now) else 0,
                'tpm_limit': ep['tpm'],
                'rpd_used': rpd.get('count', 0) if rpd.get('date') == _utc_today() else 0,
                'rpd_limit': ep['rpd'],
                'cooldown_s': max(0.0, round(cooldown - now, 1)),
            }
            try:
                entry['ready'] = _freepool_ready(ep, (cfg['bands'] or ['low'])[0])
            except Exception:
                entry['ready'] = None
            if ep.get('openrouter_free'):
                entry['chosen_model'] = _freepool_openrouter_free_model((cfg['bands'] or ['low'])[0])
            out['endpoints'].append(entry)
    except Exception:
        pass
    return out
