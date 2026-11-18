"""The Epistemic Ledger: what the think tank ASSERTS vs what it has
ESTABLISHED.

The evidence ledger (evidence.py) records per-claim findings from sources;
the question registry (questions.py) records what we do not know. Neither
answers the operator's real question: when the tank says something, is it
established, asserted, contested, stale, or still open? This module is that
layer. It does NOT duplicate claims -- it attaches evidence-ledger claims to
propositions by id and re-derives independence from source identity.

One SQLite row (epistemic_ledger, blob JSON -- never the kv_state blob,
mirroring evidence_records). The row goes through `_serve._db()`, so the
storage backend is whatever the deployment runs: remote Postgres via
db_backend.py, SQLite locally. No backend choice is made here.

Four stores in one blob:

1. items        propositions with a stored lifecycle
                unknown -> hypothesized -> evidenced -> validated ->
                superseded, plus disputed / contradicted / blocked /
                unresolved. 'stale' is computed at read time from
                reviewAfterMs (the wiki's review-after convention), never
                stored: a record cannot be stale in one reader's view and
                fresh in another's.
2. validations  the actionable form of an unknown: what to learn, what
                evidence would resolve it, how to get it, what it costs,
                whether it is blocked. Completion is REVIEW-GATED: the
                result does not advance the item until a different reviewer
                approves (the peer-review gate, applied to knowledge).
3. contradictions  two evidence claims that disagree about one item. Open
                until resolved; an LLM may not silently pick a side --
                resolution requires text, an explicit winner (or none),
                and lands in history.
4. decisions    replayable artifacts: rationale, citations, resulting
                actions, and the conditions that would force reconsideration.

The behavioral requirement this module exists for: two agents repeating
information from the same source must never count as independent
corroboration. Independence is computed per ATTACHMENT from the resolved
source identity (evidence.source_identity), so a repost that the evidence
ledger flagged is excluded here too -- and so is any second claim whose
identity matches one already attached, regardless of who filed it.
"""

import functools
import hashlib
import json
import re
import threading
import time

import core.redact as redact

# Stored lifecycle statuses. 'stale' is deliberately absent -- see the module
# docstring; it is derived from reviewAfterMs at read time.
STORED_STATUSES = frozenset({'unknown', 'hypothesized', 'evidenced', 'validated',
                             'superseded', 'disputed', 'contradicted', 'blocked',
                             'unresolved'})

# Item kinds: how the record entered the ledger. An assertion is something
# someone SAID without source-backed evidence -- asserted, never established.
ITEM_KINDS = frozenset({'unknown', 'hypothesis', 'assertion'})

# Validation lifecycle. 'reviewed' is the gate that advances the item.
VALIDATION_STATUSES = frozenset({'proposed', 'running', 'completed',
                                 'rejected', 'reviewed'})

# Allowed stored-status transitions. Everything else is refused: history
# moves forward through named moves, never arbitrary overwrites.
_TRANSITIONS = {
    'unknown': {'hypothesized', 'evidenced', 'blocked', 'unresolved',
                'contradicted', 'superseded'},
    'hypothesized': {'evidenced', 'blocked', 'unresolved', 'contradicted',
                     'superseded'},
    'evidenced': {'validated', 'disputed', 'contradicted', 'superseded',
                  'unresolved'},
    'validated': {'superseded', 'disputed', 'contradicted'},
    'disputed': {'evidenced', 'validated', 'unresolved', 'contradicted',
                 'superseded'},
    'blocked': {'unknown', 'hypothesized', 'evidenced', 'unresolved'},
    'unresolved': {'hypothesized', 'evidenced', 'blocked', 'disputed',
                   'contradicted', 'superseded'},
    'contradicted': {'superseded'},
    'superseded': set(),
}

# The review/expiry policy: a validated item stops counting as trusted
# current knowledge once its review window passes (it stays readable as
# history). Matches the wiki's DEFAULT_REVIEW_DAYS in sim_helpers.
DEFAULT_REVIEW_DAYS = 30
DEFAULT_REVIEW_MS = DEFAULT_REVIEW_DAYS * 86400 * 1000

# Content bounds -- a ledger of unbounded prose is a ledger nobody reads.
MAX_TEXT_CHARS = 2000
MAX_NOTE_CHARS = 2000
MAX_CITATIONS = 32
MAX_HISTORY = 50

_LEDGER_LOCK = threading.RLock()

# Bank service bucket the epistemic writes accrue to (accrued by the serve
# part; this module only sees the gate verdict passed in as cost_ok).
BANK_SERVICE = 'epistemic'


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LEDGER_LOCK:
            return fn(*args, **kwargs)
    return wrapper


# --- storage seam -----------------------------------------------------------

def _ledger_read():
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS epistemic_ledger (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute(
                'SELECT blob FROM epistemic_ledger WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _ledger_write(ledger):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO epistemic_ledger (id, blob, updated_at) '
            'VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, '
            'updated_at = excluded.updated_at',
            (json.dumps(ledger), time.time()),
        )


def _ensure_shape(ledger):
    ledger.setdefault('items', {})
    ledger.setdefault('validations', {})
    ledger.setdefault('contradictions', {})
    ledger.setdefault('decisions', {})
    return ledger


def _audit(actor, action, subject_id=None, detail=None):
    try:
        import core.evidence as evidence
        evidence.append_audit(actor, action, claim_id=subject_id, detail=detail)
    except Exception:
        pass


# --- helpers ----------------------------------------------------------------

def _now_ms():
    return int(time.time() * 1000)


def _clean_text(text):
    return re.sub(r'\s+', ' ', (text or '').strip())[:MAX_TEXT_CHARS]


def _clean_secret(text):
    """Ledger-entry text cleaning WITH edge scrubbing: normalize whitespace,
    cap length, and strip high-confidence secrets before they land in the
    blob (core/redact.py -- scrub at write time, never at read time)."""
    clean, _kinds = redact.scrub(_clean_text(text))
    return clean


def _text_key(text):
    """Dedup identity for item text: the same proposition restated with
    different case/spacing is one item, not two."""
    return _clean_text(text).lower()


def _item_id(text):
    return 'ek-' + hashlib.sha256(_text_key(text).encode()).hexdigest()[:16]


def _next_id(seq, prefix):
    top = 0
    for k in (seq or {}):
        try:
            top = max(top, int(str(k).split('-')[-1]))
        except ValueError:
            continue
    return f'{prefix}-{top + 1}'


def _append_history(item, *, actor, from_status, to_status, note=None):
    history = item.setdefault('history', [])
    history.append({'ts': time.time(), 'actor': (actor or 'system')[:64],
                    'from': from_status, 'to': to_status,
                    'note': (note or '')[:200]})
    item['history'] = history[-MAX_HISTORY:]


def _validate_review_ms(review_after_ms):
    if review_after_ms is None:
        return None
    try:
        ms = int(review_after_ms)
    except (TypeError, ValueError):
        raise ValueError('reviewAfterMs must be an epoch-ms integer')
    if ms < 0:
        raise ValueError('reviewAfterMs must be an epoch-ms integer')
    return ms


def _validate_cost(cost):
    if cost is None:
        return None
    try:
        c = float(cost)
    except (TypeError, ValueError):
        raise ValueError('estimatedCostUsd must be a number')
    if c < 0:
        raise ValueError('estimatedCostUsd must be a number >= 0')
    return round(c, 4)


def _validate_bool(value, name):
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f'{name} must be a boolean')
    return value


def _validate_ids(ids, limit=MAX_CITATIONS):
    if ids is None:
        return []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError('citation ids must be a list of strings')
    return [i for i in ids if i][:limit]


