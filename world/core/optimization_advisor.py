"""Deterministic optimization advisor over the think-tank health snapshot."""

_PRIORITY = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}
_CONFIDENCE = {'high': 3, 'medium': 2, 'low': 1}


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pct(value):
    if value is None:
        return None
    return _f(value)


def _credit_used_pct(snapshot):
    credits = snapshot.get('openrouter_credits') or {}
    if not isinstance(credits, dict):
        return None
    total = _f(credits.get('totalCredits'))
    used = _f(credits.get('totalUsage'))
    if total <= 0:
        return None
    return max(0.0, min(1.0, used / total))


def _rec(area, title, why, action, priority, confidence, evidence,
         expected_impact):
    return {
        'area': area,
        'title': title,
        'why': why,
        'action': action,
        'priority': priority,
        'confidence': confidence,
        'evidence': evidence,
        'expected_impact': expected_impact,
    }


def build_optimization_advice(snapshot, now=None):
    s = snapshot if isinstance(snapshot, dict) else {}
    recommendations = []

    if not s.get('db_ok', True):
        recommendations.append(_rec(
            'reliability',
            'Restore the state database before optimizing anything',
            'The health snapshot cannot read think_tank.db, so every metric is blind.',
            'Check disk space, permissions, and the database path; restore the latest backup if needed.',
            'critical', 'high', {'db_ok': False},
            'Unblocks all measurement and control.'))

    escapes = _i(s.get('red_pipeline_escapes_last_24h'))
    if escapes:
        recommendations.append(_rec(
            'quality',
            'Stop the quality-gate escape',
            f'{escapes} red-pipeline result(s) completed as done in the last 24 hours.',
            'Inspect the completion path and block done until the evidence check passes.',
            'critical', 'high',
            {'red_pipeline_escapes_last_24h': escapes},
            'Prevents false completions from corrupting the board.'))

    credit_pct = _credit_used_pct(s)
    if credit_pct is not None and credit_pct >= 0.85:
        recommendations.append(_rec(
            'budget',
            'Top up or throttle model spend',
            f'{credit_pct:.0%} of the deposited OpenRouter balance is spent.',
            'Top up the account or lower model tiers and best-of-N until the balance recovers.',
            'critical' if credit_pct >= 0.95 else 'high', 'high',
            {'openrouter_credit_used_pct': round(credit_pct, 4)},
            'Avoids a hard fail-closed stop on model calls.'))

    loops = _i(s.get('rework_loops_last_24h'))
    straight = _pct(s.get('straight_through_rate'))
    if loops >= 3 and (straight is None or straight < 0.6):
        why = (f'{loops} rework loop(s) in 24 hours; straight-through is {straight:.0%}.'
               if straight is not None else
               f'{loops} rework loop(s) in 24 hours.')
        recommendations.append(_rec(
            'rework',
            'Cut rework before adding capacity',
            why,
            'Group the top rejection signatures, fix the shared cause, and stop minting duplicate retry cards.',
            'high' if loops >= 8 or (straight is not None and straight < 0.4) else 'medium',
            'high',
            {'rework_loops_last_24h': loops,
             'straight_through_rate': straight,
             'loop_cost_usd_last_24h': round(_f(s.get('loop_cost_usd_last_24h')), 4)},
            'Fewer retries, less spend, faster cycle time.'))

    assigned = _i(s.get('task_assigned_last_24h'))
    fast = _pct(s.get('task_fast_completion_rate'))
    in_flight = _i(s.get('task_in_flight_last_24h'))
    if assigned >= 8 and fast is not None and fast < 0.5:
        recommendations.append(_rec(
            'throughput',
            'Reduce WIP or add review capacity',
            f'Only {fast:.0%} of {assigned} assigned task(s) finished within the fast horizon.',
            'Cap concurrent work and move capacity to the bottleneck stage, usually review or repair.',
            'high', 'medium',
            {'task_assigned_last_24h': assigned,
             'task_fast_completion_rate': fast,
             'task_in_flight_last_24h': in_flight},
            'Shortens queue time and makes completion measurable.'))
    elif in_flight >= 5:
        recommendations.append(_rec(
            'throughput',
            'Drain in-flight work before starting new cards',
            f'{in_flight} task(s) are still in flight from the last 24 hours.',
            'Pause new assignments, finish or retire the oldest cards, then reopen intake.',
            'medium', 'medium',
            {'task_in_flight_last_24h': in_flight},
            'Reduces context switching and stale work.'))

    aging = _i(s.get('aging_in_flight_work'))
    if aging:
        recommendations.append(_rec(
            'reliability',
            'Repair or retire aging in-flight work',
            f'{aging} non-bug task(s) are wedged past their work budget.',
            'Run the stale-work repair path; if the run is dead, fail it closed and requeue a smaller card.',
            'high' if aging >= 5 else 'medium', 'high',
            {'aging_in_flight_work': aging},
            'Frees agents and stops silent stalls.'))

    due = _i(s.get('work_queue_due_size'))
    agents = _i(s.get('agents_count'))
    if agents > 0 and due > agents * 2:
        recommendations.append(_rec(
            'staffing',
            'Queue is deeper than active capacity',
            f'{due} due work item(s) for {agents} agent(s).',
            'Activate more agents, split the queue by room, or reduce intake until the due queue is within one cycle.',
            'medium', 'medium',
            {'work_queue_due_size': due, 'agents_count': agents},
            'Prevents assignment starvation and long waits.'))

    kappa = s.get('review_grader_kappa')
    samples = _i(s.get('review_grader_samples'))
    if samples >= 10 and kappa is not None and _f(kappa) < 0.4:
        recommendations.append(_rec(
            'quality',
            'Recalibrate the review grader',
            f'Grader agreement is kappa {_f(kappa):.2f} over {samples} anchored grade(s).',
            'Audit the disagreements, tighten the rubric, and re-anchor the judge before trusting the score.',
            'medium', 'medium',
            {'review_grader_samples': samples, 'review_grader_kappa': _f(kappa)},
            'Improves the signal that gates quality and reviewer selection.'))

    missing = s.get('missing_model_tier_bands') or []
    if missing:
        recommendations.append(_rec(
            'routing',
            'Refresh model tiers before routing decisions',
            f'Missing tier band(s): {", ".join(str(x) for x in missing)}.',
            'Run the model-tier refresh and confirm the tiers reflect current price and benchmark data.',
            'medium', 'high',
            {'missing_model_tier_bands': list(missing)},
            'Keeps cost-aware routing honest.'))

    imbalance = _f(s.get('ceremony_imbalance_score'))
    if imbalance >= 1.0:
        recommendations.append(_rec(
            'coordination',
            'Shift effort from ceremony to shipped work',
            f'Coordination imbalance score is {imbalance:.2f}.',
            'Cut non-essential reviews, ceremonies, and escalations until shipped work outpaces process work.',
            'medium', 'medium',
            {'ceremony_imbalance_score': imbalance,
             'ceremony_signal': _f(s.get('ceremony_signal')),
             'progress_signal': _f(s.get('progress_signal'))},
            'More output for the same coordination cost.'))

    escalations = _i(s.get('open_escalations'))
    if escalations >= 2:
        recommendations.append(_rec(
            'escalation',
            'Clear the escalation backlog',
            f'{escalations} open escalation(s) are waiting on a human decision.',
            'Resolve or batch the decisions; if the same question repeats, encode a standing policy.',
            'medium', 'high',
            {'open_escalations': escalations},
            'Removes a player-side bottleneck.'))

    over_cap = s.get('bank_over_cap') or []
    if over_cap:
        recommendations.append(_rec(
            'budget',
            'Reallocate or raise the over-cap budget',
            f'Service(s) over cap: {", ".join(str(x) for x in over_cap)}.',
            'Move budget from low-yield work or raise the cap; keep spend tied to accepted deliverables.',
            'high', 'high',
            {'bank_over_cap': list(over_cap),
             'bank_used': round(_f(s.get('bank_used')), 4),
             'bank_cap': round(_f(s.get('bank_cap')), 4)},
            'Prevents a hard stop and funds the highest-value work.'))

    if not recommendations and assigned < 8:
        recommendations.append(_rec(
            'measurement',
            'Collect a baseline before tuning',
            f'Only {assigned} task(s) were assigned in the last 24 hours, too few to separate signal from noise.',
            'Run a controlled experiment: randomize model tier or assignment for the next 20 tasks and compare cost per accepted deliverable.',
            'low', 'high',
            {'task_assigned_last_24h': assigned},
            'Avoids optimizing noise and builds the data for routing and assignment policies.'))

    recommendations.sort(
        key=lambda r: (_PRIORITY.get(r['priority'], 0),
                       _CONFIDENCE.get(r['confidence'], 0)),
        reverse=True)
    top = recommendations[:5]
    bottleneck = top[0]['area'] if top else 'none'
    headline = top[0]['title'] if top else 'No dominant constraint detected.'
    return {
        'checked_at': s.get('checked_at') or now,
        'bottleneck': bottleneck,
        'headline': headline,
        'recommendations': top,
        'metrics': {
            'task_assigned_last_24h': assigned,
            'task_completed_last_24h': _i(s.get('task_completed_last_24h')),
            'task_fast_completion_rate': fast,
            'straight_through_rate': straight,
            'rework_loops_last_24h': loops,
            'loop_cost_usd_last_24h': round(_f(s.get('loop_cost_usd_last_24h')), 4),
            'work_queue_due_size': due,
            'agents_count': agents,
            'aging_in_flight_work': aging,
            'open_escalations': escalations,
            'openrouter_credit_used_pct': (round(credit_pct, 4)
                                            if credit_pct is not None else None),
        },
        'limitations': [
            'Deterministic advisor over the current health snapshot; it does not run experiments.',
            'Recommendations are hypotheses, not causal claims.',
            '24-hour windows are noisy at low volume.',
        ],
    }
