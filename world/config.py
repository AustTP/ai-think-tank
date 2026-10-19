"""world/config.py -- operator-edited policy JSON loaded from world/*.json.

These settings are DATA, not code. The operator edits the JSON files beside
this module to tune the village without touching Python:

- plain_writing.json    -> PLAIN_WRITING_BAN_LIST      (serve._apply_plain_writing)
- browse_policy.json    -> BROWSE_BLOCK_CATEGORIES      (the browse/execute/save Jev gate)
- research_desk.json    -> RESEARCH_DESK_PASSES         (the desk's multi-pass questions)
- watchlist.json        -> RESEARCH_WATCHLIST           (standing research topics; the seed)
- grants.json           -> GRANTS_CONFIG                (operator-granted app/file access)
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
BAND_QUALITY_FLOOR_GAP_DEFAULT = {'high': 2}
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


# ---------------------------------------------------------------------------
# Operator policy (world/operator_policy.json). The director/admin knobs that
# used to be hardcoded constants in sim.py/serve.py -- team-size caps, grade
# floors, hire/fire cooldowns, governance cadences, peer-review cadence,
# escalation caps, morale weights, the sim-loop watchdog thresholds. DATA, not
# code: the operator edits the JSON; sim/serve rebind their module constants
# through ON_RELOAD_HOOKS when reload_policy_config() runs (import time and
# POST /api/policy/reload). Spend ceilings stay in .env / POST /api/budget --
# one source of truth per knob.
# ---------------------------------------------------------------------------
OPERATOR_POLICY_PATH = os.path.join(ROOT, 'operator_policy.json')

# Fail-closed defaults, kept in code so a fresh clone (or a deleted/corrupt
# JSON file) boots with the shipped behavior. The shipped operator_policy.json
# carries the SAME values; the operator edits the JSON, not these.
OPERATOR_POLICY_DEFAULT: dict = {
    # Teams & workforce
    'maxTeamMembers': 6,
    'scrumMasterMinTeamSize': 4,
    'maxActiveAgents': 25,
    'maxTotalAgents': 500,
    # Grading floors (a deliverable graded below is coaching-worthy; at/above
    # the success floor it counts as a success).
    'deliverableGradeFloor': 5.0,
    'successGradeFloor': 8.0,
    # Hiring / firing cooldowns (ms)
    'hireCooldownMs': 45000,
    'firingCooldownMs': 60000,
    # Governance cadences (ms)
    'skillReviewCadenceMs': 30 * 60 * 1000,
    'distillCadenceMs': 30 * 60 * 1000,
    'refinementCadenceMs': 7 * 24 * 3600 * 1000,
    'roadmapCadenceMs': 7 * 24 * 3600 * 1000,
    # Work items / sprints / backlog
    'workItemMaxAttempts': 3,
    'staleWorkTimeoutMs': 3 * 3600 * 1000,
    'staleWorkBudgetGraceS': 900,
    'maxBacklogItems': 200,
    'backlogPullsPerClose': 1,
    'escalationMaxOpen': 3,
    # Peer review + director escalations (serve.py)
    'peerReviewIntervalS': 90,
    'peerReviewFileProbability': 0.4,
    'directorEscalationMaxAsks': 6,
    'directorEscalationReaskCooldownS': 15 * 60,
    # Sim-loop watchdog (serve.py; .env SIM_WATCHDOG_*_S still overrides)
    'simWatchdogIntervalS': 30.0,
    'simWatchdogStaleS': 180.0,
    # Background health-check loop cadences (serve.py). The health loop
    # reads these per-iteration/per-call, so a policy reload applies
    # without a restart (the check interval takes effect on the next
    # loop sleep).
    'healthCheckIntervalS': 300,
    'healthAlertDedupWindowS': 12 * 3600,
    'healthDigestIntervalS': 6 * 3600,
    # Morale arithmetic
    'moraleApprovedWeight': 0.5,
    'moraleApprovedCap': 15,
    'moraleDroppedWeight': 6.0,
    'moraleNeglectWeight': 3.0,
    'moraleNeglectCap': 30,
    # Delivery shape: review-gate cycles, coaching rounds, help escalation,
    # sprint velocity cap, and the "no one is picking this up" stall ceiling.
    'maxReviewCycles': 8,
    'coachingMaxRounds': 3,
    'workerHelpAfterFails': 2,
    'teamVelocityMax': 20,
    'workItemStallCeiling': 600,
    # Sim-loop timing (global: one loop serves the whole tank).
    'taskCycleS': 6.0,
    'taskWorkDurationS': 8.0,
    'taskContentTimeoutS': 120.0,
    'taskContentTimeoutMaxS': 600.0,
    # Tier throttle: today's spend below ratio * trailing 7-day median.
    'tierThrottleRatio': 0.7,
    # People-process timers (ms)
    'hireDurationMs': 8000,
    'firingReviewDurationMs': 10000,
    'reportStrikeWindowMs': 14 * 24 * 3600 * 1000,
    'onboardMeetDurationMs': 10000,
    'onboardReadinessTimeoutMs': 15 * 60 * 1000,
    # Villager socials
    'socialCadenceMs': 7 * 24 * 3600 * 1000,
    'socialMeetMs': 30 * 60 * 1000,
    'socialAdoptConfidence': 0.6,
    # Failure cooldown (hire throttling after a bad run)
    'failureCooldownHalfLifeS': 1800,
    'failureCooldownThreshold': 0.5,
    # Free spikes (spare-time moonshots)
    'freeSpikeAllowancePerWeek': 1,
    'freeSpikeGlobalCapPerWeek': 3,
    'freeSpikeBudgetMs': 2 * 60 * 1000,
    'freeSpikeDeferMs': 60 * 1000,
    # Press Office code/review lane (content.py)
    'codeContinuationAttempts': 2,
    'maxCodeProbeRounds': 3,
    'maxCodeColabRounds': 3,
    'maxReviewProbeRounds': 2,
    # Request/response ceilings (serve.py). Were hardcoded constants;
    # now operator data like every other ceiling.
    'chatMaxTokensCap': 4000,
    'smsTtlDefaultS': 300,
    'smsTtlMinS': 60,
    'smsTtlMaxS': 900,
    'decideBurstAllow': 6,
    'decideBurstWindowS': 5.0,
    'decideMinIntervalS': 0.4,
    # Sim-loop ceilings (sim.py), same promotion.
    'placementMinDist': 60,
    'ruleMineCadenceMs': 7 * 24 * 3600 * 1000,
    'runbookMaxEntriesPerProduct': 20,
    'staleWorkMaxReplans': 2,
    'refinementMaxRequests': 20,
    'breakdownMaxRequests': 3,
    'taskContentGraceMaxS': 300.0,
    'feedbackMax': 10,
    'minResearchCadenceMs': 5 * 60 * 1000,
    'minPipelineCadenceMs': 60 * 60 * 1000,
    'deviceCheckinHistoryMax': 50,
    # Experiment grant caps (sublow.py; shared with jev_grants.py).
    'grantTtlDefaultS': 1800,
    'grantTtlMaxS': 86400,
    'grantTokensDefault': 600,
    'grantTokensMax': 4000,
    # Ops hygiene (serve.py backup + prune loops; the loops sleep on the
    # live module value, so a reload takes effect on the next iteration).
    'dbBackupKeep': 1,
    'dbBackupIntervalS': 3600,
    'logRetentionDaysDefault': 7,
    'logPruneIntervalS': 21600,
    'logPruneMaxRows': 50000,
    # Model-tier watch loops (serve.py: full re-pick, price watch,
    # failed-tier recheck cooldown, tier-gate confidence floor).
    'modelTierRefreshIntervalS': 7200,
    'tierPriceWatchIntervalS': 1800,
    'tierRecheckCooldownS': 900,
    'tierGateMinConfidence': 0.55,
    # Rule/success mining bounds (serve.py proposal lanes).
    'failureMaxRecords': 200,
    'successMaxRecords': 200,
    'ruleProposalsMax': 50,
    'successProposalsMax': 50,
    'ruleMinRecurrence': 2,
    'successMinRecurrence': 2,
    # Memory maintenance (serve.py age decay/keep scan).
    'memoryMaxAgeDays': 90,
    'memoryDecayFactor': 0.2,
    'memoryKeepCount': 20,
    # Browse download cap and YouTube transcript caps (serve.py).
    'maxDownloadBytes': 25 * 1024 * 1024,
    'youtubeTranscriptMaxChars': 50000,
    'youtubeTranscriptMaxSegments': 800,
    # Agent-directory sync throttle (serve.py; env
    # AGENT_DIR_SYNC_INTERVAL_S keeps precedence through the rebind).
    'agentDirSyncIntervalS': 15.0,
    # Peer-review gate depth (serve.py lookback and signal staleness).
    'peerReviewMinLookbackS': 3600,
    'peerReviewSignalStaleS': 21600,
}

# Per-key validation: (kind, min, max). A wrong-typed or out-of-range value
# keeps the default for THAT key rather than crashing boot (fail-closed per
# field, same discipline as the other policy loaders).
_OPERATOR_SCHEMA: dict = {
    'maxTeamMembers': ('int', 1, 12),
    'scrumMasterMinTeamSize': ('int', 1, 12),
    'maxActiveAgents': ('int', 1, 100),
    'maxTotalAgents': ('int', 1, 1000),
    'deliverableGradeFloor': ('float', 0.0, 10.0),
    'successGradeFloor': ('float', 0.0, 10.0),
    'hireCooldownMs': ('int', 0, 3600000),
    'firingCooldownMs': ('int', 0, 3600000),
    'skillReviewCadenceMs': ('int', 60000, 7 * 24 * 3600 * 1000),
    'distillCadenceMs': ('int', 60000, 7 * 24 * 3600 * 1000),
    'refinementCadenceMs': ('int', 60000, 30 * 24 * 3600 * 1000),
    'roadmapCadenceMs': ('int', 60000, 30 * 24 * 3600 * 1000),
    'workItemMaxAttempts': ('int', 1, 10),
    'staleWorkTimeoutMs': ('int', 60000, 24 * 3600 * 1000),
    'staleWorkBudgetGraceS': ('int', 0, 86400),
    'maxBacklogItems': ('int', 1, 1000),
    'backlogPullsPerClose': ('int', 1, 10),
    'escalationMaxOpen': ('int', 1, 20),
    'peerReviewIntervalS': ('int', 10, 3600),
    'peerReviewFileProbability': ('float', 0.0, 1.0),
    'directorEscalationMaxAsks': ('int', 1, 20),
    'directorEscalationReaskCooldownS': ('int', 60, 86400),
    'simWatchdogIntervalS': ('float', 5.0, 600.0),
    'simWatchdogStaleS': ('float', 30.0, 3600.0),
    'healthCheckIntervalS': ('int', 60, 86400),
    'healthAlertDedupWindowS': ('int', 60, 7 * 86400),
    'healthDigestIntervalS': ('int', 3600, 7 * 86400),
    'moraleApprovedWeight': ('float', 0.0, 10.0),
    'moraleApprovedCap': ('int', 0, 100),
    'moraleDroppedWeight': ('float', 0.0, 100.0),
    'moraleNeglectWeight': ('float', 0.0, 100.0),
    'moraleNeglectCap': ('int', 0, 100),
    'maxReviewCycles': ('int', 1, 50),
    'coachingMaxRounds': ('int', 1, 20),
    'workerHelpAfterFails': ('int', 1, 10),
    'teamVelocityMax': ('int', 1, 500),
    'workItemStallCeiling': ('int', 10, 86400),
    'taskCycleS': ('float', 1.0, 60.0),
    'taskWorkDurationS': ('float', 1.0, 600.0),
    'taskContentTimeoutS': ('float', 5.0, 3600.0),
    'taskContentTimeoutMaxS': ('float', 10.0, 7200.0),
    'tierThrottleRatio': ('float', 0.0, 1.0),
    'hireDurationMs': ('int', 0, 3600000),
    'firingReviewDurationMs': ('int', 0, 3600000),
    'reportStrikeWindowMs': ('int', 3600000, 90 * 24 * 3600 * 1000),
    'onboardMeetDurationMs': ('int', 0, 3600000),
    'onboardReadinessTimeoutMs': ('int', 60000, 86400000),
    'socialCadenceMs': ('int', 3600000, 30 * 24 * 3600 * 1000),
    'socialMeetMs': ('int', 60000, 86400000),
    'socialAdoptConfidence': ('float', 0.0, 1.0),
    'failureCooldownHalfLifeS': ('int', 60, 86400),
    'failureCooldownThreshold': ('float', 0.0, 1.0),
    'freeSpikeAllowancePerWeek': ('int', 0, 100),
    'freeSpikeGlobalCapPerWeek': ('int', 0, 100),
    'freeSpikeBudgetMs': ('int', 1000, 3600000),
    'freeSpikeDeferMs': ('int', 0, 3600000),
    'codeContinuationAttempts': ('int', 1, 10),
    'maxCodeProbeRounds': ('int', 1, 20),
    'maxCodeColabRounds': ('int', 1, 20),
    'maxReviewProbeRounds': ('int', 1, 20),
    'chatMaxTokensCap': ('int', 1, 32000),
    'smsTtlDefaultS': ('int', 1, 86400),
    'smsTtlMinS': ('int', 1, 3600),
    'smsTtlMaxS': ('int', 1, 86400),
    'decideBurstAllow': ('int', 1, 100),
    'decideBurstWindowS': ('float', 0.1, 3600.0),
    'decideMinIntervalS': ('float', 0.0, 60.0),
    'placementMinDist': ('int', 0, 500),
    'ruleMineCadenceMs': ('int', 60000, 30 * 24 * 3600 * 1000),
    'runbookMaxEntriesPerProduct': ('int', 1, 200),
    'staleWorkMaxReplans': ('int', 0, 100),
    'refinementMaxRequests': ('int', 1, 500),
    'breakdownMaxRequests': ('int', 1, 100),
    'taskContentGraceMaxS': ('float', 0.0, 3600.0),
    'feedbackMax': ('int', 1, 100),
    'minResearchCadenceMs': ('int', 0, 24 * 3600 * 1000),
    'minPipelineCadenceMs': ('int', 0, 7 * 24 * 3600 * 1000),
    'deviceCheckinHistoryMax': ('int', 1, 10000),
    'grantTtlDefaultS': ('int', 1, 86400),
    'grantTtlMaxS': ('int', 1, 7 * 86400),
    'grantTokensDefault': ('int', 1, 100000),
    'grantTokensMax': ('int', 1, 1000000),
    'dbBackupKeep': ('int', 1, 20),
    'dbBackupIntervalS': ('int', 300, 86400),
    'logRetentionDaysDefault': ('int', 1, 365),
    'logPruneIntervalS': ('int', 600, 86400),
    'logPruneMaxRows': ('int', 1000, 500000),
    'modelTierRefreshIntervalS': ('int', 1800, 7 * 86400),
    'tierPriceWatchIntervalS': ('int', 300, 86400),
    'tierRecheckCooldownS': ('int', 60, 86400),
    'tierGateMinConfidence': ('float', 0.0, 1.0),
    'failureMaxRecords': ('int', 10, 5000),
    'successMaxRecords': ('int', 10, 5000),
    'ruleProposalsMax': ('int', 1, 500),
    'successProposalsMax': ('int', 1, 500),
    'ruleMinRecurrence': ('int', 2, 100),
    'successMinRecurrence': ('int', 2, 100),
    'memoryMaxAgeDays': ('int', 1, 3650),
    'memoryDecayFactor': ('float', 0.0, 1.0),
    'memoryKeepCount': ('int', 1, 1000),
    'maxDownloadBytes': ('int', 1048576, 1073741824),
    'youtubeTranscriptMaxChars': ('int', 1000, 1000000),
    'youtubeTranscriptMaxSegments': ('int', 10, 100000),
    'agentDirSyncIntervalS': ('float', 1.0, 3600.0),
    'peerReviewMinLookbackS': ('int', 300, 86400),
    'peerReviewSignalStaleS': ('int', 300, 7 * 86400),
}

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
AGENT_EDITABLE_POLICY_KEYS: frozenset = frozenset({
    'modelTierRefreshIntervalS', 'tierPriceWatchIntervalS',
    'tierRecheckCooldownS', 'tierGateMinConfidence',
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
    POLICY_BINDINGS[module_name] = {
        'pairs': tuple((attr, key) for attr, key in pairs),
        'overrides': dict(overrides or {}),
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
                        'tier_gate_criteria': dict(TIER_GATE_CRITERIA)},
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
        topics.append(entry)
        data['topics'] = topics
        with open(WATCHLIST_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        return True
    except Exception:
        return False


reload_policy_config()