# --- items ------------------------------------------------------------------

def _base_item(text, kind, *, owner, source, actor, review_after_ms):
    now = time.time()
    item = {
        'id': _item_id(text),
        'text': _clean_text(text),
        'kind': kind,
        'status': 'unknown' if kind in ('unknown', 'assertion') else 'hypothesized',
        'owner': (owner or actor or 'unknown')[:120],
        'source': (source or '')[:500] or None,
        'evidence': [],
        'contradictionIds': [],
        'validationIds': [],
        'decisionIds': [],
        'authoritativeSource': None,
        'authoritativeOwner': None,
        'reviewAfterMs': review_after_ms if review_after_ms is not None
                         else _now_ms() + DEFAULT_REVIEW_MS,
        'supersededBy': None,
        'supersedes': None,
        'preDisputeStatus': None,
        'created_ts': now,
        'updated_ts': now,
        'history': [],
    }
    return item


def _record_item(text, kind, *, owner=None, source=None, actor=None,
                 review_after_ms=None):
    text = _clean_secret(text)
    if not text:
        return {'recorded': False, 'reason': 'invalid',
                'error': f'{kind} text is required'}
    try:
        review_after_ms = _validate_review_ms(review_after_ms)
    except ValueError as e:
        return {'recorded': False, 'reason': 'invalid', 'error': str(e)}
    ledger = _ensure_shape(_ledger_read())
    iid = _item_id(text)
    existing = ledger['items'].get(iid)
    now = time.time()
    if existing is not None:
        existing.setdefault('asked_by', []).append((actor or 'unknown')[:64])
        existing['updated_ts'] = now
        ledger['items'][iid] = existing
        _ledger_write(ledger)
        return {'recorded': True, 'id': iid, 'new': False,
                'status': existing['status']}
    item = _base_item(text, kind, owner=owner, source=source, actor=actor,
                      review_after_ms=review_after_ms)
    item.setdefault('asked_by', []).append((actor or 'unknown')[:64])
    _append_history(item, actor=actor, from_status=None, to_status=item['status'],
                    note=f'{kind} recorded')
    ledger['items'][iid] = item
    _ledger_write(ledger)
    _audit(actor, 'epistemic_recorded', subject_id=iid,
           detail={'kind': kind, 'status': item['status']})
    return {'recorded': True, 'id': iid, 'new': True, 'status': item['status']}


@_serialized
def record_unknown(text, *, source=None, owner=None, actor=None,
                   review_after_ms=None):
    """An open question about the world: not known, actionable. This is the
    honest entry point for "we have no idea" -- better than a confident
    guess the ledger cannot challenge later."""
    return _record_item(text, 'unknown', source=source, owner=owner,
                        actor=actor, review_after_ms=review_after_ms)


@_serialized
def record_hypothesis(text, *, based_on_claim_ids=None, owner=None, actor=None,
                      review_after_ms=None):
    """A proposition someone believes MAY be true. A hypothesis is asserted,
    never established: the status records that distinction from day one."""
    return _record_item(text, 'hypothesis', owner=owner, actor=actor,
                        review_after_ms=review_after_ms)


@_serialized
def record_assertion(text, *, source=None, owner=None, actor=None,
                     review_after_ms=None):
    """Something stated as fact WITHOUT source-backed evidence. Recorded as
    kind 'assertion' with status 'unknown': it stays visible as an
    unsupported assumption until evidence arrives -- it can never be read
    as established by virtue of being repeated."""
    return _record_item(text, 'assertion', source=source, owner=owner,
                        actor=actor, review_after_ms=review_after_ms)


@_serialized
def annotate_item(item_id, fields, *, actor=None):
    """Whitelisted metadata annotations on a ledger item. Status is NOT
    annotatable -- transitions go through set_status (or the proper
    action functions) so history stays truthful. Used by the harness
    optimizer for sourceKey/measureBaseline/pageId bookkeeping."""
    allowed = {'sourceKey', 'measureBaseline', 'pageId', 'authoritativeOwner'}
    ledger = _ensure_shape(_ledger_read())
    item = ledger['items'].get(item_id)
    if item is None:
        return {'annotated': False, 'reason': 'unknown_item'}
    changed = []
    for key, value in (fields or {}).items():
        if key in allowed:
            item[key] = value
            changed.append(key)
    if changed:
        item['updated_ts'] = _now_ms()
        _ledger_write(ledger)
    return {'annotated': bool(changed), 'id': item_id,
            'fields': changed}


@_serialized
def set_status(item_id, to_status, *, actor=None, note=None):
    """One explicit, guarded status transition for callers that advance a
    record outside the action functions (e.g. the harness optimizer's
    hypothesis -> evidenced once the registry page exists). Enforces the
    same TRANSITIONS map and records history."""
    ledger = _ensure_shape(_ledger_read())
    item = ledger['items'].get(item_id)
    if item is None:
        return {'advanced': False, 'reason': 'unknown_item'}
    if to_status not in STORED_STATUSES:
        return {'advanced': False, 'reason': 'invalid_status'}
    if to_status in ('validated', 'superseded'):
        # Gated transitions: 'validated' only through review_validation
        # (reviewer independence), 'superseded' only through supersede_item
        # (cross-link integrity). A generic status setter must not shortcut
        # the invariants the whole ledger rests on.
        return {'advanced': False, 'reason': 'gated_transition',
                'to': to_status}
    if to_status == item['status']:
        return {'advanced': False, 'reason': 'no_change'}
    allowed = _TRANSITIONS.get(item['status']) or set()
    if to_status not in allowed:
        return {'advanced': False, 'reason': 'invalid_transition',
                'from': item['status'], 'to': to_status}
    frm = item['status']
    item['status'] = to_status
    if to_status == 'evidenced':
        item['reviewAfterMs'] = _now_ms() + DEFAULT_REVIEW_MS
    _append_history(item, actor=actor, from_status=frm, to_status=to_status,
                    note=note)
    ledger['items'][item_id] = item
    _ledger_write(ledger)
    return {'advanced': True, 'id': item_id, 'from': frm, 'to': to_status}


def get_item(item_id):
    return (_ensure_shape(_ledger_read())['items'] or {}).get(item_id)


def get_validation(validation_id):
    return (_ensure_shape(_ledger_read())['validations'] or {}).get(validation_id)


def get_contradiction(contradiction_id):
    return (_ensure_shape(_ledger_read())['contradictions'] or {}).get(contradiction_id)


def get_decision(decision_id):
    return (_ensure_shape(_ledger_read())['decisions'] or {}).get(decision_id)


def effective_status(item, now_ms=None):
    """The stored status plus the computed staleness overlay. An item whose
    review window passed loses trusted-current status ('stale') but keeps
    its history: superseded/contradicted/disputed statuses take precedence
    (their meaning is stronger than mere staleness), and unknowns and
    hypotheses never go stale -- they were never trusted to begin with."""
    now_ms = now_ms if now_ms is not None else _now_ms()
    status = item.get('status')
    if status in ('evidenced', 'validated'):
        review = item.get('reviewAfterMs')
        if isinstance(review, (int, float)) and now_ms > review:
            return 'stale'
    return status


def _attach(item, entry, *, supports, actor, review_after_ms):
    """Append one evidence attachment with identity-level independence.
    Two attachments sharing a resolved identity are ONE source -- the
    anti-false-corroboration rule -- so the second only records the repeat."""
    prior_identities = {e.get('identity') for e in item.get('evidence', [])}
    entry = dict(entry)
    entry['independent'] = entry.get('identity') not in prior_identities
    entry['supports'] = bool(supports)
    entry['ts'] = time.time()
    entry.setdefault('actor', (actor or 'unknown')[:64])
    item.setdefault('evidence', []).append(entry)
    if review_after_ms is not None:
        item['reviewAfterMs'] = review_after_ms
    item['updated_ts'] = time.time()
    return entry


