"""Supervisor budget autopilot (2026-11-28, operator directive).

A card that exhausted its model-spend budget is already closed + one-time
notified to its owning director, and a high-priority [BUDGET] supervise card
asks the director to grant or close. That path still waits on the director
actually acting. The operator wants the SUPERVISOR to decide without player
intervention, as soon as the exhaustion is known.

So this pass makes the determination server-side, on a short cadence, acting
AS the card's owning director (the same stand-in precedent the director
approval loop uses). It runs a real low-tier judgment over the diagnostic
package the exhaustion note already carries (budget vs spend, attempt count,
failure note, deliverable on record, overrun factor), then:

  grant -> calls the SAME enforced desk tool a human director would call
           (supervisor_grant_budget: team check, $5 hard cap, must exceed
           spend, reason recorded) and notifies the original author through
           the shared sim helper, so the worker learns the ceiling is raised
           and the re-queued card will reach them through the normal queue
           when they are free.
  close -> leaves the card failed and records the reason on the card and in
           the governance log; the director's mailbox note already exists.

Two-phase, single-writer-safe: phase 1 decides on a state snapshot and saves
the decision; phase 2 (next tick) re-reads fresh state and applies it through
the desk tool, so a human grant racing in between is never clobbered by a
whole-blob save. Model outage fail-closes to "leave it for the human" after a
bounded retry budget -- it never grants blind and never re-notifies forever.
"""

SUPERVISOR_BUDGET_AUTOPILOT_INTERVAL_S = 60
# Per-candidate retry budget for the judgment call: after this many failed
# attempts (with cooldown between them) the card is left to the human.
SUPERVISOR_BUDGET_MAX_LLM_ATTEMPTS = 6
SUPERVISOR_BUDGET_RETRY_COOLDOWN_MS = 240_000
# Judgment-call output cap: one JSON object, short reason.
_SUPERVISOR_BUDGET_DECISION_TOKENS = 160
# At most this many candidates are decided per pass, oldest exhaustion first.
_SUPERVISOR_BUDGET_MAX_PER_PASS = 3


def _supervisor_budget_candidates(state):
    """Failed, budget-exhausted cards that still need a supervisor decision:
    no decision recorded yet, no human grant on record, and the exhaustion
    actually notified. Oldest exhaustion first."""
    out = []
    for task in (state.get('tasks') or {}).values():
        if not isinstance(task, dict):
            continue
        if task.get('status') != 'failed' or not task.get('budgetExhausted'):
            continue
        if task.get('_supervisorBudgetDecision') or task.get('budgetGrants'):
            continue
        if not task.get('_budgetExhaustedNotified'):
            continue
        out.append(task)
    out.sort(key=lambda t: t.get('budgetExhaustedAtMs') or t.get('createdAt') or 0)
    return out[:_SUPERVISOR_BUDGET_MAX_PER_PASS]


def _supervisor_budget_context(state, task, spent, attempts):
    try:
        import core.budget as _budget
        factor = _budget.overrun_factor(
            state, room=task.get('room'), team_id=task.get('teamId'),
            product_id=task.get('productId'), task_type=task.get('taskType'))
    except Exception:
        factor = 1.0
    deliverable = next((task.get(f) for f in ('reportPath', 'libraryPath',
                                              'artifactPath', 'codePublished')
                        if task.get(f)), None)
    return (
        'You are the supervising director for one card that ran out of '
        'model-spend budget and was closed.\n'
        f'Task: "{task.get("title")}" (room {task.get("room")}, '
        f'type {task.get("taskType")}, id {task.get("id")}).\n'
        f'Ceiling: ${float(task.get("budgetUsd") or 0):.2f}. '
        f'Spent: ${float(spent):.4f} across {int(attempts)} model calls.\n'
        f'Failure note: {str(task.get("failNote") or "")[:300]}\n'
        f'Last agent note: {str(task.get("note") or "")[:300]}\n'
        f'Deliverable on record: {deliverable or "none"}.\n'
        + (f'Similar future cards now budget {factor:.2f}x their type default.\n'
           if factor and factor > 1.0 else '')
        + 'Decide as its supervisor. Reply with ONE JSON object only: '
        '{"decision": "grant" or "close", "budgetUsd": <new total ceiling, '
        'number>, "reason": "<one sentence>"}. Grant when the spend bought '
        'real progress and a bounded raise (at most 3x the old ceiling, never '
        'above $5.00) is likely to finish the work. Close when the work is '
        'wrong, unneeded, blocked by a real issue, or the spend bought nothing.')


