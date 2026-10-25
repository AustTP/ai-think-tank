# --- Teamwork agreements -----------------------------------------------------
# A team may set ANY of these keys on its own `agreement` record (state's
# teams[i]['agreement'], written through PUT /api/teams/{id}). Everything else
# in the operator policy is operator-only: teams can read it, never override
# it. Unset keys fall through team -> operator_policy.json -> code default, so
# an empty agreement behaves exactly like today.
TEAM_AGREEABLE_KEYS: frozenset = frozenset({
    # team shape
    'maxTeamMembers', 'scrumMasterMinTeamSize',
    # grading floors
    'deliverableGradeFloor', 'successGradeFloor',
    # governance cadences (team-scoped consumers only; tank-wide stamps
    # like skillReviewCadenceMs/hireCooldownMs stay operator-only)
    'refinementCadenceMs',
    # work items / backlog
    'workItemMaxAttempts', 'staleWorkTimeoutMs', 'staleWorkBudgetGraceS',
    'backlogPullsPerClose', 'escalationMaxOpen',
    # morale arithmetic
    'moraleApprovedWeight', 'moraleApprovedCap', 'moraleDroppedWeight',
    'moraleNeglectWeight', 'moraleNeglectCap',
    # delivery shape
    'maxReviewCycles', 'coachingMaxRounds', 'workerHelpAfterFails',
    'teamVelocityMax', 'workItemStallCeiling',
})

# Of the agreeable keys, these are CAPS: the operator value is a hard ceiling
# and the effective value is min(team, operator) at READ time. That keeps the
# operator's limit live (raising/lowering the file takes effect without a
# state edit) while the stored team value stays stable. All other agreeable
# keys are plain: the team's value wins outright.
TEAM_CEILING_KEYS: frozenset = frozenset({
    'maxTeamMembers', 'escalationMaxOpen', 'workItemMaxAttempts',
    'backlogPullsPerClose', 'moraleApprovedCap', 'moraleNeglectCap',
    'maxReviewCycles', 'coachingMaxRounds', 'workerHelpAfterFails',
    'teamVelocityMax', 'workItemStallCeiling',
})

# Agent-editable operator-policy keys (surface audit follow-up, 2026-10-18).
# The governed policy_edit tool may write ONLY these keys into
# operator_policy.json, and only after a JEV quorum review of each change.
# Everything else stays operator-only. Seeded with the Phase 3 migrated
# serve knobs the tank could plausibly tune itself; the DB/log hygiene keys
# stay player-only on purpose (they protect the audit trail's retention
# shape). The player keeps final authority either way: the JSON itself and
# POST /api/policy/reload are untouched, so any agent change can simply be
# reverted by editing the file.
# Keys that shape a safety or quality gate stay player-only. An agent may
# still propose them through an escalation, but not through policy_edit: a
# reviewed threshold change must not have less authority than a code edit to
# the same gate.
PLAYER_ONLY_POLICY_KEYS: frozenset = frozenset({
    'tierGateMinConfidence',
})
AGENT_EDITABLE_POLICY_KEYS: frozenset = frozenset({
    'modelTierRefreshIntervalS', 'tierPriceWatchIntervalS',
    'tierRecheckCooldownS',
    'failureMaxRecords', 'successMaxRecords', 'ruleProposalsMax',
    'successProposalsMax', 'ruleMinRecurrence', 'successMinRecurrence',
    'memoryMaxAgeDays', 'memoryDecayFactor', 'memoryKeepCount',
    'maxDownloadBytes', 'youtubeTranscriptMaxChars',
    'youtubeTranscriptMaxSegments', 'agentDirSyncIntervalS',
    'peerReviewMinLookbackS', 'peerReviewSignalStaleS',
})

# Loaded values (module-level dict, MUTATED IN PLACE by reload so consumers
# that did `from config import OPERATOR_POLICY` see reloads immediately).
OPERATOR_POLICY: dict = dict(OPERATOR_POLICY_DEFAULT)

_OPERATOR_CACHE = {'mtime': None, 'data': None}