def _advance_on_evidence(item, actor, review_after_ms):
    """Advance an item that just gained its first independent supporting
    source. Items already evidenced/validated only gain corroboration; a
    disputed item stays disputed until its contradiction resolves."""
    if effective_status(item) in ('disputed', 'contradicted', 'superseded',
                                  'stale', 'evidenced', 'validated'):
        return None
    if independent_source_count(item) < 1:
        return None
    frm = item['status']
    item['status'] = 'evidenced'
    if review_after_ms is None:
        item['reviewAfterMs'] = _now_ms() + DEFAULT_REVIEW_MS
    _append_history(item, actor=actor, from_status=frm, to_status='evidenced',
                    note='independent evidence attached')
    return 'evidenced'


def _independent_sources(item):
    """Distinct resolved source identities among supporting attachments.
    THE corroboration rule: same identity repeated N times is 1 source."""
    return {e.get('identity') for e in (item.get('evidence') or [])
            if e.get('supports') and e.get('identity')}


def independent_source_count(item):
    return len(_independent_sources(item))


@_serialized
def attach_evidence(item_id, claim_id, *, supports=True, actor=None,
                    review_after_ms=None):
    """Attach an evidence-ledger claim to a proposition. The claim must
    exist in the evidence ledger -- an attachment without a recorded claim
    is not evidence, it is the same unsupported assertion with better
    handwriting. Returns {'attached': True, 'id', 'corroborated',
    'independentSources', 'status'}."""
    ledger = _ensure_shape(_ledger_read())
    item = ledger['items'].get(item_id)
    if item is None:
        return {'attached': False, 'reason': 'unknown_item', 'id': item_id}
    if item['status'] in ('superseded', 'contradicted'):
        return {'attached': False, 'reason': 'closed',
                'error': f'item status is {item["status"]}; record a new item'}
    try:
        review_after_ms = _validate_review_ms(review_after_ms)
        supports = _validate_bool(supports, 'supports')
    except ValueError as e:
        return {'attached': False, 'reason': 'invalid', 'error': str(e)}
    import core.evidence as evidence
    claim = evidence.get_claim(claim_id)
    if claim is None:
        return {'attached': False, 'reason': 'unknown_claim',
                'error': f'no evidence claim {claim_id!r}; record it first '
                         '(evidence.record_source_claim)'}
    item = dict(item)
    entry = _attach(item, {'claimId': claim_id, 'identity': claim.get('identity'),
                           'sourceHost': claim.get('source_host'),
                           'actor': actor}, supports=supports, actor=actor,
                    review_after_ms=review_after_ms)
    new_status = None
    if supports:
        new_status = _advance_on_evidence(item, actor, review_after_ms)
        if new_status:
            entry['advancedTo'] = new_status
    ledger['items'][item_id] = item
    _ledger_write(ledger)
    _audit(actor, 'epistemic_evidence', subject_id=item_id,
           detail={'claimId': claim_id, 'identity': claim.get('identity'),
                   'independent': entry['independent'], 'supports': supports})
    return {'attached': True, 'id': item_id,
            'corroborated': not entry['independent'],
            'independentSources': independent_source_count(item),
            'status': effective_status(item), 'claimId': claim_id}


@_serialized
def attach_evidence_from_source(item_id, source_url, claim_text, *,
                                resolved_url=None, supports=True, actor=None,
                                review_after_ms=None):
    """Record a claim in the evidence ledger from a raw source, then attach
    it. Convenience for an agent that has a source in hand and no claim id
    yet: the evidence ledger still owns identity/repost rules, and the
    returned result carries repost_of so the caller sees when it did NOT
    count as a new independent source."""
    import core.evidence as evidence
    result = evidence.record_source_claim(
        {'source_url': source_url, 'claim': claim_text,
         'resolved_url': resolved_url}, actor=actor)
    if not result.get('recorded'):
        return {'attached': False, 'reason': 'claim_not_recorded',
                'detail': result}
    out = attach_evidence(item_id, result['id'], supports=supports,
                          actor=actor, review_after_ms=review_after_ms)
    out['claim'] = result
    return out


# --- validations ------------------------------------------------------------

@_serialized
def propose_validation(item_id, procedure, *, needed_evidence=None,
                       obtainment=None, blocked=False, blocked_reason=None,
                       estimated_cost_usd=None, risk=None, worth_it=None,
                       worth_it_note=None, actor=None):
    """Turn an unknown into a plan: what to learn, what evidence would
    resolve it, how to get it, whether that path is blocked, its expected
    cost and risk, and whether resolving the uncertainty is worth the cost.
    A blocked proposal still records the unknown as actionable-with-a-plan
    -- it surfaces in the blocked view so the operator can unblock it."""
    ledger = _ensure_shape(_ledger_read())
    item = ledger['items'].get(item_id)
    if item is None:
        return {'proposed': False, 'reason': 'unknown_item', 'id': item_id}
    procedure = _clean_secret(procedure)
    if not procedure:
        return {'proposed': False, 'reason': 'invalid',
                'error': 'procedure text is required'}
    try:
        blocked = _validate_bool(blocked, 'blocked')
        worth_it = _validate_bool(worth_it, 'worthIt')
        cost = _validate_cost(estimated_cost_usd)
    except ValueError as e:
        return {'proposed': False, 'reason': 'invalid', 'error': str(e)}
    item = dict(item)
    vid = _next_id(ledger['validations'], 'val')
    validation = {
        'id': vid,
        'itemId': item_id,
        'procedure': procedure,
        'neededEvidence': _clean_secret(needed_evidence) or None,
        'obtainment': _clean_secret(obtainment) or None,
        'blocked': bool(blocked),
        'blockedReason': _clean_text(blocked_reason) or None,
        'estimatedCostUsd': cost,
        'risk': _clean_text(risk) or None,
        'worthIt': worth_it,
        'worthItNote': _clean_text(worth_it_note) or None,
        'status': 'proposed',
        'result': None,
        'resultClaimId': None,
        'createdBy': (actor or 'unknown')[:64],
        'createdAt': time.time(),
        'startedTs': None,
        'completedTs': None,
        'completedBy': None,
        'reviewedBy': None,
        'reviewTs': None,
        'reviewNote': None,
    }
    item.setdefault('validationIds', []).append(vid)
    _append_history(item, actor=actor, from_status=item['status'],
                    to_status=item['status'],
                    note=f'validation proposed: {procedure[:120]}')
    ledger['items'][item_id] = item
    ledger['validations'][vid] = validation
    _ledger_write(ledger)
    _audit(actor, 'epistemic_validation', subject_id=vid,
           detail={'itemId': item_id, 'blocked': bool(blocked), 'cost': cost})
    return {'proposed': True, 'id': vid, 'itemId': item_id}


