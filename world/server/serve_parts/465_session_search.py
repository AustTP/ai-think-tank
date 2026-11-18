"""Cross-session search (the aise idea from the 2026-11-18 dankornas round-3
review, library/reports/dankornas-review-2026-11-18.md).

/api/failures (455_failure_memory.py) searches only failure-class action_log
rows, and /api/replay is per-task, so "what did we do about X last time?"
still meant guessing task ids. This is the general read: ONE read-only
operator/admin endpoint that searches ALL action_log rows (every decision,
tool call, escalation and failure the tank taped) plus every agent mailbox
(live + archive) and the player inbox, newest first, with a snippet around
the first match so an old session's answer is visible without opening the
task.

Read-only: no state mutation. Same auth and query contract as /api/failures
(operator/admin only; q required; days window 1..90, default 7).
"""

import asyncio
import json
import time

from fastapi import Request
from fastapi.responses import JSONResponse

_SS_LIMIT = 40
_SS_PREFILTER = 4000
_SS_SNIPPET = 220
# Building-era window bound (operator decision 2026-11-18): the tank is only
# ~61 days old and most of that is build/troubleshoot churn, so search
# surfaces the last 14 days for now. Raise this to 90+ once there is that
# much steady-state history worth recalling -- it is the ONLY knob: it clamps
# the endpoint's days parameter and bounds the mail scan together.
_SS_WINDOW_DAYS = 14


def _ss_tokens(q):
    return [t.lower() for t in str(q or '').split() if t.strip()][:8]


def _ss_matches_all(text, toks):
    low = str(text or '').lower()
    return all(t in low for t in toks)


def _ss_snippet(text, toks):
    """A short window around the first token's occurrence, so a hit can be
    judged without opening the task. Falls back to the text head."""
    text = ' '.join(str(text or '').split())
    if len(text) <= _SS_SNIPPET:
        return text
    at = text.lower().find(toks[0]) if toks else -1
    if at < 0:
        return text[:_SS_SNIPPET]
    start = max(0, at - _SS_SNIPPET // 3)
    out = text[start:start + _SS_SNIPPET]
    return ('...' + out) if start else out


def _ss_log_rows(conn, toks, since_ts):
    """All action_log rows whose action or details match every token. The
    LIKE prefilter bounds the DB scan; the full-token check runs in Python
    because details is freeform JSON."""
    first = '%' + toks[0] + '%'
    try:
        rows = conn.execute(
            'SELECT ts, agent_id, action, details FROM action_log '
            'WHERE ts >= ? AND LOWER(details) LIKE ? '
            'ORDER BY ts DESC LIMIT ?',
            (since_ts, first, _SS_PREFILTER),
        ).fetchall()
    except Exception:
        rows = []
    out = []
    for ts, agent_id, action, details in rows:
        try:
            parsed = json.loads(details) if details else {}
        except Exception:
            parsed = {'raw': str(details)[:300]}
        searchable = ' '.join([action, details or '', str(parsed)])
        if not _ss_matches_all(searchable, toks):
            continue
        out.append({
            'source': 'log', 'atMs': int(ts * 1000), 'agent': agent_id,
            'action': action,
            'taskId': parsed.get('taskId'),
            'title': str(parsed.get('title') or '')[:160],
            'note': str(parsed.get('failNote') or parsed.get('note')
                        or parsed.get('reason') or '')[:220],
            'snippet': _ss_snippet(searchable, toks),
        })
    return out


def _ss_mail_hits(state, toks, now_ms):
    """Agent mailboxes (live + archive) and the player inbox. Retention
    (MAILBOX_KEEP_COUNT / MAILBOX_ARCHIVE_KEEP_COUNT) already bounds the
    scan; entries older than the window drop out. Mail stamps are epoch-ms
    under createdAt or ts -- both appear in the wild, so read both."""
    window = _SS_WINDOW_DAYS * 24 * 3600 * 1000
    min_ms = now_ms - window
    out = []
    for agent_id, agent in (state.get('agents') or {}).items():
        if not isinstance(agent, dict):
            continue
        pool = list(agent.get('mailbox') or []) + \
            list(agent.get('mailboxArchive') or [])
        for m in pool:
            if not isinstance(m, dict):
                continue
            text = ' '.join([m.get('kind') or '', m.get('about') or '',
                             m.get('title') or '', m.get('text') or ''])
            if not _ss_matches_all(text, toks):
                continue
            stamp = int(m.get('createdAt') or m.get('ts') or 0)
            if stamp < min_ms:
                continue
            out.append({'source': 'mail', 'atMs': stamp,
                        'agent': agent_id,
                        'kind': m.get('kind'),
                        'title': str(m.get('title') or '')[:160],
                        'snippet': _ss_snippet(text, toks)})
    for entry in (state.get('playerInbox') or []):
        if not isinstance(entry, dict):
            continue
        text = ' '.join([entry.get('question') or '', entry.get('answer') or ''])
        if not _ss_matches_all(text, toks):
            continue
        stamp = int(entry.get('ts') or entry.get('createdAt') or 0)
        if stamp < min_ms:
            continue
        out.append({'source': 'inbox', 'atMs': stamp,
                    'agent': entry.get('agentId'),
                    'title': str(entry.get('question') or '')[:160],
                    'snippet': _ss_snippet(text, toks)})
    return out


def _ss_session_search(q, limit, days):
    toks = _ss_tokens(q)
    if not toks:
        return {'error': 'empty query'}
    now_ms = int(time.time() * 1000)
    since_ts = time.time() - max(1, min(int(days or 7), _SS_WINDOW_DAYS)) \
        * 24 * 3600
    state = get_state_from_db('none') or {}
    with _db() as conn:
        log_hits = _ss_log_rows(conn, toks, since_ts)
    mail_hits = _ss_mail_hits(state, toks, now_ms)
    limit = max(1, min(int(limit or _SS_LIMIT), 200))
    hits = sorted(log_hits + mail_hits, key=lambda h: h.get('atMs') or 0,
                  reverse=True)
    return {
        'query': q,
        'matches': hits[:limit],
        'counts': {'log': len(log_hits),
                   'mail': sum(1 for h in mail_hits if h['source'] == 'mail'),
                   'inbox': sum(1 for h in mail_hits
                                if h['source'] == 'inbox')},
        'truncated': len(hits) > limit,
    }


@app.get('/api/session-search')
async def api_session_search(request: Request, q: str = '', limit: int = 0,
                             days: int = 14):
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    if not _ss_tokens(q):
        return JSONResponse({'error': 'query parameter q is required'},
                            status_code=400)
    return JSONResponse(await asyncio.to_thread(
        _ss_session_search, q, limit, days))


_SS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'session_search',
        'description': 'Search your own recorded history before re-doing '
                       'work or re-hitting a failure: your action_log rows '
                       '(every action you took) and your mailbox (live + '
                       'archive). Self-scoped: you see only your own rows; '
                       'the operator-wide search is GET /api/session-search '
                       '(operator only). Query tokens are ANDed, window is '
                       'the last N days (default 7, max 14).',
        'parameters': {
            'type': 'object',
            'properties': {
                'q': {'type': 'string',
                      'description': 'Space-separated tokens, ANDed.'},
                'days': {'type': 'integer',
                         'description': 'Look-back window in days '
                                        '(default 7, max 14).'},
                'limit': {'type': 'integer',
                          'description': 'Max results (default 20, cap 40).'},
            },
            'required': ['q'],
        },
    },
}


