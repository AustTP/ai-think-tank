# Feed-proposal voting (2026-11-03): the costed ballot layer for the feed
# triage lane. Markers pull items from feeds and POST structured proposals
# here. Voting is WEEKLY by operator decision: proposals accumulate all week,
# the ballot is published Monday 09:00 local and mailed to the PLAYER (the
# operator wants to review every item in advance and strike the ones they
# oppose), a 48-hour veto window follows, and then one batched vote runs on
# the survivors. Off-cycle votes happen only on an explicit director or
# chief-of-staff call and are logged as off-cycle. Every decision lands in
# the tank's action_log.
#
# Authority policy (operator): any agent may PROPOSE; the decision to cast
# votes (publish early, run off-cycle, veto) is a director or chief-of-staff
# call via the tank's own _is_director_or_admin gate; the player session is
# the operator and overrides. The scheduled cron (scripts/vote_run_cron.py)
# acts AS theo -- the chief of staff's standing duty.
#
# Cost guards: reject-dominated items cost one screen call; batched ballots
# amortize the prompt across up to 5 proposals per rubric; voter-call failure
# after one retry defers instead of blocking; duplicate proposals are
# clustered out for 7 days. A whole week of triage stays in the pennies.

import hashlib
import json
import re
import time
import sqlite3
import threading
import contextlib