# Consumer refresh hooks. Operator-policy scalars now bind declaratively via
# bind_policy()/POLICY_BINDINGS below, so sim/serve/content/sublow no longer
# register refresh callbacks here. This list stays for ad-hoc zero-arg
# callbacks (tests, future consumers): run at the end of reload_policy_config
# (import time + the player-session POST /api/policy/reload endpoint).
# Exceptions are swallowed per-hook: one bad consumer refresh must not abort
# the others' reload.
ON_RELOAD_HOOKS: list = []

# Declarative operator-policy bindings: module name -> {'pairs':
# ((module_attribute, policy_key), ...), 'overrides': {attr: fn(policy)}}.
# Modules register once at import via bind_policy(); every
# reload_policy_config() run re-applies every registered binding, so operator
# edits take effect without a restart and each module owns exactly one
# binding declaration instead of its own rebind function.
POLICY_BINDINGS: dict = {}


def _apply_binding(module_name):
    module = sys.modules.get(module_name)
    if module is None:
        return False
    entry = POLICY_BINDINGS[module_name]
    policy = get_operator_policy()
    overrides = entry['overrides']
    for attr, key in entry['pairs']:
        override = overrides.get(attr)
        setattr(module, attr, override(policy) if override else policy[key])
    return True


def apply_policy_bindings(module_name=None):
    """(Re)apply registered operator-policy bindings. module_name=None
    re-applies every registered module. Returns the module names that were
    bound. Used by reload_policy_config, by modules that need a second
    import-time rebind after later constants are defined, and by tests that
    patch get_operator_policy for a deterministic rebind."""
    names = [module_name] if module_name else list(POLICY_BINDINGS)
    return [name for name in names if _apply_binding(name)]


def bind_policy(module_name, pairs, overrides=None):
    """Register one module's operator-policy bindings and apply them now.

    pairs: iterable of (module_attribute, policy_key). overrides: optional
    mapping of module_attribute -> callable(policy_dict) for keys derived
    from env at import time (the callable re-reads the module's live globals
    each time it runs, so patching/env reloads stay honored). Registration
    must run at module import so a later reload_policy_config() re-applies
    the bindings without a restart."""
    entry = POLICY_BINDINGS.get(module_name) or {'pairs': (), 'overrides': {}}
    merged_pairs = list(entry['pairs'])
    known = {attr for attr, _key in merged_pairs}
    for attr, key in pairs:
        if attr in known:
            merged_pairs = [(a, k) for a, k in merged_pairs if a != attr]
        merged_pairs.append((attr, key))
        known.add(attr)
    merged_overrides = dict(entry['overrides'])
    merged_overrides.update(overrides or {})
    POLICY_BINDINGS[module_name] = {
        'pairs': tuple(merged_pairs),
        'overrides': merged_overrides,
    }
    _apply_binding(module_name)


def _coerce_operator_value(key, value):
    """Validate one operator-policy field against _OPERATOR_SCHEMA. Returns the
    coerced value, or None when the key is unknown or the value is invalid
    (caller keeps the default for that key)."""
    spec = _OPERATOR_SCHEMA.get(key)
    if spec is None:
        return None
    kind, lo, hi = spec
    try:
        if kind == 'int':
            if isinstance(value, bool):
                return None
            out = int(value)
            if out != float(value):
                return None
        else:
            if isinstance(value, bool):
                return None
            out = float(value)
    except (TypeError, ValueError):
        return None
    if out < lo or out > hi:
        return None
    return out


def validate_team_agreement(raw, operator=None):
    """Validate a teamwork agreement against TEAM_AGREEABLE_KEYS + schema.

    Fail-closed per key, never per record: unknown keys, wrong types and
    out-of-range values are dropped, the rest is kept, and the input is never
    trusted (a non-dict yields {}). Ceilings are NOT applied here -- a team may
    store a value above the operator's cap and get min(team, operator) at read
    time, so a later operator edit stays live without a state rewrite.
    `operator` is the values to range-check against; defaults to the live
    operator policy."""
    if not isinstance(raw, dict):
        return {}
    if not isinstance(operator, dict):
        operator = get_operator_policy()
    out = {}
    for key, value in raw.items():
        if key not in TEAM_AGREEABLE_KEYS:
            continue
        coerced = _coerce_operator_value(key, value)
        if coerced is not None:
            out[key] = coerced
    return out


