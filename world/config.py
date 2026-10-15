"""world/config.py -- operator-edited policy JSON loaded from world/*.json.

These settings are DATA, not code. The operator edits the JSON files beside
this module to tune the village without touching Python:

- plain_writing.json    -> PLAIN_WRITING_BAN_LIST      (serve._apply_plain_writing)
- browse_policy.json    -> BROWSE_BLOCK_CATEGORIES      (the browse/execute/save Jev gate)
- research_desk.json    -> RESEARCH_DESK_PASSES         (the desk's multi-pass questions)
- watchlist.json        -> RESEARCH_WATCHLIST           (standing research topics; the seed)
- grants.json           -> GRANTS_CONFIG                (operator-granted app/file access)

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

# Loaded values (module-level dict, MUTATED IN PLACE by reload so consumers
# that did `from config import OPERATOR_POLICY` see reloads immediately).
OPERATOR_POLICY: dict = dict(OPERATOR_POLICY_DEFAULT)

_OPERATOR_CACHE = {'mtime': None, 'data': None}

# Consumer refresh hooks. sim.py and serve.py define module-level scalar
# constants (MAX_TEAM_MEMBERS, PEER_REVIEW_INTERVAL_S, ...); a rebind of
# config.OPERATOR_POLICY cannot rebind those copies, so each module registers
# a zero-arg callback here that re-reads OPERATOR_POLICY and rebinds its own
# globals. Run at the end of reload_policy_config (import time + the
# player-session POST /api/policy/reload endpoint). Exceptions are swallowed
# per-hook: one bad consumer refresh must not abort the others' reload.
ON_RELOAD_HOOKS: list = []


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
    for _hook in list(ON_RELOAD_HOOKS):
        try:
            _hook()
        except Exception:
            pass
    _WATCHLIST_LOAD_ERROR = None
    return {
        'plain_writing': list(PLAIN_WRITING_BAN_LIST),
        'browse_policy': list(BROWSE_BLOCK_CATEGORIES),
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
