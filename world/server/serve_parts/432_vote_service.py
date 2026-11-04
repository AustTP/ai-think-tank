# Feed-proposal voting (2026-11-03): the costed ballot layer for the feed
# triage lane. Markers pull items from feeds and POST structured proposals
# here; a screen call (sublow band) kills noise for one cheap call; passing
# items above the thresholds get a two-voter quorum (novelty rubric, cost
# rubric -- separate calls so the voters never share a context); agreement
# files a card instruction to the coordinator, a split escalates with both
# reasons, and mechanical cases never vote at all. Every decision lands in
# action_log. Ballots are tight (~1k tokens in, ~80 out) so a full day of
# triage stays in the pennies; the naive full-context version costs three
# orders of magnitude more.
#
# Filing stays with the coordinator (ruth) exactly like pairing mode: this
# service decides, it never files cards itself. Escalation and filing mails
# are sent AS theo (chief of staff runs the vote service) via the loopback
# mail endpoint, so the sender binding stays server-authoritative.
#
# Cost guards baked in: reject-dominated items cost one screen call; batched
# ballots amortize the prompt across up to 5 proposals per rubric; one revote
# is possible only because a resubmitted proposal is a new row (clusters keep
# duplicates out for 7 days); voter-call failure after one retry defers to
# the coordinator instead of blocking on assembly.

import hashlib
import json
import re
import time
import sqlite3
import threading
import contextlib

_VOTE_COORDINATOR_ID = 'ruth'
_VOTE_RUN_LIMIT_DEFAULT = 8
_VOTE_RUN_LIMIT_MAX = 25
_VOTE_BATCH = 5                  # proposals per ballot call
_VOTE_SCREEN_MAX_TOKENS = 140
_VOTE_BALLOT_MAX_TOKENS = 320
_VOTE_AUTO_FILE_CONFIDENCE = 0.85
_VOTE_AUTO_FILE_MAX_COST = 2.0   # $ estimated work below which no quorum runs
_VOTE_ESCALATE_COST = 20.0       # expensive proposals skip voting entirely
_VOTE_DUPLICATE_WINDOW = 7 * 24 * 3600.0
_VOTE_MAX_TEXT = 1200

_VOTE_SCREEN_PROMPT = (
    'You are a feed-item screener for a software-building agent organization. '
    'Return ONLY JSON, no prose: {"verdict": "pass" or "reject", '
    '"reason": "one sentence"}. Reject noise: advertising, reposts, items '
    'with no plausible concrete action, or claims without substance.')

_VOTE_RUBRICS = (
    ('novelty',
     'Rubric: novelty. Approve only if acting on this adds something the '
     'organization does not already know or do; reject reposts and known '
     'work.'),
    ('cost',
     'Rubric: cost-effectiveness. Approve only if the estimated cost is '
     'proportionate to expected value AND the proposed action is concrete '
     'enough to file as a work card.'))

_VOTE_BALLOT_PROMPT = (
    'You are one voting panel member in an agent organization. You see '
    'numbered proposals. Return ONLY a JSON array, no prose: '
    '[{"id": <n>, "vote": "approve" or "reject", "reason": "one sentence"}]. '
    'Be decisive: reject unclear, low-value, or cost-disproportionate items.')

