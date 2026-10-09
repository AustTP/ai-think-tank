"""The Research Desk: a claim-level evidence ledger and a review-gated
coverage ledger for scheduled research/monitoring work.

Extracted into its own module (like bank.py) so claim/coverage accounting is
independent of the whole-think tank blob and independently testable. All serve
state is reached through `_serve.X` at call time; the two ledgers live in
their own SQLite rows (never the kv_state blob), mirroring kv_spend's
independence.

Two ledgers, one idea ("find something interesting" must not outrun "say
something true"):

1. `evidence_records` -- one row per CLAIM. Each claim carries the source URL
   as found (which may be a short link), the RESOLVED landing URL (the
   identity basis -- a short link's raw string is never an identity, because
   t.co/abc and bit.ly/abc are different landing sites that LOOK identical,
   and a short token can be repointed to a malicious destination at any
   time), a claim_type (official_announcement / user_report / opinion /
   benchmark_claim / other / unclassified), scope, and a status lifecycle
   (needs_review -> cleared | blocked). A claim whose resolved identity
   already has a record is a REPOST (independent=False, repost_of=<first id>):
   it never counts as a second independent source.

2. `coverage_ledger` -- one row per source identity tracking "have we covered
   this?" It is REVIEW-GATED: an item only counts as covered after the brief
   has been reviewed (mark_covered(reviewed=True)). A failed run records a
   failure and NEVER suppresses the next attempt (dedup only suppresses on
   'covered', never on 'failed'). `search_since_ms` applies the 24h overlap
   so recurring runs catch late discoveries.
"""

import hashlib
import json
import re
import sqlite3
import time
import urllib.parse

from web_helpers import _is_short_link

# The lifecycle statuses a claim record can take.
CLAIM_STATUSES = frozenset({'needs_review', 'cleared', 'blocked'})
# The article's claim-type taxonomy. `unclassified` is the honest default at
# crawl time -- classification is a Verifier job, not a crawl-time guess.
CLAIM_TYPES = frozenset({'official_announcement', 'user_report', 'opinion',
                         'benchmark_claim', 'other', 'unclassified'})
# The Verifier's per-record decision (kept separate from status, per the
# article: "Save that decision separately from status").
CLAIM_DECISIONS = frozenset({'usable_as_written', 'usable_with_narrower_wording',
                             'blocked_until_checked'})
# Coverage entry states. Only 'covered' suppresses a re-report.
COVERAGE_STATUSES = frozenset({'pending_review', 'covered', 'failed', 'blocked'})

# Append-only audit of every ledger mutation. Each row is immutable (inserted
# once, never updated or deleted): a compromised agent can ADD rows but can
# never rewrite or erase history, so the audit is the trustworthy trail the
# operator reads. It also powers the per-actor covered-flip rate cap below.
AUDIT_ACTIONS = frozenset({'claim_recorded', 'claim_reviewed',
                           'coverage_pending_review', 'coverage_covered',
                           'coverage_failed'})

# Per-actor cap on COVERED flips within the window: a compromised Verifier
# cannot silently flip the whole ledger to covered in one pass. When an agent
# hits the cap, the player must review the rest.
VERIFIER_COVERED_MAX = 12
VERIFIER_COVERED_WINDOW_S = 6 * 3600  # 6 hours

# Recurring runs search the period since the last successful run, plus a
# 24-hour overlap so a discovery that landed just after last run's window is
# not missed. Matches the article's routine: "a 24-hour overlap to catch late
# discoveries."
DEFAULT_OVERLAP_MS = 24 * 3600 * 1000

# Identity marker for a source whose landing URL could not be resolved. A
# short link with no resolved URL is UNRESOLVED, never a verified source: it
# gets its own opaque identity (so it cannot collide with, or be confused
# for, any other source) and a needs_review/blocked record.
UNRESOLVED_PREFIX = 'unresolved:'

# x.com / twitter.com status-post id extractor. A post id is the most stable
# identity for a single X post (the URL path can carry tracking query junk).
_STATUS_RE = re.compile(r'/(?:status|statuses)/(\d+)')


