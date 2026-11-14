"""Scheduled items (2026-11-07): team-owned recurring cards.

The org could only schedule two ways before this: the player's one-offs
(`assignedTo` accepts just "player") and player-managed recurring research
topics. Nobody else could OWN a recurring item -- the chief of staff, a
regional VP, a senior director, or a worker could not carry a standing
duty like "quarterly review of the technical agreements", and teams could
not self-schedule. This module adds state['scheduledItems']: an owner
role + cadence + deliverable contract the engine turns into a directed
card each period (165__scheduled_items.py), reusing queue_work so dedup,
refinement pins, and the completion gate all apply unchanged.

The first standing item is the operator-directed regional VP daily scan:
every day each village's RVP reads the last 24h of Telegram alerts
(`write_telegram_alert_digest` lands them in the Library first, so the
card needs only read_library_file) and triages which alerts were real
issues that a director should address.
"""
from __future__ import annotations

import datetime
import json
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse

OWNER_ROLES = ('chief-of-staff', 'regional-vp', 'senior-director',
               'recursive-director', 'worker')
CADENCES = ('30min', 'hourly', 'daily', 'weekly', 'monthly', 'quarterly')
_CADENCE_MS = {
    '30min': 30 * 60 * 1000,
    'hourly': 3600 * 1000,
    'daily': 24 * 3600 * 1000,
    'weekly': 7 * 24 * 3600 * 1000,
    'monthly': 30 * 24 * 3600 * 1000,
    'quarterly': 91 * 24 * 3600 * 1000,
}


def telegram_digest_path(day=None):
    """Library path (no `library/` prefix) for one day's alert digest."""
    day = day or datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    return f'analysis/telegram-alert-digest-{day}.md'


def telegram_digest_scan_path(now=None):
    """Library path for ONE scan's digest. Per-scan files, not a rolling
    daily file: a 30-minute scan overwriting the day's 24h digest destroyed
    the record (the RVP's second scan read a 0-alert 0.25h window where the
    24h triage source used to be). Scans accumulate; nothing is lost."""
    dt = datetime.datetime.fromtimestamp(
        now if now is not None else time.time(), datetime.timezone.utc)
    return (f'analysis/telegram-alert-digest-{dt.strftime("%Y-%m-%d")}'
            f'-{dt.strftime("%H%M")}.md')


def _staging_usage():
    """(bytes, files) across world/.selfedit_staging -- the selfedit
    hygiene loops' own scratch. Surfaced by the digest so the VP sees when
    it balloons instead of discovering it at a full disk."""
    root = os.path.join(config.ROOT, '.selfedit_staging')
    total = files = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                    files += 1
                except OSError:
                    pass
    except OSError:
        return 0, 0
    return total, files


