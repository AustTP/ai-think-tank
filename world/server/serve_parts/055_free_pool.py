"""Free-provider pool: a quota-aware free-first layer in front of the paid
tier picks of the bands listed in model_providers.json's free_pool section.

Three free sources, each with its own quota reality (operator-researched):
  * Google AI Studio (Gemini free tier) -- quotas are PER MODEL: exhausting
    one model's limit never touches another's. Flash-class models are the
    usable workhorses (Pro-class is ~2 RPM / ~50 RPD and useless for volume).
    Gemini endpoints may set model 'auto': the concrete model is then
    gathered from the key's live models list (one read-only GET per key per
    hour) and determined per band -- sublow takes the weakest scored model
    holding the band floor, low the strongest. No Gemini model name is ever
    pinned in config.
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
import urllib.parse
import urllib.request

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
    # api_key_env -> {'at': ts, 'names': [model_id, ...]}: the chat-capable
    # models the gathered Google models list exposes for that key.
    'gem_models': {},
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
        service = str(ep.get('service') or '').strip() or ('openrouter' if is_or_free else name)
        # 'auto' (gemini service only, and an absent model counts as auto):
        # the concrete model is gathered from the key's models list and
        # determined per band at call time -- the free Gemini models are
        # never pinned in this file.
        is_gemini_auto = (service == 'gemini' and model in ('', 'auto'))
        # 'auto' is a gemini-only escape hatch: any other service naming it
        # is a config typo and must drop the endpoint, never redirect traffic.
        if model == 'auto' and not is_gemini_auto:
            continue
        if not is_or_free and not (base_url and (model or is_gemini_auto)):
            continue
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
            # Auto-gemini family filter: lowercase substrings a gathered
            # model name must contain to be a candidate. Defaults to the
            # operator-researched flash class ("Pro-class is ~2 RPM / ~50
            # RPD and useless for volume") -- the CLASS is the policy, the
            # model names inside it are always gathered live.
            'model_family': ([str(f).lower() for f in ep['model_family']]
                             if isinstance(ep.get('model_family'), list)
                             else (['flash'] if is_gemini_auto else [])),
            'pool_key': str(ep.get('pool_key') or '').strip() or name,
            'rpm': _limit('rpm'), 'rpd': _limit('rpd'), 'tpm': _limit('tpm'),
            # Per-model daily cap map (gemini auto picks): keys match model
            # names by substring, longest key wins (see _freepool_model_cap).
            'model_rpd': (ep['model_rpd'] if isinstance(ep.get('model_rpd'),
                                                        dict) else {}),
            # Scope whitelist (2026-10-30): an endpoint may restrict itself to
            # specific call kinds ('ask', ...) to protect a sensitive key's
            # free tier. Absent/empty = unrestricted. Non-list junk is dropped.
            'allowed_kinds': ([str(k) for k in ep['allowed_kinds']]
                              if isinstance(ep.get('allowed_kinds'), list) else []),
        })
    if not endpoints:
        return None
    try:
        min_score = pool.get('or_free_min_score')
        min_score = None if min_score is None else float(min_score)
    except (TypeError, ValueError):
        min_score = None
    budget_bands = [b for b in (pool.get('budget_bands') or [])
                    if isinstance(b, str) and b.strip()]
    return {'enabled': True, 'bands': [b.strip() for b in bands],
            'budget_bands': [b.strip() for b in budget_bands],
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

FREE_POOL_SERVICES = ('gemini', 'groq', 'openrouter', 'numes')
# The paid switch rides the same policy document and the same team/global
# scopes, but it is not a pool endpoint: it blocks the LOW band's paid leg
# (the DB pick or a @provider:low pin) so the free pool can be tested with
# no paid safety net underneath it. Mid/high/coding calls are untouched.
POLICY_SERVICES = FREE_POOL_SERVICES + ('paid',)

_FREE_POOL_ACTOR_TEAM: contextvars.ContextVar = contextvars.ContextVar(
    'free_pool_actor_team', default=None)
_FREE_POOL_ACTOR_AGENT: contextvars.ContextVar = contextvars.ContextVar(
    'free_pool_actor_agent', default=None)
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
            # Root-director attribution: a worker under a subdirector still
            # spends against the TEAM's budget -- walk the reporting chain up
            # instead of trusting one `director` hop.
            mapping = governance.team_director_map(state)
        except Exception:
            mapping = {}
        with _free_pool_lock:
            _FREE_POOL_TEAM_MAP['map'] = mapping
            _FREE_POOL_TEAM_MAP['at'] = now
    with _free_pool_lock:
        return _FREE_POOL_TEAM_MAP['map'].get(agent_id)


def _set_free_pool_actor(agent_id):
    """Stamp the acting agent's identity and team into the ambient slots.
    Called at the top of a work-dispatch worker thread (sim 030) so every
    free-pool pick made while that agent's work runs is attributed to the
    agent's team, and scale-tier decisions (070) can see WHO is acting."""
    try:
        _FREE_POOL_ACTOR_TEAM.set(_free_pool_team_of(agent_id))
    except Exception:
        pass
    try:
        _FREE_POOL_ACTOR_AGENT.set(agent_id)
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