@_serialized
def start_validation(validation_id, *, cost_ok=True, actor=None):
    """Begin running a validation. The Bank verdict arrives as cost_ok (the
    serve layer asks the Bank; this module stays out of spend accounting).
    False means the tank cannot afford uncertainty reduction right now: the
    validation parks as blocked with the reason on record instead of
    silently running beyond the budget."""
    ledger = _ensure_shape(_ledger_read())
    validation = ledger['validations'].get(validation_id)
    if validation is None:
        return {'started': False, 'reason': 'unknown_validation'}
    if validation['status'] not in ('proposed',):
        return {'started': False, 'reason': 'wrong_status',
                'status': validation['status']}
    item = ledger['items'].get(validation['itemId'])
    if item is not None and item['status'] in ('superseded', 'contradicted'):
        return {'started': False, 'reason': 'item_closed',
                'status': item['status']}
    validation = dict(validation)
    if validation.get('blocked'):
        return {'started': False, 'reason': 'blocked',
                'blockedReason': validation.get('blockedReason')}
    try:
        cost_ok = _validate_bool(cost_ok, 'costOk')
    except ValueError as e:
        return {'started': False, 'reason': 'invalid', 'error': str(e)}
    if not cost_ok:
        validation['status'] = 'proposed'
        validation['blocked'] = True
        validation['blockedReason'] = 'bank budget exhausted (epistemic lane)'
        _audit(actor, 'epistemic_validation', subject_id=validation_id,
               detail={'blocked': 'bank_budget'})
        ledger['validations'][validation_id] = validation
        _ledger_write(ledger)
        return {'started': False, 'reason': 'bank_budget_exhausted'}
    validation['status'] = 'running'
    validation['startedTs'] = time.time()
    ledger['validations'][validation_id] = validation
    _ledger_write(ledger)
    return {'started': True, 'id': validation_id, 'status': 'running'}


@_serialized
def complete_validation(validation_id, result_text, *, claim_id=None,
                        source_url=None, claim_text=None, resolved_url=None,
                        actor=None):
    """File a validation result. The result becomes evidence: either an
    existing evidence-ledger claim id, or a new claim recorded from
    source_url. A result with NO evidence is a claim that the review gate
    will (correctly) reject -- the whole point is that results cite where
    the new knowledge came from. The item is NOT advanced here: completion
    only marks the validation completed-and-awaiting-review. A different
    agent must review it before the parent proposition becomes validated
    (peer review, applied to knowledge)."""
    result_text = _clean_secret(result_text)
    if not result_text:
        return {'completed': False, 'reason': 'invalid',
                'error': 'result text is required'}
    ledger = _ensure_shape(_ledger_read())
    validation = ledger['validations'].get(validation_id)
    if validation is None:
        return {'completed': False, 'reason': 'unknown_validation'}
    if validation['status'] not in ('running', 'proposed'):
        return {'completed': False, 'reason': 'wrong_status',
                'status': validation['status']}
    validation = dict(validation)
    if claim_id:
        import core.evidence as evidence
        if evidence.get_claim(claim_id) is None:
            return {'completed': False, 'reason': 'unknown_claim',
                    'error': f'no evidence claim {claim_id!r}'}
    elif source_url:
        claim_text = claim_text or result_text
        import core.evidence as evidence
        claim_result = evidence.record_source_claim(
            {'source_url': source_url, 'claim': claim_text,
             'resolved_url': resolved_url}, actor=actor)
        if not claim_result.get('recorded'):
            return {'completed': False, 'reason': 'claim_not_recorded',
                    'detail': claim_result}
        claim_id = claim_result['id']
    else:
        return {'completed': False, 'reason': 'invalid',
                'error': 'a result must cite an evidence claim: pass claimId '
                         'or sourceUrl'}
    validation['status'] = 'completed'
    validation['result'] = result_text
    validation['resultClaimId'] = claim_id
    validation['completedBy'] = (actor or 'unknown')[:64]
    validation['completedTs'] = time.time()
    ledger['validations'][validation_id] = validation
    item = ledger['items'].get(validation['itemId'])
    if item is not None:
        item = dict(item)
        _append_history(item, actor=actor, from_status=item['status'],
                        to_status=item['status'],
                        note=f'validation {validation_id} completed; '
                             'awaiting review')
        ledger['items'][validation['itemId']] = item
    _ledger_write(ledger)
    _audit(actor, 'epistemic_validation', subject_id=validation_id,
           detail={'completed': True, 'resultClaimId': claim_id})
    return {'completed': True, 'id': validation_id, 'status': 'completed',
            'resultClaimId': claim_id, 'awaitingReview': True}


@_serialized
def review_validation(validation_id, approve, *, reviewer=None, note=None,
                      authoritative_owner=None):
    """The review gate on knowledge. approve=True advances the parent item
    to 'validated' (its first independent source is now reviewed result
    evidence); reject leaves the item where it is and marks the validation
    rejected with the reviewer's note. Reviewer independence is enforced:
    the agent that completed a validation may not review it, the same rule
    the peer-review gate applies to work."""
    if not isinstance(approve, bool):
        return {'reviewed': False, 'reason': 'invalid',
                'error': 'approve must be a boolean'}
    ledger = _ensure_shape(_ledger_read())
    validation = ledger['validations'].get(validation_id)
    if validation is None:
        return {'reviewed': False, 'reason': 'unknown_validation'}
    if validation['status'] != 'completed':
        return {'reviewed': False, 'reason': 'wrong_status',
                'status': validation['status']}
    reviewer = (reviewer or 'unknown')[:64]
    if validation.get('completedBy') and reviewer == validation['completedBy']:
        return {'reviewed': False, 'reason': 'reviewer_independence',
                'error': f'{reviewer} completed this validation and cannot '
                         'review it'}
    validation = dict(validation)
    validation['reviewedBy'] = reviewer
    validation['reviewTs'] = time.time()
    validation['reviewNote'] = _clean_text(note) or None
    validation['status'] = 'reviewed' if approve else 'rejected'
    ledger['validations'][validation_id] = validation
    item = ledger['items'].get(validation['itemId'])
    item_status = None
    if approve and item is not None:
        item = dict(item)
        if item['status'] in ('disputed', 'contradicted', 'superseded'):
            # The dispute/refutation still stands: a review cannot silently
            # overturn an open contradiction. Resolve the contradiction
            # first (resolve_contradiction), or supersede the item.
            validation['status'] = 'reviewed'
            _append_history(item, actor=reviewer, from_status=item['status'],
                            to_status=item['status'],
                            note=f'validation {validation_id} reviewed; item '
                                 'stays disputed until contradiction resolves')
        else:
            frm = item['status']
            item['status'] = 'validated'
            if authoritative_owner:
                item['authoritativeOwner'] = _clean_text(authoritative_owner)[:120]
            item['reviewAfterMs'] = _now_ms() + DEFAULT_REVIEW_MS
            item_status = 'validated'
            _append_history(item, actor=reviewer, from_status=frm,
                            to_status='validated',
                            note=f'validation {validation_id} approved')
    ledger['items'][validation['itemId']] = item or ledger['items'].get(validation['itemId'])
    _ledger_write(ledger)
    _audit(reviewer, 'epistemic_validated', subject_id=validation['itemId'],
           detail={'validationId': validation_id, 'approve': approve,
                   'itemStatus': item_status})
    return {'reviewed': True, 'id': validation_id,
            'status': validation['status'], 'itemStatus': item_status}


# --- contradictions ---------------------------------------------------------