def _load_operator_policy(path):
    """Read operator_policy.json and return a fully-populated validated dict
    (every key in OPERATOR_POLICY_DEFAULT present). Missing/corrupt file ->
    the defaults. A wrong-typed or out-of-range field keeps the default for
    that field; unknown keys (including the shipped `_notes` doc block) are
    ignored."""
    data = dict(OPERATOR_POLICY_DEFAULT)
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data
    for key in data:
        coerced = _coerce_operator_value(key, raw.get(key))
        if coerced is not None:
            data[key] = coerced
    return data


def get_operator_policy():
    """Return the current operator_policy.json contents, re-reading the file
    whenever its mtime changes -- the same live-reload pattern as
    get_grants_config. Also refreshes the module-level OPERATOR_POLICY dict in
    place so `from config import OPERATOR_POLICY` consumers see the edit."""
    try:
        mtime = os.path.getmtime(OPERATOR_POLICY_PATH)
    except OSError:
        mtime = -1
    if _OPERATOR_CACHE['mtime'] != mtime:
        _OPERATOR_CACHE['mtime'] = mtime
        _OPERATOR_CACHE['data'] = _load_operator_policy(OPERATOR_POLICY_PATH)
        OPERATOR_POLICY.clear()
        OPERATOR_POLICY.update(_OPERATOR_CACHE['data'])
    return _OPERATOR_CACHE['data']


def op(key):
    """One operator-policy field: the validated live value, or the shipped
    default when the key is unknown. Consumers that need a scalar copy at
    import time should register an ON_RELOAD_HOOKS callback instead."""
    return get_operator_policy().get(key, OPERATOR_POLICY_DEFAULT.get(key))


def get_grants_config():
    """Return the current grants.json contents, re-reading the file whenever
    its mtime changes. This is what makes an operator edit take effect without
    restarting the server -- every grant check (fs.py) and tool-offer decision
    (serve.py/content.py) calls this, so adding an appGrant for Notes to the
    JSON flips the tank's access live."""
    try:
        mtime = os.path.getmtime(GRANTS_PATH)
    except OSError:
        mtime = -1
    if _GRANTS_CACHE['mtime'] != mtime:
        _GRANTS_CACHE['mtime'] = mtime
        _GRANTS_CACHE['data'] = _load_grants(GRANTS_PATH)
    return _GRANTS_CACHE['data']


def _load_credential_access(path):
    """Validate the per-service read-only login config. Fail-closed: a bad
    entry is dropped, and a bad file falls back to the built-in defaults."""
    out = {'autoGrant': {}, 'readOnlyHosts': []}
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # Fail closed: a missing or corrupt switchboard grants nothing. The
        # operator must put the services back explicitly.
        return out
    if not isinstance(data, dict):
        return out
    hosts = data.get('readOnlyHosts')
    if isinstance(hosts, list):
        out['readOnlyHosts'] = [str(h).strip().lower()
                                for h in hosts if str(h).strip()]
    auto = data.get('autoGrant')
    if isinstance(auto, dict):
        for service, spec in auto.items():
            name = str(service).strip().lower()
            if not name or not isinstance(spec, dict):
                continue
            credential = str(spec.get('credential') or name).strip()
            spec_hosts = spec.get('hosts')
            if not isinstance(spec_hosts, list):
                continue
            clean_hosts = [str(h).strip().lower() for h in spec_hosts
                           if str(h).strip()]
            if not credential or not clean_hosts:
                continue
            methods = spec.get('methods')
            clean_methods = ([str(m).strip().upper() for m in methods
                              if str(m).strip()]
                             if isinstance(methods, list) else ['POST'])
            try:
                ttl = max(60, min(int(spec.get('ttlSec') or 900), 3600))
            except (TypeError, ValueError):
                ttl = 900
            out['autoGrant'][name] = {
                'credential': credential,
                'hosts': clean_hosts,
                'methods': clean_methods or ['POST'],
                'ttlSec': ttl,
            }
    if not out['readOnlyHosts']:
        out['readOnlyHosts'] = list(CREDENTIAL_ACCESS_DEFAULT['readOnlyHosts'])
    # An empty autoGrant is a deliberate revocation, not a cue to restore the
    # shipped Facebook/Instagram defaults.
    return out


