# ---------------------------------------------------------------------------
# Outcome metrics (2026-11-18, Priority 5 of the coherence review): does the
# tank produce validated knowledge at reasonable cost, or merely activity?
# Everything here is computed from data the tank already records -- no new
# logging. Where a metric is NOT yet derivable (task reopen rate, duplicate
# work rate, human acceptance) it is reported under 'unavailable' with the
# reason, per the review's honesty bar: a missing metric stays missing, it
# is not approximated.
#
# operator/admin gated, read-only.
# ---------------------------------------------------------------------------
import statistics


def _knowledge_metrics(ledger, now_ms):
    import core.epistemics as epistemics
    items = ledger.get('items') or {}
    established = [it for it in items.values()
                   if it.get('status') == 'validated']
    corrob2 = [it for it in established
               if epistemics.independent_source_count(it) >= 2]
    hours = []
    for it in established:
        created = it.get('created_ts')
        if not created:
            continue
        for h in (it.get('history') or []):
            if h.get('to') == 'validated' and h.get('ts'):
                hours.append((h['ts'] - created) / 3600.0)
                break
    stale = [it for it in items.values()
             if epistemics.effective_status(it, now_ms) == 'stale']
    return {
        'itemsTotal': len(items),
        'establishedCount': len(established),
        'establishedWith2PlusSources': len(corrob2),
        'establishedCorroborationRate': round(len(corrob2) / len(established), 3)
            if established else None,
        'medianHoursUnknownToEstablished': round(statistics.median(hours), 2)
            if len(hours) >= 3 else None,
        'staleCount': len(stale),
    }


def _validation_metrics(ledger):
    validations = ledger.get('validations') or {}
    by_status = {}
    for v in validations.values():
        s = v.get('status') or 'unknown'
        by_status[s] = by_status.get(s, 0) + 1
    reviewed = by_status.get('reviewed', 0)
    rejected = by_status.get('rejected', 0)
    completed = by_status.get('completed', 0)
    decided = reviewed + rejected
    return {
        'validationsTotal': len(validations),
        'byStatus': by_status,
        'reviewGateRate': round(reviewed / decided, 3) if decided else None,
        'rejectionRate': round(rejected / decided, 3) if decided else None,
        'awaitingReview': completed,
    }


def _contradiction_metrics(ledger):
    contradictions = ledger.get('contradictions') or {}
    by_status = {}
    for c in contradictions.values():
        s = c.get('status') or 'unknown'
        by_status[s] = by_status.get(s, 0) + 1
    return {'contradictionsTotal': len(contradictions), 'byStatus': by_status}


def _routing_shadow_metrics():
    """The foundry pilot's agreement rate: how often the tiny net matches
    the live Jev lane. This is the promotion signal for fast-path routing."""
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT details FROM action_log '
                "WHERE action = 'route_shadow_classified' "
                'ORDER BY ts DESC LIMIT 2000').fetchall()
    except Exception as e:  # noqa: BLE001 -- report, never blank the readout
        return {'error': str(e)}
    total = agree = 0
    for (details,) in rows:
        try:
            import json as _json
            d = _json.loads(details) if isinstance(details, str) else details
        except Exception:
            continue
        if isinstance(d, dict) and 'agree' in d:
            total += 1
            agree += 1 if d.get('agree') else 0
    if not total:
        return {'shadowRows': 0, 'agreeRate': None}
    return {'shadowRows': total, 'agree': agree,
            'agreeRate': round(agree / total, 3)}


def _spend_metrics():
    try:
        ledger = _spend_ledger_read() or {}
        used = 0.0
        calls = 0
        for bucket in ledger.values():
            if isinstance(bucket, dict):
                used += float(bucket.get('used', 0) or 0)
                calls += int(bucket.get('calls', 0) or 0)
        return ({'trackedSpendUsd': round(used, 4), 'pricedCalls': calls},
                None)
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _outcome_metrics():
    import core.epistemics as epistemics
    now_ms = int(time.time() * 1000)
    ledger = epistemics._ensure_shape(epistemics._ledger_read())
    errors = {}
    spend, spend_err = _spend_metrics()
    if spend_err:
        errors['spend'] = spend_err
    shadow = _routing_shadow_metrics()
    metrics = {
        'knowledge': _knowledge_metrics(ledger, now_ms),
        'validation': _validation_metrics(ledger),
        'contradictions': _contradiction_metrics(ledger),
        'routingShadow': shadow,
        'spend': spend,
        'unavailable': [
            {'metric': 'taskReopenRate',
             'reason': 'reopen events are not logged as a distinct '
                       'action kind yet'},
            {'metric': 'duplicateWorkRate',
             'reason': 'needs cross-task semantic comparison, not yet '
                       'recorded'},
            {'metric': 'humanAcceptanceRate',
             'reason': 'player corrections are not tagged on decisions yet'},
        ],
    }
    return {'metrics': metrics, 'errors': errors,
            'generatedAtMs': now_ms}


@app.get('/api/outcome-metrics')
async def api_outcome_metrics(request: Request):
    """The 'is the system getting smarter or just busier' readout:
    knowledge quality, review-gate rates, contradiction flow, route-shadow
    agreement, and tracked spend. Operator/admin only."""
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    return JSONResponse(await asyncio.to_thread(_outcome_metrics))