@_serialized
def record_contradiction(item_id, claim_id_a, claim_id_b, *, note=None,
                         actor=None):
    """Two evidence claims disagree about one proposition. Both sides stay
    attached and visible until a human-or-reviewer resolution lands; the
    item flips to 'disputed' so no downstream reader mistakes either side
    for established. Nothing here picks a winner automatically."""
    ledger = _ensure_shape(_ledger_read())
    item = ledger['items'].get(item_id)
    if item is None:
        return {'recorded': False, 'reason': 'unknown_item', 'id': item_id}
    if item['status'] in ('superseded',):
        return {'recorded': False, 'reason': 'closed', 'status': item['status']}
    import core.evidence as evidence
    for cid in (claim_id_a, claim_id_b):
        if evidence.get_claim(cid) is None:
            return {'recorded': False, 'reason': 'unknown_claim',
                    'error': f'no evidence claim {cid!r}'}
    item = dict(item)
    cid = _next_id(ledger['contradictions'], 'cx')
    contradiction = {
        'id': cid,
        'itemId': item_id,
        'claimIdA': claim_id_a,
        'claimIdB': claim_id_b,
        'note': _clean_secret(note) or None,
        'status': 'open',
        'openedBy': (actor or 'unknown')[:64],
        'openedAt': time.time(),
        'resolution': None,
    }
    item.setdefault('contradictionIds', []).append(cid)
    if item['status'] not in ('disputed',):
        item['preDisputeStatus'] = item['status']
    frm = item['status']
    item['status'] = 'disputed'
    _append_history(item, actor=actor, from_status=frm, to_status='disputed',
                    note=f'contradiction {cid} opened')
    ledger['items'][item_id] = item
    ledger['contradictions'][cid] = contradiction
    _ledger_write(ledger)
    _audit(actor, 'epistemic_contradiction', subject_id=cid,
           detail={'itemId': item_id, 'claimIdA': claim_id_a,
                   'claimIdB': claim_id_b})
    return {'recorded': True, 'id': cid, 'itemId': item_id,
            'itemStatus': 'disputed'}


@_serialized
def resolve_contradiction(contradiction_id, resolution_text, *,
                          winner_claim_id=None, validation_id=None,
                          actor=None):
    """Resolve an open contradiction WITH a written resolution. The
    winner (if any) must be one of the recorded sides -- an LLM cannot
    resolve by inventing a third version, and cannot resolve silently:
    the resolution text is required and lands in history. winner=None
    means "neither side is trusted" and the item drops to 'unresolved'.
    A validation_id records which validation justified the resolution."""
    resolution_text = _clean_secret(resolution_text)
    if not resolution_text:
        return {'resolved': False, 'reason': 'invalid',
                'error': 'resolution text is required'}
    ledger = _ensure_shape(_ledger_read())
    cx = ledger['contradictions'].get(contradiction_id)
    if cx is None:
        return {'resolved': False, 'reason': 'unknown_contradiction'}
    if cx['status'] != 'open':
        return {'resolved': False, 'reason': 'wrong_status',
                'status': cx['status']}
    sides = (cx['claimIdA'], cx['claimIdB'])
    if winner_claim_id is not None and winner_claim_id not in sides:
        return {'resolved': False, 'reason': 'invalid',
                'error': 'winner must be one of the two recorded claims '
                         f'({sides[0]}, {sides[1]}) or None'}
    cx = dict(cx)
    cx['status'] = 'resolved'
    cx['resolution'] = {
        'text': resolution_text,
        'winnerClaimId': winner_claim_id,
        'validationId': validation_id,
        'resolvedBy': (actor or 'unknown')[:64],
        'resolvedAt': time.time(),
    }
    ledger['contradictions'][contradiction_id] = cx
    item = ledger['items'].get(cx['itemId'])
    item_status = None
    if item is not None:
        item = dict(item)
        other_open = [c for c in (item.get('contradictionIds') or [])
                      if c != contradiction_id
                      and (ledger['contradictions'].get(c) or {}).get('status') == 'open']
        if other_open:
            _append_history(item, actor=actor, from_status=item['status'],
                            to_status=item['status'],
                            note=f'contradiction {contradiction_id} resolved; '
                                 f'{other_open[0]} still open')
        else:
            frm = item['status']
            if winner_claim_id and item.get('preDisputeStatus') in ('evidenced', 'validated'):
                item['status'] = item['preDisputeStatus']
            elif winner_claim_id:
                item['status'] = 'evidenced'
            else:
                item['status'] = 'unresolved'
            item['preDisputeStatus'] = None
            item_status = item['status']
            _append_history(item, actor=actor, from_status=frm,
                            to_status=item_status,
                            note=f'contradiction {contradiction_id} resolved')
        ledger['items'][cx['itemId']] = item
    _ledger_write(ledger)
    _audit(actor, 'epistemic_contradiction_resolved',
           subject_id=contradiction_id,
           detail={'itemId': cx['itemId'], 'winner': winner_claim_id,
                   'itemStatus': item_status})
    return {'resolved': True, 'id': contradiction_id, 'itemStatus': item_status}


# --- supersede / staleness --------------------------------------------------

@_serialized
def supersede_item(item_id, replacement_text, *, actor=None, owner=None):
    """The correction move on a proposition: a validated/evidenced item is
    no longer current truth. The old item stays readable forever with
    status 'superseded' and a supersededBy pointer; the replacement is a
    fresh item that must earn its own status from the bottom (evidence,
    validation, review) -- supersession is never a free pass to
    'validated'."""
    replacement_text = _clean_secret(replacement_text)
    if not replacement_text:
        return {'superseded': False, 'reason': 'invalid',
                'error': 'replacement text is required'}
    ledger = _ensure_shape(_ledger_read())
    old = ledger['items'].get(item_id)
    if old is None:
        return {'superseded': False, 'reason': 'unknown_item'}
    if old['status'] == 'superseded':
        return {'superseded': False, 'reason': 'already_superseded',
                'supersededBy': old.get('supersededBy')}
    result = _record_item(replacement_text, old['kind'], owner=owner,
                          actor=actor)
    if not result.get('recorded'):
        return {'superseded': False, 'reason': 'replacement_not_recorded',
                'detail': result}
    new_id = result['id']
    old = dict(old)
    old['status'] = 'superseded'
    old['supersededBy'] = new_id
    _append_history(old, actor=actor, from_status=old['status'],
                    to_status='superseded', note=f'superseded by {new_id}')
    ledger['items'][item_id] = old
    fresh = ledger['items'][new_id]
    fresh['supersedes'] = item_id
    ledger['items'][new_id] = fresh
    _ledger_write(ledger)
    _audit(actor, 'epistemic_superseded', subject_id=item_id,
           detail={'supersededBy': new_id})
    return {'superseded': True, 'id': item_id, 'newId': new_id}


@_serialized
def refresh_stale(item_id, *, review_after_ms=None, actor=None):
    """A stale item was re-verified against a current source. The window
    resets and the item counts as trusted current knowledge again; the
    history records the refresh so staleness episodes stay auditable."""
    ledger = _ensure_shape(_ledger_read())
    item = ledger['items'].get(item_id)
    if item is None:
        return {'refreshed': False, 'reason': 'unknown_item'}
    try:
        review_after_ms = _validate_review_ms(review_after_ms)
    except ValueError as e:
        return {'refreshed': False, 'reason': 'invalid', 'error': str(e)}
    item = dict(item)
    frm = effective_status(item)
    if frm not in ('stale', 'evidenced', 'validated'):
        return {'refreshed': False, 'reason': 'wrong_status', 'status': frm}
    item['reviewAfterMs'] = review_after_ms if review_after_ms is not None \
        else _now_ms() + DEFAULT_REVIEW_MS
    _append_history(item, actor=actor, from_status=frm,
                    to_status=effective_status(item, item['reviewAfterMs']),
                    note='stale review window reset')
    ledger['items'][item_id] = item
    _ledger_write(ledger)
    _audit(actor, 'epistemic_stale_refresh', subject_id=item_id,
           detail={'reviewAfterMs': item['reviewAfterMs']})
    return {'refreshed': True, 'id': item_id,
            'status': effective_status(item, item['reviewAfterMs'])}


# --- decisions --------------------------------------------------------------