def write_telegram_alert_digest(now=None, since_ts=None):
    """Land the last 24h of outbound Telegram alerts into the Library so the
    regional VP's daily scan card can read them with read_library_file.
    Returns the stored path, or None when nothing fired (an empty day is a
    real result the digest still records -- write it anyway, cheap and it
    proves the scan ran rather than silently skipping)."""
    now = now or time.time()
    # Incremental scan window: a scheduled item stamps lastRunAtMs each
    # period, so a high-frequency scan (30min) reads only what fired SINCE
    # the last scan instead of re-reading the same 24h. First run: 24h.
    cutoff = max(since_ts or 0, now - 24 * 3600) if since_ts else now - 24 * 3600
    rows = []
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT ts, details FROM action_log '
                "WHERE action IN ('telegram_sent', 'telegram_digest_only') "
                'AND ts >= ? ORDER BY ts ASC',
                (cutoff,)).fetchall()
    except Exception:
        pass
    by_class = {}
    for ts, details_raw in rows:
        try:
            d = json.loads(details_raw) if isinstance(details_raw, str) else details_raw
        except Exception:
            d = {}
        preview = str(d.get('preview') or '')
        # Class by the alert headline inside the preview: the same sender
        # formats every message as "[AI Think Tank] <headline>\n\n...".
        headline = preview.split(']', 1)[-1].split('\n', 1)[0].strip() \
            if preview.startswith('[AI Think Tank]') else (preview[:60] or '(no preview)')
        by_class.setdefault(headline, []).append(
            (int(ts), preview.split('\n', 3)[-1][:220]))
    # Internal tank signals (owner direction 2026-11-11): governance events
    # that never ping the operator's Telegram are invisible to this scan, so
    # the VP cannot triage them. Surface the curated set from action_log
    # directly -- these are exactly the anomalies a live operator session
    # monitors (pattern suppressions, repeat-failure freezes, cap closures,
    # walk-churn storms, hollow grades, content-thread failures, sweeps that
    # could not run).
    _INTERNAL_SIGNAL_ACTIONS = (
        'card_pattern_suppressed', 'repair_loop_stopped',
        'operator_veto_window_expired', 'task_auto_closed',
        'task_requeued_unreachable', 'task_parked_unreachable',
        'grade_artifact_mismatch', 'scheduled_item_skipped',
        'operator_quality_blocks_cancelled')
    internal_rows = []
    try:
        with _db() as conn:
            marks = ','.join('?' * len(_INTERNAL_SIGNAL_ACTIONS))
            internal_rows = conn.execute(
                'SELECT ts, action, details FROM action_log '
                f'WHERE action IN ({marks}) AND ts >= ? ORDER BY ts ASC',
                (*_INTERNAL_SIGNAL_ACTIONS, cutoff)).fetchall()
    except Exception:
        internal_rows = []
    internal_by_class = {}
    for row in internal_rows:
        if len(row) < 3:
            continue  # a legacy or mocked cursor: skip, never crash the scan
        ts, action, details_raw = row[0], row[1], row[2]
        try:
            d = json.loads(details_raw) if isinstance(details_raw, str) else details_raw
        except Exception:
            d = {}
        tail = ''
        if action == 'task_requeued_unreachable':
            tail = f"{d.get('agent')} could not reach {d.get('room')}"
        elif action in ('card_pattern_suppressed', 'repair_loop_stopped'):
            tail = str(d.get('taskId') or '')
        else:
            tail = str(d.get('title') or d.get('taskId') or d.get('path') or '')
        internal_by_class.setdefault(action, []).append(
            (int(ts), tail[:160]))
    day = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    window_h = max(0.25, round((now - cutoff) / 3600.0, 2))
    lines = [
        f'# Telegram alert digest, last {window_h}h (UTC {day})',
        '',
        f'Source: action_log rows action=telegram_sent since UTC '
        f'{datetime.datetime.fromtimestamp(cutoff, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")}. '
        f'Total alerts: {len(rows)}.',
        '',
        'Your job (operator direction, 2026-11-07): triage which of these were',
        'REAL issues the tank should have handled itself -- recurring failure',
        'classes, alerts that fired while the condition was already resolved,',
        'alerts that should have gone to a director instead of the operator --',
        'and file one issue per genuine problem for the owning director. Do',
        'not re-litigate one-off failures the tank already recovered from.',
        '',
    ]
    for headline in sorted(by_class, key=lambda h: -len(by_class[h])):
        occurrences = by_class[headline]
        lines.append(f'## {headline} (x{len(occurrences)})')
        for ts, tail in occurrences[:6]:
            stamp = datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime('%H:%M')
            lines.append(f'- {stamp} UTC: {tail}'.rstrip())
        if len(occurrences) > 6:
            lines.append(f'- ... {len(occurrences) - 6} more')
        lines.append('')
    lines.append('## Internal tank signals (these never pinged the operator;')
    lines.append('triage them the same way -- which were REAL issues the tank')
    lines.append('should have handled itself?)')
    lines.append('')
    for action in sorted(internal_by_class, key=lambda a: -len(internal_by_class[a])):
        occurrences = internal_by_class[action]
        lines.append(f'### {action} (x{len(occurrences)})')
        for ts, tail in occurrences[:6]:
            stamp = datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime('%H:%M')
            lines.append(f'- {stamp} UTC: {tail}'.rstrip())
        if len(occurrences) > 6:
            lines.append(f'- ... {len(occurrences) - 6} more')
        lines.append('')
    # Selfedit integrity reconciliation (owner direction 2026-11-14): the
    # normalized-deviation review found deviations this scan could not see
    # -- windows that processed without their approval chain, rollbacks
    # whose candidate copy failed to land, crash counts, and staging
    # scratch growth. Fold the checks in so the VP's triage sees them next
    # to the alerts instead of nowhere.
    integrity_lines = []
    try:
        week_cutoff = now - 7 * 24 * 3600
        with _db() as conn:
            counts = dict(conn.execute(
                'SELECT action, COUNT(*) FROM action_log WHERE action IN '
                "(?, ?, ?) AND ts >= ? GROUP BY action",
                ('self_edit_rolled_back', 'self_edit_rollback_failed',
                 'selfedit_crashed', week_cutoff)).fetchall())
            unescalated = conn.execute(
                'SELECT id, status, created FROM dormancy_windows '
                "WHERE status IN ('approved', 'fired') "
                "AND COALESCE(escalation_id, '') = '' "
                "AND COALESCE(validator, '') = '' "
                'ORDER BY created DESC LIMIT 5').fetchall()
        stage_bytes, stage_files = _staging_usage()
        integrity_lines.append(
            '- 7d trend: rollbacks=%s, rollback_failures=%s, '
            'selfedit_crashed=%s' % (
                counts.get('self_edit_rolled_back', 0),
                counts.get('self_edit_rollback_failed', 0),
                counts.get('selfedit_crashed', 0)))
        if counts.get('self_edit_rollback_failed'):
            integrity_lines.append(
                '- INVARIANT BREACH: a rollback ran but its candidate copy '
                'did not land -- recover it from .selfedit_backups/*.cand '
                'before the scratch is pruned')
        for wid, status, created in unescalated:
            stamp = datetime.datetime.fromtimestamp(
                created, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')
            integrity_lines.append(
                f'- window {wid} ({status}, created {stamp} UTC) processed '
                'with NO escalation_id and NO validator -- expected only '
                'when the operator explicitly waived the notice gate; '
                'otherwise the approval chain silently did not run')
        integrity_lines.append(
            '- .selfedit_staging: %.0f MB in %d files (hygiene loops own '
            'this scratch; growth past a few GB means a prune pass is '
            'failing)' % (stage_bytes / (1024 * 1024), stage_files))
    except Exception:
        integrity_lines = []
    if integrity_lines:
        lines.append('## Selfedit integrity (7d trend + invariants)')
        lines.append('')
        lines.extend(integrity_lines)
        lines.append('')
    rel = telegram_digest_scan_path(now)
    target = _safe_library_path(rel)
    if not target:
        return None
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'w') as f:
        f.write('\n'.join(lines))
    log_action('system', 'telegram_digest_written',
               {'path': rel, 'alerts': len(rows), 'classes': len(by_class),
                'internalSignals': len(internal_rows),
                'internalClasses': len(internal_by_class)},
               authorized=True)
    return rel


