"""Investigations (2026-11-18, Priority 1 of the coherence review): the
case-level composite the review asked for -- ONE durable object that binds
a question to its ledger items, tasks, wiki pages, owner, deadline,
reassessment date, current conclusion, and final disposition.

This module deliberately does NOT duplicate what those references hold.
The epistemic ledger stays the truth store for claims/evidence/contradictions;
the task mirror stays the truth store for work; the wiki stays the knowledge
layer. An investigation is the thin INDEX a human reads: open one object and
see the whole case -- question, what we now believe (derived live from the
referenced ledger items' statuses), what is unproven, who owns it, when it
is due for reassessment, and how it ended.

Storage: one SQLite row (investigations, blob JSON) through the same
_serve._db() seam the epistemic ledger uses, so the backend is whatever the
deployment runs. Reads derive the validation view at call time -- a case
never stores a stale copy of what its ledger items say.

Validation-state derivation (the review's "validation state" field, computed
never stored):
  established  every referenced ledger item is validated and fresh
  contradicted at least one referenced item is disputed/contradicted and
               none of the contradictions resolved in its favor
  stale        every referenced item is stale (readable, untrusted)
  partial      referenced items exist in mixed states
  unverified   no ledger items attached yet
"""
import json
import re
import time

MAX_OPEN_INVESTIGATIONS = 500
_DISPOSITIONS = frozenset({'open', 'concluded', 'superseded', 'abandoned'})
_CONFIDENCES = frozenset({'low', 'medium', 'high'})


def _store_read():
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS investigations (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute(
                'SELECT blob FROM investigations WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _store_write(cases):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO investigations (id, blob, updated_at) '
            'VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET '
            'blob = excluded.blob, updated_at = excluded.updated_at',
            (json.dumps(cases), time.time()))


def _ensure_shape(cases):
    cases.setdefault('investigations', {})
    cases.setdefault('history', [])
    return cases


def _clean(text, cap):
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:cap]


def _id(question, seq):
    digest = __import__('hashlib').sha256(
        question.lower().encode()).hexdigest()[:8]
    return f'inv-{digest}-{seq}'


def _serialized(fn):
    import threading
    lock = threading.Lock()

    def wrapper(*args, **kwargs):
        with lock:
            cases = _ensure_shape(_store_read())
            out = fn(cases, *args, **kwargs)
            _store_write(cases)
            return out
    return wrapper


