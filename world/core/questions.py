"""The Question Registry: a durable, status-tracked ledger of open questions.

The evidence ledger answers "what did we learn from a source?"; this module
answers "what do we still not know?" Claims come and go per source identity,
but a question about the work itself (telemetry availability, ownership,
architecture decisions) must survive across runs, sources, and conversations
until it is answered or contradicted.

One SQLite row (question_registry, blob JSON -- never the kv_state blob,
mirroring evidence_records), one RLock, and every mutation lands in the same
insert-only evidence_audit trail the evidence ledger uses.

A question:
- text: the question itself (deduped by text hash -- the same question asked
  twice is one question with two askers, not two rows);
- status: unknown -> partial -> tentatively_answered -> answered, with
  contradicted as the honest terminal state when later evidence flips it;
- evidence_claim_ids: the evidence-ledger claims that support or refute it;
- people_who_may_know: stakeholder names uniquely positioned to answer;
- related_question_ids: the chain a question spawns ("what telemetry?" ->
  "is it available?" -> "where does it land?").

Supersession is modeled explicitly: when a conversation reframes the question
(old question closed, new question opened and linked), raise_conflict() keeps
the chain auditable instead of silently overwriting.
"""

import functools
import hashlib
import json
import threading
import time

# Status lifecycle. ANSWER_STATUSES are the terminal-ish states a review can
# set; OPEN_STATUSES are what open_questions() returns.
QUESTION_STATUSES = frozenset({'unknown', 'partial', 'tentatively_answered',
                               'answered', 'contradicted'})
OPEN_STATUSES = frozenset({'unknown', 'partial', 'tentatively_answered'})
ANSWERED_STATUSES = frozenset({'tentatively_answered', 'answered'})

_LEDGER_LOCK = threading.RLock()