def _ss_agent_search(agent_id, args):
    """Dispatch a `session_search` tool call. Self-scope rule, same shape as
    the replay/investigate tools: an agent sees only its own action rows and
    its own mailbox (live + archive); never other agents' mail and never the
    player inbox. Read-only by construction."""
    args = args or {}
    toks = _ss_tokens(args.get('q'))
    if not toks:
        return 'session_search: q is required (space-separated tokens)'
    now_ms = int(time.time() * 1000)
    days = max(1, min(int(args.get('days') or 7), _SS_WINDOW_DAYS))
    since_ts = time.time() - days * 24 * 3600
    limit = max(1, min(int(args.get('limit') or 20), _SS_LIMIT))
    with _db() as conn:
        try:
            rows = conn.execute(
                'SELECT ts, action, details FROM action_log '
                'WHERE agent_id = ? AND ts >= ? '
                'AND LOWER(details) LIKE ? ORDER BY ts DESC LIMIT ?',
                (agent_id, since_ts, '%' + toks[0] + '%', _SS_PREFILTER),
            ).fetchall()
        except Exception:
            rows = []
    hits = []
    for ts, action, details in rows:
        try:
            parsed = json.loads(details) if details else {}
        except Exception:
            parsed = {'raw': str(details)[:300]}
        searchable = ' '.join([action, details or '', str(parsed)])
        if not _ss_matches_all(searchable, toks):
            continue
        hits.append({'source': 'log', 'atMs': int(ts * 1000),
                     'action': action,
                     'taskId': parsed.get('taskId'),
                     'snippet': _ss_snippet(searchable, toks)})
    agent = (get_state_from_db('none') or {}).get('agents', {}).get(agent_id)
    if isinstance(agent, dict):
        pool = list(agent.get('mailbox') or []) + \
            list(agent.get('mailboxArchive') or [])
        min_ms = now_ms - _SS_WINDOW_DAYS * 24 * 3600 * 1000
        for m in pool:
            if not isinstance(m, dict):
                continue
            text = ' '.join([m.get('kind') or '', m.get('about') or '',
                             m.get('title') or '', m.get('text') or ''])
            if not _ss_matches_all(text, toks):
                continue
            stamp = int(m.get('createdAt') or m.get('ts') or 0)
            if stamp < min_ms:
                continue
            hits.append({'source': 'mail', 'atMs': stamp,
                         'kind': m.get('kind'),
                         'snippet': _ss_snippet(text, toks)})
    hits.sort(key=lambda h: h.get('atMs') or 0, reverse=True)
    if not hits:
        return (f"No hits for {' '.join(toks)} in your last {days} days of "
                'actions and mail.')
    lines = [f"{len(hits)} hit(s) in your last {days} days "
             f"(self-scoped; the operator-wide search is "
             f"GET /api/session-search):"]
    for h in hits[:limit]:
        when = time.strftime('%m-%d %H:%M', time.gmtime(h['atMs'] / 1000))
        label = h.get('action') or h.get('kind') or '?'
        tid = f" [{h['taskId']}]" if h.get('taskId') else ''
        lines.append(f"- {when} {h['source']}:{label}{tid} "
                     f"{h.get('snippet', '')[:160]}")
    return '\n'.join(lines)