_VOTE_COORDINATOR_ID = 'ruth'
_VOTE_BATCH = 5                  # proposals per ballot call
_VOTE_SCREEN_MAX_TOKENS = 140
_VOTE_BALLOT_MAX_TOKENS = 320
_VOTE_AUTO_FILE_CONFIDENCE = 0.85
_VOTE_AUTO_FILE_MAX_COST = 2.0   # $ estimated work below which no quorum runs
_VOTE_ESCALATE_COST = 20.0       # expensive proposals skip voting entirely
_VOTE_DUPLICATE_WINDOW = 7 * 24 * 3600.0
_VOTE_MAX_TEXT = 1200
_VOTE_PUBLISH_DOW = 0            # Monday (time.localtime tm_wday)
_VOTE_PUBLISH_HOUR = 9           # 09:00 local
_VOTE_VETO_WINDOW = 48 * 3600.0  # operator review window after publication

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
CREATE TABLE IF NOT EXISTS vote_weeks (
    week_key     TEXT PRIMARY KEY,
    published_ts REAL,
    voted_ts     REAL
);
'''


@contextlib.contextmanager
def _vote_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_VOTE_SCHEMA)
        for _col, _ddl in (('processing_ts', 'ALTER TABLE vote_proposals '
                            'ADD COLUMN processing_ts REAL'),
                           ('ballot_week', 'ALTER TABLE vote_proposals '
                            'ADD COLUMN ballot_week TEXT')):
            try:
                conn.execute(_ddl)
            except Exception:
                pass   # column already exists
        yield conn
        conn.commit()
    finally:
        conn.close()


def _vote_actor(request):
    # Any roster agent (its own key) or the player session; the
    # deny-by-default middleware already keeps anonymous traffic out.
    key_owner = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if key_owner:
        return key_owner
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return 'player'
    return None


def _vote_authority(request):
    # (actor, is_authority). The player session is the operator and always
    # authoritative; agents need the tank's director/chief-of-staff gate.
    actor = _vote_actor(request)
    if not actor:
        return None, False
    if actor == 'player':
        return actor, True
    try:
        state = get_state_from_db()
    except Exception:
        return actor, False
    return actor, bool(_is_director_or_admin(state, actor))


def _vote_log(action, details, agent_id='vote-service'):
    # action_log lives in the tank's OWN backend (postgres on this host) --
    # the service-local sqlite handle is only for the vote tables.
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


def _vote_mail(to_id, text):
    try:
        result = _http_json('POST', SELF_BASE_URL, '/api/mail/send',
                            {'toId': to_id, 'text': text[:2000]},
                            _vote_theo_key(), timeout=30)
        return isinstance(result, dict) and result.get('ok')
    except Exception:
        return False


def _vote_telegram(text):
    # The operator reads the ballot on Telegram (their explicit delivery
    # preference). _telegram_send_text_sync caps at 4000 chars per message,
    # so chunk; each call is a blocking HTTP round-trip, so this runs in its
    # own thread -- never on the event loop.
    sent = 0
    text = text or ''
    for start in range(0, len(text), 3900):
        try:
            if _telegram_send_text_sync(text[start:start + 3900]):
                sent += 1
        except Exception:
            pass
    return sent


def _vote_deliver(player_text, ruth_note):
    # Fire-and-forget fan-out in a daemon thread: two mails + Telegram, all
    # blocking calls that must stay off the event loop.
    def _send():
        if player_text:
            _vote_mail('player', player_text)
            _vote_telegram(player_text)
        if ruth_note:
            _vote_mail(_VOTE_COORDINATOR_ID, ruth_note)
    threading.Thread(target=_send, daemon=True, name='vote-deliver').start()


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
    return (f"{idx}. [{p['id']}] {p['title']}\n"
            f"   claim: {p['claim']}\n"
            f"   proposed action: {p['action']}\n   source: {p['source']}\n"
            f"   estimated cost: ${p['est_cost']:.2f}  confidence: "
            f"{p['confidence']:.2f}")


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
    # Returns {proposal_id: (vote, reason)} for the batch: the prompt numbers
    # them 1..n and the model's numeric ids map back through the batch.
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


def _vote_process_items(rows):
    # The decision pipeline for a batch of ballot rows. Runs in its OWN
    # thread: model calls here are blocking and can take minutes under
    # transport failures; they must never stall the server's event loop.
    passing = []
    for p in rows:
        try:
            if p['est_cost'] >= _VOTE_ESCALATE_COST:
                _vote_mail(_VOTE_COORDINATOR_ID,
                           f"VOTE ESCALATION (expensive, no ballot run): "
                           f"{p['title']} est ${p['est_cost']:.2f}. "
                           f"Claim: {p['claim']}")
                _vote_decide(p, 'escalated',
                             f"est_cost ${p['est_cost']:.2f} over threshold")
                continue
            if p['confidence'] >= _VOTE_AUTO_FILE_CONFIDENCE and \
                    p['est_cost'] <= _VOTE_AUTO_FILE_MAX_COST:
                _vote_mail(_VOTE_COORDINATOR_ID,
                           f"FILE (high-confidence proposal, no quorum needed): "
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
                _vote_mail(_VOTE_COORDINATOR_ID,
                           f"FILE (proposal, quorum approved): {p['action']} "
                           f"-- from {p['source']}: {p['title']}")
            elif status == 'escalated':
                _vote_mail(_VOTE_COORDINATOR_ID,
                           f"VOTE SPLIT on {p['title']}: novelty and cost "
                           f"voters disagree -- decide or refile. "
                           f"Claim: {p['claim']}")


# --- weekly cycle -----------------------------------------------------------

def _vote_week_key(ts=None):
    return time.strftime('%G-W%V', time.localtime(ts or time.time()))


def _vote_publish_moment():
    # Epoch of this week's Monday 09:00 local.
    lt = time.localtime()
    monday = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday,
                          _VOTE_PUBLISH_HOUR, 0, 0, 0, 0, -1))
    return monday


def _vote_get_week(week_key):
    with _vote_db() as conn:
        row = conn.execute('SELECT * FROM vote_weeks WHERE week_key = ?',
                           (week_key,)).fetchone()
        if not row:
            conn.execute('INSERT INTO vote_weeks (week_key) VALUES (?)',
                         (week_key,))
            row = conn.execute('SELECT * FROM vote_weeks WHERE week_key = ?',
                               (week_key,)).fetchone()
    return dict(row)


def _vote_ballot_paper(week_key):
    with _vote_db() as conn:
        rows = conn.execute(
            'SELECT * FROM vote_proposals WHERE ballot_week = ? '
            'ORDER BY created_ts', (week_key,)).fetchall()
    return [dict(r) for r in rows]


def _vote_publish(week_key, by):
    # Move every pending proposal onto this week's ballot and deliver the
    # paper to the player (mail + Telegram) AND the coordinator. Publishing
    # twice is an AMENDMENT: the paper already went out, so new pendings join
    # the open ballot and a fresh copy is delivered (the veto window still
    # runs from the first publish). Once the week has voted, nothing moves.
    now = time.time()
    with _vote_db() as conn:
        week_row = conn.execute('SELECT * FROM vote_weeks WHERE week_key = ?',
                                (week_key,)).fetchone()
        if week_row and week_row['voted_ts'] is not None:
            return None   # week closed
        fresh = conn.execute(
            'UPDATE vote_weeks SET published_ts = ? WHERE week_key = ? '
            'AND published_ts IS NULL', (now, week_key)).rowcount
        rows = conn.execute(
            "SELECT * FROM vote_proposals WHERE status = 'pending' "
            'ORDER BY created_ts').fetchall()
        for r in rows:
            conn.execute(
                'UPDATE vote_proposals SET status = "balloted", '
                'ballot_week = ? WHERE id = ? AND status = "pending"',
                (week_key, r['id']))
    rows = [dict(r) for r in rows]
    amended = not fresh
    if not rows:
        if amended:
            return []   # nothing new to add to an already-published ballot
        body = (f'Weekly vote ballot {week_key}: no proposals were queued '
                f'this week; no vote will run.')
        _vote_deliver(body, f'Ballot {week_key} published (0 items).')
        _vote_log('vote_ballot_published', f'{week_key} by {by}: 0 items')
        return []
    paper = '\n\n'.join(_vote_proposal_text(p, i + 1)
                        for i, p in enumerate(rows))
    if amended:
        body = (f'Ballot {week_key} AMENDED -- {len(rows)} new item(s) joined '
                f'the open ballot (48h review window still runs from the '
                f'first publish; reply to oppose any item):\n\n{paper}')
    else:
        body = (f'Weekly vote ballot {week_key} -- {len(rows)} item(s) will '
                f'be voted after the 48-hour review window. Reply to oppose '
                f'any item (strike it from the ballot):\n\n{paper}')
    _vote_deliver(body,
                  f'Ballot {week_key} {"amended" if amended else "published"} '
                  f'({len(rows)} item(s)); vote runs after the 48h window.')
    _vote_log('vote_ballot_amended' if amended else 'vote_ballot_published',
              f'{week_key} by {by}: {len(rows)} item(s)')
    return rows


def _vote_execute(week_key, by):
    # Claim this week's non-vetoed ballot items and run the pipeline in a
    # background thread; the week closes (voted_ts + result mail) when the
    # thread finishes.
    with _vote_db() as conn:
        open_week = conn.execute(
            'UPDATE vote_weeks SET voted_ts = ? WHERE week_key = ? '
            'AND published_ts IS NOT NULL AND voted_ts IS NULL',
            (time.time(), week_key)).rowcount
        if not open_week:
            return None   # nothing to vote, or already voted this week
        rows = conn.execute(
            "SELECT * FROM vote_proposals WHERE ballot_week = ? AND "
            "status = 'balloted' ORDER BY created_ts", (week_key,)).fetchall()
        for r in rows:
            conn.execute(
                'UPDATE vote_proposals SET status = "processing", '
                'processing_ts = ? WHERE id = ?', (time.time(), r['id']))

    def _run_and_close():
        _vote_process_items(rows)
        with _vote_db() as conn:
            conn.execute('UPDATE vote_weeks SET voted_ts = ? WHERE week_key = ?',
                         (time.time(), week_key))
        summary = _vote_week_summary(week_key)
        _vote_deliver(f'Vote results for {week_key}:\n\n{summary}',
                      f'Vote closed for {week_key}.')
        _vote_log('vote_week_closed', f'{week_key} by {by}: {summary[:400]}')

    threading.Thread(target=_run_and_close, daemon=True,
                     name='vote-week').start()
    return len(rows)


def _vote_week_summary(week_key):
    with _vote_db() as conn:
        rows = conn.execute(
            'SELECT * FROM vote_proposals WHERE ballot_week = ? '
            'ORDER BY created_ts', (week_key,)).fetchall()
    lines = []
    for r in rows:
        lines.append(f"- [{r['status']}] {r['title'][:90]}")
    return '\n'.join(lines) or '(no items)'


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
    return JSONResponse({'ok': True, 'proposalId': pid, 'status': 'pending',
                         'note': 'queued for the weekly ballot; the ballot is '
                                 'published Monday 09:00 and voted 48h later'})


@app.post('/api/vote/weekly')
async def api_vote_weekly(request: Request):
    # The cron tick (any authenticated caller): the SERVER owns the schedule,
    # the tick just advances it. Publishes the weekly ballot when due, then
    # runs the vote once the veto window has closed.
    if not _vote_actor(request):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    week = _vote_week_key()
    row = _vote_get_week(week)
    now = time.time()
    if row['published_ts'] is None:
        due = _vote_publish_moment()
        if now >= due:
            items = _vote_publish(week, 'scheduled')
            return JSONResponse({'ok': True, 'week': week, 'phase': 'published',
                                 'items': len(items or [])})
        return JSONResponse({'ok': True, 'week': week, 'phase': 'collecting',
                             'publishDueAt': due})
    if row['voted_ts'] is None:
        ends = row['published_ts'] + _VOTE_VETO_WINDOW
        if now >= ends:
            n = _vote_execute(week, 'scheduled')
            if n is None:
                return JSONResponse({'ok': True, 'week': week,
                                     'phase': 'nothing-to-vote'})
            return JSONResponse({'ok': True, 'week': week, 'phase': 'voting',
                                 'items': n})
        return JSONResponse({'ok': True, 'week': week, 'phase': 'ballot-open',
                             'vetoWindowEnds': ends})
    return JSONResponse({'ok': True, 'week': week, 'phase': 'done'})


@app.post('/api/vote/publish')
async def api_vote_publish(request: Request):
    # Publish the ballot early (director / chief of staff / player).
    actor, authority = _vote_authority(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    if not authority:
        return JSONResponse(
            {'error': 'publishing the ballot is a director or chief-of-staff '
                      'decision'}, status_code=403)
    week = _vote_week_key()
    rows = _vote_publish(week, f'early:{actor}')
    if rows is None:
        return JSONResponse({'ok': True, 'week': week,
                             'note': 'already published this week'})
    return JSONResponse({'ok': True, 'week': week, 'published': len(rows)})


@app.post('/api/vote/run')
async def api_vote_run(request: Request):
    # Run this week's vote. Off-cycle (before the veto window closes) this is
    # an explicit director/chief-of-staff/player call and is logged as such.
    actor, authority = _vote_authority(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    if not authority:
        return JSONResponse(
            {'error': 'vote runs are called by a director or the '
                      'chief of staff'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    week = _vote_week_key()
    row = _vote_get_week(week)
    now = time.time()
    if row['published_ts'] is None:
        _vote_publish(week, f'pre-run:{actor}')
        row = _vote_get_week(week)
    if row['voted_ts'] is not None:
        return JSONResponse({'ok': False, 'week': week,
                             'error': 'this week already voted'}, status_code=409)
    off_cycle = now < row['published_ts'] + _VOTE_VETO_WINDOW
    if off_cycle:
        _vote_log('vote_off_cycle', f'{week} forced early by {actor}')
    n = _vote_execute(week, actor)
    if n is None:
        return JSONResponse({'ok': True, 'week': week,
                             'note': 'nothing left to vote this week'})
    return JSONResponse({'ok': True, 'week': week, 'items': n,
                         'offCycle': off_cycle,
                         'note': 'processing in background; watch '
                                 'GET /api/vote/ballot'})


@app.post('/api/vote/veto')
async def api_vote_veto(request: Request):
    # The operator's advance-review power: strike an item from the open
    # ballot before the vote. Player session or director/chief of staff.
    actor, authority = _vote_authority(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    if not authority:
        return JSONResponse(
            {'error': 'vetoes come from the player or a director'}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    pid = (body.get('proposalId') or '').strip()
    reason = (body.get('reason') or '').strip()[:300] or 'no reason given'
    week = _vote_week_key()
    with _vote_db() as conn:
        row = conn.execute('SELECT * FROM vote_proposals WHERE id = ?',
                           (pid,)).fetchone()
        if not row or row['ballot_week'] != week:
            return JSONResponse(
                {'error': 'item is not on the current ballot'}, status_code=404)
        if row['status'] != 'balloted':
            return JSONResponse(
                {'error': f'item is {row["status"]}, not on the open ballot'},
                status_code=409)
        conn.execute(
            'UPDATE vote_proposals SET status = "vetoed", decided_ts = ? '
            'WHERE id = ?', (time.time(), pid))
    _vote_log('vote_vetoed', f'{pid} by {actor}: {reason}')
    _vote_mail(_VOTE_COORDINATOR_ID,
               f'Operator veto on ballot {week}: "{row["title"]}" struck '
               f'({reason}).')
    return JSONResponse({'ok': True, 'proposalId': pid, 'status': 'vetoed'})


@app.get('/api/vote/ballot')
async def api_vote_ballot(request: Request):
    if not _vote_actor(request):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    week = _vote_week_key()
    row = _vote_get_week(week)
    return JSONResponse({'ok': True, 'week': week,
                         'publishedTs': row['published_ts'],
                         'votedTs': row['voted_ts'],
                         'vetoWindowEnds': (row['published_ts'] +
                                            _VOTE_VETO_WINDOW
                                            if row['published_ts'] else None),
                         'items': _vote_ballot_paper(week)})


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
