"""world/config.py -- operator-edited policy JSON loaded from world/*.json.

These settings are DATA, not code. The operator edits the JSON files beside
this module to tune the village without touching Python:

- plain_writing.json    -> PLAIN_WRITING_BAN_LIST      (serve._apply_plain_writing)
- browse_policy.json    -> BROWSE_BLOCK_CATEGORIES      (the browse/execute/save Jev gate)
- research_desk.json    -> RESEARCH_DESK_PASSES         (the desk's multi-pass questions)
- watchlist.json        -> RESEARCH_WATCHLIST           (standing research topics; the seed)
- grants.json           -> GRANTS_CONFIG                (operator-granted app/file access)
- credential_access.json -> CREDENTIAL_ACCESS_CONFIG   (per-service need-based read-only logins)
- browse_policy.json    -> STANDING_SAFE_DOMAINS        (read-only reference hosts, in the same file)
- service_costs.json    -> TREG_ENDPOINT_COSTS          (per-endpoint prices for metered APIs)
- model_bands.json      -> MODEL_PRICE_BANDS, MODEL_BAND_PURPOSE, BAND_BENCHMARK,
                           BAND_QUALITY_FLOOR_GAP, TIER_GATE_CRITERIA
- redaction.json        -> SECRET_PATTERNS               (credential shapes scrubbed from logs)
- tool_descriptions.json -> TOOL_DESCRIPTIONS            (per-tool description overrides)
- actions.json          -> CEREMONY_ACTIONS, PROGRESS_ACTIONS, REWORK_ACTIONS,
                           API_LOG_ALLOWED_ACTIONS       (action-name taxonomies)
- endpoint_policy.json  -> NON_WAKING_READS_EXTRA, NON_WAKING_WRITES_EXTRA
                           (endpoints that stay silent while the tank sleeps)
- rooms.json            -> ROOM_DEFINITION_OVERRIDES, ROOM_DELEGATABLE_OVERRIDES
- seed_profiles.json    -> SEED_PROFILE_OVERRIDES, SEED_DEFAULT_PROFILE_OVERRIDE

The two Research Desk files are deliberately agent-visible: the passes are the
standing questions every research run answers, and the watchlist is the list
of sources the desk watches. Agents extend the watchlist through the intent
lane; serve appends those additions back to watchlist.json (write-back), so
the operator sees and edits the village's additions in the same file.

grants.json is the operator's own switchboard for agent access, edited without
touching the API: an entry in `appGrants` (a bundle id, e.g. com.apple.Notes)
enables the `app_script` tool against that app; an entry in `fileGrants` (an
absolute folder scope) enables scoped file/`read_store` access there. Entries
present = granted, removed = revoked; edits apply without restarting the
server. The /api/file-grants and /api/app-grants endpoints grant the same
things into live state for a player session.

Load discipline, matching world/api_services.json:
- Every loader is fail-tolerant and fail-closed: a missing or malformed file
  falls back to the built-in default so the server always boots, and each
  value is validated to its expected shape (a wrong-shaped field keeps the
  default for that field rather than crashing boot).
- Loaded once at import. Call reload_policy_config() to re-read after the
  operator edits a file (or in tests).
- Write-back to watchlist.json is gated on ALLOW_WATCHLIST_WRITE, which the
  real server boot path (serve._lifespan) turns on. Tests never run lifespan,
  so a test or a stray import can never silently edit operator-owned JSON.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

PLAIN_WRITING_PATH = os.path.join(ROOT, 'plain_writing.json')
BROWSE_POLICY_PATH = os.path.join(ROOT, 'browse_policy.json')
RESEARCH_DESK_PATH = os.path.join(ROOT, 'research_desk.json')
WATCHLIST_PATH = os.path.join(ROOT, 'watchlist.json')
GRANTS_PATH = os.path.join(ROOT, 'grants.json')
CREDENTIAL_ACCESS_PATH = os.path.join(ROOT, 'credential_access.json')

# Fail-closed defaults, kept in code so a fresh clone (or a deleted/corrupt
# JSON file) boots with a working policy. The shipped JSON files carry the
# SAME values; the operator edits the JSON, not these.
PLAIN_WRITING_BAN_LIST_DEFAULT = (
    'leverage', 'utilize', 'seamless', 'robust', 'crucial', 'ensure',
    'delve', 'dive into', 'unpack', 'harness', 'foster', 'elevate',
    'underscore', 'illuminate', 'showcase', 'empower', 'innovative',
    'transformative', 'cutting-edge', 'state-of-the-art', 'significant',
    'notably', 'moreover', 'furthermore', 'additionally', 'overall',
    'essentially', 'fundamentally', 'ultimately', 'comprehensive',
    'it is worth noting', 'needless to say', 'in conclusion', 'in summary',
    'that being said', 'having said that', 'when it comes to', 'at its core',
    'play a role', 'serve as', 'act as', 'allow for', 'facilitate',
    'potentially', 'effectively', 'efficiently', 'appropriately',
)

BROWSE_BLOCK_CATEGORIES_DEFAULT = (
    'child sexual abuse material or content sexualizing minors in any way',
    'illegal drug or weapons marketplaces, or instructions for making weapons/explosives',
    'hacking, malware, or exploit distribution, or unauthorized-access instructions',
    'doxxing, stolen personal data, or non-consensual intimate imagery',
    'human trafficking or exploitation',
    'fraud, scams, or phishing',
    'terrorism or violent extremist content',
    'pirated copyrighted media distribution',
)

RESEARCH_DESK_PASSES_DEFAULT = [
    {'key': 'announcements',
     'question': 'What did the watched sources announce or change in the search window?'},
    {'key': 'practical_examples',
     'question': 'Which items show someone using a relevant feature, with enough detail or '
                 'linked material to inspect the example?'},
    {'key': 'limitations',
     'question': 'Are there documented restrictions, corrections, availability details, or '
                 'unresolved problems with those findings?'},
]

RESEARCH_WATCHLIST_DEFAULT: list = []

# Write-back gate. False by default; serve._lifespan sets it True on real boot.
ALLOW_WATCHLIST_WRITE = False

# Story worktrees branch from the shared sandbox base repo. Syncing that base
# to the live project's committed HEAD is a boot-only behavior (tests must
# never reset their own fixtures). Set by the server's lifespan.
ALLOW_WORKSPACE_SYNC = False

# Loaded values (module-level, replaced by reload_policy_config).
PLAIN_WRITING_BAN_LIST = list(PLAIN_WRITING_BAN_LIST_DEFAULT)
BROWSE_BLOCK_CATEGORIES = list(BROWSE_BLOCK_CATEGORIES_DEFAULT)
RESEARCH_DESK_PASSES = list(RESEARCH_DESK_PASSES_DEFAULT)
RESEARCH_WATCHLIST = list(RESEARCH_WATCHLIST_DEFAULT)

# Operator-edited grants (world/grants.json). Same shape as the state grants
# the /api endpoints manage, but authored by editing the JSON: `appGrants` is a
# list of {id, bundleId, label, caps:{use}} entries and `fileGrants` is a list
# of {id, scope, label, caps:{read,write,delete}} entries. Missing/invalid
# entries are dropped (fail-closed); an absent or corrupt file grants nothing.
GRANTS_DEFAULT: dict = {'appGrants': [], 'fileGrants': []}
GRANTS_CONFIG: dict = {'appGrants': [], 'fileGrants': []}

# Per-service, need-based read-only logins (world/credential_access.json).
# An agent cannot enumerate the vault. It asks for ONE service by name while it
# is working an active task, and the server mints a short-lived handle scoped
# to that service's hosts and POST (the login form) only. `readOnlyHosts` is
# the hard browser policy: social hosts refuse fill/click/submit; the only
# write-shaped op is the login submit on an auth path.
CREDENTIAL_ACCESS_DEFAULT: dict = {
    'autoGrant': {
        'facebook': {
            'credential': 'facebook',
            'hosts': ['facebook.com', 'www.facebook.com', 'm.facebook.com',
                      'mbasic.facebook.com'],
            'methods': ['POST'],
            'ttlSec': 900,
        },
        'instagram': {
            'credential': 'instagram',
            'hosts': ['instagram.com', 'www.instagram.com'],
            'methods': ['POST'],
            'ttlSec': 900,
        },
    },
    'readOnlyHosts': [
        'facebook.com', 'www.facebook.com', 'm.facebook.com', 'mbasic.facebook.com',
        'instagram.com', 'www.instagram.com',
    ],
}
CREDENTIAL_ACCESS_CONFIG: dict = {
    'autoGrant': {k: dict(v) for k, v in CREDENTIAL_ACCESS_DEFAULT['autoGrant'].items()},
    'readOnlyHosts': list(CREDENTIAL_ACCESS_DEFAULT['readOnlyHosts']),
}

SERVICE_COSTS_PATH = os.path.join(ROOT, 'service_costs.json')
MODEL_BANDS_PATH = os.path.join(ROOT, 'model_bands.json')
REDACTION_PATH = os.path.join(ROOT, 'redaction.json')
TOOL_DESCRIPTIONS_PATH = os.path.join(ROOT, 'tool_descriptions.json')
ACTIONS_PATH = os.path.join(ROOT, 'actions.json')
ENDPOINT_POLICY_PATH = os.path.join(ROOT, 'endpoint_policy.json')
ROOMS_PATH = os.path.join(ROOT, 'rooms.json')
SEED_PROFILES_PATH = os.path.join(ROOT, 'seed_profiles.json')

# Curated READ-ONLY public-reference hosts (browse_policy.json
# `standing_safe_domains`). These skip the Jev classify round trip on read
# paths but never join the write-capable allowlist; see serve._is_standing_safe_host.
STANDING_SAFE_DOMAINS_DEFAULT = (
    'wikipedia.org', 'wikimedia.org', 'wiktionary.org', 'wikidata.org',
    'arxiv.org', 'biorxiv.org', 'medrxiv.org', 'plos.org',
    'developer.mozilla.org', 'docs.python.org', 'docs.rs', 'learn.microsoft.com',
    'developer.apple.com', 'go.dev',
    'bbc.com', 'bbc.co.uk', 'reuters.com', 'apnews.com', 'npr.org', 'theguardian.com',
    'gov', 'edu',
)
STANDING_SAFE_DOMAINS: set = set(STANDING_SAFE_DOMAINS_DEFAULT)

# Per-endpoint prices for metered APIs (service_costs.json). A new endpoint on a
# metered service must be priced here or the chokepoint cannot accrue it.
TREG_ENDPOINT_COSTS_DEFAULT = {
    'x.x.get-trends-by-woeid': 0.01,
    'scrapecreators.x.v1-linkedin-search-posts': 0.00188,
}
TREG_ENDPOINT_COSTS: dict = dict(TREG_ENDPOINT_COSTS_DEFAULT)

# Model tier facts (model_bands.json): price bands, the job each band does, the
# benchmark chain backing each pick, the quality-floor gap that lets price
# break a tie, and the Jev criteria used by the tier gate. External facts
# (prices, benchmark names) drift; they live here so the village can re-tune
# without a kernel edit.
MODEL_PRICE_BANDS_DEFAULT = {
    'low': (0, 0.5),
    'mid': (0.5, 5),
    'high': (5, 50),
}
MODEL_BAND_PURPOSE_DEFAULT = {
    'low': 'Routine, high-volume work: short in-character replies, simple task narration. Needs to be cheap above all else, but still coherent -- not a model too weak to follow basic instructions.',
    'sublow': 'Short, bounded test scenarios run at scale (hundreds of reenactment/testing agents): brief in-character replies, throwaway probes, simple narration. The cheapest model that is still coherent -- hard per-model price ceiling, coherence is the bar, not depth. Never for real deliverables or long-running narrative work, where the coherence loss shows.',
    'mid': "Judgment calls: classifying report severity, generating a new hire's onboarding file, deciding what belongs in an agent's own notes. Needs real reasoning, moderate cost is fine.",
    'coding': 'Writing and reviewing real code (Code Reviewer, and any role that writes code). Correctness of generated code is the whole job here.',
    'high': 'Breaking a large, vague request into as many concrete, well-scoped subtasks as the work actually requires (never a fixed count -- a small ask may be one card, a sprawling one many) and deciding how work is distributed (assignBigTask). Runs once per real request, and every downstream call depends on this one being right -- a bad decomposition wastes everything after it, so this is the band to spend on.',
}
BAND_BENCHMARK_DEFAULT = {
    'low': ('intelligence_index', 'MMLU-Pro'),
    'sublow': ('intelligence_index', 'MMLU-Pro'),
    'mid': ('intelligence_index', 'MMLU-Pro'),
    'high': ('agentic_index',),
    'coding': ('coding_index',),
    'vision': ('MMMU',),
}
BAND_QUALITY_FLOOR_GAP_DEFAULT = {'default': 6, 'high': 2}
TIER_GATE_CRITERIA_DEFAULT = {
    'high': 'High-stakes, one-shot planning or decomposition where getting it wrong wastes every downstream step (e.g. breaking one large vague request into the subtasks everything else depends on).',
    'mid': 'Needs real multi-step reasoning or judgment: synthesis, classification, summarization of gathered material, a considered in-character reply. Routine but non-trivial thinking.',
    'low': 'Routine, cheap work: a short reply, simple extraction, or a one-line note where the cheap tier is plenty.',
}

MODEL_PRICE_BANDS: dict = dict(MODEL_PRICE_BANDS_DEFAULT)
MODEL_BAND_PURPOSE: dict = dict(MODEL_BAND_PURPOSE_DEFAULT)
BAND_BENCHMARK: dict = dict(BAND_BENCHMARK_DEFAULT)
BAND_QUALITY_FLOOR_GAP: dict = dict(BAND_QUALITY_FLOOR_GAP_DEFAULT)
TIER_GATE_CRITERIA: dict = dict(TIER_GATE_CRITERIA_DEFAULT)

# Secret shapes scrubbed from anything written to the action log / wiki /
# reports (redaction.json). A missed shape is a leaked credential, so this is
# operator-editable and live-reloadable.
SECRET_PATTERN_DEFAULTS = (
    r'sk-[a-zA-Z0-9_-]{16,}',
    r'(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*\S+',
    r'AKIA[0-9A-Z]{16}',
)
SECRET_PATTERNS: list = [re.compile(p) for p in SECRET_PATTERN_DEFAULTS]

# Action-name taxonomies (actions.json). These feed accounting and the /api/log
# filter; the passport hash-chain list (_HASHED_ACTIONS) stays in serve.py on
# purpose, so tamper evidence cannot be widened by editing data.
CEREMONY_ACTIONS_DEFAULT = ('task_peer_widened', 'task_review_requeued', 'review_escalate',
                            'story_vetoed', 'escalation_unsure', 'escalation_approve',
                            'escalation_deny', 'access_request')
PROGRESS_ACTIONS_DEFAULT = ('task_completed', 'product_released', 'sprint_closed',
                            'artifact_published', 'spike_promoted', 'library_promote')
REWORK_ACTIONS_DEFAULT = ('task_review_requeued', 'story_vetoed', 'review_escalate',
                          'task_peer_widened')
API_LOG_ALLOWED_ACTIONS_DEFAULT = (
    'firing_review', 'mail_sent', 'work_item_abandoned', 'task_assigned',
    'big_task_delegated', 'pair_programming', 'media_digest_filed',
    'task_completed', 'handoff', 'hire_blocked_at_cap', 'hire',
)
CEREMONY_ACTIONS: list = list(CEREMONY_ACTIONS_DEFAULT)
PROGRESS_ACTIONS: list = list(PROGRESS_ACTIONS_DEFAULT)
REWORK_ACTIONS: list = list(REWORK_ACTIONS_DEFAULT)
API_LOG_ALLOWED_ACTIONS: set = set(API_LOG_ALLOWED_ACTIONS_DEFAULT)

# Extensions to the kernel's dormancy telemetry sets (endpoint_policy.json).
# ADDITIVE on purpose: a new read endpoint not listed here wakes a sleeping
# tank, so the village can add paths but can never shrink the kernel's safe set.
NON_WAKING_READS_EXTRA: set = set()
NON_WAKING_WRITES_EXTRA: set = set()

# Room overrides (rooms.json). The kernel's definitions are the fail-closed
# seed; these entries are merged over them by key, and `delegatable` replaces
# the kernel list only when non-empty.
ROOM_DEFINITION_OVERRIDES: dict = {}
ROOM_DELEGATABLE_OVERRIDES: list = []

# Role-profile overrides (seed_profiles.json), merged over the kernel's launch
# profiles by role. The kernel text remains the fallback for any role not
# overridden.
SEED_PROFILE_OVERRIDES: dict = {}
SEED_DEFAULT_PROFILE_OVERRIDE: dict = {}

# Per-tool description overrides (tool_descriptions.json). Empty by default:
# serve.py's inline descriptions are the fail-closed source, and this file is
# the editable override layer the village can rewrite without touching code.
TOOL_DESCRIPTIONS: dict = {}


_BUNDLE_ID_RE = re.compile(r'^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$')
_GRANTS_CACHE = {'mtime': None, 'data': None}
_CREDENTIAL_ACCESS_CACHE = {'mtime': None, 'data': None}

_WATCHLIST_CACHE: dict = {'mtime': None, 'data': [], 'ok': False}

_WATCHLIST_LOAD_ERROR = None


def _load_list(path, default, *, key, min_len=1):
    """Read a JSON file holding {key: [string, ...]} and return a validated
    list of non-empty strings, or the default on any failure."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        items = data.get(key)
        if not isinstance(items, list):
            return list(default)
        out = [str(x).strip() for x in items if isinstance(x, str) and str(x).strip()]
        return out or (list(default) if min_len else [])
    except Exception:
        return list(default)