def get_credential_access_config():
    """Current credential_access.json, re-read on mtime. This is the operator
    switchboard for need-based read-only logins: add a service to autoGrant to
    let that service be requested, remove it to revoke the path. The file holds
    no secrets -- only credential names, hosts, methods, and TTLs."""
    try:
        mtime = os.path.getmtime(CREDENTIAL_ACCESS_PATH)
    except OSError:
        mtime = -1
    if _CREDENTIAL_ACCESS_CACHE['mtime'] != mtime:
        _CREDENTIAL_ACCESS_CACHE['mtime'] = mtime
        _CREDENTIAL_ACCESS_CACHE['data'] = _load_credential_access(
            CREDENTIAL_ACCESS_PATH)
    return _CREDENTIAL_ACCESS_CACHE['data']


def get_research_watchlist():
    """Return the current watchlist.json topics, re-reading the file whenever
    its mtime changes -- the same live-reload pattern as get_grants_config, so
    an operator edit applies without restarting the server. Returns
    (topics, ok): `ok` is False when the file is missing or unparseable, so a
    live sync can refuse to wipe the standing schedule on a transient bad read
    instead of deleting everything. Also refreshes the module-level
    RESEARCH_WATCHLIST on change so config consumers see the edit live."""
    try:
        mtime = os.path.getmtime(WATCHLIST_PATH)
    except OSError:
        mtime = -1
    if _WATCHLIST_CACHE['mtime'] != mtime:
        _WATCHLIST_CACHE['mtime'] = mtime
        try:
            with open(WATCHLIST_PATH, encoding='utf-8') as f:
                json.load(f)  # parseability check: tells an empty list from a corrupt file
        except Exception:
            _WATCHLIST_CACHE['ok'] = False
            return list(_WATCHLIST_CACHE['data']), False
        _WATCHLIST_CACHE['data'] = _load_watchlist(WATCHLIST_PATH)
        _WATCHLIST_CACHE['ok'] = True
        RESEARCH_WATCHLIST[:] = [dict(t) for t in _WATCHLIST_CACHE['data']]
    return list(_WATCHLIST_CACHE['data']), _WATCHLIST_CACHE['ok']


