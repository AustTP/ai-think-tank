"""Supervisor budget autopilot (2026-11-28, operator directive).

A card that exhausted its model-spend budget is already closed + one-time
notified to its owning director, and a high-priority [BUDGET] supervise card
asks the director to grant or close. That path still waits on the director
actually acting. The operator wants the SUPERVISOR to decide without player
intervention, as soon as the exhaustion is known.

So this pass makes the determination server-side, on a short cadence, acting
AS the card's owning director (the same stand-in precedent the director
approval loop uses). Before judging, it runs a BOUNDED negotiation so the
decision rests on the worker's own account, not just the ledger:

  1. the WORKER (the card's original author) reports, as one low-tier JSON
     call: what work is already complete, why the budget was not enough, and
     how much MORE budget the remaining work needs;
  2. the DIRECTOR weighs the diagnostic package plus that report and either
     concludes or asks the worker ONE follow-up; a asked follow-up gets one
     worker reply, then the director must conclude. At most 4 model calls per
     decision, and a failed consult never blocks deciding from the record.

Then:

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


def _supervisor_budget_context(state, task, spent, attempts, worker=None):
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
    worker_lines = ''
    if worker:
        worker_lines = (
            '\nThe author of this card reported, before your decision:\n'
            f'- Work already complete: {worker.get("completed") or "not provided"}\n'
            f'- Why the budget was not enough: {worker.get("whyOver") or "not provided"}\n'
            f'- More budget the author says the remaining work needs: '
            + (f'${worker["moreBudgetUsd"]:.2f}'
               if worker.get('moreBudgetUsd') is not None else 'not provided') + '\n'
            + (f'- Director follow-up asked: {worker.get("followup")}\n'
               f'- Author reply: {worker.get("reply")}\n'
               if worker.get('followup') else ''))
    else:
        worker_lines = ('\nThe author could not be consulted; decide from the '
                        'record below.\n')
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
        + worker_lines
        + 'Decide as its supervisor. Reply with ONE JSON object only: '
        '{"decision": "grant" or "close", "budgetUsd": <new total ceiling, '
        'number>, "reason": "<one sentence>"}. Grant when the spend bought '
        'real progress and a bounded raise (at most 3x the old ceiling, never '
        'above $5.00) is likely to finish the work. Close when the work is '
        'wrong, unneeded, blocked by a real issue, or the spend bought nothing.')


def _supervisor_budget_worker_prompt(task, spent, attempts, question=None):
    return (
        f'You are {task.get("assignedTo")}, the author of the card '
        f'"{task.get("title")}". It exhausted its '
        f'${float(task.get("budgetUsd") or 0):.2f} model-spend budget after '
        f'${float(spent):.4f} across {int(attempts)} model calls and was '
        'closed. Your director wants your account before deciding on more '
        'budget. Answer from what you actually did on the card.\n'
        'Reply with ONE JSON object only: {"completed": "<what work is '
        'already finished>", "whyOver": "<why the budget was not enough>", '
        '"moreBudgetUsd": <how many MORE dollars the remaining work needs, '
        'number>, "reason": "<one sentence>"}.'
        + (f'\nAlso answer your director\'s follow-up in the same fields: '
           f'{question}' if question else ''))


def _parse_worker_report(text):
    import json as _json
    import re as _re
    try:
        m = _re.search(r'\{.*\}', (text or ''), _re.DOTALL)
        data = _json.loads(m.group()) if m else _json.loads((text or '').strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    more = data.get('moreBudgetUsd')
    more = float(more) if isinstance(more, (int, float)) and more > 0 else None
    return {'completed': str(data.get('completed') or '')[:400],
            'whyOver': str(data.get('whyOver') or '')[:400],
            'moreBudgetUsd': more,
            'reason': str(data.get('reason') or '')[:300]}


def _supervisor_budget_worker_consult(state, task, spent, attempts,
                                      question=None):
    """The worker side of the negotiation: one low-tier call in the author's
    voice reporting completed work, the shortfall reason and the ask. Or None
    when the model could not be reached."""
    model = _low_tier_slug() or _mid_tier_slug()
    if not model:
        return None
    data = _call_openrouter_sync(
        model,
        [{'role': 'system',
          'content': _supervisor_budget_worker_prompt(
              task, spent, attempts, question=question)},
         {'role': 'user', 'content': 'Give your report.'}],
        240)
    return _parse_worker_report((data.get('choices') or [{}])[0]
                                .get('message', {}).get('content'))


def _parse_supervisor_decision(text, allow_ask=False):
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
    if decision in ('grant', 'close'):
        amount = data.get('budgetUsd')
        amount = float(amount) if isinstance(amount, (int, float)) and amount > 0 else None
        reason = data.get('reason') if isinstance(data.get('reason'), str) else ''
        return {'decision': decision, 'budgetUsd': amount, 'reason': reason}
    if allow_ask and data.get('action') == 'ask' \
            and isinstance(data.get('question'), str) and data['question'].strip():
        return {'action': 'ask', 'question': data['question'].strip()[:300]}
    return None


def _clamped_grant_amount(task, spent, parsed_amount, worker_more=None):
    """The decision names an amount; the desk tool enforces the hard cap, so
    pre-clamp into the legal window: above what was spent, at or below the
    supervisor cap, and a floor so the gate would not refuse the next call.
    The worker's own ask feeds the default when the director named none."""
    ceiling = float(task.get('budgetUsd') or 0)
    low = round(max(0.05, float(spent) + 0.05), 2)
    default = round(max(float(spent) * 2.0, ceiling * 1.5, low,
                        (float(spent) + float(worker_more)) if worker_more else 0.0), 2)
    amount = round(float(parsed_amount), 2) if parsed_amount else default
    return min(SUPERVISOR_BUDGET_CAP_USD, max(low, amount))