def _load_standing_safe_domains(path):
    """browse_policy.json `standing_safe_domains`: hosts allowed as READ-only
    reference sources. Suffix-matched later, so entries are bare hosts; a
    scheme, path, port or empty entry is dropped rather than trusted."""
    try:
        with open(path, encoding='utf-8') as f:
            items = json.load(f).get('standing_safe_domains')
    except Exception:
        return set(STANDING_SAFE_DOMAINS_DEFAULT)
    if not isinstance(items, list):
        return set(STANDING_SAFE_DOMAINS_DEFAULT)
    out = set()
    for raw in items:
        if not isinstance(raw, str):
            continue
        host = raw.strip().lower().lstrip('.')
        if not host or any(ch in host for ch in '/:@ '):
            continue
        if host in ('gov', 'edu') or '.' in host:
            out.add(host)
    return out or set(STANDING_SAFE_DOMAINS_DEFAULT)


def _load_treg_endpoint_costs(path):
    """service_costs.json `treg_endpoint_costs`: endpoint id -> USD per call.
    Negative or non-numeric prices are dropped; an empty result keeps the
    shipped table so metered calls cannot silently become free."""
    try:
        with open(path, encoding='utf-8') as f:
            items = json.load(f).get('treg_endpoint_costs')
    except Exception:
        return dict(TREG_ENDPOINT_COSTS_DEFAULT)
    if not isinstance(items, dict):
        return dict(TREG_ENDPOINT_COSTS_DEFAULT)
    out = {}
    for key, value in items.items():
        if not isinstance(key, str) or not key.strip():
            continue
        try:
            cost = float(value)
        except (TypeError, ValueError):
            continue
        if cost < 0:
            continue
        out[key.strip()] = cost
    return out or dict(TREG_ENDPOINT_COSTS_DEFAULT)


