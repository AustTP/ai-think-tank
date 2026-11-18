# ---------------------------------------------------------------------------
# Unified briefing (2026-11-18, Priority 3 of the coherence review): ONE
# endpoint answering the four questions the review said the product must
# answer immediately -- what changed, what do we now believe, what remains
# unproven or blocked, what should I do next. It JOINS existing truth stores
# (it never duplicates them): the epistemic ledger for beliefs/unknowns/
# contradictions, investigations for open cases, escalations.json for
# decisions awaiting the player, action_log for material activity, and the
# story-budget ledger for spend posture. Read-only, operator/admin gated;
# every section degrades to an empty list when its store is unavailable so
# one wedged store cannot blank the whole briefing.
# ---------------------------------------------------------------------------
import json as _json


def _brief_beliefs(now_ms):
    """What we now believe + what is still open, from the ledger."""
    try:
        import core.epistemics as epistemics
        items = (epistemics.query_items(limit=500) or {}).get('items') or []
        established = [i for i in items if i.get('status') == 'validated']
        disputed = [i for i in items
                    if i.get('status') in ('disputed', 'contradicted')]
        open_items = [i for i in items
                      if i.get('status') in ('unknown', 'hypothesized',
                                             'evidenced')]
        stale = [i for i in items
                 if epistemics.effective_status(i, now_ms) == 'stale']
        brief = {
            'establishedCount': len(established),
            'established': [{'id': i['id'], 'text': i['text'][:200]}
                            for i in established[:5]],
            'disputedCount': len(disputed),
            'disputed': [{'id': i['id'], 'text': i['text'][:200]}
                         for i in disputed[:5]],
            'openCount': len(open_items),
            'open': [{'id': i['id'], 'text': i['text'][:200]}
                     for i in open_items[:5]],
            'staleCount': len(stale),
            'stale': [{'id': i['id'], 'text': i['text'][:120]}
                      for i in stale[:5]],
        }
        return brief, None
    except Exception as e:  # noqa: BLE001 -- degrade, never blank the brief
        return None, str(e)


def _brief_investigations():
    try:
        import core.investigations as investigations
        cases = investigations.list_investigations() or []
        open_cases = [c for c in cases if c.get('disposition') == 'open']
        return {
            'openCount': len(open_cases),
            'overdue': [c['id'] for c in open_cases if c.get('overdue')],
            'reassessmentDue': [c['id'] for c in open_cases
                                if c.get('reassessmentDue')],
            'contradictedCases': [c['id'] for c in open_cases
                                  if c.get('validationState')
                                  == 'contradicted'],
            'open': [{'id': c['id'], 'question': c['question'][:160],
                      'owner': c.get('owner'),
                      'validationState': c.get('validationState')}
                     for c in open_cases[:5]],
        }, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _brief_decisions_for_player():
    """Pending escalations: the approve/deny queue that waits on the
    player (escalations.json; each row's token gate is unchanged -- this
    only LISTS that they exist, never exposes tokens)."""
    try:
        with open(ESCALATIONS_PATH, encoding='utf-8') as fh:
            escalations = _json.load(fh)
        pending = [{'id': esc_id,
                    'kind': (esc or {}).get('kind'),
                    'openedAt': (esc or {}).get('openedAt')}
                   for esc_id, esc in (escalations or {}).items()
                   if isinstance(esc, dict)
                   and esc.get('status') == 'pending']
        return {'pendingCount': len(pending), 'pending': pending[:8]}, None
    except FileNotFoundError:
        return {'pendingCount': 0, 'pending': []}, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _brief_activity(since_ts):
    """Material change counts from action_log over the window."""
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT action, COUNT(*) AS n FROM action_log '
                'WHERE ts >= ? GROUP BY action ORDER BY n DESC LIMIT 12',
                (since_ts,)).fetchall()
        return ({'sinceTs': since_ts,
                 'actions': [{'action': a, 'count': n} for a, n in rows]},
                None)
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _brief_spend():
    """Spend posture from the story-budget ledger in state."""
    try:
        state = get_state_from_db() or {}
        import agents.story_budget as story_budget
        ledger = story_budget.BudgetLedger.from_state(
            state.get('budgetLedger'))
        if ledger is None:
            return {'overrunFactor': 0.0, 'alerts': 0}, None
        return {'overrunFactor': ledger.overrun_factor(),
                'alerts': len(ledger.alerts() or [])}, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _build_briefing(days):
    now_ms = int(time.time() * 1000)
    since_ts = time.time() - max(1, min(int(days or 1), 14)) * 24 * 3600
    beliefs, beliefs_err = _brief_beliefs(now_ms)
    cases, cases_err = _brief_investigations()
    decisions, decisions_err = _brief_decisions_for_player()
    activity, activity_err = _brief_activity(since_ts)
    spend, spend_err = _brief_spend()
    sections = {'beliefs': beliefs, 'investigations': cases,
                'decisionsForPlayer': decisions, 'activity': activity,
                'spend': spend}
    errors = {k: v for k, v in [('beliefs', beliefs_err),
                                ('investigations', cases_err),
                                ('decisionsForPlayer', decisions_err),
                                ('activity', activity_err),
                                ('spend', spend_err)] if v}
    return {'sections': sections, 'sectionErrors': errors,
            'generatedAtMs': now_ms}


@app.get('/api/briefing')
async def api_briefing(request: Request, days: int = 1):
    """The daily intelligence brief: what changed / what we now believe /
    what remains unproven or blocked / what needs the player. Operator/admin
    only."""
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    return JSONResponse(await asyncio.to_thread(_build_briefing, days))