def _supervisor_budget_decide(state, task):
    """The director side of the negotiation: one real mid-tier call over the
    diagnostic package plus the worker's report, or None when the model could
    not be reached. Round 1 may ask the worker ONE follow-up; round 2 must
    conclude. Deterministic mid tier: granting money is too consequential for
    the cheapest lane, but this is a bounded verdict, not synthesis."""
    draft = task.get('_supervisorBudgetNegotiationDraft') or {}
    spent, attempts = _task_budget_spent(task.get('id'))
    model = _mid_tier_slug() or _low_tier_slug()
    if not model:
        return None
    prompt = _supervisor_budget_context(state, task, spent, attempts,
                                        worker=draft.get('worker'))
    if int(draft.get('round_no') or 1) >= 2:
        prompt += (' This is your last round: conclude now; no follow-up '
                   'questions.')
    else:
        prompt += (' If you need exactly one clarification from the author '
                   'before deciding, reply with ONE JSON object only: '
                   '{"action": "ask", "question": "<one question>"}. '
                   'Otherwise reply with the decision object.')
    data = _call_openrouter_sync(
        model,
        [{'role': 'system', 'content': prompt},
         {'role': 'user', 'content': 'Give your decision.'}],
        _SUPERVISOR_BUDGET_DECISION_TOKENS)
    return _parse_supervisor_decision(
        (data.get('choices') or [{}])[0].get('message', {}).get('content'),
        allow_ask=int(draft.get('round_no') or 1) < 2)


def _supervisor_budget_step(state, decide=None, grant=None, save=None,
                            consult=None):
    """Phase 1 for at most one candidate per call: run the negotiation (worker
    report -> director verdict, with one bounded follow-up round) and stamp
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
    consult = consult or _supervisor_budget_worker_consult
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
    spent, attempts = _task_budget_spent(task.get('id'))
    # 1) the worker's account: completed work, why the budget fell short, and
    # the ask. A failed consult never blocks deciding from the record.
    worker = None
    try:
        worker = consult(state, task, spent, attempts)
    except Exception:
        worker = None
    draft = {'worker': worker, 'round_no': 1,
             'rounds': ([{'side': 'worker', 'text': (worker.get('reason')
                         or worker.get('whyOver') or '')[:300]}] if worker else [])}
    task['_supervisorBudgetNegotiationDraft'] = draft
    parsed = None
    try:
        first = decide(state, task)
    except Exception:
        first = None
    if first and first.get('decision') in ('grant', 'close'):
        parsed = first
    elif first and first.get('action') == 'ask' and first.get('question'):
        # 2) the director asked one follow-up; the worker answers it, then the
        # director concludes (no further asks).
        draft['rounds'].append({'side': 'director',
                                'text': first['question'][:300]})
        draft['round_no'] = 2
        reply = None
        try:
            reply = consult(state, task, spent, attempts,
                            question=first['question'])
        except Exception:
            reply = None
        if reply:
            draft['worker'] = dict(reply)
            draft['worker']['followup'] = first['question'][:200]
            draft['worker']['reply'] = (reply.get('reason')
                                        or reply.get('whyOver') or '')[:300]
            draft['rounds'].append({'side': 'worker', 'text':
                                    draft['worker']['reply']})
        try:
            second = decide(state, task)
        except Exception:
            second = None
        if second and second.get('decision') in ('grant', 'close'):
            parsed = second
    task.pop('_supervisorBudgetNegotiationDraft', None)
    if not parsed:
        retry['attempts'] = int(retry.get('attempts') or 0) + 1
        retry['lastAtMs'] = now_ms
        return 0
    worker_report = draft.get('worker') or {}
    task['_supervisorBudgetDecision'] = {
        'decision': parsed['decision'],
        'budgetUsd': (_clamped_grant_amount(task, spent, parsed.get('budgetUsd'),
                                            worker_report.get('moreBudgetUsd'))
                      if parsed['decision'] == 'grant' else None),
        'workerEstimateUsd': worker_report.get('moreBudgetUsd'),
        'workerCompleted': str(worker_report.get('completed') or '')[:200],
        'negotiation': draft.get('rounds') or [],
        'reason': parsed['reason'][:300], 'at': now_ms, 'applied': False}
    if save:
        save(state)
    log_action(None, 'supervisor_budget_autopilot_decided',
                {'taskId': task.get('id'), 'decision': parsed['decision'],
                 'budgetUsd': task['_supervisorBudgetDecision']['budgetUsd'],
                 'workerEstimateUsd': worker_report.get('moreBudgetUsd'),
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
    estimate = decision.get('workerEstimateUsd')
    reason = (f'autopilot grant as {director_id}: {decision["reason"]} '
              f'(ceiling ${decision["budgetUsd"]:.2f} after exhausting '
              f'${float(task.get("budgetUsd") or 0):.2f}'
              + (f'; the author reported {decision.get("workerCompleted") or "no progress"}'
                 f' and estimated ${estimate:.2f} more'
                 if estimate else '') + ')')
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