def _load_model_bands(path):
    """model_bands.json: price bands, band purpose prose, benchmark chains,
    quality-floor gaps, and the tier-gate criteria. Each section validates
    independently and falls back to its shipped default, so one malformed
    section cannot blank the rest."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def _bands(raw, default):
        if not isinstance(raw, dict):
            return dict(default)
        out = {}
        for band, pair in raw.items():
            if not isinstance(band, str) or not band.strip():
                continue
            try:
                lo, hi = float(pair[0]), float(pair[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if lo < 0 or hi <= lo:
                continue
            out[band.strip()] = (lo, hi)
        return out or dict(default)

    def _prose(raw, default):
        if not isinstance(raw, dict):
            return dict(default)
        out = {str(k).strip(): str(v).strip() for k, v in raw.items()
               if isinstance(k, str) and isinstance(v, str) and v.strip()}
        return out or dict(default)

    def _chains(raw, default):
        if not isinstance(raw, dict):
            return dict(default)
        out = {}
        for band, chain in raw.items():
            if not isinstance(band, str) or not band.strip():
                continue
            if not isinstance(chain, list):
                continue
            metrics = tuple(str(m).strip() for m in chain
                            if isinstance(m, str) and m.strip())
            if metrics:
                out[band.strip()] = metrics
        return out or dict(default)

    def _numbers(raw, default):
        if not isinstance(raw, dict):
            return dict(default)
        out = {}
        for band, gap in raw.items():
            if not isinstance(band, str) or not band.strip():
                continue
            try:
                value = float(gap)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                out[band.strip()] = int(value) if value.is_integer() else value
        return out or dict(default)

    return {
        'price_bands': _bands(data.get('price_bands'), MODEL_PRICE_BANDS_DEFAULT),
        'band_purpose': _prose(data.get('band_purpose'), MODEL_BAND_PURPOSE_DEFAULT),
        'band_benchmark': _chains(data.get('band_benchmark'), BAND_BENCHMARK_DEFAULT),
        'quality_floor_gap': _numbers(data.get('quality_floor_gap'), BAND_QUALITY_FLOOR_GAP_DEFAULT),
        'tier_gate_criteria': _prose(data.get('tier_gate_criteria'), TIER_GATE_CRITERIA_DEFAULT),
    }


def _load_endpoint_policy(path):
    """endpoint_policy.json: path lists added to the kernel's dormancy sets.
    Only absolute API paths are accepted; anything else is dropped."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def _paths(key):
        raw = data.get(key)
        if not isinstance(raw, list):
            return set()
        return {p.strip() for p in raw
                if isinstance(p, str) and p.strip().startswith('/')}

    return {'reads': _paths('extra_non_waking_reads'),
            'writes': _paths('extra_non_waking_writes')}