def reload_policy_config():
    """Re-read all policy files. Called once at import and available for
    tests and for an operator who edits a file without restarting the server.

    The module-level lists are MUTATED in place, never rebound: modules
    that did `from config import PLAIN_WRITING_BAN_LIST` (serve.py, content.py)
    hold a reference to the same list objects, so an in-place update is visible
    to every consumer immediately -- a reload takes effect without a restart
    and without anyone re-importing. GRANTS_CONFIG follows the same rule; it is
    also re-read live on every grant check by get_grants_config()."""
    global _WATCHLIST_LOAD_ERROR
    PLAIN_WRITING_BAN_LIST[:] = _load_list(PLAIN_WRITING_PATH, PLAIN_WRITING_BAN_LIST_DEFAULT, key='banned')
    BROWSE_BLOCK_CATEGORIES[:] = _load_list(BROWSE_POLICY_PATH, BROWSE_BLOCK_CATEGORIES_DEFAULT, key='block_categories')
    STANDING_SAFE_DOMAINS.clear()
    STANDING_SAFE_DOMAINS.update(_load_standing_safe_domains(BROWSE_POLICY_PATH))
    TREG_ENDPOINT_COSTS.clear()
    TREG_ENDPOINT_COSTS.update(_load_treg_endpoint_costs(SERVICE_COSTS_PATH))
    _model_bands = _load_model_bands(MODEL_BANDS_PATH)
    MODEL_PRICE_BANDS.clear(); MODEL_PRICE_BANDS.update(_model_bands['price_bands'])
    MODEL_BAND_PURPOSE.clear(); MODEL_BAND_PURPOSE.update(_model_bands['band_purpose'])
    BAND_BENCHMARK.clear(); BAND_BENCHMARK.update(_model_bands['band_benchmark'])
    BAND_QUALITY_FLOOR_GAP.clear(); BAND_QUALITY_FLOOR_GAP.update(_model_bands['quality_floor_gap'])
    TIER_GATE_CRITERIA.clear(); TIER_GATE_CRITERIA.update(_model_bands['tier_gate_criteria'])
    MODEL_BAND_ALIASES.clear(); MODEL_BAND_ALIASES.update(_model_bands.get('band_aliases') or {})
    _providers = _load_model_providers(MODEL_PROVIDERS_PATH)
    MODEL_PROVIDER_OVERRIDES.clear()
    MODEL_PROVIDER_OVERRIDES.update(_providers)
    SECRET_PATTERNS[:] = _load_secret_patterns(REDACTION_PATH)
    TOOL_DESCRIPTIONS.clear()
    TOOL_DESCRIPTIONS.update(_load_tool_descriptions(TOOL_DESCRIPTIONS_PATH))
    _endpoints = _load_endpoint_policy(ENDPOINT_POLICY_PATH)
    NON_WAKING_READS_EXTRA.clear(); NON_WAKING_READS_EXTRA.update(_endpoints['reads'])
    NON_WAKING_WRITES_EXTRA.clear(); NON_WAKING_WRITES_EXTRA.update(_endpoints['writes'])
    _rooms = _load_room_overrides(ROOMS_PATH)
    ROOM_DEFINITION_OVERRIDES.clear(); ROOM_DEFINITION_OVERRIDES.update(_rooms['definitions'])
    ROOM_DELEGATABLE_OVERRIDES[:] = _rooms['delegatable']
    _profiles = _load_seed_profiles(SEED_PROFILES_PATH)
    SEED_PROFILE_OVERRIDES.clear(); SEED_PROFILE_OVERRIDES.update(_profiles['profiles'])
    SEED_DEFAULT_PROFILE_OVERRIDE.clear()
    if _profiles['default']:
        SEED_DEFAULT_PROFILE_OVERRIDE.update(_profiles['default'])
    _actions = _load_action_taxonomy(ACTIONS_PATH)
    CEREMONY_ACTIONS[:] = _actions['ceremony']
    PROGRESS_ACTIONS[:] = _actions['progress']
    REWORK_ACTIONS[:] = _actions['rework']
    API_LOG_ALLOWED_ACTIONS.clear()
    API_LOG_ALLOWED_ACTIONS.update(_actions['api_log'])
    RESEARCH_DESK_PASSES[:] = _load_research_desk(RESEARCH_DESK_PATH)
    RESEARCH_WATCHLIST[:] = _load_watchlist(WATCHLIST_PATH)
    _grants = get_grants_config()
    GRANTS_CONFIG['appGrants'] = [dict(g) for g in _grants.get('appGrants') or []]
    GRANTS_CONFIG['fileGrants'] = [dict(g) for g in _grants.get('fileGrants') or []]
    _CREDENTIAL_ACCESS_CACHE['mtime'] = None
    CREDENTIAL_ACCESS_CONFIG.clear()
    CREDENTIAL_ACCESS_CONFIG.update(get_credential_access_config())
    # Operator policy: refresh the in-place dict, then notify consumers so
    # their scalar module constants (sim.MAX_TEAM_MEMBERS and friends) rebind.
    # Force the mtime check to re-run even if nothing changed on disk -- an
    # explicit reload must always re-derive from the current file.
    _OPERATOR_CACHE['mtime'] = None
    _op_data = get_operator_policy()
    apply_policy_bindings()
    for _hook in list(ON_RELOAD_HOOKS):
        try:
            _hook()
        except Exception:
            pass
    _WATCHLIST_LOAD_ERROR = None
    return {
        'plain_writing': list(PLAIN_WRITING_BAN_LIST),
        'browse_policy': list(BROWSE_BLOCK_CATEGORIES),
        'standing_safe_domains': sorted(STANDING_SAFE_DOMAINS),
        'service_costs': dict(TREG_ENDPOINT_COSTS),
        'model_bands': {'price_bands': dict(MODEL_PRICE_BANDS),
                        'band_purpose': dict(MODEL_BAND_PURPOSE),
                        'band_benchmark': dict(BAND_BENCHMARK),
                        'quality_floor_gap': dict(BAND_QUALITY_FLOOR_GAP),
                        'tier_gate_criteria': dict(TIER_GATE_CRITERIA),
                        'band_aliases': dict(MODEL_BAND_ALIASES)},
        'model_providers': {'default': dict(MODEL_PROVIDER_OVERRIDES.get('default') or {}),
                            'tiers': {k: dict(v) for k, v in (MODEL_PROVIDER_OVERRIDES.get('tiers') or {}).items()}},
        'secret_patterns': [p.pattern for p in SECRET_PATTERNS],
        'tool_descriptions': dict(TOOL_DESCRIPTIONS),
        'endpoint_policy': {'extra_non_waking_reads': sorted(NON_WAKING_READS_EXTRA),
                            'extra_non_waking_writes': sorted(NON_WAKING_WRITES_EXTRA)},
        'rooms': {'overrides': dict(ROOM_DEFINITION_OVERRIDES),
                  'delegatable': list(ROOM_DELEGATABLE_OVERRIDES)},
        'seed_profiles': {'roles': sorted(SEED_PROFILE_OVERRIDES),
                          'default_overridden': bool(SEED_DEFAULT_PROFILE_OVERRIDE)},
        'action_taxonomy': {'ceremony': list(CEREMONY_ACTIONS),
                            'progress': list(PROGRESS_ACTIONS),
                            'rework': list(REWORK_ACTIONS),
                            'api_log_allowed': sorted(API_LOG_ALLOWED_ACTIONS)},
        'research_desk': [dict(p) for p in RESEARCH_DESK_PASSES],
        'watchlist': [dict(t) for t in RESEARCH_WATCHLIST],
        'grants': {'appGrants': [dict(g) for g in GRANTS_CONFIG['appGrants']],
                   'fileGrants': [dict(g) for g in GRANTS_CONFIG['fileGrants']]},
        'operator': dict(_op_data),
    }