def _validate_scheduled_item(body):
    """Shared validation for the create/edit endpoints. Returns (item, error)."""
    title = str(body.get('title') or '').strip()
    if not title:
        return None, 'title is required'
    owner_role = str(body.get('ownerRole') or '').strip()
    if owner_role not in OWNER_ROLES:
        return None, f'ownerRole must be one of {", ".join(OWNER_ROLES)}'
    cadence = str(body.get('cadence') or '').strip()
    if cadence not in CADENCES:
        return None, f'cadence must be one of {", ".join(CADENCES)}'
    deliverable = str(body.get('deliverable') or 'report').strip()
    if deliverable not in ('code', 'report', 'note', 'meeting', 'supervision', 'review'):
        return None, 'deliverable must be a valid deliverable'
    item = {
        'title': title,
        'ownerRole': owner_role,
        'cadence': cadence,
        'deliverable': deliverable,
        'instructions': str(body.get('instructions') or '').strip() or None,
        'villageId': (str(body.get('villageId') or '').strip() or None),
        'teamId': (str(body.get('teamId') or '').strip() or None),
        'agentId': (str(body.get('agentId') or '').strip() or None),
        'feature': (str(body.get('feature') or '').strip() or None),
    }
    return item, None


@app.post('/api/intent/schedule-recurring')
@_state_writer
async def intent_schedule_recurring(request: Request):
    """Schedule a team-owned recurring card: {title, ownerRole, cadence,
    deliverable?, instructions?, villageId?/teamId?/agentId?, feature?}.
    The engine (165__scheduled_items.py) resolves the owner role to live
    agent(s) each period and mints a directed card -- so the org's standing
    duties (RVP daily alert scan, quarterly agreement reviews) live in the
    same queue, gates, and dedup as every other card."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    item, err = _validate_scheduled_item(body if isinstance(body, dict) else {})
    if err:
        return JSONResponse({'error': err}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    items = state.setdefault('scheduledItems', [])
    if not isinstance(items, list):
        state['scheduledItems'] = items = []
    seq = int(state.get('scheduledItemSeq') or 0) + 1
    state['scheduledItemSeq'] = seq
    item.update({'id': f'sch-{seq}', 'enabled': True, 'lastRunAtMs': 0})
    items.append(item)
    save_state_to_db(state)
    log_action('player', 'scheduled_item_created',
               {'id': item['id'], 'title': item['title'][:120],
                'ownerRole': item['ownerRole'], 'cadence': item['cadence']},
               authorized=True)
    return JSONResponse({'ok': True, 'item': item})


@app.patch('/api/schedules/item/{item_id}')
@_state_writer
async def patch_scheduled_item(item_id: str, request: Request):
    """Edit or toggle one scheduled item. Accepts any subset of {enabled,
    title, ownerRole, cadence, deliverable, instructions, villageId, teamId,
    agentId}."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await request.json()
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    item = next((i for i in (state.get('scheduledItems') or [])
                 if isinstance(i, dict) and i.get('id') == item_id), None)
    if not item:
        return JSONResponse({'error': 'unknown scheduled item'}, status_code=404)
    body = body if isinstance(body, dict) else {}
    if 'enabled' in body:
        item['enabled'] = bool(body['enabled'])
    merged = {k: item.get(k) for k in ('title', 'ownerRole', 'cadence',
                                       'deliverable', 'instructions',
                                       'villageId', 'teamId', 'agentId')}
    for k in ('title', 'ownerRole', 'cadence', 'deliverable', 'instructions',
              'villageId', 'teamId', 'agentId'):
        if k in body:
            merged[k] = body[k]
    validated, err = _validate_scheduled_item(merged)
    if err:
        return JSONResponse({'error': err}, status_code=400)
    item.update(validated)
    save_state_to_db(state)
    log_action('player', 'scheduled_item_edited', {'id': item_id}, authorized=True)
    return JSONResponse({'ok': True, 'item': item})


@app.delete('/api/schedules/item/{item_id}')
@_state_writer
async def delete_scheduled_item(item_id: str, request: Request):
    """Remove one scheduled item."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    items = state.get('scheduledItems') or []
    kept = [i for i in items
            if not (isinstance(i, dict) and i.get('id') == item_id)]
    if len(kept) == len(items):
        return JSONResponse({'error': 'unknown scheduled item'}, status_code=404)
    state['scheduledItems'] = kept
    save_state_to_db(state)
    log_action('player', 'scheduled_item_deleted', {'id': item_id}, authorized=True)
    return JSONResponse({'ok': True})