_VOTE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS vote_proposals (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    claim       TEXT NOT NULL,
    action      TEXT NOT NULL,
    est_cost    REAL NOT NULL DEFAULT 0,
    confidence  REAL NOT NULL DEFAULT 0.5,
    cluster_hash TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    decided_ts  REAL,
    created_ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vote_proposals_status
    ON vote_proposals(status, created_ts);
CREATE TABLE IF NOT EXISTS vote_ballots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id  TEXT NOT NULL REFERENCES vote_proposals(id),
    voter        TEXT NOT NULL,
    rubric       TEXT NOT NULL,
    vote         TEXT NOT NULL,
    reason       TEXT NOT NULL,
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vote_ballots_proposal
    ON vote_ballots(proposal_id);
'''


@contextlib.contextmanager
def _vote_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_VOTE_SCHEMA)
        try:
            conn.execute('ALTER TABLE vote_proposals ADD COLUMN processing_ts REAL')
        except Exception:
            pass   # column already exists
        yield conn
        conn.commit()
    finally:
        conn.close()


def _vote_actor(request):
    # Any roster agent (its own key) or the player session may propose and
    # run; the deny-by-default middleware already keeps anonymous traffic out.
    key_owner = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if key_owner:
        return key_owner
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return 'player'
    return None


def _vote_log(action, details, agent_id='vote-service'):
    # action_log lives in the tank's OWN backend (postgres on this host) --
    # writing it through the service-local sqlite handle silently landed the
    # rows in a stale sqlite table the operator never reads. _db() is the
    # backend-aware handle the rest of the tank audits through.
    try:
        with _db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, '
                'authorized, ts, trace_id) VALUES (?, ?, ?, 1, ?, NULL)',
                (agent_id, action, details[:900], time.time()))
    except Exception:
        pass   # audit logging must never break the decision itself


def _vote_model(band):
    # The band's PAID slug from model_tiers, skipping free-pool overlay
    # strings (a circuit-broken free endpoint must not kill a vote).
    try:
        with _vote_db() as conn:
            row = conn.execute(
                'SELECT slug FROM model_tiers WHERE band = ?', (band,)).fetchone()
        slug = (row['slug'] if row else '') or ''
        if slug and not slug.startswith('@'):
            return slug
    except Exception:
        pass
    return band   # literal band names resolve inside /api/chat


def _vote_theo_key():
    return get_or_create_agent_key('theo')


def _vote_chat_on(band, messages, max_tokens, timeout=90):
    result = _http_json(
        'POST', SELF_BASE_URL, '/api/chat',
        {'model': _vote_model(band), 'messages': messages,
         'max_tokens': max_tokens, 'agentId': 'theo', 'service': 'voting'},
        _vote_theo_key(), timeout=timeout)
    if not isinstance(result, dict) or result.get('error') or not result.get('reply'):
        return None
    return result['reply'].strip()


def _vote_parse_json(text):
    if not text:
        return None
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r'[\[{].*[\]}]', cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None


def _vote_cluster_hash(title, claim):
    norm = re.sub(r'[^a-z0-9]+', '', (title + ' ' + claim).lower())
    return hashlib.sha256(norm.encode('utf-8')).hexdigest()[:16]


def _vote_proposal_text(p, idx):
    return (f"{idx}. title: {p['title']}\n   claim: {p['claim']}\n"
            f"   proposed action: {p['action']}\n   source: {p['source']}\n"
            f"   estimated cost: ${p['est_cost']:.2f}")


def _vote_mail_ruth(text):
    try:
        result = _http_json('POST', SELF_BASE_URL, '/api/mail/send',
                            {'toId': _VOTE_COORDINATOR_ID, 'text': text[:2000]},
                            _vote_theo_key(), timeout=30)
        return isinstance(result, dict) and result.get('ok')
    except Exception:
        return False


def _vote_decide(p, status, note):
    with _vote_db() as conn:
        conn.execute(
            'UPDATE vote_proposals SET status = ?, decided_ts = ? WHERE id = ?',
            (status, time.time(), p['id']))
    _vote_log('vote_decision', f"{p['id']} -> {status}: {note}")


def _vote_store_ballot(pid, voter, rubric, vote, reason):
    with _vote_db() as conn:
        conn.execute(
            'INSERT INTO vote_ballots (proposal_id, voter, rubric, vote, '
            'reason, ts) VALUES (?, ?, ?, ?, ?, ?)',
            (pid, voter, rubric, vote, reason, time.time()))


def _vote_screen(p):
    reply = _vote_chat_on(
        'sublow',
        [{'role': 'system', 'content': _VOTE_SCREEN_PROMPT},
         {'role': 'user', 'content': _vote_proposal_text(p, 1)}],
        _VOTE_SCREEN_MAX_TOKENS, timeout=45)
    if reply is None:
        return None   # transport/model failure, not a verdict
    parsed = _vote_parse_json(reply) or {}
    verdict = (parsed.get('verdict') or '').strip().lower()
    reason = (parsed.get('reason') or '').strip()[:300] or 'no reason given'
    if verdict not in ('pass', 'reject'):
        return None
    _vote_store_ballot(p['id'], 'screen', 'noise', verdict, reason)
    return verdict, reason


def _vote_quorum_ballot(rubric_name, rubric_text, batch):
    # Returns {proposal_id: (vote, reason)} for the batch, keyed by the REAL
    # proposal id (the prompt numbers them 1..n and the model's numeric ids
    # map back through the batch).
    user = '\n\n'.join(_vote_proposal_text(p, i + 1)
                       for i, p in enumerate(batch))
    reply = _vote_chat_on(
        'low',
        [{'role': 'system', 'content': _VOTE_BALLOT_PROMPT + ' ' + rubric_text},
         {'role': 'user', 'content': user}],
        _VOTE_BALLOT_MAX_TOKENS, timeout=60)
    if reply is None:
        return None
    parsed = _vote_parse_json(reply)
    if not isinstance(parsed, list):
        return None
    out = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        vote = (item.get('vote') or '').strip().lower()
        if vote not in ('approve', 'reject'):
            continue
        reason = (item.get('reason') or '')[:300] or 'no reason given'
        raw_id = item.get('id')
        p = None
        try:
            p = batch[int(raw_id) - 1]
        except (TypeError, ValueError, IndexError):
            p = next((q for q in batch if str(q['id']) == str(raw_id)), None)
        if p is not None:
            out[p['id']] = (vote, reason)
    return out if out else None


def _vote_run_quorum(passing):
    # Two rubric calls, each grading the batch in separate contexts so the
    # voters cannot collapse into one biased opinion. Returns a list of
    # (proposal, status, note); decides everything, including deferrals.
    decided = []
    ballots = {p['id']: {} for p in passing}
    for rubric_name, rubric_text in _VOTE_RUBRICS:
        for start in range(0, len(passing), _VOTE_BATCH):
            chunk = passing[start:start + _VOTE_BATCH]
            by_id = None
            for _attempt in (0, 1):   # one transport retry
                by_id = _vote_quorum_ballot(rubric_name, rubric_text, chunk)
                if by_id is not None:
                    break
            if by_id is None:
                continue   # missing rubric shows up as a deferral below
            for p in chunk:
                got = by_id.get(p['id'])
                if got:
                    ballots[p['id']][rubric_name] = got
                    _vote_store_ballot(p['id'], f'{rubric_name}-voter',
                                       rubric_name, got[0], got[1])
    for p in passing:
        got = ballots[p['id']]
        if 'novelty' not in got or 'cost' not in got:
            missing = [r for r in ('novelty', 'cost') if r not in got]
            decided.append((p, 'deferred',
                            f'{", ".join(missing)} voter unavailable after retry'))
            continue
        n_vote, n_reason = got['novelty']
        c_vote, c_reason = got['cost']
        if n_vote == 'approve' and c_vote == 'approve':
            status, note = 'filed', f'quorum approved ({n_reason} / {c_reason})'
        elif n_vote == 'reject' and c_vote == 'reject':
            status, note = 'rejected', f'quorum rejected ({n_reason} / {c_reason})'
        else:
            status = 'escalated'
            note = (f"split: novelty {n_vote} ({n_reason}); "
                    f"cost {c_vote} ({c_reason})")
        decided.append((p, status, note))
    return decided


@app.post('/api/vote/propose')
async def api_vote_propose(request: Request):
    actor = _vote_actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    title = (body.get('title') or '').strip()[:_VOTE_MAX_TEXT]
    claim = (body.get('claim') or '').strip()[:_VOTE_MAX_TEXT]
    action = (body.get('action') or '').strip()[:_VOTE_MAX_TEXT]
    source = (body.get('source') or '').strip()[:200] or 'unattributed'
    if not title or not action:
        return JSONResponse({'error': 'title and action are required'}, status_code=400)
    try:
        est_cost = max(0.0, min(float(body.get('estCost') or 0.0), 1000.0))
    except (TypeError, ValueError):
        est_cost = 0.0
    try:
        confidence = max(0.0, min(float(body.get('confidence') or 0.5), 1.0))
    except (TypeError, ValueError):
        confidence = 0.5
    chash = _vote_cluster_hash(title, claim)
    now = time.time()
    with _vote_db() as conn:
        dup = conn.execute(
            'SELECT id, status FROM vote_proposals WHERE cluster_hash = ? '
            'AND created_ts > ? ORDER BY created_ts DESC LIMIT 1',
            (chash, now - _VOTE_DUPLICATE_WINDOW)).fetchone()
        if dup:
            _vote_log('vote_duplicate', f'{dup["id"]} blocked a resubmission')
            return JSONResponse({'ok': True, 'duplicateOf': dup['id'],
                                 'status': dup['status']})
        pid = 'vote-%d' % int(now * 1000)
        conn.execute(
            'INSERT INTO vote_proposals (id, source, title, claim, action, '
            'est_cost, confidence, cluster_hash, status, created_ts) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (pid, source, title, claim, action, est_cost, confidence,
             chash, 'pending', now))
    _vote_log('vote_proposed', f'{pid} by {actor}: {title[:120]}')
    return JSONResponse({'ok': True, 'proposalId': pid, 'status': 'pending'})


def _vote_process_claimed(rows):
    # Runs in its OWN thread: model calls here are blocking and can take
    # minutes under transport failures; they must never stall the server's
    # event loop (the first inline version froze the whole server mid-run).
    passing = []
    for p in rows:
        try:
            if p['est_cost'] >= _VOTE_ESCALATE_COST:
                _vote_mail_ruth(
                    f"VOTE ESCALATION (expensive, no ballot run): "
                    f"{p['title']} est ${p['est_cost']:.2f}. Claim: {p['claim']}")
                _vote_decide(p, 'escalated',
                             f"est_cost ${p['est_cost']:.2f} over threshold")
                continue
            if p['confidence'] >= _VOTE_AUTO_FILE_CONFIDENCE and \
                    p['est_cost'] <= _VOTE_AUTO_FILE_MAX_COST:
                _vote_mail_ruth(
                    f"FILE (high-confidence feed proposal, no quorum needed): "
                    f"{p['action']} -- from {p['source']}: {p['title']}")
                _vote_decide(p, 'filed', 'auto-filed: confidence/cost thresholds')
                continue
            screen = _vote_screen(p)
            if screen is None:
                _vote_decide(p, 'deferred', 'screener unavailable after failure')
                continue
            verdict, reason = screen
            if verdict == 'reject':
                _vote_decide(p, 'screened_out', f'screener: {reason}')
                continue
            passing.append(p)
        except Exception as e:   # one bad proposal never kills the batch
            _vote_decide(p, 'deferred', f'processing error: {e}'[:300])
    if passing:
        for p, status, note in _vote_run_quorum(passing):
            _vote_decide(p, status, note)
            if status == 'filed':
                _vote_mail_ruth(
                    f"FILE (feed proposal, quorum approved): {p['action']} "
                    f"-- from {p['source']}: {p['title']}")
            elif status == 'escalated':
                _vote_mail_ruth(
                    f"VOTE SPLIT on {p['title']}: novelty and cost voters "
                    f"disagree -- decide or refile. Claim: {p['claim']}")


_VOTE_PROCESS_STALE = 600.0   # seconds before an abandoned 'processing' claim is re-takeable


@app.post('/api/vote/run')
async def api_vote_run(request: Request):
    actor = _vote_actor(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        limit = max(1, min(int(body.get('limit') or _VOTE_RUN_LIMIT_DEFAULT),
                           _VOTE_RUN_LIMIT_MAX))
    except (TypeError, ValueError):
        limit = _VOTE_RUN_LIMIT_DEFAULT
    now = time.time()
    with _vote_db() as conn:
        rows = conn.execute(
            "SELECT * FROM vote_proposals WHERE status = 'pending' OR "
            "(status = 'processing' AND (processing_ts IS NULL OR "
            'processing_ts < ?)) ORDER BY created_ts LIMIT ?',
            (now - _VOTE_PROCESS_STALE, limit)).fetchall()
        if not rows:
            return JSONResponse({'ok': True, 'claimed': 0})
        ids = [r['id'] for r in rows]
        marks = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE vote_proposals SET status = "processing", processing_ts = ? '
            f'WHERE id IN ({marks})',   # nosec B608 -- placeholders are parameterized
            (now, *ids))
    thread = threading.Thread(target=_vote_process_claimed, args=(rows,),
                              daemon=True, name='vote-run')
    thread.start()
    _vote_log('vote_run_claimed',
              f'{len(rows)} proposal(s) claimed by {actor}: '
              f'{", ".join(ids[:5])}{"..." if len(ids) > 5 else ""}')
    return JSONResponse({'ok': True, 'claimed': len(ids),
                         'ids': ids,
                         'note': 'processing in background; watch '
                                 'GET /api/vote/proposals?status=processing'})


@app.get('/api/vote/proposals')
async def api_vote_proposals(request: Request):
    if not _vote_actor(request):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(str(request.url)).query)
    status = (qs.get('status') or [''])[0].strip()
    try:
        limit = max(1, min(int((qs.get('limit') or ['50'])[0]), 200))
    except (TypeError, ValueError):
        limit = 50
    with _vote_db() as conn:
        if status:
            rows = conn.execute(
                'SELECT * FROM vote_proposals WHERE status = ? '
                'ORDER BY created_ts DESC LIMIT ?', (status, limit)).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM vote_proposals ORDER BY created_ts DESC '
                'LIMIT ?', (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['ballots'] = [dict(b) for b in conn.execute(
                'SELECT voter, rubric, vote, reason, ts FROM vote_ballots '
                'WHERE proposal_id = ? ORDER BY ts', (r['id'],)).fetchall()]
            out.append(d)
    return JSONResponse({'ok': True, 'proposals': out})


@app.get('/api/vote/ballots')
async def api_vote_ballots(request: Request):
    if not _vote_actor(request):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(str(request.url)).query)
    pid = (qs.get('proposalId') or [''])[0].strip()
    if not pid:
        return JSONResponse({'error': 'proposalId is required'}, status_code=400)
    with _vote_db() as conn:
        rows = conn.execute(
            'SELECT voter, rubric, vote, reason, ts FROM vote_ballots '
            'WHERE proposal_id = ? ORDER BY ts', (pid,)).fetchall()
    return JSONResponse({'ok': True, 'proposalId': pid,
                         'ballots': [dict(r) for r in rows]})
