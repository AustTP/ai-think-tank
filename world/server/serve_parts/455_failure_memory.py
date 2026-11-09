"""Failure-memory search (MAYA-21 row #2, the 'Eidetic Engine' pattern).

One read-only endpoint that searches every place the tank records why work
failed: failure-class action_log rows, live failed/blocked cards, the
durable repair-failure ledger, and archived terminal cards. The point is
that a failure the colony already paid for once is searchable before
anyone pays for it again -- the same mistake re-minted dozens of cards
during the 2026-11-09 refusal storm because nothing could answer "has
this failed before, and why?".

Operator/admin only, like /api/replay. Read-only: no state mutation.
"""

import asyncio
import json
import time

from fastapi import Request
from fastapi.responses import JSONResponse

_SEARCH_LIMIT = 40
_SQL_PREFILTER_LIMIT = 2000
_FAILURE_ACTIONS = (
    'task_completion_refused', 'content_run_abandoned',
    'task_requeued_stale', 'card_pattern_suppressed',
    'repair_loop_stopped', 'infra_send_back', 'fix_send_back_deduped',
    'worker_stuck_help',
)


def _tokens(q):
    return [t.lower() for t in str(q or '').split() if t.strip()][:8]


def _matches_all(text, toks):
    low = str(text or '').lower()
    return all(t in low for t in toks)


def _log_rows(conn, toks, since_ts):
    """Failure-class action_log rows whose details match every token.
    A LOWER(details) LIKE prefilter on the first token bounds the DB scan
    (portable: LIKE is case-insensitive for ASCII on sqlite, and LOWER on
    both sides keeps postgres exact); the remaining tokens match in Python
    because details is freeform text."""
    first = '%' + toks[0] + '%'
    actions = ','.join("'%s'" % a for a in _FAILURE_ACTIONS)
    try:
        rows = conn.execute(
            'SELECT ts, agent_id, action, details FROM action_log '
            f'WHERE action IN ({actions}) AND ts >= ? '
            'AND LOWER(details) LIKE ? ORDER BY ts DESC LIMIT ?',
            (since_ts, first, _SQL_PREFILTER_LIMIT),
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
        if not _matches_all(searchable, toks):
            continue
        out.append({'source': 'log', 'ts': ts, 'agent': agent_id,
                    'action': action, 'taskId': parsed.get('taskId'),
                    'title': str(parsed.get('title') or '')[:160],
                    'note': str(parsed.get('failNote')
                                or parsed.get('note')
                                or parsed.get('reason') or '')[:220]})
    return out


def _task_matches(t, toks):
    text = ' '.join([t.get('title') or '', t.get('failNote') or '',
                     t.get('blockedReason') or '', t.get('nextAction') or '',
                     t.get('note') or ''])
    return _matches_all(text, toks)


def _live_cards(state, toks, now_ms):
    window = 30 * 24 * 3600 * 1000
    out = []
    for tid, t in (state.get('tasks') or {}).items():
        if not isinstance(t, dict):
            continue
        if t.get('status') not in ('failed', 'blocked', 'repairing'):
            continue
        if not _task_matches(t, toks):
            continue
        stamp = int(t.get('failedAt') or t.get('blockedAt')
                    or t.get('completedAt') or 0)
        if stamp and now_ms - stamp > window:
            continue
        out.append({'source': 'task', 'taskId': tid,
                    'status': t.get('status'),
                    'title': str(t.get('title') or '')[:160],
                    'failNote': str(t.get('failNote') or '')[:220],
                    'blockedReason': t.get('blockedReason'),
                    'atMs': stamp})
    return out


def _ledger_rows(state, toks):
    out = []
    for entry in (state.get('_repairFailureLog') or []):
        if not isinstance(entry, dict):
            continue
        if not _matches_all(json.dumps(entry), toks):
            continue
        out.append({'source': 'ledger', 'key': entry.get('key'),
                    'count': entry.get('count'),
                    'lastTs': entry.get('lastTs'),
                    'entries': entry})
    return out


def _blob_fail_note(row, toks):
    cid, blob = row
    try:
        t = json.loads(blob) if isinstance(blob, str) else blob
    except Exception:
        return None
    if not isinstance(t, dict):
        return None
    if not _matches_all(
            ' '.join([t.get('failNote') or '', t.get('blockedReason') or '']),
            toks):
        return None
    return {'source': 'archive', 'taskId': cid, 'status': t.get('status'),
            'title': str(t.get('title') or '')[:160],
            'failNote': str(t.get('failNote') or '')[:220],
            'blockedReason': t.get('blockedReason')}


def _archive_rows(conn, toks):
    like = '%' + toks[0] + '%'
    import db_backend as _dbm
    try:
        if _dbm.is_postgres():
            rows = conn.execute(
                "SELECT id, blob FROM task_archive "
                "WHERE (blob::jsonb->>'failNote') ILIKE ? "
                "   OR (blob::jsonb->>'blockedReason') ILIKE ? "
                "LIMIT ?",
                (like, like, _SQL_PREFILTER_LIMIT),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT id, blob FROM task_archive LIMIT ?',
                (_SQL_PREFILTER_LIMIT,),
            ).fetchall()
    except Exception:
        return []
    out = []
    for row in rows:
        hit = _blob_fail_note(row, toks)
        if hit:
            out.append(hit)
    return out


def _failure_memory_search(q, limit, days):
    toks = _tokens(q)
    if not toks:
        return {'error': 'empty query'}
    now_ms = int(time.time() * 1000)
    since_ts = time.time() - max(1, min(int(days or 7), 90)) * 24 * 3600
    state = get_state_from_db('none') or {}
    with _db() as conn:
        log_hits = _log_rows(conn, toks, since_ts)
        archive_hits = _archive_rows(conn, toks)
    task_hits = _live_cards(state, toks, now_ms)
    ledger_hits = _ledger_rows(state, toks)
    limit = max(1, min(int(limit or _SEARCH_LIMIT), 200))
    total = (len(task_hits) + len(ledger_hits) + len(log_hits)
             + len(archive_hits))
    return {
        'query': q,
        'matches': (task_hits + ledger_hits + log_hits + archive_hits)[:limit],
        'counts': {'task': len(task_hits), 'ledger': len(ledger_hits),
                   'log': len(log_hits), 'archive': len(archive_hits)},
        'truncated': total > limit,
    }


@app.get('/api/failures')
async def api_failures(request: Request, q: str = '', limit: int = 0,
                       days: int = 7):
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    if not _tokens(q):
        return JSONResponse({'error': 'query parameter q is required'},
                            status_code=400)
    return JSONResponse(await asyncio.to_thread(
        _failure_memory_search, q, limit, days))