def _load_room_overrides(path):
    """rooms.json: per-room {label?, purpose?} overrides plus an optional
    delegatable list. Partial entries are allowed; unknown keys are dropped."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {}
    raw_defs = data.get('definitions')
    if isinstance(raw_defs, dict):
        for room, spec in raw_defs.items():
            if not isinstance(room, str) or not room.strip() or not isinstance(spec, dict):
                continue
            entry = {}
            for field in ('label', 'purpose'):
                value = spec.get(field)
                if isinstance(value, str) and value.strip():
                    entry[field] = value.strip()
            if entry:
                out[room.strip()] = entry
    delegatable = []
    raw_del = data.get('delegatable')
    if isinstance(raw_del, list):
        delegatable = [r.strip() for r in raw_del if isinstance(r, str) and r.strip()]
    return {'definitions': out, 'delegatable': delegatable}


def _load_seed_profiles(path):
    """seed_profiles.json: role profile overrides plus an optional default
    profile. A profile is {mission: str, instructions: [str], notes: [str]}."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def _profile(raw):
        if not isinstance(raw, dict):
            return None
        mission = raw.get('mission')
        if not isinstance(mission, str) or not mission.strip():
            return None
        out = {'mission': mission.strip()}
        instructions = raw.get('instructions')
        if isinstance(instructions, list):
            out['instructions'] = [i.strip() for i in instructions
                                   if isinstance(i, str) and i.strip()]
        notes = raw.get('notes')
        out['notes'] = [n for n in notes if isinstance(n, str)] if isinstance(notes, list) else []
        return out

    profiles = {}
    raw_profiles = data.get('profiles')
    if isinstance(raw_profiles, dict):
        for role, raw in raw_profiles.items():
            profile = _profile(raw)
            if isinstance(role, str) and role.strip() and profile:
                profiles[role.strip()] = profile
    return {'profiles': profiles,
            'default': _profile(data.get('default'))}