def _ref_ids(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for v in value:
        v = _clean(v, 120)
        if v and v not in out:
            out.append(v)
    return out


def _validation_state(item_statuses):
    """Derived, never stored -- see module docstring."""
    if not item_statuses:
        return 'unverified'
    statuses = set(item_statuses)
    if statuses <= {'validated'}:
        return 'established'
    if statuses & {'disputed', 'contradicted'}:
        return 'contradicted'
    if statuses <= {'stale'}:
        return 'stale'
    return 'partial'


def describe(case, now_ms=None):
    """The human view of one case: stored fields plus the LIVE validation
    view of its referenced ledger items (statuses derived via
    core.epistemics, staleness computed at read time)."""
    import core.epistemics as epistemics
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    item_states = {}
    for item_id in case.get('ledgerItemIds') or []:
        item = epistemics.get_item(item_id)
        if item is None:
            item_states[item_id] = 'missing'
            continue
        item_states[item_id] = epistemics.effective_status(item, now_ms)
    return {
        **{k: v for k, v in case.items() if k != 'history'},
        # History rides along (capped) -- the human view of a case includes
        # who changed what.
        'history': (case.get('history') or [])[-20:],
        'validationState': _validation_state(item_states.values()),
        'itemStates': item_states,
        'overdue': bool(case.get('deadlineMs')
                        and case['deadlineMs'] < now_ms
                        and case.get('disposition') == 'open'),
        'reassessmentDue': bool(case.get('reassessmentMs')
                                and case['reassessmentMs'] < now_ms
                                and case.get('disposition') == 'open'),
    }


@_serialized
def create_investigation(cases, question, *, owner=None, actor=None,
                         ledger_item_ids=(), task_ids=(),
                         wiki_page_ids=(), deadline_ms=None,
                         reassessment_ms=None):
    question = _clean(question, 400)
    if not question:
        return {'created': False, 'reason': 'invalid',
                'error': 'a question is required'}
    open_count = sum(1 for c in cases['investigations'].values()
                     if c.get('disposition') == 'open')
    if open_count >= MAX_OPEN_INVESTIGATIONS:
        return {'created': False, 'reason': 'capacity',
                'error': f'{open_count} open investigations at the cap '
                         f'({MAX_OPEN_INVESTIGATIONS}); conclude some first'}
    seq = len(cases['investigations']) + 1
    case = {
        'id': _id(question, seq),
        'question': question,
        'owner': _clean(owner, 64) or None,
        'createdBy': _clean(actor, 64) or 'unknown',
        'createdAtMs': int(time.time() * 1000),
        'updatedAtMs': int(time.time() * 1000),
        'ledgerItemIds': _ref_ids(ledger_item_ids),
        'taskIds': _ref_ids(task_ids),
        'wikiPageIds': _ref_ids(wiki_page_ids),
        'deadlineMs': int(deadline_ms) if deadline_ms else None,
        'reassessmentMs': int(reassessment_ms) if reassessment_ms else None,
        'conclusion': None,
        'confidence': None,
        'disposition': 'open',
        'finalDisposition': None,
        'history': [{'atMs': int(time.time() * 1000),
                     'actor': _clean(actor, 64) or 'unknown',
                     'event': 'created'}],
    }
    cases['investigations'][case['id']] = case
    return {'created': True, 'id': case['id'], 'case': describe(case)}


def get_investigation(inv_id, now_ms=None):
    cases = _ensure_shape(_store_read())
    case = cases['investigations'].get(inv_id)
    return describe(case, now_ms) if case else None


def list_investigations(*, disposition=None, owner=None, now_ms=None):
    cases = _ensure_shape(_store_read())
    out = []
    for case in cases['investigations'].values():
        if disposition and case.get('disposition') != disposition:
            continue
        if owner and case.get('owner') != owner:
            continue
        out.append(describe(case, now_ms))
    out.sort(key=lambda c: (c.get('disposition') != 'open',
                            -(c.get('createdAtMs') or 0)))
    return out


@_serialized
def update_investigation(cases, inv_id, *, actor=None, owner=None,
                         deadline_ms=None, reassessment_ms=None,
                         add_ledger_item_ids=(), add_task_ids=(),
                         add_wiki_page_ids=(), drop_ref_ids=()):
    case = cases['investigations'].get(inv_id)
    if case is None:
        return {'updated': False, 'reason': 'unknown_investigation'}
    changed = []
    if owner is not None:
        case['owner'] = _clean(owner, 64) or None
        changed.append('owner')
    if deadline_ms is not None:
        case['deadlineMs'] = int(deadline_ms) or None
        changed.append('deadline')
    if reassessment_ms is not None:
        case['reassessmentMs'] = int(reassessment_ms) or None
        changed.append('reassessment')
    for field, additions in (('ledgerItemIds', add_ledger_item_ids),
                             ('taskIds', add_task_ids),
                             ('wikiPageIds', add_wiki_page_ids)):
        for ref in _ref_ids(additions):
            if ref not in case[field]:
                case[field].append(ref)
                changed.append(field)
    for ref in _ref_ids(drop_ref_ids):
        for field in ('ledgerItemIds', 'taskIds', 'wikiPageIds'):
            if ref in case[field]:
                case[field].remove(ref)
                changed.append(field)
    if not changed:
        return {'updated': False, 'reason': 'no_changes'}
    case['updatedAtMs'] = int(time.time() * 1000)
    case['history'].append({'atMs': case['updatedAtMs'],
                            'actor': _clean(actor, 64) or 'unknown',
                            'event': 'updated', 'fields': changed})
    return {'updated': True, 'changed': changed,
            'case': describe(case)}


@_serialized
def conclude_investigation(cases, inv_id, conclusion, *, confidence=None,
                           disposition='concluded', final_text=None,
                           actor=None):
    """Close the case. Discipline: a conclusion requires text, an explicit
    confidence, and -- unless the case is being abandoned -- at least one
    referenced ledger item or an explicit override via a superseding
    disposition. 'concluded' means the referenced knowledge now carries the
    conclusion; 'superseded' hands the question to a successor case
    (name it in final_text); 'abandoned' closes it without a knowledge
    claim."""
    case = cases['investigations'].get(inv_id)
    if case is None:
        return {'concluded': False, 'reason': 'unknown_investigation'}
    if case.get('disposition') != 'open':
        return {'concluded': False, 'reason': 'wrong_disposition',
                'disposition': case.get('disposition')}
    if disposition not in ('concluded', 'superseded', 'abandoned'):
        return {'concluded': False, 'reason': 'invalid',
                'error': 'disposition must be concluded, superseded, '
                         'or abandoned'}
    confidence = _clean(confidence, 16).lower() or None
    if disposition == 'abandoned':
        conclusion = _clean(conclusion, 2000) or None
        confidence = None
    else:
        conclusion = _clean(conclusion, 2000)
        if not conclusion:
            return {'concluded': False, 'reason': 'invalid',
                    'error': 'a conclusion is required'}
        if confidence not in _CONFIDENCES:
            return {'concluded': False, 'reason': 'invalid',
                    'error': 'confidence must be low, medium, or high'}
        if not case.get('ledgerItemIds'):
            return {'concluded': False, 'reason': 'no_ledger_items',
                    'error': 'a conclusion must cite ledger items; attach '
                             'them first (the conclusion rests on the '
                             'ledger, not on this object)'}
    case['conclusion'] = conclusion
    case['confidence'] = confidence
    case['disposition'] = disposition
    case['finalDisposition'] = _clean(final_text, 2000) or None
    case['updatedAtMs'] = int(time.time() * 1000)
    case['history'].append({'atMs': case['updatedAtMs'],
                            'actor': _clean(actor, 64) or 'unknown',
                            'event': disposition,
                            'confidence': confidence})
    return {'concluded': True, 'disposition': disposition,
            'case': describe(case)}
