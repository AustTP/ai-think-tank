"""The Stakeholder Graph: who knows what, who decided what, who owns what.

The agent roster models the simulated village; this module models the real
organization around the work -- the colleagues whose answers close open
questions. Knowing WHO to ask is often the harder half of the question, and
"already have a meeting with the owning team" beats scheduling a new one.

One SQLite row (stakeholder_graph, blob JSON -- never the kv_state blob),
one RLock, mutations land in the shared evidence_audit trail.

Every free-text field is credential-scrubbed on write: a person record must
never become a place where a shared secret lands (the same discipline
agents/memory.py applies to LTM). Person identity is a lowercased slug of the
name, so "Brian Barrett" and "brian barrett" are one person.
"""

import functools
import hashlib
import json
import re
import threading
import time

_LEDGER_LOCK = threading.RLock()

MAX_NAME_CHARS = 120
MAX_FIELD_ITEMS = 24
MAX_ITEM_CHARS = 400
MAX_PERSONS = 500

# Write-side credential redaction, kept local so core never imports agents
# (the package boundary only allows agents -> core). Same intent as
# memory.py: REDACT in place, keep the surrounding context.
_CREDENTIAL_PATTERNS = (
    re.compile(r"\b(my\s+)?password\s*(is|was|:)\s*[\"']?[\w@#$%^&*!]+[\"']?",
               re.IGNORECASE),
    re.compile(r"\b(api[\s_-]?key|apikey|api[\s_-]?token)\s*(:|=|is)\s+"
               r"[\"']?[a-zA-Z0-9_\-]{16,}[\"']?", re.IGNORECASE),
    re.compile(r"\b(bearer|token|secret)\s+[a-zA-Z0-9_\-\.]{20,}",
               re.IGNORECASE),
    re.compile(r"\b(postgres(ql)?|mysql|redis|mongodb(\+srv)?)://[^\s]+"),
)
_REDACTED = '[REDACTED]'


def _redact_secrets(text):
    if not text:
        return ''
    out = str(text)
    for pattern in _CREDENTIAL_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LEDGER_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _graph_read():
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS stakeholder_graph (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute(
                'SELECT blob FROM stakeholder_graph WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _graph_write(graph):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO stakeholder_graph (id, blob, updated_at) VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
            (json.dumps(graph), time.time()),
        )


def _audit(actor, action, detail=None):
    try:
        import core.evidence as evidence
        evidence.append_audit(actor, action, identity=detail and detail.get('person_id'),
                              detail=detail)
    except Exception:
        pass


def person_id(name):
    return 'st-' + hashlib.sha256((name or '').strip().lower().encode()).hexdigest()[:12]


def _clean_name(name):
    name = (name or '').strip()[:MAX_NAME_CHARS]
    if not name:
        raise ValueError('person name is required')
    return name


def _clean_items(items):
    out = []
    for item in (items or []):
        item = _redact_secrets((item or '').strip())[:MAX_ITEM_CHARS]
        if item and item not in out:
            out.append(item)
    return out[:MAX_FIELD_ITEMS]


@_serialized
def record_person(name, *, team=None, capabilities=None, systems=None,
                  decisions=None, known_topics=None, notes=None, actor=None):
    """Create or merge one person. Merging is additive: a second observation
    about the same person adds capabilities/decisions/topics, it never wipes
    what earlier evidence established. Returns the person record."""
    name = _clean_name(name)
    now = time.time()
    graph = _graph_read()
    pid = person_id(name)
    rec = dict(graph.get(pid) or {})
    rec.setdefault('id', pid)
    rec['name'] = name if not rec.get('name') else rec['name']
    if team:
        rec['team'] = _redact_secrets(team.strip())[:MAX_ITEM_CHARS]
    for field, value in (('capabilities', capabilities), ('systems', systems),
                         ('decisions', decisions), ('known_topics', known_topics)):
        if value:
            merged = list(rec.get(field) or [])
            for item in _clean_items(value):
                if item not in merged:
                    merged.append(item)
            rec[field] = merged
    if notes:
        rec['notes'] = _redact_secrets(notes.strip())[:MAX_ITEM_CHARS]
    rec['updated_ts'] = now
    rec.setdefault('created_ts', now)
    graph[pid] = rec
    _graph_write(graph)
    _audit(actor, 'stakeholder_recorded', detail={'person_id': pid, 'name': name})
    return rec


def get_person(name_or_id):
    """Look up a person by name or by id. Unknown returns None, never a
    guess."""
    if not name_or_id:
        return None
    graph = _graph_read()
    key = name_or_id if str(name_or_id).startswith('st-') else person_id(name_or_id)
    return graph.get(key)


def all_persons():
    return sorted(_graph_read().values(), key=lambda p: p.get('name', ''))


def persons_knowing(topic):
    """People whose capabilities/known_topics/decisions mention `topic`
    (case-insensitive substring). The routing view: 'who can answer this?'
    Returns [] when nothing matches -- absence is explicit."""
    topic = (topic or '').strip().lower()
    if not topic:
        return []
    out = []
    for rec in _graph_read().values():
        haystacks = [rec.get('team') or ''] + list(rec.get('capabilities') or []) \
            + list(rec.get('known_topics') or []) + list(rec.get('decisions') or [])
        if any(topic in (h or '').lower() for h in haystacks):
            out.append(rec)
    out.sort(key=lambda p: p.get('name', ''))
    return out
