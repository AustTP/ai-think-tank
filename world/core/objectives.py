"""Objectives: the coverage chain (objective -> requirement -> evidence ->
implementation -> validation -> owner -> gap).

The question registry answers "what do we not know?"; the claim ledger
answers "what did we learn?"; neither says "are we COVERED for the objective
we are actually responsible for?" An objective decomposes into requirements;
each requirement needs evidence (claim ids), an implementation note, a
validation note, and an owner. A requirement missing any of those is a GAP,
computed -- never asserted -- so the coverage view cannot drift from the
data.

One SQLite row (objective_ledger, blob JSON -- never the kv_state blob), one
RLock, mutations land in the shared evidence_audit trail. This is the
generalized form of the security coverage chain (threat -> telemetry ->
detection -> validation -> response -> owner -> gap) with the fields left
free-form, so the same shape covers security coverage, research
completeness, or architecture decisions without a schema fork per domain.
"""

import functools
import hashlib
import json
import threading
import time

_LEDGER_LOCK = threading.RLock()

MAX_TITLE_CHARS = 300
MAX_REQ_TEXT_CHARS = 600
MAX_REQUIREMENTS = 64
MAX_CLAIMS_PER_REQ = 16
MAX_OBJECTIVES = 200

# The four facts a requirement needs to read as covered. Each is optional at
# write time -- capture what you have, reconcile later -- but the computed
# coverage view marks any missing one a gap.
COVERAGE_FIELDS = ('evidence_claim_ids', 'implementation', 'validation', 'owner')


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LEDGER_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _objectives_read():
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS objective_ledger (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute(
                'SELECT blob FROM objective_ledger WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _objectives_write(objectives):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO objective_ledger (id, blob, updated_at) VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
            (json.dumps(objectives), time.time()),
        )


def _objective_id(title):
    digest = hashlib.sha256((title or '').strip().lower().encode()).hexdigest()[:16]
    return f'obj-{digest}'


def _audit(actor, action, detail=None):
    try:
        import core.evidence as evidence
        evidence.append_audit(actor, action, detail=detail)
    except Exception:
        pass


def _clean_title(title):
    title = (title or '').strip()[:MAX_TITLE_CHARS]
    if not title:
        raise ValueError('objective title is required')
    return title


def _clean_claims(claims):
    return [c for c in (claims or []) if c][:MAX_CLAIMS_PER_REQ]


def requirement_coverage(req):
    """The computed per-requirement view: which of the four coverage facts are
    present, which are gaps, and whether the requirement is covered at all
    (all four present). Computed, never stored -- it cannot drift."""
    present = {}
    for field in COVERAGE_FIELDS:
        value = req.get(field)
        if field == 'evidence_claim_ids':
            present[field] = bool(value)
        else:
            present[field] = bool((value or '').strip())
    gaps = [field for field in COVERAGE_FIELDS if not present[field]]
    return {'requirement_id': req.get('requirement_id'),
            'text': req.get('text'),
            'covered': not gaps,
            'gaps': gaps,
            'owner': req.get('owner') or None,
            'question_ids': list(req.get('question_ids') or [])}


def _clean_requirement(text):
    text = (text or '').strip()[:MAX_REQ_TEXT_CHARS]
    if not text:
        raise ValueError('requirement text is required')
    return text


@_serialized
def record_objective(title, *, requirements=None, question_ids=None,
                     owner=None, actor=None):
    """Create or merge one objective. Requirements are matched by cleaned
    text: re-recording an objective with a fuller version of a requirement
    updates that requirement in place instead of duplicating it. Returns the
    objective record."""
    title = _clean_title(title)
    now = time.time()
    objectives = _objectives_read()
    if len(objectives) >= MAX_OBJECTIVES and _objective_id(title) not in objectives:
        return None
    oid = _objective_id(title)
    rec = dict(objectives.get(oid) or {})
    rec.setdefault('id', oid)
    rec['title'] = title
    if owner:
        rec['owner'] = (owner or '').strip()[:200]
    if question_ids:
        merged = list(rec.get('question_ids') or [])
        for qid in question_ids[:MAX_REQUIREMENTS]:
            if qid not in merged:
                merged.append(qid)
        rec['question_ids'] = merged
    existing_reqs = {r.get('text', '').strip().lower(): dict(r)
                     for r in (rec.get('requirements') or [])}
    for req in (requirements or []):
        if isinstance(req, str):
            req = {'text': req}
        if not isinstance(req, dict):
            continue
        try:
            text = _clean_requirement(req.get('text'))
        except ValueError:
            continue
        key = text.lower()
        merged = existing_reqs.get(key) or {
            'requirement_id': 'req-' + hashlib.sha256(
                f'{oid}|{key}'.encode()).hexdigest()[:12],
            'text': text,
            'evidence_claim_ids': [],
            'implementation': '',
            'validation': '',
            'owner': '',
            'question_ids': [],
        }
        claims = list(merged.get('evidence_claim_ids') or [])
        for c in _clean_claims(req.get('evidence_claim_ids')):
            if c not in claims:
                claims.append(c)
        merged['evidence_claim_ids'] = claims
        for field in ('implementation', 'validation', 'owner'):
            value = (req.get(field) or '').strip()[:MAX_REQ_TEXT_CHARS]
            if value:
                merged[field] = value
        if req.get('question_ids'):
            qids = list(merged.get('question_ids') or [])
            for qid in req['question_ids'][:MAX_REQUIREMENTS]:
                if qid not in qids:
                    qids.append(qid)
            merged['question_ids'] = qids
        existing_reqs[key] = merged
    rec['requirements'] = list(existing_reqs.values())
    rec['updated_ts'] = now
    rec.setdefault('created_ts', now)
    objectives[oid] = rec
    _objectives_write(objectives)
    _audit(actor, 'objective_recorded', detail={'objective_id': oid, 'title': title})
    return rec


def get_objective(title_or_id):
    if not title_or_id:
        return None
    objectives = _objectives_read()
    key = title_or_id if str(title_or_id).startswith('obj-') else _objective_id(title_or_id)
    return objectives.get(key)


def all_objectives():
    return sorted(_objectives_read().values(), key=lambda o: o.get('title', ''))


def objective_coverage(title_or_id):
    """The coverage view for one objective: per-requirement computed coverage,
    the list of open gaps, and an overall covered flag (every requirement
    covered). Unknown objective returns None."""
    obj = get_objective(title_or_id)
    if obj is None:
        return None
    reqs = [requirement_coverage(r) for r in (obj.get('requirements') or [])]
    gaps = [
        {'requirement_id': r['requirement_id'], 'text': r['text'],
         'missing': r['gaps']}
        for r in reqs if not r['covered']
    ]
    return {'objective_id': obj['id'], 'title': obj['title'],
            'requirements': reqs,
            'total_requirements': len(reqs),
            'covered_requirements': sum(1 for r in reqs if r['covered']),
            'covered': bool(reqs) and not gaps,
            'gaps': gaps}