def _evidence_read():
    """Read the claim ledger from its own evidence_records row. Never the
    whole-think tank blob -- see the kv_spend DDL comment for why accounting
    is independent."""
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS evidence_records (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute('SELECT blob FROM evidence_records WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _evidence_write(records):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO evidence_records (id, blob, updated_at) VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
            (json.dumps(records), time.time()),
        )


def _coverage_read():
    """Read the coverage ledger from its own coverage_ledger row."""
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS coverage_ledger (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute('SELECT blob FROM coverage_ledger WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _coverage_write(records):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO coverage_ledger (id, blob, updated_at) VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
            (json.dumps(records), time.time()),
        )


def _audit_ensure():
    import serve as _serve
    with _serve._db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS evidence_audit (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            claim_id TEXT,
            identity TEXT,
            detail TEXT NOT NULL DEFAULT '{}'
        )''')


def append_audit(actor, action, *, claim_id=None, identity=None, detail=None):
    """Append one immutable row to the evidence audit. Never updated or
    deleted -- the audit is the history the ledgers cannot rewrite. Failures
    are silent: an audit row is a trail, never a gate on the ledger write
    itself."""
    if not action or action not in AUDIT_ACTIONS:
        return None
    try:
        _audit_ensure()
        import serve as _serve
        with _serve._db() as conn:
            cur = conn.execute(
                'INSERT INTO evidence_audit (ts, actor, action, claim_id, identity, detail) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (time.time(), (actor or 'system')[:64], action, claim_id,
                 identity, json.dumps(detail or {})),
            )
            return cur.lastrowid
    except Exception:
        return None


def count_actor_action(actor, action, since_ts):
    """Number of audit rows for `actor`/`action` at/after `since_ts` (epoch
    seconds). Backs the covered-flip rate cap; returns 0 on any failure."""
    if action not in AUDIT_ACTIONS:
        return 0
    try:
        _audit_ensure()
        import serve as _serve
        with _serve._db() as conn:
            row = conn.execute(
                'SELECT COUNT(*) FROM evidence_audit '
                'WHERE actor = ? AND action = ? AND ts >= ?',
                (actor, action, since_ts)).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def list_audit(limit=500):
    """Read the audit trail, newest first. The operator's view of every ledger
    mutation and who made it -- the detection layer for a compromised key."""
    try:
        _audit_ensure()
        import serve as _serve
        with _serve._db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT seq, ts, actor, action, claim_id, identity, detail '
                'FROM evidence_audit ORDER BY seq DESC LIMIT ?', (int(limit),)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _normalize_url(url):
    """A canonical form of a URL for identity comparison: scheme+netloc
    lowercased, default ports dropped, fragment removed, trailing slash kept
    as-is (path is significant). Query string is preserved -- two posts at the
    same path with different params can genuinely be different pages -- but
    the twitter-status special case below overrides with the stable post id."""
    if not url:
        return ''
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return ''
    host = (p.hostname or '').lower()
    if not host:
        return ''
    port = p.port
    default_port = (443 if p.scheme == 'https' else 80 if p.scheme == 'http' else None)
    if port and port == default_port:
        port = None
    netloc = host if not port else f'{host}:{port}'
    path = p.path or '/'
    return urllib.parse.urlunparse((p.scheme or 'https', netloc, path,
                                    p.params, p.query, '')) or url


def source_identity(source_url, resolved_url=None):
    """The canonical source identity for a claim: the RESOLVED landing URL, not
    the raw string. A short link's raw string is never an identity, because
    two short links can look the same and land on different sites, and a short
    token can be repointed to a malicious destination.

    Returns (identity, meta) where meta carries:
    - resolved_ok: True when we have a resolvable final URL
    - is_short_link: whether the as-found URL was a known shortener
    - source_host / resolved_host for inspection

    Resolution rules, in order of trust:
    1. A resolved_url that parses as http(s) wins (this is what /api/browse
       returns after it re-gates the redirect chain's FINAL host). An
       x.com/twitter status post reduces to its status URL -- the post id is
       the stable event identity.
    2. A SHORT source_url with no resolved_url is UNRESOLVED: opaque identity
       (cannot collide with any other source), resolved_ok=False. It is never
       claimed to be "the same source" as anything, and it cannot serve as a
       verified source on its own. A short link is never treated as its own
       landing URL -- the raw string tells us only that a redirect exists,
       not where it leads.
    3. A non-short source_url with no resolved_url is its own identity (the
       raw URL is the landing URL -- no redirect layer to mistrust).
    """
    is_short = _is_short_link(source_url or '')
    candidate = resolved_url or ''
    if candidate:
        try:
            parsed = urllib.parse.urlparse(candidate)
        except ValueError:
            parsed = None
        if parsed and parsed.scheme in ('http', 'https') and parsed.hostname:
            m = _STATUS_RE.search(parsed.path)
            host = parsed.hostname.lower()
            if m:
                # A status post's identity is its status URL (host + full
                # path), with tracking query/fragment dropped -- the post id
                # is the stable event identity, and the same post shared with
                # different tracking params must collapse to one identity.
                identity = urllib.parse.urlunparse(
                    (parsed.scheme, host, parsed.path, '', '', ''))
                return identity, {
                    'resolved_ok': True,
                    'is_short_link': is_short,
                    'source_host': _host(source_url),
                    'resolved_host': host,
                }
            identity = _normalize_url(candidate)
            if identity:
                return identity, {
                    'resolved_ok': True,
                    'is_short_link': is_short,
                    'source_host': _host(source_url),
                    'resolved_host': host,
                }
    if is_short:
        # Short link, no resolved landing URL: UNRESOLVED. Opaque identity.
        digest = hashlib.sha256((source_url or '').encode()).hexdigest()[:16]
        return UNRESOLVED_PREFIX + digest, {
            'resolved_ok': False,
            'is_short_link': True,
            'source_host': _host(source_url),
            'resolved_host': None,
        }
    identity = _normalize_url(source_url or '')
    return identity, {
        'resolved_ok': bool(identity),
        'is_short_link': is_short,
        'source_host': _host(source_url),
        'resolved_host': _host(source_url),
    }


def _host(url):
    if not url:
        return None
    try:
        return (urllib.parse.urlparse(url).hostname or '').lower() or None
    except ValueError:
        return None


def _claim_id(identity, claim=None):
    digest = hashlib.sha256(f'{identity}|{claim or ""}'.encode()).hexdigest()[:16]
    return f'ev-{digest}'


def _unique_claim_id(claims, base_id):
    """A claim id that is guaranteed free in `claims`. Two records on the same
    identity with the same claim text hash to the same base id -- a REPOST must
    still get its own row (it is a distinct observation, not an overwrite of
    the original), so a collision gets a numeric suffix."""
    if base_id not in claims:
        return base_id
    n = 2
    while f'{base_id}-{n}' in claims:
        n += 1
    return f'{base_id}-{n}'


def _validate_claim_record(record):
    """Reject a claim record that cannot be acted on. A claim with no source
    or no claim text is not a claim -- it is noise, and a ledger of noise is
    worse than no ledger."""
    if not record or not isinstance(record, dict):
        raise ValueError('claim record must be a dict')
    if not (record.get('source_url') or '').strip():
        raise ValueError('claim record requires a source_url')
    if not (record.get('claim') or '').strip():
        raise ValueError('claim record requires a claim')
    ct = record.get('claim_type') or 'unclassified'
    if ct not in CLAIM_TYPES:
        raise ValueError(f'claim_type must be one of {sorted(CLAIM_TYPES)}')


def record_source_claim(record, actor=None):
    """Record one claim into the evidence ledger, applying the source-identity
    and coverage rules. Returns a result dict:

    - {'recorded': True, 'id', 'independent': True, 'identity', 'is_short_link',
       'resolved_ok'} -- a new independent claim.
    - {'recorded': True, 'id', 'independent': False, 'repost_of', 'identity', ...}
      -- same resolved identity already in the ledger: a REPOST. Never counts
      as a second independent source.
    - {'recorded': False, 'reason': 'covered', 'identity'} -- the identity is
      already covered (reviewed) and the source has no material update since.
      Callers should skip re-reporting it.
    - {'recorded': False, 'reason': 'invalid', 'error'} -- rejected input.
    """
    try:
        _validate_claim_record(record)
    except ValueError as e:
        return {'recorded': False, 'reason': 'invalid', 'error': str(e)}
    source_url = (record.get('source_url') or '').strip()
    resolved_url = (record.get('resolved_url') or '').strip() or None
    identity, meta = source_identity(source_url, resolved_url)
    now = time.time()
    claims = _evidence_read()
    coverage = _coverage_read()

    # Coverage gate first: an identity that is ALREADY COVERED (brief
    # reviewed) is skipped unless the source reports a material update after
    # the covered date. Reposts of a covered item are also skipped -- the
    # article's rule: "skip previously covered announcements unless there is a
    # material update."
    covered = coverage.get(identity)
    material_ms = record.get('lastModified')
    if covered and covered.get('status') == 'covered' and not _material_update(covered, material_ms):
        return {'recorded': False, 'reason': 'covered', 'identity': identity,
                'covered_date': covered.get('covered_date')}

    # Repost dedup: any existing claim with this identity makes the new one a
    # repost -- not an independent source.
    for cid, ex in claims.items():
        if ex.get('identity') == identity:
            new_id = _unique_claim_id(claims, _claim_id(identity, record.get('claim')))
            rec = dict(record)
            rec['id'] = new_id
            rec['identity'] = identity
            rec['independent'] = False
            rec['repost_of'] = cid
            rec['is_short_link'] = bool(meta['is_short_link'])
            rec['resolved_ok'] = bool(meta['resolved_ok'])
            rec['source_host'] = meta['source_host']
            rec['resolved_host'] = meta['resolved_host']
            rec['status'] = 'needs_review'
            rec['created_ts'] = now
            rec['updated_ts'] = now
            claims[new_id] = rec
            _evidence_write(claims)
            append_audit(actor, 'claim_recorded', claim_id=new_id, identity=identity,
                         detail={'independent': False, 'repost_of': cid,
                                 'source_url': source_url})
            return {'recorded': True, 'id': new_id, 'independent': False,
                    'repost_of': cid, 'identity': identity,
                    'is_short_link': bool(meta['is_short_link']),
                    'resolved_ok': bool(meta['resolved_ok'])}

    new_id = _claim_id(identity, record.get('claim'))
    rec = dict(record)
    rec['id'] = new_id
    rec['identity'] = identity
    rec['independent'] = True
    rec['repost_of'] = None
    rec['is_short_link'] = bool(meta['is_short_link'])
    rec['resolved_ok'] = bool(meta['resolved_ok'])
    rec['source_host'] = meta['source_host']
    rec['resolved_host'] = meta['resolved_host']
    rec.setdefault('claim_type', 'unclassified')
    rec.setdefault('status', 'needs_review')
    rec.setdefault('decision', None)
    rec.setdefault('open_questions', [])
    rec['created_ts'] = now
    rec['updated_ts'] = now
    claims[new_id] = rec
    _evidence_write(claims)
    append_audit(actor, 'claim_recorded', claim_id=new_id, identity=identity,
                 detail={'independent': True, 'resolved_ok': bool(meta['resolved_ok']),
                         'is_short_link': bool(meta['is_short_link']),
                         'source_url': source_url})

    # An unresolved short link is its own signal: record it as a needs-review
    # claim with the question spelled out, so nobody reads it as verified.
    if not meta['resolved_ok']:
        rec['open_questions'] = list(rec.get('open_questions') or [])
        if 'short link not resolved; destination unknown' not in rec['open_questions']:
            rec['open_questions'].append('short link not resolved; destination unknown')
        claims[new_id] = rec
        _evidence_write(claims)
        append_audit(actor, 'claim_recorded', claim_id=new_id, identity=identity,
                     detail={'independent': True, 'unresolved': True})

    return {'recorded': True, 'id': new_id, 'independent': True,
            'identity': identity, 'is_short_link': bool(meta['is_short_link']),
            'resolved_ok': bool(meta['resolved_ok'])}


def _material_update(covered, material_ms):
    """True when the source reports a material change AFTER the covered date,
    so a covered item may be reopened. `material_ms` is the source's
    lastModified epoch-ms (the /api/browse signal). Unknown is treated as
    unchanged -- a covered item is NOT re-reported on a mere re-crawl."""
    if not material_ms:
        return False
    covered_date = covered.get('covered_date')
    return isinstance(covered_date, (int, float)) and material_ms > covered_date


def get_claim(claim_id):
    return (_evidence_read() or {}).get(claim_id)


def list_claims(status=None, limit=None):
    claims = (_evidence_read() or {}).values()
    if status is not None:
        claims = [c for c in claims if c.get('status') == status]
    claims = sorted(claims, key=lambda c: c.get('created_ts', 0))
    if limit:
        claims = claims[-limit:]
    return claims


def update_claim(claim_id, *, status=None, decision=None, open_questions=None,
                 claim=None, scope=None):
    """Transition one claim's status/decision/scope. The Verifier's move: it
    separates the source's claim from our inference and records whether the
    wording is usable as written, needs narrowing, or is blocked until
    checked. Returns the updated record, or None when the claim is unknown."""
    claims = _evidence_read()
    rec = claims.get(claim_id)
    if rec is None:
        return None
    rec = dict(rec)
    if status is not None:
        if status not in CLAIM_STATUSES:
            raise ValueError(f'status must be one of {sorted(CLAIM_STATUSES)}')
        rec['status'] = status
    if decision is not None:
        if decision not in CLAIM_DECISIONS:
            raise ValueError(f'decision must be one of {sorted(CLAIM_DECISIONS)}')
        rec['decision'] = decision
    if open_questions is not None:
        rec['open_questions'] = list(open_questions)
    if claim is not None:
        rec['claim'] = claim
    if scope is not None:
        rec['scope'] = scope
    rec['updated_ts'] = time.time()
    claims[claim_id] = rec
    _evidence_write(claims)
    return rec


def mark_reviewed(claim_id, decision, artifact=None, actor=None):
    """The article's review step: a claim that survives verification becomes
    cleared (and, when independent, folds its identity into the coverage
    ledger as COVERED -- the only state that suppresses re-reporting). A
    blocked-until-checked decision blocks the claim and does NOT mark
    coverage. Returns the updated claim, or None when unknown."""
    if decision not in CLAIM_DECISIONS:
        raise ValueError(f'decision must be one of {sorted(CLAIM_DECISIONS)}')
    rec = update_claim(claim_id, decision=decision)
    if rec is None:
        return None
    if decision == 'blocked_until_checked':
        update_claim(claim_id, status='blocked')
        rec['status'] = 'blocked'
        append_audit(actor, 'claim_reviewed', claim_id=claim_id,
                     identity=rec.get('identity'), detail={'decision': decision})
        return rec
    update_claim(claim_id, status='cleared')
    rec['status'] = 'cleared'
    append_audit(actor, 'claim_reviewed', claim_id=claim_id,
                 identity=rec.get('identity'),
                 detail={'decision': decision, 'independent': bool(rec.get('independent'))})
    if rec.get('independent') and rec.get('identity'):
        mark_covered(rec['identity'], artifact=artifact or rec.get('artifact'),
                     reviewed=True, event_id=rec.get('event_id'),
                     source_url=rec.get('source_url'),
                     resolved_url=rec.get('resolved_url') or rec.get('source_url'),
                     actor=actor)
    return rec


def coverage_status(identity):
    return (_coverage_read() or {}).get(identity)


def is_source_covered(identity, material_update_ms=None):
    """True only when the identity is COVERED -- i.e. the brief was reviewed --
    and the source has no material update since. 'pending_review', 'blocked',
    and 'failed' are all NOT covered: a failed run never suppresses the next
    attempt, and an unreviewed brief does not count as coverage."""
    cov = (_coverage_read() or {}).get(identity)
    if not cov or cov.get('status') != 'covered':
        return False
    if _material_update(cov, material_update_ms):
        return False
    return True


def mark_covered(identity, *, artifact=None, reviewed=False, event_id=None,
                 source_url=None, resolved_url=None, material_update_ms=None,
                 run_id=None, actor=None):
    """Write a coverage entry for a source identity. REVIEW-GATED: `reviewed`
    is False by default and yields 'pending_review' (visible, but it does NOT
    suppress re-reporting). Only `reviewed=True` marks 'covered' and stamps
    covered_date. A 'covered' entry is the sole state that suppresses a
    re-report. Returns the coverage record."""
    if not identity:
        return None
    now = time.time()
    now_ms = int(now * 1000)
    coverage = _coverage_read()
    existing = coverage.get(identity) or {}
    existing = dict(existing)
    if reviewed:
        existing['status'] = 'covered'
        # covered_date is epoch-MILLISECONDS, matching the source's
        # lastModified signal from /api/browse -- the material-update
        # comparison in _material_update is ms-vs-ms, never ms-vs-seconds.
        existing['covered_date'] = now_ms
        existing['reviewed'] = True
    else:
        existing.setdefault('status', 'pending_review')
        existing.setdefault('reviewed', False)
    existing['identity'] = identity
    if artifact is not None:
        existing['artifact'] = artifact
    if event_id is not None:
        existing['event_id'] = event_id
    if source_url is not None:
        existing['source_url'] = source_url
    if resolved_url is not None:
        existing['resolved_url'] = resolved_url
    if material_update_ms is not None:
        existing['material_update_ms'] = material_update_ms
    if run_id is not None:
        existing['last_run_id'] = run_id
    existing['updated_ts'] = now
    coverage[identity] = existing
    _coverage_write(coverage)
    append_audit(actor, 'coverage_covered' if reviewed else 'coverage_pending_review',
                 identity=identity,
                 detail={'reviewed': bool(reviewed), 'artifact': artifact})
    return existing


def record_run_failure(identity, reason, *, artifact=None, event_id=None,
                       source_url=None, resolved_url=None, actor=None):
    """Record a failed run against a source identity. A failure NEVER marks an
    item covered and NEVER suppresses the next attempt: is_source_covered()
    only returns True for status 'covered'. The failure is kept so the desk
    can see why a run did not land, without burning the coverage slot."""
    if not identity:
        return None
    now = time.time()
    coverage = _coverage_read()
    existing = coverage.get(identity) or {}
    existing = dict(existing)
    if existing.get('status') == 'covered':
        # Never demote a covered item to failed -- a later failure does not
        # un-review an earlier reviewed brief.
        return existing
    existing.setdefault('identity', identity)
    existing['status'] = 'failed'
    existing['reviewed'] = False
    existing['last_error'] = reason
    existing['updated_ts'] = now
    if artifact is not None:
        existing['artifact'] = artifact
    if event_id is not None:
        existing['event_id'] = event_id
    if source_url is not None:
        existing['source_url'] = source_url
    if resolved_url is not None:
        existing['resolved_url'] = resolved_url
    failures = list(existing.get('failures') or [])
    failures.append({'ts': now, 'reason': reason})
    existing['failures'] = failures[-20:]  # bounded tail
    coverage[identity] = existing
    _coverage_write(coverage)
    append_audit(actor, 'coverage_failed', identity=identity, detail={'reason': reason})
    return existing


def search_since_ms(last_run_at, overlap_ms=DEFAULT_OVERLAP_MS):
    """The search window for a recurring run: since the last successful run
    MINUS the overlap, so a discovery that landed just after the previous
    window is caught. last_run_at is the previous run's epoch-ms stamp (0 on
    first run -> full window)."""
    if not isinstance(last_run_at, (int, float)) or last_run_at <= 0:
        return 0
    return max(0, int(last_run_at) - int(overlap_ms))