@_serialized
def record_decision(text, *, rationale=None, item_ids=None, claim_ids=None,
                    validation_ids=None, contradiction_ids=None,
                    reconsider_when=None, reconsider_triggers=None,
                    actions=None, actor=None):
    """A decision as a replayable artifact: what was decided, why, on which
    evidence/claims/unknowns/validations/contradictions, what to do about
    it, and what would force reconsideration. Citations are validated
    against the ledger -- a decision citing unknown records is noise.
    Actions (resulting work) start 'open'."""
    text = _clean_text(text)
    text = _clean_secret(text)
    if not text:
        return {'recorded': False, 'reason': 'invalid',
                'error': 'decision text is required'}
    item_ids = _validate_ids(item_ids)
    claim_ids = _validate_ids(claim_ids)
    validation_ids = _validate_ids(validation_ids)
    contradiction_ids = _validate_ids(contradiction_ids)
    triggers = _validate_ids(reconsider_triggers, limit=8)
    ledger = _ensure_shape(_ledger_read())
    for iid in item_ids:
        if iid not in ledger['items']:
            return {'recorded': False, 'reason': 'unknown_citation',
                    'error': f'no item {iid!r}'}
    for vid in validation_ids:
        if vid not in ledger['validations']:
            return {'recorded': False, 'reason': 'unknown_citation',
                    'error': f'no validation {vid!r}'}
    for cxid in contradiction_ids:
        if cxid not in ledger['contradictions']:
            return {'recorded': False, 'reason': 'unknown_citation',
                    'error': f'no contradiction {cxid!r}'}
    import core.evidence as evidence
    for cid in claim_ids:
        if evidence.get_claim(cid) is None:
            return {'recorded': False, 'reason': 'unknown_citation',
                    'error': f'no evidence claim {cid!r}'}
    did = _next_id(ledger['decisions'], 'dc')
    decision = {
        'id': did,
        'text': text,
        'rationale': _clean_secret(rationale) or None,
        'citations': {'itemIds': item_ids, 'claimIds': claim_ids,
                      'validationIds': validation_ids,
                      'contradictionIds': contradiction_ids},
        'reconsiderWhen': _clean_secret(reconsider_when) or None,
        'reconsiderTriggers': triggers,
        'actions': [],
        'createdBy': (actor or 'unknown')[:64],
        'createdAt': time.time(),
    }
    ledger['decisions'][did] = decision
    for iid in item_ids:
        item = ledger['items'].get(iid)
        if item is not None:
            item = dict(item)
            item.setdefault('decisionIds', []).append(did)
            ledger['items'][iid] = item
    _ledger_write(ledger)
    _audit(actor, 'epistemic_decision', subject_id=did,
           detail={'items': item_ids})
    for action_spec in (actions or []):
        # Accept plain strings or {'text': ...} dicts (structured callers).
        action_text = action_spec if isinstance(action_spec, str) \
            else (action_spec or {}).get('text')
        out = record_action(did, action_text, actor=actor)
        if not out.get('recorded'):
            break
    return {'recorded': True, 'id': did,
            'actionIds': [a['id'] for a in
                          (ledger['decisions'][did].get('actions') or [])]}


@_serialized
def record_action(decision_id, text, *, actor=None):
    """One resulting action from a decision: recommended next work, kept
    open until explicitly completed. Unknowns that generated validations
    are covered there; this is for the DECISION's follow-through."""
    text = _clean_secret(text)
    if not text:
        return {'recorded': False, 'reason': 'invalid',
                'error': 'action text is required'}
    ledger = _ensure_shape(_ledger_read())
    decision = ledger['decisions'].get(decision_id)
    if decision is None:
        return {'recorded': False, 'reason': 'unknown_decision'}
    decision = dict(decision)
    actions = list(decision.get('actions') or [])
    aid = _next_id({a['id']: True for a in actions}, 'act')
    actions.append({'id': aid, 'text': text, 'status': 'open',
                    'ts': time.time(), 'createdBy': (actor or 'unknown')[:64],
                    'doneTs': None, 'doneBy': None})
    decision['actions'] = actions
    ledger['decisions'][decision_id] = decision
    _ledger_write(ledger)
    _audit(actor, 'epistemic_action', subject_id=decision_id,
           detail={'actionId': aid, 'text': text[:200]})
    return {'recorded': True, 'id': aid, 'decisionId': decision_id}


@_serialized
def complete_action(decision_id, action_id, *, actor=None):
    ledger = _ensure_shape(_ledger_read())
    decision = ledger['decisions'].get(decision_id)
    if decision is None:
        return {'completed': False, 'reason': 'unknown_decision'}
    decision = dict(decision)
    actions = []
    found = False
    for a in decision.get('actions') or []:
        if a.get('id') == action_id:
            a = dict(a)
            a['status'] = 'done'
            a['doneTs'] = time.time()
            a['doneBy'] = (actor or 'unknown')[:64]
            found = True
        actions.append(a)
    if not found:
        return {'completed': False, 'reason': 'unknown_action'}
    decision['actions'] = actions
    ledger['decisions'][decision_id] = decision
    _ledger_write(ledger)
    return {'completed': True, 'id': action_id, 'decisionId': decision_id}


def replay_decision(decision_id):
    """The replay view: the decision artifact plus the CURRENT state of
    every cited record, so a reader can see what has changed since the
    decision was made (an item cited as validated that is now stale or
    disputed shows up immediately)."""
    ledger = _ensure_shape(_ledger_read())
    decision = ledger['decisions'].get(decision_id)
    if decision is None:
        return None
    now_ms = _now_ms()
    items = {}
    for iid in decision.get('citations', {}).get('itemIds') or []:
        it = ledger['items'].get(iid)
        if it:
            items[iid] = {'text': it['text'],
                          'statusAtDecision': None,
                          'statusNow': effective_status(it, now_ms)}
    citations = dict(decision.get('citations') or {})
    for cxid in citations.get('contradictionIds') or []:
        c = ledger['contradictions'].get(cxid)
        if c:
            items.setdefault(cxid, {'text': f'contradiction {cxid}',
                                    'statusAtDecision': None,
                                    'statusNow': c['status']})
    return {'decision': decision, 'citedNow': items, 'replayedAtMs': now_ms}


# --- snapshot / brief -------------------------------------------------------

def _item_view(item, now_ms, contradictions):
    sources = [{'identity': e.get('identity'), 'claimId': e.get('claimId'),
                'independent': e.get('independent'),
                'host': e.get('sourceHost')}
               for e in (item.get('evidence') or []) if e.get('supports')]
    open_cx = [c for c in (item.get('contradictionIds') or [])
               if (contradictions.get(c) or {}).get('status') == 'open']
    status = effective_status(item, now_ms)
    return {
        'id': item['id'],
        'text': item['text'],
        'kind': item.get('kind'),
        'status': status,
        'independentSources': independent_source_count(item),
        'sources': sources,
        'owner': item.get('owner'),
        'authoritativeOwner': item.get('authoritativeOwner'),
        'reviewAfterMs': item.get('reviewAfterMs'),
        'openContradictions': open_cx,
        'validationIds': item.get('validationIds') or [],
        'decisionIds': item.get('decisionIds') or [],
        'supersededBy': item.get('supersededBy'),
        # Optimizer bookkeeping (see annotate_item) surfaced so views can
        # dedupe and measure without re-reading raw records.
        'sourceKey': item.get('sourceKey'),
        'measureBaseline': item.get('measureBaseline'),
        'pageId': item.get('pageId'),
    }


