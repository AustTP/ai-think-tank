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
    _WATCHLIST_LOAD_ERROR = None
    return {
        'plain_writing': list(PLAIN_WRITING_BAN_LIST),
        'browse_policy': list(BROWSE_BLOCK_CATEGORIES),
        'research_desk': [dict(p) for p in RESEARCH_DESK_PASSES],
        'watchlist': [dict(t) for t in RESEARCH_WATCHLIST],
        'grants': {'appGrants': [dict(g) for g in GRANTS_CONFIG['appGrants']],
                   'fileGrants': [dict(g) for g in GRANTS_CONFIG['fileGrants']]},
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