# Markers that identify Gemini-list models that are NOT plain text chat
# workhorses (speech, image generation, robotics, research agents...). The
# free pool only serves text completion calls, so anything carrying one of
# these substrings is not a candidate.
_GEMINI_NON_CHAT_MARKERS = (
    'tts', 'image', 'transcribe', 'lyria', 'robotics', 'nano-banana',
    'computer-use', 'deep-research', 'antigravity', 'customtools',
    'embedding', 'aqa', 'live',
)
_GEMINI_MODELS_TTL_S = 3600.0
_GEMINI_MODELS_URL = ('https://generativelanguage.googleapis.com/v1beta/models')


def _freepool_gemini_models(ep):
    """The text-chat model ids the endpoint's Google API key can actually
    serve, gathered live from the models-list endpoint and cached per key for
    an hour. The pool's Gemini models are never hardcoded: the key decides
    what exists (the list only ever shows models the key can reach), and the
    per-band picker decides what serves. A failed gather keeps the
    last-known-good list; with no list at all the endpoint is simply not
    ready. One read-only GET per key per hour."""
    key_env = ep.get('api_key_env') or ''
    now = time.time()
    with _free_pool_lock:
        cached = _free_pool_state['gem_models'].get(key_env)
        if cached and now - cached['at'] < _GEMINI_MODELS_TTL_S:
            return cached['names']
    key = (_load_env().get(key_env) or '').strip() if key_env else ''
    if not key:
        return (cached or {}).get('names')
    try:
        url = (_GEMINI_MODELS_URL + '?key=' + urllib.parse.quote(key)
               + '&pageSize=1000')
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
        names = []
        for m in (data.get('models') or []):
            name = str(m.get('name') or '').split('models/', 1)[-1]
            if not name or 'generateContent' not in (m.get('supportedGenerationMethods') or []):
                continue
            low = name.lower()
            if any(w in low for w in _GEMINI_NON_CHAT_MARKERS):
                continue
            names.append(name)
    except Exception:
        return (cached or {}).get('names')
    with _free_pool_lock:
        _free_pool_state['gem_models'][key_env] = {'at': now, 'names': names}
    return names