def epistemic_snapshot(now_ms=None):
    """The whole epistemic state, grouped by what a reader must NOT confuse:
    established (validated, fresh, uncontradicted) vs asserted (hypotheses
    and unsupported assumptions) vs unknown vs contested vs stale. This is
    the surface the benchmark grades and the agent brief renders."""
    now_ms = now_ms if now_ms is not None else _now_ms()
    ledger = _ensure_shape(_ledger_read())
    items = ledger['items']
    snap = {'established': [], 'asserted': [], 'unknowns': [], 'disputed': [],
            'contradicted': [], 'stale': [], 'blocked': [], 'superseded': [],
            'openContradictions': [], 'validationsRequired': [],
            'validationsBlocked': [], 'validationsAwaitingReview': [],
            'decisions': [], 'openActions': []}
    views = {}
    contradictions = ledger['contradictions']
    for item in items.values():
        view = _item_view(item, now_ms, contradictions)
        views[item['id']] = view
        status = view['status']
        if status == 'validated':
            snap['established'].append(view)
        elif status == 'disputed':
            snap['disputed'].append(view)
        elif status == 'contradicted':
            snap['contradicted'].append(view)
        elif status == 'stale':
            snap['stale'].append(view)
        elif status == 'superseded':
            snap['superseded'].append(view)
        elif status == 'unresolved':
            snap['unknowns'].append(view)
        elif status == 'blocked':
            snap['blocked'].append(view)
        elif status == 'hypothesized':
            snap['asserted'].append(view)
        elif status == 'evidenced':
            # Supported but NOT validated: asserted-not-established, the
            # middle ground readers most often misread as known.
            snap['asserted'].append(view)
        else:  # 'unknown'
            if item.get('kind') == 'assertion':
                snap['asserted'].append(view)
            else:
                snap['unknowns'].append(view)
    for cx in ledger['contradictions'].values():
        if cx.get('status') == 'open':
            snap['openContradictions'].append({
                'id': cx['id'], 'itemId': cx['itemId'],
                'text': (items.get(cx['itemId']) or {}).get('text'),
                'claimIdA': cx['claimIdA'], 'claimIdB': cx['claimIdB'],
                'note': cx.get('note')})
    for v in ledger['validations'].values():
        if v.get('status') == 'proposed' and not v.get('blocked'):
            snap['validationsRequired'].append(_validation_view(v))
        elif v.get('status') == 'proposed' and v.get('blocked'):
            snap['validationsBlocked'].append(_validation_view(v))
        elif v.get('status') in ('running', 'completed'):
            snap['validationsAwaitingReview'].append(_validation_view(v))
    for d in ledger['decisions'].values():
        snap['decisions'].append({
            'id': d['id'], 'text': d['text'],
            'reconsiderWhen': d.get('reconsiderWhen')})
        for a in d.get('actions') or []:
            if a.get('status') == 'open':
                snap['openActions'].append(
                    {'id': a['id'], 'decisionId': d['id'], 'text': a['text']})
    return snap


def _validation_view(v):
    return {'id': v['id'], 'itemId': v['itemId'],
            'procedure': v['procedure'],
            'neededEvidence': v.get('neededEvidence'),
            'obtainment': v.get('obtainment'),
            'blockedReason': v.get('blockedReason'),
            'estimatedCostUsd': v.get('estimatedCostUsd'),
            'worthIt': v.get('worthIt')}


def classify_item(item, now_ms=None):
    """One benchmark grade class for an item: Known (validated, fresh,
    uncontradicted), Unknown, Hypothesis, Unsupported Assumption,
    Contradiction, or Stale Evidence."""
    status = effective_status(item, now_ms)
    if status == 'validated':
        return 'Known'
    if status == 'stale':
        return 'Stale Evidence'
    if status == 'disputed':
        return 'Contradiction'
    if status == 'contradicted':
        return 'Contradiction'
    if status == 'hypothesized':
        return 'Hypothesis'
    if item.get('kind') == 'assertion' and status in ('unknown', 'unresolved',
                                                     'blocked'):
        return 'Unsupported Assumption'
    return 'Unknown'


# --- dimension query (the ClickHouse lesson, on the deployment's own SQL) ---

_INDEX_DDL = '''CREATE TABLE IF NOT EXISTS epistemic_index (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    owner TEXT,
    independent_sources INTEGER DEFAULT 0,
    open_contradictions INTEGER DEFAULT 0,
    validation_count INTEGER DEFAULT 0,
    created_ts REAL,
    updated_ts REAL,
    review_after_ms REAL
)'''


def _index_sync(ledger):
    """Mirror item facts into the epistemic_index SQL table so dimension
    queries hit parameterized SQL (Postgres in production, SQLite in dev)
    instead of scanning the blob. Best-effort: an index failure never
    fails the write -- query_items falls back to a blob scan, which is
    correct, just slower. Called from _ledger_write after the blob lands."""
    try:
        import serve as _serve
        with _serve._db() as conn:
            conn.execute(_INDEX_DDL)
            for item in (ledger.get('items') or {}).values():
                conn.execute(
                    'INSERT INTO epistemic_index '
                    '(id, kind, status, owner, independent_sources, '
                    ' open_contradictions, validation_count, created_ts, '
                    ' updated_ts, review_after_ms) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, '
                    'status = excluded.status, owner = excluded.owner, '
                    'independent_sources = excluded.independent_sources, '
                    'open_contradictions = excluded.open_contradictions, '
                    'validation_count = excluded.validation_count, '
                    'created_ts = excluded.created_ts, '
                    'updated_ts = excluded.updated_ts, '
                    'review_after_ms = excluded.review_after_ms',
                    (item['id'], item.get('kind'), item.get('status'),
                     item.get('owner'), independent_source_count(item),
                     len([c for c in (item.get('contradictionIds') or [])]),
                     len(item.get('validationIds') or []),
                     item.get('created_ts'), item.get('updated_ts'),
                     item.get('reviewAfterMs')))
    except Exception:
        pass


def _ledger_write(ledger):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO epistemic_ledger (id, blob, updated_at) '
            'VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, '
            'updated_at = excluded.updated_at',
            (json.dumps(ledger), time.time()),
        )
    _index_sync(ledger)


def query_items(*, status=None, kind=None, owner=None, min_sources=None,
                has_open_contradiction=None, created_after=None,
                created_before=None, order='newest', limit=50):
    """Dimension query over the ledger: filter items by status, kind,
    owner, independent-source count, open contradictions, and creation
    window. Tries the SQL index first (millisecond slices on Postgres);
    falls back to a blob scan when the index is unavailable -- same
    answers, slower path, and hermetic tests need no DB at all. Returns
    full item views plus which path served the query."""
    criteria = dict(status=status, kind=kind, owner=owner,
                    min_sources=min_sources,
                    has_open_contradiction=has_open_contradiction,
                    created_after=created_after, created_before=created_before)
    try:
        ids, source = _index_query(criteria, order, limit)
    except Exception:
        ids, source = None, 'scan'
    ledger = _ensure_shape(_ledger_read())
    items = ledger['items']
    if ids is None:
        matches = [it for it in items.values()
                   if _matches_criteria(it, criteria, _now_ms())]
        matches.sort(key=lambda it: -(it.get('created_ts') or 0)
                     if order == 'newest' else (it.get('created_ts') or 0))
        if order == 'sources':
            matches.sort(key=lambda it: -independent_source_count(it))
        chosen = matches[:max(1, int(limit or 50))]
        contradictions = ledger['contradictions']
        return {'items': [_item_view(it, _now_ms(), contradictions)
                          for it in chosen],
                'source': 'scan', 'total': len(matches)}
    contradictions = ledger['contradictions']
    out = []
    for iid in ids:
        it = items.get(iid)
        if it:
            out.append(_item_view(it, _now_ms(), contradictions))
    return {'items': out, 'source': source, 'total': len(ids)}