def _load_action_taxonomy(path):
    """actions.json: one validated string list per taxonomy plus the
    api_log_allowed set. Each section falls back to its shipped default
    independently; a section whose entries are all invalid keeps the default."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def _names(key, default):
        raw = data.get(key)
        if not isinstance(raw, list):
            return list(default)
        out = [x.strip() for x in raw if isinstance(x, str) and x.strip()]
        return out or list(default)

    return {
        'ceremony': _names('ceremony_actions', CEREMONY_ACTIONS_DEFAULT),
        'progress': _names('progress_actions', PROGRESS_ACTIONS_DEFAULT),
        'rework': _names('rework_actions', REWORK_ACTIONS_DEFAULT),
        'api_log': _names('api_log_allowed_actions', API_LOG_ALLOWED_ACTIONS_DEFAULT),
    }


def _load_tool_descriptions(path):
    """tool_descriptions.json `descriptions`: tool name -> replacement
    description text. Malformed entries are dropped individually; an empty or
    missing file yields {} so every tool keeps its inline description."""
    try:
        with open(path, encoding='utf-8') as f:
            items = json.load(f).get('descriptions')
    except Exception:
        return {}
    if not isinstance(items, dict):
        return {}
    return {str(name).strip(): str(text).strip()
            for name, text in items.items()
            if isinstance(name, str) and name.strip()
            and isinstance(text, str) and text.strip()}


def _load_secret_patterns(path):
    """redaction.json `secret_patterns`: regexes scrubbed from logged text. An
    invalid regex is dropped individually; an empty or missing file keeps the
    shipped patterns, so redaction never silently turns off."""
    try:
        with open(path, encoding='utf-8') as f:
            items = json.load(f).get('secret_patterns')
    except Exception:
        return [re.compile(p) for p in SECRET_PATTERN_DEFAULTS]
    if not isinstance(items, list):
        return [re.compile(p) for p in SECRET_PATTERN_DEFAULTS]
    out = []
    for raw in items:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            out.append(re.compile(raw))
        except re.error:
            continue
    return out or [re.compile(p) for p in SECRET_PATTERN_DEFAULTS]


def _load_research_desk(path):
    """Read the desk passes file: {passes: [{key, question}, ...]}. Each pass
    must have a non-empty key and question; malformed entries are dropped, and
    an empty or invalid result falls back to the default passes."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        passes = data.get('passes')
        if not isinstance(passes, list):
            return [dict(p) for p in RESEARCH_DESK_PASSES_DEFAULT]
        out = []
        for p in passes:
            if isinstance(p, dict) and (p.get('key') or '').strip() and (p.get('question') or '').strip():
                out.append({'key': str(p['key']).strip(),
                            'question': str(p['question']).strip()})
        return out or [dict(p) for p in RESEARCH_DESK_PASSES_DEFAULT]
    except Exception:
        return [dict(p) for p in RESEARCH_DESK_PASSES_DEFAULT]