def _freepool_gemini_candidates(band, ep):
    """The qualifying free Gemini models for one band on this endpoint, in
    the band's ROTATION ORDER. 'sublow' ranks weakest-scored first (its role
    is 'cheapest coherent', and it keeps the stronger models' daily quotas
    for the low band's real work); every other band ranks strongest-first
    (free is free -- quality is the only axis left). Both rules skip
    unscored models (a floored band cannot claim a threshold it has no
    measurement for) and reasoning-mandatory models (the pool's tight token
    budgets starve hidden thinkers into null content), mirroring the
    OpenRouter free leg's starvation rule. Candidates are also restricted to
    the endpoint's model_family classes (default ['flash']): the strongest
    gathered model is often a flagship whose free-tier quota is useless for
    volume -- the class restriction encodes that quota reality without
    pinning any model name."""
    names = _freepool_gemini_models(ep) or []
    if not names:
        return []
    families = [f for f in (ep.get('model_family') or []) if f]
    if families:
        names = [n for n in names if any(f in n.lower() for f in families)]
        if not names:
            return []
    benchmark = ((BAND_BENCHMARK or {}).get(band) or ['intelligence_index'])[0]
    floor = BAND_MIN_SCORE.get(band)
    if floor is None:
        floor = _or_free_min_score()
    try:
        placeholders = ','.join('?' * len(names))
        with _db() as conn:
            rows = conn.execute(
                'SELECT b.model_id, b.score FROM model_benchmark_scores b '
                'WHERE b.benchmark = ? AND b.model_id IN (' + placeholders + ')',
                [benchmark] + ['google/' + n for n in names]).fetchall()
    except Exception:
        return []
    scored = []
    for model_id, score in rows:
        if not model_id or ':batch' in model_id or score is None or score < floor:
            continue
        name = model_id.split('google/', 1)[-1]
        if any(w in name.lower() for w in ('reason', 'think', '-r1')):
            continue
        scored.append((float(score), name))
    if not scored:
        return []
    scored.sort()
    # Weakest-first for sublow, strongest-first for everything else.
    return [name for _score, name in scored] if band == 'sublow' \
        else [name for _score, name in reversed(scored)]


def _freepool_model_cap(ep, name):
    """The daily call cap for one model on this endpoint: the LONGEST entry
    in the endpoint's model_rpd map whose key occurs in the model name
    (longest wins so 'flash-lite' is not shadowed by 'flash'), else the
    endpoint-level rpd (0 = ride Google's true per-model free allowance via
    the daily-limit 429 marker). Unlisted models are always uncapped."""
    try:
        cap_map = ep.get('model_rpd') or {}
        if name and cap_map:
            for key in sorted(cap_map, key=len, reverse=True):
                if key and str(key) in str(name):
                    return int(cap_map[key])
        return int(ep.get('rpd') or 0)
    except Exception:
        return int(ep.get('rpd') or 0)


def _freepool_gemini_model(band, ep):
    """The free Gemini model a band should serve on this endpoint right now:
    the first candidate in the band's rotation order that still has
    per-model daily quota room. Google's free tier meters PER MODEL PER KEY,
    so exhausting one model's day never touches another's -- walking the
    ordered candidate list as each model's allowance fills multiplies the
    free calls a single key can serve in a day. The counter is keyed by
    (endpoint, model) and SHARED across bands: one key has one allowance
    per model, whoever spends it. A model is spent when its cap is reached
    (the endpoint's model_rpd map entry, falling back to the endpoint's rpd),
    or -- with no cap (ride Google's true per-model free allowance) -- once
    the daily-limit 429 marker is set. Returns None when every candidate is
    spent (the endpoint is then not ready and the pool rotates onward)."""
    for name in _freepool_gemini_candidates(band, ep):
        key = ep['name'] + '::' + name
        with _free_pool_lock:
            rpd = _free_pool_state['rpd'].get(key) or {}
            if rpd.get('date') == _utc_today():
                cap = _freepool_model_cap(ep, name)
                if rpd.get('exhausted') \
                        or (cap and rpd.get('count', 0) >= cap):
                    continue
        return name
    return None