def _index_query(criteria, order, limit):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(_INDEX_DDL)
        # Consistency gate: the index must cover the SAME generation of
        # items as the blob seam, or the answer is silently wrong (an
        # empty/stale index answering "nothing matches"). A count mismatch
        # falls back to the blob scan -- correct, just slower.
        count = conn.execute('SELECT COUNT(*) FROM epistemic_index').fetchone()[0]
        if int(count) != len((_ledger_read() or {}).get('items') or {}):
            raise RuntimeError('epistemic_index out of sync with ledger blob')
        where, params = [], []
        if criteria.get('status'):
            where.append('status = ?')
            params.append(criteria['status'])
        if criteria.get('kind'):
            where.append('kind = ?')
            params.append(criteria['kind'])
        if criteria.get('owner'):
            where.append('owner = ?')
            params.append(criteria['owner'])
        if criteria.get('min_sources') is not None:
            where.append('independent_sources >= ?')
            params.append(int(criteria['min_sources']))
        if criteria.get('has_open_contradiction'):
            where.append('open_contradictions > 0')
        if criteria.get('created_after') is not None:
            where.append('created_ts >= ?')
            params.append(float(criteria['created_after']))
        if criteria.get('created_before') is not None:
            where.append('created_ts <= ?')
            params.append(float(criteria['created_before']))
        sql = 'SELECT id FROM epistemic_index'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += {'newest': ' ORDER BY created_ts DESC',
                'oldest': ' ORDER BY created_ts ASC',
                'sources': ' ORDER BY independent_sources DESC',
                }.get(order, ' ORDER BY created_ts DESC')
        sql += ' LIMIT ?'
        params.append(max(1, int(limit or 50)))
        rows = conn.execute(sql, params).fetchall()
        return [r[0] for r in rows], 'index'


def _matches_criteria(item, criteria, now_ms):
    if criteria.get('status') and item.get('status') != criteria['status'] \
            and effective_status(item, now_ms) != criteria['status']:
        return False
    if criteria.get('kind') and item.get('kind') != criteria['kind']:
        return False
    if criteria.get('owner') and item.get('owner') != criteria['owner']:
        return False
    if criteria.get('min_sources') is not None \
            and independent_source_count(item) < int(criteria['min_sources']):
        return False
    if criteria.get('has_open_contradiction') \
            and not (item.get('contradictionIds') or []):
        return False
    if criteria.get('created_after') is not None \
            and (item.get('created_ts') or 0) < float(criteria['created_after']):
        return False
    if criteria.get('created_before') is not None \
            and (item.get('created_ts') or 0) > float(criteria['created_before']):
        return False
    return True


# --- structural evaluator (the online-evaluator lesson, without the LLM) ----

def evaluate_item(item_id, *, apply=False, actor=None):
    """Score a proposition's epistemic standing from STRUCTURAL signals --
    independent-source count, source claim types, open contradictions,
    staleness -- and optionally apply the promotion. This is the tank's
    version of an online evaluator: it scores every item the same way on
    arrival and on demand, without a model call (the expensive judgment
    stays where the tank already puts it: the review gate).

    Promotion is deliberately modest: a strong item moves
    unknown/hypothesized/blocked/unresolved -> evidenced and gets a
    proposed validation if it has none. 'validated' (established) is
    NEVER granted by the evaluator -- that stays behind the peer-review
    gate, which is the whole point of asserted-vs-established."""
    ledger = _ensure_shape(_ledger_read())
    item = ledger['items'].get(item_id)
    if item is None:
        return {'evaluated': False, 'reason': 'unknown_item'}
    now_ms = _now_ms()
    score = 0.0
    reasons = []
    sources = independent_source_count(item)
    if sources >= 1:
        score += 0.35
        reasons.append(f'{sources} independent source(s)')
    if sources >= 2:
        score += 0.35
        reasons.append('multi-source corroboration')
    import core.evidence as evidence
    official = 0
    for e in item.get('evidence') or []:
        claim = evidence.get_claim(e.get('claimId')) or {}
        if claim.get('claim_type') == 'official_announcement':
            official += 1
    if official:
        score += min(0.16, 0.08 * official)
        reasons.append(f'{official} official-announcement claim(s)')
    open_cx = [c for c in (item.get('contradictionIds') or [])
               if (ledger['contradictions'].get(c) or {}).get('status') == 'open']
    if open_cx:
        score = min(score, 0.2)
        reasons.append('contradiction open -- score capped')
    if effective_status(item, now_ms) == 'stale':
        score -= 0.2
        reasons.append('review window expired')
    if item.get('kind') == 'assertion' and sources == 0:
        score = min(score, 0.2)
        reasons.append('unsupported assertion')
    score = max(0.0, min(1.0, round(score, 2)))
    status = effective_status(item, now_ms)
    has_validation = bool(item.get('validationIds'))
    if open_cx:
        recommendation = 'resolve_contradiction'
    elif score >= 0.7 and status in ('unknown', 'hypothesized', 'blocked',
                                     'unresolved'):
        recommendation = 'promote'
    elif score >= 0.5 and status in ('evidenced',) and not has_validation:
        recommendation = 'propose_validation'
    elif score <= 0.3:
        recommendation = 'flag_unsupported'
    else:
        recommendation = 'none'
    applied = False
    if apply and recommendation == 'promote':
        item = dict(item)
        frm = item['status']
        item['status'] = 'evidenced'
        item['reviewAfterMs'] = now_ms + DEFAULT_REVIEW_MS
        _append_history(item, actor=actor or 'evaluator', from_status=frm,
                        to_status='evidenced',
                        note=f'evaluator promotion (score {score})')
        ledger['items'][item_id] = item
        _ledger_write(ledger)
        applied = True
        status = 'evidenced'
    return {'evaluated': True, 'id': item_id, 'score': score,
            'reasons': reasons, 'recommendation': recommendation,
            'applied': applied, 'status': status}


def epistemic_brief(now_ms=None, max_lines=16):
    """The agent-facing text brief: what is established, what is only
    asserted, what conflicts remain open, what is stale, and what is
    blocked. Rendered plain -- every line is checkable against the ledger.
    Empty string when nothing is recorded (callers prepend nothing)."""
    snap = epistemic_snapshot(now_ms)
    lines = []
    for view in snap['established'][:3]:
        lines.append(f"Established: {view['text']} [{view['id']}]")
    for view in snap['unknowns'][:2]:
        lines.append(f"Unknown (do not assert): {view['text']} [{view['id']}]")
    for view in snap['asserted'][:3]:
        lines.append(f"Asserted, NOT established ({view['kind']}): "
                     f"{view['text']} [{view['id']}]")
    for cx in snap['openContradictions'][:2]:
        lines.append(f"CONFLICT unresolved on [{cx['itemId']}]: "
                     f"{cx['claimIdA']} vs {cx['claimIdB']} -- do not treat "
                     'either side as fact until resolved')
    for view in snap['stale'][:2]:
        lines.append(f"Stale (review overdue, do not trust as current): "
                     f"{view['text']} [{view['id']}]")
    for v in snap['validationsBlocked'][:2]:
        lines.append(f"Blocked validation [{v['id']}]: {v['procedure']} "
                     f"({v.get('blockedReason')})")
    for a in snap['openActions'][:2]:
        lines.append(f"Next action: {a['text']} [{a['id']}]")
    if not lines:
        return ''
    lines.insert(0, 'EPISTEMIC STATE (asserted is not established):')
    return '\n'.join(lines[:max_lines + 1])