def remove_watchlist_topic(topic, start_url=''):
    """Drop a schedule from world/watchlist.json so the live sync does not add
    it back. Silent on failure: state removal still stands for this session."""
    if not ALLOW_WATCHLIST_WRITE:
        return False
    topic = (topic or '').strip()
    if not topic:
        return False
    try:
        with open(WATCHLIST_PATH, encoding='utf-8') as f:
            data = json.load(f)
        topics = data.get('topics')
        if not isinstance(topics, list):
            return False
        kept = [t for t in topics
                if not ((t or {}).get('topic') == topic
                        and (not start_url or (t or {}).get('startUrl') == start_url))]
        if len(kept) == len(topics):
            return False
        data['topics'] = kept
        with open(WATCHLIST_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        return True
    except Exception:
        return False


def update_watchlist_topic(old_topic, old_url, record):
    """Replace one watchlist entry in place after a schedule edit. Removes the
    old entry and appends the new one, so the live sync sees exactly one."""
    if not remove_watchlist_topic(old_topic, old_url):
        pass
    return note_watchlist_topic(record)


def note_watchlist_topic(record):
    """Append a villager-added research topic to world/watchlist.json so the
    operator sees (and can edit) what the village asked the desk to watch.

    Gated on ALLOW_WATCHLIST_WRITE (set only by the real server boot), and
    idempotent: a topic already present (by topic + startUrl) is not
    duplicated. Fully silent on any failure -- the topic already lives in
    state (researchTopics); the file is the operator-facing copy, never a
    source of truth that can block the request. Returns True when written,
    False when skipped (not enabled, already present, or write failed)."""
    if not ALLOW_WATCHLIST_WRITE:
        return False
    if not record or not isinstance(record, dict):
        return False
    topic = (record.get('topic') or '').strip()
    start_url = (record.get('startUrl') or '').strip()
    if not topic or not start_url:
        return False
    try:
        with open(WATCHLIST_PATH, encoding='utf-8') as f:
            data = json.load(f)
        topics = data.get('topics')
        if not isinstance(topics, list):
            topics = []
        if any((t or {}).get('topic') == topic and (t or {}).get('startUrl') == start_url
               for t in topics):
            return False
        entry = {'topic': topic, 'startUrl': start_url}
        if record.get('cadenceMs'):
            entry['cadenceMs'] = record['cadenceMs']
        if (record.get('linkKeyword') or '').strip():
            entry['linkKeyword'] = record['linkKeyword']
        if (record.get('pageKeyword') or '').strip():
            entry['pageKeyword'] = record['pageKeyword']
        if record.get('enabled') is False:
            entry['enabled'] = False
        topics.append(entry)
        data['topics'] = topics
        with open(WATCHLIST_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        return True
    except Exception:
        return False


reload_policy_config()