def _freepool_ready(ep, band):
    """True when this endpoint may take a call right now: not switched off by
    the global or team policy, key present, key healthy, circuit closed,
    cooldown spent, and every configured quota window (RPM/TPM by pool_key,
    RPD per endpoint) has room."""
    if _freepool_service_blocked(ep):
        return False
    is_gemini_auto = (ep['service'] == 'gemini'
                      and (ep.get('model') or 'auto') == 'auto')
    if ep.get('openrouter_free'):
        if not _freepool_openrouter_free_model(band):
            return False
    elif is_gemini_auto:
        # Auto gemini endpoint: ready only when the per-band pick resolves.
        if not _freepool_gemini_model(band, ep):
            return False
    if ep.get('api_key_env') and not (_load_env().get(ep['api_key_env']) or '').strip():
        return False
    # Key health is tracked per KEY, not per service name: two endpoints may
    # share a service while carrying different keys (gemini-free vs
    # gemini-free-key3), and one key's quota failures must not take the
    # other key's endpoint down with it.
    if _api_key_is_broken(
            f"{ep['service']}:{ep['api_key_env']}" if ep.get('api_key_env')
            else ep['service']):
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
        # A provider-reported daily-limit 429 marks the endpoint's day as
        # exhausted even when no local cap is configured (the marker is how
        # the tank observes a published daily allowance it does not model).
        # Retrying until midnight would only burn calls: skip for the day.
        if rpd.get('date') == _utc_today() and rpd.get('exhausted'):
            return False
        if ep['rpd'] and rpd.get('date') == _utc_today() and rpd.get('count', 0) >= ep['rpd']:
            return False
    return True


_FREE_POOL_KIND: contextvars.ContextVar = contextvars.ContextVar(
    'free_pool_kind', default=None)


def _set_free_pool_kind(kind):
    """Stamp the ambient call KIND for free-pool scope enforcement ('ask' =
    the player-facing ask lane; unset/None means routine agent work). Set at
    the top of an async request task or a worker thread; the pickers read it
    to honor endpoint `allowed_kinds` scopes (2026-10-30: the operator's
    second Gemini key is scoped to the ask lane only)."""
    try:
        _FREE_POOL_KIND.set(str(kind) if kind else None)
    except Exception:
        pass


def _free_pool_active_kind():
    """The ambient kind, defaulting to 'work': unset context ALWAYS means
    routine agent traffic -- the highest-volume class -- so a scoped endpoint
    is invisible to any path that never opted in."""
    try:
        return _FREE_POOL_KIND.get() or 'work'
    except Exception:
        return 'work'


def _freepool_kind_allows(ep, kind):
    """An endpoint's `allowed_kinds` scope: absent/empty means unrestricted
    (every existing key stays any-purpose); present means the endpoint only
    serves the listed kinds. Unknown kinds never match a scope."""
    allowed = ep.get('allowed_kinds')
    if not allowed:
        return True
    return kind in allowed


def _freepool_route_for_band(band, force=False):
    """The band's current free route as a synthetic slug, or None to use the
    normal paid pick. `force` ignores the mode gate (used by the paid-failure
    fallback, which tries a free endpoint whenever one is healthy regardless
    of mode)."""
    try:
        cfg = _free_pool_cfg()
        if not cfg or band not in cfg['bands']:
            return None
        kind = _free_pool_active_kind()
        paid_blocked = _low_paid_blocked()
        budget_bands = set(cfg.get('budget_bands') or [])
        if not force and band in budget_bands:
            # A budget band never rides the pool on its own merits: the pool
            # engages for it only when the paid leg is actually unavailable
            # (credits paused, spend cap hit, or the paid leg blocked). The
            # paid-failure fallback below still reaches the pool via
            # force=True. 2026-11-10: putting 'mid' in plain `bands` made
            # every mid call free-first even with credits healthy -- the
            # operator wants paid mid when credits exist, free mid when they
            # don't.
            if not (credits_pause_active() or _think_tank_spend_cap_exceeded()
                    or paid_blocked):
                return None
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
            if not _freepool_kind_allows(ep, kind):
                continue
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
        pinned = (ep.get('model') or '').strip()
        if ep.get('openrouter_free'):
            model_slug = _freepool_openrouter_free_model(band)
        elif ep['service'] == 'gemini' and pinned in ('', 'auto'):
            # Auto gemini: the model is gathered from the key's live models
            # list and determined per band, never a pinned name.
            model_slug = _freepool_gemini_model(band, ep)
        else:
            model_slug = pinned
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
            # Auto-gemini endpoints meter their daily quota PER MODEL (one
            # key = one allowance per model, shared across bands), so the
            # RPD counter key carries the concrete model slug.
            'auto_model': (ep['service'] == 'gemini' and model_slug is not None
                           and (ep.get('model') or 'auto') == 'auto'),
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
            rpd = _free_pool_state['rpd'].setdefault(
                _freepool_rpd_key(provider), {'date': today, 'count': 0})
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
            # Auto-gemini meters per model: a daily-limit 429 spends THAT
            # model's day and the rotation takes the next candidate, instead
            # of idling the whole endpoint until UTC midnight.
            if _freepool_exhaust_model_day(provider):
                return
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