def _load_watchlist(path):
    """Read the standing research watchlist: {topics: [{topic, startUrl, ...}]}.
    Entries without a topic or a real absolute startUrl are dropped (the same
    fail-closed rule sim.add_research_topic applies at seed time)."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        topics = data.get('topics')
        if not isinstance(topics, list):
            return []
        out = []
        for t in topics:
            if not isinstance(t, dict):
                continue
            topic = (t.get('topic') or '').strip()
            start_url = (t.get('startUrl') or '').strip()
            if not topic or not start_url:
                continue
            parsed = None
            try:
                from urllib.parse import urlparse
                parsed = urlparse(start_url)
            except ValueError:
                parsed = None
            if not parsed or parsed.scheme not in ('http', 'https') or not parsed.netloc:
                continue
            entry = {'topic': topic, 'startUrl': start_url}
            if t.get('cadenceMs'):
                entry['cadenceMs'] = int(t['cadenceMs'])
            if (t.get('linkKeyword') or '').strip():
                entry['linkKeyword'] = str(t['linkKeyword']).strip()
            if (t.get('pageKeyword') or '').strip():
                entry['pageKeyword'] = str(t['pageKeyword']).strip()
            out.append(entry)
        return out
    except Exception:
        return []


def _load_grants(path):
    """Read operator-edited grants: {appGrants: [{id, bundleId, caps}],
    fileGrants: [{id, scope, caps}]}. Fail-closed: a missing/corrupt file
    grants nothing, malformed entries are dropped, and capabilities default to
    the least-privilege shape (app use on, file read on, writes off)."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {'appGrants': [], 'fileGrants': []}
    apps = []
    for g in data.get('appGrants') or []:
        if not isinstance(g, dict):
            continue
        bid = (g.get('bundleId') or '').strip()
        if not _BUNDLE_ID_RE.match(bid):
            continue
        caps = g.get('caps') or {}
        apps.append({'id': (g.get('id') or '').strip() or f'cfg-app-{len(apps) + 1}',
                     'bundleId': bid,
                     'label': (g.get('label') or '').strip() or bid,
                     'caps': {'use': bool(caps.get('use', True))}})
    files = []
    for g in data.get('fileGrants') or []:
        if not isinstance(g, dict):
            continue
        scope = (g.get('scope') or '').strip()
        if not scope:
            continue
        caps = g.get('caps') or {}
        files.append({'id': (g.get('id') or '').strip() or f'cfg-file-{len(files) + 1}',
                      'scope': os.path.expanduser(scope),
                      'label': (g.get('label') or '').strip() or scope,
                      'caps': {'read': bool(caps.get('read', True)),
                               'write': bool(caps.get('write', False)),
                               'delete': bool(caps.get('delete', False))}})
    return {'appGrants': apps, 'fileGrants': files}