# A question without text is not a question -- same bar as the claim ledger's
# "no claim text is noise".
MAX_TEXT_CHARS = 2000
MAX_PEOPLE = 16
MAX_RELATED = 16
MAX_CLAIMS = 16
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LEDGER_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _questions_read():
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS question_registry (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute(
                'SELECT blob FROM question_registry WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _questions_write(questions):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO question_registry (id, blob, updated_at) VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
            (json.dumps(questions), time.time()),
        )


def _question_id(text):
    digest = hashlib.sha256((text or '').strip().lower().encode()).hexdigest()[:16]
    return f'q-{digest}'


def _audit(actor, action, question_id=None, detail=None):
    try:
        import core.evidence as evidence
        evidence.append_audit(actor, action, claim_id=question_id, detail=detail)
    except Exception:
        pass


def _clean_person(name):
    return (name or '').strip()[:120]


def _clean_text(text):
    return (text or '').strip()[:MAX_TEXT_CHARS]


def _validate_confidence(confidence):
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        raise ValueError('confidence must be a number between 0.0 and 1.0')
    if not (CONFIDENCE_MIN <= c <= CONFIDENCE_MAX):
        raise ValueError('confidence must be a number between 0.0 and 1.0')
    return round(c, 4)


@_serialized
def record_question(text, *, objective=None, people_who_may_know=None,
                    evidence_claim_ids=None, related_question_ids=None,
                    confidence=None, source=None, actor=None):
    """Record or dedupe one question. The same text (case-insensitive) is the
    same question: a repeat records the second asker against the existing row
    and never creates a duplicate. Returns {'recorded': True, 'id', 'new'} or
    {'recorded': False, 'reason': 'invalid', 'error'}."""
    text = _clean_text(text)
    if not text:
        return {'recorded': False, 'reason': 'invalid', 'error': 'question text is required'}
    now = time.time()
    people = [p for p in (_clean_person(p) for p in (people_who_may_know or [])) if p][:MAX_PEOPLE]
    claims = [c for c in (evidence_claim_ids or []) if c][:MAX_CLAIMS]
    related = [r for r in (related_question_ids or [])][:MAX_RELATED]
    questions = _questions_read()
    qid = _question_id(text)
    existing = questions.get(qid)
    if existing is not None:
        # Same question, possibly a second observer or new routing hint.
        changed = False
        for p in people:
            if p not in existing.get('people_who_may_know', []):
                existing.setdefault('people_who_may_know', []).append(p)
                changed = True
        for c in claims:
            if c not in existing.get('evidence_claim_ids', []):
                existing.setdefault('evidence_claim_ids', []).append(c)
                changed = True
        for r in related:
            if r not in existing.get('related_question_ids', []):
                existing.setdefault('related_question_ids', []).append(r)
                changed = True
        if objective and objective != existing.get('objective'):
            existing['objective'] = objective
            changed = True
        existing.setdefault('asked_by', []).append(_clean_person(source) or actor or 'unknown')
        existing['updated_ts'] = now
        questions[qid] = existing
        _questions_write(questions)
        return {'recorded': True, 'id': qid, 'new': False}
    rec = {
        'id': qid,
        'text': text,
        'objective': (objective or '').strip()[:200] or None,
        'status': 'unknown',
        'confidence': None,
        'answer': None,
        'evidence_claim_ids': claims,
        'people_who_may_know': people,
        'related_question_ids': related,
        'asked_by': [_clean_person(source) or actor or 'unknown'],
        'created_ts': now,
        'updated_ts': now,
        'last_reviewed_ts': None,
    }
    if confidence is not None:
        try:
            rec['confidence'] = _validate_confidence(confidence)
        except ValueError as e:
            return {'recorded': False, 'reason': 'invalid', 'error': str(e)}
    questions[qid] = rec
    _questions_write(questions)
    _audit(actor, 'question_recorded', question_id=qid, detail={'text': text})
    return {'recorded': True, 'id': qid, 'new': True}


@_serialized
def answer_question(question_id, answer, *, status='answered', confidence=1.0,
                    evidence_claim_id=None, actor=None):
    """Set an answer on a question. `status` must be an answered-family status
    ('tentatively_answered' keeps the question visibly soft); 'contradicted'
    is refused here -- that is contradict_question()'s move, not an answer's.
    Returns the updated question, or None when unknown."""
    answer = _clean_text(answer)
    if not answer:
        raise ValueError('answer text is required')
    if status not in ANSWERED_STATUSES:
        raise ValueError(f'status must be one of {sorted(ANSWERED_STATUSES)}')
    confidence = _validate_confidence(confidence)
    now = time.time()
    questions = _questions_read()
    rec = questions.get(question_id)
    if rec is None:
        return None
    rec['answer'] = answer
    rec['status'] = status
    rec['confidence'] = confidence
    if evidence_claim_id and evidence_claim_id not in rec.get('evidence_claim_ids', []):
        rec.setdefault('evidence_claim_ids', []).append(evidence_claim_id)
    rec['last_reviewed_ts'] = now
    rec['updated_ts'] = now
    questions[question_id] = rec
    _questions_write(questions)
    _audit(actor, 'question_answered', question_id=question_id,
           detail={'status': status, 'confidence': confidence})
    return rec


@_serialized
def contradict_question(question_id, *, evidence_claim_id=None, reason=None, actor=None):
    """Flip a question to contradicted: later evidence showed its working
    premise was wrong. The answer is cleared (not kept -- a contradicted
    question must not be read as a source of truth), and the evidence stays on
    the record. Returns the updated question, or None when unknown."""
    now = time.time()
    questions = _questions_read()
    rec = questions.get(question_id)
    if rec is None:
        return None
    rec['status'] = 'contradicted'
    rec['answer'] = None
    rec['confidence'] = None
    if evidence_claim_id and evidence_claim_id not in rec.get('evidence_claim_ids', []):
        rec.setdefault('evidence_claim_ids', []).append(evidence_claim_id)
    rec['last_reviewed_ts'] = now
    rec['updated_ts'] = now
    questions[question_id] = rec
    _questions_write(questions)
    _audit(actor, 'question_contradicted', question_id=question_id,
           detail={'reason': (reason or '').strip()[:200]})
    return rec


@_serialized
def raise_conflict(old_question_id, new_text, *, objective=None,
                   people_who_may_know=None, actor=None):
    """The conversation-reframing move: a question's premise changed, so the
    old question closes as contradicted and a NEW question opens, linked back
    to it. The chain is the audit trail of how our understanding evolved
    ("how do we ingest the data?" -> "what content should we build from it?").
    Returns the new question record."""
    new_text = _clean_text(new_text)
    if not new_text:
        raise ValueError('new question text is required')
    questions = _questions_read()
    old = questions.get(old_question_id)
    if old is not None:
        old['status'] = 'contradicted'
        old['answer'] = None
        old['updated_ts'] = time.time()
        questions[old_question_id] = old
        _questions_write(questions)
        _audit(actor, 'question_contradicted', question_id=old_question_id,
               detail={'superseded_by': _question_id(new_text)})
    result = record_question(new_text, objective=objective,
                             people_who_may_know=people_who_may_know, actor=actor)
    if result.get('recorded'):
        new_rec = questions.get(result['id']) or _questions_read().get(result['id'])
        if new_rec is not None:
            related = list(new_rec.get('related_question_ids') or [])
            if old_question_id not in related:
                related.append(old_question_id)
            new_rec['related_question_ids'] = related[:MAX_RELATED]
            new_rec['updated_ts'] = time.time()
            questions[result['id']] = new_rec
            _questions_write(questions)
    return _questions_read().get(result.get('id')) if result.get('recorded') else None


def get_question(question_id):
    return _questions_read().get(question_id)


def open_questions(objective=None, limit=None):
    """Open questions (unknown/partial/tentatively_answered), oldest first.
    Filtered by objective when given. The gap view: this is what the system
    still does not know."""
    questions = (_questions_read() or {}).values()
    out = [q for q in questions
           if q.get('status') in OPEN_STATUSES
           and (objective is None or q.get('objective') == objective)]
    out.sort(key=lambda q: q.get('created_ts', 0))
    if limit:
        out = out[-int(limit):]
    return out


def questions_for_person(person, limit=None):
    """Open questions this person may be able to answer -- the routing view
    behind conversation prep. People explicitly named first, then everything
    open (a fallback a briefing can order by confidence in the person)."""
    person = _clean_person(person)
    if not person:
        return []
    questions = (_questions_read() or {}).values()
    named = [q for q in questions
             if q.get('status') in OPEN_STATUSES
             and person in (q.get('people_who_may_know') or [])]
    named.sort(key=lambda q: q.get('created_ts', 0))
    out = named
    if limit:
        out = out[-int(limit):]
    return out


@_serialized
def prepare_conversation(person, *, channel='email', max_questions=5):
    """Pre-meeting briefing for one person: what we know, what is worth
    asking THEM, what NOT to ask (already answered), and the attention price
    of each ask.

    The cap is the feature: an opening bundle of a dozen questions reads as a
    chore ("this is a lot -- easier to talk through it"), so the briefing
    stops at max_questions. Person-specific routing: questions explicitly
    naming the person come first, then stakeholder-graph matches by topic.
    Returns {'person', 'known', 'unknown', 'do_not_ask', 'asks'} where each
    ask carries its attention_quote so the planner can see the price."""
    person = _clean_person(person)
    if not person:
        raise ValueError('person name is required')
    try:
        import core.stakeholders as stakeholders
        import core.budget as budget
        rec = stakeholders.get_person(person)
        known = list(rec.get('known_topics') or []) if rec else []
        topics = [t.lower() for t in known]
        if rec:
            topics += [t.lower() for t in (rec.get('decisions') or [])]
    except Exception:
        rec = None
        known = []
        topics = []
    questions = _questions_read()
    open_qs = [q for q in questions.values() if q.get('status') in OPEN_STATUSES]
    named = [q for q in open_qs if person in (q.get('people_who_may_know') or [])]
    named.sort(key=lambda q: q.get('created_ts', 0))
    # Topic-matched fallback: this person's graph entry says they know it.
    matched = [q for q in open_qs if q not in named
               and any(t in q['text'].lower() for t in topics)]
    ordered = (named + matched)[:max(0, int(max_questions))]
    asks = []
    for q in ordered:
        try:
            quote = budget.attention_quote(
                person, channel=channel, question_text=q['text'],
                asked_before=person in (q.get('asked_by') or []),
                answerable_from_evidence=False)
        except Exception:
            quote = {'person': person, 'channel': channel, 'cost': None,
                     'reasons': ['attention quote unavailable'],
                     'question_text': q['text']}
        asks.append({'question_id': q['id'], 'text': q['text'], 'quote': quote})
    # Do-not-ask: questions this person is tied to that are already answered.
    do_not_ask = [
        {'question_id': q['id'], 'text': q['text'], 'answer': q.get('answer')}
        for q in questions.values()
        if q.get('status') in ANSWERED_STATUSES
        and (person in (q.get('people_who_may_know') or [])
             or any(t in q['text'].lower() for t in topics))
    ]
    return {'person': person, 'known': known,
            'unknown': [a['text'] for a in asks],
            'do_not_ask': do_not_ask[:max_questions * 2],
            'asks': asks}


@_serialized
def registry_brief(*, window_s=7 * 86400):
    """The living brief for the registry: open questions (the gap view),
    what changed recently (answered/contradicted in the window -- the change
    view), and counts. Read-only composition of data the registry already
    holds; the caller renders it. No confidence percentages: counts and
    statuses are the honest units here."""
    now = time.time()
    questions = _questions_read()
    open_qs = [q for q in questions.values()
               if q.get('status') in OPEN_STATUSES]
    open_qs.sort(key=lambda q: q.get('created_ts', 0))
    changed = [
        {'question_id': q['id'], 'text': q['text'], 'status': q['status'],
         'answer': q.get('answer'), 'last_reviewed_ts': q.get('last_reviewed_ts')}
        for q in questions.values()
        if isinstance(q.get('last_reviewed_ts'), (int, float))
        and q['last_reviewed_ts'] > now - window_s
    ]
    changed.sort(key=lambda q: -(q['last_reviewed_ts'] or 0))
    by_status = {}
    for q in questions.values():
        by_status[q.get('status') or 'unknown'] = by_status.get(q.get('status') or 'unknown', 0) + 1
    return {
        'window_days': round(window_s / 86400),
        'open_questions': [{'question_id': q['id'], 'text': q['text'],
                            'status': q['status'],
                            'people_who_may_know': q.get('people_who_may_know') or []}
                           for q in open_qs],
        'changed_recently': changed,
        'counts': {'total': len(questions),
                   'open': len(open_qs),
                   'by_status': by_status},
        'at': now,
    }