def _freepool_rpd_key(provider):
    """The daily-counter key for one free call: the endpoint name, or
    'name::model' for auto-gemini endpoints whose Google quota is per model
    per key. Must agree with the picker's rotation check exactly."""
    if not provider:
        return ''
    if provider.get('auto_model') and provider.get('model'):
        return provider['name'] + '::' + provider['model']
    return provider.get('name') or ''


def _freepool_exhaust_model_day(provider):
    """Mark ONE auto-gemini model's daily allowance as spent (the provider
    answered with a daily-limit 429). Retrying that model today would only
    burn calls; the picker's rotation walk skips a fully-spent model and
    takes the next candidate, and the endpoint itself stays ready. The
    marker is the 'exhausted' flag on the per-model counter -- it must skip
    models even when no cap is configured (riding Google's true per-model
    free allowance, where no local cap exists to trip). The cap lookup must
    agree with the picker's rotation check exactly (the endpoint's model_rpd
    entry for this model, falling back to the endpoint's rpd)."""
    key = _freepool_rpd_key(provider)
    if not key or key == provider.get('name'):
        return False
    cap = None
    try:
        cfg = _free_pool_cfg()
        ep = next((e for e in (cfg or {}).get('endpoints') or []
                   if e['name'] == provider.get('name')), None)
        cap = _freepool_model_cap(ep, provider.get('model'))
    except Exception:
        cap = None
    try:
        today = _utc_today()
        with _free_pool_lock:
            rpd = _free_pool_state['rpd'].setdefault(key, {'date': today, 'count': 0})
            if rpd.get('date') != today:
                rpd['date'] = today
                rpd['count'] = 0
            if cap:
                rpd['count'] = max(rpd['count'], cap)
            rpd['exhausted'] = True
        return True
    except Exception:
        return False


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
                    'budget_bands': list(cfg.get('budget_bands') or []),
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
            first_band = (cfg['bands'] or ['low'])[0]
            if ep.get('openrouter_free'):
                entry['chosen_model'] = _freepool_openrouter_free_model(first_band)
            elif ep['service'] == 'gemini' and (ep.get('model') or 'auto') == 'auto':
                entry['chosen_model'] = _freepool_gemini_model(first_band, ep)
                # The rotation's daily counter is per model; report the pick's.
                if entry['chosen_model']:
                    with _free_pool_lock:
                        rpd = _free_pool_state['rpd'].get(
                            ep['name'] + '::' + entry['chosen_model']) or {}
                    entry['rpd_used'] = rpd.get('count', 0) \
                        if rpd.get('date') == _utc_today() else 0
                    entry['rpd_exhausted'] = bool(
                        rpd.get('date') == _utc_today() and rpd.get('exhausted'))
                    entry['rotation'] = _freepool_gemini_candidates(first_band, ep)
            out['endpoints'].append(entry)
    except Exception:
        pass
    return out