def _parse_supervisor_decision(text):
    import json as _json
    import re as _re
    try:
        m = _re.search(r'\{.*\}', (text or ''), _re.DOTALL)
        data = _json.loads(m.group()) if m else _json.loads((text or '').strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    decision = data.get('decision')
    if decision not in ('grant', 'close'):
        return None
    amount = data.get('budgetUsd')
    amount = float(amount) if isinstance(amount, (int, float)) and amount > 0 else None
    reason = data.get('reason') if isinstance(data.get('reason'), str) else ''
    return {'decision': decision, 'budgetUsd': amount, 'reason': reason}


def _clamped_grant_amount(task, spent, parsed_amount):
    """The decision names an amount; the desk tool enforces the hard cap, so
    pre-clamp into the legal window: above what was spent, at or below the
    supervisor cap, and a floor so the gate would not refuse the next call."""
    ceiling = float(task.get('budgetUsd') or 0)
    low = round(max(0.05, float(spent) + 0.05), 2)
    default = round(max(float(spent) * 2.0, ceiling * 1.5, low), 2)
    amount = round(float(parsed_amount), 2) if parsed_amount else default
    return min(SUPERVISOR_BUDGET_CAP_USD, max(low, amount))


def _supervisor_budget_decide(state, task):
    """One real model call producing the supervisor's judgment, or None when
    the model could not be reached. Deterministic mid tier: granting money is
    too consequential for the cheapest lane, but this is a bounded verdict,
    not synthesis."""
    spent, attempts = _task_budget_spent(task.get('id'))
    model = _mid_tier_slug() or _low_tier_slug()
    if not model:
        return None
    data = _call_openrouter_sync(
        model,
        [{'role': 'system',
          'content': _supervisor_budget_context(state, task, spent, attempts)},
         {'role': 'user', 'content': 'Give your decision.'}],
        _SUPERVISOR_BUDGET_DECISION_TOKENS)
    return _parse_supervisor_decision((data.get('choices') or [{}])[0]
                                      .get('message', {}).get('content'))


def _supervisor_budget_step(state, decide=None, grant=None, save=None):
    """Phase 1 for at most one candidate per call: run the judgment and stamp
    the decision (saved by the caller or the injected `save`). Returns the
    number of cards that reached a decision this call. The `grant` callable is
    phase 2's applier (see _supervisor_budget_apply) -- injection keeps the
    step hermetic for tests."""
    now_ms = int(time.time() * 1000)
    candidates = _supervisor_budget_candidates(state)
    if not candidates:
        return 0
    task = candidates[0]
    decide = decide or _supervisor_budget_decide
    retry = task.setdefault('_supervisorBudgetLlm', {'attempts': 0})
    if now_ms - int(retry.get('lastAtMs') or 0) < SUPERVISOR_BUDGET_RETRY_COOLDOWN_MS:
        return 0
    if int(retry.get('attempts') or 0) >= SUPERVISOR_BUDGET_MAX_LLM_ATTEMPTS:
        task['_supervisorBudgetDecision'] = {
            'decision': 'deferred', 'at': now_ms,
            'reason': 'autopilot could not reach a model; left for the human'}
        log_action(None, 'supervisor_budget_autopilot_deferred',
                    {'taskId': task.get('id'),
                     'attempts': retry.get('attempts')}, authorized=True)
        if save:
            save(state)
        return 1
    try:
        parsed = decide(state, task)
    except Exception:
        parsed = None
    if not parsed:
        retry['attempts'] = int(retry.get('attempts') or 0) + 1
        retry['lastAtMs'] = now_ms
        return 0
    spent, attempts = _task_budget_spent(task.get('id'))
    task['_supervisorBudgetDecision'] = {
        'decision': parsed['decision'],
        'budgetUsd': (_clamped_grant_amount(task, spent, parsed.get('budgetUsd'))
                      if parsed['decision'] == 'grant' else None),
        'reason': parsed['reason'][:300], 'at': now_ms, 'applied': False}
    if save:
        save(state)
    log_action(None, 'supervisor_budget_autopilot_decided',
                {'taskId': task.get('id'), 'decision': parsed['decision'],
                 'budgetUsd': task['_supervisorBudgetDecision']['budgetUsd'],
                 'reason': parsed['reason'][:300]}, authorized=True)
    return 1


def _supervisor_budget_apply(state, grant=None, save=None):
    """Phase 2: apply a decided-but-unapplied decision. Grant goes through the
    SAME enforced desk tool a human director uses (team check, hard cap,
    must-exceed-spend), on freshly read state, so a human grant that raced in
    is detected and never clobbered. Close just stamps + saves. Returns the
    number of decisions applied."""
    decided = [t for t in (state.get('tasks') or {}).values()
               if isinstance(t, dict) and t.get('status') == 'failed'
               and t.get('budgetExhausted')
               and (t.get('_supervisorBudgetDecision') or {}).get('applied') is False]
    if not decided:
        return 0
    task = decided[0]
    decision = task['_supervisorBudgetDecision']
    if task.get('budgetGrants'):
        # A human granted while the decision was pending; nothing to apply.
        decision['applied'] = True
        decision['humanGrantObserved'] = True
        if save:
            save(state)
        return 1
    if decision['decision'] != 'grant':
        decision['applied'] = True
        task['failNote'] = ((task.get('failNote') or '').strip()
                            + f" Supervisor closed it: {decision['reason']}")[:500]
        if save:
            save(state)
        return 1
    import sim as _sim_mod
    director_id = _sim_mod._task_owner_director(state, task)
    if not director_id:
        decision['applied'] = True
        decision['reason'] = 'no owning director found; ' + decision['reason']
        if save:
            save(state)
        return 1
    reason = (f'autopilot grant as {director_id}: {decision["reason"]} '
              f'(ceiling ${decision["budgetUsd"]:.2f} after exhausting '
              f'${float(task.get("budgetUsd") or 0):.2f})')
    grant = grant or supervisor_grant_budget
    ok, out = grant(director_id, task.get('id'), decision['budgetUsd'], reason)
    decision['applied'] = True
    decision['grantOk'] = bool(ok)
    if not ok:
        decision['reason'] = f'desk tool refused: {out} ' + decision['reason']
    if save:
        save(state)
    return 1


async def _supervisor_budget_autopilot_loop():
    """One cadence loop driving both phases. Phase 1 and phase 2 each run on
    their own fresh state read, so the desk tool's internal save is never
    racing a stale whole-blob write from this loop."""
    while True:
        await asyncio.sleep(SUPERVISOR_BUDGET_AUTOPILOT_INTERVAL_S)
        try:
            await asyncio.to_thread(_supervisor_budget_autopilot_pass)
        except Exception as e:
            print(f'[supervisor-budget] loop error: {e}', flush=True)


def _supervisor_budget_autopilot_pass():
    state = get_state_from_db() or {}
    decided = _supervisor_budget_step(state)
    if decided:
        save_state_to_db(state)
    # Phase 2 on a FRESH read: the phase-1 save (or the desk tool's own save)
    # must be visible before anything is applied.
    state = get_state_from_db() or {}
    _supervisor_budget_apply(state)
    return decided
